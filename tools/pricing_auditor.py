"""
报价审计模块 V2 — 数据驱动的异常检测 + LLM审计 + AI深探

流程:
1. detect_anomalies(): 纯规则+统计，从已评分关键词中找出价格可疑的词（0成本，<1秒）
2. llm_audit_anomalies(): 把可疑词+矛盾证据交给LLM判断（1次调用，~15秒）
3. deep_probe_keywords(): 对LLM判定"需要深探"的词做AI引擎直测（复用ai_tester）
4. apply_corrections(): 用市场信号计算动态保护系数，安全地修正价格

V2 核心改进:
- 所有调整幅度上下限由 _calculate_price_guard() 动态计算
- 基于 SEM竞价/搜索量/意图/竞争度/地域市场规模 等实际数据
- 消除硬编码城市名和固定系数
"""
import asyncio
import json
import math
import re
from typing import Optional

# [M4 SSOT 2026-06-07] 单个关键词最低售价 import 自 keyword_value_scorer(全仓单一权威源 = ¥400)·防 auditor 把价压回旧地板
from tools.keyword_value_scorer import MIN_KEYWORD_PRICE


def _price_within_band(kw: dict) -> bool:
    """【v1.3 band-aware】售价是否在它自己 keyword_type 的 band 区间内(出厂 band × markup)。
    在 band 内 = v1.3 band 内插值的预期结果(按真实竞争铺开·非离群)→ 跳过跨批统计异常检测
    (IQR / 批次低估 / 相似词价差),避免把"故意按竞争铺开的差异"当异常误报刷 needs_review。
    保留数据矛盾类规则(高值低竞争/意图误判/value-difficulty 矛盾)——那些和 band 无关。"""
    try:
        bmin = float(kw.get("band_min") or 0)
        bmax = float(kw.get("band_max") or 0)
        markup = float(kw.get("markup_ratio") or 1.0) or 1.0
        price = float(kw.get("selling_price") or 0)
        if bmax <= 0 or price <= 0:
            return False  # 无 band 信息(legacy)→ 不跳过(走原统计检测)
        return (bmin * markup * 0.9) <= price <= (bmax * markup * 1.1)  # 10% 容差
    except (TypeError, ValueError):
        return False


# ========================================
# 0. 市场信号驱动的价格保护系数
# ========================================

# 地域市场规模参考数据（来源：国家统计局2024年数据 + 第一财经新一线城市研究所分级）
# tier: 城市等级, pop_m: 常住人口(百万), gdp_b: GDP(千亿)
# 用于估算关键词的地域覆盖人口和市场规模
_GEO_REFERENCE = {
    # === 一线 (tier 1) ===
    "北京": {"tier": 1, "pop_m": 21.5, "gdp_b": 43.8},
    "上海": {"tier": 1, "pop_m": 24.9, "gdp_b": 47.2},
    "广州": {"tier": 1, "pop_m": 18.8, "gdp_b": 30.4},
    "深圳": {"tier": 1, "pop_m": 17.6, "gdp_b": 34.6},
    # === 新一线 (tier 2) ===
    "成都": {"tier": 2, "pop_m": 21.4, "gdp_b": 22.1},
    "重庆": {"tier": 2, "pop_m": 32.1, "gdp_b": 30.1},
    "杭州": {"tier": 2, "pop_m": 12.5, "gdp_b": 20.1},
    "武汉": {"tier": 2, "pop_m": 13.7, "gdp_b": 20.0},
    "苏州": {"tier": 2, "pop_m": 12.8, "gdp_b": 24.7},
    "南京": {"tier": 2, "pop_m": 9.5, "gdp_b": 17.4},
    "天津": {"tier": 2, "pop_m": 13.6, "gdp_b": 16.7},
    "长沙": {"tier": 2, "pop_m": 10.5, "gdp_b": 14.3},
    "东莞": {"tier": 2, "pop_m": 10.5, "gdp_b": 11.2},
    "宁波": {"tier": 2, "pop_m": 9.6, "gdp_b": 16.2},
    "佛山": {"tier": 2, "pop_m": 9.6, "gdp_b": 12.7},
    "合肥": {"tier": 2, "pop_m": 9.6, "gdp_b": 12.7},
    "青岛": {"tier": 2, "pop_m": 10.3, "gdp_b": 15.8},
    "郑州": {"tier": 2, "pop_m": 12.8, "gdp_b": 13.7},
    "西安": {"tier": 2, "pop_m": 13.0, "gdp_b": 12.0},
    "沈阳": {"tier": 2, "pop_m": 9.1, "gdp_b": 8.1},
    "昆明": {"tier": 2, "pop_m": 8.5, "gdp_b": 7.8},
    # === 二线 (tier 3) ===
    "厦门": {"tier": 3, "pop_m": 5.3, "gdp_b": 8.1},
    "济南": {"tier": 3, "pop_m": 9.4, "gdp_b": 12.3},
    "福州": {"tier": 3, "pop_m": 8.4, "gdp_b": 12.3},
    "大连": {"tier": 3, "pop_m": 7.5, "gdp_b": 8.8},
    "温州": {"tier": 3, "pop_m": 9.6, "gdp_b": 8.7},
    "哈尔滨": {"tier": 3, "pop_m": 10.0, "gdp_b": 5.6},
    "石家庄": {"tier": 3, "pop_m": 11.2, "gdp_b": 7.1},
    "南宁": {"tier": 3, "pop_m": 8.7, "gdp_b": 5.6},
    "长春": {"tier": 3, "pop_m": 9.1, "gdp_b": 7.0},
    "泉州": {"tier": 3, "pop_m": 8.8, "gdp_b": 12.1},
    "贵阳": {"tier": 3, "pop_m": 6.1, "gdp_b": 5.3},
    "太原": {"tier": 3, "pop_m": 5.4, "gdp_b": 5.5},
    "南昌": {"tier": 3, "pop_m": 6.4, "gdp_b": 7.4},
    "金华": {"tier": 3, "pop_m": 7.1, "gdp_b": 5.9},
    "惠州": {"tier": 3, "pop_m": 6.1, "gdp_b": 5.4},
    "嘉兴": {"tier": 3, "pop_m": 5.5, "gdp_b": 6.7},
    "台州": {"tier": 3, "pop_m": 6.6, "gdp_b": 6.0},
    "绍兴": {"tier": 3, "pop_m": 5.3, "gdp_b": 7.4},
    "中山": {"tier": 3, "pop_m": 4.4, "gdp_b": 3.8},
    "珠海": {"tier": 3, "pop_m": 2.4, "gdp_b": 4.3},
    "保定": {"tier": 3, "pop_m": 9.2, "gdp_b": 3.9},
    "潍坊": {"tier": 3, "pop_m": 9.4, "gdp_b": 7.4},
    "烟台": {"tier": 3, "pop_m": 7.1, "gdp_b": 9.3},
    "揭阳": {"tier": 3, "pop_m": 5.6, "gdp_b": 2.3},
    "湖州": {"tier": 3, "pop_m": 3.4, "gdp_b": 4.0},
    # === 省级（覆盖整个省的搜索词） ===
    "广东": {"tier": "province", "pop_m": 127.0, "gdp_b": 135.7},
    "江苏": {"tier": "province", "pop_m": 85.2, "gdp_b": 128.2},
    "山东": {"tier": "province", "pop_m": 101.7, "gdp_b": 92.1},
    "浙江": {"tier": "province", "pop_m": 65.8, "gdp_b": 82.6},
    "河南": {"tier": "province", "pop_m": 98.8, "gdp_b": 59.1},
    "四川": {"tier": "province", "pop_m": 83.7, "gdp_b": 60.1},
    "湖北": {"tier": "province", "pop_m": 58.4, "gdp_b": 55.8},
    "福建": {"tier": "province", "pop_m": 41.9, "gdp_b": 54.1},
    "湖南": {"tier": "province", "pop_m": 66.2, "gdp_b": 50.0},
    "安徽": {"tier": "province", "pop_m": 61.3, "gdp_b": 47.1},
    "河北": {"tier": "province", "pop_m": 74.4, "gdp_b": 43.4},
    "陕西": {"tier": "province", "pop_m": 39.5, "gdp_b": 33.8},
    "江西": {"tier": "province", "pop_m": 45.3, "gdp_b": 32.2},
    "辽宁": {"tier": "province", "pop_m": 42.2, "gdp_b": 30.3},
    "云南": {"tier": "province", "pop_m": 47.0, "gdp_b": 30.0},
    "广西": {"tier": "province", "pop_m": 50.3, "gdp_b": 27.2},
    "贵州": {"tier": "province", "pop_m": 38.6, "gdp_b": 21.0},
    "山西": {"tier": "province", "pop_m": 34.8, "gdp_b": 25.4},
    "内蒙古": {"tier": "province", "pop_m": 24.0, "gdp_b": 23.9},
    "新疆": {"tier": "province", "pop_m": 25.9, "gdp_b": 19.1},
    "吉林": {"tier": "province", "pop_m": 23.4, "gdp_b": 13.7},
    "黑龙江": {"tier": "province", "pop_m": 31.0, "gdp_b": 15.9},
    "甘肃": {"tier": "province", "pop_m": 24.9, "gdp_b": 11.8},
    "海南": {"tier": "province", "pop_m": 10.3, "gdp_b": 7.6},
    "宁夏": {"tier": "province", "pop_m": 7.3, "gdp_b": 5.3},
    "青海": {"tier": "province", "pop_m": 5.9, "gdp_b": 3.8},
    "西藏": {"tier": "province", "pop_m": 3.6, "gdp_b": 2.4},
}


