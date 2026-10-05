"""A2 判别锁:飞轮 job 的成败必须留痕,失败必须告警。

这一组测试就是工单里那句"注入一个必失败 job → 心跳表出现 failed 且告警产生"的可执行版本。
"""
from __future__ import annotations

import pytest

from db.connection import get_db
from db.flywheel_job_heartbeat_db import get_job_summary
from services.flywheel_heartbeat import (
    ALERT_RULE_JOB_FAILED,
    ALERT_RULE_JOB_STALE,
    FLYWHEEL_JOBS,
    flywheel_job,
    run_flywheel_watchdog,
    run_tracked,
    selftest_failing_job_wrapped,
)

pytestmark = pytest.mark.integration


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall() or []]


def _alerts(rule_key: str, fingerprint: str) -> list[dict]:
    return _rows(
        "SELECT * FROM ai_ops_alerts WHERE rule_key=%s AND fingerprint=%s ORDER BY id",
        (rule_key, fingerprint),
    )


def test_success_writes_heartbeat_with_processed(flywheel_db):
    @flywheel_job("media_effective_pool_distill")
    def _job():
        return {"processed": 7, "status": "ok"}

    assert _job() == {"processed": 7, "status": "ok"}

    rows = _rows("SELECT * FROM flywheel_job_heartbeats WHERE job_key=%s",
                 ("media_effective_pool_distill",))
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["processed"] == 7
    assert rows[0]["finished_at"] is not None
    assert rows[0]["error"] is None


def test_injected_failing_job_lands_failed_row_and_alert(flywheel_db):
    """🔒 判别锁:注入必失败 job → 心跳表 failed 行 + AIOps 告警 firing。"""
    result = selftest_failing_job_wrapped()
    # 默认不向 scheduler 抛(避免单个 job 拖挂调度器),但必须留下痕迹。
    assert result is None

    rows = _rows("SELECT * FROM flywheel_job_heartbeats WHERE job_key=%s",
                 ("flywheel_selftest_failing",))
    assert len(rows) == 1, "失败被静默吞了 —— 心跳表没有任何行"
    assert rows[0]["status"] == "failed"
    assert "RuntimeError" in (rows[0]["error"] or "")

    alerts = _alerts(ALERT_RULE_JOB_FAILED, "flywheel_selftest_failing")
    assert len(alerts) == 1, "失败没有产生 AIOps 告警"
    assert alerts[0]["status"] == "firing"


def test_status_error_return_counts_as_failure(flywheel_db):
    """项目里大量 job 用 `return {'status':'error'}` 收尾 —— 不抛异常也必须算失败。"""
    def _job():
        return {"status": "error", "error": "distill 数据源 500"}

    run_tracked("media_effective_pool_distill", _job)

    rows = _rows("SELECT * FROM flywheel_job_heartbeats WHERE job_key=%s",
                 ("media_effective_pool_distill",))
    assert rows[0]["status"] == "failed"
    assert "500" in (rows[0]["error"] or "")
    assert _alerts(ALERT_RULE_JOB_FAILED, "media_effective_pool_distill")


def test_success_resolves_previous_failure_alert(flywheel_db):
    run_tracked("publish_outcome_sync", lambda: {"status": "error", "error": "boom"})
    firing = _alerts(ALERT_RULE_JOB_FAILED, "publish_outcome_sync")
    assert firing and firing[0]["status"] == "firing"

    run_tracked("publish_outcome_sync", lambda: {"processed": 3})
    after = _alerts(ALERT_RULE_JOB_FAILED, "publish_outcome_sync")
    assert all(a["status"] == "resolved" for a in after), "恢复后旧告警没有自动消掉"


def test_keyboard_interrupt_is_recorded_then_reraised(flywheel_db):
    """中止信号不能被当成普通失败吞掉 —— 落表之后必须继续往上抛。"""
    def _job():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_tracked("writing_outcome_backfill", _job)

    rows = _rows("SELECT * FROM flywheel_job_heartbeats WHERE job_key=%s",
                 ("writing_outcome_backfill",))
    assert rows and rows[0]["status"] == "failed"


def test_consecutive_failures_counted_from_last_success(flywheel_db):
    run_tracked("publish_outcome_sync", lambda: {"processed": 1})
    for _ in range(3):
        run_tracked("publish_outcome_sync", lambda: {"status": "error", "error": "x"})

    summary = get_job_summary("publish_outcome_sync")
    assert summary["consecutive_failures"] == 3
    assert summary["last_success_at"] is not None


def test_watchdog_fires_when_job_is_stale(flywheel_db, monkeypatch):
    """连续 N 个周期未成功 → 拉停跑告警;重新成功 → 自动消警。"""
    job_key = "media_effective_pool_distill"
    spec = FLYWHEEL_JOBS[job_key]

    run_tracked(job_key, lambda: {"processed": 1})
    # 把这次成功推回到"远超阈值"的过去(阈值 = 周期 × alert_after_periods)。
    stale_days = int(spec.stale_after_seconds / 86400.0) + 3
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE flywheel_job_heartbeats "
            "SET started_at = NOW() - make_interval(days => %s), "
            "    finished_at = NOW() - make_interval(days => %s) "
            "WHERE job_key = %s",
            (stale_days, stale_days, job_key),
        )

    # 闸门规则不是本用例的被测面(它要另外几张业务表),这里只留 job 规则。
    monkeypatch.setattr("services.flywheel_heartbeat.evaluate_gate_health", lambda: [])

    out = run_flywheel_watchdog()
    assert out["firing"] >= 1
    stale_alerts = _alerts(ALERT_RULE_JOB_STALE, job_key)
    assert stale_alerts and stale_alerts[0]["status"] == "firing"

    run_tracked(job_key, lambda: {"processed": 2})
    after = _alerts(ALERT_RULE_JOB_STALE, job_key)
    assert all(a["status"] == "resolved" for a in after)


def test_watchdog_stays_quiet_on_fresh_ledger(flywheel_db, monkeypatch):
    """账本刚建立时全员"从未成功"是正常的,不许上线当天七条告警齐响。"""
    monkeypatch.setattr("services.flywheel_heartbeat.evaluate_gate_health", lambda: [])
    run_tracked("publish_outcome_sync", lambda: {"processed": 1})

    out = run_flywheel_watchdog()
    assert out["firing"] == 0

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM ai_ops_alerts WHERE status='firing'")
        assert cur.fetchone()["c"] == 0
