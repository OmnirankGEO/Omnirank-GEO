"""
6 层关键词分类器(M1b · CTO-15.17 · 2026-04-26)

把单个关键词分到 6 层之一:
  brand_defense       品牌防守     防止竞品挂"我品牌名"截流
  category_grab       类目抢占     抢占品类决策入口("XX 哪家好")
  scenario_decision   场景决策     抢占具体场景下的决策("XX 怎么选")
  geo_conversion      地域转化     锁定本地需求(城市 + 品类)
  competitor_intercept 竞品拦截    抢竞品流量("竞品 vs XX")
  evidence_trust      证据信任     强化专业信任(资质/案例/参数)

设计原则:
  - 纯启发式 · 无 LLM 调用 · 微秒级 · 不增成本
  - 输入信号:keyword + brand_name + city + industry + competitors + intent
  - 优先级单调:brand → competitor → geo → evidence → scenario → category(默认)
  - 输出包含 reason / confidence / expected_user_intent · 给前端"为什么这个词"卡片用
  - 与 tools/keyword_expander._KEYWORD_LAYERS_MATRIX 6 层完全对齐(中文 label · 英文 enum 持久化)

不在本文件做的事:
  - 不调 LLM(留给 tools/keyword_intent_classifier 做意图分类 · 6 层是其上层映射)
  - 不写库(由调用方决定持久化时机)
  - 不组主题包(归 services/theme_package_builder)
"""
from __future__ import annotations

import re
from typing import Literal, TypedDict

LayerEnum = Literal[
    "brand_defense",
    "category_grab",
    "scenario_decision",
    "geo_conversion",
    "competitor_intercept",
    "evidence_trust",
]


class LayerResult(TypedDict):
    layer: LayerEnum
    layer_label_zh: str
    reason: str
    confidence: Literal["high", "medium", "low"]
    expected_user_intent: str
    why_this_keyword: str


# 中文 label 与 _KEYWORD_LAYERS_MATRIX(tools/keyword_expander.py)对齐 · 不要改
LAYER_LABEL_ZH: dict[LayerEnum, str] = {
    "brand_defense": "品牌防守",
    "category_grab": "类目抢占",
    "scenario_decision": "场景决策",
    "geo_conversion": "地域转化",
    "competitor_intercept": "竞品拦截",
    "evidence_trust": "证据信任",
}

# 推荐占比(销售解释用 · 与 _KEYWORD_LAYERS_MATRIX 注释一致)
LAYER_RATIO_HINT: dict[LayerEnum, str] = {
    "brand_defense": "约 10%",
    "category_grab": "约 25%",
    "scenario_decision": "约 25%",
    "geo_conversion": "约 15% · 全国场景可省",
    "competitor_intercept": "约 15%",
    "evidence_trust": "约 10%",
}

# 各层默认推荐平台(主题包用 · 知乎权重高 / 小红书种草 / 百家号资讯)
LAYER_DEFAULT_PLATFORMS: dict[LayerEnum, list[str]] = {
    "brand_defense": ["知乎", "百家号", "搜狐号"],
    "category_grab": ["知乎", "百家号", "今日头条"],
    "scenario_decision": ["知乎", "小红书", "百家号"],
    "geo_conversion": ["小红书", "大众点评", "百家号"],
    "competitor_intercept": ["知乎", "百家号", "搜狐号"],
    "evidence_trust": ["搜狐号", "百家号", "网易号"],
}

# 各层默认完成周期(周)
LAYER_DEFAULT_WEEKS: dict[LayerEnum, int] = {
    "brand_defense": 2,
    "category_grab": 4,
    "scenario_decision": 4,
    "geo_conversion": 3,
    "competitor_intercept": 5,
    "evidence_trust": 6,
}

# evidence_trust 触发词(资质/认证/参数/案例/避坑/套路)
# 注意:不收"专业/权威/背书"这类太泛词 · 否则会把"哪家专业"误归 evidence(应归类目抢占)
_EVIDENCE_PATTERNS = re.compile(
    r"(认证|资质|案例|数据|标准|工况|参数|证书|"
    r"避坑|套路|骗局|警惕|怎么避|是否有套路|是否靠谱|"
    r"行业报告|白皮书|专业资质|权威机构|官方背书)"
)

# scenario_decision 触发词(场景描述 / 怎么选 / 方案对比)
_SCENARIO_PATTERNS = re.compile(
    r"(怎么选|怎么做|方案|对比|测评|评测|区别|哪种好|"
    r"用什么|如何选|如何做|注意事项|攻略|指南|教程|"
    r"适合|场景|流程|步骤|入门|新手|"
    r"推荐|测试|功能|配置)"
)