def _estimate_geo_market_weight(keyword: str) -> float:
    """
    从关键词中提取地域信息，根据覆盖人口和GDP估算市场权重

    返回 0 ~ 0.20 的加成值:
    - 全国性词 → ~0.20（覆盖14亿人口）
    - 省级词   → 0.10~0.18（按省GDP/人口）
    - 一线城市 → 0.08~0.15
    - 新一线   → 0.06~0.12
    - 三线城市 → 0.03~0.08
    - 无地域   → 0
    """
    # 全国性关键词
    national_markers = ("全国", "中国", "国内")
    if any(m in keyword for m in national_markers):
        return 0.20

    # 匹配地域参考数据 — 取最大匹配（"广东深圳"同时匹配省和市，取省）
    best_weight = 0.0
    for geo_name, data in _GEO_REFERENCE.items():
        if geo_name not in keyword:
            continue
        pop = data["pop_m"]   # 百万人
        gdp = data["gdp_b"]  # 千亿元
        tier = data["tier"]

        if tier == "province":
            # 省级权重: 基于人口和GDP的对数归一化
            # 广东(127M, 135千亿) → ~0.18, 西藏(3.6M, 2.4千亿) → ~0.06
            pop_factor = math.log10(max(pop, 1)) / math.log10(130)  # 归一化到广东
            gdp_factor = math.log10(max(gdp, 1)) / math.log10(136)
            weight = 0.06 + 0.14 * (pop_factor * 0.4 + gdp_factor * 0.6)
        else:
            # 城市权重: tier越高权重越大，同tier内按GDP区分
            tier_base = {1: 0.10, 2: 0.06, 3: 0.03}.get(tier, 0.02)
            gdp_bonus = min(0.08, gdp / 600)  # GDP千亿 / 600，最多+0.08
            weight = tier_base + gdp_bonus

        best_weight = max(best_weight, weight)

    # 通用地域后缀（"XX省"、"XX市"等，但未在参考表中）
    if best_weight == 0:
        geo_suffixes = ("省", "市", "区", "县", "镇", "乡")
        if any(keyword.endswith(s) or f"{s}的" in keyword or f"{s}哪" in keyword for s in geo_suffixes):
            best_weight = 0.03  # 未知地域的最小加成
        # 模糊本地词
        local_markers = ("附近", "本地", "当地", "周边", "身边")
        if any(m in keyword for m in local_markers):
            best_weight = max(best_weight, 0.04)

    return round(best_weight, 3)


