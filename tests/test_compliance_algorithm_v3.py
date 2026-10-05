"""
v3 公平算法单元测试 (CTO-15.23 2026-05-13)

验证 _compute_effective_rate_v3 算法行为符合老板诉求:
- 不"永远还债"(打到了立刻承认)
- 慢掉保护(达标后短期波动不立即跳掉)
- 懒政暴露(连续 7 天不达标切瞬时)
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from db.monitoring_db import _compute_effective_rate_v3


def _make_cursor(avg_rate, days, recent_is_compliant_desc):
    """
    构造一个 mock cursor · 按 v3 函数调用顺序返回结果:
      第 1 次 execute (AVG/COUNT) → fetchone() 返 {"avg_rate": avg_rate, "days": days}
      第 2 次 execute (历史 is_compliant) → fetchall() 返 list of {"check_date": ..., "is_compliant": ...}

    recent_is_compliant_desc: list[bool],从最近到最远(DESC 顺序)
    """
    cur = MagicMock()

    # 准备 fetchall 结果
    fetchall_rows = [
        {"check_date": datetime.date.today() - datetime.timedelta(days=i + 1),
         "is_compliant": v}
        for i, v in enumerate(recent_is_compliant_desc)
    ]

    # [2026-05-29 修 pre-existing] v1.7 给 _compute_effective_rate_v3 加了 _get_effective_window 兜底
    # (未传 effective_start/end 时多调一次 fetchone)· 旧 mock 缺这条 → StopIteration。
    # 补一条宽窗口 fetchone(service_start 远早 / service_days 大)· 不影响下游 canned avg/consec 断言。
    fetchone_calls = iter([
        {"paid_start": None,
         "service_start": datetime.date.today() - datetime.timedelta(days=365),
         "service_days": 730, "kms_start": None},
        {"avg_rate": avg_rate, "days": days},
    ])
    fetchall_calls = iter([fetchall_rows])

    cur.fetchone = lambda: next(fetchone_calls)
    cur.fetchall = lambda: next(fetchall_calls)
    cur.execute = MagicMock()
    return cur


def test_no_history_uses_detection_rate():
    """无历史(首次监测) → effective = detection_rate"""
    cur = _make_cursor(avg_rate=None, days=0, recent_is_compliant_desc=[])
    eff, hist_days, consec = _compute_effective_rate_v3(
        cur, kw_id=1, kw_source="confirmed", quote_id=1, detection_rate=75.0,
    )
    assert eff == 75.0
    assert hist_days == 0


def test_boss_scenario_A_slow_drop():
    """
    老板情景 A:第 1 天 100% · 第 2 天 0% → 第 2 天显示 50%(慢掉)

    模拟第 2 天 compute_compliance 跑时:
      历史: [Day1: detection=100, is_compliant=TRUE]
      今日 detection = 0
    """
    cur = _make_cursor(
        avg_rate=100.0, days=1,
        recent_is_compliant_desc=[True],  # 昨天 (Day1) 达标过
    )
    eff, hist_days, consec = _compute_effective_rate_v3(
        cur, kw_id=1, kw_source="confirmed", quote_id=1, detection_rate=0.0,
    )
    # rolling = (100×1 + 0)/2 = 50, max(0, 50) = 50
    assert eff == 50.0
    assert hist_days == 1
    assert consec == 0  # 昨天 is_compliant=True · 不连续不达标


def test_boss_scenario_B_no_debt():
    """
    老板情景 B:50/0/50/50 → 第 3-4 天显示 50%(打到了立刻承认 · 不还债)

    模拟第 4 天 compute_compliance:
      历史 [Day1=50 ✓, Day2=0 ✗, Day3=50 ✓]
      今日 detection = 50
    """
    cur = _make_cursor(
        avg_rate=(50 + 0 + 50) / 3,  # = 33.33
        days=3,
        recent_is_compliant_desc=[True, False, True],  # DESC: Day3 ✓, Day2 ✗, Day1 ✓
    )
    eff, hist_days, consec = _compute_effective_rate_v3(
        cur, kw_id=1, kw_source="confirmed", quote_id=1, detection_rate=50.0,
    )
    # rolling = (33.33×3 + 50)/4 = 37.5
    # max(50, 37.5) = 50 ← 关键:打到了立刻承认 · 不被历史拖累
    assert eff == 50.0
    assert consec == 0  # 最近一天(Day3) 达标 · 不连续不达标


def test_boss_screenshot_scenario_immediate_recovery():
    """
    老板 2026-05-13 截图:[100,0,0,0,0,0,0] + 今日 100% → 应立刻显示 100%

    v2 算法会算成 32.7%(全期累计平均拖累)
    v3 算法应该立刻 100%(max 保护)
    """
    cur = _make_cursor(
        avg_rate=(100 + 0 * 6) / 7,  # = 14.29
        days=7,
        recent_is_compliant_desc=[False, False, False, False, False, False, True],
        # DESC: 最近 6 天全 ✗, 第 7 天 ✓
    )
    eff, hist_days, consec = _compute_effective_rate_v3(
        cur, kw_id=1, kw_source="confirmed", quote_id=1, detection_rate=100.0,
    )
    # rolling = (14.29×7 + 100)/8 = 25
    # max(100, 25) = 100 ← 立刻达标!
    assert eff == 100.0
    assert consec == 6  # 最近 6 天连续不达标 · 还没到 7 天阈值


def test_lazy_mode_after_7_consecutive_below():
    """连续 7 天 is_compliant=FALSE → 切瞬时 detection_rate(暴露懒政)"""
    cur = _make_cursor(
        avg_rate=50.0,  # 历史平均还行
        days=7,
        recent_is_compliant_desc=[False] * 7,  # 全 ✗
    )
    eff, hist_days, consec = _compute_effective_rate_v3(
        cur, kw_id=1, kw_source="confirmed", quote_id=1, detection_rate=20.0,
        lazy_threshold_days=7,
    )
    # 连续 7 天不达标 → 切瞬时 = 20%
    # 不走 max 保护(否则 max(20, rolling) 会 > 20)
    assert eff == 20.0
    assert consec == 7


def test_lazy_mode_recovery_resets_consec():
    """懒政模式中 · 今天打到了 · effective 应立刻反映(不被锁死)"""
    cur = _make_cursor(
        avg_rate=0.0,  # 历史平均 0
        days=7,
        recent_is_compliant_desc=[False] * 7,  # 连续 7 天不达标
    )
    eff, hist_days, consec = _compute_effective_rate_v3(
        cur, kw_id=1, kw_source="confirmed", quote_id=1, detection_rate=100.0,
        lazy_threshold_days=7,
    )
    # 即便 consec=7 触发懒政 · effective = detection = 100%
    # 这样今日打到了 is_compliant 立刻 TRUE · 不被锁
    assert eff == 100.0
    assert consec == 7


def test_max_protection_doesnt_inflate_zero():
    """完全没打 0% × 7 天 + 今日 0 → max(0, 0) = 0 · 不虚高"""
    cur = _make_cursor(
        avg_rate=0.0,
        days=7,
        recent_is_compliant_desc=[False] * 7,
    )
    eff, hist_days, consec = _compute_effective_rate_v3(
        cur, kw_id=1, kw_source="confirmed", quote_id=1, detection_rate=0.0,
    )
    # 即便懒政切瞬时 · 也是 0%
    assert eff == 0.0


def test_window_8_days_drops_old_peak():
    """8 天前的 100% 应滑出 7 天窗口 · 不再保护"""
    # 模拟:近 7 天全 0% · 8 天前的 100% 不在窗口里(SQL WHERE 已过滤)
    cur = _make_cursor(
        avg_rate=0.0,  # 近 7 天都 0(8 天前 100 没进 avg)
        days=7,
        recent_is_compliant_desc=[False] * 7,
    )
    eff, hist_days, consec = _compute_effective_rate_v3(
        cur, kw_id=1, kw_source="confirmed", quote_id=1, detection_rate=0.0,
    )
    # 懒政切瞬时 = 0% · 第 1 天的 100% 已经滑出
    assert eff == 0.0
    assert hist_days == 7  # 窗口里有 7 行数据 · 但全是 0
