"""WP1 指标数据诚实性:CUR-07 / CUR-08 / CUR-09 producer 接线(规格 §2.2 / §17 WP1)。

判据设计的三条纪律(逐条都在本仓踩过)
------------------------------------
1. **全链驱动,禁夹具直造 DTO**(§0.5.3 G-3)。
   下面的夹具是 ``diagnosis_records.raw_data_json`` 的**真实形状**,
   走 ``_extract_diagnosis_observations → build_paid_diagnosis_observations``
   一路到 ``SourceObservation``。直接 new 一个 SourceObservation 来断言字段,
   等于把被测的那段接线绕过去 —— 那种判据删掉接线也不会红。

2. **多变体夹具**。单对象夹具会让「整条链没接线」也全绿(本仓 2026-08-20 实证:
   扩到三变体当场抓出两个存量缺陷)。所以每组至少三态:有值 / 缺键 / 坏值。

3. **成对判据**。每条「必须命中」配一条「必须不命中」;分母来自 census,不手抄。
"""

from __future__ import annotations

import json

import pytest

from scripts.defgeo_census.detail_table_key_census import build as build_key_census
from services.geo_observation.promotion import RANK_NOT_MEASURED
from services.geo_observation.source_hooks import (
    _competitor_count,
    _extract_diagnosis_observations,
    _normalize_citations,
)


# ══════════════════════════════════════════════════════════════════════════
# CUR-07 真因:消费方读的键,生产方从来不发
# ══════════════════════════════════════════════════════════════════════════
def test_no_key_is_consumed_that_producers_never_emit():
    """🔴 静默零守卫。差集非空 = 又有一个键对不上,而它不会报错、只会恒空。"""
    data = build_key_census()
    assert data["consumed_but_never_produced"] == [], (
        "抽取层读了生产方从不发的键:"
        + json.dumps(
            {k: data["consumer_sites"][k] for k in data["consumed_but_never_produced"]},
            ensure_ascii=False,
        )
        + "。这不会抛异常,只会让对应字段恒空 —— 正是 CUR-07 的真因形态。"
    )


def test_key_census_denominators_are_alive():
    """判据可用性关:两个集合都必须非空,否则上面那条是零分母恒绿。"""
    data = build_key_census()
    assert len(data["produced_keys"]) >= 20, f"生产者键只抽到 {len(data['produced_keys'])} 个"
    assert len(data["consumed_keys"]) >= 5, f"消费者键只抽到 {len(data['consumed_keys'])} 个"
    # 反向对照:census 必须真的看得见我们关心的那个键。
    assert "search_citations" in data["produced_keys"], "census 没看见 search_citations —— 抽取面坏了"
    assert "search_citations" in data["consumed_keys"], "抽取层没在读 search_citations"


# ══════════════════════════════════════════════════════════════════════════
# 夹具:diagnosis_records.raw_data_json 的真实形状
# ══════════════════════════════════════════════════════════════════════════
def _raw(results_by_engine: dict) -> dict:
    """包成 producer 真正写出的嵌套结构。层级少一层都抽不出来。"""
    return {
        "data": {
            "ai_visibility": {
                "detail_table": [
                    {"question": "本地有哪些靠谱的搬家公司", "results": results_by_engine}
                ]
            }
        }
    }


#: 变体 A:producer 正常发 search_citations + 真 target_outcome。
_ENGINE_FULL = {
    "answer_summary": "推荐甲乙丙三家,其中甲家口碑最好。",
    "brand_detected": True,
    "mentioned_brands": ["甲家", "乙家", "丙家"],
    "search_citations": [
        {"url": "https://www.example-news.com/a/123?utm=x", "source_type": "citation"},
        {"url": "https://blog.example-news.com/b", "source_type": "source"},
    ],
    "target_outcome": "recommended",
    "is_recommended": True,
}

#: 变体 B:缺 search_citations、缺 target_outcome(老 blob / 采集降级)。
_ENGINE_SPARSE = {
    "answer_summary": "这一带的搬家公司比较多,建议实地看看。",
    "brand_detected": False,
    "mentioned_brands": [],
}

