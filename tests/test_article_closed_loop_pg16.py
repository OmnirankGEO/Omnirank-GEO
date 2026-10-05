from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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


def test_fresh_2x_exact_readiness_and_shadow_runtime(monkeypatch):
    schema = _schema("geo_loop_fresh")
    _fresh(schema)
    conn = _connect(schema)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(MIGRATION_SQL)
        from services.article_closed_loop_schema_contract import assert_schema_ready

        assert_schema_ready(cur)
        cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Test Brand',7) RETURNING id")
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO quotes(brand_id,owner_user_id,status,service_status,source_type,paid_amount,writing_status,confirmed_at)
            VALUES (%s,7,'confirmed','active','agent_quote',100,'pending',NOW()) RETURNING id
            """,
            (brand_id,),
        )
        quote_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO confirmed_keywords(quote_id,keyword,required_articles,monitoring_query) VALUES (%s,'细胞治疗药物研发和生产隔离器推荐',2,'原始计费监测短句')",
            (quote_id,),
        )
        cur.execute(
            "INSERT INTO confirmed_keywords(quote_id,keyword,required_articles) VALUES (%s,'隔离器选型证据',1)",
            (quote_id,),
        )
        cur.execute(
            "INSERT INTO keyword_selection_sessions(quote_id,status,confirmed_at,payment_received_at,updated_at) VALUES (%s,'active',NOW()::text,NOW()::text,NOW()::text)",
            (quote_id,),
        )
    finally:
        conn.close()

    import services.article_delivery_plan as plan

    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_RECONCILER_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_SHADOW_ENABLED", "true")
    monkeypatch.setattr(plan, "_get_connection", lambda: _connect(schema))

    def enqueue_once(_number: int):
        return plan.enqueue_quote_event_durable(quote_id, "quote_paid_standard")

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(enqueue_once, range(20)))
    assert all(item["enqueued"] for item in results)

    status = plan.dispatch_plan_events(limit=20)
    assert status == {"claimed": 1, "completed": 1, "failed": 0}
    replay = plan.dispatch_plan_events(limit=20)
    assert replay == {"claimed": 0, "completed": 0, "failed": 0}

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_plan_outbox")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_plan_runs WHERE status='completed'")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_delivery_slots WHERE current_state='active'")
        assert cur.fetchone()["n"] == 3
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_delivery_slot_events")
        assert cur.fetchone()["n"] == 3
        cur.execute("SELECT COUNT(*) AS n FROM topics")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT output_snapshot,comparison_snapshot FROM geo_article_plan_runs")
        run = cur.fetchone()
        assert run["output_snapshot"]["automatic_title_generation"] is False
        assert run["output_snapshot"]["automatic_article_generation"] is False
        assert run["output_snapshot"]["automatic_billing"] is False
        assert run["comparison_snapshot"]["canary_verdict"] == "INSUFFICIENT_SAMPLES"
        assert all("原始计费监测短句" not in str(slot) for slot in run["output_snapshot"]["slots"])
    finally:
        conn.close()


def test_shadow_recompile_refuses_unbacked_delivery_decrease(monkeypatch):
    """A smaller snapshot is not authority to cancel contracted delivery.

    Refund/cancel/bonus events were removed after production readback found no
    authoritative quantity detail.  The compiler must therefore preserve the
    current projection and fail closed instead of inventing a cancellation.
    """
    schema = _schema("geo_loop_decrease")
    _fresh(schema)
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Decrease Guard',7) RETURNING id")
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO quotes(brand_id,owner_user_id,status,service_status,source_type,paid_amount,writing_status,confirmed_at)
            VALUES (%s,7,'confirmed','active','agent_quote',100,'pending',NOW()) RETURNING id
            """,
            (brand_id,),
        )
        quote_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO confirmed_keywords(quote_id,keyword,required_articles,is_core) VALUES (%s,'守卫测试',2,TRUE) RETURNING id",
            (quote_id,),
        )
        keyword_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO keyword_selection_sessions(quote_id,status,confirmed_at,payment_received_at,updated_at) "
            "VALUES (%s,'active',NOW()::text,NOW()::text,NOW()::text)",
            (quote_id,),
        )
        conn.commit()
    finally:
        conn.close()

    import services.article_delivery_plan as plan

    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_RECONCILER_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_SHADOW_ENABLED", "true")
    monkeypatch.setattr(plan, "_get_connection", lambda: _connect(schema))
    assert plan.enqueue_quote_event_durable(quote_id, "quote_paid_standard")["enqueued"]
    assert plan.dispatch_plan_events(limit=10) == {"claimed": 1, "completed": 1, "failed": 0}

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE confirmed_keywords SET required_articles=1 WHERE id=%s", (keyword_id,))
        conn.commit()
    finally:
        conn.close()

    assert plan.enqueue_quote_event_durable(quote_id, "keyword_reassigned")["enqueued"]
    assert plan.dispatch_plan_events(limit=10) == {"claimed": 1, "completed": 0, "failed": 1}
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT current_state,COUNT(*) AS n FROM geo_article_delivery_slots GROUP BY current_state"
        )
        assert cur.fetchall() == [{"current_state": "active", "n": 2}]
        cur.execute(
            "SELECT status,last_error FROM geo_article_plan_outbox WHERE event_kind='keyword_reassigned'"
        )
        failed = cur.fetchone()
        assert failed["status"] == "pending"
        assert "delivery_count_decrease_requires_authoritative_cancel_or_refund_detail" in failed["last_error"]
    finally:
        conn.close()


