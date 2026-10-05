"""Reviewed media flywheel handoff for publish recommendations.

This module is intentionally narrow: it only consumes approved media-entity
bindings after an industry takeover policy is marked ready and the admin
feature switch is enabled.  Otherwise placement falls back to the legacy
recommendation engine.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from services.media_entity_flywheel import normalize_industry_key
from writing.feature_switches import is_feature_enabled

logger = logging.getLogger("GEO-MediaFlywheelRec")


def _empty_result(
    *,
    industry_key: str,
    reason: str,
    media_type: str,
) -> dict[str, Any]:
    return {
        "used": False,
        "reason": reason,
        "recommendation_source": "legacy",
        "matched_industry": industry_key,
        "media_type": media_type,
        "vertical": [],
        "generic": [],
    }


def _canonical_media_source(media_source: str, media_type: str = "") -> str:
    raw = str(media_source or "").strip().lower()
    if raw in {"mhz_wemedia", "wemedia", "自媒体"} or media_type == "wemedia":
        return "mhz_wemedia"
    return "mhz_media"


def _wanted_source(media_type: str) -> str:
    return "mhz_wemedia" if media_type == "wemedia" else "mhz_media"


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _load_ready_policy(industry_key: str) -> dict[str, Any] | None:
    from db.media_entity_flywheel_db import get_media_takeover_policy

    keys = [industry_key or "general"]
    if keys[0] != "general":
        keys.append("general")
    for key in keys:
        policy = get_media_takeover_policy(key)
        if policy and policy.get("status") == "ready_shadow":
            return policy
    return None


def _load_flywheel_rows(
    *,
    industry_key: str,
    media_type: str,
    limit: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    from db.media_entity_flywheel_db import (
        get_media_inventory_item,
        list_approved_media_binding_candidates,
    )

    wanted = _wanted_source(media_type)
    keys = [industry_key or "general"]
    if keys[0] != "general":
        keys.append("general")
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, int]] = set()
    for key in keys:
        bindings = list_approved_media_binding_candidates(industry_key=key, limit=max(limit * 3, 20))
        for binding in bindings:
            source = _canonical_media_source(binding.get("media_source"), media_type="")
            if source != wanted:
                continue
            inventory_id = _safe_int(binding.get("inventory_id"))
            if inventory_id <= 0:
                continue
            dedupe_key = (source, inventory_id)
            if dedupe_key in seen:
                continue
            inventory = get_media_inventory_item(source, inventory_id)
            if not inventory or inventory.get("is_active") is False:
                continue
            seen.add(dedupe_key)
            rows.append((binding, inventory))

    # [B6-1] 接管排序改读 shadow_score(现只用 match_confidence):批量取 shadow_score 附到 binding,
    # 按 shadow_score(0-100,回退 match_confidence*100)降序排,再截 limit。全在 media_takeover flag 后。
    try:
        from db.media_entity_flywheel_db import get_shadow_scores_by_entity_keys
        ekeys = [b.get("entity_key") for (b, _inv) in rows if b.get("entity_key")]
        smap = get_shadow_scores_by_entity_keys(ekeys, industry_key)
        for (b, _inv) in rows:
            ek = b.get("entity_key")
            if ek in smap:
                b["shadow_score"] = smap[ek]
    except Exception as e:
        logger.warning(f"[B6-1] shadow_score 批量取失败(降级 match_confidence 排序): {e}")

    def _rank_key(bi: tuple[dict[str, Any], dict[str, Any]]) -> float:
        b = bi[0]
        sv = b.get("shadow_score")
        if sv is not None:
            return _safe_float(sv)
        return _safe_float(b.get("match_confidence")) * 100.0

    rows.sort(key=_rank_key, reverse=True)
    return rows[:limit]


def _build_reason(binding: dict[str, Any], inventory: dict[str, Any]) -> str:
    parts = ["飞轮审核绑定"]
    confidence = _safe_float(binding.get("match_confidence"))
    if confidence:
        parts.append(f"匹配度 {confidence:.0%}")
    portal = str(inventory.get("portal_media") or inventory.get("platform") or "").strip()
    if portal:
        parts.append(portal)
    category = str(inventory.get("resource_type_name") or inventory.get("industry") or "").strip()
    if category:
        parts.append(category)
    return " · ".join(parts)


def _build_recommendation(
    *,
    binding: dict[str, Any],
    inventory: dict[str, Any],
    policy_id: int,
    media_type: str,
) -> dict[str, Any]:
    source = _canonical_media_source(binding.get("media_source"), media_type)
    media_id = _safe_int(inventory.get("inventory_id") or inventory.get("id") or binding.get("inventory_id"))
    price = _safe_float(inventory.get("price"))
    # [B6-1] 质量/排序信号改读 shadow_score(0-100 归一 0-1);无 shadow 快照 → 回退 match_confidence。
    match_conf = _safe_float(binding.get("match_confidence"))
    shadow = binding.get("shadow_score")
    if shadow is not None:
        quality = max(0.0, min(1.0, _safe_float(shadow) / 100.0))
    else:
        quality = match_conf
    return {
        "media_id": media_id,
        "media_name": inventory.get("media_name") or binding.get("media_name") or "",
        "platform": inventory.get("portal_media") or inventory.get("platform") or binding.get("media_name") or "",
        "category": inventory.get("resource_type_name") or inventory.get("industry") or "",
        "price": price,
        "our_price_points": _safe_int(inventory.get("our_price_points")),
        "our_price_yuan": _safe_float(inventory.get("our_price_yuan")),
        "inclusion_rate": inventory.get("inclusion_rate") or "",
        "authority_media": _safe_int(inventory.get("authority_media")),
        "geo_rank": _safe_int(inventory.get("geo_rank")),
        "geo_rank_platform": inventory.get("geo_rank_platform") or "",
        "geo_engine_coverage": 0,
        "portal_media": inventory.get("portal_media") or inventory.get("platform") or "",
        "entrance_link": inventory.get("entrance_link") or "",
        "engines": [],
        "geo_score": round(quality * 100, 3),
        "citation_rate": 0,
        "quality_score_v2f": round(quality, 3),
        "flywheel_shadow_score": _safe_float(shadow) if shadow is not None else None,  # 观测
        "flywheel_match_confidence": round(match_conf, 3),  # 观测
        "reason": _build_reason(binding, inventory),
        "media_type": media_type,
        "source": "media_flywheel",
        "recommendation_source": "media_flywheel",
        "flywheel_entity_key": binding.get("entity_key") or "",
        "flywheel_binding_id": binding.get("id"),
        "flywheel_policy_id": policy_id,
        "flywheel_media_source": source,
    }


def _excluded_ids(values: Iterable[Any] | None) -> set[int]:
    excluded: set[int] = set()
    for item in values or []:
        val = _safe_int(item)
        if val > 0:
            excluded.add(val)
    return excluded


def recommend_from_media_flywheel(
    *,
    industry: str,
    media_type: str,
    limit: int,
    exclude_media_ids: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Return approved flywheel recommendations or a legacy-fallback signal."""

    normalized_type = "wemedia" if media_type == "wemedia" else "media"
    industry_key = normalize_industry_key(industry or "")
    if not is_feature_enabled("media_takeover"):
        return _empty_result(
            industry_key=industry_key,
            reason="media_takeover_disabled",
            media_type=normalized_type,
        )

    policy = _load_ready_policy(industry_key)
    if not policy:
        return _empty_result(
            industry_key=industry_key,
            reason="media_takeover_policy_not_ready",
            media_type=normalized_type,
        )

    rows = _load_flywheel_rows(
        industry_key=industry_key,
        media_type=normalized_type,
        limit=max(limit * 2, 10),
    )
    excluded = _excluded_ids(exclude_media_ids)
    vertical: list[dict[str, Any]] = []
    generic: list[dict[str, Any]] = []
    policy_id = _safe_int(policy.get("id"))
    for binding, inventory in rows:
        rec = _build_recommendation(
            binding=binding,
            inventory=inventory,
            policy_id=policy_id,
            media_type=normalized_type,
        )
        if rec["media_id"] in excluded:
            continue
        target = generic if binding.get("industry_key") == "general" else vertical
        target.append(rec)
        if len(vertical) + len(generic) >= limit:
            break

    if not vertical and not generic:
        return _empty_result(
            industry_key=industry_key,
            reason="media_flywheel_no_usable_bindings",
            media_type=normalized_type,
        )

    return {
        "used": True,
        "reason": "media_flywheel_takeover",
        "recommendation_source": "media_flywheel",
        "matched_industry": industry_key,
        "media_type": normalized_type,
        "policy_id": policy_id,
        "vertical": vertical,
        "generic": generic,
    }
