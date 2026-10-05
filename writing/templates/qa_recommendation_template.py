"""Legacy template compatibility routed to the six-family evidence contract."""

from .canonical_family_templates import prompt_for_style

QA_RECOMMENDATION_META = {
    "name": "证据型问答",
    "code": "qa_recommendation",
    "status": "safe_compat",
    "suitable_platforms": [],
}

QA_RECOMMENDATION_PROMPT = prompt_for_style("qa_recommendation")