def test_contract_recompile_preserves_blocked_slot_until_explicit_resolution(monkeypatch):
    schema = _schema("geo_loop_blocked_recompile")
    _fresh(schema)
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Blocked Guard',7) RETURNING id")
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO quotes(brand_id,owner_user_id,status,service_status,source_type,paid_amount,writing_status) "
            "VALUES (%s,7,'confirmed','active','agent_quote',100,'pending') RETURNING id",
            (brand_id,),
        )
        quote_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO confirmed_keywords(quote_id,keyword,required_articles,is_core) "
            "VALUES (%s,'阻断状态继承',1,TRUE) RETURNING id",
            (quote_id,),
        )
        keyword_id = int(cur.fetchone()["id"])
        conn.commit()
    finally:
        conn.close()

    import services.article_delivery_plan as plan
    from services.article_closed_loop_metadata import set_slot_blocked

    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_RECONCILER_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_SHADOW_ENABLED", "true")
    monkeypatch.setattr(plan, "_get_connection", lambda: _connect(schema))
    assert plan.enqueue_quote_event_durable(quote_id, "contract_add_on")["enqueued"]
    assert plan.dispatch_plan_events(limit=10) == {"claimed": 1, "completed": 1, "failed": 0}

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("SELECT delivery_slot_key FROM geo_article_delivery_slots WHERE quote_id=%s", (quote_id,))
        slot_key = str(cur.fetchone()["delivery_slot_key"])
        set_slot_blocked(
            cur,
            delivery_slot_key=slot_key,
            reason_code="missing_material",
            user_message="需要客户补充材料",
            owner_kind="agent",
            next_action="联系客户补充材料",
            target_resolution_at=datetime.now(timezone.utc) + timedelta(days=2),
            actor_user_id=7,
        )
        cur.execute("UPDATE confirmed_keywords SET monitoring_query='合同版本二的监测短句' WHERE id=%s", (keyword_id,))
        conn.commit()
    finally:
        conn.close()

    assert plan.enqueue_quote_event_durable(quote_id, "keyword_reassigned")["enqueued"]
    assert plan.dispatch_plan_events(limit=10) == {"claimed": 1, "completed": 1, "failed": 0}
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT current_state,blocked_reason_code,blocked_user_message,owner_kind,next_action "
            "FROM geo_article_delivery_slots WHERE delivery_slot_key=%s",
            (slot_key,),
        )
        assert cur.fetchone() == {
            "current_state": "blocked",
            "blocked_reason_code": "missing_material",
            "blocked_user_message": "需要客户补充材料",
            "owner_kind": "agent",
            "next_action": "联系客户补充材料",
        }
        cur.execute(
            "SELECT event_kind,target_state FROM geo_article_delivery_slot_events "
            "WHERE delivery_slot_key=%s ORDER BY slot_version DESC LIMIT 1",
            (slot_key,),
        )
        assert cur.fetchone() == {"event_kind": "reassigned", "target_state": "blocked"}
    finally:
        conn.close()


