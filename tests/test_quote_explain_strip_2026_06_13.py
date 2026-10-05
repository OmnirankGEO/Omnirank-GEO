"""报价解释层一期 · 客户面脱敏边界测试(2026-06-13)。

guarantee_unavailable 是内部护栏信号(价超天花板/放飞)→ 必须从客户面出口剥除(代理端仍可见)。
super_red_ocean / should_quote 是有意保留给客户的状态标(需深度报价 / 信息型不报价)→ 不剥。

提取真实 _CUSTOMER_INTERNAL_KW_FIELDS + _strip_internal_pricing_fields(selection_api import 会 eager 连库 · 用 regex/exec 提取)。

跑法:ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 python -m pytest tests/test_quote_explain_strip_2026_06_13.py -q
"""
import os
import re
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_real_strip():
    """提取真实黑名单元组 + 脱敏函数(避免 import selection_api 触发连库)。"""
    sel = open(os.path.join(ROOT, "api", "selection_api.py"), encoding="utf-8").read()
    mt = re.search(r"(_CUSTOMER_INTERNAL_KW_FIELDS\s*=\s*\([\s\S]*?\n\))", sel)
    assert mt, "未定位 _CUSTOMER_INTERNAL_KW_FIELDS 元组"
    mf = re.search(r"(def _strip_internal_pricing_fields\(payload\)[\s\S]*?)\n\ndef ", sel)
    assert mf, "未定位 _strip_internal_pricing_fields"
    ns: dict = {}
    exec(textwrap.dedent(mt.group(1)), ns)
    exec(textwrap.dedent(mf.group(1)), ns)
    return ns["_CUSTOMER_INTERNAL_KW_FIELDS"], ns["_strip_internal_pricing_fields"]


_BLACKLIST, _STRIP = _load_real_strip()


def test_guarantee_unavailable_in_blacklist():
    assert "guarantee_unavailable" in _BLACKLIST   # 内部护栏信号 · 客户面必剥


def test_super_red_ocean_kept_for_client():
    # 市场事实状态标 · 有意保留给客户(需深度报价)· 不剥
    assert "super_red_ocean" not in _BLACKLIST
    assert "super_red_ocean_level" not in _BLACKLIST
    assert "should_quote" not in _BLACKLIST           # 信息型不报价 · 客户可见 · 不剥


def test_strip_removes_guarantee_unavailable_keeps_super_red_ocean():
    payload = {
        "tiers": {},
        "keywords": [{
            "keyword": "测试词", "guarantee_unavailable": True, "super_red_ocean": True,
            "should_quote": False, "cost_per_article": 55, "effective_competition": 80,
            "entry": {"price": 0, "articles": 1}, "standard": {"price": 0, "articles": 2},
        }],
    }
    _STRIP(payload)
    kw = payload["keywords"][0]
    assert "guarantee_unavailable" not in kw          # 已剥(内部信号不泄露)
    assert "cost_per_article" not in kw                # 原有脱敏不回退
    assert "effective_competition" not in kw
    assert kw.get("super_red_ocean") is True           # 市场事实标保留(客户需看)
    assert kw.get("should_quote") is False             # 信息型标保留
    assert kw["standard"]["price"] == 0                # 三档价(客户渲染用)保留
