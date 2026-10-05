"""B1 采集选题 / B5 写作策略选择的行为边界。

两条共同的红线在这里各测一遍:
  - **模型只准做减法**:选出候选集合以外的东西一律丢弃(防止把幻觉喂进生产管道);
  - **兜底后行为与改前一致**:模型不可用时全量 plan / 既定文体照旧,不因为智能层挂了就停摆。
"""
from __future__ import annotations

import pytest

from services.flywheel_judgment import JudgmentResult
from services.flywheel_topic_selection import select_round_topics
from services.flywheel_writing_strategy_choice import choose_writing_strategy

PLAN = {
    "industries": [
        {"id": 1, "name": "医美", "slug": "meiye"},
        {"id": 2, "name": "留学", "slug": "liuxue"},
        {"id": 3, "name": "装修", "slug": "zhuangxiu"},
    ],
    "prompts_by_industry": {
        1: [{"id": 11, "prompt_text": "医美哪家好"}],
        2: [{"id": 21, "prompt_text": "留学中介推荐"}],
        3: [{"id": 31, "prompt_text": "装修公司排名"}],
    },
}


def _stub_judge(monkeypatch, payload, source="llm"):
    monkeypatch.setattr(
        "services.flywheel_judgment.judge",
        lambda point_key, **kw: JudgmentResult(point_key, source, payload),
    )


@pytest.fixture(autouse=True)
def _no_db_facts(monkeypatch):
    """选题事实查询走真实 SQL,本组只测取舍逻辑,统一替身成空事实。"""
    monkeypatch.setattr(
        "services.flywheel_topic_selection._industry_facts", lambda ids: {}
    )


# ---------------- B1 ----------------

def test_topic_selection_filters_plan(monkeypatch):
    _stub_judge(monkeypatch, {"selected_industry_ids": [1, 3], "reasons": {"1": "客户多"}})
    out = select_round_topics(PLAN)
    assert [i["id"] for i in out["industries"]] == [1, 3]
    assert set(out["prompts_by_industry"]) == {1, 3}
    assert out["topic_selection"]["source"] == "llm"


def test_topic_selection_drops_ids_outside_the_bank(monkeypatch):
    """🔒 模型只准从 active 题库里挑;挑出题库外的行业必须被丢弃,不许造题。"""
    _stub_judge(monkeypatch, {"selected_industry_ids": [1, 999, "abc"]})
    out = select_round_topics(PLAN)
    assert [i["id"] for i in out["industries"]] == [1]


def test_topic_selection_falls_back_to_full_plan_when_empty(monkeypatch):
    _stub_judge(monkeypatch, {"selected_industry_ids": [999]})
    out = select_round_topics(PLAN)
    assert out is PLAN, "全部越界时必须原样退回全量 plan(= 改前行为)"


def test_topic_selection_respects_max_industries(monkeypatch):
    _stub_judge(monkeypatch, {"selected_industry_ids": [1, 2, 3]})
    out = select_round_topics(PLAN, max_industries=2)
    assert len(out["industries"]) == 2


def test_topic_selection_keeps_full_plan_when_selection_has_no_prompts(monkeypatch):
    """挑出来的行业一个 prompt 都没有 = 会把一轮跑空,宁可退回全量。"""
    plan = {
        "industries": PLAN["industries"],
        "prompts_by_industry": {1: [], 2: [], 3: [{"id": 31, "prompt_text": "x"}]},
    }
    _stub_judge(monkeypatch, {"selected_industry_ids": [1, 2]})
    assert select_round_topics(plan) is plan


def test_single_industry_plan_skips_judgment(monkeypatch):
    """只有一个行业时没有取舍空间,直接跳过 —— 不浪费一次调用。"""
    def _boom(*a, **kw):
        raise AssertionError("单行业不该调用判断层")

    monkeypatch.setattr("services.flywheel_judgment.judge", _boom)
    plan = {"industries": [{"id": 1, "name": "医美"}], "prompts_by_industry": {1: [{"id": 11}]}}
    assert select_round_topics(plan) is plan


# ---------------- B5 ----------------

def test_style_choice_rejects_family_outside_contract(monkeypatch):
    """🔒 只能选六文体家族契约里的家族;契约外的一律回落既定家族。"""
    monkeypatch.setattr(
        "services.flywheel_writing_strategy_choice.family_citation_stats", lambda ik: {}
    )
    _stub_judge(monkeypatch, {"style_family": "我编的家族", "angle": "x", "reason": "y"})
    out = choose_writing_strategy(industry_key="geo_test", default_family="guide")
    assert out["style_family"] == "guide"


def test_style_choice_uses_valid_family(monkeypatch):
    from writing.article_style_contract import USER_CHOICE_FAMILY_CODES

    target = USER_CHOICE_FAMILY_CODES[0]
    monkeypatch.setattr(
        "services.flywheel_writing_strategy_choice.family_citation_stats", lambda ik: {}
    )
    _stub_judge(monkeypatch, {"style_family": target, "angle": "多用可核对数据", "reason": "r"})
    out = choose_writing_strategy(industry_key="geo_test", default_family="guide")
    assert out["style_family"] == target
    assert out["angle"] == "多用可核对数据"


def test_style_choice_rule_fallback_picks_best_cited_family(monkeypatch):
    """规则兜底不是"随便给一个":有被引数据时挑被引密度最高的家族。"""
    from services.flywheel_writing_strategy_choice import _rule_choice, _style_families

    families = _style_families()
    stats = {
        families[0]: {"assignments": 4, "citations": 1, "citations_per_assignment": 0.25},
        families[1]: {"assignments": 3, "citations": 9, "citations_per_assignment": 3.0},
    }
    out = _rule_choice(families, stats, default_family="guide")
    assert out["style_family"] == families[1]


def test_style_choice_rule_fallback_keeps_default_without_data():
    from services.flywheel_writing_strategy_choice import _rule_choice, _style_families

    out = _rule_choice(_style_families(), {}, default_family="guide")
    assert out["style_family"] == "guide", "没有被引数据时必须沿用既定家族(= 改前行为)"


def test_style_choice_survives_missing_contract(monkeypatch):
    monkeypatch.setattr(
        "services.flywheel_writing_strategy_choice._style_families", lambda: []
    )
    out = choose_writing_strategy(industry_key="geo_test", default_family="guide")
    assert out["style_family"] == "guide"
    assert out["fallback_reason"] == "style_contract_unavailable"