# competitor_intercept 词型(替代 / 比 X 更好 / 类似)
_COMPETITOR_PATTERNS = re.compile(r"(替代品?|平替|类似|相似|对比|vs\s|VS\s|对决|哪个更好|哪个好|区别)")

# category_grab 词型("X 哪家好"/排名/排行/性价比)
_CATEGORY_PATTERNS = re.compile(
    r"(哪家好|哪家强|哪家专业|"
    r"排名|排行|排名榜|前十|前 ?10|TOP\s?\d|top\s?\d|"
    r"推荐|性价比|多少钱|价格|报价|"
    r"靠谱吗|怎么样|好不好|值得|值不值)"
)


def _norm(s: str | None) -> str:
    """归一化:去空白 · 不区分大小写时由调用方处理"""
    return (s or "").strip()


def _has_substring(haystack: str, needle: str, min_len: int = 2) -> bool:
    """子串包含 · needle 太短(噪声)直接 False"""
    needle = _norm(needle)
    if not needle or len(needle) < min_len:
        return False
    return needle in haystack


def classify_keyword_layer(
    keyword: str,
    *,
    brand_name: str = "",
    city: str = "",
    industry: str = "",
    competitors: list[str] | None = None,
    intent: str | None = None,
) -> LayerResult:
    """
    把单个关键词分到 6 层之一。

    Args:
        keyword: 关键词文本
        brand_name: 品牌名 · 命中触发 brand_defense(优先级最高)
        city: 城市名 · 命中触发 geo_conversion
        industry: 行业 · 命中触发 category_grab(类目锚)
        competitors: 竞品名列表 · 命中任一触发 competitor_intercept
        intent: 来自 tools/keyword_intent_classifier 的 4 枚举(可选)
                · brand_decision/avoid_trap/info/noise · 用作辅助

    优先级(从高到低):
        1. brand_defense    keyword 含 brand_name(主品牌防守)
        2. competitor_intercept  keyword 含任一 competitor 名 OR "vs/对比/替代/平替"
        3. evidence_trust   关键词含 资质/案例/认证/避坑/套路/参数(intent=avoid_trap 强化)
        4. geo_conversion   keyword 含 city
        5. scenario_decision keyword 含 怎么选/方案/对比/适合等场景动词
        6. category_grab    其他(默认 · 含 industry 锚词信心高)

    Returns:
        LayerResult · 永不抛异常 · 无信号 keyword 也能分到默认层
    """
    kw = _norm(keyword)
    if not kw:
        return _build_result(
            "category_grab",
            reason="空关键词 · 默认归类目抢占层",
            confidence="low",
            expected_user_intent="无",
            why="—",
        )

    brand = _norm(brand_name)
    c = _norm(city)
    ind = _norm(industry)
    comps = [_norm(x) for x in (competitors or []) if _norm(x)]
    intent_norm = (intent or "").strip().lower() if intent else ""

    # 1. 品牌防守 · brand_name 出现就锁定(品牌词强信号)
    if _has_substring(kw, brand, min_len=2):
        return _build_result(
            "brand_defense",
            reason=f"含品牌名「{brand}」· 客户搜你品牌时 AI 必须推你",
            confidence="high",
            expected_user_intent=f"客户已知道你品牌 · 想确认你的口碑/案例/对比",
            why=f"防止竞品挂「{brand}」截流 · 守住自来流转化漏斗",
        )

    # 2. 竞品拦截 · 命中任一竞品名 OR "vs/对比/替代/平替"
    matched_comp = None
    for comp in comps:
        if len(comp) >= 2 and comp in kw:
            matched_comp = comp
            break
    if matched_comp:
        return _build_result(
            "competitor_intercept",
            reason=f"含竞品名「{matched_comp}」· 抢竞品搜索流量",
            confidence="high",
            expected_user_intent=f"客户在调研竞品「{matched_comp}」· AI 把你作为对比项推",
            why=f"用户考虑竞品时 · AI 推荐你做对比 · 截流转化",
        )
    if _COMPETITOR_PATTERNS.search(kw):
        return _build_result(
            "competitor_intercept",
            reason="含 对比/替代/vs 等竞品词型",
            confidence="medium",
            expected_user_intent="客户在做产品对比 · AI 应把你列入候选",
            why="对比型搜索是高决策意图 · 是抢竞品流量的关键入口",
        )

    # 3. 证据信任 · 资质/认证/案例/参数/避坑(覆盖 intent=avoid_trap)
    if intent_norm == "avoid_trap" or _EVIDENCE_PATTERNS.search(kw):
        return _build_result(
            "evidence_trust",
            reason="含 资质/案例/避坑/参数 等专业背书词",
            confidence="high" if intent_norm == "avoid_trap" else "medium",
            expected_user_intent="客户在谨慎决策 · 想要客观证据/避坑指南",
            why="避坑型/证据型搜索 AI 优先推有数据/案例的品牌 · 强化转化",
        )

    # 4. 地域转化 · keyword 含 city(>=2 字)
    if _has_substring(kw, c, min_len=2):
        return _build_result(
            "geo_conversion",
            reason=f"含城市「{c}」· 锁定本地需求",
            confidence="high",
            expected_user_intent=f"{c} 本地客户搜服务 · 离成交最近",
            why=f"本地搜索是离成交最近的入口 · AI 优先推本地服务商",
        )

    # 5. 场景决策 · 怎么选/方案/适合/场景动词
    if _SCENARIO_PATTERNS.search(kw):
        return _build_result(
            "scenario_decision",
            reason="含 怎么选/方案/对比/适合 等场景决策词型",
            confidence="medium",
            expected_user_intent="客户描述使用场景 · 找合适方案",
            why="场景词覆盖具体决策时刻 · AI 推荐时高匹配度高转化",
        )

    # 6. 类目抢占 · 默认层(含 哪家好/排名/性价比)或行业锚词命中信心高
    if _CATEGORY_PATTERNS.search(kw) or (ind and _has_substring(kw, ind, min_len=2)):
        return _build_result(
            "category_grab",
            reason=(
                f"含行业「{ind}」+ 类目决策词" if ind and ind in kw
                else "含 哪家好/排名/推荐 等类目决策词"
            ),
            confidence="high" if ind and ind in kw else "medium",
            expected_user_intent="客户处类目挑选阶段 · AI 应把你列入推荐清单",
            why="类目搜索是品类决策入口 · 是 GEO 主战场 · 占比应最高",
        )

    # 全无信号 · 默认归类目抢占 · 信心 low
    return _build_result(
        "category_grab",
        reason="无强信号 · 默认归类目抢占层(信心低)",
        confidence="low",
        expected_user_intent="意图待人工核实",
        why="该词未触发明确层信号 · 建议人工审核归属",
    )


