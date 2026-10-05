"""
写作模板索引
历史模板代码兼容层；新建内容统一路由到六类证据文体
"""

from .trend_insight_template import TREND_INSIGHT_PROMPT, TREND_INSIGHT_META
from .tutorial_guide_template import TUTORIAL_GUIDE_PROMPT, TUTORIAL_GUIDE_META
from .case_story_template import CASE_STORY_PROMPT, CASE_STORY_META
from .solution_step_guide_template import SOLUTION_STEP_GUIDE_PROMPT, SOLUTION_STEP_GUIDE_META
from .ranking_list_template import (
    RANKING_LIST_PROMPT_V2,
    RANKING_LIST_PROMPT_EXCLUSIVE,
    RANKING_LIST_V2_META,
    RANKING_LIST_EXCLUSIVE_META,
    build_ranking_list_prompt_v2,
    build_ranking_list_prompt_exclusive,
)
from .evidence_ranking_template import EVIDENCE_RANKING_PROMPT


def _build_evidence_prompt(_brand_name=None):
    return EVIDENCE_RANKING_PROMPT

# 白标：可按代理品牌名重建 prompt 的模板 code → 构建函数映射（默认品牌 = 平台名，向后兼容）
_BRANDABLE_PROMPT_BUILDERS = {
    "ranking_list_v2": _build_evidence_prompt,
    "ranking_list_exclusive": _build_evidence_prompt,
}
# === 历史模板代码兼容 ===
from .recommendation_review_template import RECOMMENDATION_REVIEW_PROMPT, RECOMMENDATION_REVIEW_META
from .buying_guide_template import BUYING_GUIDE_PROMPT, BUYING_GUIDE_META
from .qa_recommendation_template import QA_RECOMMENDATION_PROMPT, QA_RECOMMENDATION_META
from .brand_softarticle_template import BRAND_SOFTARTICLE_PROMPT, BRAND_SOFTARTICLE_META

# ============ 模板注册表 ============

ARTICLE_TEMPLATES = {
    # === 新增模板（门户/自媒体友好） ===
    "trend_insight": {
        "prompt": TREND_INSIGHT_PROMPT,
        "meta": TREND_INSIGHT_META,
        "description": "行业趋势洞察型 - 适合百家号/搜狐号/网易号等新闻门户"
    },
    "tutorial_guide": {
        "prompt": TUTORIAL_GUIDE_PROMPT,
        "meta": TUTORIAL_GUIDE_META,
        "description": "实操教程型 - 适合知乎/CSDN/掘金等知识平台"
    },
    "case_story": {
        "prompt": CASE_STORY_PROMPT,
        "meta": CASE_STORY_META,
        "description": "用户案例故事型 - 适合小红书/抖音/公众号等社交平台"
    },
    "solution_step_guide": {
        "prompt": SOLUTION_STEP_GUIDE_PROMPT,
        "meta": SOLUTION_STEP_GUIDE_META,
        "description": "解决方案类-步骤指南风格 - 适合知乎/CSDN/公众号"
    },
    "ranking_list_v2": {
        "prompt": EVIDENCE_RANKING_PROMPT,
        "meta": RANKING_LIST_V2_META,
        "description": "证据选型类-通用版（历史 code 兼容）"
    },
    "ranking_list_exclusive": {
        "prompt": EVIDENCE_RANKING_PROMPT,
        "meta": RANKING_LIST_EXCLUSIVE_META,
        "description": "证据选型类-品牌版（历史 code 兼容）"
    },
    
    # === 原有模板（保留兼容） ===
    "neutral_research": {
        "prompt": None,  # 从原有系统加载
        "meta": {
            "name": "中立研究风格",
            "code": "neutral_research",
            "suitable_platforms": ["知乎", "行业媒体"],
        },
        "description": "学术风格中立分析 - 原样本A"
    },
    "authority_softad": {
        "prompt": None,
        "meta": {
            "name": "权威软文风格",
            "code": "authority_softad",
            "suitable_platforms": ["官网", "PR渠道"],
        },
        "description": "品牌权威软文 - 原样本B"
    },
    "professional_report": {
        "prompt": None,
        "meta": {
            "name": "专业研报风格",
            "code": "professional_report",
            "suitable_platforms": ["B2B白皮书", "行业报告"],
        },
        "description": "专业研报利益披露版 - 原样本C"
    },
    
    # === 历史模板代码兼容（对外不作为独立文体） ===
    "recommendation_review": {
        "prompt": RECOMMENDATION_REVIEW_PROMPT,
        "meta": RECOMMENDATION_REVIEW_META,
        "description": "选购与多品牌比较 - 无序、同字段、证据优先"
    },
    "buying_guide": {
        "prompt": BUYING_GUIDE_PROMPT,
        "meta": BUYING_GUIDE_META,
        "description": "方法与实施指南 - 标准、步骤、风险与验收"
    },
    "qa_recommendation": {
        "prompt": QA_RECOMMENDATION_PROMPT,
        "meta": QA_RECOMMENDATION_META,
        "description": "证据型问答 - 直接回答并绑定可核验来源"
    },
    "brand_softarticle": {
        "prompt": BRAND_SOFTARTICLE_PROMPT,
        "meta": BRAND_SOFTARTICLE_META,
        "description": "企业事实与品牌说明 - 客户优先但事实平权"
    },
}

