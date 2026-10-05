"""
报价选词 · 范围锁定裁决(ScopeLock)回归
工单: docs/AI-CONTEXT/QUOTE_KEYWORD_CORRECTION_WORKORDER_2026-07-26.md · P0-1 / P0-2 / P0-3

锁三件事:
  1. 省市区地址真的能拆开("广东省深圳市龙岗区" → 深圳 / 龙岗 / 广东)—— R1 根因
  2. market_level 真的驱动地域策略(national/regional 禁街道)—— R2 根因
  3. LLM 挂了/返回垃圾一律回落启发式,**绝不阻断**(元指令 13)
"""
from __future__ import annotations

import asyncio

import pytest

from services.quote_scope_lock import (
    ScopeLock,
    build_diagnosis_summary,
    city_scope_from_market_level,
    contains_street_level,
    geo_policy_for,
    heuristic_scope_lock,
    is_full_address_form,
    normalize_market_level,
    parse_admin_region,
    parse_region_hierarchy,
    resolve_scope_lock,
    scope_lock_from_dict,
)


# ============================================================================
# P0-3 · 省市区地址解析(R1 本城进黑名单的直接根因)
# ============================================================================

@pytest.mark.parametrize("raw,city,district,province", [
    ("广东省深圳市龙岗区", "深圳", "龙岗区", "广东"),
    ("深圳市龙岗区", "深圳", "龙岗区", None),
    ("北京市朝阳区", "北京", "朝阳区", None),
    ("新疆维吾尔自治区乌鲁木齐市", "乌鲁木齐", None, "新疆"),
])
def test_full_address_is_split_into_levels(raw, city, district, province):
    parsed = parse_admin_region(raw)
    hierarchy = parse_region_hierarchy(raw)
    assert city in hierarchy["cities"], f"{raw} 应能提出城市 {city}"
    if district:
        assert parsed["district"] == district
    if province:
        assert province in hierarchy["provinces"]


def test_shenzhen_address_no_longer_swallows_the_city_name():
    """R1:老口径对"广东省深圳市龙岗区"整串原样返回 → 提不出"深圳"。"""
    hierarchy = parse_region_hierarchy("广东省深圳市龙岗区")
    assert hierarchy["cities"] == ["深圳"]
    assert "龙岗" in hierarchy["sub_regions"]
    assert hierarchy["provinces"] == ["广东"]
    assert "广东省深圳市龙岗区" not in hierarchy["cities"]


@pytest.mark.parametrize("raw,expected", [
    ("深圳", ["深圳"]),          # 无后缀 → 原样(向后兼容)
    ("朝阳区", ["朝阳区"]),      # 只有区、推不出市 → 原样(向后兼容)
    ("珠三角", ["珠三角"]),      # 俗称 → 原样
    ("全国", []),                # 非市场 token
    ("深圳,广州", ["深圳", "广州"]),
])
def test_plain_region_tokens_keep_legacy_behaviour(raw, expected):
    assert parse_region_hierarchy(raw)["cities"] == expected


def test_street_level_address_still_yields_the_city():
    hierarchy = parse_region_hierarchy("深圳市龙岗区平湖街道")
    assert hierarchy["cities"] == ["深圳"]
    assert hierarchy["streets"]


@pytest.mark.parametrize("keyword,expected", [
    ("广东省深圳市龙岗区TikTok工厂出海获客服务推荐", True),
    ("深圳市龙岗区TikTok代运营推荐", True),
    ("深圳龙岗TikTok代运营推荐", False),      # 真人问法 · 不能误杀
    ("深圳TikTok代运营哪家好", False),
    # 误伤反例:含"省/市"字但不是行政地址
    ("北京装修省钱攻略哪家好", False),
    ("上市公司品牌推广服务商推荐", False),
    ("深圳菜市场摊位转让哪家好", False),
])
def test_full_address_keyword_form_detection(keyword, expected):
    assert is_full_address_form(keyword) is expected


def test_city_name_is_not_mistaken_for_street():
    """"景德镇"是地级市,不能因为带"镇"被当街道级词误杀。"""
    assert contains_street_level("景德镇陶瓷厂家推荐", allowed_names=["景德镇"]) is False
    assert contains_street_level("龙岗龙岗街道TikTok线上展厅搭建服务商") is True


# ============================================================================
# P0-2 · market_level 驱动地域策略
# ============================================================================

@pytest.mark.parametrize("level,allow_street,max_depth", [
    ("district", True, "street"),
    ("city", False, "district"),
    ("regional", False, "city"),
    ("national", False, "city"),
])
def test_geo_policy_by_market_level(level, allow_street, max_depth):
    policy = geo_policy_for(level)
    assert policy["allow_street"] is allow_street
    assert policy["max_geo_depth"] == max_depth


def test_only_district_market_level_may_drill_to_street():
    assert geo_policy_for("district")["allow_street"] is True
    for level in ("city", "regional", "national"):
        assert geo_policy_for(level)["allow_street"] is False


def test_national_market_level_has_low_geo_ratio():
    assert geo_policy_for("national")["geo_ratio_hint"] <= 20
    assert geo_policy_for("national")["drill_region"] is False


def test_unknown_market_level_falls_back_to_city_not_district():
    """未知层级绝不能回落成 district —— 那是唯一开街道下钻的口子。"""
    policy = geo_policy_for("看不懂的值")
    assert policy["allow_street"] is False
    assert normalize_market_level(None) == "city"


@pytest.mark.parametrize("level,scope", [
    ("district", "local"), ("city", "local"),
    ("regional", "local"), ("national", "national"),
])
def test_city_scope_mapping(level, scope):
    assert city_scope_from_market_level(level) == scope


