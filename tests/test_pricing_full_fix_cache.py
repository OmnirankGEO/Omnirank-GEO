"""Stage 6 缓存/hardening 单测:recalculate_for_tier NaN 防护(v2.2 hardening②)。

llm 表 assessor_version 软失效是 SQL/migration 改动(纯 ADD COLUMN + WHERE),走部署 dry-run 验证。

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 python -m pytest tests/test_pricing_full_fix_cache.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.batch_pricing import recalculate_for_tier  # noqa: E402


def _v2_kw(true_comp, eff_comp=20):
    return {"keyword": "x", "effective_competition": eff_comp, "cost_per_article": 60,
            "pricing_formula_version": "v2.2_2026-06-11",
            "v2_assessor_data": {"true_competition": true_comp, "value_signal": 1.0}}


def test_recalc_nan_true_competition_no_crash():
    out = recalculate_for_tier([_v2_kw(float("nan"))], 0.20, markup_override=1.0)
    assert len(out) == 1 and out[0]["required_articles"] >= 7   # NaN → 回落 eff_comp · 不崩


def test_recalc_inf_true_competition_no_crash():
    out = recalculate_for_tier([_v2_kw(float("inf"))], 0.20, markup_override=1.0)
    assert len(out) == 1 and out[0]["required_articles"] >= 7


def test_recalc_normal_true_competition(monkeypatch):
    out = recalculate_for_tier([_v2_kw(40, eff_comp=10)], 0.20, markup_override=1.0)
    # true_comp=40 → standard ceil(0.20×40/0.80)=10 篇(>7 底线)
    assert out[0]["required_articles"] == 10


def test_recalc_none_true_competition_falls_back_eff():
    out = recalculate_for_tier([_v2_kw(None, eff_comp=8)], 0.20, markup_override=1.0)
    # None → eff_comp=8 → standard max(7, ceil(0.20×8/0.80)=2)=7
    assert out[0]["required_articles"] == 7
