"""飞轮闭环 A 段测试夹具。

只建本批真正需要的表(心跳账本 + ai_ops_alerts + 写作飞轮三表),不假设测试库有全量业务 schema。
"""
import pytest

# ai_ops_alerts 的 DDL 与 scripts/migration_ai_ops_center_2026_07_01.sql 逐字一致,
# 只去掉了 task_id 对 ai_ops_tasks 的外键(本批测试不涉及任务面,不必拖进整张表)。
AI_OPS_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS ai_ops_alerts (
  id SERIAL PRIMARY KEY,
  rule_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'warn' CHECK (severity IN ('info', 'warn', 'critical')),
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'firing' CHECK (status IN ('firing', 'resolved')),
  task_id INTEGER NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMP NULL,
  resolved_by INTEGER NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_ops_alerts_firing
  ON ai_ops_alerts (rule_key, fingerprint) WHERE status = 'firing';
"""


@pytest.fixture
def flywheel_db():
    """干净的心跳账本 + 告警表。每个用例前清空,避免跨用例污染。"""
    from db.connection import get_db
    from db.flywheel_job_heartbeat_db import init_flywheel_heartbeat_tables

    init_flywheel_heartbeat_tables(force=True)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(AI_OPS_ALERTS_DDL)
        cur.execute("DELETE FROM flywheel_job_heartbeats")
        cur.execute("DELETE FROM ai_ops_alerts")
    yield
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM flywheel_job_heartbeats")
        cur.execute("DELETE FROM ai_ops_alerts")


@pytest.fixture
def flywheel_corpus_db():
    """B2 语料价值标签表(干净)。"""
    from db.connection import get_db
    from db.flywheel_corpus_label_db import init_flywheel_corpus_label_tables

    init_flywheel_corpus_label_tables(force=True)
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM flywheel_corpus_value_labels")
    yield
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM flywheel_corpus_value_labels")


@pytest.fixture
def writing_flywheel_db():
    """写作飞轮三表(versions / assignments / outcome_events)+ 本批 additive schema。"""
    from db.connection import get_db
    from services.writing_strategy_assignment import ensure_assignment_schema

    ensure_assignment_schema(force=True)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM writing_strategy_outcome_events")
        cur.execute("DELETE FROM writing_strategy_assignments")
        cur.execute("DELETE FROM writing_strategy_versions WHERE industry_key LIKE 'geo_test%'")
    yield
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM writing_strategy_outcome_events")
        cur.execute("DELETE FROM writing_strategy_assignments")
        cur.execute("DELETE FROM writing_strategy_versions WHERE industry_key LIKE 'geo_test%'")
