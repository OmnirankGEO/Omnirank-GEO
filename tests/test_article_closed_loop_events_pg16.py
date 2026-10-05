from __future__ import annotations

import os
from pathlib import Path
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest


ROOT = Path(__file__).resolve().parents[1]
BASE_SQL = (ROOT / "scripts" / "fixtures" / "geo_article_closed_loop_pg16_base.sql").read_text(encoding="utf-8")
MIGRATION_SQL = (ROOT / "scripts" / "migration_geo_article_closed_loop_v1_2026_07_20.sql").read_text(encoding="utf-8")
TEST_DSN = os.getenv("ARTICLE_CLOSED_LOOP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="ARTICLE_CLOSED_LOOP_TEST_DATABASE_URL not configured")


def _schema(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _connect(schema: str):
    return psycopg2.connect(TEST_DSN, options=f"-csearch_path={schema},public", cursor_factory=RealDictCursor)


def _fresh(schema: str) -> None:
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", schema))
        cur.execute(f"SET search_path TO {schema},public")
        cur.execute(MIGRATION_SQL)
    finally:
        conn.close()


def _quote(cur, *, status: str, service_status: str, source_type: str, paid: float, writing: str) -> int:
    cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Brand',7) RETURNING id")
    brand_id = int(cur.fetchone()["id"])
    cur.execute(
        """
        INSERT INTO quotes(
            brand_id,owner_user_id,status,service_status,source_type,paid_amount,
            writing_status,confirmed_at,paid_at
        ) VALUES (%s,7,%s,%s,%s,%s,%s,NOW(),NOW()) RETURNING id
        """,
        (brand_id, status, service_status, source_type, paid, writing),
    )
    quote_id = int(cur.fetchone()["id"])
    cur.execute(
        "INSERT INTO confirmed_keywords(quote_id,keyword,required_articles,is_core) "
        "VALUES (%s,'核心词',2,TRUE),(%s,'派生非计费词',9,FALSE)",
        (quote_id, quote_id),
    )
    return quote_id


def test_all_and_only_signed_event_kinds_have_real_authority_predicates():
    schema = _schema("geo_loop_events")
    _fresh(schema)
    from services.article_closed_loop_contract import EVENT_KINDS, REMOVED_EVENT_KINDS
    from services.article_delivery_plan import AuthorityConflict, load_quote_authority_event

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        offline_id = _quote(cur, status="paid", service_status="active", source_type="offline", paid=100, writing="pending")
        agent_id = _quote(cur, status="active", service_status="active", source_type="agent_quote", paid=100, writing="pending")
        zero_id = _quote(cur, status="confirmed", service_status="", source_type="quick_writing", paid=0, writing="pending")
        add_on_id = _quote(cur, status="confirmed", service_status="active", source_type="agent_quote", paid=100, writing="titles_ready")
        events = {
            "quote_paid_offline": load_quote_authority_event(cur, offline_id, "quote_paid_offline"),
            "quote_paid_agent_activation": load_quote_authority_event(cur, agent_id, "quote_paid_agent_activation"),
            "zero_price_writing_project_created": load_quote_authority_event(cur, zero_id, "zero_price_writing_project_created"),
            "contract_add_on": load_quote_authority_event(cur, add_on_id, "contract_add_on"),
            "keyword_reassigned": load_quote_authority_event(cur, add_on_id, "keyword_reassigned"),
        }
        assert set(events) == EVENT_KINDS - {"quote_paid_standard", "publication_locked"}
        for event in events.values():
            assert len(event.snapshot["confirmed_keywords"]) == 1
            assert event.snapshot["confirmed_keywords"][0]["keyword"] == "核心词"
            assert event.snapshot["confirmed_keywords"][0]["required_articles"] == 2
        for removed in REMOVED_EVENT_KINDS:
            with pytest.raises(AuthorityConflict, match="unsupported_event_kind"):
                load_quote_authority_event(cur, add_on_id, removed)
    finally:
        conn.close()


def test_zero_article_count_is_not_silently_changed_to_one():
    schema = _schema("geo_loop_zero_count")
    _fresh(schema)
    from services.article_delivery_plan import AuthorityNotReady, _desired_assignments, load_quote_authority_event

    mixed = _desired_assignments(
        {
            "confirmed_keywords": [
                {"confirmed_keyword_id": 1, "keyword": "不交付", "required_articles": 0},
                {"confirmed_keyword_id": 2, "keyword": "交付", "required_articles": 2},
            ]
        }
    )
    assert [item["keyword_id"] for item in mixed] == [2, 2]

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        quote_id = _quote(cur, status="confirmed", service_status="active", source_type="agent_quote", paid=100, writing="pending")
        cur.execute("UPDATE confirmed_keywords SET required_articles=0 WHERE quote_id=%s AND is_core IS TRUE", (quote_id,))
        # [P1 容量合同 2026-08-08] 原因串随语义改名:容量为 0 是合法商业态(capacity_zero),
        #   不是"交付数为零 = 数据没准备好"。行为不变(仍然不编译 slot),只是原因串说人话。
        with pytest.raises(AuthorityNotReady, match="authorized_capacity_zero"):
            load_quote_authority_event(cur, quote_id, "contract_add_on")
    finally:
        conn.close()


def test_reconciler_never_guesses_between_offline_and_agent_activation(monkeypatch):
    schema = _schema("geo_loop_ambiguous")
    _fresh(schema)
    import services.article_delivery_plan as plan

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        quote_id = _quote(cur, status="paid", service_status="active", source_type="agent_quote", paid=100, writing="pending")
        cur.execute(
            "INSERT INTO audit_logs(action,entity_type,entity_id) VALUES "
            "('quote_offline_mark_paid','quote',%s),('agent_activate_service','quote',%s)",
            (quote_id, quote_id),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_RECONCILER_ENABLED", "true")
    monkeypatch.setattr(plan, "_get_connection", lambda: _connect(schema))
    result = plan.reconcile_authoritative_quotes(limit=10)
    assert result == {"scanned": 1, "enqueued": 0, "ambiguous": 1}
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_plan_outbox")
        assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_publication_lineage_savepoint_never_poison_existing_fact_transaction(monkeypatch):
    schema = _schema("geo_loop_publish_sidecar_failure")
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", schema))
    finally:
        conn.close()

    import services.article_delivery_plan as plan

    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        result = plan.enqueue_publication_locked_in_transaction_if_enabled(
            cur,
            publication_source="publish_order_items",
            publication_source_id=99,
            article_id=99,
            platform="搜狐",
            provider_receipt="receipt-99",
            public_url="https://example.com/published",
            submitted_content_hash="a" * 64,
        )
        assert result["enqueued"] is False
        assert result["reason"] in {"UndefinedTable", "UndefinedColumn"}
        cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Fact Still Commits',7)")
        conn.commit()
        cur.execute("SELECT COUNT(*) AS n FROM brands WHERE name='Fact Still Commits'")
        assert cur.fetchone()["n"] == 1
    finally:
        conn.close()


def test_business_event_loader_failure_isolated_before_commercial_commit(monkeypatch):
    schema = _schema("geo_loop_business_sidecar_failure")
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", schema))
    finally:
        conn.close()

    import services.article_delivery_plan as plan

    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Commercial Fact',7) RETURNING id")
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO quotes(brand_id,owner_user_id,status,service_status,source_type,paid_amount,writing_status) "
            "VALUES (%s,7,'confirmed','active','agent_quote',100,'pending') RETURNING id",
            (brand_id,),
        )
        quote_id = int(cur.fetchone()["id"])
        cur.execute("DROP TABLE confirmed_keywords CASCADE")
        result = plan.enqueue_quote_event_in_transaction_if_enabled(
            cur,
            quote_id,
            "contract_add_on",
        )
        assert result["enqueued"] is False
        assert result["reason"] in {"UndefinedTable", "UndefinedColumn"}
        cur.execute("UPDATE quotes SET brand_name='Commercial Fact Committed' WHERE id=%s", (quote_id,))
        conn.commit()
        cur.execute("SELECT brand_name FROM quotes WHERE id=%s", (quote_id,))
        assert cur.fetchone()["brand_name"] == "Commercial Fact Committed"
    finally:
        conn.close()
