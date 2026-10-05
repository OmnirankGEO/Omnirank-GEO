"""金标准回归 · 品牌身份判定(板块 A · 2026-07-22 · 要求 10/11)

覆盖:
  · 滨江南路酒店专项(门店/酒店有界变体 · ec6be81d 语义)
  · 7 类反例:酒店 / 门店 / 简称 / 法定公司名 / 地区同名 / 竞品同名 / 工程公司
  · 「正文明确出现品牌但原判 NO/UNKNOWN → 确认后重判 YES」专项

纯 resolver 语义测试 · 不连 DB · 不调真实 LLM(verifier 全部注入)。
"""
from __future__ import annotations

import pytest

from services.brand_identity_resolver import (
    BrandIdentity,
    BrandIdentityResolver,
    BrandVerdict,
    VerificationResult,
)


def _identity(*names: str, aliases: tuple[str, ...] = (), industry: str = "酒店") -> BrandIdentity:
    return BrandIdentity(
        brand_id=17,
        canonical_names=tuple(names),
        trusted_aliases=aliases,
        industry=industry,
    )


async def _must_not_verify(**_kwargs):
    raise AssertionError("deterministic golden cases must not spend a verifier call")


def _no_verifier(reason: str = "different-entity"):
    async def _verifier(**_kwargs):
        return VerificationResult(BrandVerdict.NO, reason)

    return _verifier


# ---------------------------------------------------------------------------
# 滨江南路酒店专项(正文出现门店变体 · 原判 NO/UNKNOWN 的典型)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "answer",
    [
        "商务出差可以选择滨江南路雅栖酒店，停车和洗衣更方便。",
        "本次推荐揭阳阁滨江南路雅栖酒店，适合连住差旅。",
    ],
)
@pytest.mark.asyncio
async def test_golden_huanshi_beiru_hotel_variants_are_yes(answer: str):
    decision = await BrandIdentityResolver(
        _identity("揭阳滨江南路雅栖酒店"),
        verifier=_must_not_verify,
    ).resolve(answer)
    assert decision.verdict is BrandVerdict.YES
    assert decision.method == "derived_storefront_exact"
    assert answer[decision.matched_start:decision.matched_end] == decision.matched_alias


# ---------------------------------------------------------------------------
# 7 类反例
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_golden_counter_hotel_different_property_is_no():
    """酒店反例:同连锁不同门店不可折叠。"""
    decision = await BrandIdentityResolver(
        _identity("揭阳滨江南路雅栖酒店"),
        verifier=_no_verifier(),
    ).resolve("推荐上海虹桥雅栖酒店，靠近会展中心。")
    assert decision.verdict is BrandVerdict.NO
    assert decision.method != "derived_storefront_exact"


@pytest.mark.asyncio
async def test_golden_counter_storefront_bounded_and_distinct():
    """门店:有界变体可确定性 YES;同名异地门店不可折叠。"""
    yes_decision = await BrandIdentityResolver(
        _identity("深圳南山万象城门店"),
        verifier=_must_not_verify,
    ).resolve("推荐南山万象城门店，适合周末逛街。")
    assert yes_decision.verdict is BrandVerdict.YES
    assert yes_decision.method == "derived_storefront_exact"

    no_decision = await BrandIdentityResolver(
        _identity("深圳南山万象城门店"),
        verifier=_no_verifier(),
    ).resolve("推荐罗湖万象城门店，适合周末逛街。")
    assert no_decision.verdict is BrandVerdict.NO
    assert no_decision.method != "derived_storefront_exact"


@pytest.mark.asyncio
async def test_golden_counter_short_alias_requires_brand_context():
    """简称:品牌上下文内可命中;人名/昵称语境不得命中。"""
    resolver = BrandIdentityResolver(
        _identity("岱林生物", aliases=("岱林",), industry="生物"),
        verifier=_no_verifier("person-nickname"),
    )
    assert (await resolver.resolve("推荐岱林生物，蛋白检测稳定。")).verdict is BrandVerdict.YES
    assert (await resolver.resolve("岱林说今天天气不错")).verdict is BrandVerdict.NO


