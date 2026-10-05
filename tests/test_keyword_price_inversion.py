"""
定价输入治理 · 地域价格倒挂自检
工单: docs/AI-CONTEXT/QUOTE_KEYWORD_CORRECTION_WORKORDER_2026-07-26.md · P1-6

生产实证(驰鲸 session 200):
  街道废词 "龙岗横岗TikTok外贸获客代运营公司"(月搜 60)  → ¥5760 / 18 篇
  城市真词 "深圳龙岗TikTok外贸获客代运营公司推荐"(月搜 120) → ¥3250 / 10 篇
  越细的地域搜索量越小却贵 77% = 定价输入被编造的竞争度带偏。

本套件锁两条:
  1. 倒挂能被检出并标 needs_review(**不改价**,定价公式是红线)
  2. 无真实需求数据的词,竞争度兜底不许再被"商业词模式"抬价
"""
from __future__ import annotations

import pytest

from tools.keyword_price_inversion import (
    DEPTH_CITY,
    DEPTH_DISTRICT,
    DEPTH_NONE,
    DEPTH_STREET,
    INVERSION_RISK_FLAG,
    annotate_price_inversions,
    detect_price_inversions,
    keyword_geo_depth,
    pricing_input_quality,
)
from tools.keyword_value_scorer import estimate_competition_from_keyword


# ============================================================================
# 地域颗粒度判定
# ============================================================================

@pytest.mark.parametrize("row,expected", [
    ({"keyword": "TikTok代运营服务商推荐", "market_scope": "national"}, DEPTH_NONE),
    ({"keyword": "深圳TikTok代运营哪家好", "market_scope": "tier1"}, DEPTH_CITY),
    ({"keyword": "深圳龙岗区TikTok代运营推荐", "market_scope": "tier1"}, DEPTH_DISTRICT),
    ({"keyword": "深圳TikTok代运营", "market_scope": "county"}, DEPTH_DISTRICT),
    ({"keyword": "龙岗龙岗街道TikTok代运营", "market_scope": "tier1"}, DEPTH_STREET),
    ({"keyword": "深圳TikTok代运营", "market_scope": "township"}, DEPTH_STREET),
])
def test_geo_depth_detection(row, expected):
    assert keyword_geo_depth(row) == expected


def test_depth_takes_the_deeper_of_tier_and_text():
    row = {"keyword": "深圳龙岗街道TikTok代运营", "market_scope": "tier1"}
    assert keyword_geo_depth(row) == DEPTH_STREET


# ============================================================================
# 倒挂检出
# ============================================================================

PRODUCTION_ROWS = [
    {"keyword": "深圳龙岗TikTok外贸获客代运营公司推荐", "market_scope": "tier1",
     "selling_price": 3250, "search_volume": 120},
    {"keyword": "龙岗横岗TikTok外贸获客代运营公司", "market_scope": "township",
     "selling_price": 5760, "search_volume": 60},
]


def test_production_inversion_is_detected():
    inversions = detect_price_inversions(PRODUCTION_ROWS)
    assert len(inversions) == 1
    item = inversions[0]
    assert item["keyword"] == "龙岗横岗TikTok外贸获客代运营公司"
    assert item["price"] == 5760
    assert item["compared_price"] == 3250
    assert item["reason"]


def test_annotate_marks_needs_review_without_touching_price():
    rows = [dict(r) for r in PRODUCTION_ROWS]
    annotate_price_inversions(rows)
    healthy, inverted = rows
    assert inverted["needs_review"] is True
    assert INVERSION_RISK_FLAG in inverted["risk_flags"]
    assert inverted["price_inversion"]
    # 价格一分不动 —— 定价公式是红线,只能标记不能改
    assert inverted["selling_price"] == 5760
    assert healthy["selling_price"] == 3250
    assert healthy.get("needs_review") is not True


def test_healthy_pricing_ladder_is_not_flagged():
    rows = [
        {"keyword": "深圳TikTok代运营哪家好", "market_scope": "tier1", "selling_price": 5200},
        {"keyword": "深圳龙岗区TikTok代运营推荐", "market_scope": "county", "selling_price": 2400},
        {"keyword": "TikTok代运营服务商推荐", "market_scope": "national", "selling_price": 9800},
    ]
    assert detect_price_inversions(rows) == []


def test_national_keywords_are_not_treated_as_inversion():
    """全国词天然竞争更大更贵,不算倒挂(只在地域词之间比)。"""
    rows = [
        {"keyword": "TikTok代运营服务商推荐", "market_scope": "national", "selling_price": 12000},
        {"keyword": "深圳TikTok代运营哪家好", "market_scope": "tier1", "selling_price": 3000},
    ]
    assert detect_price_inversions(rows) == []


def test_missing_prices_are_skipped_not_crashed():
    rows = [
        {"keyword": "深圳TikTok代运营哪家好", "market_scope": "tier1", "selling_price": 0},
        {"keyword": "深圳龙岗区TikTok代运营", "market_scope": "county", "selling_price": None},
        {"keyword": "x"},
    ]
    assert detect_price_inversions(rows) == []


# ============================================================================
# 竞争度兜底降权(不得凭编造竞争度定高价)
# ============================================================================

def test_fallback_competition_default_behaviour_is_unchanged():
    """不传 demand_signal = 逐值零变化(§9.6 定价数值不许被顺手改)。"""
    assert estimate_competition_from_keyword("深圳装修公司推荐哪家好") == 15  # 2+ 商业模式
    assert estimate_competition_from_keyword("深圳装修哪家好") == 10           # 1 个商业模式
    assert estimate_competition_from_keyword("装修流程怎么做") == 3            # 信息型
    assert estimate_competition_from_keyword("装修") == 5                      # 其他


def test_zero_demand_keywords_cannot_be_escalated_by_commercial_patterns():
    """没人搜的词不许因为写法像商业词就被判 10-15 个竞品(那正是"越废越贵")。"""
    junk = "龙岗横岗TikTok外贸获客代运营公司推荐哪家好"
    assert estimate_competition_from_keyword(junk) == 15
    assert estimate_competition_from_keyword(junk, demand_signal=0) == 5


def test_real_demand_keeps_full_escalation():
    kw = "深圳装修公司推荐哪家好"
    assert estimate_competition_from_keyword(kw, demand_signal=120) == 15


def test_zero_demand_only_lowers_never_raises():
    info_kw = "装修流程怎么做"
    assert estimate_competition_from_keyword(info_kw, demand_signal=0) <= (
        estimate_competition_from_keyword(info_kw)
    )


# ============================================================================
# 定价输入可信度
# ============================================================================

@pytest.mark.parametrize("row,expected", [
    ({"data_source": "5118", "search_volume": 320, "sem_price": 0}, "measured"),
    ({"data_source": "5118", "sem_price": 4.2}, "measured"),
    ({"data_source": "llm_estimate", "search_volume": 50}, "estimated"),
    ({"data_source": "llm_estimate", "search_volume": 50,
      "v2_assessor_data": {"metaso_source": "fallback"}}, "unavailable"),
])
def test_pricing_input_quality(row, expected):
    assert pricing_input_quality(row) == expected