def test_existing_nullable_metadata_default_null_is_canonicalized():
    schema = _schema("geo_loop_default_null")
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", schema))
        cur.execute(f"SET search_path TO {schema},public")
        cur.execute(
            "ALTER TABLE quotes "
            "ADD COLUMN article_plan_writing_mode CHARACTER VARYING(32) DEFAULT NULL, "
            "ADD COLUMN article_plan_contract_version CHARACTER VARYING(80) DEFAULT NULL"
        )
        cur.execute(
            "ALTER TABLE topics "
            "ADD COLUMN article_plan_metadata_version CHARACTER VARYING(80) DEFAULT NULL"
        )
        cur.execute(
            "ALTER TABLE articles "
            "ADD COLUMN article_revision_key CHARACTER(64) DEFAULT NULL"
        )

        cur.execute(MIGRATION_SQL)
        cur.execute(MIGRATION_SQL)

        from services.article_closed_loop_schema_contract import assert_schema_ready

        assert_schema_ready(cur)
        cur.execute(
            """
            SELECT c.relname, a.attname
              FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE n.nspname = %s
               AND (c.relname, a.attname) IN (
                   ('quotes', 'article_plan_writing_mode'),
                   ('quotes', 'article_plan_contract_version'),
                   ('topics', 'article_plan_metadata_version'),
                   ('articles', 'article_revision_key')
               )
            """,
            (schema,),
        )
        assert cur.fetchall() == []
    finally:
        conn.close()


def test_half_created_empty_table_is_repaired_but_wrong_type_fails_readiness():
    schema = _schema("geo_loop_half")
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", schema))
        cur.execute(f"SET search_path TO {schema},public")
        cur.execute("CREATE TABLE geo_article_plan_outbox (id BIGSERIAL PRIMARY KEY)")
        cur.execute(MIGRATION_SQL)
        from services.article_closed_loop_schema_contract import assert_schema_ready

        assert_schema_ready(cur)
    finally:
        conn.close()

    bad_schema = _schema("geo_loop_bad_type")
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", bad_schema))
        cur.execute(f"SET search_path TO {bad_schema},public")
        cur.execute("CREATE TABLE geo_article_plan_outbox (id BIGSERIAL PRIMARY KEY,event_key INTEGER)")
        cur.execute(MIGRATION_SQL)
        from services.article_closed_loop_schema_contract import schema_blockers

        assert any(item.startswith("wrong_type:geo_article_plan_outbox.event_key") for item in schema_blockers(cur))
    finally:
        conn.close()


def test_decoy_same_name_index_and_constraint_fail_closed():
    schema = _schema("geo_loop_decoy")
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", schema))
        cur.execute(f"SET search_path TO {schema},public")
        cur.execute("CREATE TABLE decoy(status VARCHAR(32),available_at TIMESTAMPTZ,id BIGINT,event_kind VARCHAR(64))")
        cur.execute("CREATE INDEX idx_geo_article_plan_outbox_due ON decoy(status,available_at,id)")
        cur.execute("ALTER TABLE decoy ADD CONSTRAINT ck_geo_article_plan_outbox_kind CHECK (event_kind='quote_paid_standard')")
        cur.execute(MIGRATION_SQL)
        from services.article_closed_loop_schema_contract import schema_blockers

        blockers = schema_blockers(cur)
        assert any(item.startswith("wrong_index_identity:idx_geo_article_plan_outbox_due") for item in blockers)
        assert any(item.startswith("wrong_constraint_identity:ck_geo_article_plan_outbox_kind") for item in blockers)
    finally:
        conn.close()