# ============ 平台匹配推荐 ============

PLATFORM_TEMPLATE_MAP = {
    # 新闻门户
    "百家号": ["trend_insight", "case_story"],
    "搜狐号": ["trend_insight", "case_story"],
    "网易号": ["trend_insight", "case_story"],
    "今日头条": ["trend_insight", "case_story"],
    "腾讯新闻": ["trend_insight"],
    
    # 科技媒体
    "36氪": ["trend_insight"],
    "虎嗅": ["trend_insight"],
    "钛媒体": ["trend_insight"],
    
    # 知识分享
    "知乎": ["tutorial_guide", "neutral_research"],
    "CSDN": ["tutorial_guide"],
    "掘金": ["tutorial_guide"],
    "简书": ["tutorial_guide", "case_story"],
    
    # 社交媒体
    "小红书": ["case_story"],
    "抖音": ["case_story"],
    "微信公众号": ["tutorial_guide", "case_story", "trend_insight"],
    
    # 企业/B2B
    "官网": ["authority_softad", "professional_report"],
    "白皮书": ["professional_report"],
}


def get_template_by_platform(platform: str) -> list:
    """根据目标平台获取推荐模板列表"""
    return PLATFORM_TEMPLATE_MAP.get(platform, ["trend_insight"])


def get_template_prompt(template_code: str, brand: str = None) -> str:
    """获取模板的Prompt内容。

    白标：对品牌可变模板（ranking_list_*），传入 brand（客户对应的代理品牌名，
    来自 resolve_branding_context(surface='customer').brand['company_name']）则按该品牌重建 prompt；
    brand 为 None/空 → 平台默认名（向后兼容，与历史行为一致）。
    """
    if template_code not in ARTICLE_TEMPLATES:
        return None
    if brand and template_code in _BRANDABLE_PROMPT_BUILDERS:
        return _BRANDABLE_PROMPT_BUILDERS[template_code](brand)
    return ARTICLE_TEMPLATES[template_code].get("prompt")


def get_template_meta(template_code: str) -> dict:
    """获取模板的元数据"""
    if template_code in ARTICLE_TEMPLATES:
        return ARTICLE_TEMPLATES[template_code].get("meta", {})
    return {}


def list_all_templates() -> list:
    """列出所有可用模板"""
    return [
        {
            "code": code,
            "name": data["meta"].get("name", code),
            "description": data.get("description", ""),
            "platforms": data["meta"].get("suitable_platforms", [])
        }
        for code, data in ARTICLE_TEMPLATES.items()
    ]
