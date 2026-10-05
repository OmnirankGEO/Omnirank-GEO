from __future__ import annotations

import os

import psycopg2
import psycopg2.extras
import pytest

from services import customer_operation_plan as plans


TEST_URL = os.environ["TEST_DATABASE_URL"]
assert "geo_test" in TEST_URL and "xiaobang_test_only" in TEST_URL
SCHEMA = "xiaobang_monitor_truth_test"


def _connection():
    return psycopg2.connect(
        TEST_URL,
        options=f"-c search_path={SCHEMA}",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@pytest.fixture(scope="module", autouse=True)
def monitoring_schema():
    conn = psycopg2.connect(TEST_URL)
    try:
        cur = conn.cursor()
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.confirmed_keywords (
              quote_id BIGINT, status TEXT, is_monitored BOOLEAN,
              monitoring_status TEXT, created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS {SCHEMA}.topics (
              quote_id BIGINT, status TEXT, article_id BIGINT,
              completed_at TIMESTAMP, writing_started_at TIMESTAMP,
              created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS {SCHEMA}.media_publications (
              quote_id BIGINT, publish_timestamp TIMESTAMP, created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS {SCHEMA}.monitoring_tasks (
              id BIGINT PRIMARY KEY, quote_id BIGINT, status TEXT,
              completed_at TIMESTAMP, created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS {SCHEMA}.monitoring_results (
              id BIGSERIAL PRIMARY KEY, task_id BIGINT, target_outcome TEXT,
              tested_at TIMESTAMP
            )
        """)
        for table in (
            "monitoring_results", "monitoring_tasks", "media_publications",
            "topics", "confirmed_keywords",
        ):
            cur.execute(f"DELETE FROM {SCHEMA}.{table}")
        cur.execute(
            f"INSERT INTO {SCHEMA}.monitoring_tasks "
            "(id, quote_id, status, completed_at, created_at) VALUES "
            "(1, 7001, 'completed', NOW(), NOW()), "
            "(2, 7002, 'completed', NOW(), NOW())"
        )
        cur.execute(
            f"INSERT INTO {SCHEMA}.monitoring_results (task_id, target_outcome, tested_at) "
            "SELECT 1, 'legacy_unknown', NOW() FROM generate_series(1, 99)"
        )
        cur.execute(
            f"INSERT INTO {SCHEMA}.monitoring_results (task_id, target_outcome, tested_at) "
            "VALUES (1, 'not_mentioned', NOW())"
        )
        cur.execute(
            f"INSERT INTO {SCHEMA}.monitoring_results (task_id, target_outcome, tested_at) "
            "SELECT 2, 'legacy_unknown', NOW() FROM generate_series(1, 5)"
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_unknown_is_unresolved_not_a_negative_recommendation(monkeypatch):
    import db.connection

    monkeypatch.setattr(db.connection, "get_connection", _connection)
    metrics = plans._load_operational_metrics(7001)
    assert metrics["monitoring"] == {
        "available": True,
        "tasks": 1,
        "completed_tasks": 1,
        "observations": 100,
        "classified_observations": 1,
        "valid_observations": 1,
        "unclassified_observations": 99,
        "unresolved_observations": 99,
    }
    assert metrics["publication"]["brand_mentioned"] == 0
    assert metrics["publication"]["recommended"] == 0

    monkeypatch.setattr(plans, "_load_operational_metrics", lambda quote_id: metrics)
    monkeypatch.setattr(plans, "_load_existing_gap_plan", lambda quote: None)
    monkeypatch.setattr(plans, "_load_capacity_contract", lambda quote: None)
    # [WP7 2026-08-17] 签名多了 quote_id(品牌级口径会串同品牌两张报价)。
    # 桩必须跟着改成两参 —— 否则调用方一改就 TypeError,而那不是"发现了缺陷",
    # 是桩过期了。不变式没变,只是搬了家。
    monkeypatch.setattr(plans, "_publication_outcome_summary",
                        lambda brand_id, quote_id=None: {
        "available": False, "load_failed": False,
        "articles_cited": None, "articles_observable": None,
    })
    monkeypatch.setattr(plans, "_knowledge_summary", lambda brand_id: {
        "available": True, "load_failed": False,
        "filled": 1, "total": 1, "missing": [],
    })
    context = plans.AuthorizedAssistantContext(
        brand_id=70,
        brand_name="测试品牌",
        quote_id=7001,
        article_id=None,
        publication_id=None,
        monitoring_task_id=None,
        current_page="/monitoring",
        page_name="效果监测",
        data_updated_at=None,
        quote={"id": 7001, "brand_id": 70},
    )
    plan = plans.build_customer_operation_plan(context)
    evidence = next(item["value"] for item in plan["evidence"] if item["label"] == "发布与效果")
    assert "已判定 1 条中被提及 0、被推荐 0" in evidence
    assert "另有 99 条待判定或不可用" in evidence
    assert "不能按整体 0 解读" in evidence


def test_all_legacy_unknown_keeps_positive_metrics_unknown(monkeypatch):
    import db.connection

    monkeypatch.setattr(db.connection, "get_connection", _connection)
    metrics = plans._load_operational_metrics(7002)
    assert metrics["monitoring"]["observations"] == 5
    assert metrics["monitoring"]["valid_observations"] == 0
    assert metrics["monitoring"]["unresolved_observations"] == 5
    assert metrics["publication"]["brand_mentioned"] is None
    assert metrics["publication"]["recommended"] is None