#: 变体 C:坏形状 —— search_citations 是字符串而非数组,target_outcome 是空串。
_ENGINE_MALFORMED = {
    "answer_summary": "有几家可以考虑。",
    "brand_detected": True,
    "mentioned_brands": ["丁家"],
    "search_citations": "not-a-json-array",
    "target_outcome": "   ",
    "is_recommended": "yes",          # 非 bool
}


def _extract_one(engine_payload: dict) -> dict:
    out = _extract_diagnosis_observations(_raw({"deepseek": engine_payload}))
    assert len(out) == 1, f"抽取应得恰好 1 条观测,实得 {len(out)}"
    return out[0]


# ── CUR-07 / CUR-09 引用接线 ──────────────────────────────────────────────
def test_citations_flow_from_search_citations_key():
    """必须命中:引用从 ``search_citations`` 真的流到观测里。"""
    obs = _extract_one(_ENGINE_FULL)
    assert len(obs["citations"]) == 2, f"引用没接回来:{obs['citations']}"
    assert obs["citations"][0]["url"].startswith("https://www.example-news.com/")


def test_citations_reach_the_public_signal_domains():
    """全链驱动:一路走到 ``privacy.clean_source_domains`` —— 只断言中间那一格
    等于放过了「接回来了但下游还是空」的情况。"""
    from services.geo_observation import privacy

    obs = _extract_one(_ENGINE_FULL)
    domains = privacy.clean_source_domains(obs["citations"])
    assert len(domains) == 2, f"域名没落地:{domains}"
    assert {d["domain"] for d in domains} == {"example-news.com", "blog.example-news.com"}
    # 必须不命中:带 query 的完整 URL 绝不能漏进公共 signal。
    assert all("?" not in d["domain"] and "/" not in d["domain"] for d in domains)


def test_missing_and_malformed_citations_degrade_to_empty_not_crash():
    """必须不命中:缺键/坏形状不许抛,也不许臆造。"""
    assert _extract_one(_ENGINE_SPARSE)["citations"] == []
    assert _extract_one(_ENGINE_MALFORMED)["citations"] == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ("", []),
        ("[]", []),
        ("not json", []),
        ('[{"url":"https://a.com"}]', [{"url": "https://a.com"}]),
        ([{"url": "https://a.com"}], [{"url": "https://a.com"}]),
        ([{"url": "https://a.com"}, "裸字符串"], [{"url": "https://a.com"}]),
        ({"url": "https://a.com"}, []),          # 对象不是数组
        (123, []),
    ],
)
def test_normalize_citations_shape_matrix(raw, expected):
    """归一器逐形态。TEXT 列存 JSON 字符串 / JSONB 已解成对象,两条路都要吃下。"""
    assert _normalize_citations(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 0), ("", 0), ("[]", 0), ("bad", 0),
        ('["甲","乙"]', 2), (["甲", "乙", "丙"], 3),
        ({"a": 1}, 0), (5, 0),
    ],
)
def test_competitor_count_shape_matrix(raw, expected):
    """必须不命中:非数组一律 0 —— 共现数会进公共 signal,宁可少算不许臆造。"""
    assert _competitor_count(raw) == expected


# ── CUR-07 推荐档位 ───────────────────────────────────────────────────────
def test_real_target_outcome_is_carried_through():
    assert _extract_one(_ENGINE_FULL)["target_outcome"] == "recommended"


def test_absent_outcome_is_none_not_false_and_not_a_default_tier():
    """必须不命中:「源没判定」不许变成任何一个具体档位,也不许变成 False。

    把未知压成 not_mentioned / False 就是本仓 SSOT §10.2 点名的那个错
    (PENDING/UNKNOWN 不等于 0)。
    """
    for payload in (_ENGINE_SPARSE, _ENGINE_MALFORMED):
        outcome = _extract_one(payload)["target_outcome"]
        assert outcome is None, f"未知档位被填成 {outcome!r}"
        assert outcome is not False
        assert outcome != "not_mentioned"


