"""B2 语料价值判定 / B3 媒体组合决策 / B4 诊断沉淀复用。

三条共同红线各测一遍:
  - **统计口径由代码算**:模型报的数字一概不采信,坑位/标签的数值字段全部由代码回填;
  - **模型只准做减法**:越界的域名 / article_id / 竞品名一律丢弃;
  - **provider 全挂 → 规则兜底**:行为退回改前口径,管道照样出水。
"""
from __future__ import annotations

import pytest

from services.flywheel_judgment import JudgmentResult

# ---------------------------------------------------------------------------
# B3 · 媒体组合决策(吃 T2 的 as_dict() 结构,不 import T2 任何符号)
# ---------------------------------------------------------------------------
# 下面这份 fixture 逐字段对齐媒体包 `QuestionFamilyMix.as_dict()` /
# `CombinationPlan.as_dict()` 的真实输出(codex/media-balance-strict-profile-2026-07-29 @ 9d467e83)。
MIX = {
    "version": "geo-question-family-mix-v1.0",
    "keyword": "装修公司哪家好", "industry": "装修", "intent_bucket": "recommendation",
    "source": "question_family", "window_days": 180, "total_citations": 122,
    "trunk_share": 0.55, "vertical_share": 0.45,
    "explanation": "您这类问题 AI 引用 to8to.com 30%、sohu.com 27%(近 180 天 122 次引用)",
    "entries": [
        {"domain": "to8to.com", "citations": 36, "share": 0.30, "share_pct": 30.0,
         "role": "vertical_home", "role_label": "家居垂直", "family_key": "to8to",
         "family_label": "土巴兔", "is_trunk": False, "self_serve": False},
        {"domain": "sohu.com", "citations": 33, "share": 0.27, "share_pct": 27.0,
         "role": "portal", "role_label": "综合门户", "family_key": "sohu",
         "family_label": "搜狐", "is_trunk": True, "self_serve": False},
        {"domain": "cnblogs.com", "citations": 18, "share": 0.15, "share_pct": 15.0,
         "role": "tech_community", "role_label": "技术社区", "family_key": "cnblogs",
         "family_label": "博客园", "is_trunk": True, "self_serve": True},
        {"domain": "zhihu.com", "citations": 17, "share": 0.14, "share_pct": 14.0,
         "role": "qa_community", "role_label": "问答社区", "family_key": "zhihu",
         "family_label": "知乎", "is_trunk": True, "self_serve": True},
        {"domain": "163.com", "citations": 9, "share": 0.07, "share_pct": 7.0,
         "role": "portal", "role_label": "综合门户", "family_key": "netease",
         "family_label": "网易", "is_trunk": True, "self_serve": False},
        {"domain": "jiaju.sina.com.cn", "citations": 9, "share": 0.07, "share_pct": 7.0,
         "role": "vertical_home", "role_label": "家居垂直", "family_key": "sina_home",
         "family_label": "新浪家居", "is_trunk": False, "self_serve": False},
    ],
}


def _plan(domains, total_slots=4):
    by_domain = {e["domain"]: e for e in MIX["entries"]}
    slots = [
        {"domain": d, "role": by_domain[d]["role"], "role_label": by_domain[d]["role_label"],
         "is_trunk": by_domain[d]["is_trunk"], "share_pct": by_domain[d]["share_pct"],
         "citations": by_domain[d]["citations"], "fulfilled_by": f"媒体-{d}",
         "fulfilled_media_id": 1, "substituted": False, "substitution_note": "",
         "self_serve_action": None}
        for d in domains
    ]
    return {
        "version": MIX["version"], "advisory": True, "total_slots": total_slots,
        "trunk_slots": sum(1 for s in slots if s["is_trunk"]),
        "vertical_slots": sum(1 for s in slots if not s["is_trunk"]),
        "reason": MIX["explanation"], "mix": MIX, "slots": slots,
        "substitutions": [], "self_serve_offers": [],
    }


def _annotations(domains=("to8to.com", "sohu.com", "cnblogs.com", "zhihu.com")):
    return {"question_family_mix": MIX, "combination_plan": _plan(list(domains))}


def _stub_judge(monkeypatch, payload, source="llm"):
    monkeypatch.setattr(
        "services.flywheel_judgment.judge",
        lambda point_key, **kw: JudgmentResult(
            point_key, source, payload, provider="deepseek", model="deepseek-v4-flash"
        ),
    )


