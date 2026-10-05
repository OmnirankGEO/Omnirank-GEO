"""Append content taboos without overwriting existing user facts.

P1-SEC-2 fix(Audit B · SSOT §2.1.b 铁律):
修前:不论 source 直接 auto write + auto status · 违反 "ai_inferred 必须 pending_confirm 不允许直改 profile" 铁律。
修后:user_explicit → auto · ai_inferred → pending_confirm 走 ABCD popover 给用户确认。
"""

from __future__ import annotations

from typing import Any

from db.intake_db import append_profile_social_field_items
from db.profile_memory_db import record_profile_memory_event


async def update_profile_taboo(
    *,
    profile_id: str,
    taboo: str,
    source: str,
    confidence: float = 0.9,
    ctx_user_id: int | None = None,
) -> dict[str, Any]:
    # P1-SEC-2 fix · ai_inferred 必经 pending_confirm · 不直改 social_fields.content_taboo
    if source == "ai_inferred":
        event_id = record_profile_memory_event(
            profile_id=profile_id,
            source="agent_loop",
            event_type="taboo",
            title="待确认 · 内容禁区",
            text=taboo,
            dimension="content_taboo",
            canonical_concept="guardrails",
            raw_payload={"tool": "update_profile_taboo", "source": source, "value": taboo},
            confidence=confidence,
            user_id=ctx_user_id,
            review_status="pending",
        )
        return {
            "status": "pending_confirm",
            "event_id": event_id,
            "taboo": taboo,
            "requires_user_ack": True,
        }

    # user_explicit · 直写 social_fields + 写 memory event(auto)
    changed = append_profile_social_field_items(
        profile_id,
        "content_taboo",
        [{"value": taboo, "meta": {"source": source}}],
        source="agent_loop",
    )
    event_id = record_profile_memory_event(
        profile_id=profile_id,
        source="agent_loop",
        event_type="taboo",
        title="内容禁区",
        text=taboo,
        dimension="content_taboo",
        canonical_concept="guardrails",
        raw_payload={"tool": "update_profile_taboo", "source": source},
        confidence=confidence,
        user_id=ctx_user_id,
        review_status="auto",
    )
    return {"status": "auto", "changed": bool(changed), "event_id": event_id}
