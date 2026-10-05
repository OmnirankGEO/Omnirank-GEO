"""Server-side evidence snapshots for GEO acquisition content."""
from __future__ import annotations

import hashlib
import json
from typing import Optional

EVIDENCE_SOURCE_TYPES = ("none", "system_facts", "brand_facts", "diagnosis", "latest_diagnosis")
FACT_PROVENANCE = ("customer_asserted", "system_verified", "brand_asserted", "diagnosis")

# 目前可核验的系统 SSOT:已发布诊断记录的评分与等级。
_SYSTEM_FACT_SOURCE_NOTE = "依据平台系统记录核验的事实，生成时已冻结"
_BRAND_FACT_SOURCE_NOTE = "品牌方提供的事实，未经平台核验，生成时已冻结"


def _digest(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _facts_projection(facts: Optional[list]) -> list[dict]:
    """Live-check comparison shape: only (key,label,value), never provenance.

    v1 snapshots froze three-key facts ``{key,label,value}``; v2 stamps a
    fourth ``provenance`` key.  The freeze→live comparison must accept both
    generations, so provenance (a freeze-time stamp, not source data) never
    participates in the digest.
    """
    projection: list[dict] = []
    for fact in facts or []:
        if isinstance(fact, dict):
            projection.append({
                "key": str(fact.get("key") or ""),
                "label": str(fact.get("label") or ""),
                "value": fact.get("value"),
            })
        else:
            projection.append({"key": "", "label": "", "value": fact})
    return projection


def _comparison_core(source_type: str, source_id, brand_id, facts: list,
                     organization_id, membership_id) -> dict:
    return {
        "source_type": str(source_type or ""),
        "source_id": int(source_id),
        "brand_id": int(brand_id),
        "facts": _facts_projection(facts),
        "organization_id": int(organization_id) if organization_id is not None else None,
        "created_by_membership_id": int(membership_id) if membership_id is not None else None,
    }


def _normalize_facts(facts: Optional[list], *, default_provenance: str) -> list[dict]:
    """Bound user-supplied fact entries and stamp provenance on every item."""
    normalized: list[dict] = []
    for fact in facts or []:
        if isinstance(fact, dict):
            key = str(fact.get("key") or "")[:60]
            label = str(fact.get("label") or "")[:80]
            value = fact.get("value")
            provenance = str(fact.get("provenance") or "")
        else:
            key, label, value, provenance = "", "", fact, ""
        if provenance not in FACT_PROVENANCE:
            provenance = default_provenance
        if isinstance(value, str):
            value = value[:120]
        if value in (None, "") and not label:
            continue
        normalized.append({"key": key, "label": label, "value": value, "provenance": provenance})
    return normalized[:20]


def _diagnosis_facts(row: dict, *, provenance: str = "diagnosis") -> list[dict]:
    facts = []
    if row.get("total_score") is not None:
        facts.append({
            "key": "geo_score", "label": "GEO 诊断评分",
            "value": int(row["total_score"]), "provenance": provenance,
        })
    if row.get("level"):
        facts.append({
            "key": "diagnosis_level", "label": "诊断等级",
            "value": str(row["level"])[:40], "provenance": provenance,
        })
    return facts


def _verify_system_facts(row: dict, facts: Optional[list]) -> list[dict]:
    """Only internal numbers that match the live SSOT may be frozen."""
    verified: list[dict] = []
    ssot = {fact["key"]: fact for fact in _diagnosis_facts(row, provenance="system_verified")}
    for fact in _normalize_facts(facts, default_provenance="system_verified"):
        expected = ssot.get(fact["key"])
        if expected is None or str(expected["value"]) != str(fact["value"]):
            raise ValueError("system_fact_not_verifiable")
        verified.append(expected)
    if not verified:
        raise ValueError("system_fact_not_verifiable")
    return verified


def live_brand_snapshot(brand_id: int) -> dict:
    """Return the minimum brand facts needed by prompts, rejecting tombstones."""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id,name,company_name,industry,owner_user_id,created_at,updated_at
            FROM brands
            WHERE id=%s AND COALESCE(is_deleted,FALSE)=FALSE
            """,
            (int(brand_id),),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("brand_deleted_or_missing")
        return {
            "id": int(row["id"]),
            "name": str(row.get("name") or row.get("company_name") or "")[:120],
            "company_name": str(row.get("company_name") or "")[:160],
            "industry": str(row.get("industry") or "")[:120],
            "owner_user_id": int(row["owner_user_id"]),
            "source_updated_at": row.get("updated_at") or row.get("created_at"),
        }
    finally:
        conn.close()


def freeze_evidence(
    *,
    brand_id: Optional[int],
    source_type: str = "none",
    diagnosis_id: Optional[int] = None,
    organization_id: Optional[int] = None,
    membership_id: Optional[int] = None,
    facts: Optional[list] = None,
) -> dict:
    """Freeze only published, non-deleted facts selected by the user.

    ``latest_diagnosis`` is resolved once here and converted to a concrete id;
    historic output never follows a newer diagnosis later.

    Four-way source model:
    ``none`` (evergreen, never blocks), ``brand_facts`` (caller-asserted brand
    facts, provenance ``brand_asserted``, no system verification),
    ``system_facts`` (caller-supplied internal numbers verified against the
    published-diagnosis SSOT, provenance ``system_verified``), and
    ``diagnosis``/``latest_diagnosis`` (frozen published diagnosis,
    provenance ``diagnosis``).
    """
    source_type = str(source_type or "none").strip()
    if source_type == "none":
        result = {
            "source_type": "none",
            "source_id": None,
            "facts": [],
            "source_note": "本内容未引用客户诊断数字",
            "chat_authorized": False,
        }
        result["snapshot_hash"] = _digest(result)
        return result
    if source_type not in EVIDENCE_SOURCE_TYPES:
        raise ValueError("unsupported_evidence_source")
    if source_type == "brand_facts":
        result = {
            "source_type": "brand_facts",
            "source_id": None,
            "brand_id": int(brand_id) if brand_id else None,
            "facts": _normalize_facts(facts, default_provenance="brand_asserted"),
            "source_note": _BRAND_FACT_SOURCE_NOTE,
            "chat_authorized": False,
        }
        result["snapshot_hash"] = _digest(result)
        return result
    if not brand_id:
        raise ValueError("evidence_brand_required")
    if (organization_id is None) != (membership_id is None):
        raise ValueError("evidence_organization_scope_incomplete")

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list = [int(brand_id)]
        scope_sql = ""
        if organization_id is not None and membership_id is not None:
            scope_sql = " AND d.organization_id=%s AND d.created_by_membership_id=%s"
            params.extend([int(organization_id), int(membership_id)])
        where_id = ""
        if source_type == "diagnosis":
            if not diagnosis_id:
                raise ValueError("diagnosis_id_required")
            where_id = " AND d.id=%s"
            params.append(int(diagnosis_id))
        cur.execute(
            f"""
            SELECT d.id,d.brand_id,d.total_score,d.level,d.created_at
            FROM diagnosis_records d
            JOIN brands b ON b.id=d.brand_id
            WHERE d.brand_id=%s
              AND COALESCE(d.is_deleted,FALSE)=FALSE
              AND COALESCE(b.is_deleted,FALSE)=FALSE
              AND (d.result_visibility IS NULL OR d.result_visibility='published')
              {scope_sql}
              {where_id}
            ORDER BY d.created_at DESC,d.id DESC LIMIT 1
            """,
            tuple(params),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("published_diagnosis_evidence_not_found")
        if source_type == "system_facts":
            frozen_facts = _verify_system_facts(row, facts)
            source_note = _SYSTEM_FACT_SOURCE_NOTE
        else:
            frozen_facts = _diagnosis_facts(row)
            source_note = "依据本次已发布诊断结果，生成时已冻结"
        result = {
            "source_type": source_type if source_type != "latest_diagnosis" else "diagnosis",
            "source_id": int(row["id"]),
            "brand_id": int(row["brand_id"]),
            "source_created_at": row.get("created_at"),
            "facts": frozen_facts,
            "source_note": source_note,
            "chat_authorized": False,
            "organization_id": int(organization_id) if organization_id is not None else None,
            "created_by_membership_id": int(membership_id) if membership_id is not None else None,
        }
        result["snapshot_hash"] = _digest(result)
        return result
    finally:
        conn.close()


def require_frozen_evidence_live(snapshot: dict) -> None:
    """Reject generation/read after source deletion, withholding, or mutation."""
    source_type = (snapshot or {}).get("source_type")
    if source_type in {"none", "brand_facts"}:
        # 常青与品牌方自述事实不引用系统内部数字，无需核验系统 SSOT。
        return
    source_id = (snapshot or {}).get("source_id")
    brand_id = (snapshot or {}).get("brand_id")
    if not source_id or not brand_id:
        raise ValueError("evidence_snapshot_incomplete")
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        scope_sql = ""
        params: list = [int(source_id), int(brand_id)]
        organization_id = snapshot.get("organization_id")
        membership_id = snapshot.get("created_by_membership_id")
        if (organization_id is None) != (membership_id is None):
            raise ValueError("evidence_snapshot_scope_incomplete")
        if organization_id is not None and membership_id is not None:
            scope_sql = " AND d.organization_id=%s AND d.created_by_membership_id=%s"
            params.extend([int(organization_id), int(membership_id)])
        cur.execute(
            f"""
            SELECT d.id,d.brand_id,d.total_score,d.level,d.created_at
            FROM diagnosis_records d
            JOIN brands b ON b.id=d.brand_id
            WHERE d.id=%s AND d.brand_id=%s
              AND COALESCE(d.is_deleted,FALSE)=FALSE
              AND COALESCE(b.is_deleted,FALSE)=FALSE
              AND (d.result_visibility IS NULL OR d.result_visibility='published')
              {scope_sql}
            """,
            tuple(params),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("evidence_revoked_or_deleted")
        if source_type == "system_facts":
            try:
                facts = _verify_system_facts(row, list(snapshot.get("facts") or []))
            except ValueError:
                raise ValueError("evidence_changed_since_freeze") from None
        else:
            facts = _diagnosis_facts(row)
        # v1 冻结的 facts 是三键 {key,label,value},v2 起多了 provenance 戳。
        # 比对只看 (key,label,value) 投影 + 来源/范围锚点:provenance 是冻结时
        # 打上的出处标记而非来源数据,不参与 digest——新旧两代快照都能过。
        live_core = _comparison_core(
            source_type, row["id"], row["brand_id"], facts,
            organization_id, membership_id,
        )
        frozen_core = _comparison_core(
            snapshot.get("source_type"), snapshot.get("source_id"), snapshot.get("brand_id"),
            list(snapshot.get("facts") or []),
            snapshot.get("organization_id"), snapshot.get("created_by_membership_id"),
        )
        if _digest(live_core) != _digest(frozen_core):
            raise ValueError("evidence_changed_since_freeze")
    finally:
        conn.close()
