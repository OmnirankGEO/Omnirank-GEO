"""Regression coverage for underfilled quote keyword expansion."""

import pytest

from tools.keyword_expander import KeywordExpander


DEMO_CORES = [
    "激光加工设备外贸货源",
    "创客教室激光设备采购",
    "小型激光雕刻机DIY",
    "礼品定制激光雕刻设备",
]


@pytest.mark.asyncio
async def test_three_upstream_keywords_are_not_reported_as_complete(monkeypatch):
    expander = KeywordExpander()

    async def fake_llm_expand(**_kwargs):
        return [
            ("小型激光雕刻机品牌推荐", "认知"),
            ("便携式激光雕刻机推荐", "认知"),
            ("小型激光雕刻机推荐", "认知"),
        ]

    async def fake_5118_expand(**_kwargs):
        return []

    monkeypatch.setattr(expander, "_llm_expand", fake_llm_expand)
    monkeypatch.setattr(expander, "_5118_expand", fake_5118_expand)

    result = await expander.expand_keywords(
        core_keywords=DEMO_CORES,
        industry="制造业",
        city="全国",
        target_count=50,
    )

    assert result["success"] is True
    assert len(result["keywords"]) >= 20
    assert result["summary"]["minimum_expected"] == 20
    assert result["summary"]["supplemented"] >= 17
    assert result["summary"]["quality_gate_passed"] is True
    assert {row["keyword"] for row in result["keywords"]}.issuperset(
        {
            "小型激光雕刻机品牌推荐",
            "便携式激光雕刻机推荐",
            "小型激光雕刻机推荐",
        }
    )
    supplements = [row for row in result["keywords"] if row["source"] == "rule_supplement"]
    assert supplements
    assert all(any(core in row["keyword"] for core in DEMO_CORES) for row in supplements)
    assert all(any(core in row["keyword"] for row in supplements) for core in DEMO_CORES)


@pytest.mark.asyncio
async def test_small_explicit_target_is_not_padded_past_request(monkeypatch):
    expander = KeywordExpander()

    async def fake_llm_expand(**_kwargs):
        return [
            ("激光雕刻机推荐", "认知"),
            ("激光雕刻机哪家好", "对比"),
            ("激光雕刻机报价", "决策"),
        ]

    async def fake_5118_expand(**_kwargs):
        return []

    monkeypatch.setattr(expander, "_llm_expand", fake_llm_expand)
    monkeypatch.setattr(expander, "_5118_expand", fake_5118_expand)

    result = await expander.expand_keywords(
        core_keywords=["激光雕刻机"],
        industry="制造业",
        city="全国",
        target_count=3,
    )

    assert result["success"] is True
    assert len(result["keywords"]) == 3
    assert result["summary"]["minimum_expected"] == 3
    assert result["summary"]["supplemented"] == 0
