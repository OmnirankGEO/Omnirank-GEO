"""Shared contract for unresolved monitoring identity cells.

The monitoring result remains the business-fact SSOT.  These constants keep
writers, aggregates and human-review endpoints on one fail-closed vocabulary.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final


PENDING_RESPONSE_STATUS: Final = "brand_identity_unresolved"
PENDING_MENTION_TYPE: Final = "pending_identity"
PENDING_REVIEW_STATE: Final = "pending"
AGGREGATE_ELIGIBLE_SQL: Final = (
    "COALESCE({alias}identity_review_state, 'not_required') <> 'pending' "
    "AND COALESCE({alias}response_status, 'legacy_unknown') "
    "<> 'brand_identity_unresolved' "
    "AND COALESCE({alias}mention_type, 'none') <> 'pending_identity'"
)

_NAME_SEPARATORS = re.compile(r"[\s\u3000·•・,，.。;；:：'\"“”‘’\-_/\\（）()【】\[\]《》<>]+")


def aggregate_eligible_sql(alias: str = "") -> str:
    """Return the fixed SQL predicate used by every monitoring aggregate."""
    prefix = f"{alias}." if alias else ""
    return AGGREGATE_ELIGIBLE_SQL.format(alias=prefix)


def is_pending_identity_result(value: Any) -> bool:
    """Recognize every persisted/runtime marker for one unresolved identity cell."""
    if not isinstance(value, dict):
        return False
    return any(
        (
            value.get("identity_review_state") == PENDING_REVIEW_STATE,
            value.get("response_status") == PENDING_RESPONSE_STATUS,
            value.get("mention_type") == PENDING_MENTION_TYPE,
            value.get("status") == "pending_identity",
        )
    )


def normalize_review_name(value: str) -> str:
    return _NAME_SEPARATORS.sub("", str(value or "").strip()).casefold()


def build_evidence_hash(
    *,
    brand_id: int,
    platform: str,
    keyword: str,
    candidates: list[str],
    evidence_snippet: str,
    full_response: str,
) -> str:
    payload: dict[str, Any] = {
        "brand_id": int(brand_id),
        "platform": str(platform or ""),
        "keyword": str(keyword or ""),
        "candidates": [str(item) for item in candidates],
        "evidence_snippet": str(evidence_snippet or ""),
        "response_sha256": hashlib.sha256(
            str(full_response or "").encode("utf-8")
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def bounded_identity_candidates(raw: Any, *, limit: int = 5) -> list[str]:
    values = raw if isinstance(raw, (list, tuple)) else []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = str(item or "").strip()
        normalized = normalize_review_name(value)
        if not normalized or normalized in seen or len(value) > 160:
            continue
        seen.add(normalized)
        result.append(value)
        if len(result) >= limit:
            break
    return result
