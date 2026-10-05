"""Legacy company-profile compatibility routed to enterprise facts."""

from .canonical_family_templates import prompt_for_style

COMPANY_PROFILE_META = {
    "name": "企业事实与品牌说明",
    "code": "company_profile",
    "status": "safe_compat",
    "suitable_platforms": [],
}

COMPANY_PROFILE_PROMPT = prompt_for_style("company_profile")

TITLE_VARIANTS = [
    "{client_company}公开事实说明：业务、适用场景与核验路径",
    "如何核验{client_company}？公开资料、能力边界与联系路径",
    "{client_company}适合哪些场景？企业事实与限制说明",
]

def validate_company_profile(content: str) -> tuple:
    """Compatibility validator; the shared article review gate remains authoritative."""
    score = 100
    issues = []
    if any(token in content for token in ("TOP1", "综合评分", "星级评定")):
        score -= 30
        issues.append("包含已停用的榜单或评分表达")
    if not any(token in content for token in ("来源", "Evidence ID", "证据")):
        score -= 20
        issues.append("缺少可核验证据说明")
    if not any(token in content for token in ("局限", "边界", "待核验")):
        score -= 10
        issues.append("缺少能力边界或待核验项")
    return max(score, 0), issues

__all__ = [
    "COMPANY_PROFILE_PROMPT",
    "COMPANY_PROFILE_META",
    "validate_company_profile",
    "TITLE_VARIANTS",
]