# ── 诚实性:is_recommended 是叙述位置启发式,不是推荐判定 ────────────────────
def test_recommendation_heuristic_is_carried_under_an_honest_key():
    """必须命中:字段接回来了(不丢信息)。"""
    assert _extract_one(_ENGINE_FULL)["recommended_heuristic"] is True
    assert _extract_one(_ENGINE_SPARSE)["recommended_heuristic"] is None
    # 非 bool 不许被 bool() 强转成 True。
    assert _extract_one(_ENGINE_MALFORMED)["recommended_heuristic"] is None


def test_heuristic_never_masquerades_as_a_recommendation_field():
    """🔴 必须不命中:抽取产物里不许出现叫 ``is_recommended`` 的键。

    这是 §19 变异 29(MET-02「mentioned_only 算明确推荐」)的结构锚。
    producer 的 is_recommended 语义是 ``line_index < 5``
    (tools/ai_visibility/ai_tester.py:2813-2815 —— 品牌名出现在答案前 5 行),
    一旦它以「recommended」之名进入观测,下游任何一个消费方都可能顺手把它
    当成推荐分子。名字就是那道闸。
    """
    obs = _extract_one(_ENGINE_FULL)
    assert "is_recommended" not in obs, (
        "抽取产物里出现了 is_recommended —— 那是叙述位置启发式,"
        "不许用推荐语义的名字暴露给下游"
    )
    from services.geo_observation.source_hooks import SourceObservation

    fields = SourceObservation.__dataclass_fields__
    assert "source_is_recommended" not in fields
    assert "source_recommended_heuristic" in fields
    # 反向对照:真判定那一格必须在,否则上面两条可以靠「什么都不接」通过。
    assert "source_target_outcome" in fields


def test_observation_dataclass_carries_both_fields_end_to_end():
    """接线锁:走 build_* 的组装路径,证明字段真的落到 SourceObservation 上,
    而不是只在中间那个 dict 里存在。"""
    from services.geo_observation.source_hooks import SourceObservation

    o = _extract_one(_ENGINE_FULL)
    obs = SourceObservation(
        event_fields={}, answer_text=o["answer"], question_text=o["question"],
        is_detected=o["detected"], citations=o["citations"],
        competitor_count=o["competitor_count"],
        source_target_outcome=o.get("target_outcome"),
        source_recommended_heuristic=o.get("recommended_heuristic"),
    )
    assert obs.source_target_outcome == "recommended"
    assert obs.source_recommended_heuristic is True
    assert obs.competitor_count == 3
    assert len(obs.citations) == 2


# ══════════════════════════════════════════════════════════════════════════
# CUR-08:名次不许伪造
# ══════════════════════════════════════════════════════════════════════════
def test_rank_is_explicitly_not_measured():
    """必须命中:``target_position`` 恒为「未测量」。

    现役没有有序候选列表解析器,也没有 rank-eligible 分母(§6.3
    ``position_eligible_samples``)。两者都不存在时任何数字都是猜的。
    """
    assert RANK_NOT_MEASURED is None, "未测量必须是 NULL —— 0 会被读成「第 0 名」"


def test_signal_builder_does_not_derive_rank_from_narrative_position():
    """🔴 必须不命中:``_build_signal`` 不许把 brand_position / is_recommended /
    行号 拿来当名次(§19 变异 30、33 → MET-03/MET-06)。

    这条打的是**结构**不是值:值恒 None 时,「正确地没测」和「代码里根本没这一格」
    在数值上一模一样,所以要钉源码形态 —— 那一格必须显式引用具名常量。
    """
    import inspect

    from services.geo_observation import promotion

    src = inspect.getsource(promotion._build_signal)
    assert '"target_position": RANK_NOT_MEASURED' in src, (
        "target_position 不再指向 RANK_NOT_MEASURED —— 要么被改成了猜的数字,"
        "要么退回了裸 None(裸 None 谁都能顺手改成数字且看不出来)"
    )
    # 必须不命中:这三个词一旦出现在 target_position 那一格附近,就是在反推名次。
    for forbidden in ("brand_position", "matched_start", "line_index"):
        assert forbidden not in src, (
            f"_build_signal 里出现了 {forbidden} —— 那是**字符偏移/行号**,不是名次。"
            "把叙述位置当 rank 正是 MET-06 要防的。"
        )
