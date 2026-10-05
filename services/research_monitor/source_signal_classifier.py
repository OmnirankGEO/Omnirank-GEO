"""Classify GEO research rows into trusted source signal tiers."""

from __future__ import annotations

import re
from typing import Any

from services.media_entity_flywheel import normalize_domain, normalize_industry_key
from services.research_monitor.source_signal_weighting import SourceSignal, normalize_signal_tier


def answer_mentions_source(answer_text: str, source_title: str = "", domain: str = "") -> bool:
    answer = answer_text or ""
    title = source_title or ""
    clean_title = re.sub(r"\s+", "", title)
    clean_answer = re.sub(r"\s+", "", answer)
    if clean_title and len(clean_title) >= 8 and clean_title in clean_answer:
        return True
    if clean_title and len(clean_title) >= 12:
        for i in range(0, max(1, len(clean_title) - 7)):
            if clean_title[i:i + 8] in clean_answer:
                return True
    return False


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "y"}:
        return True
    return False


def _int_or_default(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def classify_source_signal(row: dict[str, Any]) -> SourceSignal:
    """Create a SourceSignal from geo_research_raw/article/citation-like row."""
    url = row.get("cite_url") or row.get("url") or row.get("source_url") or ""
    title = row.get("cite_title") or row.get("title") or ""
    has_search_url = bool(url)
    has_true_citation_fk = bool(row.get("citation_id") or row.get("source_citation_id") or row.get("article_id"))
    adopted = _truthy(row.get("is_answer_cited")) or _truthy(row.get("answer_adopted"))
    hard_rejected = row.get("review_status") == "rejected" or row.get("domain_tier") == "blacklist"
    failed_body_quality = (
        row.get("clean_status") == "failed"
        or _int_or_default(row.get("cleaned_char_count"), 500) < 500
    )

    if hard_rejected:
        tier = "rejected_noise"
    elif failed_body_quality and adopted:
        tier = "cited_source"
    elif failed_body_quality:
        tier = "rejected_noise"
    elif adopted:
        tier = "answer_adopted"
    elif has_true_citation_fk:
        tier = "cited_source"
    elif has_search_url or row.get("search_rank") or row.get("search_result"):
        tier = "search_result_only"
    else:
        tier = "crawled_reference_only"

    return SourceSignal(
        source_url=url,
        engine=(row.get("engine") or row.get("platform") or "").lower(),
        prompt_id=str(row.get("prompt_id") or row.get("query") or row.get("round_call_id") or ""),
        signal_tier=normalize_signal_tier(tier),
        source_position=_int_or_default(row.get("search_rank") or row.get("cite_position") or row.get("source_position"), 1),
        total_sources_in_answer=_int_or_default(row.get("total_sources_in_answer") or row.get("citations_count"), 1),
        industry_key=normalize_industry_key(row.get("industry") or row.get("primary_industry") or ""),
        answer_mentioned_brand=_truthy(row.get("answer_mentioned_brand")),
        metadata={
            "title": title,
            "domain": normalize_domain(url),
            "round_id": row.get("round_id") or row.get("batch_id"),
            "adoption_rank": row.get("adoption_rank"),
        },
    )
