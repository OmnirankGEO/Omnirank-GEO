"""
报价选词 · 地域护栏反向 bug 回归(金标准 = 驰鲸 brand 712 / session 200)
工单: docs/AI-CONTEXT/QUOTE_KEYWORD_CORRECTION_WORKORDER_2026-07-26.md

生产实证的三个反向现象,本文件逐条钉死:
  1. 含"深圳"(客户自己的城市)的真词被打 scope_match=false「范围需复核」
  2. 街道级废词("龙岗龙岗街道TikTok线上展厅搭建服务商")反而 scope_match=true 默认入交付
  3. "广东省深圳市龙岗区XX"整串地址词照样进候选
"""
from __future__ import annotations

import asyncio

import pytest

from services.quote_scope_lock import heuristic_scope_lock
from tools.keyword_expander import KeywordExpander


CHIJING_BRAND = {
    "name": "深圳市驰鲸科技有限公司",
    "industry": "外贸社媒营销",
    "cities": "广东省深圳市龙岗区",       # 注册地址,不是服务市场
    "business_scope": "TikTok海外B2B精准获客/外贸社媒全案营销",
    "core_keywords": ["TikTok代运营", "外贸社媒营销"],
}

# 生产 session 200 实际产出的词(街道废词 + 被误杀的真词)
STREET_JUNK = [
    "龙岗龙岗街道TikTok线上展厅搭建服务商",
]
ADDRESS_JUNK = [
    "广东省深圳市龙岗区TikTok工厂出海获客服务推荐",
]
REAL_QUERIES = [
    "深圳TikTok代运营哪家好",
    "深圳工厂TikTok获客代运营服务商",
    "深圳外贸社媒营销培训哪家好",
    "深圳龙岗TikTok工厂出海全案代运营推荐",
    "深圳外贸TikTok代运营公司推荐",
]

# 生产实测的下钻结果:区级输入被拆成街道
PRODUCTION_DISTRICT_DRILL = {
    "parent_region": "龙岗区",
    "region_level": "district",
    "sub_regions": [
        {"name": name, "weight": 20}
        for name in ("龙岗街道", "坂田", "布吉", "横岗", "龙城")
    ],
    "region_aliases": [],
}
CITY_DRILL = {
    "parent_region": "深圳",
    "region_level": "city",
    "sub_regions": [
        {"name": name, "weight": 20}
        for name in ("南山区", "福田区", "龙岗区", "坂田", "华强北")
    ],
    "region_aliases": [],
}


def _chijing_lock():
    return heuristic_scope_lock(CHIJING_BRAND, {"target_customers": "外贸工厂老板"})


async def _run_expand(candidates, scope_lock, city=CHIJING_BRAND["cities"], drill=CITY_DRILL):
    expander = KeywordExpander()

    async def fake_llm_expand(**_kwargs):
        return [(kw, "认知") for kw in candidates]

    async def fake_5118(**_kwargs):
        return []

    async def fake_filter(**_kwargs):
        return []

    async def fake_drill(region, market_level="city"):
        return expander._enforce_drill_depth(dict(drill), market_level)

    expander._llm_expand = fake_llm_expand
    expander._5118_expand = fake_5118
    expander._llm_filter = fake_filter
    expander._drill_region = fake_drill

    return await expander.expand_keywords(
        core_keywords=["TikTok代运营"],
        industry=CHIJING_BRAND["industry"],
        city=city,
        business_scope=CHIJING_BRAND["business_scope"],
        target_count=20,
        brand_name="驰鲸",
        scope_lock=scope_lock.as_dict() if scope_lock else None,
    )


# ============================================================================
# 验收 2 上半:含"深圳"的词不再被打「范围需复核」
# ============================================================================

def test_customer_own_city_keywords_are_not_flagged_out_of_scope():
    result = asyncio.run(_run_expand(REAL_QUERIES, _chijing_lock()))
    accepted = {item["keyword"] for item in result["keywords"]}
    rejected = {item["keyword"] for item in result["rejected_keywords"]}

    for keyword in REAL_QUERIES:
        assert keyword in accepted, f"客户自己城市的真词被误杀: {keyword}"
        assert keyword not in rejected
    assert all(item["scope_match"] is True for item in result["keywords"])


def test_customer_city_survives_even_without_scope_lock():
    """没有裁决结果时(老调用方 / 裁决失败)也必须修好 R1。"""
    result = asyncio.run(_run_expand(REAL_QUERIES, None))
    accepted = {item["keyword"] for item in result["keywords"]}
    assert "深圳TikTok代运营哪家好" in accepted


# ============================================================================
# 验收 1 + 2 下半:街道词消失 / 整串地址词消失
# ============================================================================

