"""A1:采集轮漏跑自愈的判定逻辑(纯逻辑,DB 读全部注入替身)。

工单原文说"当前只有 selfserve 队列 consume/reap,无任何 job 发起轮次"。核到的事实相反:
`research_monitor_bimonthly_round` 一直存在,生产 cron-green 日志里也有它的注册记录,
`geo_research_config.cron_enabled` 在 2026-07-18 19:36 已被管理员重新打开。
真正的窟窿是**漏跑没人补**:补跑判定只在 server 启动那一刻跑一次,还要显式设 env(生产没设)。
所以这里不新造第二条起轮路径,只守住自愈判定的四个边界。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytz

from services.research_monitor import scheduler_setup as ss

BEIJING_TZ = pytz.timezone("Asia/Shanghai")


@pytest.fixture
def catchup_env(monkeypatch):
    """默认布景:闸门开、无活跃 round、最近预定时刻在 30 天前、从未成功跑过。"""
    calls: list[str] = []
    long_ago = datetime.now(BEIJING_TZ) - timedelta(days=30)

    monkeypatch.setattr(ss, "_load_cron_config_from_db",
                        lambda: {"enabled": True, "days": "1,16", "hour": 2, "available": True})
    monkeypatch.setattr(ss, "_last_scheduled_cron_time", lambda **kw: long_ago)
    monkeypatch.setattr(ss, "get_last_completed_round_at", lambda: None)
    monkeypatch.setattr(ss, "_has_active_round", lambda: False)
    monkeypatch.setattr(
        ss, "trigger_research_round_sync",
        lambda triggered_by='cron': calls.append(triggered_by) or {"processed": 1},
    )
    return {"calls": calls, "last_scheduled": long_ago}


def test_catchup_triggers_when_round_was_missed(catchup_env):
    out = ss.run_missed_round_catchup()
    assert out["catchup_triggered"] is True
    assert catchup_env["calls"] == ["missed_cron_recovery"]


def test_catchup_respects_closed_gate(catchup_env, monkeypatch):
    """闸门关着(熔断或人工停)时绝不擅自补跑 —— 熔断的意义就是等人确认。"""
    monkeypatch.setattr(ss, "_load_cron_config_from_db",
                        lambda: {"enabled": False, "days": "1,16", "hour": 2, "available": True})
    out = ss.run_missed_round_catchup()
    assert out["skipped"] == "gate_closed"
    assert catchup_env["calls"] == []


def test_catchup_waits_out_grace_window(catchup_env, monkeypatch):
    """预定时刻刚过就补跑会和正常 cron 抢跑,必须等过宽限期。"""
    just_now = datetime.now(BEIJING_TZ) - timedelta(hours=1)
    monkeypatch.setattr(ss, "_last_scheduled_cron_time", lambda **kw: just_now)
    out = ss.run_missed_round_catchup()
    assert out["skipped"] == "within_grace"
    assert catchup_env["calls"] == []


def test_catchup_skips_when_already_ran(catchup_env, monkeypatch):
    ran_after = catchup_env["last_scheduled"] + timedelta(hours=1)
    monkeypatch.setattr(ss, "get_last_completed_round_at", lambda: ran_after)
    out = ss.run_missed_round_catchup()
    assert out["skipped"] == "already_ran"
    assert catchup_env["calls"] == []


def test_catchup_skips_when_a_round_is_running(catchup_env, monkeypatch):
    monkeypatch.setattr(ss, "_has_active_round", lambda: True)
    out = ss.run_missed_round_catchup()
    assert out["skipped"] == "round_running"
    assert catchup_env["calls"] == []


def test_active_round_probe_fails_closed(monkeypatch):
    """活跃 round 查不出来时必须当作"有"(宁可不补,也不要并发起两轮)。"""
    class _BoomConn:
        def cursor(self):
            raise RuntimeError("db down")

        def close(self):
            pass

    monkeypatch.setattr(ss, "get_connection", lambda: _BoomConn())
    assert ss._has_active_round() is True


def test_round_trigger_reports_failure_shape_to_heartbeat(monkeypatch):
    """闸门查不到时返回 status=error,心跳层才认得出这是失败而不是"跳过"。"""
    def _boom():
        raise ss.ResearchCronGateUnavailable("gate table missing")

    monkeypatch.setattr(ss, "_preflight_auto_round_allowed", _boom)
    # 绕过心跳装饰器直接测内层业务返回值。
    result = ss.trigger_research_round_sync.__wrapped__()
    assert result["status"] == "error"
    assert "gate_unavailable" in result["error"]


def test_round_trigger_reports_gate_closed_as_skip_not_failure(monkeypatch):
    """闸门关是治理状态,不是任务失败 —— 不能把它记成 failed 去刷告警。"""
    monkeypatch.setattr(ss, "_preflight_auto_round_allowed", lambda: None)
    result = ss.trigger_research_round_sync.__wrapped__()
    assert result == {"processed": 0, "skipped": "gate_closed"}
