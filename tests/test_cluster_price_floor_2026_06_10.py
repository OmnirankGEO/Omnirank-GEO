# -*- coding: utf-8 -*-
"""¥400 地板(audit P1 · 2026-06-10):cluster 模式单词三档价无 ¥400 地板(价直接来自出厂 band,markup=1 时 <400),
与 approve 终验闸(≥MIN_KEYWORD_PRICE)冲突 → 含县域/低竞争词的报价单整单发不出 + 与 flat 模式价不一致。
修复 = cluster `_compute_cluster_pricing` 取 p 套 `_floor_keyword_price`,对齐 flat(selection_api:1512/1517)。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_floor_lifts_below_min():
    """报价词低于地板 → 抬到地板(过 approve 终验闸·对齐 flat)。"""
    from tools.batch_pricing import _floor_keyword_price
    from tools.keyword_value_scorer import MIN_KEYWORD_PRICE
    assert _floor_keyword_price({"should_quote": True, "is_broad": False}, MIN_KEYWORD_PRICE - 100) == MIN_KEYWORD_PRICE
    # 公式引擎无 should_quote 字段 → 默认报价词,照样套地板
    assert _floor_keyword_price({}, MIN_KEYWORD_PRICE - 108) == MIN_KEYWORD_PRICE


def test_floor_keeps_above_min():
    """已高于地板的价不动(不误压高价词)。"""
    from tools.batch_pricing import _floor_keyword_price
    from tools.keyword_value_scorer import MIN_KEYWORD_PRICE
    assert _floor_keyword_price({}, MIN_KEYWORD_PRICE + 1000) == MIN_KEYWORD_PRICE + 1000


def test_floor_skips_informational():
    """信息型(should_quote=False)不套地板(终验闸豁免·不被抬到 400)。"""
    from tools.batch_pricing import _floor_keyword_price
    assert _floor_keyword_price({"should_quote": False}, 100) == 100
    assert _floor_keyword_price({"should_quote": False, "is_broad": True}, 50) == 50


def test_floor_national_uses_national_min():
    """全国词(is_broad)用 NATIONAL 地板,与 flat selection_api:1512 一致。"""
    from tools.batch_pricing import _floor_keyword_price
    from tools.keyword_value_scorer import MIN_KEYWORD_PRICE_NATIONAL
    assert _floor_keyword_price({"is_broad": True}, 1) == MIN_KEYWORD_PRICE_NATIONAL


def test_cluster_pricing_applies_floor_core_and_covered():
    """source-scan:_compute_cluster_pricing 的 core + covered 两处取 p 都经 _floor_keyword_price。"""
    bp = (ROOT / "tools" / "batch_pricing.py").read_text(encoding="utf-8")
    assert bp.count("_floor_keyword_price(scored, int(scored.get(price_field") == 2
    assert "def _floor_keyword_price(" in bp
    # 终验闸仍在(没被放松),cluster 改为产出达标价过闸
    sel = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    assert "低于最低价" in sel
