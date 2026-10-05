"""Quality scoring for crawled GEO research articles."""

from __future__ import annotations

from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def score_article_quality(article: dict[str, Any]) -> dict[str, Any]:
    cleaned_chars = _num(article.get("cleaned_char_count") or article.get("raw_char_count"), 0)
    cleanliness = _num(article.get("cleanliness_score"), 60)
    citation_count = _num(article.get("total_citation_count"), 0)
    review_status = article.get("review_status") or "crawled"
    domain_tier = article.get("domain_tier") or "gray"
    content_type = article.get("content_type") or "article"

    length_score = min(100.0, cleaned_chars / 30)
    citation_score = min(100.0, citation_count * 12)
    review_bonus = 12 if review_status in {"in_library", "approved", "imported_to_reference"} else 0
    tier_bonus = {"whitelist": 12, "gray": 2, "blacklist": -35}.get(domain_tier, 0)
    type_bonus = 8 if content_type in {"article", "doc_tool"} else (-10 if content_type == "ecom" else 0)

    score = cleanliness * 0.35 + length_score * 0.20 + citation_score * 0.25 + review_bonus + tier_bonus + type_bonus
    risk_tags: list[str] = []
    if cleaned_chars < 500:
        risk_tags.append("too_short")
    if domain_tier == "blacklist":
        risk_tags.append("blacklisted_domain")
    if review_status == "rejected":
        risk_tags.append("rejected_by_admin")
    if content_type in {"ecom", "video", "other"}:
        risk_tags.append(f"weak_content_type:{content_type}")

    return {
        "quality_score": round(max(0.0, min(100.0, score)), 2),
        "risk_tags": risk_tags,
        "usable_for_writing": not risk_tags or risk_tags == ["weak_content_type:video"],
        "quality_components": {
            "cleanliness": cleanliness,
            "length_score": round(length_score, 2),
            "citation_score": round(citation_score, 2),
            "review_bonus": review_bonus,
            "domain_tier_bonus": tier_bonus,
            "content_type_bonus": type_bonus,
        },
    }
