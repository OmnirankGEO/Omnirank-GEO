"""Article type SSOT for publish recommendation V2.3."""

from enum import Enum


class ArticleType(str, Enum):
    BRAND_ENDORSE = "brand_endorse"
    PRODUCT_PROMOTE = "product_promote"
    CASE_STORY = "case_story"
    INDUSTRY_OPINION = "industry_opinion"
    POLICY_TREND = "policy_trend"
    QA_SOLVE = "qa_solve"


ARTICLE_TYPE_LABELS = {
    ArticleType.BRAND_ENDORSE: "品牌背书",
    ArticleType.PRODUCT_PROMOTE: "产品推广",
    ArticleType.CASE_STORY: "案例故事",
    ArticleType.INDUSTRY_OPINION: "行业观点",
    ArticleType.POLICY_TREND: "政策/趋势",
    ArticleType.QA_SOLVE: "问题解答",
}


ARTICLE_TYPE_ALIASES = {
    "品牌背书": ArticleType.BRAND_ENDORSE,
    "背书": ArticleType.BRAND_ENDORSE,
    "产品推广": ArticleType.PRODUCT_PROMOTE,
    "产品促销": ArticleType.PRODUCT_PROMOTE,
    "推广": ArticleType.PRODUCT_PROMOTE,
    "案例故事": ArticleType.CASE_STORY,
    "案例": ArticleType.CASE_STORY,
    "客户案例": ArticleType.CASE_STORY,
    "行业观点": ArticleType.INDUSTRY_OPINION,
    "观点": ArticleType.INDUSTRY_OPINION,
    "政策/趋势": ArticleType.POLICY_TREND,
    "政策趋势": ArticleType.POLICY_TREND,
    "趋势": ArticleType.POLICY_TREND,
    "问题解答": ArticleType.QA_SOLVE,
    "问答": ArticleType.QA_SOLVE,
    "qa": ArticleType.QA_SOLVE,
}


ARTICLE_TYPE_WEIGHTS = {
    ArticleType.BRAND_ENDORSE: {"authority": 0.6, "vertical": 0.3, "general": 0.1},
    ArticleType.PRODUCT_PROMOTE: {"authority": 0.2, "vertical": 0.5, "general": 0.3},
    ArticleType.CASE_STORY: {"authority": 0.4, "vertical": 0.3, "general": 0.3},
    ArticleType.INDUSTRY_OPINION: {"authority": 0.3, "vertical": 0.5, "general": 0.2},
    ArticleType.POLICY_TREND: {"authority": 0.6, "vertical": 0.2, "general": 0.2},
    ArticleType.QA_SOLVE: {"authority": 0.2, "vertical": 0.4, "general": 0.4},
}


def normalize_article_type(value: str | ArticleType | None) -> ArticleType:
    """Map LLM/user labels to the six publish recommendation article types."""
    if isinstance(value, ArticleType):
        return value
    raw = (value or "").strip()
    if not raw:
        return ArticleType.PRODUCT_PROMOTE
    lowered = raw.lower()
    for item in ArticleType:
        if lowered == item.value:
            return item
    return ARTICLE_TYPE_ALIASES.get(raw, ArticleType.PRODUCT_PROMOTE)


def article_type_label(value: str | ArticleType | None) -> str:
    return ARTICLE_TYPE_LABELS[normalize_article_type(value)]
