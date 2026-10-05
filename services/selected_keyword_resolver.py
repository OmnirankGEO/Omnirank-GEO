"""Resolve keyword-selection local ids to stable keyword text."""

from __future__ import annotations

import json
from typing import Any


def safe_json_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def safe_json_dict(value: Any) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _add_keyword(id_to_keyword: dict[int, str], item: Any) -> None:
    if not isinstance(item, dict):
        return
    local_id = _int_or_none(item.get("id"))
    keyword = str(item.get("keyword") or "").strip()
    if local_id is None or not keyword:
        return
    id_to_keyword.setdefault(local_id, keyword)


def _collect_cluster_keywords(clusters_data: dict) -> list[dict]:
    items: list[dict] = []
    for cluster in clusters_data.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        for key in ("core_keywords", "covered_keywords"):
            values = cluster.get(key) or []
            if isinstance(values, list):
                items.extend(v for v in values if isinstance(v, dict))
    unclustered = clusters_data.get("unclustered_keywords") or []
    if isinstance(unclustered, list):
        items.extend(v for v in unclustered if isinstance(v, dict))
    return items


def resolve_selected_keyword_texts(
    selected_keyword_ids: Any,
    *,
    keywords_snapshot: Any = None,
    pricing_data: Any = None,
    clusters_data: Any = None,
) -> list[str]:
    """Map session-local selected ids to keyword text.

    keyword_selection_sessions.final_keyword_ids stores ids from the selection
    snapshot/pricing payload, not confirmed_keywords.id.
    """
    selected_ids = safe_json_list(selected_keyword_ids)
    id_to_keyword: dict[int, str] = {}

    for item in safe_json_list(keywords_snapshot):
        _add_keyword(id_to_keyword, item)

    pricing = safe_json_dict(pricing_data)
    for item in pricing.get("keywords") or []:
        _add_keyword(id_to_keyword, item)

    clusters = safe_json_dict(clusters_data)
    for item in _collect_cluster_keywords(clusters):
        _add_keyword(id_to_keyword, item)

    result: list[str] = []
    seen: set[str] = set()
    for raw_id in selected_ids:
        local_id = _int_or_none(raw_id)
        keyword = id_to_keyword.get(local_id) if local_id is not None else str(raw_id or "").strip()
        if keyword and keyword not in seen:
            result.append(keyword)
            seen.add(keyword)
    return result