# ============================================================================
# P0-1 · 启发式裁决(驰鲸金标准)
# ============================================================================

CHIJING_BRAND = {
    "name": "深圳市驰鲸科技有限公司",
    "industry": "外贸社媒营销",
    "cities": "广东省深圳市龙岗区",
    "business_scope": "TikTok海外B2B精准获客/外贸社媒全案营销",
    "core_keywords": ["TikTok代运营", "外贸社媒营销"],
}


def test_chijing_heuristic_is_b2b_and_never_district():
    lock = heuristic_scope_lock(CHIJING_BRAND, {"target_customers": "外贸工厂老板"})
    assert lock.service_market == ("深圳",), "服务市场应归一到深圳,不是整串注册地址"
    assert lock.business_type == "B2B"
    assert lock.market_level != "district", "B2B 外贸代运营绝不能判成街边店"
    assert geo_policy_for(lock.market_level)["allow_street"] is False


def test_local_store_brand_still_gets_district_level():
    lock = heuristic_scope_lock({
        "name": "老王理发店", "industry": "美发", "cities": "深圳市龙岗区",
        "business_scope": "到店剪发、烫染",
    })
    assert lock.market_level == "district"
    assert geo_policy_for(lock.market_level)["allow_street"] is True


def test_heuristic_seeds_are_natural_queries_not_jargon():
    lock = heuristic_scope_lock(CHIJING_BRAND, {})
    assert lock.real_query_seeds, "必须给出问法种子(风格锚)"
    for seed in lock.real_query_seeds:
        assert not is_full_address_form(seed)
        assert len(seed) <= 24


def test_scope_lock_roundtrips_through_dict():
    lock = heuristic_scope_lock(CHIJING_BRAND, {})
    restored = scope_lock_from_dict(lock.as_dict())
    assert isinstance(restored, ScopeLock)
    assert restored.service_market == lock.service_market
    assert restored.market_level == lock.market_level
    assert restored.business_type == lock.business_type


# ============================================================================
# 诊断摘要接入(验收 6)
# ============================================================================

def test_diagnosis_summary_extracts_eight_questions_and_context():
    record = {
        "id": 42,
        "brand_name": "驰鲸",
        "industry": "外贸社媒营销",
        "raw_data_json": (
            '{"input_params": {"client_location": "深圳"},'
            ' "data": {"business_context": {'
            '  "core_business": "TikTok海外B2B获客",'
            '  "target_customers": "外贸工厂老板",'
            '  "real_user_questions": ["深圳TikTok代运营哪家好", "TikTok代运营多少钱"],'
            '  "competitors": ["某某出海"]}}}'
        ),
    }
    summary = build_diagnosis_summary(record)
    assert summary["diagnosis_id"] == 42
    assert summary["core_business"] == "TikTok海外B2B获客"
    assert summary["real_user_questions"][0] == "深圳TikTok代运营哪家好"
    assert summary["competitors"] == ["某某出海"]
    assert summary["client_location"] == "深圳"


def test_diagnosis_summary_tolerates_garbage_and_missing_records():
    assert build_diagnosis_summary(None) == {}
    assert build_diagnosis_summary({}) == {}
    assert build_diagnosis_summary({"id": 1, "raw_data_json": "not-json"}) == {"diagnosis_id": 1}


# ============================================================================
# 元指令 13:永不中断
# ============================================================================

def test_resolve_scope_lock_falls_back_when_llm_explodes():
    async def boom(_prompt):
        raise RuntimeError("LLM down")

    lock = asyncio.run(resolve_scope_lock(CHIJING_BRAND, {}, llm_call=boom))
    assert lock.source == "heuristic"
    assert lock.service_market == ("深圳",)


def test_resolve_scope_lock_falls_back_on_garbage_json():
    async def garbage(_prompt):
        return "抱歉，我无法回答这个问题。"

    lock = asyncio.run(resolve_scope_lock(CHIJING_BRAND, {}, llm_call=garbage))
    assert lock.source == "heuristic"


def test_resolve_scope_lock_uses_llm_decision_when_valid():
    async def good(_prompt):
        return (
            '{"service_market": ["深圳市"], "market_level": "city",'
            ' "business_type": "B2B", "buyer_persona": "外贸工厂老板",'
            ' "real_query_seeds": ["深圳TikTok代运营哪家好",'
            ' "广东省深圳市龙岗区TikTok代运营", "深圳工厂TikTok获客代运营服务商"],'
            ' "reason": "跨境代运营"}'
        )

    lock = asyncio.run(resolve_scope_lock(CHIJING_BRAND, {"diagnosis_id": 7}, llm_call=good))
    assert lock.source == "llm"
    assert lock.market_level == "city"
    assert lock.service_market == ("深圳",), "LLM 返的'深圳市'要归一成口语地名"
    assert lock.used_diagnosis is True
    assert lock.diagnosis_id == 7
    # 整串地址种子必须被剔掉(它正是要治的病)
    assert all(not is_full_address_form(s) for s in lock.real_query_seeds)
    assert "深圳TikTok代运营哪家好" in lock.real_query_seeds


def test_llm_cannot_mark_b2b_brand_as_street_level_shop():
    async def wrong(_prompt):
        return (
            '{"service_market": ["深圳"], "market_level": "district",'
            ' "business_type": "B2B", "buyer_persona": "x", "real_query_seeds": []}'
        )

    lock = asyncio.run(resolve_scope_lock(CHIJING_BRAND, {}, llm_call=wrong))
    assert lock.market_level == "city", "B2B 判 district 必须被纠正,否则街道下钻会被误开"
    assert geo_policy_for(lock.market_level)["allow_street"] is False