def test_b3_applies_model_selection(monkeypatch):
    from services.flywheel_media_mix_choice import refine_media_combination

    _stub_judge(monkeypatch, {
        "selected_domains": ["sohu.com", "163.com", "to8to.com", "jiaju.sina.com.cn"],
        "reason": "补两个垂直站",
    })
    out = refine_media_combination(_annotations(), keyword="装修公司哪家好", industry="装修")
    plan = out["combination_plan"]
    assert [s["domain"] for s in plan["slots"]] == [
        "sohu.com", "163.com", "to8to.com", "jiaju.sina.com.cn"
    ]
    assert plan["llm_choice"]["applied"] is True
    assert plan["advisory"] is True


def test_b3_recomputes_counts_from_code_not_model(monkeypatch):
    """🔒 统计口径由代码算:主干/垂类计数与坑位数值全部回填自 mix,不读模型。"""
    from services.flywheel_media_mix_choice import refine_media_combination

    _stub_judge(monkeypatch, {
        "selected_domains": ["sohu.com", "to8to.com"],
        "trunk_slots": 99, "vertical_slots": 99,          # 模型乱报的计数
        "slots": [{"domain": "sohu.com", "citations": 99999}],  # 模型乱报的被引数
        "reason": "x",
    })
    plan = refine_media_combination(_annotations())["combination_plan"]
    assert plan["trunk_slots"] == 1 and plan["vertical_slots"] == 1
    sohu = next(s for s in plan["slots"] if s["domain"] == "sohu.com")
    assert sohu["citations"] == 33, "被引数必须回填自 mix,不能采信模型输出"
    assert sohu["role_label"] == "综合门户"


def test_b3_drops_domains_outside_observed_mix(monkeypatch):
    from services.flywheel_media_mix_choice import refine_media_combination

    _stub_judge(monkeypatch, {
        "selected_domains": ["sohu.com", "我编的站.com", "to8to.com"], "reason": "x",
    })
    plan = refine_media_combination(_annotations())["combination_plan"]
    assert [s["domain"] for s in plan["slots"]] == ["sohu.com", "to8to.com"]


def test_b3_rejects_all_trunk_selection(monkeypatch):
    """🔒 混合结构守卫:数据里两类都有,模型却选成清一色 → 判不合规,退回代码方案。

    这正是这台引擎存在的意义(修正发布组合与真实被引域的错配),不能被一次模型抖动毁掉。
    """
    from services.flywheel_media_mix_choice import refine_media_combination

    captured = {}

    def _fake_judge(point_key, *, validate=None, rule_fallback, **kw):
        captured["valid"] = validate({
            "selected_domains": ["sohu.com", "cnblogs.com", "zhihu.com", "163.com"],
        })
        return JudgmentResult(point_key, "rule", rule_fallback(),
                              fallback_reason="all_providers_failed")

    monkeypatch.setattr("services.flywheel_judgment.judge", _fake_judge)
    original = _annotations()
    plan = refine_media_combination(original)["combination_plan"]
    assert captured["valid"] is False, "全主干的选择必须被判不合规"
    assert plan["llm_choice"]["applied"] is False
    assert [s["domain"] for s in plan["slots"]] == [
        "to8to.com", "sohu.com", "cnblogs.com", "zhihu.com"
    ]


def test_b3_falls_back_to_code_plan_when_providers_down(monkeypatch):
    from services.flywheel_media_mix_choice import refine_media_combination

    monkeypatch.setattr(
        "services.flywheel_judgment.judge",
        lambda point_key, **kw: JudgmentResult(
            point_key, "rule", None, fallback_reason="all_providers_failed"
        ),
    )
    plan = refine_media_combination(_annotations())["combination_plan"]
    assert plan["llm_choice"] == {
        "source": "rule", "fallback_reason": "all_providers_failed", "applied": False,
    }
    assert [s["domain"] for s in plan["slots"]] == [
        "to8to.com", "sohu.com", "cnblogs.com", "zhihu.com"
    ]


def test_b3_passes_through_when_t2_unavailable():
    """T2 不在场(媒体包未合流)时 annotations 是 None,必须原样返回,不炸。"""
    from services.flywheel_media_mix_choice import refine_media_combination

    payload = {"question_family_mix": None, "combination_plan": None}
    assert refine_media_combination(payload) is payload


