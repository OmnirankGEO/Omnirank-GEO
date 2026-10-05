"""报价关键词「地域与商业意图双向反转」修复 · 逐字验收矩阵。

工单 WO_QUOTE_KEYWORD_GEO_COMMERCIAL_DOUBLE_INVERSION_2026-08-09 §5 / §6。

🔴 本文件**不做源码字符串断言**(工单 §8 最后一条明令)。每一条都是把关键词真的
   喂进 `KeywordExpander.expand_keywords()`(注入假 LLM/5118 返回,走真实
   Step 5 / Step 5a / 三轴决策 / 输出分桶),再断言最终结构化结果。

🔴 fixture 的业务范围按 §5 首段要求写死:商场运营、品牌招商、商铺租赁、
   餐饮入驻、家具建材/办公家具经营;服务市场 = 深圳市龙岗区。
"""
from __future__ import annotations

import asyncio

import pytest

from services.commercial_query_policy import (
    INTENT_COMMERCIAL,
    INTENT_KNOWLEDGE,
    INTENT_UNCERTAIN,
    evaluate,
)
from services.keyword_delivery_decision import (
    GEO_MATCHED,
    GEO_OUTSIDE_MARKET,
    GEO_TOO_BROAD,
    GROUP_GEO_OUTSIDE,
    GROUP_GEO_TOO_BROAD,
    GROUP_KNOWLEDGE,
    GROUP_NEEDS_CONFIRM,
    GROUP_SCOPE_MISMATCH,
    REASON_GEO_OUTSIDE,
    REASON_GEO_TOO_BROAD,
    REASON_OK,
    REASON_SCOPE_MISMATCH,
    SCOPE_MATCHED,
    SCOPE_MISMATCHED,
)
from tools.keyword_expander import KeywordExpander

# ── §5 首段:fixture 的业务范围与服务市场 ───────────────────────────────
FIXTURE_SCOPE = "商场运营、品牌招商、商铺租赁、餐饮入驻、家具建材经营、办公家具经营"
FIXTURE_SCOPE_NO_FURNITURE = "商场运营、品牌招商、商铺租赁、餐饮入驻"
FIXTURE_INDUSTRY = "商业地产运营"
FIXTURE_ADDRESS = "广东省深圳市龙岗区"

# ── §5.1 必须默认进入交付(截图里被误排除的六条精准词)───────────────────
PRECISE_LOCAL = [
    "深圳龙岗买家具建材去哪里好",
    "深圳龙岗租商铺做餐饮哪里合适",
    "深圳龙岗办公家具批发市场在哪",
    "深圳龙岗建材市场有哪些",
    "深圳龙岗商场招商电话",
    "深圳龙岗哪个商场适合租商铺开店",
]

# ── §5.2 本地客户不得默认进入,但不得冒充知识题(截图里被误入选的泛词)──
OVERBROAD_COMMERCIAL = [
    "商场推荐",
    "商业广场推荐",
    "商业综合体推荐",
    "商超推荐",
    "商业广场品牌推荐",
]

# ── §5.5 必须保持知识/方法类 ────────────────────────────────────────────
KNOWLEDGE_CONTROLS = [
    "商场是什么",
    "商场招商流程",
    "建材市场发展趋势",
    "租商铺需要什么证件",
    "商业综合体设计标准",
]


def _make_expander(candidates: list[str]) -> KeywordExpander:
    """真实 expander,只把两个**外部数据源**换成确定性假返回。

    判定链路(Step 5 三轴 / Step 5a 结构过滤 / Step 6 / 分桶)一行没被替换 ——
    这是"端到端"和"源码字符串断言"的区别所在。
    """
    expander = KeywordExpander()
    expander.llm_api_key = "test-key"

    async def fake_llm_expand(*_a, **_k):
        return [(kw, "行业核心") for kw in candidates]

    async def fake_5118(*_a, **_k):
        return []

    async def fake_drill(*_a, **_k):
        return {"sub_regions": [{"name": "龙岗区"}, {"name": "龙岗"}], "region_aliases": []}

    expander._llm_expand = fake_llm_expand
    expander._fetch_5118 = fake_5118
    expander._5118_expand = fake_5118
    expander._drill_region = fake_drill
    return expander


def _scope_lock(market_level: str, service_market: list[str]) -> dict:
    return {
        "service_market": service_market,
        "market_level": market_level,
        "business_type": "B2C",
        "buyer_persona": "本地商业项目招商负责人",
        "real_query_seeds": [],
        "sub_regions": ["龙岗", "龙岗区"] if service_market else [],
        "provinces": ["广东"] if service_market else [],
        "source": "manual",
    }


