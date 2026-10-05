"""
Phase 4 PLAN 03 Task 1 · Scheduler 定时任务 + archive_done_older_than 单测

对齐 <behavior> 8 个 test:
  1. test_archive_done_older_than_done_100d     100 天前的 done 被归档
  2. test_archive_done_older_than_skips_80d      80 天前的 done 不归档(< 90 天)
  3. test_archive_done_older_than_includes_failed  failed 100 天前也被归档
  4. test_archive_skips_already_archived         archived_at IS NOT NULL 不重复
  5. test_zombie_killer_job_marks_stale          heartbeat=now-3min running → failed:zombie
  6. test_zombie_killer_skips_recent             heartbeat=now-30s running 不动
  7. test_scheduler_registers_2_new_jobs         get_jobs() 含新 2 个 job id
  8. test_zombie_job_handles_db_error            mark_zombie raise → _zombie_killer_job 不崩

运行:
  docker exec omnirank-ai pytest tests/test_scheduler_geo_plan_jobs.py -x -v
  # 或 python -m pytest tests/test_scheduler_geo_plan_jobs.py -x -v
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

try:
    import pytest
except ImportError:
    class _PytestStub:
        @staticmethod
        def skip(msg): raise Exception(f'SKIP: {msg}')
        @staticmethod
        def fail(msg): raise AssertionError(msg)
        class raises:
            def __init__(self, exc): self.exc = exc
            def __enter__(self): return self
            def __exit__(self, t, v, tb):
                if t is None or not issubclass(t, self.exc):
                    raise AssertionError(f'expected {self.exc.__name__}, got {t}')
                return True
    pytest = _PytestStub()

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_REGRESSION_DIR = _ROOT / "tests" / "regression"
if str(_REGRESSION_DIR) not in sys.path:
    sys.path.insert(0, str(_REGRESSION_DIR))


# 测试专用 user_id (9_000_2xx 段给 PLAN 03 scheduler 单测)
_TEST_USER_A = 9_000_201
_TEST_BRAND_A = 9_000_201


@pytest.fixture(scope="module", autouse=True)
def _provision_geo_plan_schema():
    """Make this DB test reproducible without relying on another test package."""
    from _dbsafe import (
        require_destructive_allowed,
        resolve_test_db_url,
    )

    url = resolve_test_db_url()
    if not url:
        pytest.skip("需要安全的 TEST_DATABASE_URL")
    require_destructive_allowed()

    import psycopg2

    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        conn.cursor().execute(
            """
            CREATE TABLE IF NOT EXISTS geo_plan_tasks (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                brand_id INTEGER NOT NULL,
                status VARCHAR(24) NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','running','settling','settlement_pending',
                                      'refund_pending','settle_conflict','done','failed',
                                      'cancelled','timeout')),
                params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                brand_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
                progress_stage VARCHAR(32),
                progress_percent SMALLINT NOT NULL DEFAULT 0
                    CHECK (progress_percent BETWEEN 0 AND 100),
                progress_message TEXT,
                result_json JSONB,
                error_code VARCHAR(32),
                error_detail TEXT,
                queued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at TIMESTAMPTZ,
                heartbeat_at TIMESTAMPTZ,
                done_at TIMESTAMPTZ,
                data_mode VARCHAR(16) NOT NULL DEFAULT 'full'
                    CHECK (data_mode IN ('full','l1l2_fallback')),
                source VARCHAR(32) NOT NULL,
                ip_at_start VARCHAR(64),
                linked_quote_id INTEGER,
                archived_at TIMESTAMPTZ,
                freeze_id INTEGER,
                freeze_table VARCHAR(40),
                pending_terminal VARCHAR(16),
                settle_retry_count SMALLINT NOT NULL DEFAULT 0,
                settle_last_error TEXT
            )
            """
        )
    finally:
        conn.close()
    yield
    _cleanup_tasks()


def _get_conn():
    from db.connection import get_connection
    return get_connection()


def _cleanup_tasks():
    """清理测试 user 的所有 task."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM geo_plan_tasks WHERE user_id = %s",
            (_TEST_USER_A,),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _insert_task_with_raw_times(
    status: str,
    done_at_days_ago: int | None = None,
    heartbeat_seconds_ago: int | None = None,
    archived: bool = False,
    error_code: str | None = None,
) -> int:
    """直接 INSERT 一条 task,绕开 mark_status,方便精确控制时间戳."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        sets_sql = [
            "user_id", "brand_id", "status", "params_json", "brand_snapshot",
            "progress_percent", "data_mode", "source", "queued_at",
        ]
        vals_sql = ["%s", "%s", "%s", "'{}'::jsonb", "'{}'::jsonb",
                    "0", "'full'", "'c_end_chat'", "NOW()"]
        params: list = [_TEST_USER_A, _TEST_BRAND_A, status]

        if status != "queued":
            sets_sql.append("started_at")
            vals_sql.append("NOW()")

        if done_at_days_ago is not None:
            sets_sql.append("done_at")
            vals_sql.append("NOW() - (%s || ' days')::interval")
            params.append(str(done_at_days_ago))

        if heartbeat_seconds_ago is not None:
            sets_sql.append("heartbeat_at")
            vals_sql.append("NOW() - (%s || ' seconds')::interval")
            params.append(str(heartbeat_seconds_ago))

        if archived:
            sets_sql.append("archived_at")
            vals_sql.append("NOW()")

        if error_code:
            sets_sql.append("error_code")
            vals_sql.append("%s")
            params.append(error_code)

        sql = f"INSERT INTO geo_plan_tasks ({', '.join(sets_sql)}) VALUES ({', '.join(vals_sql)}) RETURNING id"
        cur.execute(sql, params)
        tid = cur.fetchone()["id"]
        conn.commit()
        return int(tid)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _get_task(task_id: int) -> dict | None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM geo_plan_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# archive_done_older_than
# ============================================================


class TestArchiveDoneOlderThan:
    """db.geo_plan_tasks_db.archive_done_older_than 函数单测."""

    def setup_method(self, method):
        _cleanup_tasks()

    def teardown_method(self, method):
        _cleanup_tasks()

    def test_archive_done_older_than_done_100d(self):
        """100 天前的 done 应该被归档."""
        from db.geo_plan_tasks_db import archive_done_older_than
        old_tid = _insert_task_with_raw_times(status="done", done_at_days_ago=100)

        n = archive_done_older_than(days=90)
        assert n >= 1, f"应至少归档 1 条,实际 {n}"
        row = _get_task(old_tid)
        assert row["archived_at"] is not None, "old done 应被归档"

    def test_archive_done_older_than_skips_80d(self):
        """80 天前的 done 不应被归档(< 90 天阈值)."""
        from db.geo_plan_tasks_db import archive_done_older_than
        fresh_tid = _insert_task_with_raw_times(status="done", done_at_days_ago=80)

        archive_done_older_than(days=90)
        row = _get_task(fresh_tid)
        assert row["archived_at"] is None, "80 天前(未满 90d)不应归档"

    def test_archive_done_older_than_includes_failed(self):
        """failed/cancelled/timeout 100 天前也应被归档(status IN ('done','failed','cancelled','timeout'))."""
        from db.geo_plan_tasks_db import archive_done_older_than
        failed_tid = _insert_task_with_raw_times(
            status="failed", done_at_days_ago=100, error_code="external_api"
        )
        cancelled_tid = _insert_task_with_raw_times(status="cancelled", done_at_days_ago=100)
        timeout_tid = _insert_task_with_raw_times(
            status="timeout", done_at_days_ago=100, error_code="timeout"
        )

        archive_done_older_than(days=90)

        for tid in (failed_tid, cancelled_tid, timeout_tid):
            row = _get_task(tid)
            assert row["archived_at"] is not None, f"task {tid} ({row['status']}) 应被归档"

    def test_archive_skips_already_archived(self):
        """archived_at 已非空的不重复归档(archived_at 值不变)."""
        from db.geo_plan_tasks_db import archive_done_older_than
        tid = _insert_task_with_raw_times(
            status="done", done_at_days_ago=100, archived=True,
        )
        row_before = _get_task(tid)
        archived_at_before = row_before["archived_at"]

        archive_done_older_than(days=90)
        row_after = _get_task(tid)
        assert row_after["archived_at"] == archived_at_before, "archived_at 不应被重写"


# ============================================================
# _zombie_killer_job / _archive_job
# ============================================================


class TestSchedulerJobs:
    """api.scheduler._zombie_killer_job + _archive_job + job 注册."""

    def setup_method(self, method):
        _cleanup_tasks()

    def teardown_method(self, method):
        _cleanup_tasks()

    def test_zombie_killer_job_marks_stale(self):
        """Stale paid-capable work enters refund compensation before its final state."""
        from api.scheduler import _zombie_killer_job

        stale_tid = _insert_task_with_raw_times(
            status="running", heartbeat_seconds_ago=180,
        )

        # 调 job,直接跑 mark_zombie 逻辑
        _zombie_killer_job()

        row = _get_task(stale_tid)
        assert row["status"] == "refund_pending", \
            f"冻结结算前不得直接 failed, 实际 {row['status']}"
        assert row["pending_terminal"] == "failed"
        assert row["error_code"] == "zombie", f"期望 zombie, 实际 {row['error_code']}"
        assert row["done_at"] is None, "退款补偿完成前不得伪造终态时间"

    def test_zombie_killer_skips_recent(self):
        """heartbeat=now-30s 的 running task 不应被 zombie killer 动."""
        from api.scheduler import _zombie_killer_job

        fresh_tid = _insert_task_with_raw_times(
            status="running", heartbeat_seconds_ago=30,
        )

        _zombie_killer_job()

        row = _get_task(fresh_tid)
        assert row["status"] == "running", f"30s 前的心跳不应被清理,实际 {row['status']}"

    def test_archive_job_callable(self):
        """_archive_job 可直接调用,空表也不抛."""
        from api.scheduler import _archive_job
        # 不抛异常即 OK
        _archive_job()

    def test_zombie_job_handles_db_error(self):
        """mark_zombie mock raise → _zombie_killer_job 捕获 log 不崩."""
        import api.scheduler as sched_mod

        def _raise(*args, **kwargs):
            raise RuntimeError("simulated DB error")

        with patch("db.geo_plan_tasks_db.mark_zombie", side_effect=_raise):
            # 不应抛异常,所有错误 try/except 捕获
            try:
                sched_mod._zombie_killer_job()
            except Exception as e:
                pytest.fail(f"_zombie_killer_job 应当吞异常,但抛了 {e!r}")

    def test_scheduler_registers_2_new_jobs(self):
        """register_v32_core_tasks() 调完,scheduler 含 geo_plan_task_zombie_killer 和
        geo_plan_task_cleanup_archiver 两个 job id."""
        from api.scheduler import register_v32_core_tasks, get_scheduler

        # 必须调一次确保挂上 (idempotent · replace_existing=True)
        register_v32_core_tasks()

        scheduler = get_scheduler()
        job_ids = {j.id for j in scheduler.get_jobs()}
        assert "geo_plan_task_zombie_killer" in job_ids, \
            f"missing geo_plan_task_zombie_killer, 现有 jobs: {job_ids}"
        assert "geo_plan_task_cleanup_archiver" in job_ids, \
            f"missing geo_plan_task_cleanup_archiver, 现有 jobs: {job_ids}"


# =========================================================================
# 独立跑:python tests/test_scheduler_geo_plan_jobs.py
# =========================================================================

if __name__ == "__main__":
    for cls in (TestArchiveDoneOlderThan, TestSchedulerJobs):
        t = cls()
        methods = [m for m in dir(t) if m.startswith("test_")]
        for m in methods:
            t.setup_method(m)
            try:
                getattr(t, m)()
                print(f"[PASS] {cls.__name__}.{m}")
            except Exception as e:
                print(f"[FAIL] {cls.__name__}.{m}: {type(e).__name__}: {e}")
            finally:
                t.teardown_method(m)
