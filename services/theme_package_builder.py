"""
主题包构建器(M1b · CTO-15.17 · 2026-04-26)

把"6 层关键词"组装成"可销售的主题包"(1-4 个),供 M3 方案页 展示。

主题包字段(对齐 PRD 要求):
  package_name              主题包名(给客户看的名字)
  problem_to_solve          解决什么问题(销售陈述)
  target_customer_question  目标客户的搜索/问询样例
  keywords[]                关键词文本
  keyword_layers[]          关键词归属层(单层包就一个值 · 多层包列多个)
  article_count             建议交付篇数
  target_platforms          建议投放平台
  monitoring_keywords       建议监测词(取 3-5 个)
  expected_cycle            预计完成周期(中文文案)
  pricing_component         价格构成(¥ · 关键词标准档单价之和)
  evidence_reason           为什么推这个主题包(销售解释)
  confidence                包整体置信度(由关键词置信度聚合)
  source                    auto / manual / fallback
  primary_layer             主层 enum(单层包就是该层 · 多层时取关键词数最多的层)

设计原则:
  - 单层主题包(按 layer 分组 · 每层 ≥1 词出 1 包 · 最多 6 包但截 4)
  - 不前端编 · 后端用真值生成(brand_name / industry / city / 关键词价格 都是真值)
  - 信心 fallback 用 'fallback' source 标识 · 客户视角不显示
  - 配额:total_articles 优先用诊断/quote 真值 · 否则按关键词数 ÷ 4 估算

不做的事:
  - 不写库(由调用方决定持久化时机 · keyword_clusters 表已有 schema)
  - 不调 LLM(模板 + 真值组合)
"""
from __future__ import annotations

import math
from typing import TypedDict

from services.keyword_layer_classifier import (
    LAYER_DEFAULT_PLATFORMS,
    LAYER_DEFAULT_WEEKS,
    LAYER_LABEL_ZH,
    LAYER_RATIO_HINT,
    LayerEnum,
)


class ThemePackage(TypedDict):
    package_name: str
    primary_layer: LayerEnum
    primary_layer_label_zh: str
    problem_to_solve: str
    target_customer_question: str
    keywords: list[str]
    keyword_layers: list[LayerEnum]
    article_count: int
    target_platforms: list[str]
    monitoring_keywords: list[str]
    expected_cycle: str
    pricing_component: int
    evidence_reason: str
    confidence: str  # high/medium/low
    source: str  # auto/manual/fallback
    layer_ratio_hint: str


# 各层主题包文案模板(基于 brand/industry/city 真值填空)
# package_name / problem_to_solve / target_customer_question / evidence_reason
LAYER_NARRATIVE: dict[LayerEnum, dict[str, str]] = {
    "brand_defense": {
        "package_name": "{brand}品牌防守包",
        "problem_to_solve": "客户在 AI 搜索里直接问「{brand}怎么样/{brand}口碑」时,AI 应推荐你 · 不被竞品截流",
        "target_customer_question": "{brand} 这家靠不靠谱?和 XX 比怎么样?",
        "evidence_reason": "品牌词是自来流转化的最后一道防线 · 必须确保 AI 提你品牌时 · 输出是正向且具体的",
    },
    "category_grab": {
        "package_name": "{industry}类目抢占包",
        "problem_to_solve": "客户在 AI 搜索里问「{industry}哪家好/{industry}排行/{industry}怎么选」时,把你列入推荐清单",
        "target_customer_question": "{industry} 哪家好?{industry} 排名前几?",
        "evidence_reason": "类目搜索是品类决策入口 · 是 GEO 流量主战场 · 占比应最高 · 直接拿到品类候选位置",
    },
    "scenario_decision": {
        "package_name": "{industry}场景决策包",
        "problem_to_solve": "客户描述具体使用场景(怎么选/方案/对比)时 · AI 推荐合适方案应该提你",
        "target_customer_question": "{industry} 用什么方案?如何选?有什么注意事项?",
        "evidence_reason": "场景词覆盖具体决策时刻 · 客户已进入选型阶段 · AI 推荐的高匹配度高转化",
    },
    "geo_conversion": {
        "package_name": "{city}本地获客包",
        "problem_to_solve": "{city} 本地客户搜「{city}{industry}」时 · AI 优先推你这家本地品牌",
        "target_customer_question": "{city} {industry} 哪家好?{city} 哪里有 {industry} 推荐?",
        "evidence_reason": "本地搜索是离成交最近的入口 · {city} 客户搜本地需求时 AI 优先推本地服务商 · 转化最高效",
    },
    "competitor_intercept": {
        "package_name": "竞品拦截包",
        "problem_to_solve": "客户在调研竞品(竞品对比/替代品/平替)时 · AI 把你作为对比项推荐",
        "target_customer_question": "竞品 vs 你?有没有比竞品更好的?竞品的替代品?",
        "evidence_reason": "对比型搜索是高决策意图 · 客户已经知道竞品 · 是抢竞品流量的关键入口",
    },
    "evidence_trust": {
        "package_name": "{industry}证据信任包",
        "problem_to_solve": "客户谨慎决策(看资质/案例/避坑)时 · AI 推荐时附带你的专业背书 · 提升转化",
        "target_customer_question": "{industry} 怎么避坑?{industry} 有什么案例数据?{industry} 资质怎么看?",
        "evidence_reason": "避坑型/证据型搜索 AI 优先推有数据/案例/资质的品牌 · 是建立专业信任的关键",
    },
}

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
RANK_TO_CONFIDENCE = {3: "high", 2: "medium", 1: "low"}