def _calculate_price_guard(kw: dict) -> float:
    """
    基于市场信号计算价格保护系数（最小保留比例）

    返回值含义: 审计后价格 >= 原价 × guard
    例如 guard=0.85 表示最多允许降价15%

    计算依据（全部来自关键词自身数据）:
    1. SEM竞价 — 广告主愿意出的价格，直接反映市场认可度
    2. 搜索量 — 用户需求量级
    3. 搜索意图 — 商业意图越强，转化价值越高
    4. 价值/难度系数 — 定价引擎的综合评估
    5. 地域市场规模 — 覆盖人口和GDP
    6. 竞争密度 — 越多人竞争说明市场越有价值
    7. 竞品来源权威度 — 竞品发布在高AI引用率平台，说明定价有市场支撑

    Returns:
        0.50 ~ 0.95 之间的 float
    """
    keyword = kw.get("keyword", "")

    # 基线: 任何关键词至少保留50%的价格
    base = 0.50

    # ---- 信号1: SEM竞价（广告主用真金白银投票的市场价值） ----
    sem_price = float(kw.get("sem_price", 0) or 0)
    sem_bonus = min(0.12, sem_price / 125)

    # ---- 信号2: 搜索量（用户需求规模，对数衰减） ----
    search_volume = int(kw.get("search_volume", 0) or 0)
    vol_bonus = min(0.08, math.log10(max(search_volume, 1)) * 0.02) if search_volume > 0 else 0

    # ---- 信号3: 搜索意图（离成交越近，保护越强） ----
    intent = kw.get("intent", "informational")
    intent_bonus = {
        "transactional": 0.10,
        "commercial": 0.07,
        "navigational": 0.04,
        "informational": 0.0,
    }.get(intent, 0)

    # ---- 信号4: 定价引擎的综合评估 ----
    value_score = float(kw.get("value_score", 1.0) or 1.0)
    difficulty_score = float(kw.get("difficulty_score", 1.0) or 1.0)
    score_bonus = min(0.10, max(0, (value_score - 0.8)) * 0.08 + max(0, (difficulty_score - 0.8)) * 0.04)

    # ---- 信号5: 地域市场规模 ----
    geo_bonus = _estimate_geo_market_weight(keyword)

    # ---- 信号6: AI搜索竞争密度 ----
    competitor_count = int(kw.get("competitor_count", 0) or 0)
    comp_bonus = min(0.08, competitor_count * 0.005)

    # ---- 信号7: 竞品来源权威度（基于GEO调研AI引用率数据） ----
    # 如果竞品主要发布在高AI引用率平台（知乎/搜狐/今日头条），说明市场竞争是真实的
    # GEO调研数据: 知乎79.6%, 搜狐45.6%, 今日头条47.8%, 新浪48.1%
    # B级平台(知乎/搜狐/新浪)是AI引用的主力，竞品在这些平台=市场认可度高
    _HIGH_CITE_TIERS = {"B", "social"}   # 知乎/搜狐/抖音等AI高引用率平台
    _MID_CITE_TIERS = {"C", "D"}         # 今日头条/CSDN/门户 中等引用率
    source_auth = kw.get("source_authority", {})
    if source_auth:
        total_sources = sum(source_auth.values())
        if total_sources > 0:
            high_cite_ratio = sum(source_auth.get(t, 0) for t in _HIGH_CITE_TIERS) / total_sources
            mid_cite_ratio = sum(source_auth.get(t, 0) for t in _MID_CITE_TIERS) / total_sources
            # 竞品60%+在高引用率平台 → +0.06, 30%+在中引用率 → +0.03
            cite_bonus = min(0.06, high_cite_ratio * 0.10) + min(0.03, mid_cite_ratio * 0.05)
        else:
            cite_bonus = 0
    else:
        cite_bonus = 0

    # ---- 信号8: 全国词溢价保护 ----
    # 全国词覆盖全国市场，但Signal 5可能返回0（词中无城市名）
    # 需要补偿: 全国市场 ≥ 一线城市市场
    is_broad = kw.get("is_broad", False)
    geo_multiplier = float(kw.get("geo_multiplier", 1.0) or 1.0)
    broad_bonus = 0
    if is_broad and geo_multiplier > 1.0:
        # 1. 地域权重兜底: 全国词至少等同一线城市（补偿Signal 5的缺失）
        geo_bonus = max(geo_bonus, 0.15)
        # 2. 额外溢价保护: geo_multiplier越高，已定价越准确，保护越强
        broad_bonus = min(0.08, (geo_multiplier - 1.0) * 0.03)

    guard = base + sem_bonus + vol_bonus + intent_bonus + score_bonus + geo_bonus + comp_bonus + cite_bonus + broad_bonus

    # 上限: 永远允许至少5%的调整空间
    guard = min(0.95, guard)

    return round(guard, 3)


def _calculate_price_ceiling(kw: dict) -> float:
    """
    基于市场信号计算涨价上限（最大允许倍数）

    与 guard（保护下限）对称：guard约束降价，ceiling约束涨价
    低价值词不应被审计大幅涨价

    Returns:
        1.05 ~ 2.0 之间的 float（1.5 = 最多涨50%）
    """
    # 基线: 任何词最多涨100%
    ceiling = 2.0

    # 低SEM → 压低涨价空间（市场不认可的词不应该涨太多）
    sem_price = float(kw.get("sem_price", 0) or 0)
    if sem_price < 1:
        ceiling -= 0.30  # 无SEM → 最多涨70%

    # 信息获取型 → 压低
    intent = kw.get("intent", "informational")
    if intent == "informational":
        ceiling -= 0.20

    # 低搜索量 → 压低
    search_volume = int(kw.get("search_volume", 0) or 0)
    if search_volume < 100:
        ceiling -= 0.15

    # 低竞争 → 压低（没人竞争的词涨价没有依据）
    competitor_count = int(kw.get("competitor_count", 0) or 0)
    if competitor_count < 3:
        ceiling -= 0.15

    # 下限: 至少允许5%涨幅
    return max(1.05, round(ceiling, 2))


def _guard_explanation(kw: dict, guard: float) -> str:
    """生成保护系数的可读解释（用于审计备注）"""
    parts = []
    sem = float(kw.get("sem_price", 0) or 0)
    vol = int(kw.get("search_volume", 0) or 0)
    intent = kw.get("intent", "informational")
    comp = int(kw.get("competitor_count", 0) or 0)
    geo_w = _estimate_geo_market_weight(kw.get("keyword", ""))
    source_auth = kw.get("source_authority", {})

    if sem > 0:
        parts.append(f"SEM竞价¥{sem:.1f}")
    if vol > 0:
        parts.append(f"月搜索量{vol}")
    if intent in ("transactional", "commercial"):
        parts.append(f"{'购买' if intent == 'transactional' else '商业'}意图")
    if comp > 5:
        parts.append(f"{comp}个品牌竞争")
    if geo_w > 0.05:
        parts.append(f"地域市场权重{geo_w:.0%}")
    if source_auth:
        high = sum(source_auth.get(t, 0) for t in ("B", "social"))
        total = sum(source_auth.values())
        if total > 0 and high / total > 0.3:
            parts.append(f"竞品{high}/{total}条在高引用平台")
    if kw.get("is_broad") and float(kw.get("geo_multiplier", 1.0) or 1.0) > 1.0:
        parts.append(f"全国词(溢价{kw['geo_multiplier']:.1f}x)")

    evidence = "、".join(parts) if parts else "基础市场信号"
    max_cut = round((1 - guard) * 100)
    return f"基于{evidence}，最大允许降幅{max_cut}%"


# ========================================
# 1. 异常检测（纯规则，0成本，<1秒）
# ========================================

def _compute_batch_thresholds(scored_keywords: list[dict]) -> dict:
    """
    从批次数据分布计算动态阈值（替代硬编码常数）

    用百分位数适配不同行业/关键词组的特性:
    - 医疗行业 SEM 普遍高 → P75 高 → "高价值"门槛自动上移
    - 资讯行业 SEM 普遍低 → P75 低 → 即使 2 元也算异常高
    每个阈值设绝对下限，防止全零批次产生无意义检测
    """
    def _percentile(values: list, pct: float) -> float:
        if not values:
            return 0
        s = sorted(values)
        idx = min(int(pct * (len(s) - 1)), len(s) - 1)
        return s[idx]

    sems = [float(kw.get("sem_price", 0) or 0) for kw in scored_keywords]
    volumes = [int(kw.get("search_volume", 0) or 0) for kw in scored_keywords]
    comps = [int(kw.get("competitor_count", 0) or 0) for kw in scored_keywords]
    bidwords = [int(kw.get("bidword_company_count", 0) or 0) for kw in scored_keywords]
    prices = [kw.get("selling_price", 0) for kw in scored_keywords]

    # 价格 IQR（四分位距）用于统计异常检测
    p_q1 = _percentile(prices, 0.25)
    p_q3 = _percentile(prices, 0.75)
    p_iqr = max(p_q3 - p_q1, 1)  # 避免除0
    # [CTO-15.23 2026-05-11 P0#5] 加批次中位数 · 双向低价检测用
    p_median = _percentile(prices, 0.5)

    return {
        "sem_high": max(1.5, _percentile(sems, 0.75)),      # "高SEM" = P75 或至少1.5元
        "bidword_high": max(3, _percentile(bidwords, 0.75)), # "高竞价企业" = P75 或至少3家
        "comp_low": max(2, _percentile(comps, 0.25)),        # "低竞争" = P25 或至少2条
        "comp_high": max(8, _percentile(comps, 0.75)),       # "高竞争" = P75 或至少8条
        "volume_low": max(20, _percentile(volumes, 0.25)),   # "低搜索量" = P25 或至少20
        "price_q1": p_q1,
        "price_q3": p_q3,
        "price_iqr": p_iqr,
        "price_median": p_median,
    }


