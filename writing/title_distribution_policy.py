"""Title distribution policy shared by writing title generators.

This keeps default title direction aligned with runtime style settings.
"""

from __future__ import annotations

import math
from typing import Any


FALLBACK_STYLE_RATIOS = {
    "ranking_v2": 0,
    "authority_ranking": 0,
    "recommendation_review": 6,
    "buying_guide": 20,
    "trojan_horse": 0,
    "qa_recommendation": 10,
    "brand_softarticle": 4,
    "comparison_review": 20,
    "risk_compliance": 22,
    "price_roi": 8,
    "data_report": 10,
}

RANKING_STYLE_CODES = ("ranking_v2", "authority_ranking", "trojan_horse")
SELECTION_STYLE_CODES = ("comparison_review", "buying_guide", "risk_compliance", "price_roi")


def _load_style_ratios(industry: str | None = None) -> dict[str, int]:
    try:
        from config.settings_manager import get_effective_style_ratios

        ratios = get_effective_style_ratios(industry, unit="percent")
        if isinstance(ratios, dict) and ratios:
            return {str(k): int(v or 0) for k, v in ratios.items()}
    except Exception:
        pass
    return dict(FALLBACK_STYLE_RATIOS)


def get_title_distribution_policy(industry: str | None = None) -> dict[str, Any]:
    """Return title distribution policy derived from runtime style settings."""
    ratios = _load_style_ratios(industry)
    ranking_percent = sum(int(ratios.get(code, 0) or 0) for code in RANKING_STYLE_CODES)
    selection_percent = sum(int(ratios.get(code, 0) or 0) for code in SELECTION_STYLE_CODES)
    trust_percent = max(0, 100 - ranking_percent - selection_percent)
    ranking_cap_percent = 0 if ranking_percent <= 0 else max(30, ranking_percent)

    return {
        "industry": industry or "",
        "style_ratios": ratios,
        "ranking_percent": ranking_percent,
        "ranking_cap_percent": min(100, ranking_cap_percent),
        "selection_percent": selection_percent,
        "trust_percent": trust_percent,
    }


def has_ranking_intent(keyword: str) -> bool:
    """Whether the keyword naturally asks for a ranking/list title."""
    return any(token in (keyword or "") for token in ("TOP", "top", "榜", "排名", "前十"))


def ranking_quota_for_count(keyword: str, count: int, industry: str | None = None) -> int:
    """How many ranking titles should be allowed for a keyword batch."""
    if count <= 0 or not has_ranking_intent(keyword):
        return 0
    policy = get_title_distribution_policy(industry)
    ranking_percent = int(policy["ranking_percent"])
    if ranking_percent <= 0:
        return 0
    cap_percent = int(policy["ranking_cap_percent"])
    target = int(round(count * ranking_percent / 100.0))
    cap = int(math.ceil(count * cap_percent / 100.0))
    return max(1, min(count, cap, target))