def run_expansion(
    candidates: list[str],
    *,
    business_scope: str = FIXTURE_SCOPE,
    market_level: str = "district",
    service_market: list[str] | None = None,
    city: str = FIXTURE_ADDRESS,
) -> dict[str, dict]:
    """跑真实扩词 → {keyword: 该词的最终结构化结果}(交付区与待确认区都收)。"""
    if service_market is None:
        service_market = ["深圳"]
    expander = _make_expander(candidates)
    result = asyncio.run(expander.expand_keywords(
        core_keywords=["商场招商"],
        industry=FIXTURE_INDUSTRY,
        city=city,
        business_scope=business_scope,
        target_count=60,
        profile_data={"core_business": business_scope},
        brand_name="龙岗某商业中心",
        scope_lock=_scope_lock(market_level, service_market),
    ))
    out: dict[str, dict] = {}
    for row in result.get("keywords", []):
        out[row["keyword"]] = row
    for row in result.get("rejected_keywords", []):
        out.setdefault(row["keyword"], row)
    out["__result__"] = result
    return out


@pytest.fixture(scope="module")
def local_client_matrix() -> dict[str, dict]:
    """一次扩词跑完 §5.1 + §5.2 + §5.5 + §5.4 的深圳/上海对照词。"""
    return run_expansion([
        *PRECISE_LOCAL,
        *OVERBROAD_COMMERCIAL,
        *KNOWLEDGE_CONTROLS,
        "商业综合体设计公司",
        "商场洗手间在哪",
        "上海商场推荐",
        "深圳商业广场推荐",
    ])


# ============================================================================
# §5.1 必须默认进入交付
# ============================================================================

@pytest.mark.parametrize("keyword", PRECISE_LOCAL)
def test_precise_local_commercial_keywords_enter_delivery(local_client_matrix, keyword):
    row = local_client_matrix.get(keyword)
    assert row is not None, f"「{keyword}」根本没出现在响应里 = 被静默丢弃(违反 T2)"
    assert row["commercial_intent"] == INTENT_COMMERCIAL, row
    assert row["business_scope"] == SCOPE_MATCHED, row
    assert row["geo_scope"] == GEO_MATCHED, row
    assert row["default_selected"] is True, row
    assert row["reason_code"] == REASON_OK, row


def test_precise_local_keywords_are_not_called_knowledge_questions(local_client_matrix):
    """事故现场那句话:六条精准词被统一解释成"通常不会让 AI 推荐具体品牌"。

    这条锁**只钉那句话不许再出现在这六条词上** —— 与上面的三轴断言不是重复:
    三轴断言换了实现也可能仍然把这句文案挂上去。
    """
    for keyword in PRECISE_LOCAL:
        row = local_client_matrix[keyword]
        text = f"{row.get('reason_text', '')}{row.get('rejection_reason', '')}"
        assert "不会让 AI 推荐" not in text, (keyword, text)
        assert "不会推荐" not in text, (keyword, text)


# ============================================================================
# §5.2 本地客户不得默认进入,但不得冒充知识题
# ============================================================================

@pytest.mark.parametrize("keyword", OVERBROAD_COMMERCIAL)
def test_overbroad_commercial_keywords_are_geo_too_broad_not_knowledge(
    local_client_matrix, keyword,
):
    row = local_client_matrix.get(keyword)
    assert row is not None, f"「{keyword}」被静默丢弃"
    assert row["commercial_intent"] == INTENT_COMMERCIAL, row
    assert row["geo_scope"] == GEO_TOO_BROAD, row
    assert row["default_selected"] is False, row
    assert row["human_override_allowed"] is True, row
    assert row["reason_code"] == REASON_GEO_TOO_BROAD, row
    assert row["reason_group"] == GROUP_GEO_TOO_BROAD, row
    # 反向对照:绝不能被写成知识题(那正是老实现的 R2)
    assert row["commercial_intent"] != INTENT_KNOWLEDGE, row
    assert row["reason_group"] != GROUP_KNOWLEDGE, row


def test_design_company_is_business_mismatch_not_auto_selected(local_client_matrix):
    """`商业综合体设计公司` 老实现只因带"公司"二字就 PROVIDER_OBJECT 放行(§1.1)。

    对一个做商场经营/招商的客户,它求的是「设计」服务 → 业务不匹配。
    """
    row = local_client_matrix["商业综合体设计公司"]
    assert row["commercial_intent"] == INTENT_COMMERCIAL, row
    assert row["business_scope"] == SCOPE_MISMATCHED, row
    assert row["default_selected"] is False, row
    assert row["reason_code"] == REASON_SCOPE_MISMATCH, row
    assert row["reason_group"] == GROUP_SCOPE_MISMATCH, row
    assert row["human_override_allowed"] is True, row


# ============================================================================
# §5.3 业务范围反向对照(删掉家具建材业务)
# ============================================================================

