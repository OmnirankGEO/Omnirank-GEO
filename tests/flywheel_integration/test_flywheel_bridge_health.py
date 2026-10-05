"""[item7] flywheel bridge run audit + anti-drift health snapshot."""
import pytest

from db.flywheel_bridge_db import (
    finish_bridge_run,
    get_flywheel_bridge_health,
    init_flywheel_bridge_tables,
    list_recent_bridge_runs,
    start_bridge_run,
)


def test_bridge_run_audit_recorded():
    init_flywheel_bridge_tables()
    run_id = start_bridge_run("RH_AUDIT", "batch_RH_AUDIT", trigger_source="manual", dry_run=False)
    assert run_id > 0
    finish_bridge_run(
        run_id,
        status="success",
        stages={"source_signals": {"written": 3, "status": "success"}},
        totals={"loaded": 5, "inserted": 3, "updated": 0, "skipped": 1, "failed": 0},
        last_processed_raw_id=999,
        error=None,
    )
    runs = list_recent_bridge_runs(limit=50)
    mine = [r for r in runs if r["round_id"] == "RH_AUDIT"]
    assert mine, "bridge run 应被审计记录"
    assert mine[0]["status"] == "success"
    assert int(mine[0]["inserted"]) == 3
    assert int(mine[0]["last_processed_raw_id"]) == 999


def test_health_snapshot_has_anti_drift_fields():
    init_flywheel_bridge_tables()
    health = get_flywheel_bridge_health()
    for key in (
        "last_raw_at", "last_source_signal_at", "last_answer_metric_at",
        "last_media_entity_at", "lag_hours", "stale", "stale_threshold_hours",
        "stale_message", "last_bridge_status", "last_success_round_id", "last_error",
    ):
        assert key in health, f"health 缺字段 {key}"
    assert isinstance(health["stale"], bool)
    assert health["stale_threshold_hours"] == 24.0
    # stale_message present only when stale
    if health["stale"]:
        assert health["stale_message"]
    else:
        assert health["stale_message"] == ""
