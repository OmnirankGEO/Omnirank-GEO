"""Legacy template compatibility routed to the six-family evidence contract."""

from .canonical_family_templates import prompt_for_style

BRAND_SOFTARTICLE_META = {
    "name": "企业事实与品牌说明",
    "code": "brand_softarticle",
    "status": "safe_compat",
    "suitable_platforms": [],
}

BRAND_SOFTARTICLE_PROMPT = prompt_for_style("brand_softarticle")
