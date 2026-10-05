"""super_red_ocean 误伤长句搜索项修复 · 单测(2026-06-16)。

老板修:yellow(占比≥0.90 但竞争未接近物理上限)不再 super_red_ocean=true、不剥离套餐、不深度报价。
hard red = ratio≥0.90 且 (competition_count≥90 或 effective_competition≥90)。
真实样本「深圳小户型法式装修公司推荐」:comp=22/content=100/effective=52 → ratio=0.22 → 正常报价。

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
       python -m pytest tests/test_super_red_ocean_yellow_fix_2026_06_16.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.pricing_bands as B          # noqa: E402
import tools.pricing_llm_assessor as A   # noqa: E402
import tools.llm_pricing_flag as F       # noqa: E402

D = B.detect_super_red_ocean


# ============================================================
# 1. detect_super_red_ocean 纯函数:yellow 不剥离 / 只 hard red 剥离
# ============================================================

def test_real_sample_french_decor_not_super_red():
    # 真实样本:comp=22, content=100, effective=52 → ratio 0.22 → 连 yellow 都不是 → 正常报价
    r = D(22, 100, effective_competition=52)
    assert r["super_red_ocean"] is False
    assert r["super_red_ocean_level"] == "none"
    assert r["competition_ratio"] == 0.22


def test_ratio_below_threshold_not_super_red():
    assert D(31, 60)["super_red_ocean"] is False            # ratio 0.517
    assert D(31, 60)["super_red_ocean_level"] == "none"
    # content=100 命中上限但占比低(52/100)→ 不红(占比才是红海本质)
    assert D(52, 100)["super_red_ocean"] is False


def test_yellow_high_ratio_no_hard_signal_not_super_red():
    # 占比达标但竞争未接近上限 → yellow · super_red_ocean=False(不剥离)
    for comp, content in [(10, 10), (18, 20), (54, 60)]:
        r = D(comp, content)
        assert r["super_red_ocean"] is False, f"{comp}/{content} 不应剥离套餐"
        assert r["super_red_ocean_level"] == "yellow", f"{comp}/{content} 应为 yellow 内部观测"


def test_hard_red_by_competition_count():
    # 占比达标 且 竞品数≥90 → hard red
    r = D(90, 100)
    assert r["super_red_ocean"] is True and r["super_red_ocean_level"] == "red"
    r2 = D(95, 95)
    assert r2["super_red_ocean"] is True and r2["super_red_ocean_level"] == "red"


def test_hard_red_by_effective_competition():
    # 占比达标 且 有效竞争≥90(竞品数本身<90)→ hard red
    r = D(50, 50, effective_competition=90)
    assert r["super_red_ocean"] is True and r["super_red_ocean_level"] == "red"
    # effective<90 且 comp<90 → 仍 yellow 不剥离
    assert D(50, 50, effective_competition=52)["super_red_ocean"] is False


def test_small_sample_and_empty_guard():
    # 小样本(content<10)不判
    assert D(9, 9)["super_red_ocean"] is False
    assert D(9, 9)["super_red_ocean_level"] == "none"
    # 空 content → none
    assert D(50, 0)["super_red_ocean"] is False


def test_competition_ratio_preserved():
    # ratio 观测字段保留
    assert D(90, 100)["competition_ratio"] == 0.9
    assert D(22, 100, effective_competition=52)["competition_ratio"] == 0.22


# ============================================================
# 2. _assemble_result 级:yellow 搜索项仍有价格 / 不进 super_red_ocean / 不剥离
# ============================================================
LLM_META = {"llm_deviation_pct": 0.0, "llm_used": "dual"}
_F5 = {"search_volume": 800, "sem_price": 12.0, "bidword_company_count": 40}


def _ctx(metaso=None, comp=18, **kw):
    base = dict(keyword="深圳小户型法式装修公司推荐", measured_comp=comp, saturated=False,
                metaso=(metaso or {"competition_count": comp, "content_count": 20,
                                    "effective_competition": comp}),
                cost_multiplier=1.0, dynamic_cost=90.0, five118=_F5, metaso_fallback=False)
    base.update(kw)
    return base


def _judged(comp=18, value_signal=2.0, **kw):
    base = dict(true_competition=comp, cost_per_article_suggested=90.0, keyword_type="local_city",
                city="深圳", city_tier="tier1", media_tier_required="B", value_signal=value_signal,
                reasoning="x", risk_flags=[], confidence=0.8, national_unclamped=False)
    base.update(kw)
    return base


def _flags(mp):
    mp.setattr(F, "is_cost_snapshot_enabled", lambda: False)
    mp.setattr(F, "is_value_evidence_gate_relaxed", lambda: False)
    mp.setattr(F, "is_national_unclamped_enabled", lambda: False)
    mp.setattr(F, "is_trust_asset_enabled", lambda: False)


def test_assemble_yellow_keeps_prices_not_stripped(monkeypatch):
    # 高占比但非 hard red(comp 18 / content 20 / effective 18)→ yellow → super_red_ocean 不进 risk_flags
    _flags(monkeypatch)
    r = A._assemble_result(
        _ctx(metaso={"competition_count": 18, "content_count": 20, "effective_competition": 18}),
        _judged(), LLM_META, {}, 2.0, None)
    assert "super_red_ocean" not in r["risk_flags"]            # 不标超红海
    assert r["entry_price"] > 0 and r["standard_price"] > 0 and r["flagship_price"] > 0
    assert r["standard_articles"] >= 1                          # 不再标准版 0 篇


def test_assemble_real_sample_normal_quote(monkeypatch):
    # 「深圳小户型法式装修公司推荐」真实数据 ratio 0.22 → 正常出三档价
    _flags(monkeypatch)
    r = A._assemble_result(
        _ctx(metaso={"competition_count": 22, "content_count": 100, "effective_competition": 52}),
        _judged(), LLM_META, {}, 2.0, None)
    assert "super_red_ocean" not in r["risk_flags"]
    assert r["entry_price"] > 0 and r["standard_price"] > 0 and r["flagship_price"] > 0


def test_assemble_hard_red_still_stripped(monkeypatch):
    # 对照:hard red(comp 95 / content 100)→ 仍标 super_red_ocean(剥离 + 需深度报价)
    _flags(monkeypatch)
    r = A._assemble_result(
        _ctx(metaso={"competition_count": 95, "content_count": 100, "effective_competition": 95}),
        _judged(comp=95), LLM_META, {}, 2.0, None)
    assert "super_red_ocean" in r["risk_flags"]
    assert r["needs_review"] is True
