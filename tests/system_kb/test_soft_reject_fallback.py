"""
tests/system_kb/test_soft_reject_fallback.py

验证 _should_soft_reject 纯函数逻辑：
  - 有 page_card 时一律不软拒（保底）
  - 无 page_card 时按分数/业务关键词判定
"""

from api.xiaobang_api import _should_soft_reject


def test_soft_reject_when_low_score_no_pagecard():
    assert _should_soft_reject([], 0.0, False, has_page_card=False) is True


def test_no_soft_reject_when_pagecard_present():
    # 有页面卡 → 即使无结果、低分也不软拒（保底）
    assert _should_soft_reject([], 0.0, False, has_page_card=True) is False


def test_soft_reject_low_score_biz_kw_no_pagecard():
    assert _should_soft_reject([("x", 0.5)], 0.5, False, has_page_card=False) is True