def detect_anomalies(scored_keywords: list[dict]) -> list[dict]:
    """
    从已评分的关键词中找出价格可疑的词

    不调任何API，纯规则+统计检测
    阈值基于批次数据分布动态计算（非硬编码）

    Returns:
        可疑词列表，按严重程度排序
    """
    if len(scored_keywords) < 3:
        return []

    anomalies = []

    # 基于批次分布计算动态阈值
    thresholds = _compute_batch_thresholds(scored_keywords)

    for kw in scored_keywords:
        reasons = []

        sem_price = float(kw.get("sem_price", 0) or 0)
        search_volume = int(kw.get("search_volume", 0) or 0)
        bidword_companies = int(kw.get("bidword_company_count", 0) or 0)
        competitor_count = int(kw.get("competitor_count", 0) or 0)
        intent = kw.get("intent", "")
        value_score = kw.get("value_score", 1.0)
        difficulty_score = kw.get("difficulty_score", 1.0)
        selling_price = kw.get("selling_price", 0)
        keyword = kw.get("keyword", "")

        # ---- 规则1: 高商业价值 + 低竞争 = 报价可能偏低 ----
        # 阈值动态: "高"和"低"相对于本批次关键词分布
        high_value_signals = (sem_price >= thresholds["sem_high"]) or (bidword_companies >= thresholds["bidword_high"])
        low_competition = competitor_count <= thresholds["comp_low"]

        if high_value_signals and low_competition:
            reasons.append(
                f"商业价值高于批次P75(SEM≥{thresholds['sem_high']:.1f}或竞价企业≥{thresholds['bidword_high']})"
                f"但AI竞争采样仅{competitor_count}条(≤P25={thresholds['comp_low']})→初始采样可能不全,报价偏低"
            )

        # ---- 规则2: 低价值 + 高竞争 = 报价可能偏高 ----
        low_value_signals = (sem_price < 0.5 and search_volume < thresholds["volume_low"])
        high_competition = competitor_count >= thresholds["comp_high"]

        if low_value_signals and high_competition:
            reasons.append(
                f"商业价值低(SEM<0.5,搜索量<{thresholds['volume_low']})"
                f"但竞争{competitor_count}条(≥P75={thresholds['comp_high']})→竞品分级可能偏高,报价偏高"
            )

        # ---- 规则3: 含强商业意图词但被判为informational ----
        commercial_words = ["推荐", "哪家好", "排名", "价格", "多少钱", "怎么选", "费用", "哪家强"]
        has_commercial = any(w in keyword for w in commercial_words)

        if has_commercial and intent == "informational":
            reasons.append(
                f"含商业词'{next(w for w in commercial_words if w in keyword)}'"
                f"但意图判为informational→意图分类可能有误"
            )

        # ---- 规则4: IQR价格异常检测（比z-score对偏态分布更鲁棒）----
        iqr = thresholds["price_iqr"]
        lower_fence = thresholds["price_q1"] - 1.5 * iqr
        upper_fence = thresholds["price_q3"] + 1.5 * iqr

        if (selling_price < lower_fence or selling_price > upper_fence) and not _price_within_band(kw):
            direction = "偏高" if selling_price > upper_fence else "偏低"
            deviation = (selling_price - thresholds["price_q3"]) / iqr if selling_price > upper_fence \
                else (thresholds["price_q1"] - selling_price) / iqr
            reasons.append(
                f"价格{selling_price}元超出IQR围栏"
                f"[{int(lower_fence)}-{int(upper_fence)}],"
                f"偏离{deviation:.1f}×IQR→统计异常({direction})"
            )

        # ---- 规则5: value_score和difficulty_score方向矛盾 ----
        if value_score >= 1.5 and difficulty_score <= 0.9:
            reasons.append(
                f"高价值(value={value_score})但低难度(difficulty={difficulty_score})"
                f"→信号矛盾,可能有数据源不准"
            )
        if value_score <= 0.85 and difficulty_score >= 1.8:
            reasons.append(
                f"低价值(value={value_score})但高难度(difficulty={difficulty_score})"
                f"→做这个词可能不划算"
            )

        # ---- [CTO-15.23 2026-05-11 P0#5] 规则6:绝对低价检测(双向拦截) ----
        # 老板痛点:审计只标黄高价词,低价异常(¥153/¥120 这种)漏审 · 销售误以为定价合理
        # 跳过品牌词例外(_make_brand_keyword_price 固定低价是预期行为)
        # [v2.1 2026-06-11] v2 成本驱动词也跳过:出厂价 = 篇数 × 单篇成本 = 结构性 ≥ 真实成本,
        #   低竞争词 ¥245-490 是合法成本价(markup=1 默认)· 按旧阈值会大面积误报白烧 LLM 审计
        ABSOLUTE_LOW_PRICE = MIN_KEYWORD_PRICE * 1.5  # = 400 × 1.5 = 600
        _is_v2_cost_driven = str(kw.get("pricing_formula_version") or "").startswith("v2.")
        if (not kw.get("is_brand_keyword", False)
            and not _is_v2_cost_driven
            and selling_price < ABSOLUTE_LOW_PRICE
            and sem_price > 1.0):
            reasons.append(
                f"绝对低价异常:售价{selling_price}元低于阈值{int(ABSOLUTE_LOW_PRICE)}元"
                f",但市场SEM竞价{sem_price}元(广告主愿付钱),报价严重偏低"
            )

        # ---- [CTO-15.23 2026-05-11 P0#5] 规则7:批次内明显低估 ----
        # 老板说"我发现他现在会审计价格高的词,但是价格低的词他不审计"
        # IQR 检测(规则4)中 lower_fence 经常 < 0,实际上"价格 < 中位数 30%"+ 有竞争更敏感
        price_median = thresholds["price_median"]
        if (not kw.get("is_brand_keyword", False)
            and price_median > 200
            and selling_price < price_median * 0.3
            and competitor_count >= 3
            and selling_price > 0
            and not _price_within_band(kw)):  # v1.3:band 内的低价是预期(冷门词在 band 下沿)·不算低估
            reasons.append(
                f"批次内异常低估:售价{selling_price}元 < 中位数{int(price_median)}元×30%"
                f",有{competitor_count}个竞品,意图={intent},定价可能漏算"
            )

        if reasons:
            anomalies.append({
                "keyword": keyword,
                "current_price": selling_price,
                "reasons": reasons,
                "severity": len(reasons),
                "raw_data": {
                    "sem_price": sem_price,
                    "search_volume": search_volume,
                    "bidword_company_count": bidword_companies,
                    "competitor_count": competitor_count,
                    "intent": intent,
                    "value_score": value_score,
                    "difficulty_score": difficulty_score,
                    "content_count": kw.get("content_count", 0),
                    "data_source": kw.get("data_source", ""),
                    "is_broad": kw.get("is_broad", False),
                    "geo_multiplier": kw.get("geo_multiplier", 1.0),
                },
            })

    # 语义相似词价格差异检测
    _detect_similar_keyword_gaps(scored_keywords, anomalies)

    # 按严重程度排序
    anomalies.sort(key=lambda x: -x["severity"])

    return anomalies