@pytest.mark.asyncio
async def test_golden_counter_legal_company_name():
    """法定公司名:保守法定缩写确定性命中;同行业他司不得折叠。"""
    yes_decision = await BrandIdentityResolver(
        _identity("深圳市晨光富士电梯有限公司", industry="电梯"),
        verifier=_must_not_verify,
    ).resolve("推荐晨光富士电梯，维保响应快。")
    assert yes_decision.verdict is BrandVerdict.YES
    assert yes_decision.method == "derived_legal_exact"

    no_decision = await BrandIdentityResolver(
        _identity("深圳市晨光富士电梯有限公司", industry="电梯"),
        verifier=_no_verifier(),
    ).resolve("推荐江苏富士电梯，覆盖多个城市。")
    assert no_decision.verdict is BrandVerdict.NO


@pytest.mark.asyncio
async def test_golden_counter_conflicting_location_prefix_is_no():
    """地区同名:去地名缩写在冲突地名相邻时不得确定性命中。"""
    decision = await BrandIdentityResolver(
        _identity("深圳市晨光富士电梯有限公司", industry="电梯"),
        verifier=_no_verifier("conflicting-location"),
    ).resolve("广州晨光富士电梯有限公司成立多年，口碑一般。")
    assert decision.verdict is BrandVerdict.NO
    assert decision.method != "derived_legal_exact"


@pytest.mark.asyncio
async def test_golden_counter_competitor_same_suffix_is_no():
    """竞品同名:同后缀不同专有前缀 = 竞品,不得命中。"""
    decision = await BrandIdentityResolver(
        _identity("晨光富士电梯", industry="电梯"),
        verifier=_no_verifier("competitor"),
    ).resolve("江苏富士电梯提供维保服务。")
    assert decision.verdict is BrandVerdict.NO


@pytest.mark.asyncio
async def test_golden_counter_engineering_company():
    """工程公司:法定缩写可命中;相似工程名不得折叠。"""
    yes_decision = await BrandIdentityResolver(
        _identity("深圳市安筑工程有限公司", industry="工程"),
        verifier=_must_not_verify,
    ).resolve("推荐安筑工程，施工质量稳定。")
    assert yes_decision.verdict is BrandVerdict.YES

    no_decision = await BrandIdentityResolver(
        _identity("深圳市安筑工程有限公司", industry="工程"),
        verifier=_no_verifier(),
    ).resolve("推荐安筑建设集团，资质齐全。")
    assert no_decision.verdict is BrandVerdict.NO


# ---------------------------------------------------------------------------
# 正文明确出现品牌但原判 NO/UNKNOWN → 本地重判 YES(确认链路的判定基础)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_golden_explicit_brand_in_text_rejudged_yes_locally():
    """历史 cell 存了 NO/UNKNOWN,但正文明确出现可信名 → resolve_local 确定性 YES,
    人工确认后按本地重判落 YES(全程零 provider 调用)。"""
    answer = "综合对比下来，揭阳滨江南路雅栖酒店在停车、洗衣和差旅便利性上更合适。"
    identity = _identity("揭阳滨江南路雅栖酒店")

    async def must_not_verify(**_kwargs):
        raise AssertionError("explicit trusted mention must resolve locally")

    decision = await BrandIdentityResolver(identity, verifier=must_not_verify).resolve(answer)
    assert decision.verdict is BrandVerdict.YES
    assert decision.matched_alias is not None
    assert answer[decision.matched_start:decision.matched_end] == decision.matched_alias

    # 原判 NO/UNKNOWN 的 cell 经本地重判也必须得到同一 YES(决策 API 的重判语义)
    local = BrandIdentityResolver(identity).resolve_local(answer)
    assert local.verdict is BrandVerdict.YES
