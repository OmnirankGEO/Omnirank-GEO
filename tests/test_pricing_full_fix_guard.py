"""Stage 3 联合爆价护栏 + Stage 4 P0-B 价值证据闸单测。

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 python -m pytest tests/test_pricing_full_fix_guard.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.pricing_bands as B            # noqa: E402
import tools.pricing_llm_assessor as A     # noqa: E402
import tools.llm_pricing_flag as F         # noqa: E402

CFG = {
    "factory_ceiling_niche": 6500.0,
    "factory_ceiling_local": 3500.0,
    "entry_selling_ceiling_niche": 15000.0,
    "entry_selling_ceiling_local": 10000.0,
    "no_cache_selling": 10000.0,
}


def _t(factory, selling=0, entry_factory=0, entry_selling=0):
    return {
        "entry": {"factory_price": entry_factory, "selling_price": entry_selling},
        "flagship": {"factory_price": factory, "selling_price": selling},
    }


def _bl(cost, vm, comp, factory):
    return {"cost": cost, "value_mult": vm, "true_competition": comp, "factory_price": factory}


# ---------------- Stage 3 护栏(纯函数)----------------
def test_guard_inert_all_flags_off():
    r = B.compute_blowup_guards(tiers=_t(99999, 99999), keyword_type="national_niche", value_mult=1.75,
                                national_unclamped=True, p0a_on=False, p0b_relaxed=False, p0c_on=False, cfg=CFG)
    assert r == {"needs_review": False, "guarantee_unavailable": False, "no_cache": False, "guards": {}}


def test_guard_cost_alone_no_trigger():
    # P0-A 成本单独上抬(预期纠偏)· value=1 · 无 national · factory 天花板下 → 不触发
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="local_city", value_mult=1.0,
                                national_unclamped=False, p0a_on=True, p0b_relaxed=False, p0c_on=False, cfg=CFG)
    assert r["needs_review"] is False and r["guarantee_unavailable"] is False and r["guards"] == {}


def test_guard_high_flagship_price_does_not_hide_affordable_entry_quote():
    # 只有高档/旗舰档超旧 ceiling 时,不能把整项变成「参考价·待人工核」。
    # 入门价仍可售,只做 no_cache 防止高价共享锁污染。
    r = B.compute_blowup_guards(
        tiers=_t(8000, 16000, entry_factory=1200, entry_selling=1560),
        keyword_type="local_city", value_mult=1.0,
        national_unclamped=False, p0a_on=True, p0b_relaxed=False, p0c_on=False, cfg=CFG,
    )
    assert r["guarantee_unavailable"] is False
    assert r["needs_review"] is False
    assert "factory_ceiling" not in r["guards"]
    assert "entry_price_ceiling" not in r["guards"]
    assert r["no_cache"] is True


def test_guard_entry_price_ceiling_local_hides_quote():
    r = B.compute_blowup_guards(
        tiers=_t(9000, 18000, entry_factory=9000, entry_selling=12000),
        keyword_type="local_city", value_mult=1.0,
        national_unclamped=False, p0a_on=True, p0b_relaxed=False, p0c_on=False, cfg=CFG,
    )
    assert r["guarantee_unavailable"] is True and r["needs_review"] is True
    assert r["guards"]["entry_price_ceiling"]["entry_selling"] == 12000.0
    assert r["guards"]["entry_price_ceiling"]["ceiling"] == 10000.0


def test_guard_entry_price_ceiling_national_uses_higher_threshold():
    r = B.compute_blowup_guards(
        tiers=_t(9000, 30000, entry_factory=9000, entry_selling=12000),
        keyword_type="national_niche", value_mult=1.0,
        national_unclamped=False, p0a_on=True, p0b_relaxed=False, p0c_on=False, cfg=CFG,
    )
    assert r["guarantee_unavailable"] is False
    assert r["needs_review"] is False
    assert "entry_price_ceiling" not in r["guards"]
    assert r["no_cache"] is True


def test_guard_entry_price_ceiling_national_hides_quote():
    r = B.compute_blowup_guards(
        tiers=_t(12000, 36000, entry_factory=12000, entry_selling=16000),
        keyword_type="national_head", value_mult=1.0,
        national_unclamped=False, p0a_on=True, p0b_relaxed=False, p0c_on=False, cfg=CFG,
    )
    assert r["guarantee_unavailable"] is True and r["needs_review"] is True
    assert r["guards"]["entry_price_ceiling"]["entry_selling"] == 16000.0
    assert r["guards"]["entry_price_ceiling"]["ceiling"] == 15000.0


def test_guard_three_levers_review_but_keep_affordable_entry_quote():
    r = B.compute_blowup_guards(
        tiers=_t(2000, 4000, entry_factory=1200, entry_selling=1560),
        keyword_type="national_niche", value_mult=1.5,
        national_unclamped=True, p0a_on=True, p0b_relaxed=True, p0c_on=False,
        cfg=CFG, cost=80.0, true_competition=40, baseline=_bl(55.0, 1.1, 28, 1200.0),
    )
    assert r["needs_review"] is True
    assert r["guarantee_unavailable"] is False
    assert "lever_three_compound" in r["guards"]
    assert "entry_price_ceiling" not in r["guards"]


def test_guard_lever_compound_value_and_competition():
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="national_niche", value_mult=1.5,
                                national_unclamped=True, p0a_on=True, p0b_relaxed=True, p0c_on=False, cfg=CFG)
    assert r["needs_review"] is True and r["guards"]["lever_compound"] == ["value", "competition"]


def test_guard_p0c_national_review_without_hiding_affordable_entry_quote():
    r = B.compute_blowup_guards(tiers=_t(2000, 4000), keyword_type="national_niche", value_mult=1.0,
                                national_unclamped=True, p0a_on=True, p0b_relaxed=False, p0c_on=True, cfg=CFG)
    assert r["guarantee_unavailable"] is False and r["needs_review"] is True
    assert "national_unclamped_review" in r["guards"]
    assert "national_unclamped_no_guarantee" not in r["guards"]


# ---------------- Stage 4 P0-B 价值证据闸(_assemble_result)----------------
LLM_META = {"llm_deviation_pct": 0.0, "llm_used": "dual"}


def _ctx_no5118(**kw):
    base = dict(keyword="深圳哪家好", measured_comp=12, saturated=False,
                metaso={"competition_count": 12, "content_count": 5}, cost_multiplier=1.0,
                dynamic_cost=55.0, five118={}, metaso_fallback=False)  # five118 空 = 无证据
    base.update(kw)
    return base


def _judged_hi(**kw):
    base = dict(true_competition=12, cost_per_article_suggested=55.0, keyword_type="local_city",
                city="深圳", city_tier="tier1", media_tier_required="B", value_signal=3.0,
                reasoning="x", risk_flags=[], confidence=0.8, national_unclamped=False)
    base.update(kw)
    return base


def _all_flags(monkeypatch, cost=False, value=False, nat=False):
    monkeypatch.setattr(F, "is_cost_snapshot_enabled", lambda: cost)
    monkeypatch.setattr(F, "is_value_evidence_gate_relaxed", lambda: value)
    monkeypatch.setattr(F, "is_national_unclamped_enabled", lambda: nat)


def test_p0b_off_clamps_no_evidence(monkeypatch):
    _all_flags(monkeypatch)  # 全关
    r = A._assemble_result(_ctx_no5118(), _judged_hi(), LLM_META, {}, 2.0, None)
    assert r["value_multiplier"] == 1.25                # 无 5118 证据 → signal 钳 1.0 → vm=1+1×0.25=1.25
    assert "value_evidence_cap" in r["guards"]


def test_p0b_on_relaxes_no_evidence(monkeypatch):
    _all_flags(monkeypatch, value=True)  # 仅 P0-B 开
    r = A._assemble_result(_ctx_no5118(), _judged_hi(), LLM_META, {}, 2.0, None)
    assert r["value_multiplier"] == 1.5                 # 放行 → 1+3×0.25 钳到价值上限
    assert r["guards"].get("value_gate_relaxed") is True
