"""Immutable commercial-service relationship versions.

``customer_agent_bindings`` remains the legacy current-state projection.  This
ledger preserves every source fact before that projection is replaced or cleared.
Only end-of-validity fields on an active version may be updated.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


def _dict(row: Any) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


def close_current_version(
    cur,
    *,
    customer_user_id: int,
    projection: Optional[Dict[str, Any]],
    operator_user_id: int,
    reason: str,
    request_id: str,
) -> Optional[int]:
    """Snapshot a legacy projection if needed, then end the active version."""
    cur.execute(
        """SELECT * FROM customer_agent_binding_history
           WHERE customer_user_id=%s AND effective_to IS NULL FOR UPDATE""",
        (int(customer_user_id),),
    )
    active = _dict(cur.fetchone())
    if active and projection:
        if (
            active["relationship_state"] != "service_provider"
            or int(active["provider_user_id"]) != int(projection["agent_user_id"])
        ):
            raise RuntimeError("商业关系历史与当前投影不一致，已拒绝覆盖证据")
    if not active and projection:
        cur.execute(
            """INSERT INTO customer_agent_binding_history(
                   customer_user_id,provider_user_id,relationship_state,source_binding_id,
                   binding_source,source_token,effective_from,dispute_status,dispute_note,
                   evidence_jsonb
               ) VALUES (%s,%s,'service_provider',%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s::jsonb)
               RETURNING id""",
            (
                int(customer_user_id), int(projection["agent_user_id"]), projection.get("id"),
                projection.get("binding_source"), projection.get("source_token"),
                projection.get("bound_at"), projection.get("dispute_status"),
                projection.get("dispute_note"),
                json.dumps({"origin": "legacy_current_projection"}, separators=(",", ":")),
            ),
        )
        active = {"id": int(cur.fetchone()["id"])}
    if not active:
        return None
    cur.execute(
        """UPDATE customer_agent_binding_history
           SET effective_to=NOW(),ended_by_operator_user_id=%s,ended_reason=%s,ended_request_id=%s
           WHERE id=%s AND effective_to IS NULL RETURNING id""",
        (int(operator_user_id), reason, request_id, int(active["id"])),
    )
    ended = cur.fetchone()
    if not ended:
        raise RuntimeError("商业关系历史已被并发结束")
    return int(ended["id"])


def append_current_version(
    cur,
    *,
    customer_user_id: int,
    provider_user_id: Optional[int],
    source_binding_id: Optional[int],
    operator_user_id: int,
    reason: str,
    request_id: str,
    binding_source: str = "admin_manual",
    source_token: Optional[str] = None,
) -> int:
    state = "service_provider" if provider_user_id is not None else "platform_direct"
    cur.execute(
        """INSERT INTO customer_agent_binding_history(
               customer_user_id,provider_user_id,relationship_state,source_binding_id,
               binding_source,source_token,effective_from,created_by_operator_user_id,
               created_reason,created_request_id,evidence_jsonb
           ) VALUES (%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s,
                     '{"registration_attribution_untouched":true}'::jsonb)
           RETURNING id""",
        (
            int(customer_user_id), provider_user_id, state, source_binding_id,
            binding_source, source_token,
            int(operator_user_id), reason, request_id,
        ),
    )
    return int(cur.fetchone()["id"])


def record_automatic_binding(
    cur,
    *,
    customer_user_id: int,
    provider_user_id: int,
    source_binding_id: int,
    binding_source: str,
    source_token: Optional[str],
) -> Optional[int]:
    """Version a newly-created customer binding in the writer transaction.

    Compatibility environments that have not applied this additive migration are
    left unchanged; production prestart and web startup both enforce the table.
    """
    cur.execute("SELECT to_regclass('customer_agent_binding_history') AS reg")
    row = cur.fetchone()
    reg = row.get("reg") if isinstance(row, dict) else (row[0] if row else None)
    if reg is None:
        return None
    cur.execute(
        """SELECT id,relationship_state,provider_user_id
           FROM customer_agent_binding_history
           WHERE customer_user_id=%s AND effective_to IS NULL FOR UPDATE""",
        (int(customer_user_id),),
    )
    active = _dict(cur.fetchone())
    if active:
        if active["relationship_state"] != "platform_direct":
            raise RuntimeError("新绑定与现役商业关系历史冲突")
        cur.execute(
            """UPDATE customer_agent_binding_history
               SET effective_to=NOW(),ended_reason='客户通过有效入口建立商业服务绑定'
               WHERE id=%s AND effective_to IS NULL""",
            (int(active["id"]),),
        )
    cur.execute(
        """INSERT INTO customer_agent_binding_history(
               customer_user_id,provider_user_id,relationship_state,source_binding_id,
               binding_source,source_token,effective_from,evidence_jsonb
           ) VALUES (%s,%s,'service_provider',%s,%s,%s,NOW(),
                     '{"origin":"customer_binding_writer"}'::jsonb)
           RETURNING id""",
        (
            int(customer_user_id), int(provider_user_id), int(source_binding_id),
            binding_source, source_token,
        ),
    )
    return int(cur.fetchone()["id"])
