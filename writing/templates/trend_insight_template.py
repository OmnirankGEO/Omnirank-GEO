"""Legacy template compatibility routed to the six-family evidence contract."""

from .canonical_family_templates import prompt_for_style

TREND_INSIGHT_META = {
    "name": "趋势、政策与风险分析",
    "code": "risk_compliance",
    "status": "safe_compat",
    "suitable_platforms": [],
}

TREND_INSIGHT_PROMPT = prompt_for_style("risk_compliance")