def _detect_similar_keyword_gaps(
    scored_keywords: list[dict],
    anomalies: list[dict],
):
    """检测语义相似的关键词之间价格差异过大的情况"""
    existing_kws = {a["keyword"] for a in anomalies}

    for i, kw1 in enumerate(scored_keywords):
        for kw2 in scored_keywords[i + 1:]:
            # 简单相似度：共享词数 / 总词数
            words1 = set(kw1["keyword"])
            words2 = set(kw2["keyword"])
            overlap = len(words1 & words2) / max(len(words1 | words2), 1)

            if overlap < 0.6:
                continue

            p1 = kw1["selling_price"]
            p2 = kw2["selling_price"]
            if min(p1, p2) == 0:
                continue

            ratio = max(p1, p2) / min(p1, p2)
            # v1.3:两词各自都在自己 band 内 → 3 倍差是跨 band(市 vs 全国 同根词)的合理差异·非异常
            if ratio >= 3.0 and not (_price_within_band(kw1) and _price_within_band(kw2)):
                cheaper = kw1 if p1 < p2 else kw2
                dearer = kw1 if p1 >= p2 else kw2
                reason = (
                    f"与'{dearer['keyword']}'({dearer['selling_price']}元)语义相似"
                    f"但价格差{ratio:.1f}倍→某个定价数据可能有误"
                )

                # 两个词都标记
                for kw_data in [cheaper, dearer]:
                    if kw_data["keyword"] not in existing_kws:
                        anomalies.append({
                            "keyword": kw_data["keyword"],
                            "current_price": kw_data["selling_price"],
                            "reasons": [reason],
                            "severity": 1,
                            "raw_data": {
                                "sem_price": kw_data.get("sem_price", 0),
                                "search_volume": kw_data.get("search_volume", 0),
                                "bidword_company_count": kw_data.get("bidword_company_count", 0),
                                "competitor_count": kw_data.get("competitor_count", 0),
                                "intent": kw_data.get("intent", ""),
                                "value_score": kw_data.get("value_score", 0),
                                "difficulty_score": kw_data.get("difficulty_score", 0),
                                "content_count": kw_data.get("content_count", 0),
                                "data_source": kw_data.get("data_source", ""),
                            },
                        })
                        existing_kws.add(kw_data["keyword"])


# ========================================
# 2. LLM审计（1次调用，~15秒，~0.05元）
# ========================================

async def llm_audit_anomalies(anomalies: list[dict], industry: str = "") -> list[dict]:
    """
    让LLM审查可疑词，判断是数据问题还是合理现象

    Args:
        anomalies: detect_anomalies() 的输出
        industry: 行业背景

    Returns:
        审计结果列表，每项含:
        - keyword, verdict, explanation, action, adjust_factor
        - action: "deep_probe" / "adjust_up" / "adjust_down" / "keep"
    """
    from tools.multi_llm_caller import call_llm_with_fallback

    if not anomalies:
        return []

    # 最多审计15个（控制prompt长度）
    to_audit = anomalies[:15]

    items = []
    for i, a in enumerate(to_audit):
        rd = a["raw_data"]
        # 告诉LLM这个词的降价保护范围，涨价无限制
        guard = _calculate_price_guard(rd)
        geo_tag = ""
        if rd.get("is_broad"):
            geo_tag = f", 地域范围=全国(溢价{rd.get('geo_multiplier', 1.0):.1f}x)"

        items.append(
            f"{i + 1}. 关键词: {a['keyword']}\n"
            f"   当前报价: {a['current_price']}元/月\n"
            f"   可疑原因: {'; '.join(a['reasons'])}\n"
            f"   原始数据: 市场竞价参考={rd['sem_price']}元, "
            f"搜索量={rd['search_volume']}, "
            f"AI搜索竞品数={rd['competitor_count']}条, "
            f"相关内容总量={rd['content_count']}条, "
            f"付费竞争企业={rd['bidword_company_count']}家, "
            f"意图={rd['intent']}, "
            f"价值系数={rd['value_score']}, "
            f"难度系数={rd['difficulty_score']}"
            f"{geo_tag}\n"
            f"   降价保护: 最低保留{guard:.0%}(最多降{(1-guard)*100:.0f}%), 涨价无上限"
        )

    industry_hint = f"\n行业背景: {industry}" if industry else ""

    prompt = f"""你是GEO(生成式引擎优化)行业定价审计专家。{industry_hint}

以下{len(to_audit)}个关键词的报价数据存在矛盾，请逐个审查判断：

{chr(10).join(items)}

对每个词做出判断:
- verdict: "数据可疑"(数据源可能不准,需要重新调查) / "合理"(可以解释为什么矛盾) / "修正"(数据明显有问题,可以直接调整)
- explanation: 30字以内的判断理由
- action: "deep_probe"(需要直接问AI引擎验证) / "adjust_up"(应上调价格) / "adjust_down"(应下调价格) / "keep"(保持不变)
- adjust_factor: 如果action是adjust_up或adjust_down, 建议的调整系数(如1.5=上调50%, 0.7=下调30%), 否则填1.0
  重要: 降价有保护下限(每个词不同)，低于下限会被截断; 涨价无上限，如果初始定价严重偏低请大胆上调

判断原则:
1. "高价值+低竞争"的词大概率是初始采样不全,真实竞争更激烈 → deep_probe
2. "低价值+高竞争"的词大概率是LLM把B/C类内容误判为A类 → adjust_down
3. 含"推荐/排名/哪家好"但被判为informational → adjust_up(意图应该是commercial)
4. 统计异常但原始数据合理 → keep
5. 无地域限制的全国词已包含地域溢价系数,不要因为"看起来贵"就降价——全国竞争的投放成本确实远高于地域词
5. 语义相似词价差过大 → deep_probe(需要验证哪个是对的)
6. 含地名的词（省/市级）覆盖人口多,市场大,谨慎下调

严格按JSON数组返回,不要其他文字:
[{{"keyword": "...", "verdict": "...", "explanation": "...", "action": "...", "adjust_factor": 1.0}}]"""

    try:
        response = await call_llm_with_fallback(prompt, verbose=False)

        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if not json_match:
            print("  [审计] LLM返回无法解析,跳过审计")
            return []

        results = json.loads(json_match.group())

        # 验证和清洗 — LLM的adjust_factor不做硬性截断，
        # 最终在 apply_corrections 中由 price_guard 动态约束
        cleaned = []
        for item in results:
            action = item.get("action", "keep")
            if action not in ("deep_probe", "adjust_up", "adjust_down", "keep"):
                action = "keep"

            factor = float(item.get("adjust_factor", 1.0))
            # 仅做合理性校验: 不允许极端值（翻5倍或砍到1折）
            factor = max(0.1, min(5.0, factor))

            cleaned.append({
                "keyword": item.get("keyword", ""),
                "verdict": item.get("verdict", "合理"),
                "explanation": item.get("explanation", ""),
                "action": action,
                "adjust_factor": factor,
            })

        return cleaned

    except Exception as e:
        print(f"  [审计] LLM审计失败: {e}")
        return []


