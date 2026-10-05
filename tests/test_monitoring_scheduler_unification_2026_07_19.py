"""Regression coverage for the monitoring delivery/billing ownership boundary."""

from __future__ import annotations

import ast
import os
import inspect
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_root_scheduler_does_not_register_automatic_daily_monitoring():
    source = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    setup = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "setup_default_jobs"
    )
    calls = [node for node in ast.walk(setup) if isinstance(node, ast.Call)]

    registered_ids = {
        keyword.value.value
        for call in calls
        for keyword in call.keywords
        if keyword.arg == "job_id" and isinstance(keyword.value, ast.Constant)
    }
    registered_callables = {
        call.args[0].id
        for call in calls
        if call.args and isinstance(call.args[0], ast.Name)
    }

    assert "daily_monitoring" not in registered_ids
    assert "job_daily_monitoring" not in registered_callables
    assert "registered by api.scheduler" in ast.get_source_segment(source, setup)


def test_api_scheduler_owns_keyword_subscription_registration():
    source = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")

    assert "def register_monitoring_delivery_jobs(" in source
    assert 'job_id = "keyword_subscription_daily_monitoring"' in source
    assert "id=job_id" in source
    assert "scheduler_sync_callable(" in source
    assert "sched_claim(job_id" in source
    assert "register_monitoring_delivery_jobs(scheduler)" in source
    assert '"keyword_subscription_daily_monitoring",' in source[
        source.index("def get_status") : source.index("def _run_answer_entity_daily_extract")
    ]


def test_api_scheduler_registration_is_single_and_synchronous():
    from api.scheduler import register_monitoring_delivery_jobs

    class FakeScheduler:
        def __init__(self):
            self.jobs = {}

        def get_job(self, job_id):
            return self.jobs.get(job_id)

        def add_job(self, func, *, id, **kwargs):
            self.jobs[id] = {"func": func, **kwargs}

    scheduler = FakeScheduler()

    assert register_monitoring_delivery_jobs(scheduler) is True
    assert register_monitoring_delivery_jobs(scheduler) is False
    assert list(scheduler.jobs) == ["keyword_subscription_daily_monitoring"]
    assert not inspect.iscoroutinefunction(
        scheduler.jobs["keyword_subscription_daily_monitoring"]["func"]
    )


def test_paid_monitoring_registration_failure_is_fatal_for_cron():
    from api.scheduler import register_v32_core_tasks

    class FailingScheduler:
        def get_job(self, job_id):
            # Skip the unrelated notification registration and fail specifically
            # while adding the paid monitoring delivery job.
            if job_id == "notification_outbox_dispatch":
                return object()
            return None

        def add_job(self, _func, *, id, **_kwargs):
            if id == "keyword_subscription_daily_monitoring":
                raise OSError("scheduler store unavailable")
            raise AssertionError(f"unexpected registration after fatal failure: {id}")

    import api.scheduler as scheduler_module

    original = scheduler_module.get_scheduler
    scheduler_module.get_scheduler = lambda: FailingScheduler()
    try:
        with pytest.raises(RuntimeError, match="拒绝启动 cron"):
            register_v32_core_tasks()
    finally:
        scheduler_module.get_scheduler = original


def test_server_propagates_cron_core_registration_failure():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    marker = "# ========== v3.2/v3.3 核心定时任务"
    block = source[source.index(marker) : source.index("# ========== 启动时恢复卡死", source.index(marker))]
    assert 'if _IS_CRON_ROLE:' in block
    assert 'raise RuntimeError("cron 核心任务注册失败，拒绝带病启动")' in block


