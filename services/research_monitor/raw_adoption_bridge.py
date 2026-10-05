"""Bridge raw answer-adoption rows into GEO source signals.

This module is intentionally narrow: it converts already-detected answer
adoption evidence in geo_research_raw into source-signal payloads. It does not
write to the database and does not infer adoption from search exposure alone.
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.media_entity_flywheel import normalize_domain
from services.research_monitor.source_signal_classifier import classify_source_signal
from services.research_monitor.source_signal_weighting import weighted_signal_value


def _sha1(value: str) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y"}:
        return True
    return False


def _has_adoption_rank(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return True


def should_bridge_raw_adoption(row: dict[str, Any]) -> bool:
    """Return True only for rows with explicit answer adoption evidence."""
    if not (row.get("cite_url") or row.get("source_url") or row.get("url")):
        return False
    return _truthy(row.get("is_answer_cited")) or _has_adoption_rank(row.get("adoption_rank"))


def build_raw_adoption_source_signal_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Convert one raw adoption row into an upsert_source_signal payload."""
    if not should_bridge_raw_adoption(row):
        raise ValueError("raw row does not contain answer adoption evidence")

    normalized_row = dict(row)
    # adoption_rank is persisted evidence from the answer footnote parser.  Make
    # it visible to the existing classifier without treating plain cite_url as
    # adoption.
    normalized_row["answer_adopted"] = True

    signal = classify_source_signal(normalized_row)
    metadata = dict(signal.metadata or {})
    metadata.update({
        "source": "geo_research_raw_answer_bridge",
        "raw_id": row.get("id"),
        "batch_id": row.get("batch_id"),
        "researcher": row.get("researcher"),
        "adoption_rank": row.get("adoption_rank"),
    })
    if row.get("created_at"):
        metadata["raw_created_at"] = str(row.get("created_at"))

    return {
        "source_url": signal.source_url,
        "url_hash": _sha1(signal.source_url),
        "domain": normalize_domain(signal.source_url),
        "industry_key": signal.industry_key,
        "engine": signal.engine,
        "prompt_id": signal.prompt_id,
        "signal_tier": signal.signal_tier,
        "source_position": signal.source_position,
        "total_sources_in_answer": signal.total_sources_in_answer,
        "balanced_weight": weighted_signal_value(signal),
        "answer_mentioned_brand": signal.answer_mentioned_brand,
        "round_id": row.get("batch_id") or metadata.get("round_id") or "",
        "article_id": row.get("article_id"),
        "metadata": metadata,
    }