def test_street_level_junk_is_rejected_for_non_district_market():
    result = asyncio.run(_run_expand(STREET_JUNK + REAL_QUERIES, _chijing_lock()))
    accepted = {item["keyword"] for item in result["keywords"]}
    for keyword in STREET_JUNK:
        assert keyword not in accepted, f"街道级废词仍然入交付: {keyword}"


def test_full_address_keyword_is_rejected():
    result = asyncio.run(_run_expand(ADDRESS_JUNK + REAL_QUERIES, _chijing_lock()))
    accepted = {item["keyword"] for item in result["keywords"]}
    for keyword in ADDRESS_JUNK:
        assert keyword not in accepted, f"整串行政地址词仍然入交付: {keyword}"


def test_rejected_junk_is_itemized_with_reason_and_override_flag():
    """绝不静默删除:每条被拒都要有原因,且默认允许人工放行(非硬边界)。"""
    result = asyncio.run(_run_expand(STREET_JUNK + ADDRESS_JUNK + REAL_QUERIES, _chijing_lock()))
    rejected = {item["keyword"]: item for item in result["rejected_keywords"]}
    for keyword in STREET_JUNK + ADDRESS_JUNK:
        assert keyword in rejected
        assert rejected[keyword]["rejection_reason"]
        assert rejected[keyword]["default_selected"] is False
        assert rejected[keyword]["hard_block"] is False
        assert rejected[keyword]["human_override_allowed"] is True


# ============================================================================
# R2:地域下钻深度守卫
# ============================================================================

@pytest.mark.parametrize("market_level", ["national", "regional", "city"])
def test_drill_never_returns_street_for_non_district_market(market_level):
    expander = KeywordExpander()
    guarded = expander._enforce_drill_depth(dict(PRODUCTION_DISTRICT_DRILL), market_level)
    names = [s["name"] for s in guarded["sub_regions"]]
    assert names == [], f"{market_level} 不该从区级再下钻出任何子区域,实得 {names}"


def test_city_market_keeps_districts_but_drops_street_names():
    expander = KeywordExpander()
    guarded = expander._enforce_drill_depth(dict(CITY_DRILL), "city")
    names = [s["name"] for s in guarded["sub_regions"]]
    assert names == ["南山区", "福田区", "龙岗区"]
    assert "坂田" not in names and "华强北" not in names


def test_district_market_still_gets_streets():
    """真街边店不能被误伤:district 仍然保留街道下钻能力。"""
    expander = KeywordExpander()
    guarded = expander._enforce_drill_depth(dict(PRODUCTION_DISTRICT_DRILL), "district")
    names = [s["name"] for s in guarded["sub_regions"]]
    assert "龙岗街道" in names and "坂田" in names


def test_province_drill_to_cities_is_untouched():
    expander = KeywordExpander()
    province_drill = {
        "parent_region": "广东", "region_level": "province",
        "sub_regions": [{"name": n, "weight": 25} for n in ("广州", "深圳", "东莞", "佛山")],
        "region_aliases": ["珠三角"],
    }
    for level in ("national", "regional", "city", "district"):
        guarded = expander._enforce_drill_depth(dict(province_drill), level)
        assert [s["name"] for s in guarded["sub_regions"]] == ["广州", "深圳", "东莞", "佛山"]


# ============================================================================
# 范围裁决驱动 prompt / 补充词
# ============================================================================

def test_scope_lock_drives_prompt_geo_policy_and_seed_anchor():
    lock = _chijing_lock()
    block = KeywordExpander._build_scope_context_block(lock)
    assert "服务市场" in block and "深圳" in block
    assert lock.market_level in block
    assert "真实问法种子" in block
    assert KeywordExpander._build_scope_context_block(None) == ""


def test_rule_supplements_use_the_city_not_the_registered_address():
    """兜底补充词也不许再拼出"广东省深圳市龙岗区XX推荐"。"""
    expander = KeywordExpander()
    supplements = expander._build_rule_supplements(
        core_keywords=["TikTok代运营"],
        city="广东省深圳市龙岗区",
        limit=10,
    )
    keywords = [item["keyword"] for item in supplements]
    assert any(kw.startswith("深圳") for kw in keywords), keywords
    assert not any("广东省" in kw for kw in keywords), keywords


def test_summary_reports_market_level_and_service_market():
    result = asyncio.run(_run_expand(REAL_QUERIES, _chijing_lock()))
    assert result["summary"]["market_level"] == _chijing_lock().market_level
    assert result["summary"]["service_market"] == ["深圳"]
    assert result["scope_lock"]["service_market"] == ["深圳"]