@pytest.mark.skipif(
    not os.environ.get("MONITORING_SCHEDULER_PG_TEST_URL"),
    reason="set MONITORING_SCHEDULER_PG_TEST_URL to run the PostgreSQL ownership proof",
)
def test_postgres_entitlement_ownership_is_disjoint(monkeypatch):
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    from db import monitoring_db
    from api.monitoring_api import _resolve_keyword_display_platforms
    from tools.monitoring.batch_monitor import PlatformAdapter

    dsn = os.environ["MONITORING_SCHEDULER_PG_TEST_URL"]
    database_name = f"monitor_owner_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

        parsed = urlsplit(dsn)
        target_dsn = urlunsplit(
            (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
        )

        def connect():
            return psycopg2.connect(
                target_dsn,
                cursor_factory=RealDictCursor,
            )

        monkeypatch.setattr(monitoring_db, "get_connection", connect)

        conn = connect()
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE quotes (
                    id INTEGER PRIMARY KEY,
                    brand_id INTEGER NOT NULL,
                    brand_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    paid_at TIMESTAMP,
                    service_start_date DATE,
                    service_end_date DATE,
                    service_days INTEGER DEFAULT 365,
                    service_status TEXT DEFAULT 'active',
                    monitoring_enabled BOOLEAN DEFAULT FALSE,
                    monitoring_frequency INTEGER DEFAULT 1,
                    monitoring_start_hour INTEGER DEFAULT 8,
                    monitoring_interval_hours INTEGER DEFAULT 24,
                    monitoring_last_run_at TIMESTAMP
                );
                CREATE TABLE confirmed_keywords (
                    id INTEGER PRIMARY KEY,
                    quote_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    monitoring_query TEXT,
                    category TEXT,
                    is_core BOOLEAN DEFAULT TRUE,
                    super_red_ocean BOOLEAN DEFAULT FALSE,
                    is_monitored BOOLEAN DEFAULT FALSE,
                    monitoring_status TEXT DEFAULT 'active'
                );
                CREATE TABLE client_keywords (
                    id INTEGER PRIMARY KEY,
                    platforms TEXT DEFAULT 'dashscope,deepseek,doubao,yuanbao'
                );
                CREATE TABLE extra_keywords (
                    id INTEGER PRIMARY KEY,
                    quote_id INTEGER NOT NULL,
                    brand_id INTEGER,
                    keyword TEXT,
                    monitoring_query TEXT,
                    target_brand TEXT,
                    platforms TEXT,
                    difficulty TEXT,
                    status TEXT DEFAULT 'active'
                );
                CREATE TABLE monitoring_config (
                    id INTEGER PRIMARY KEY,
                    default_platforms TEXT DEFAULT 'dashscope,deepseek,doubao,yuanbao',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE monitoring_tasks (
                    id INTEGER PRIMARY KEY,
                    platform_count INTEGER DEFAULT 4
                );
                CREATE TABLE keyword_compliance_log (
                    keyword_id INTEGER NOT NULL,
                    keyword_source TEXT NOT NULL,
                    quote_id INTEGER NOT NULL,
                    check_date DATE NOT NULL,
                    is_compliant BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE keyword_monitor_subscriptions (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    keyword_id INTEGER NOT NULL,
                    quote_id INTEGER NOT NULL,
                    brand_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    daily_points INTEGER DEFAULT 130,
                    feature_code TEXT DEFAULT 'monitoring_keyword_daily',
                    last_charged_at TIMESTAMP
                );
                CREATE TABLE articles (
                    id INTEGER PRIMARY KEY,
                    quote_id INTEGER NOT NULL,
                    first_published_at TIMESTAMP
                );
                """
            )
            retired_migration_sql = (
                ROOT / "scripts" / "migration_monitoring_yuanbao_default_2026_07_20.sql"
            ).read_text(encoding="utf-8")
            migration_sql = (
                ROOT / "scripts" / "migration_monitoring_product_matrix_2026_07_21.sql"
            ).read_text(encoding="utf-8")
            cur.execute(retired_migration_sql)
            cur.execute(migration_sql)
            cur.execute(
                """
                INSERT INTO monitoring_product_platform_matrices (version, platforms)
                VALUES
                    ('monitoring-deepseek-v1', 'deepseek'),
                    ('monitoring-doubao-v1', 'doubao'),
                    ('monitoring-kimi-v1', 'kimi'),
                    ('monitoring-dashscope-v1', 'dashscope'),
                    ('monitoring-deepseek-yuanbao-v1', 'deepseek,yuanbao')
                """
            )

            # q1: legacy quote-only delivery. q2: active keyword entitlement.
            # q3: paused entitlement must not fall through. q4: an explicitly
            # cancelled per-keyword entitlement must not silently resume through
            # the legacy quote path. q5 proves ownership is per phrase.
            cur.execute(
                """
                INSERT INTO quotes
                    (id, brand_id, brand_name, status, paid_at, service_start_date,
                     service_end_date, service_days, monitoring_enabled)
                VALUES
                    (1, 101, 'legacy', 'paid', NOW(), CURRENT_DATE, CURRENT_DATE + 30, 30, TRUE),
                    (2, 102, 'active-sub', 'paid', NOW(), CURRENT_DATE, CURRENT_DATE + 30, 30, TRUE),
                    (3, 103, 'paused-sub', 'paid', NOW(), CURRENT_DATE, CURRENT_DATE + 30, 30, TRUE),
                    (4, 104, 'cancelled-sub', 'paid', NOW(), CURRENT_DATE, CURRENT_DATE + 30, 30, TRUE),
                    (5, 105, 'mixed', 'paid', NOW(), CURRENT_DATE, CURRENT_DATE + 30, 30, TRUE);

                INSERT INTO confirmed_keywords
                    (id, quote_id, keyword, monitoring_query,
                     monitoring_product_version, is_monitored)
                VALUES
                    (11, 1, 'legacy phrase', 'legacy phrase exact', 'monitoring-deepseek-v1', TRUE),
                    (21, 2, 'active phrase', 'active phrase exact', 'monitoring-doubao-v1', TRUE),
                    (31, 3, 'paused phrase', 'paused phrase exact', 'monitoring-kimi-v1', FALSE),
                    (41, 4, 'cancelled phrase', 'cancelled phrase exact', 'monitoring-dashscope-v1', FALSE),
                    (51, 5, 'mixed subscribed phrase', 'mixed subscribed exact', 'monitoring-deepseek-yuanbao-v1', TRUE),
                    (52, 5, 'mixed legacy phrase', 'mixed legacy exact', 'monitoring-doubao-v1', FALSE);

                INSERT INTO articles (id, quote_id, first_published_at)
                VALUES (1, 1, NOW()), (2, 2, NOW()), (3, 3, NOW()), (4, 4, NOW()),
                       (5, 5, NOW());

                INSERT INTO keyword_monitor_subscriptions
                    (id, user_id, keyword_id, quote_id, brand_id, status)
                VALUES
                    (201, 9002, 21, 2, 102, 'active'),
                    (301, 9003, 31, 3, 103, 'paused_low_balance'),
                    (401, 9004, 41, 4, 104, 'cancelled'),
                    (501, 9005, 51, 5, 105, 'active');
                """
            )

            cur.execute(
                """
                SELECT COUNT(*) AS count
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'confirmed_keywords'
                   AND column_name = 'platforms'
                """
            )
            assert cur.fetchone()["count"] == 0

        subscriptions = monitoring_db.list_active_subscriptions()
        legacy_quotes = monitoring_db.get_monitoring_enabled_clients()

        subscription_keyword_ids = {row["keyword_id"] for row in subscriptions}
        legacy_keyword_ids = set()
        for quote in legacy_quotes:
            rows = monitoring_db.get_keywords_for_monitoring(
                quote_id=quote["quote_id"],
                exclude_keyword_subscription_owned=True,
            )
            legacy_keyword_ids.update(
                row["id"] for row in rows if row.get("source") == "confirmed"
            )

        assert subscription_keyword_ids == {21, 51}
        assert legacy_keyword_ids == {11, 52}
        assert {
            row["keyword_id"]: row["entitlement_platforms"]
            for row in subscriptions
        } == {21: "doubao", 51: "deepseek,yuanbao"}
        legacy_keyword_rows = [
            row
            for quote in legacy_quotes
            for row in monitoring_db.get_keywords_for_monitoring(
                quote_id=quote["quote_id"],
                exclude_keyword_subscription_owned=True,
            )
            if row.get("source") == "confirmed"
        ]
        legacy_rows = {
            row["id"]: row["entitlement_platforms"]
            for row in legacy_keyword_rows
        }
        assert legacy_rows == {11: "deepseek", 52: "doubao"}
        assert subscription_keyword_ids.isdisjoint(legacy_keyword_ids)
        assert {31, 41}.isdisjoint(subscription_keyword_ids | legacy_keyword_ids)

        billing_features_by_keyword = {
            **{row["keyword_id"]: row["feature_code"] for row in subscriptions},
            **{keyword_id: "scheduled_monitoring" for keyword_id in legacy_keyword_ids},
        }
        assert billing_features_by_keyword == {
            11: "scheduled_monitoring",
            21: "monitoring_keyword_daily",
            51: "monitoring_keyword_daily",
            52: "scheduled_monitoring",
        }

        # The rows above came from real PostgreSQL. Prove that execution and
        # customer projection both keep those per-keyword rights instead of
        # expanding the aggregate platform union back onto every keyword.
        purchased_rows = [
            {
                "id": row["keyword_id"],
                "quote_id": row["quote_id"],
                "source": "confirmed",
                "entitlement_platforms": row["entitlement_platforms"],
            }
            for row in subscriptions
        ] + legacy_keyword_rows
        plan = PlatformAdapter.resolve_keyword_monitoring_plan(
            purchased_rows,
            configured_platforms="dashscope,deepseek,kimi,doubao",
        )
        execution_pairs = [
            (row["id"], platform)
            for row in plan["keywords"]
            for platform in row["_eligible_monitoring_platforms"]
        ]
        assert execution_pairs == [
            (21, "doubao"),
            (51, "deepseek"),
            (11, "deepseek"),
            (52, "doubao"),
        ]
        assert len(execution_pairs) == 4
        assert {
            row["id"]: _resolve_keyword_display_platforms(
                row,
                "dashscope,deepseek,kimi,doubao",
            )
            for row in purchased_rows
        } == {
            11: ["deepseek"],
            21: ["doubao"],
            51: ["deepseek"],
            52: ["doubao"],
        }
    finally:
        try:
            with admin.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                      FROM pg_stat_activity
                     WHERE datname = %s
                       AND pid <> pg_backend_pid()
                    """,
                    (database_name,),
                )
                cur.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(database_name)
                    )
                )
        finally:
            admin.close()