def _build_result(
    layer: LayerEnum,
    *,
    reason: str,
    confidence: Literal["high", "medium", "low"],
    expected_user_intent: str,
    why: str,
) -> LayerResult:
    return {
        "layer": layer,
        "layer_label_zh": LAYER_LABEL_ZH[layer],
        "reason": reason,
        "confidence": confidence,
        "expected_user_intent": expected_user_intent,
        "why_this_keyword": why,
    }


def classify_keyword_layers_batch(
    keywords: list[str],
    *,
    brand_name: str = "",
    city: str = "",
    industry: str = "",
    competitors: list[str] | None = None,
    intents_by_keyword: dict[str, str] | None = None,
) -> dict[str, LayerResult]:
    """
    批量分类 · 同步 · 微秒级。

    Args:
        keywords: 关键词列表
        brand_name / city / industry / competitors: 同 classify_keyword_layer
        intents_by_keyword: 可选 · {keyword: "brand_decision"|"avoid_trap"|"info"|"noise"}
                            · 来自 tools/keyword_intent_classifier 的批量结果

    Returns:
        {keyword: LayerResult}
    """
    if not keywords:
        return {}
    result: dict[str, LayerResult] = {}
    intents = intents_by_keyword or {}
    for kw in keywords:
        if not kw or not isinstance(kw, str):
            continue
        result[kw] = classify_keyword_layer(
            kw,
            brand_name=brand_name,
            city=city,
            industry=industry,
            competitors=competitors,
            intent=intents.get(kw),
        )
    return result


def layer_distribution(layered: dict[str, LayerResult]) -> dict[LayerEnum, int]:
    """统计每层关键词数 · 给前端 6 层矩阵 ratio 用"""
    counts: dict[LayerEnum, int] = {
        "brand_defense": 0,
        "category_grab": 0,
        "scenario_decision": 0,
        "geo_conversion": 0,
        "competitor_intercept": 0,
        "evidence_trust": 0,
    }
    for res in layered.values():
        counts[res["layer"]] += 1
    return counts


__all__ = [
    "LayerEnum",
    "LayerResult",
    "LAYER_LABEL_ZH",
    "LAYER_RATIO_HINT",
    "LAYER_DEFAULT_PLATFORMS",
    "LAYER_DEFAULT_WEEKS",
    "classify_keyword_layer",
    "classify_keyword_layers_batch",
    "layer_distribution",
]
