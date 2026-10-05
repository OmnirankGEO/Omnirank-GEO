"""Stage 2 · P0-A 成本接线单测:flag 全关=现状不变 / flag 开=snapshot×mhz+overhead /
overhead 在倍率外(correction 1)/ override 绝对优先 / 快照无数据回落+needs_review。

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 python -m pytest tests/test_pricing_full_fix_p0a.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.pricing_llm_assessor as A      # noqa: E402
import tools.llm_pricing_flag as F          # noqa: E402
import tools.media_cost_ssot as MCS         # noqa: E402

LLM_META = {"llm_deviation_pct": 0.0, "llm_used": "dual"}
BASELINE = {}


def _ctx(**kw):
    base = dict(keyword="测试词", measured_comp=12, saturated=False,
                metaso={"competition_count": 12, "content_count": 5},
                cost_multiplier=1.0, dynamic_cost=55.0,
                five118={"search_volume": 100}, metaso_fallback=False)
    base.update(kw)
    return base


def _judged(**kw):
    base = dict(true_competition=12, cost_per_article_suggested=55.0, keyword_type="local_city",
                city="深圳", city_tier="tier1", media_tier_required="A", value_signal=1.0,
                reasoning="x", risk_flags=[], confidence=0.8, national_unclamped=False)
    base.update(kw)
    return base


def _assemble(ctx, judged, markup=2.0, cost_override=None):
    return A._assemble_result(ctx, judged, LLM_META, BASELINE, markup, cost_override)


def test_p0a_off_uses_current_formula(monkeypatch):
    monkeypatch.setattr(F, "is_cost_snapshot_enabled", lambda: False)
    r = _assemble(_ctx(), _judged())
    assert r["cost_per_article"] == 55.0          # max(dynamic55, suggested55)×1.0
    assert "cost_snapshot" not in r["guards"]


def test_p0a_on_snapshot_plus_overhead(monkeypatch):
    monkeypatch.setattr(F, "is_cost_snapshot_enabled", lambda: True)
    monkeypatch.setattr(A, "_snapshot_media_factory_cost", lambda tier: (150.0, {"needs_review": False}))
    monkeypatch.setattr(A, "_article_overhead", lambda: 30.0)
    r = _assemble(_ctx(cost_multiplier=1.0), _judged(media_tier_required="A"))
    assert r["cost_per_article"] == 180.0         # 150×1.0 + 30
    assert r["guards"]["cost_snapshot"]["src"] == "snapshot"


def test_p0a_overhead_outside_multiplier(monkeypatch):
    """correction 1:overhead 必须在上级倍率之外平价加。"""
    monkeypatch.setattr(F, "is_cost_snapshot_enabled", lambda: True)
    monkeypatch.setattr(A, "_snapshot_media_factory_cost", lambda tier: (150.0, {"needs_review": False}))
    monkeypatch.setattr(A, "_article_overhead", lambda: 30.0)
    r = _assemble(_ctx(cost_multiplier=2.0), _judged())
    assert r["cost_per_article"] == 330.0         # 150×2 + 30(NOT (150+30)×2=360)


def test_p0a_override_absolute_priority(monkeypatch):
    monkeypatch.setattr(F, "is_cost_snapshot_enabled", lambda: True)
    monkeypatch.setattr(A, "_snapshot_media_factory_cost", lambda tier: (150.0, {}))
    r = _assemble(_ctx(), _judged(), cost_override=88.0)
    assert r["cost_per_article"] == 88.0          # 自设绝对优先·不走 snapshot/倍率


def test_p0a_no_data_fallback_needs_review(monkeypatch):
    monkeypatch.setattr(F, "is_cost_snapshot_enabled", lambda: True)
    monkeypatch.setattr(A, "_snapshot_media_factory_cost", lambda tier: (None, {"confidence": "no_data"}))
    r = _assemble(_ctx(), _judged())
    assert r["cost_per_article"] == 55.0          # 回落现状公式·绝不产出低价
    assert r["needs_review"] is True
    assert r["guards"]["cost_snapshot"]["src"] == "snapshot_no_data_fallback"


def test_snapshot_media_factory_composition(monkeypatch):
    class FakeSnap:
        def media_tier_cost(self, tier):
            return {"cost": 100.0, "needs_review": False}
    monkeypatch.setattr(MCS, "load_media_cost_snapshot", lambda *a, **k: FakeSnap())
    monkeypatch.setattr(A, "_mhz_markup_ratio", lambda: 1.5)
    mf, meta = A._snapshot_media_factory_cost("A")
    assert mf == 150.0                            # 100 × 1.5(real × 代发 markup)


def test_snapshot_no_data_returns_none(monkeypatch):
    class FakeSnap:
        def media_tier_cost(self, tier):
            return {"cost": None, "needs_review": True, "confidence": "no_data"}
    monkeypatch.setattr(MCS, "load_media_cost_snapshot", lambda *a, **k: FakeSnap())
    mf, meta = A._snapshot_media_factory_cost("A")
    assert mf is None and meta.get("confidence") == "no_data"
