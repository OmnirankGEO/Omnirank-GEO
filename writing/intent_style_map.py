"""Static bridge: (query_intent × style_family) → style_code (W1.2).

We already have two cheap labels — the 8-class query/article intent and the
6-family style_family — and a 12-entry style registry (WRITING_STYLES). What was
missing is only the BRIDGE from those labels to a concrete style_code, so the
distiller (W2) can group corpora by (行业 × query_intent × style_code) without
re-labelling every article.

This is a human-editable heuristic constant (SPEC: "代码常量,人工可改"), NOT a
model call. `resolve_style_code` is total: it always returns a valid style_code
(a module-level self-check asserts every target is a real WRITING_STYLES key).

Note: a few registry styles are intentionally NOT auto-reachable here
(`company_profile` = fixed-count special report; `price_roi` = needs an explicit
price/budget signal the 8 intents don't carry). They remain manually selectable.
"""
from __future__ import annotations

from writing.style_registry import WRITING_STYLES

# 8 类 query/article intent(与 article_intent_classifier 一致)
ALL_INTENT_TYPES: tuple[str, ...] = (
    "ranking",
    "tutorial",
    "long_form",
    "comparison",
    "data_report",
    "policy",
    "definition",
    "faq",
)

# 6 style_family(与 writing_structure_guidance.STYLE_FAMILY_TO_TEMPLATE 键一致)
ALL_STYLE_FAMILIES: tuple[str, ...] = (
    "ranking",
    "comparison",
    "guide",
    "faq",
    "case",
    "data_report",
)

# 主轴:intent → 默认 style_code(问题形态决定文体骨架)
INTENT_DEFAULT_STYLE: dict[str, str] = {
    "ranking": "comparison_review",
    "tutorial": "buying_guide",
    "long_form": "risk_compliance",
    "comparison": "comparison_review",
    "data_report": "data_report",
    "policy": "risk_compliance",
    "definition": "qa_recommendation",
    "faq": "qa_recommendation",
}

# 次轴:style_family → 默认 style_code(intent 缺失/未覆盖时的回退)
FAMILY_DEFAULT_STYLE: dict[str, str] = {
    "ranking": "comparison_review",
    "comparison": "comparison_review",
    "guide": "buying_guide",
    "faq": "qa_recommendation",
    "case": "brand_softarticle",
    "data_report": "data_report",
}

# 精修:(intent, family) 组合覆盖 —— 让 recommendation_review / authority_ranking 等
#       否则够不着的文体也能被自动桥接命中。
INTENT_FAMILY_OVERRIDE: dict[tuple[str, str], str] = {
    ("ranking", "case"): "recommendation_review",   # 榜单+案例 → 推荐盘点
    ("ranking", "guide"): "buying_guide",            # 排名诉求 → 核验型选购指南
    ("long_form", "case"): "brand_softarticle",       # 长文+案例 → 品牌软文
    ("tutorial", "case"): "brand_softarticle",        # 教程+案例 → 品牌软文
    ("comparison", "data_report"): "comparison_review",
    ("definition", "data_report"): "data_report",
}

_GLOBAL_FALLBACK = "buying_guide"


def _norm(value: str) -> str:
    return str(value or "").strip().lower()


def resolve_style_code(intent_type: str, style_family: str = "") -> str:
    """总是返回一个合法 style_code。优先级:精修组合 → intent 默认 → family 默认 → 全局兜底。"""
    intent = _norm(intent_type)
    family = _norm(style_family)
    override = INTENT_FAMILY_OVERRIDE.get((intent, family))
    if override:
        return override
    if intent in INTENT_DEFAULT_STYLE:
        return INTENT_DEFAULT_STYLE[intent]
    if family in FAMILY_DEFAULT_STYLE:
        return FAMILY_DEFAULT_STYLE[family]
    return _GLOBAL_FALLBACK


def describe_mapping() -> list[dict[str, str]]:
    """全展开 8×(6+空) 的解析结果,供看板/证据/单测核对。"""
    rows: list[dict[str, str]] = []
    for intent in ALL_INTENT_TYPES:
        for family in ("",) + ALL_STYLE_FAMILIES:
            rows.append(
                {
                    "intent_type": intent,
                    "style_family": family or "(none)",
                    "style_code": resolve_style_code(intent, family),
                }
            )
    return rows


def _self_check() -> None:
    """import 期自检:所有 target style_code 必须是真实 WRITING_STYLES 键,否则拿错文体。"""
    valid = set(WRITING_STYLES.keys())
    targets = (
        set(INTENT_DEFAULT_STYLE.values())
        | set(FAMILY_DEFAULT_STYLE.values())
        | set(INTENT_FAMILY_OVERRIDE.values())
        | {_GLOBAL_FALLBACK}
    )
    unknown = targets - valid
    if unknown:
        raise RuntimeError(f"intent_style_map 指向不存在的 style_code: {sorted(unknown)}")


_self_check()