def test_exact_readiness_rejects_weakened_columns_checks_fks_and_partial_indexes():
    schema = _schema("geo_loop_weakened")
    _fresh(schema)
    conn = _connect(schema)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("ALTER TABLE geo_article_plan_outbox ALTER COLUMN event_key DROP NOT NULL")
        cur.execute("ALTER TABLE geo_article_plan_outbox DROP CONSTRAINT ck_geo_article_plan_outbox_status")
        cur.execute(
            "ALTER TABLE geo_article_plan_outbox ADD CONSTRAINT ck_geo_article_plan_outbox_status "
            "CHECK (status IS NOT NULL) NOT VALID"
        )
        cur.execute("ALTER TABLE geo_article_plan_outbox DROP CONSTRAINT fk_geo_article_plan_outbox_quote")
        cur.execute(
            "ALTER TABLE geo_article_plan_outbox ADD CONSTRAINT fk_geo_article_plan_outbox_quote "
            "FOREIGN KEY (brand_id) REFERENCES brands(id)"
        )
        cur.execute("DROP INDEX uq_topics_delivery_slot_key")
        cur.execute("CREATE INDEX uq_topics_delivery_slot_key ON topics (delivery_slot_key)")

        from services.article_closed_loop_schema_contract import schema_blockers

        blockers = schema_blockers(cur)
        assert any(item.startswith("wrong_nullability:geo_article_plan_outbox.event_key") for item in blockers)
        assert any(item.startswith("wrong_constraint_validated:ck_geo_article_plan_outbox_status") for item in blockers)
        assert any(item.startswith("wrong_constraint_definition:ck_geo_article_plan_outbox_status") for item in blockers)
        assert any(item.startswith("wrong_constraint_identity:fk_geo_article_plan_outbox_quote") for item in blockers)
        assert any(item.startswith("wrong_constraint_reference:fk_geo_article_plan_outbox_quote") for item in blockers)
        assert any(item.startswith("wrong_index_identity:uq_topics_delivery_slot_key") for item in blockers)
        assert any(item.startswith("wrong_index_predicate:uq_topics_delivery_slot_key") for item in blockers)
    finally:
        conn.close()


def test_unleased_main_reset_is_absent_and_canonical_recovery_has_guards():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "UPDATE topics SET status='pending' WHERE status='writing'" not in source
    canonical = (
        '"UPDATE topics SET status = \'pending\' "\n'
        '        "WHERE status = \'writing\' "\n'
        '        "  AND article_id IS NULL "'
    )
    assert canonical in source
    assert "writing_started_at < NOW() - INTERVAL '90 minutes'" in source


def test_rollback_then_forward_and_claim_lease_recovery_are_idempotent(monkeypatch):
    schema = _schema("geo_loop_recovery")
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", schema))
        cur.execute(f"SET search_path TO {schema},public")
        cur.execute("BEGIN")
        cur.execute(MIGRATION_SQL)
        cur.execute("ROLLBACK")
        cur.execute("SELECT to_regclass('geo_article_plan_outbox') AS relation")
        assert cur.fetchone()[0] is None
        cur.execute(MIGRATION_SQL)
    finally:
        conn.close()

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Recovery Brand',7) RETURNING id")
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO quotes(brand_id,owner_user_id,status,service_status,source_type,paid_amount,writing_status,confirmed_at)
            VALUES (%s,7,'confirmed','active','agent_quote',100,'pending',NOW()) RETURNING id
            """,
            (brand_id,),
        )
        quote_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO confirmed_keywords(quote_id,keyword,required_articles,is_core) VALUES (%s,'恢复测试',1,TRUE)",
            (quote_id,),
        )
        cur.execute(
            "INSERT INTO keyword_selection_sessions(quote_id,status,confirmed_at,payment_received_at,updated_at) "
            "VALUES (%s,'active',NOW()::text,NOW()::text,NOW()::text)",
            (quote_id,),
        )
        conn.commit()
    finally:
        conn.close()

    import services.article_delivery_plan as plan

    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_RECONCILER_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_SHADOW_ENABLED", "true")
    monkeypatch.setattr(plan, "_get_connection", lambda: _connect(schema))
    assert plan.enqueue_quote_event_durable(quote_id, "quote_paid_standard")["enqueued"]
    claim_token, rows = plan.claim_plan_events(limit=1)
    assert claim_token and len(rows) == 1

    # Simulate kill -9 after claim: no completion callback runs.  An expired
    # durable lease must be reclaimable without a duplicate plan or slot.
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE geo_article_plan_outbox SET claimed_at=NOW()-INTERVAL '10 minutes' WHERE id=%s",
            (rows[0]["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    assert plan.dispatch_plan_events(limit=10) == {"claimed": 1, "completed": 1, "failed": 0}
    assert plan.dispatch_plan_events(limit=10) == {"claimed": 0, "completed": 0, "failed": 0}
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_plan_runs")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_delivery_slots")
        assert cur.fetchone()["n"] == 1
    finally:
        conn.close()