def test_b3_skips_when_no_room_to_choose(monkeypatch):
    from services.flywheel_media_mix_choice import refine_media_combination

    def _boom(*a, **kw):
        raise AssertionError("候选不足 2 个时不该调用判断层")

    monkeypatch.setattr("services.flywheel_judgment.judge", _boom)
    thin = {"question_family_mix": {**MIX, "entries": MIX["entries"][:1]},
            "combination_plan": _plan(["to8to.com"], total_slots=1)}
    assert refine_media_combination(thin) is thin


# ---------------------------------------------------------------------------
# B2 · 语料价值判定
# ---------------------------------------------------------------------------
BATCH = [
    {"article_id": 501, "title": "2026 装修公司排行榜", "domain": "sohu.com",
     "intent_type": "ranking", "industry_key": "zhuangxiu", "industry_name": "装修",
     "citations": 7},
    {"article_id": 502, "title": "装修甲醛怎么处理", "domain": "zhihu.com",
     "intent_type": "faq", "industry_key": "zhuangxiu", "industry_name": "装修",
     "citations": 1},
]


def test_b2_rule_label_is_conservative():
    from services.flywheel_corpus_value import _rule_label

    high = _rule_label(BATCH[0])
    assert high["value_tier"] == "high" and high["reusable"] is True
    assert "diagnosis_question" in high["scenarios"]

    low = _rule_label(BATCH[1])
    assert low["value_tier"] == "low" and low["reusable"] is False


def test_b2_rule_label_handles_unclassified_intent():
    """生产 14044 篇 intent_type 为空 —— 反复被引的仍要给一个可用场景,不能标空。"""
    from services.flywheel_corpus_value import _rule_label

    label = _rule_label({**BATCH[0], "intent_type": None})
    assert label["reusable"] is True
    assert label["scenarios"] == ["writing_evidence"]


def test_b2_llm_labels_are_filtered_and_completed(monkeypatch):
    """🔒 越界 article_id 丢弃;模型漏判的补规则兜底 —— 进了批次就必须有结论。"""
    from services.flywheel_corpus_value import label_corpus_batch

    _stub_judge(monkeypatch, {"labels": [
        {"article_id": 501, "reusable": True, "value_tier": "high",
         "scenarios": ["diagnosis_question", "我编的场景"], "reason": "榜单可直接出题"},
        {"article_id": 999999, "reusable": True, "value_tier": "high", "scenarios": []},
    ]})
    out = label_corpus_batch(BATCH)
    by_id = {r["article_id"]: r for r in out}
    assert set(by_id) == {501, 502}, "越界 id 必须丢弃,漏判的必须补齐"
    assert by_id[501]["source"] == "llm"
    assert by_id[501]["scenarios"] == ["diagnosis_question"], "词表外的场景必须过滤"
    assert by_id[502]["source"] == "rule"


def test_b2_falls_back_to_rules_for_whole_batch(monkeypatch):
    from services.flywheel_corpus_value import label_corpus_batch

    monkeypatch.setattr(
        "services.flywheel_judgment.judge",
        lambda point_key, *, rule_fallback, **kw: JudgmentResult(
            point_key, "rule", rule_fallback(), fallback_reason="all_providers_failed"
        ),
    )
    out = label_corpus_batch(BATCH)
    assert len(out) == 2 and all(r["source"] == "rule" for r in out)


def test_b2_labels_roundtrip_through_db(flywheel_corpus_db):
    from db.flywheel_corpus_label_db import list_reusable_corpus, upsert_corpus_labels

    written = upsert_corpus_labels([
        {"article_id": 501, "industry_key": "zhuangxiu", "reusable": True,
         "value_tier": "high", "scenarios": ["diagnosis_question", "越界"],
         "reason": "r", "source": "llm", "model": "deepseek-v4-flash", "citations": 7},
        {"article_id": 502, "industry_key": "zhuangxiu", "reusable": False,
         "value_tier": "low", "scenarios": [], "reason": "r", "source": "rule",
         "citations": 1},
    ])
    assert written == 2
    rows = list_reusable_corpus("zhuangxiu", min_tier="medium")
    assert [r["article_id"] for r in rows] == [501], "reusable=False 的不该被下游取到"
    assert rows[0]["scenarios"] == ["diagnosis_question"], "词表外场景在写库层也要挡掉"

    # 幂等:同 (article_id, industry_key) 重跑刷新不新增行
    upsert_corpus_labels([{"article_id": 501, "industry_key": "zhuangxiu", "reusable": True,
                           "value_tier": "medium", "scenarios": [], "citations": 9}])
    rows = list_reusable_corpus("zhuangxiu", min_tier="medium")
    assert len(rows) == 1 and rows[0]["value_tier"] == "medium"