FURNITURE_KEYWORDS = [
    "深圳龙岗买家具建材去哪里好",
    "深圳龙岗办公家具批发市场在哪",
    "深圳龙岗建材市场有哪些",
]


@pytest.fixture(scope="module")
def no_furniture_matrix() -> dict[str, dict]:
    return run_expansion(FURNITURE_KEYWORDS, business_scope=FIXTURE_SCOPE_NO_FURNITURE)


@pytest.mark.parametrize("keyword", FURNITURE_KEYWORDS)
def test_removing_business_line_blocks_delivery_but_keeps_commercial(
    no_furniture_matrix, keyword,
):
    row = no_furniture_matrix.get(keyword)
    assert row is not None, f"「{keyword}」被静默丢弃"
    assert row["commercial_intent"] == INTENT_COMMERCIAL, row
    assert row["business_scope"] in ("mismatched", "uncertain"), row
    assert row["default_selected"] is False, row
    # §5.3 逐字:"绝不能改成知识题"
    assert row["commercial_intent"] != INTENT_KNOWLEDGE, row
    assert row["reason_group"] != GROUP_KNOWLEDGE, row


@pytest.mark.parametrize("keyword", FURNITURE_KEYWORDS)
def test_business_axis_is_the_only_difference_between_the_two_fixtures(
    local_client_matrix, no_furniture_matrix, keyword,
):
    """成对判据:同一批词、同一地域,唯一变量是业务范围 → 结论必须翻转。

    没有这一条,上面两组断言各自都可能被一个"恒判 matched"或"恒判 uncertain"
    的实现同时满足。
    """
    with_furniture = local_client_matrix[keyword]
    without = no_furniture_matrix[keyword]
    assert with_furniture["geo_scope"] == without["geo_scope"] == GEO_MATCHED
    assert with_furniture["default_selected"] is True
    assert without["default_selected"] is False
    assert with_furniture["business_scope"] != without["business_scope"]


# ============================================================================
# §5.4 地域反向对照
# ============================================================================

def test_other_city_keyword_is_outside_market(local_client_matrix):
    row = local_client_matrix["上海商场推荐"]
    assert row["commercial_intent"] == INTENT_COMMERCIAL, row
    assert row["geo_scope"] == GEO_OUTSIDE_MARKET, row
    assert row["default_selected"] is False, row
    assert row["reason_code"] == REASON_GEO_OUTSIDE, row
    assert row["reason_group"] == GROUP_GEO_OUTSIDE, row


def test_service_city_keyword_is_geo_compatible(local_client_matrix):
    """`深圳商业广场推荐` 地域相容;是否进交付由业务范围决定(§5.4 第三条)。"""
    row = local_client_matrix["深圳商业广场推荐"]
    assert row["geo_scope"] == GEO_MATCHED, row


def test_district_keyword_is_geo_compatible(local_client_matrix):
    """§5.4 第四条:龙岗范围客户 + `深圳龙岗商场招商电话` → 地域相容。"""
    assert local_client_matrix["深圳龙岗商场招商电话"]["geo_scope"] == GEO_MATCHED


def test_national_client_may_default_select_geoless_commercial_word():
    """§5.4 第二条 —— 也是"无地域词全站硬删"这条假修复的反向对照(§8)。"""
    matrix = run_expansion(
        ["商场推荐", "商业广场推荐", "上海商场推荐"],
        market_level="national", service_market=[], city="",
    )
    assert matrix["商场推荐"]["geo_scope"] == GEO_MATCHED, matrix["商场推荐"]
    assert matrix["商场推荐"]["default_selected"] is True, matrix["商场推荐"]
    assert matrix["商业广场推荐"]["default_selected"] is True
    # 全国客户也不该收外地专属词?—— 全国即服务市场,这里只断言它没被当成本地泛词
    assert matrix["上海商场推荐"]["commercial_intent"] == INTENT_COMMERCIAL


def test_local_vs_national_is_the_only_difference_for_the_same_word():
    """成对判据:同一个 `商场推荐`,只改 market_level → too_broad ↔ matched。"""
    local = run_expansion(["商场推荐"])["商场推荐"]
    national = run_expansion(
        ["商场推荐"], market_level="national", service_market=[], city="",
    )["商场推荐"]
    assert local["geo_scope"] == GEO_TOO_BROAD
    assert national["geo_scope"] == GEO_MATCHED
    assert local["default_selected"] is False
    assert national["default_selected"] is True


# ============================================================================
# §5.5 知识题反向对照
# ============================================================================