# ========================================
# 3. AI深探（复用ai_tester能力）
# ========================================

async def deep_probe_keywords(
    keywords: list[str],
    concurrency: int = 3,
) -> dict[str, dict]:
    """
    对指定关键词做AI引擎直接探测

    复用现有的AI引擎调用能力,向DeepSeek(秘塔)/Kimi/DashScope
    发送真实搜索请求,解析回答中的品牌和来源

    Args:
        keywords: 需要深探的关键词列表
        concurrency: 并发数

    Returns:
        {关键词: {competitor_brands, avg_citations, cited_source_tiers, raw_responses}}
    """
    from tools.multi_llm_caller import call_llm_with_fallback

    if not keywords:
        return {}

    print(f"  [深探] 对{len(keywords)}个可疑词进行AI引擎直测...")

    semaphore = asyncio.Semaphore(concurrency)
    results = {}

    async def probe_one(keyword: str):
        async with semaphore:
            return keyword, await _probe_single_keyword(keyword)

    tasks = [probe_one(kw) for kw in keywords]
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    for resp in responses:
        if isinstance(resp, Exception):
            continue
        kw, data = resp
        results[kw] = data

    return results


async def _probe_single_keyword(keyword: str) -> dict:
    """
    对单个关键词进行AI引擎探测

    策略: 用秘塔RAG(DeepSeek后端)做一次联网搜索,
    然后用LLM从回答中提取品牌和来源
    """
    from tools.competition_analyzer import search_metaso

    # 构造自然搜索问题
    query = f"推荐几家做{keyword}比较好的公司或服务商"

    # 用秘塔的问答模式（自带联网搜索）
    # 这里复用search_metaso但用问答模式
    try:
        from tools.multi_llm_caller import MultiLLMCaller

        # 用DashScope联网搜索（最稳定）
        import httpx
        import os
        api_key = os.getenv("DASHSCOPE_API_KEY", "")
        if not api_key:
            return _empty_probe_result()

        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=60) as client:
            async with llm_track(
                "pricing_auditor_deep_probe",
                "dashscope",
                model="qwen3.7-max",
                metadata={"enable_search": True},
            ) as tracker:
                response = await client.post(
                    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "qwen3.7-max",
                        "input": {"messages": [{"role": "user", "content": query}]},
                        "parameters": {
                            "max_tokens": 2000,
                            "enable_search": True,
                            "search_options": {
                                "enable_source": True,
                                "enable_citation": True,
                                "search_strategy": "turbo",
                                "forced_search": True,
                            },
                            "result_format": "message",
                        },
                    },
                )
                data_for_usage = response.json() if response.status_code == 200 else {}
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data_for_usage)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=response.status_code == 200,
                    error_msg=None if response.status_code == 200 else response.text[:200],
                )

            if response.status_code != 200:
                return _empty_probe_result()

            data = data_for_usage
            output = data.get("output", {})
            ai_text = output.get("choices", [{}])[0].get("message", {}).get("content", "")

            # 提取搜索来源
            search_info = output.get("search_info", {})
            search_results = search_info.get("search_results", [])

            # 从引用来源中分析权威度分布
            source_tiers = []
            for sr in search_results:
                url = sr.get("url", sr.get("link", ""))
                tier = _classify_url_authority(url)
                source_tiers.append(tier)

            # 用LLM从回答中提取被推荐的品牌
            brands = await _extract_brands_from_response(ai_text, keyword)

            return {
                "competitor_brands": len(brands),
                "brand_list": brands,
                "source_count": len(search_results),
                "source_tiers": source_tiers,
                "authority_distribution": _count_tiers(source_tiers),
                "ai_response_preview": ai_text[:200],
            }

    except Exception as e:
        print(f"    [深探] {keyword} 探测失败: {e}")
        return _empty_probe_result()


def _empty_probe_result() -> dict:
    return {
        "competitor_brands": 0,
        "brand_list": [],
        "source_count": 0,
        "source_tiers": [],
        "authority_distribution": {},
        "ai_response_preview": "",
    }


async def _extract_brands_from_response(ai_text: str, keyword: str) -> list[str]:
    """从AI回答中提取被推荐的品牌名称"""
    from tools.multi_llm_caller import call_llm_with_fallback

    if not ai_text or len(ai_text) < 20:
        return []

    prompt = f"""从以下AI搜索回答中，提取所有被推荐/提及的公司名或品牌名。

搜索词: {keyword}
AI回答:
{ai_text[:1500]}

只提取公司名/品牌名,忽略平台名(如知乎、百度等)。
严格按JSON数组返回,不要其他文字: ["品牌A", "品牌B", ...]
如果没有提及任何品牌,返回空数组: []"""

    try:
        response = await call_llm_with_fallback(prompt, verbose=False)
        json_match = re.search(r'\[.*?\]', response, re.DOTALL)
        if json_match:
            brands = json.loads(json_match.group())
            return [b for b in brands if isinstance(b, str) and len(b) >= 2]
    except Exception:
        pass

    return []


# ========================================
# 来源权威度分级
# ========================================

_S_DOMAINS = ["people.com.cn", "xinhuanet.com", "cctv.com", "gov.cn", "china.com.cn"]
_A_DOMAINS = ["36kr.com", "iyiou.com", "jiqizhixin.com", "huxiu.com", "tmtpost.com", "leiphone.com"]
_B_DOMAINS = ["zhihu.com", "baijiahao.baidu.com", "sohu.com", "163.com", "qq.com", "sina.com.cn", "sina.cn"]
_C_DOMAINS = ["csdn.net", "cnblogs.com", "jianshu.com", "toutiao.com", "bilibili.com"]
_SOCIAL_DOMAINS = ["douyin.com", "xiaohongshu.com", "weibo.com"]