# ---------------------------------------------------------------------------
# B4 · 诊断沉淀复用
# ---------------------------------------------------------------------------
CORPUS = [{"article_id": 501, "value_tier": "high", "scenarios": ["diagnosis_question"],
           "citations": 7, "reason": "r"}]
ENTITIES = [
    {"entity_name": "甲公司", "entity_type": "brand", "mentions": 12,
     "best_rank": 1, "avg_confidence": 0.9},
    {"entity_name": "乙公司", "entity_type": "brand", "mentions": 8,
     "best_rank": 3, "avg_confidence": 0.8},
    {"entity_name": "本客户", "entity_type": "brand", "mentions": 2,
     "best_rank": 9, "avg_confidence": 0.5},
]


@pytest.fixture
def reuse_candidates(monkeypatch):
    monkeypatch.setattr(
        "services.flywheel_diagnosis_reuse._corpus_candidates", lambda k, n: list(CORPUS)
    )
    monkeypatch.setattr(
        "services.flywheel_diagnosis_reuse._competitor_candidates", lambda k, n: list(ENTITIES)
    )


def test_b4_returns_material_and_drops_out_of_scope(reuse_candidates, monkeypatch):
    """🔒 只准从候选里挑:编造的 article_id / 竞品名一律丢弃,客户自己也要剔掉。"""
    from services.flywheel_diagnosis_reuse import suggest_reusable_material

    _stub_judge(monkeypatch, {
        "corpus_article_ids": [501, 88888],
        "competitor_seeds": ["甲公司", "我编的公司", "本客户"],
        "question_seeds": ["深圳装修公司哪家靠谱", "  "],
        "reason": "同城同行",
    })
    out = suggest_reusable_material(
        brand_name="本客户", industry="装修", city="深圳", industry_key="zhuangxiu"
    )
    assert out["advisory"] is True
    assert out["corpus_article_ids"] == [501]
    assert out["competitor_seeds"] == ["甲公司"], "编造的与客户自己都必须剔掉"
    assert out["question_seeds"] == ["深圳装修公司哪家靠谱"]


def test_b4_rule_fallback_excludes_self(reuse_candidates, monkeypatch):
    from services.flywheel_diagnosis_reuse import suggest_reusable_material

    monkeypatch.setattr(
        "services.flywheel_judgment.judge",
        lambda point_key, *, rule_fallback, **kw: JudgmentResult(
            point_key, "rule", rule_fallback(), fallback_reason="all_providers_failed"
        ),
    )
    out = suggest_reusable_material(
        brand_name="本客户", industry="装修", industry_key="zhuangxiu"
    )
    assert out["source"] == "rule"
    assert out["competitor_seeds"] == ["甲公司", "乙公司"]
    assert out["question_seeds"] == [], "没有模型时不硬编码问题种子(硬编码正是那两处 bug 的病根)"


def test_b4_honest_when_industry_has_no_sediment(monkeypatch):
    from services.flywheel_diagnosis_reuse import suggest_reusable_material

    monkeypatch.setattr("services.flywheel_diagnosis_reuse._corpus_candidates", lambda k, n: [])
    monkeypatch.setattr("services.flywheel_diagnosis_reuse._competitor_candidates", lambda k, n: [])

    def _boom(*a, **kw):
        raise AssertionError("没有候选时不该调用判断层")

    monkeypatch.setattr("services.flywheel_judgment.judge", _boom)
    out = suggest_reusable_material(brand_name="X", industry="新行业", industry_key="new")
    assert out["corpus_article_ids"] == [] and out["competitor_seeds"] == []
    assert "尚无可复用沉淀" in out["reason"]


def test_b4_survives_candidate_query_failure(monkeypatch):
    """候选查询炸了也要给结果 —— 诊断绝不因为素材层不可用而卡住。"""
    from services.flywheel_diagnosis_reuse import suggest_reusable_material

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("services.flywheel_diagnosis_reuse._corpus_candidates", _boom)
    monkeypatch.setattr("services.flywheel_diagnosis_reuse._competitor_candidates", _boom)
    out = suggest_reusable_material(brand_name="X", industry="装修", industry_key="zhuangxiu")
    assert out["advisory"] is True and out["corpus_article_ids"] == []