@pytest.mark.parametrize("keyword", KNOWLEDGE_CONTROLS)
def test_knowledge_controls_stay_knowledge(local_client_matrix, keyword):
    row = local_client_matrix.get(keyword)
    assert row is not None, f"「{keyword}」被静默丢弃"
    assert row["commercial_intent"] == INTENT_KNOWLEDGE, row
    assert row["default_selected"] is False, row
    assert row["reason_group"] == GROUP_KNOWLEDGE, row


def test_toilet_question_is_not_captured_by_venue_rules(local_client_matrix):
    """工单 T1 逐字点名的反例:`商场洗手间在哪`。

    「在哪」不是万能商业信号 —— 它必须紧跟在**商业对象**之后才算数。
    这条锁是 T1 组合判定判别力的直接证明。
    """
    row = local_client_matrix["商场洗手间在哪"]
    assert row["commercial_intent"] == INTENT_UNCERTAIN, row
    assert row["default_selected"] is False, row
    assert row["reason_group"] == GROUP_NEEDS_CONFIRM, row


def test_venue_locator_pair_control():
    """成对判据:同样是「在哪」,紧跟商业对象就成立,跟着设施就不成立。

    只断言其中一半,一个"所有含在哪都判商业"或"所有含在哪都不判商业"的实现
    都能骗过去。
    """
    assert evaluate("深圳龙岗办公家具批发市场在哪").intent_type == INTENT_COMMERCIAL
    assert evaluate("商场洗手间在哪").intent_type != INTENT_COMMERCIAL


# ============================================================================
# T2:不静默删除 + 三态语义不再被压平
# ============================================================================

def test_no_candidate_is_silently_dropped(local_client_matrix):
    """每一个喂进去的原始关键词都必须在响应里找得到(交付区或待确认区)。"""
    fed = [
        *PRECISE_LOCAL, *OVERBROAD_COMMERCIAL, *KNOWLEDGE_CONTROLS,
        "商业综合体设计公司", "商场洗手间在哪", "上海商场推荐", "深圳商业广场推荐",
    ]
    missing = [kw for kw in fed if kw not in local_client_matrix]
    assert not missing, f"被静默丢弃:{missing}"


def test_uncertain_is_not_written_as_knowledge(local_client_matrix):
    """R2 的核心:`uncertain` 被 Step 5 统一写成 `knowledge`。

    在同一批结果里必须同时存在 knowledge 与 uncertain 两种取值 ——
    只断言"存在 uncertain"是不够的,那不能排除实现把所有词都标成 uncertain。
    """
    intents = {
        row["commercial_intent"]
        for kw, row in local_client_matrix.items() if kw != "__result__"
    }
    assert INTENT_UNCERTAIN in intents, intents
    assert INTENT_KNOWLEDGE in intents, intents
    assert INTENT_COMMERCIAL in intents, intents


def test_reason_groups_are_plural_not_one_bucket(local_client_matrix):
    """R5:所有原因被塞进同一块、同一句话。

    这一批词按设计应当落进**至少四个不同分组**;退回单桶实现时这条必红。
    """
    groups = {
        row.get("reason_group")
        for kw, row in local_client_matrix.items()
        if kw != "__result__" and row.get("default_selected") is False
    }
    assert len(groups) >= 4, groups
    assert GROUP_GEO_TOO_BROAD in groups
    assert GROUP_SCOPE_MISMATCH in groups
    assert GROUP_KNOWLEDGE in groups
    assert GROUP_GEO_OUTSIDE in groups


def test_rejected_rows_all_carry_a_stable_reason_code(local_client_matrix):
    result = local_client_matrix["__result__"]
    for row in result["rejected_keywords"]:
        assert row.get("reason_code"), row
        assert row.get("reason_group"), row
        assert row.get("reason_text"), row
        assert "human_override_allowed" in row, row


# ============================================================================
# T6:老字段必须是**派生**的,不是各写一套
# ============================================================================

def test_legacy_fields_are_derived_from_the_three_axes(local_client_matrix):
    for kw, row in local_client_matrix.items():
        if kw == "__result__":
            continue
        expected_eligible = row["commercial_intent"] in ("commercial", "brand_direct")
        assert row["commercial_delivery_eligible"] is expected_eligible, row
        assert row["geo_recommend"] is expected_eligible, row
        expected_scope_match = not (
            row["business_scope"] == "mismatched" or row["geo_scope"] != "matched"
        )
        assert row["scope_match"] is expected_scope_match, row


def test_step6_no_longer_hardcodes_scope_match_true(local_client_matrix):
    """R4 的直接反证:老 Step 6 把交付区每一条都写成 scope_match=True。

    现在交付区之外必须真的存在 scope_match=False 的行,且交付区内全为 True。
    """
    result = local_client_matrix["__result__"]
    assert result["keywords"], "交付区不该为空"
    assert all(r["scope_match"] is True for r in result["keywords"])
    assert any(r["scope_match"] is False for r in result["rejected_keywords"])
