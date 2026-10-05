"""v2.7.1 GEO 文体改造 · SSOT 配置守护单测

覆盖:
- content_ratios 8 key sum=100(percent) / sum=1.0(fraction)
- style_ratios 11 key sum=100 / sum=1.0
- industry_overrides 医疗 / 法律 sum=100 + authority/ranking 强制 0
- company_profile 不在 content/style_ratios
- get_effective_*_ratios keyword-only unit 必填(漏传 TypeError)
"""
import math
import os
import sys

# 项目根路径
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from config.settings_manager import (
    SystemSettings,
    get_effective_content_ratios,
    get_effective_style_ratios,
)


def test_content_ratios_default_sum_100_percent():
    s = SystemSettings()
    total = sum(s.content_ratios.values())
    assert total == 100, f"content_ratios sum={total} != 100"


def test_style_ratios_default_sum_100_percent():
    s = SystemSettings()
    total = sum(s.style_ratios.values())
    assert total == 100, f"style_ratios sum={total} != 100"


def test_industry_overrides_medical_sum_100():
    s = SystemSettings()
    med = s.industry_overrides["医疗健康"]
    assert sum(med["content_ratios"].values()) == 100
    assert sum(med["style_ratios"].values()) == 100


def test_industry_overrides_legal_sum_100():
    s = SystemSettings()
    leg = s.industry_overrides["法律商务"]
    assert sum(leg["content_ratios"].values()) == 100
    assert sum(leg["style_ratios"].values()) == 100


def test_medical_authority_zero():
    """医疗 authority + ranking_v2 + authority_ranking 强制 0"""
    s = SystemSettings()
    med = s.industry_overrides["医疗健康"]
    assert med["content_ratios"].get("authority") == 0
    assert med["style_ratios"].get("ranking_v2") == 0
    assert med["style_ratios"].get("authority_ranking") == 0


def test_legal_authority_zero():
    s = SystemSettings()
    leg = s.industry_overrides["法律商务"]
    assert leg["content_ratios"].get("authority") == 0
    assert leg["style_ratios"].get("ranking_v2") == 0
    assert leg["style_ratios"].get("authority_ranking") == 0


def test_company_profile_not_in_content_ratios():
    """company_profile 永远不进 content_ratios(走 fixed_count=1)"""
    s = SystemSettings()
    assert "company_profile" not in s.content_ratios


def test_company_profile_not_in_style_ratios():
    """company_profile 永远不进 style_ratios(走 fixed_count=1)"""
    s = SystemSettings()
    assert "company_profile" not in s.style_ratios


def test_get_effective_content_ratios_fraction_sum_1():
    """fraction unit 必须 sum 严格 1.0(允许 0.001 tol)"""
    ratios = get_effective_content_ratios(unit="fraction")
    total = sum(ratios.values())
    assert math.isclose(total, 1.0, abs_tol=0.001), f"fraction sum={total} != 1.0"


def test_get_effective_style_ratios_fraction_sum_1():
    ratios = get_effective_style_ratios(unit="fraction")
    total = sum(ratios.values())
    assert math.isclose(total, 1.0, abs_tol=0.001), f"style fraction sum={total} != 1.0"


def test_get_effective_content_ratios_unit_required_typeerror():
    """v2.3 阻断点 #2:unit 必填 · 漏传 → TypeError"""
    with pytest.raises(TypeError):
        get_effective_content_ratios()  # 漏传 unit


def test_get_effective_style_ratios_unit_required_typeerror():
    with pytest.raises(TypeError):
        get_effective_style_ratios()


def test_get_effective_content_ratios_positional_unit_typeerror():
    """unit 必须 keyword-only · positional 不允许"""
    with pytest.raises(TypeError):
        # 试图 positional 传 unit · 应 TypeError(keyword-only barrier)
        get_effective_content_ratios("医疗健康", "fraction")  # type: ignore[arg-type,misc]


def test_get_effective_content_ratios_industry_override():
    """医疗 / 法律 industry 命中走 override · authority=0"""
    ratios = get_effective_content_ratios("医疗健康", unit="percent")
    assert ratios.get("authority") == 0


def test_get_effective_style_ratios_industry_override():
    ratios = get_effective_style_ratios("医疗健康", unit="percent")
    assert ratios.get("ranking_v2") == 0
    assert ratios.get("authority_ranking") == 0


def test_get_effective_content_ratios_unknown_industry_falls_back_default():
    """未知行业也必须经过 evidence-first 运行时归一。"""
    ratios = get_effective_content_ratios("未知行业", unit="percent")
    assert ratios.get("authority") == 0
    assert sum(ratios.values()) == 100


def test_style_ratios_4_new_styles_present():
    """v2.7.1 4 新 style 必存在"""
    s = SystemSettings()
    for new_style in ["comparison_review", "risk_compliance", "price_roi", "data_report"]:
        assert new_style in s.style_ratios, f"{new_style} 不在 style_ratios"
        assert s.style_ratios[new_style] > 0, f"{new_style} ratio=0(应 > 0)"