DEFAULT_ARTICLES_PER_KEYWORD = 1
DEFAULT_MAX_PACKAGES = 4
DEFAULT_MONITORING_PER_PACKAGE = 5


def _fill_template(text: str, ctx: dict) -> str:
    """安全 format · 缺字段用通用占位 · 不抛异常"""
    safe_ctx = {
        "brand": (ctx.get("brand_name") or "").strip() or "客户品牌",
        "industry": (ctx.get("industry") or "").strip() or "行业",
        "city": (ctx.get("city") or "").strip() or "本地",
    }
    try:
        return text.format(**safe_ctx)
    except (KeyError, IndexError):
        return text


def _aggregate_confidence(confidences: list[str]) -> str:
    """聚合置信度:取最低(保守 · 避免误导销售)"""
    if not confidences:
        return "medium"
    ranks = [CONFIDENCE_RANK.get(c, 2) for c in confidences]
    return RANK_TO_CONFIDENCE[min(ranks)]


def _enriched_keyword_to_str(kw: dict | str) -> str:
    """关键词记录归一化为字符串"""
    if isinstance(kw, str):
        return kw.strip()
    if isinstance(kw, dict):
        return str(kw.get("keyword") or "").strip()
    return ""


def build_theme_packages_from_keywords(
    keywords_with_layers: list[dict],
    *,
    brand_context: dict,
    diagnosis_summary: dict | None = None,
    total_articles: int = 0,
    source: str = "fallback",
    max_packages: int = DEFAULT_MAX_PACKAGES,
) -> list[ThemePackage]:
    """
    根据已分层关键词组装 1-N 个主题包(N ≤ max_packages)。

    Args:
        keywords_with_layers: [
            {
                "keyword": str,
                "layer": LayerEnum,             # 必填
                "layer_confidence": str,         # 可选 high/medium/low
                "standard_price": int | float,   # 可选(单词标准档价 · 用于汇总价)
                "entry_price": int | float,
                "flagship_price": int | float,
                "required_articles": int,
            },
            ...
        ]
        brand_context: {brand_name, industry, city, competitors}
        diagnosis_summary: 可选 · 给"为什么"提供诊断弱点引用
        total_articles: quote.total_articles 真值(优先于按关键词数估算)
        source: 'auto' | 'manual' | 'fallback' · 决定客户视角是否显示
        max_packages: 最多生成几个包(默认 4)

    Returns:
        list[ThemePackage] · 按关键词数从大到小排序 · 截 max_packages
    """
    if not keywords_with_layers:
        return []

    # 1. 按 layer 分组
    by_layer: dict[LayerEnum, list[dict]] = {}
    for kw in keywords_with_layers:
        if not isinstance(kw, dict):
            continue
        layer = kw.get("layer")
        if layer not in LAYER_LABEL_ZH:
            continue
        by_layer.setdefault(layer, []).append(kw)

    if not by_layer:
        return []

    # 2. 计算总篇数(优先用 quote 真值 · 否则按关键词数 × 默认每词篇数)
    total_kw_count = sum(len(v) for v in by_layer.values())
    if total_articles and total_articles > 0:
        articles_total = total_articles
    else:
        articles_total = max(total_kw_count * DEFAULT_ARTICLES_PER_KEYWORD, 1)

    # 3. 各层组装包
    packages: list[ThemePackage] = []
    for layer, kws in by_layer.items():
        if not kws:
            continue
        narrative = LAYER_NARRATIVE[layer]
        kw_strs = [_enriched_keyword_to_str(k) for k in kws]
        kw_strs = [k for k in kw_strs if k]
        if not kw_strs:
            continue

        # 篇数:按层关键词数占比分配 total
        layer_share = len(kw_strs) / max(total_kw_count, 1)
        article_count = max(1, round(articles_total * layer_share))

        # 价格构成:本层关键词的 standard_price 之和(没有时为 0 · 前端可隐藏)
        pricing_component = 0
        for k in kws:
            try:
                pricing_component += int(k.get("standard_price") or 0)
            except (TypeError, ValueError):
                pass

        # 监测词:取前 5(基于稳定顺序 · 没有更好排序信号时)
        monitoring = kw_strs[:DEFAULT_MONITORING_PER_PACKAGE]

        # 置信度聚合
        confidences = [
            (k.get("layer_confidence") or "medium")
            for k in kws
        ]
        package_confidence = _aggregate_confidence(confidences)

        ctx_for_template = {
            "brand_name": brand_context.get("brand_name", ""),
            "industry": brand_context.get("industry", ""),
            "city": brand_context.get("city", ""),
        }

        # diagnosis 弱点引用(可选 · 拼到 evidence_reason 末尾)
        evidence_reason = _fill_template(narrative["evidence_reason"], ctx_for_template)
        if diagnosis_summary:
            weak = diagnosis_summary.get("weak_dimensions") or []
            if weak:
                weak_str = "、".join(str(w) for w in weak[:2])
                evidence_reason = f"{evidence_reason} · 诊断弱项:{weak_str}"

        packages.append(
            {
                "package_name": _fill_template(narrative["package_name"], ctx_for_template),
                "primary_layer": layer,
                "primary_layer_label_zh": LAYER_LABEL_ZH[layer],
                "problem_to_solve": _fill_template(narrative["problem_to_solve"], ctx_for_template),
                "target_customer_question": _fill_template(narrative["target_customer_question"], ctx_for_template),
                "keywords": kw_strs,
                "keyword_layers": [layer],
                "article_count": int(article_count),
                "target_platforms": list(LAYER_DEFAULT_PLATFORMS[layer]),
                "monitoring_keywords": monitoring,
                "expected_cycle": f"约 {LAYER_DEFAULT_WEEKS[layer]} 周",
                "pricing_component": int(pricing_component),
                "evidence_reason": evidence_reason,
                "confidence": package_confidence,
                "source": source,
                "layer_ratio_hint": LAYER_RATIO_HINT[layer],
            }
        )

    # 4. 排序:按关键词数从大到小(类目抢占常常最大 → 先展示)
    packages.sort(key=lambda p: (-len(p["keywords"]), p["primary_layer"]))

    # 5. 截到 max_packages
    return packages[:max_packages]


