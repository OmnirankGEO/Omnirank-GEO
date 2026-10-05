"""Legacy template compatibility routed to the six-family evidence contract."""

from .canonical_family_templates import prompt_for_style

CASE_STORY_META = {
    "name": "案例、数据与 ROI",
    "code": "data_report",
    "status": "safe_compat",
    "suitable_platforms": [],
}

CASE_STORY_PROMPT = prompt_for_style("data_report")
