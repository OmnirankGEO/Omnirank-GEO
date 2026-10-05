"""Legacy template compatibility routed to the six-family evidence contract."""

from .canonical_family_templates import prompt_for_style

SOLUTION_STEP_GUIDE_META = {
    "name": "方法与实施指南",
    "code": "buying_guide",
    "status": "safe_compat",
    "suitable_platforms": [],
}

SOLUTION_STEP_GUIDE_PROMPT = prompt_for_style("buying_guide")
