"""判别测试 · 媒体平衡推荐灰度闸（Owner 2026-07-29「先灰度」）。

两条硬要求：
1. **默认关**，且 settings 读不到时也关（灰度期 fail-closed）；
2. **关掉后推荐口径逐位回到上线前** —— 分组走老 `_GENERIC_PLATFORMS` 名单、
   评分不乘成功率、不返回两块新面板数据。
"""
from __future__ import annotations

import types

import pytest

from services.media_balance_gate import (
    LEGACY_GENERIC_PLATFORMS,
    is_media_balance_enabled,
)
from services.placement_service import _is_trunk_platform, _quality_score_v2f


def _settings(**kwargs):
    return types.SimpleNamespace(**kwargs)


def _patch_settings(monkeypatch, settings_obj):
    import config.settings_manager as sm

    monkeypatch.setattr(sm, "load_settings", lambda: settings_obj, raising=False)


# ---------------------------------------------------------------------------
# 闸本身
# ---------------------------------------------------------------------------
def test_default_is_off(monkeypatch):
    """未配置任何字段 → 关（保守）。"""
    _patch_settings(monkeypatch, _settings())
    assert is_media_balance_enabled(user_id=1, brand_id=1) is False


def test_explicit_false_without_whitelist_is_off(monkeypatch):
    _patch_settings(monkeypatch, _settings(
        media_balance_enabled=False, media_balance_whitelist={"user_ids": [], "brand_ids": []}))
    assert is_media_balance_enabled(user_id=42, brand_id=7) is False


def test_global_on(monkeypatch):
    _patch_settings(monkeypatch, _settings(media_balance_enabled=True))
    assert is_media_balance_enabled() is True
    assert is_media_balance_enabled(user_id=999) is True


def test_whitelist_by_user_and_brand(monkeypatch):
    _patch_settings(monkeypatch, _settings(
        media_balance_enabled=False,
        media_balance_whitelist={"user_ids": [42], "brand_ids": [7]}))
    assert is_media_balance_enabled(user_id=42) is True
    assert is_media_balance_enabled(brand_id=7) is True
    assert is_media_balance_enabled(user_id=43, brand_id=8) is False


def test_settings_unavailable_fails_closed(monkeypatch):
    """settings 炸了不能顺势放行 —— 灰度期宁可少给，不可多给。"""
    import config.settings_manager as sm

    def _boom():
        raise RuntimeError("settings backend down")

    monkeypatch.setattr(sm, "load_settings", _boom, raising=False)
    assert is_media_balance_enabled(user_id=42, brand_id=7) is False


def test_malformed_whitelist_is_off(monkeypatch):
    _patch_settings(monkeypatch, _settings(
        media_balance_enabled=False, media_balance_whitelist="not-a-dict"))
    assert is_media_balance_enabled(user_id=42) is False


# ---------------------------------------------------------------------------
# 关掉后必须逐位回到上线前
# ---------------------------------------------------------------------------
def test_legacy_list_is_the_pre_launch_one():
    """老名单必须逐字保留，否则谈不上『回到上线前』。"""
    assert LEGACY_GENERIC_PLATFORMS == frozenset({
        '知乎', '搜狐', '今日头条', '抖音', '百度', '新浪网', '手机搜狐网',
        '微信公众平台', '新浪', '腾讯', '网易', '凤凰', '微博',
    })


@pytest.mark.parametrize("name", sorted(LEGACY_GENERIC_PLATFORMS))
def test_gray_off_grouping_matches_legacy_membership(name):
    assert _is_trunk_platform(name, media_balance_enabled=False) is True


def test_gray_off_restores_old_vertical_classification():
    """博客园/CSDN 在新口径是主干、老口径是垂类 —— 关掉必须变回垂类。"""
    for name in ("博客园", "CSDN", "央视频官方"):
        assert _is_trunk_platform(name, media_balance_enabled=True) is True, name
        assert _is_trunk_platform(name, media_balance_enabled=False) is False, name


def test_gray_off_scoring_is_bit_identical():
    """关掉时不传 success_factor → 评分公式与上线前逐位相同。"""
    row = {"price": 45, "authority_media": 1, "geo_rank": 3,
           "portal_media": "其他门户", "geo_rank_platform": "豆包,DeepSeek"}
    legacy = _quality_score_v2f(row, citation_strength=0.7)
    assert _quality_score_v2f(row, citation_strength=0.7, success_factor=None) == legacy
    # 开启后才会乘因子
    assert _quality_score_v2f(row, citation_strength=0.7, success_factor=0.55) < legacy


def test_strict_profile_and_presubmit_are_not_gated():
    """Owner 明示不灰度的两项：严审档 v2 与下单前预审必须始终在线。"""
    import services.media_balance_gate as gate
    from services.strict_media_presubmit import is_strict_review_media
    from writing.platform_safety_profiles import review_for_platform

    src = gate.__doc__ or ""
    assert "严审" in src and "预审" in src  # 文档明确写了不受闸影响

    # 闸关着也照拦：合规硬门跟灰度无关
    assert is_strict_review_media(media_name="搜狐网新闻（官方）") is True
    verdict = review_for_platform(
        title="深圳最好的装修公司", content="正文" * 500,
        profile="strict_media_v2", domain="sohu.com",
    )
    codes = [f["code"] for f in verdict.hard_failures]
    assert "ad_law_absolute_term_in_title" in codes, codes
