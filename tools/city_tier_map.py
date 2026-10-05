"""
city_tier_map(v2.0 thin wrapper · 2026-06-11)— 城市 tier 老 API 兼容层

【v2.0 老板拍 一次性重构】
  - 删 TIER1 / NEW_TIER1 / TIER2 / TIER3 / TIER4 / COUNTY_LIST 城市字典(永远填不满 · 全国 2800+ 城/县/区)
  - 删配套 substring 扫描函数(_normalize_city / 老 get_city_tier 字典命中分支)
  - 主算价路径已切 pricing_llm_assessor.assess_keyword_pricing(LLM 直接输出 city_tier · 不再用字典)
  - 老 API 接口完全兼容(get_city_tier / get_city_multiplier / get_city_multiplier_from_keyword /
    is_low_confidence_city / describe_city_tier)

调用方(继续工作):
  - tools/keyword_value_scorer 历史 _compute_geo_scope_multiplier · v2 已删
  - tools/keyword_classifier 历史 _detect_geo · v2 已切全 LLM 不再 import 本模块
  - 前端 frontend/src/config/cityTierMap.ts(独立 SSOT · 不依赖本 Python · v2 暂不动)

【保留业务定义类(老板拍)】
  - TIER_MULTIPLIER 系数表(tier1=1.0 / new_tier1=0.8 / tier2=0.6 / tier3=0.45 / tier4=0.3 / county=0.2 / township=0.1)
  - describe_city_tier 中文翻译(日志 / UI 兼容)
  - is_low_confidence_city 判定(county / township 走 sanity banner)

启发式后缀判定(替字典命中)· 不准但保守(常见后缀级别识别 · 一线大城市需 LLM 评估师辅助):
  - 空 / 全国 / 不限 → tier1
  - "X 县" → county
  - "X 乡" / "X 镇" → township(除"镇江"等假阳性)
  - 其他 → tier3 兜底(保守偏低 · 跟原 fallback 一致)

红线:不碰 billing.py / 红线 4 文件 baseline · 失败 fail-soft 返保守值。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("GEO-CityTierMap")

# ===== 业务定义类(老板拍 · v2 保留)=====
# tier 系数(老板 2026-04-24 批)· 一线 1.0 / 新一线 0.8 / 二线 0.6 / 三线 0.45 / 四线 0.3 / 县级 0.2 / 乡镇 0.1
TIER_MULTIPLIER: dict[str, float] = {
    "tier1": 1.0,
    "new_tier1": 0.8,
    "tier2": 0.6,
    "tier3": 0.45,
    "tier4": 0.3,
    "county": 0.2,
    "township": 0.1,
}

# ===== 兼容空字典(老 import 不报错 · 内部不再 substring 扫)=====
TIER1: frozenset[str] = frozenset()
NEW_TIER1: frozenset[str] = frozenset()
TIER2: frozenset[str] = frozenset()
TIER3: frozenset[str] = frozenset()
TIER4: frozenset[str] = frozenset()
COUNTY_LIST: frozenset[str] = frozenset()

# 乡镇假阳性排除(镇江结尾"江" · 不应判为乡镇)
_TOWNSHIP_EXCLUDE: frozenset[str] = frozenset({"镇江"})


def get_city_tier(city: Optional[str]) -> str:
    """[v2.0 启发式] 城市 tier 分级 · 字典删后改后缀判定(常见情况保守)。

    规则:
    1. 空 / 全国 / 不限 → tier1(中性)
    2. 乡镇后缀(乡/镇)· 排除"镇江"等假阳性 → township
    3. "X 县"后缀 → county
    4. 其他 → tier3(0.45 保守偏低 · 跟原 fallback 一致)

    准确 tier 判定走 pricing_llm_assessor.assess_keyword_pricing(LLM 直接输出 city_tier)
    """
    if not city or city in ("全国", "不限") or not city.strip():
        return "tier1"
    trimmed = city.strip()
    if trimmed not in _TOWNSHIP_EXCLUDE:
        if trimmed.endswith("乡") or trimmed.endswith("镇"):
            return "township"
    if trimmed.endswith("县"):
        return "county"
    return "tier3"


def get_city_multiplier(city: Optional[str]) -> float:
    """城市 geo 乘数(乘以原 geo_multiplier)"""
    return TIER_MULTIPLIER[get_city_tier(city)]


def get_city_multiplier_from_keyword(keyword: str) -> float:
    """[v2.0] 从关键词字面提取城市 → tier multiplier · 字典删后返 1.0 中性。

    pricing v2 LLM 评估师已内化 city_tier 完整判定(不再连乘 geo_multiplier)· 本函数仅老 API 兼容。
    """
    return 1.0


def is_low_confidence_city(city: Optional[str]) -> bool:
    """是否低置信度地域(县级 / 乡镇)· 用于 sanity banner 提示"""
    tier = get_city_tier(city)
    return tier in ("county", "township")


def describe_city_tier(tier: str) -> str:
    """中文描述(日志 / UI)"""
    return {
        "tier1": "一线城市",
        "new_tier1": "新一线城市",
        "tier2": "二线城市",
        "tier3": "三线城市",
        "tier4": "四线城市",
        "county": "县级",
        "township": "乡镇",
    }.get(tier, "未知")
