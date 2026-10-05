"""
tools/keyword_price_inversion.py — 地域价格倒挂自检(工单 2026-07-26 · P1-6)

【治什么】
生产实证(驰鲸 brand 712 / session 200):
  街道级废词 "龙岗横岗TikTok外贸获客代运营公司"(月搜 60)标 ¥5760 / 18 篇,
  比城市级真词 "深圳龙岗TikTok外贸获客代运营公司推荐"(月搜 120)的 ¥3250 / 10 篇
  **贵 77%**。越细的地域 = 越少人搜 = 越该便宜,反过来就是定价输入被编造数据带偏了。

【怎么治】
本模块**不改任何定价公式**(`generate_batch_quote` / `transparent_pricing` /
`compute_v2_tier_price` 一行不动),只做一件事:在同一批报价里发现"下级地域词比
上级地域词还贵",就把这些词标 `needs_review=True` 强制人工审,并给出可解释的原因。

【地域层级怎么判】
优先用 LLM 定价评估师已经算好的 `city_tier`(tier1/…/tier4/county/township),
再用关键词文本兜底(区/县后缀、街道/片区词、城市名表)。两者取更深的一个。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# 地域颗粒度层级(数字越大越细)
DEPTH_NONE = 0        # 无地域锚点 / 全国词
DEPTH_CITY = 1        # 城市级
DEPTH_DISTRICT = 2    # 区 / 县
DEPTH_STREET = 3      # 街道 / 镇 / 片区

DEPTH_LABELS = {
    DEPTH_NONE: "全国/无地域",
    DEPTH_CITY: "城市级",
    DEPTH_DISTRICT: "区县级",
    DEPTH_STREET: "街道级",
}

# LLM 评估师的 city_tier → 地域颗粒度
_CITY_TIER_DEPTH = {
    "national": DEPTH_NONE,
    "tier1": DEPTH_CITY,
    "new_tier1": DEPTH_CITY,
    "tier2": DEPTH_CITY,
    "tier3": DEPTH_CITY,
    "tier4": DEPTH_CITY,
    "county": DEPTH_DISTRICT,
    "township": DEPTH_STREET,
}

INVERSION_RISK_FLAG = "geo_price_inversion"


def _text_depth(
    keyword: str,
    cities: Optional[Iterable[str]] = None,
    sub_regions: Optional[Iterable[str]] = None,
    streets: Optional[Iterable[str]] = None,
) -> int:
    """纯文本兜底判定地域颗粒度。"""
    from services.quote_scope_lock import contains_street_level

    kw = str(keyword or "")
    if not kw:
        return DEPTH_NONE

    explicit_streets = [str(s).strip() for s in (streets or []) if str(s or "").strip()]
    if contains_street_level(kw, street_names=explicit_streets):
        return DEPTH_STREET

    for name in (sub_regions or []):
        name = str(name or "").strip()
        if len(name) >= 2 and name in kw:
            return DEPTH_DISTRICT

    if re.search(r"[一-龥]{2,4}(?:区|县)", kw):
        return DEPTH_DISTRICT

    for name in (cities or []):
        name = str(name or "").strip()
        if len(name) >= 2 and name in kw:
            return DEPTH_CITY

    from tools.keyword_value_scorer import _CITY_PREFIXES
    for city in _CITY_PREFIXES:
        if city in kw:
            return DEPTH_CITY

    return DEPTH_NONE


def keyword_geo_depth(
    row: dict,
    cities: Optional[Iterable[str]] = None,
    sub_regions: Optional[Iterable[str]] = None,
    streets: Optional[Iterable[str]] = None,
) -> int:
    """一条已评分关键词的地域颗粒度(评估师 city_tier 与文本判定取更深者)。"""
    if not isinstance(row, dict):
        return DEPTH_NONE
    tier = row.get("market_scope") or row.get("geo_level")
    v2 = row.get("v2_assessor_data")
    if isinstance(v2, dict) and v2.get("city_tier"):
        tier = v2.get("city_tier")
    tier_depth = _CITY_TIER_DEPTH.get(str(tier or "").strip(), DEPTH_NONE)
    text_depth = _text_depth(row.get("keyword", ""), cities, sub_regions, streets)
    return max(tier_depth, text_depth)


def _price_of(row: dict, price_field: str) -> float:
    try:
        return float(row.get(price_field) or 0)
    except (TypeError, ValueError):
        return 0.0


def detect_price_inversions(
    rows: list[dict],
    *,
    cities: Optional[Iterable[str]] = None,
    sub_regions: Optional[Iterable[str]] = None,
    streets: Optional[Iterable[str]] = None,
    price_field: str = "selling_price",
) -> list[dict]:
    """找出"下级地域词比上级地域词还贵"的词。

    只在**地域词之间**比较(depth >= 1):全国词天然竞争更大更贵,不算倒挂。
    判定:某条 depth=d 的词,单价严格大于所有 depth<d(且 depth>=1)的词的最高价。
    """
    if not rows:
        return []

    by_depth: dict[int, list[tuple[str, float]]] = {}
    depths: dict[int, int] = {}
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        depth = keyword_geo_depth(row, cities, sub_regions, streets)
        depths[idx] = depth
        if depth >= DEPTH_CITY:
            by_depth.setdefault(depth, []).append(
                (str(row.get("keyword") or ""), _price_of(row, price_field))
            )

    inversions: list[dict] = []
    for idx, row in enumerate(rows):
        depth = depths.get(idx, DEPTH_NONE)
        if depth <= DEPTH_CITY:
            continue
        price = _price_of(row, price_field)
        if price <= 0:
            continue
        shallower = [
            (kw, p)
            for d, items in by_depth.items() if DEPTH_CITY <= d < depth
            for kw, p in items if p > 0
        ]
        if not shallower:
            continue
        top_kw, top_price = max(shallower, key=lambda item: item[1])
        if price > top_price:
            inversions.append({
                "keyword": str(row.get("keyword") or ""),
                "depth": depth,
                "depth_label": DEPTH_LABELS.get(depth, str(depth)),
                "price": price,
                "compared_keyword": top_kw,
                "compared_price": top_price,
                "reason": (
                    f"{DEPTH_LABELS.get(depth, '下级')}词单价 ¥{price:.0f} 高于"
                    f"上级地域词「{top_kw}」的 ¥{top_price:.0f} —— 越细的地域搜索量越小，"
                    f"价格倒挂通常意味着竞争度/搜索量是估算值而非实测值，请人工复核"
                ),
            })
    return inversions


def annotate_price_inversions(
    rows: list[dict],
    *,
    cities: Optional[Iterable[str]] = None,
    sub_regions: Optional[Iterable[str]] = None,
    streets: Optional[Iterable[str]] = None,
    price_field: str = "selling_price",
) -> list[dict]:
    """就地把倒挂词标成 `needs_review=True`,返回倒挂明细。

    **不改价格**,只加复核标记 —— 定价公式是红线,该由人来拍。
    """
    inversions = detect_price_inversions(
        rows, cities=cities, sub_regions=sub_regions, streets=streets,
        price_field=price_field,
    )
    if not inversions:
        return []
    flagged = {item["keyword"] for item in inversions}
    reason_map = {item["keyword"]: item["reason"] for item in inversions}
    for row in rows:
        if not isinstance(row, dict):
            continue
        kw = str(row.get("keyword") or "")
        if kw not in flagged:
            continue
        row["needs_review"] = True
        row["price_inversion"] = reason_map[kw]
        flags = row.get("risk_flags")
        if not isinstance(flags, list):
            flags = []
        if INVERSION_RISK_FLAG not in flags:
            flags.append(INVERSION_RISK_FLAG)
        row["risk_flags"] = flags
        v2 = row.get("v2_assessor_data")
        if isinstance(v2, dict):
            v2_flags = v2.get("risk_flags")
            if not isinstance(v2_flags, list):
                v2_flags = []
            if INVERSION_RISK_FLAG not in v2_flags:
                v2_flags.append(INVERSION_RISK_FLAG)
            v2["risk_flags"] = v2_flags
    return inversions


def pricing_input_quality(row: dict) -> str:
    """这条词的定价输入到底有多少是**实测**的。

    - `measured`   : 5118 有真实需求数据(搜索量/出价/投放家数任一 > 0)
    - `estimated`  : 5118 无数据 → 搜索量是 LLM 估算(data_source=llm_estimate)
    - `unavailable`: 连竞争度都拿不到(metaso 兜底)· 价格完全建立在启发式上
    """
    if not isinstance(row, dict):
        return "estimated"
    data_source = str(row.get("data_source") or "").strip()
    measured_5118 = any(
        float(row.get(field) or 0) > 0
        for field in ("sem_price", "bidword_company_count")
    ) or (data_source == "5118" and float(row.get("search_volume") or 0) > 0)
    if measured_5118:
        return "measured"
    v2 = row.get("v2_assessor_data") if isinstance(row.get("v2_assessor_data"), dict) else {}
    if str(v2.get("metaso_source") or "") == "fallback":
        return "unavailable"
    return "estimated"
