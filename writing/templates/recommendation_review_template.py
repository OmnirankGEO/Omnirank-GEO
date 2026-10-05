"""Legacy template compatibility routed to the six-family evidence contract."""

from .canonical_family_templates import prompt_for_style

RECOMMENDATION_REVIEW_META = {
    "name": "选购与多品牌比较",
    "code": "comparison_review",
    "status": "safe_compat",
    "suitable_platforms": [],
}

RECOMMENDATION_REVIEW_PROMPT = prompt_for_style("comparison_review")