def _classify_url_authority(url: str) -> str:
    """根据URL判断权威等级: S/A/B/C/D/E/social"""
    if not url:
        return "D"
    for d in _S_DOMAINS:
        if d in url:
            return "S"
    for d in _A_DOMAINS:
        if d in url:
            return "A"
    for d in _B_DOMAINS:
        if d in url:
            return "B"
    for d in _C_DOMAINS:
        if d in url:
            return "C"
    for d in _SOCIAL_DOMAINS:
        if d in url:
            return "social"
    return "D"


def _count_tiers(tiers: list[str]) -> dict:
    """统计权威等级分布"""
    counts = {}
    for t in tiers:
        counts[t] = counts.get(t, 0) + 1
    return counts


# ========================================
# 4. 应用修正（数据驱动的价格保护）
# ========================================

def apply_corrections(
    scored_keywords: list[dict],
    audit_results: list[dict],
    probe_results: dict[str, dict],
) -> list[dict]:
    """
    根据审计和深探结果修正关键词价格

    核心机制: 每个关键词的调整幅度由 _calculate_price_guard() 动态约束，
    而非硬编码的固定系数。guard 越高，允许的降幅越小。

    Args:
        scored_keywords: 原始评分结果
        audit_results: LLM审计结果
        probe_results: AI深探结果

    Returns:
        修正后的评分结果（原列表被修改）
    """
    # 构建审计结果索引
    audit_map = {a["keyword"]: a for a in audit_results}

    corrections_applied = 0

    for kw in scored_keywords:
        keyword = kw["keyword"]
        audit = audit_map.get(keyword)

        if not audit:
            continue

        action = audit.get("action", "keep")
        factor = audit.get("adjust_factor", 1.0)
        old_price = kw["selling_price"]

        # 计算此关键词的市场信号保护系数
        guard = _calculate_price_guard(kw)

        if action == "keep":
            kw["audit_status"] = "reviewed_ok"
            kw["audit_note"] = audit.get("explanation", "")
            continue

        if action in ("adjust_up", "adjust_down"):
            # 用 guard 约束下调幅度: factor 不能低于 guard
            effective_factor = max(guard, factor) if factor < 1.0 else factor
            # 涨价不设上限: 初始定价可能因数据缺失严重偏低，审计涨价是纠错不是投机

            # [v2.1 2026-06-11] v2 词(成本驱动 · 价 = 篇数 × 单篇成本 × 系数)调价必须落到【底盘 cost】:
            #   只改 selling_price 会被 recalculate_for_tier 的 SSOT 现算覆盖回原价 → 审计标签说"已调价"
            #   但客户价没变(误导销售)。改 cost_per_article 让 SSOT 现算自然生效(同 deep_probe 模式)。
            if str(kw.get("pricing_formula_version") or "").startswith("v2."):
                old_cost = float(kw.get("cost_per_article", 60) or 60)
                new_cost = max(35.0, round(old_cost * effective_factor, 1))
                kw["cost_per_article"] = new_cost
                v2_data = kw.get("v2_assessor_data")
                if isinstance(v2_data, dict):
                    v2_data["cost_per_article"] = new_cost
                    v2_data.setdefault("guards", {})["audit_cost_factor"] = effective_factor
                kw["selling_price"] = max(MIN_KEYWORD_PRICE, int(old_price * effective_factor))
            else:
                kw["selling_price"] = max(MIN_KEYWORD_PRICE, int(old_price * effective_factor))
            kw["audit_status"] = "adjusted"

            guard_note = _guard_explanation(kw, guard) if effective_factor != factor else ""
            kw["audit_note"] = (
                f"{audit.get('explanation', '')} (LLM建议x{factor:.2f}"
                f"{'→受保护约束为x' + f'{effective_factor:.2f}' if effective_factor != factor else ''}"
                f"{'，' + guard_note if guard_note else ''})"
            )
            kw["price_before_audit"] = old_price
            corrections_applied += 1

        if action == "deep_probe" and keyword in probe_results:
            probe = probe_results[keyword]
            competitor_brands = probe.get("competitor_brands", 0)

            # 深探额外收益：用探测到的来源权威分布更新单篇成本
            probe_auth = probe.get("authority_distribution", {})
            if probe_auth and sum(probe_auth.values()) > 0:
                from tools.transparent_pricing import get_dynamic_cost_per_article
                new_cost = get_dynamic_cost_per_article(probe_auth)
                old_cost = kw.get("cost_per_article", 60)
                if abs(new_cost - old_cost) / max(old_cost, 1) > 0.15:
                    kw["cost_per_article"] = round(new_cost, 1)
                    kw["_cost_updated_by_probe"] = True

            if competitor_brands > 0:
                old_competitors = kw.get("competitor_count", 1)

                if competitor_brands > old_competitors * 1.5:
                    # 深探发现竞争比初始采样激烈得多 → 按实际竞争比例上调
                    ratio = competitor_brands / max(old_competitors, 1)
                    adjust = 1.0 + (ratio - 1) * 0.6
                    kw["selling_price"] = max(MIN_KEYWORD_PRICE, int(old_price * adjust))
                    kw["audit_status"] = "deep_probed_up"
                    kw["audit_note"] = (
                        f"多引擎交叉验证发现{competitor_brands}个品牌实际竞争"
                        f"(初始采样{old_competitors}条偏低) → 系数{adjust:.2f}"
                    )
                elif competitor_brands < old_competitors * 0.5:
                    # 深探发现竞争没那么激烈 → 下调，但受 guard 保护
                    raw_factor = competitor_brands / max(old_competitors, 1)
                    effective_factor = max(guard, raw_factor)
                    kw["selling_price"] = max(MIN_KEYWORD_PRICE, int(old_price * effective_factor))
                    kw["audit_status"] = "deep_probed_down"
                    guard_note = _guard_explanation(kw, guard)
                    kw["audit_note"] = (
                        f"深探{competitor_brands}品牌vs初始{old_competitors}条 → "
                        f"原始系数{raw_factor:.2f}，{guard_note}，"
                        f"实际系数{effective_factor:.2f}"
                    )
                else:
                    kw["audit_status"] = "deep_probed_confirmed"
                    kw["audit_note"] = f"多引擎交叉验证确认{competitor_brands}个竞争品牌,定价合理"

                kw["price_before_audit"] = old_price
                kw["probe_brands"] = probe.get("brand_list", [])
                corrections_applied += 1
            else:
                kw["audit_status"] = "deep_probe_failed"
                kw["audit_note"] = "多引擎验证暂未获取到有效竞争数据,建议保留当前定价"

    if corrections_applied > 0:
        print(f"  [审计] 修正了{corrections_applied}个关键词的价格")

    # ========== 一致性回检：相似词修正后价差不应扩大 ==========
    _post_correction_consistency_check(scored_keywords)

    return scored_keywords


