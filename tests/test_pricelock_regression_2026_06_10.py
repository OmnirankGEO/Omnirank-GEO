# -*- coding: utf-8 -*-
"""价格锁回归(audit P1 · 2026-06-10):定价权批 P1-1 把 markup_override 改为恒传实际值(防回退 settings),
致引擎 `markup_override is None` 写缓存判据恒 False、共享 7 天价格锁(keyword_price_cache)主路径全面停写。
修复 = 解耦 allow_cache_write 开关。本测试锁住:默认口径写 / per-agent 不写 / 自设成本不写 / 向后兼容 / 三主路径传参。"""
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def test_should_write_default_kou_jing_writes():
    """回归核心:默认口径(allow=True · markup 已是实际值 1.0 非 None)→ 写。旧 `markup is None` 判据会漏写。"""
    from tools.batch_pricing import _should_write_shared_cache
    assert _should_write_shared_cache(1.0, None, True) is True
    assert _should_write_shared_cache(None, None, True) is True


def test_should_write_per_agent_not_write():
    """per-agent 自设系数(allow=False)→ 不写(成品价快照不污染共享缓存)。"""
    from tools.batch_pricing import _should_write_shared_cache
    assert _should_write_shared_cache(1.5, None, False) is False
    assert _should_write_shared_cache(2.0, None, False) is False


def test_should_write_self_cost_never_writes():
    """自设单篇成本(底盘字段)→ 任何情况都不写(双保险防污染共享 cost)。"""
    from tools.batch_pricing import _should_write_shared_cache
    assert _should_write_shared_cache(1.0, 350.0, True) is False
    assert _should_write_shared_cache(None, 350.0, None) is False
    assert _should_write_shared_cache(1.5, 60.0, False) is False


def test_should_write_backward_compat_none():
    """allow_cache_write=None(旧调用方)→ 退回旧判据 markup_override is None(向后兼容)。"""
    from tools.batch_pricing import _should_write_shared_cache
    assert _should_write_shared_cache(None, None, None) is True   # 旧默认口径
    assert _should_write_shared_cache(2.0, None, None) is False   # 旧 per-agent
    # ⚠️ 旧逻辑下 markup=1.0(实际值非 None)恒不写 = 正是被 P1-1 触发的回归(故新调用方必须显式传 allow)
    assert _should_write_shared_cache(1.0, None, None) is False


def test_is_default_quote_pricing():
    """默认口径判定:本人有效系数==平台默认(override 版返 None)且无自设成本 → True。"""
    import services.quote_pricing_preferences as qpp
    with patch.object(qpp, "get_quote_markup_override_for_quote_viewer", return_value=None), \
         patch.object(qpp, "get_cost_per_article_for_quote_viewer", return_value=None):
        assert qpp.is_default_quote_pricing(123) is True
    with patch.object(qpp, "get_quote_markup_override_for_quote_viewer", return_value=1.5), \
         patch.object(qpp, "get_cost_per_article_for_quote_viewer", return_value=None):
        assert qpp.is_default_quote_pricing(123) is False
    with patch.object(qpp, "get_quote_markup_override_for_quote_viewer", return_value=None), \
         patch.object(qpp, "get_cost_per_article_for_quote_viewer", return_value=350.0):
        assert qpp.is_default_quote_pricing(123) is False


def test_callsites_pass_allow_cache_write():
    """source-scan:flat/cluster/诊断三条主报价路径都传 allow_cache_write,防回归被改回。"""
    sel = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    srv = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "quote_allow_cache = is_default_quote_pricing(user_id)" in sel
    # flat 调用 generate_batch_quote + cluster 分支调用 _generate_quote_cluster_mode 各传一次
    assert sel.count("allow_cache_write=quote_allow_cache") >= 2
    # cluster 子函数 → generate_cluster_quote 透传参数
    assert "allow_cache_write=allow_cache_write" in sel
    assert "quote_allow_cache = is_default_quote_pricing(user_id)" in srv
    assert "allow_cache_write=quote_allow_cache" in srv


def test_engine_uses_helper_not_inline():
    """引擎两处写缓存判据都走纯函数(DRY),不再用恒 False 的 markup_override is None 内联。"""
    bp = (ROOT / "tools" / "batch_pricing.py").read_text(encoding="utf-8")
    assert bp.count("_can_write_cache = _should_write_shared_cache(") == 2
    assert "def _should_write_shared_cache(" in bp
