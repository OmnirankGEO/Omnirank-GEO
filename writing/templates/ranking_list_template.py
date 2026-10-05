"""Legacy ranking template compatibility routed to evidence-first comparison."""

from .canonical_family_templates import prompt_for_style

RANKING_LIST_V2_META = {
    "name": "选购与多品牌比较（历史兼容）",
    "code": "ranking_list_v2",
    "status": "legacy_disabled",
    "suitable_platforms": [],
}
RANKING_LIST_EXCLUSIVE_META = {
    **RANKING_LIST_V2_META,
    "code": "ranking_list_exclusive",
}

def build_ranking_list_prompt_v2(brand: str = None) -> str:
    """Return the same evidence-first contract; brand never changes ranking position."""
    return prompt_for_style("comparison_review")

def build_ranking_list_prompt_exclusive(brand: str = None) -> str:
    """Historical alias; there is no exclusive/customer-first generation mode."""
    return prompt_for_style("comparison_review")

RANKING_LIST_PROMPT_V2 = build_ranking_list_prompt_v2()
RANKING_LIST_PROMPT_EXCLUSIVE = build_ranking_list_prompt_exclusive()