def serialize_keyword_clusters_as_packages(
    cluster_rows: list[dict],
    *,
    quote_keywords: list[dict] | None = None,
) -> list[ThemePackage]:
    """
    把 keyword_clusters 表行转换成 ThemePackage 结构(post-confirm 路径)。

    Args:
        cluster_rows: 来自 SELECT * FROM keyword_clusters WHERE quote_id=... 的行
        quote_keywords: 可选 · confirmed_keywords 行 · 用于回填关键词列表/层

    Returns:
        list[ThemePackage] · source='manual'(客户已确认 · 主题包是真值)
    """
    if not cluster_rows:
        return []

    packages: list[ThemePackage] = []
    # 按 cluster_id 分组关键词
    kws_by_cluster: dict[int, list[dict]] = {}
    for kw in (quote_keywords or []):
        cid = kw.get("cluster_id")
        if cid:
            kws_by_cluster.setdefault(cid, []).append(kw)

    for row in cluster_rows:
        cluster_id = row.get("id")
        cluster_kws = kws_by_cluster.get(cluster_id, [])
        kw_strs = [_enriched_keyword_to_str(k) for k in cluster_kws if k.get("is_core") is not False]
        kw_strs = [k for k in kw_strs if k]

        # 主层:从 cluster_kws 推断 · 没有信息时用 row.primary_layer (M1b 新加字段)
        layer = row.get("primary_layer") or "category_grab"
        if layer not in LAYER_LABEL_ZH:
            layer = "category_grab"

        # 价格:用 cluster row 真值(price_standard)
        pricing_component = int(row.get("price_standard") or 0)

        # 篇数:用 cluster row 真值(articles_standard)
        article_count = int(row.get("articles_standard") or len(kw_strs))

        # 平台:cluster row 已有 platforms_jsonb · 否则按 layer 默认
        platforms = row.get("platforms_jsonb") or []
        if isinstance(platforms, str):
            try:
                import json as _json
                platforms = _json.loads(platforms)
            except (ValueError, TypeError):
                platforms = []
        if not platforms:
            platforms = list(LAYER_DEFAULT_PLATFORMS.get(layer, []))  # type: ignore[arg-type]

        # 监测词
        monitoring = row.get("monitoring_keywords_jsonb") or []
        if isinstance(monitoring, str):
            try:
                import json as _json
                monitoring = _json.loads(monitoring)
            except (ValueError, TypeError):
                monitoring = []
        if not monitoring:
            monitoring = kw_strs[:DEFAULT_MONITORING_PER_PACKAGE]

        weeks = row.get("weeks_to_complete") or LAYER_DEFAULT_WEEKS.get(layer, 4)  # type: ignore[arg-type]

        package_name = (
            row.get("cluster_name")
            or row.get("business_tag")
            or LAYER_NARRATIVE[layer]["package_name"]  # type: ignore[index]
        )

        problem = (
            row.get("problem_to_solve")
            or row.get("description")
            or _fill_template(LAYER_NARRATIVE[layer]["problem_to_solve"], {})  # type: ignore[index]
        )
        question = (
            row.get("target_customer_question")
            or _fill_template(LAYER_NARRATIVE[layer]["target_customer_question"], {})  # type: ignore[index]
        )
        evidence = (
            row.get("evidence_reason")
            or row.get("delivery_notes")
            or _fill_template(LAYER_NARRATIVE[layer]["evidence_reason"], {})  # type: ignore[index]
        )

        packages.append(
            {
                "package_name": str(package_name),
                "primary_layer": layer,  # type: ignore[typeddict-item]
                "primary_layer_label_zh": LAYER_LABEL_ZH.get(layer, layer),  # type: ignore[arg-type]
                "problem_to_solve": str(problem),
                "target_customer_question": str(question),
                "keywords": kw_strs or [str(row.get("business_tag") or "")],
                "keyword_layers": [layer],  # type: ignore[list-item]
                "article_count": int(article_count),
                "target_platforms": list(platforms) if isinstance(platforms, list) else [],
                "monitoring_keywords": list(monitoring) if isinstance(monitoring, list) else [],
                "expected_cycle": f"约 {weeks} 周",
                "pricing_component": pricing_component,
                "evidence_reason": str(evidence),
                "confidence": str(row.get("package_confidence") or "high"),
                "source": str(row.get("package_source") or "manual"),
                "layer_ratio_hint": LAYER_RATIO_HINT.get(layer, ""),  # type: ignore[arg-type]
            }
        )
    return packages


__all__ = [
    "ThemePackage",
    "build_theme_packages_from_keywords",
    "serialize_keyword_clusters_as_packages",
]