def _post_correction_consistency_check(scored_keywords: list[dict]):
    """
    审计后一致性校验：检测语义相似词的价格在修正后是否更不一致

    只对被审计修改过的词做检查，如果修正后相似词价差反而扩大，
    则将偏差较大的一方拉向中间值
    """
    adjusted_kws = [kw for kw in scored_keywords if kw.get("price_before_audit")]
    if len(adjusted_kws) < 2:
        return

    kw_map = {kw["keyword"]: kw for kw in scored_keywords}

    for kw in adjusted_kws:
        keyword = kw["keyword"]
        current_price = kw["selling_price"]

        # 找语义相似词（字符重叠度>60%）
        for other in scored_keywords:
            if other["keyword"] == keyword:
                continue
            w1 = set(keyword)
            w2 = set(other["keyword"])
            overlap = len(w1 & w2) / max(len(w1 | w2), 1)
            if overlap < 0.6:
                continue

            other_price = other["selling_price"]
            if min(current_price, other_price) == 0:
                continue

            ratio = max(current_price, other_price) / min(current_price, other_price)

            # 修正前的价差
            old_price = kw.get("price_before_audit", current_price)
            other_old = other.get("price_before_audit", other_price)
            if min(old_price, other_old) > 0:
                old_ratio = max(old_price, other_old) / min(old_price, other_old)
            else:
                old_ratio = 1.0

            # 如果修正后价差比修正前更大（且>2.5倍），拉回
            if ratio > 2.5 and ratio > old_ratio * 1.2:
                midpoint = int((current_price + other_price) / 2)
                # 只调整被审计过的那个词，向中间靠拢30%
                if kw.get("price_before_audit"):
                    adjusted = int(current_price + (midpoint - current_price) * 0.3)
                    kw["selling_price"] = max(MIN_KEYWORD_PRICE, adjusted)
                    kw["audit_note"] = (
                        kw.get("audit_note", "") +
                        f" [一致性校验: 与'{other['keyword']}'价差{ratio:.1f}x→修正]"
                    )


# ========================================
# 5. 完整审计流程（一键调用）
# ========================================

def apply_audit_flags(
    scored_keywords: list[dict],
    audit_results: list[dict],
    probe_results: dict[str, dict],
) -> int:
    """【v1.3 · 老板拍 A:只标警不改价】审计结果只写 needs_review + audit_note,**绝不改 selling_price**。

    为什么不改价:v1.3 band 体系把价做成确定性公式(稳定+可审计)。LLM/深探改价 = 把不稳定请回来,
    且会把 band 故意按竞争铺开的差异当"离群"抹平。故 LLM 只当探测器挑刺,价仍走 band 公式;
    真异常 → 标复核 → admin 调 band(provisional),不是改单次报价。返回标记数。
    """
    audit_map = {a["keyword"]: a for a in audit_results}
    flagged = 0
    _DIR = {"adjust_up": "疑偏低", "adjust_down": "疑偏高", "deep_probe": "需核实竞争"}
    for kw in scored_keywords:
        audit = audit_map.get(kw.get("keyword"))
        if not audit:
            continue
        action = audit.get("action", "keep")
        explanation = audit.get("explanation", "")
        if action == "keep":
            kw["audit_status"] = "reviewed_ok"
            kw["audit_note"] = explanation
            continue
        # adjust_up / adjust_down / deep_probe → 只标复核 · 价不动
        kw["needs_review"] = True
        kw["audit_status"] = "flagged"
        kw["sanity_verdict"] = audit.get("verdict", "")
        note = f"⚠️ 价格复核({_DIR.get(action, '疑可疑')}):{explanation}"
        if action in ("adjust_up", "adjust_down"):
            note += f"(LLM 参考系数 x{audit.get('adjust_factor', 1.0):.2f}·价未改·需 admin 核 band)"
        # deep_probe 的探测结果作为复核佐证(不改价)
        if action == "deep_probe" and kw.get("keyword") in probe_results:
            brands = probe_results[kw["keyword"]].get("competitor_brands", 0)
            if brands:
                note += f"(深探见 {brands} 个竞争品牌)"
        kw["audit_note"] = note
        flagged += 1
    if flagged:
        print(f"  [审计] 标记 {flagged} 个价格可疑词(needs_review · 价未改 · v1.3 flag-only)")
    return flagged


async def audit_and_correct(
    scored_keywords: list[dict],
    industry: str = "",
    flag_only: bool = True,
) -> tuple[list[dict], dict]:
    """
    完整审计流程: 异常检测 → LLM审计 → 深探 → **标警(默认 flag_only · 不改价)**

    Args:
        scored_keywords: score_keywords() 的输出
        industry: 行业
        flag_only: True(默认·v1.3)= 只标 needs_review 不改价(价走确定性 band);
                   False = 旧行为 apply_corrections 改价(保留给 admin 手动审计/兼容)

    Returns:
        (scored_keywords, 审计报告)
    """
    # Step 1: 异常检测
    anomalies = detect_anomalies(scored_keywords)

    if not anomalies:
        print("  [审计] 未发现价格异常,跳过审计")
        return scored_keywords, {"anomalies": 0, "audited": 0, "probed": 0, "corrected": 0}

    print(f"  [审计] 发现{len(anomalies)}个可疑词,开始LLM审计...")

    # Step 2: LLM审计
    audit_results = await llm_audit_anomalies(anomalies, industry)

    # Step 3: 筛选需要深探的词
    deep_probe_kws = [
        a["keyword"] for a in audit_results
        if a.get("action") == "deep_probe"
    ]

    # Step 4: 深探
    probe_results = {}
    if deep_probe_kws:
        print(f"  [审计] {len(deep_probe_kws)}个词需要AI深探: {deep_probe_kws}")
        probe_results = await deep_probe_keywords(deep_probe_kws)

    # Step 5: v1.3 默认 flag_only(只标警不改价·价走确定性 band)· flag_only=False 才走旧 apply_corrections 改价
    if flag_only:
        flagged = apply_audit_flags(scored_keywords, audit_results, probe_results)
        corrected = 0
    else:
        scored_keywords = apply_corrections(scored_keywords, audit_results, probe_results)
        flagged = 0
        corrected = sum(1 for k in scored_keywords if k.get("audit_status", "").startswith(("adjusted", "deep_probed")))

    # 审计报告
    report = {
        "anomalies": len(anomalies),
        "anomaly_keywords": [a["keyword"] for a in anomalies],
        "audited": len(audit_results),
        "audit_details": audit_results,
        "probed": len(deep_probe_kws),
        "probe_keywords": deep_probe_kws,
        "flag_only": flag_only,
        "flagged": flagged,           # v1.3:标复核数(不改价)
        "corrected": corrected,       # 仅 flag_only=False 改价数
    }

    print(
        f"  [审计] 完成: {report['anomalies']}个可疑 → "
        f"{report['audited']}个审计 → {report['probed']}个深探 → "
        f"{'标警 ' + str(flagged) + ' 个(价未改)' if flag_only else '修正 ' + str(corrected) + ' 个'}"
    )

    return scored_keywords, report
