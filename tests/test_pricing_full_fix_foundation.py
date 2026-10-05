"""完整修复 Stage 1 基础单测:3 flag 默认关 / 护栏 config / 累计媒体倍率(correction 3)。

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
      python -m pytest tests/test_pricing_full_fix_foundation.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import llm_pricing_flag as F           # noqa: E402
from config import pricing_config as PC            # noqa: E402
from services import quote_pricing_preferences as QPP  # noqa: E402


# ---- 3 flag 默认关(=现状 0 变化)----
def test_flags_default_false(monkeypatch):
    monkeypatch.setattr(F, "_get_setting", lambda k, d="": d)  # 强制取默认值
    assert F.is_cost_snapshot_enabled() is False
    assert F.is_value_evidence_gate_relaxed() is False
    assert F.is_national_unclamped_enabled() is False


def test_flag_on_when_setting_true(monkeypatch):
    monkeypatch.setattr(F, "_get_setting", lambda k, d="": "true")
    assert F.is_cost_snapshot_enabled() is True


# ---- 护栏 / overhead config 默认(老板审核取值)----
def test_guard_config_defaults():
    g = PC.get_blowup_guard_config()
    assert g["lever_two"] == 1.6 and g["lever_three"] == 1.4
    assert g["factory_ceiling_niche"] == 6000.0 and g["factory_ceiling_local"] == 3000.0
    assert g["entry_selling_ceiling_niche"] == 12000.0 and g["entry_selling_ceiling_local"] == 8000.0
    assert g["quote_total_ceiling"] == 25000.0 and g["quote_ratio"] == 3.0
    assert g["no_cache_selling"] == 8000.0 and g["national_unclamped_factory_ratio"] == 2.5
    assert PC.get_article_overhead_yuan() == 50.0
    assert PC.get_value_mult_max() == 1.5


# ---- correction 3:累计媒体倍率 ----
def test_cumulative_no_owner(monkeypatch):
    monkeypatch.setattr(QPP, "resolve_owning_agent", lambda uid: None)
    assert QPP.get_cumulative_media_procurement_multiplier(123, max_depth=8) == 1.0


def test_cumulative_single_level_equals_direct(monkeypatch):
    # 1 级链 → 退化 == 直属上级倍率(与旧 get_procurement_cost_multiplier 等价)
    monkeypatch.setattr(QPP, "resolve_owning_agent", lambda uid: 99 if uid == 123 else None)
    monkeypatch.setattr(QPP, "_sku_markup_of_agent", lambda a: 1.5 if a == 99 else 1.0)
    assert QPP.get_cumulative_media_procurement_multiplier(123, max_depth=8) == 1.5


def test_cumulative_multi_level_accumulates(monkeypatch):
    chain = {123: 99, 99: 50, 50: None}
    sku = {99: 1.5, 50: 1.2}
    monkeypatch.setattr(QPP, "resolve_owning_agent", lambda uid: chain.get(uid))
    monkeypatch.setattr(QPP, "_sku_markup_of_agent", lambda a: sku.get(a, 1.0))
    assert QPP.get_cumulative_media_procurement_multiplier(123, max_depth=8) == round(1.5 * 1.2, 4)


def test_cumulative_cycle_guard(monkeypatch):
    chain = {123: 99, 99: 123}  # 环
    monkeypatch.setattr(QPP, "resolve_owning_agent", lambda uid: chain.get(uid))
    monkeypatch.setattr(QPP, "_sku_markup_of_agent", lambda a: 2.0)
    # 123→99(×2),99→123 已访问 → 停。不无限循环。
    assert QPP.get_cumulative_media_procurement_multiplier(123, max_depth=8) == 2.0


def test_cumulative_depth_limit(monkeypatch):
    # 无限上溯链 · depth_limit=2 → 只累 2 层
    monkeypatch.setattr(QPP, "resolve_owning_agent", lambda uid: uid + 1)
    monkeypatch.setattr(QPP, "_sku_markup_of_agent", lambda a: 2.0)
    assert QPP.get_cumulative_media_procurement_multiplier(1, max_depth=2) == 4.0  # 2×2
