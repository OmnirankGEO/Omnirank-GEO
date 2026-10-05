from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("MONITORING_SCHEDULER_PG_TEST_URL")


def test_pg16_constraint_normalizer_accepts_dump_restore_varchar_array_casts():
    from db.monitoring_db import _normalize_pg16_constraint_definition

    direct = (
        "CHECK (state::text = ANY (ARRAY['queued'::character varying, "
        "'running'::character varying]::text[]))"
    )
    restored = (
        direct
        .replace("::character varying", "::character varying::text")
        .replace("]::text[]", "]")
    )
    assert _normalize_pg16_constraint_definition(restored) == (
        _normalize_pg16_constraint_definition(direct)
    )
    assert _normalize_pg16_constraint_definition(
        restored.replace("'running'", "'failed'")
    ) != direct


@pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")
def test_monitoring_cell_retry_real_postgres_contract(monkeypatch):
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    database_name = f"monitor_cell_retry_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        parsed = urlsplit(PG_URL)
        target_url = urlunsplit(
            (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
        )

        def connect():
            return psycopg2.connect(target_url, cursor_factory=RealDictCursor)

        monkeypatch.setenv("DATABASE_URL", target_url)
        monkeypatch.setenv("TEST_DATABASE_URL", target_url)
        from db import connection as connection_db
        monkeypatch.setattr(connection_db, "DATABASE_URL", target_url)
        monkeypatch.setattr(connection_db, "_pool", None)
        from db import diagnosis_db, monitoring_db
        monkeypatch.setattr(diagnosis_db, "get_connection", connect)
        monkeypatch.setattr(monitoring_db, "get_connection", connect)

        diagnosis_db.init_db()
        monitoring_db.init_monitoring_tables()
        migration_files = (
            "migration_monitoring_yuanbao_default_2026_07_20.sql",
            "migration_monitoring_product_matrix_2026_07_21.sql",
            "migration_monitoring_identity_review_2026_07_21.sql",
            "migration_monitoring_cell_retry_2026_07_21.sql",
        )
        conn = connect()
        with conn, conn.cursor() as cursor:
            # The production manifest applies organization schema first.  This
            # focused PG fixture keeps only the durable columns referenced by
            # the monitoring refund fence.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.organization_charge_links(
                    id BIGSERIAL PRIMARY KEY,
                    status TEXT NOT NULL,
                    physical_backend TEXT NOT NULL,
                    physical_freeze_id TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.point_freezes(
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    feature_code TEXT NOT NULL,
                    amount_total BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    task_ref TEXT,
                    brand_id INTEGER
                )
                """
            )
            for filename in migration_files:
                cursor.execute((ROOT / "scripts" / filename).read_text(encoding="utf-8"))
            # Idempotent forward migration and exact startup guard.
            cursor.execute((ROOT / "scripts" / migration_files[-1]).read_text(encoding="utf-8"))
            monitoring_db.assert_monitoring_cell_retry_ready(cursor)
            cursor.execute(
                "ALTER TABLE public.monitoring_run_cells ALTER COLUMN attempt_count DROP DEFAULT"
            )
            with pytest.raises(RuntimeError, match="column definitions drifted"):
                monitoring_db.assert_monitoring_cell_retry_ready(cursor)
            cursor.execute(
                "ALTER TABLE public.monitoring_run_cells "
                "ALTER COLUMN attempt_count SET DEFAULT 0"
            )
            monitoring_db.assert_monitoring_cell_retry_ready(cursor)

            cursor.execute("INSERT INTO brands(name) VALUES ('cell owner') RETURNING id")
            brand_id = int(cursor.fetchone()["id"])
            cursor.execute("INSERT INTO brands(name) VALUES ('other tenant') RETURNING id")
            other_brand_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS public.roles(id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS public.user_roles(user_id INTEGER NOT NULL, role_id INTEGER NOT NULL)"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS public.user_clients(user_id INTEGER NOT NULL, brand_id INTEGER NOT NULL)"
            )
            cursor.execute(
                "ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS brand_display_names TEXT"
            )
            cursor.execute(
                "ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.client_profiles(
                    brand_id INTEGER,
                    brand_display_names TEXT,
                    is_deleted INTEGER DEFAULT 0,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.brand_aliases(
                    brand_id INTEGER,
                    canonical_name TEXT,
                    alias TEXT,
                    source TEXT
                )
                """
            )
            cursor.execute(
                "INSERT INTO public.roles(id,name) VALUES (1,'admin') ON CONFLICT (id) DO NOTHING"
            )
            cursor.execute("INSERT INTO public.user_roles(user_id,role_id) VALUES (999,1)")
            cursor.execute(
                """
                INSERT INTO quotes(brand_id,brand_name,status,paid_at)
                VALUES (%s,'cell owner','paid',NOW()) RETURNING id
                """,
                (brand_id,),
            )
            quote_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO quotes(brand_id,brand_name,status,paid_at)
                VALUES (%s,'other tenant','paid',NOW()) RETURNING id
                """,
                (other_brand_id,),
            )
            other_quote_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "INSERT INTO confirmed_keywords(quote_id,keyword) VALUES (%s,'single cell phrase') RETURNING id",
                (quote_id,),
            )
            keyword_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "INSERT INTO confirmed_keywords(quote_id,keyword) "
                "VALUES (%s,'other tenant phrase') RETURNING id",
                (other_quote_id,),
            )
            other_keyword_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO keyword_monitor_subscriptions
                    (user_id,keyword_id,quote_id,brand_id,status,feature_code)
                VALUES (4242,%s,%s,%s,'active','monitoring_keyword_daily')
                RETURNING id
                """,
                (keyword_id, quote_id, brand_id),
            )
            subscription_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO extra_keywords
                    (quote_id,client_id,brand_id,keyword,target_brand)
                VALUES (%s,%s,%s,'cross tenant extra','cell owner')
                RETURNING id
                """,
                (quote_id, str(quote_id), other_brand_id),
            )
            cross_tenant_extra_id = int(cursor.fetchone()["id"])
        conn.commit()

        task_id = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[keyword_id],
            trigger_type="manual_sse",
            planned_test_count=2,
            planned_platform_count=2,
        )
        cells = monitoring_db.create_monitoring_run_cells(
            task_id=task_id,
            brand_id=brand_id,
            keywords=[{
                "id": keyword_id,
                "quote_id": quote_id,
                "keyword": "single cell phrase",
                "monitoring_query": "single cell question",
                "target_brand": "cell owner",
                "source": "confirmed",
                "entitlement_platforms": "dashscope,deepseek,kimi,doubao",
                "_eligible_monitoring_platforms": ["dashscope", "deepseek"],
            }],
            search_mode="enhanced",
            fulfillment_credential=str(uuid.uuid4()),
            fulfillment_state="covered",
            settlement_reference="monitor_stream_test_reference",
            retry_coverage={
                "policy_version": "monitoring-retry-v1",
                "coverage": "included",
                "max_attempts": 3,
            },
        )
        assert [(row["platform"], row["state"]) for row in cells] == [
            ("dashscope", "queued"), ("deepseek", "queued"),
            ("kimi", "unavailable"), ("doubao", "unavailable"),
        ]
        assert {row["settlement_reference"] for row in cells} == {
            "monitor_stream_test_reference"
        }
        wrong_keyword_task = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[other_keyword_id],
            trigger_type="manual_user",
            planned_test_count=1,
            planned_platform_count=1,
        )
        with pytest.raises(monitoring_db.MonitoringCellConflict):
            monitoring_db.create_monitoring_run_cells(
                task_id=wrong_keyword_task,
                brand_id=brand_id,
                keywords=[{
                    "id": other_keyword_id,
                    "quote_id": quote_id,
                    "keyword": "other tenant phrase",
                    "target_brand": "cell owner",
                    "source": "confirmed",
                    "entitlement_platforms": "dashscope",
                    "_eligible_monitoring_platforms": ["dashscope"],
                }],
                search_mode="enhanced",
                fulfillment_credential=str(uuid.uuid4()),
                fulfillment_state="covered",
                retry_coverage={"coverage": "included", "max_attempts": 1},
            )
        wrong_extra_task = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[cross_tenant_extra_id],
            trigger_type="manual_user",
            planned_test_count=1,
            planned_platform_count=1,
        )
        with pytest.raises(monitoring_db.MonitoringCellConflict):
            monitoring_db.create_monitoring_run_cells(
                task_id=wrong_extra_task,
                brand_id=brand_id,
                keywords=[{
                    "id": cross_tenant_extra_id,
                    "quote_id": quote_id,
                    "keyword": "cross tenant extra",
                    "target_brand": "cell owner",
                    "source": "extra",
                    "entitlement_platforms": "dashscope",
                    "_eligible_monitoring_platforms": ["dashscope"],
                }],
                search_mode="enhanced",
                fulfillment_credential=str(uuid.uuid4()),
                fulfillment_state="covered",
                retry_coverage={"coverage": "included", "max_attempts": 1},
            )

        # A hard-killed admin task has no physical freeze.  Once the whole
        # execution is stale, both the dispatched running cell and its never-
        # claimed queued neighbor must reach durable terminal states.
        abandoned_admin_task = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[keyword_id],
            trigger_type="manual_user",
            planned_test_count=2,
            planned_platform_count=2,
        )
        abandoned_reference = f"batch_mon_admin_{uuid.uuid4().hex}"
        abandoned_cells = monitoring_db.create_monitoring_run_cells(
            task_id=abandoned_admin_task,
            brand_id=brand_id,
            keywords=[{
                "id": keyword_id,
                "quote_id": quote_id,
                "keyword": "single cell phrase",
                "monitoring_query": "single cell question",
                "target_brand": "cell owner",
                "source": "confirmed",
                "entitlement_platforms": "dashscope,deepseek",
                "_eligible_monitoring_platforms": ["dashscope", "deepseek"],
            }],
            search_mode="enhanced",
            fulfillment_credential=str(uuid.uuid4()),
            fulfillment_state="admin_covered",
            settlement_reference=abandoned_reference,
            retry_coverage={"coverage": "included", "max_attempts": 1},
        )
        abandoned_planned = [cell for cell in abandoned_cells if cell["is_planned"]]
        abandoned_claim = monitoring_db.claim_monitoring_run_cell(
            cell_id=abandoned_planned[0]["id"],
            task_id=abandoned_admin_task,
            brand_id=brand_id,
        )
        monitoring_db.mark_monitoring_cell_dispatched(
            cell_id=abandoned_planned[0]["id"],
            claim_token=abandoned_claim["claim_token"],
        )
        abandoned_conn = connect()
        with abandoned_conn, abandoned_conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE monitoring_run_cells
                   SET claim_until=CASE WHEN state='running' THEN NOW()-INTERVAL '2 hours' ELSE claim_until END,
                       updated_at=NOW()-INTERVAL '2 hours'
                 WHERE task_id=%s
                """,
                (abandoned_admin_task,),
            )
        stale = monitoring_db.list_stale_monitoring_task_settlements(1)
        abandoned_row = next(
            row for row in stale if row["settlement_reference"] == abandoned_reference
        )
        assert abandoned_row["execution_abandoned"] is True
        assert abandoned_row["all_admin_covered"] is True
        assert monitoring_db.recover_abandoned_monitoring_task_execution(
            abandoned_admin_task
        ) == 2
        monitoring_db.refresh_monitoring_task_from_cells(abandoned_admin_task)
        recovered_admin_cells = monitoring_db.list_monitoring_run_cells(
            abandoned_admin_task, brand_id=brand_id
        )
        assert [cell["state"] for cell in recovered_admin_cells if cell["is_planned"]] == [
            "pending_provider_confirmation", "failed",
        ]
        abandoned_task_row = monitoring_db.get_task(abandoned_admin_task)
        assert abandoned_task_row["status"] == "completed"

        # A full organization refund is new durable evidence that revokes the
        # original covered retry entitlement.  The generic transition remains
        # forbidden; only the exact linked refunded charge can release it.
        refunded_task = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[keyword_id],
            trigger_type="manual_user",
            planned_test_count=1,
            planned_platform_count=1,
        )
        refunded_reference = f"monitor_org_refund_{uuid.uuid4().hex}"
        refunded_cells = monitoring_db.create_monitoring_run_cells(
            task_id=refunded_task,
            brand_id=brand_id,
            keywords=[{
                "id": keyword_id,
                "quote_id": quote_id,
                "keyword": "single cell phrase",
                "monitoring_query": "single cell question",
                "target_brand": "cell owner",
                "source": "confirmed",
                "entitlement_platforms": "dashscope",
                "_eligible_monitoring_platforms": ["dashscope"],
            }],
            search_mode="enhanced",
            fulfillment_credential=str(uuid.uuid4()),
            fulfillment_state="covered",
            settlement_reference=refunded_reference,
            retry_coverage={"coverage": "included", "max_attempts": 1},
        )
        refunded_cell = next(cell for cell in refunded_cells if cell["is_planned"])
        refunded_claim = monitoring_db.claim_monitoring_run_cell(
            cell_id=refunded_cell["id"], task_id=refunded_task, brand_id=brand_id,
        )
        monitoring_db.finish_monitoring_cell_error(
            cell_id=refunded_cell["id"],
            claim_token=refunded_claim["claim_token"],
            state="failed",
            error_code="definite_provider_rejection",
            error_message="known failure before refund",
        )
        refund_conn = connect()
        with refund_conn, refund_conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO point_freezes
                    (user_id,feature_code,amount_total,status,task_ref,brand_id)
                VALUES (7777,'monitor_single',130,'committed',%s,%s)
                RETURNING id
                """,
                (refunded_reference, brand_id),
            )
            refunded_freeze_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO organization_charge_links
                    (status,physical_backend,physical_freeze_id)
                VALUES ('committed','legacy_user_wallet',%s)
                RETURNING id
                """,
                (str(refunded_freeze_id),),
            )
            refunded_charge_id = int(cursor.fetchone()["id"])
        blocked_transition = connect()
        try:
            with blocked_transition.cursor() as cursor:
                with pytest.raises(psycopg2.Error):
                    cursor.execute(
                        "UPDATE monitoring_run_cells SET fulfillment_state='released' "
                        "WHERE id=%s",
                        (refunded_cell["id"],),
                    )
            blocked_transition.rollback()
        finally:
            blocked_transition.close()
        refund_conn = connect()
        with refund_conn, refund_conn.cursor() as cursor:
            cursor.execute(
                "UPDATE organization_charge_links SET status='refunded' WHERE id=%s",
                (refunded_charge_id,),
            )
        # The money ledger is authoritative immediately; retry must not wait
        # for the hourly fulfillment projection.
        with pytest.raises(
            monitoring_db.MonitoringCellConflict,
            match="organization fulfillment is no longer retryable",
        ):
            monitoring_db.reserve_monitoring_cell_retry(
                task_id=refunded_task,
                cell_id=refunded_cell["id"],
                brand_id=brand_id,
                request_id=str(uuid.uuid4()),
                expected_plan_hash=refunded_cell["plan_hash"],
            )
        refund_candidates = monitoring_db.list_stale_monitoring_task_settlements(999)
        assert any(
            row["settlement_reference"] == refunded_reference
            and row.get("organization_refunded") is True
            for row in refund_candidates
        )
        assert monitoring_db.revoke_monitoring_task_coverage_for_organization_refund(
            refunded_task, refunded_charge_id
        ) == 4
        refunded_after = monitoring_db.list_monitoring_run_cells(
            refunded_task, brand_id=brand_id
        )
        assert {cell["fulfillment_state"] for cell in refunded_after} == {"released"}
        with pytest.raises(
            monitoring_db.MonitoringCellConflict,
            match="organization fulfillment is no longer retryable",
        ):
            monitoring_db.reserve_monitoring_cell_retry(
                task_id=refunded_task,
                cell_id=refunded_cell["id"],
                brand_id=brand_id,
                request_id=str(uuid.uuid4()),
                expected_plan_hash=refunded_cell["plan_hash"],
            )

        settlement_task = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[keyword_id],
            trigger_type="scheduled",
            planned_test_count=1,
            planned_platform_count=1,
        )
        settlement_reference = f"monitoring_daily:{settlement_task}:contract:{keyword_id}:{uuid.uuid4()}"
        settlement_keyword = {
            "id": keyword_id,
            "quote_id": quote_id,
            "keyword": "single cell phrase",
            "monitoring_query": "single cell question",
            "target_brand": "cell owner",
            "source": "contract",
            "user_id": 4242,
            "subscription_id": subscription_id,
            "feature_code": "monitoring_keyword_daily",
            "entitlement_platforms": "dashscope",
            "_eligible_monitoring_platforms": ["dashscope"],
            "_fulfillment_credential": str(uuid.uuid4()),
            "_fulfillment_state": "reserved",
            "_settlement_reference": settlement_reference,
            "_retry_coverage": {"coverage": "included", "max_attempts": 1},
        }
        monitoring_db.create_monitoring_keyword_settlements(
            task_id=settlement_task,
            brand_id=brand_id,
            keywords=[settlement_keyword],
        )
        settlement_cells = monitoring_db.create_monitoring_run_cells(
            task_id=settlement_task,
            brand_id=brand_id,
            keywords=[settlement_keyword],
            search_mode="enhanced",
            fulfillment_credential=str(uuid.uuid4()),
            fulfillment_state="reserved",
        )
        settlement_claim = monitoring_db.claim_subscription_for_today_with_settlement(
            subscription_id, settlement_reference
        )
        assert settlement_claim is not None
        monitoring_db.record_monitoring_keyword_settlement_freeze(
            settlement_reference,
            {"freeze_id": 8123, "freeze_table": "legacy", "amount": 130},
        )
        settlement_cell = next(cell for cell in settlement_cells if cell["is_planned"])
        cell_claim = monitoring_db.claim_monitoring_run_cell(
            cell_id=settlement_cell["id"],
            task_id=settlement_task,
            brand_id=brand_id,
        )
        monitoring_db.mark_monitoring_cell_dispatched(
            cell_id=settlement_cell["id"], claim_token=cell_claim["claim_token"]
        )
        monitoring_db.mark_monitoring_keyword_settlement_dispatched(settlement_reference)
        monitoring_db.finish_monitoring_cell_error(
            cell_id=settlement_cell["id"],
            claim_token=cell_claim["claim_token"],
            state="failed",
            error_code="definite_provider_rejection",
            error_message="known failure",
        )
        monitoring_db.settle_monitoring_keyword_reference(
            settlement_reference, "committed"
        )
        assert monitoring_db.record_monitoring_subscription_charge_for_settlement(
            settlement_reference
        ) is True
        assert monitoring_db.record_monitoring_subscription_charge_for_settlement(
            settlement_reference
        ) is False
        settlement_check = connect()
        with settlement_check, settlement_check.cursor() as cursor:
            cursor.execute(
                "SELECT state,charge_recorded FROM monitoring_keyword_settlements "
                "WHERE settlement_reference=%s",
                (settlement_reference,),
            )
            assert cursor.fetchone() == {"state": "committed", "charge_recorded": True}
            cursor.execute(
                "SELECT total_charged,last_charge_amount FROM keyword_monitor_subscriptions "
                "WHERE id=%s",
                (subscription_id,),
            )
            assert cursor.fetchone() == {"total_charged": 130, "last_charge_amount": 130}
        dashscope = next(row for row in cells if row["platform"] == "dashscope")
        deepseek = next(row for row in cells if row["platform"] == "deepseek")

        tamper = connect()
        tamper.autocommit = True
        try:
            with pytest.raises(psycopg2.Error):
                with tamper.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO monitoring_cell_retry_requests
                            (request_id,cell_id,task_id,brand_id,plan_hash,params_hash)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            str(uuid.uuid4()), dashscope["id"], task_id, other_brand_id,
                            dashscope["plan_hash"], "f" * 64,
                        ),
                    )
        finally:
            tamper.close()

        # DB-level task/cell tenant identity cannot diverge even if a future
        # writer bypasses the normal plan helper.
        tenant_tamper = connect()
        try:
            with tenant_tamper.cursor() as cursor:
                with pytest.raises(psycopg2.Error):
                    cursor.execute(
                        "UPDATE monitoring_tasks SET brand_id=%s WHERE id=%s",
                        (other_brand_id, task_id),
                    )
            tenant_tamper.rollback()
        finally:
            tenant_tamper.close()

        initial = monitoring_db.claim_monitoring_run_cell(
            cell_id=dashscope["id"], task_id=task_id, brand_id=brand_id,
        )
        monitoring_db.finish_monitoring_cell_error(
            cell_id=dashscope["id"], claim_token=initial["claim_token"],
            state="failed", error_code="definite_provider_rejection",
            error_message="known response",
        )

        request_id = str(uuid.uuid4())
        reservation = monitoring_db.reserve_monitoring_cell_retry(
            task_id=task_id, cell_id=dashscope["id"], brand_id=brand_id,
            request_id=request_id, expected_plan_hash=dashscope["plan_hash"],
        )
        replay = monitoring_db.reserve_monitoring_cell_retry(
            task_id=task_id, cell_id=dashscope["id"], brand_id=brand_id,
            request_id=request_id, expected_plan_hash=dashscope["plan_hash"],
        )
        assert reservation["replay"] is False
        assert replay["replay"] is True
        assert replay["request"]["claim_token"] == reservation["request"]["claim_token"]

        # Same key + different parameters is rejected and cannot move another tenant/cell.
        with pytest.raises(monitoring_db.MonitoringCellConflict, match="different parameters"):
            monitoring_db.reserve_monitoring_cell_retry(
                task_id=task_id, cell_id=deepseek["id"], brand_id=brand_id,
                request_id=request_id, expected_plan_hash=deepseek["plan_hash"],
            )
        with pytest.raises(monitoring_db.MonitoringCellNotFound):
            monitoring_db.reserve_monitoring_cell_retry(
                task_id=task_id, cell_id=dashscope["id"], brand_id=other_brand_id,
                request_id=str(uuid.uuid4()), expected_plan_hash=dashscope["plan_hash"],
            )

        monitoring_db.mark_monitoring_cell_dispatched(
            cell_id=dashscope["id"], claim_token=reservation["request"]["claim_token"]
        )
        result_id = monitoring_db.save_monitoring_result(
            task_id=task_id, keyword_id=keyword_id, keyword="single cell phrase",
            platform="dashscope", is_detected=True, mention_type="direct",
            response_snippet="visible", full_response="cell owner is visible",
            lineage={
                "sent_question_snapshot": "single cell question",
                "keyword_source": "confirmed", "keyword_type": "monitoring",
                "keyword_resolver_status": "resolved", "provider": "dashscope",
                "model": "qwen3-max", "model_revision": "unknown",
                "surface": "ai_search", "search_mode": "forced_search",
                "response_status": "success", "target_brand": "cell owner",
                "target_outcome": "mentioned",
            },
            cell_id=dashscope["id"],
            cell_claim_token=reservation["request"]["claim_token"],
        )
        # The provider result and immutable cell succeeded, but the HTTP response
        # was lost before the request row was finalized. Replaying the same key
        # repairs the request from the durable cell without another provider call.
        response_lost_replay = monitoring_db.reserve_monitoring_cell_retry(
            task_id=task_id, cell_id=dashscope["id"], brand_id=brand_id,
            request_id=request_id, expected_plan_hash=dashscope["plan_hash"],
        )
        assert response_lost_replay["replay"] is True
        assert response_lost_replay["request"]["status"] == "succeeded"
        assert response_lost_replay["request"]["response_snapshot"]["result_id"] == result_id
        monitoring_db.complete_monitoring_cell_retry_request(
            request_id=request_id,
            claim_token=reservation["request"]["claim_token"],
            status="succeeded",
            response_snapshot={"state": "succeeded", "result_id": result_id},
        )
        confirmed_stats = monitoring_db.get_keyword_stats(keyword_id)
        assert confirmed_stats["total_tests"] == 1
        assert confirmed_stats["detected_count"] == 1
        synced_trend = monitoring_db.sync_task_trends(task_id)
        assert synced_trend["success"] is True
        assert synced_trend["synced_keywords"] == 1
        trend_rows = monitoring_db.get_keyword_trend(
            keyword_id, keyword_source="confirmed", limit=1
        )
        assert trend_rows and trend_rows[0]["test_count"] == 1
        with pytest.raises(monitoring_db.MonitoringCellConflict, match="cannot be retried"):
            monitoring_db.reserve_monitoring_cell_retry(
                task_id=task_id, cell_id=dashscope["id"], brand_id=brand_id,
                request_id=str(uuid.uuid4()), expected_plan_hash=dashscope["plan_hash"],
            )

        # Human identity confirmation closes both the result review and its
        # durable cell; the same successful result remains immutable.
        identity_task_id = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[keyword_id],
            trigger_type="manual_sse",
            planned_test_count=1,
            planned_platform_count=1,
        )
        identity_cells = monitoring_db.create_monitoring_run_cells(
            task_id=identity_task_id,
            brand_id=brand_id,
            keywords=[{
                "id": keyword_id,
                "quote_id": quote_id,
                "keyword": "single cell phrase",
                "monitoring_query": "single cell question",
                "target_brand": "cell owner",
                "source": "confirmed",
                "entitlement_platforms": "dashscope",
                "_eligible_monitoring_platforms": ["dashscope"],
            }],
            search_mode="enhanced",
            fulfillment_credential=str(uuid.uuid4()),
            fulfillment_state="covered",
            retry_coverage={
                "policy_version": "monitoring-retry-v1",
                "coverage": "included",
                "max_attempts": 1,
            },
        )
        identity_cell = next(cell for cell in identity_cells if cell["is_planned"])
        identity_claim = monitoring_db.claim_monitoring_run_cell(
            cell_id=identity_cell["id"], task_id=identity_task_id, brand_id=brand_id,
        )
        monitoring_db.mark_monitoring_cell_dispatched(
            cell_id=identity_cell["id"], claim_token=identity_claim["claim_token"]
        )
        with pytest.raises(monitoring_db.MonitoringCellConflict, match="lineage mismatch"):
            monitoring_db.save_monitoring_result(
                task_id=identity_task_id,
                keyword_id=keyword_id,
                keyword="single cell phrase",
                platform="dashscope",
                is_detected=False,
                lineage={
                    "sent_question_snapshot": "single cell question",
                    "keyword_source": "extra",
                    "target_brand": "cell owner",
                },
                cell_id=identity_cell["id"],
                cell_claim_token=identity_claim["claim_token"],
            )
        identity_result_id = monitoring_db.save_monitoring_result(
            task_id=identity_task_id,
            keyword_id=keyword_id,
            keyword="single cell phrase",
            platform="dashscope",
            is_detected=False,
            mention_type="pending_identity",
            response_snippet="possible alias",
            full_response="CellOwnerAlias appears in the response",
            lineage={
                "sent_question_snapshot": "single cell question",
                "keyword_source": "confirmed",
                "keyword_type": "monitoring",
                "keyword_resolver_status": "resolved",
                "provider": "dashscope",
                "model": "qwen3-max",
                "model_revision": "unknown",
                "surface": "ai_search",
                "search_mode": "forced_search",
                "response_status": "brand_identity_unresolved",
                "target_brand": "cell owner",
                "target_outcome": "entity_ambiguous",
            },
            identity_brand_id=brand_id,
            identity_candidates=["CellOwnerAlias"],
            identity_evidence_snippet="CellOwnerAlias",
            identity_review_state="pending",
            cell_id=identity_cell["id"],
            cell_claim_token=identity_claim["claim_token"],
        )
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT identity_evidence_hash FROM monitoring_results WHERE id=%s",
                (identity_result_id,),
            )
            identity_hash = cursor.fetchone()["identity_evidence_hash"]
        identity_decision = monitoring_db.decide_monitoring_identity_review(
            result_id=identity_result_id,
            brand_id=brand_id,
            actor_user_id=999,
            action="yes",
            selected_name="CellOwnerAlias",
            expected_version=0,
            evidence_hash=identity_hash,
            request_id=str(uuid.uuid4()),
        )
        assert identity_decision["review_state"] == "confirmed"
        resolved_identity_cells = monitoring_db.list_monitoring_run_cells(
            identity_task_id, brand_id=brand_id
        )
        assert next(
            cell for cell in resolved_identity_cells if cell["id"] == identity_cell["id"]
        )["state"] == "succeeded"
        composite_tamper = connect()
        try:
            with composite_tamper.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE monitoring_run_cells DISABLE TRIGGER "
                    "trg_monitoring_run_cell_terminal_overwrite"
                )
                with pytest.raises(psycopg2.Error):
                    cursor.execute(
                        "UPDATE monitoring_run_cells SET quote_id=%s WHERE id=%s",
                        (other_quote_id, identity_cell["id"]),
                    )
            composite_tamper.rollback()
            with composite_tamper.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE monitoring_run_cells DISABLE TRIGGER "
                    "trg_monitoring_run_cell_terminal_overwrite"
                )
                with pytest.raises(psycopg2.Error):
                    cursor.execute(
                        "UPDATE monitoring_run_cells SET result_id=%s WHERE id=%s",
                        (result_id, identity_cell["id"]),
                    )
            composite_tamper.rollback()
        finally:
            composite_tamper.close()

        # A worker lost before dispatch is safely reclaimable after lease expiry.
        pre_dispatch = monitoring_db.claim_monitoring_run_cell(
            cell_id=deepseek["id"], task_id=task_id, brand_id=brand_id,
        )
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE monitoring_run_cells SET claim_until=NOW()-INTERVAL '1 second' WHERE id=%s",
                (deepseek["id"],),
            )
        recovered = monitoring_db.list_monitoring_run_cells(task_id, brand_id=brand_id)
        recovered_deepseek = next(row for row in recovered if row["id"] == deepseek["id"])
        assert recovered_deepseek["state"] == "failed"
        assert recovered_deepseek["error_code"] == "worker_lost_before_dispatch"
        assert recovered_deepseek["retry_count"] == 0
        # Two workers / duplicate clicks racing the same idempotency key share
        # one claim. Reserving alone does not consume retry coverage.
        concurrent_request_id = str(uuid.uuid4())
        def reserve_same_request(_index):
            return monitoring_db.reserve_monitoring_cell_retry(
                task_id=task_id, cell_id=deepseek["id"], brand_id=brand_id,
                request_id=concurrent_request_id, expected_plan_hash=deepseek["plan_hash"],
            )
        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent = list(executor.map(reserve_same_request, range(2)))
        assert sorted(item["replay"] for item in concurrent) == [False, True]
        assert len({item["request"]["claim_token"] for item in concurrent}) == 1
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT retry_count FROM monitoring_run_cells WHERE id=%s",
                (deepseek["id"],),
            )
            assert cursor.fetchone()["retry_count"] == 0
            cursor.execute(
                "UPDATE monitoring_run_cells SET claim_until=NOW()-INTERVAL '1 second' WHERE id=%s",
                (deepseek["id"],),
            )
        recovered_retry = monitoring_db.list_monitoring_run_cells(task_id, brand_id=brand_id)
        assert next(row for row in recovered_retry if row["id"] == deepseek["id"])["state"] == "failed"
        lost_response = monitoring_db.reserve_monitoring_cell_retry(
            task_id=task_id, cell_id=deepseek["id"], brand_id=brand_id,
            request_id=concurrent_request_id, expected_plan_hash=deepseek["plan_hash"],
        )
        assert lost_response["replay"] is True
        assert lost_response["request"]["status"] == "failed"
        assert lost_response["request"]["response_snapshot"]["error_code"] == "worker_lost_before_dispatch"
        reclaimed = monitoring_db.reserve_monitoring_cell_retry(
            task_id=task_id, cell_id=deepseek["id"], brand_id=brand_id,
            request_id=str(uuid.uuid4()), expected_plan_hash=deepseek["plan_hash"],
        )
        monitoring_db.mark_monitoring_cell_dispatched(
            cell_id=deepseek["id"], claim_token=reclaimed["request"]["claim_token"]
        )
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT retry_count FROM monitoring_run_cells WHERE id=%s",
                (deepseek["id"],),
            )
            assert cursor.fetchone()["retry_count"] == 1
            cursor.execute(
                "UPDATE monitoring_run_cells SET claim_until=NOW()-INTERVAL '1 second' WHERE id=%s",
                (deepseek["id"],),
            )
        unknown = monitoring_db.list_monitoring_run_cells(task_id, brand_id=brand_id)
        unknown_deepseek = next(row for row in unknown if row["id"] == deepseek["id"])
        assert unknown_deepseek["state"] == "pending_provider_confirmation"
        with pytest.raises(monitoring_db.MonitoringCellConflict, match="provider outcome"):
            monitoring_db.reserve_monitoring_cell_retry(
                task_id=task_id, cell_id=deepseek["id"], brand_id=brand_id,
                request_id=str(uuid.uuid4()), expected_plan_hash=deepseek["plan_hash"],
            )
        with pytest.raises(ValueError, match="claim state"):
            monitoring_db.claim_monitoring_run_cell(
                cell_id=deepseek["id"], task_id=task_id, brand_id=brand_id,
                allowed_state="failed",
            )
        provider_review_request = str(uuid.uuid4())
        reviewed = monitoring_db.decide_monitoring_provider_review(
            task_id=task_id,
            cell_id=deepseek["id"],
            brand_id=brand_id,
            actor_user_id=999,
            action="confirm_failed",
            note="provider console confirms no completed response",
            request_id=provider_review_request,
            expected_plan_hash=deepseek["plan_hash"],
        )
        reviewed_replay = monitoring_db.decide_monitoring_provider_review(
            task_id=task_id,
            cell_id=deepseek["id"],
            brand_id=brand_id,
            actor_user_id=999,
            action="confirm_failed",
            note="provider console confirms no completed response",
            request_id=provider_review_request,
            expected_plan_hash=deepseek["plan_hash"],
        )
        assert reviewed["state_after"] == "failed"
        assert reviewed_replay["status"] == "idempotent"
        reviewed_cells = monitoring_db.list_monitoring_run_cells(task_id, brand_id=brand_id)
        assert next(row for row in reviewed_cells if row["id"] == deepseek["id"])["state"] == "failed"
        with pytest.raises(PermissionError):
            monitoring_db.decide_monitoring_provider_review(
                task_id=task_id,
                cell_id=deepseek["id"],
                brand_id=brand_id,
                actor_user_id=998,
                action="confirm_failed",
                note="provider console confirms no completed response",
                request_id=provider_review_request,
                expected_plan_hash=deepseek["plan_hash"],
            )
        append_only = connect()
        try:
            with append_only.cursor() as cursor:
                with pytest.raises(psycopg2.Error):
                    cursor.execute(
                        "UPDATE monitoring_provider_review_events SET note='tampered' "
                        "WHERE request_id=%s",
                        (provider_review_request,),
                    )
            append_only.rollback()
            with append_only.cursor() as cursor:
                with pytest.raises(psycopg2.Error):
                    cursor.execute("TRUNCATE monitoring_provider_review_events")
            append_only.rollback()
        finally:
            append_only.close()

        # Same request UUID racing across different failed cells must produce a
        # structured idempotency conflict, never a raw UniqueViolation/500.
        conflict_task_id = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[keyword_id],
            trigger_type="manual_user",
            planned_test_count=2,
            planned_platform_count=2,
        )
        conflict_cells = monitoring_db.create_monitoring_run_cells(
            task_id=conflict_task_id,
            brand_id=brand_id,
            keywords=[{
                "id": keyword_id,
                "quote_id": quote_id,
                "keyword": "single cell phrase",
                "monitoring_query": "single cell question",
                "target_brand": "cell owner",
                "source": "confirmed",
                "entitlement_platforms": "dashscope,deepseek",
                "_eligible_monitoring_platforms": ["dashscope", "deepseek"],
            }],
            search_mode="enhanced",
            fulfillment_credential=str(uuid.uuid4()),
            fulfillment_state="covered",
            retry_coverage={
                "policy_version": "monitoring-retry-v1",
                "coverage": "included",
                "max_attempts": 1,
            },
        )
        conflict_planned = [cell for cell in conflict_cells if cell["is_planned"]]
        for cell in conflict_planned:
            claimed = monitoring_db.claim_monitoring_run_cell(
                cell_id=cell["id"], task_id=conflict_task_id, brand_id=brand_id
            )
            monitoring_db.finish_monitoring_cell_error(
                cell_id=cell["id"], claim_token=claimed["claim_token"],
                state="failed", error_code="definite_provider_rejection",
                error_message="known failure",
            )
        cross_cell_request = str(uuid.uuid4())

        def reserve_different_cell(cell):
            try:
                value = monitoring_db.reserve_monitoring_cell_retry(
                    task_id=conflict_task_id,
                    cell_id=cell["id"],
                    brand_id=brand_id,
                    request_id=cross_cell_request,
                    expected_plan_hash=cell["plan_hash"],
                )
                return ("accepted", value)
            except monitoring_db.MonitoringCellConflict as exc:
                return ("conflict", str(exc))

        with ThreadPoolExecutor(max_workers=2) as executor:
            cross_cell_results = list(executor.map(reserve_different_cell, conflict_planned))
        assert sorted(item[0] for item in cross_cell_results) == ["accepted", "conflict"]
        assert "different parameters" in next(
            item[1] for item in cross_cell_results if item[0] == "conflict"
        )

        # A contract that requires a new fee (or has no explicit coverage) is
        # fail-closed; this endpoint never changes price or creates a new debit.
        paid_retry_task = monitoring_db.create_monitoring_task(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword_ids=[keyword_id],
            trigger_type="manual_user",
            planned_test_count=1,
            planned_platform_count=1,
        )
        paid_retry_cells = monitoring_db.create_monitoring_run_cells(
            task_id=paid_retry_task,
            brand_id=brand_id,
            keywords=[{
                "id": keyword_id,
                "quote_id": quote_id,
                "keyword": "single cell phrase",
                "target_brand": "cell owner",
                "source": "confirmed",
                "entitlement_platforms": "dashscope",
                "_eligible_monitoring_platforms": ["dashscope"],
            }],
            search_mode="enhanced",
            fulfillment_credential=str(uuid.uuid4()),
            fulfillment_state="covered",
            retry_coverage={
                "policy_version": "monitoring-retry-v1",
                "coverage": "requires_new_charge",
                "max_attempts": 1,
            },
        )
        paid_retry_cell = next(cell for cell in paid_retry_cells if cell["is_planned"])
        paid_claim = monitoring_db.claim_monitoring_run_cell(
            cell_id=paid_retry_cell["id"], task_id=paid_retry_task, brand_id=brand_id
        )
        monitoring_db.finish_monitoring_cell_error(
            cell_id=paid_retry_cell["id"], claim_token=paid_claim["claim_token"],
            state="failed", error_code="definite_provider_rejection",
            error_message="known failure",
        )
        with pytest.raises(monitoring_db.MonitoringCellConflict, match="does not include"):
            monitoring_db.reserve_monitoring_cell_retry(
                task_id=paid_retry_task,
                cell_id=paid_retry_cell["id"],
                brand_id=brand_id,
                request_id=str(uuid.uuid4()),
                expected_plan_hash=paid_retry_cell["plan_hash"],
            )

        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM monitoring_results WHERE task_id=%s", (task_id,))
            assert cursor.fetchone()["count"] == 1
            cursor.execute("SELECT total_tests FROM monitoring_tasks WHERE id=%s", (task_id,))
            assert cursor.fetchone()["total_tests"] == 2
            cursor.execute(
                "SELECT retry_count, entitlement_snapshot->>'monitoring_product_version' AS version "
                "FROM monitoring_run_cells WHERE id=%s",
                (deepseek["id"],),
            )
            retry_snapshot = cursor.fetchone()
            assert retry_snapshot["retry_count"] == 1
            assert retry_snapshot["version"] == "monitoring-classic4-v1"
            with pytest.raises(psycopg2.Error, match="immutable"):
                cursor.execute(
                    "UPDATE monitoring_run_cells SET state='failed' WHERE id=%s",
                    (dashscope["id"],),
                )
        rollback_conn = connect()
        try:
            with rollback_conn.cursor() as cursor:
                with pytest.raises(psycopg2.Error, match="fulfillment transition"):
                    cursor.execute(
                        "UPDATE monitoring_run_cells SET fulfillment_state='released' WHERE id=%s",
                        (dashscope["id"],),
                    )
            rollback_conn.rollback()
            with rollback_conn.cursor() as cursor:
                with pytest.raises(psycopg2.Error, match="retained facts exist"):
                    cursor.execute(
                        (ROOT / "scripts" / "rollback_monitoring_cell_retry_2026_07_21.sql").read_text(
                            encoding="utf-8"
                        )
                    )
            rollback_conn.rollback()
        finally:
            rollback_conn.close()
        clean_rollback = connect()
        clean_rollback.autocommit = True
        try:
            with clean_rollback.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE monitoring_provider_review_events DISABLE TRIGGER USER"
                )
                cursor.execute(
                    "TRUNCATE monitoring_provider_review_events, "
                    "monitoring_cell_retry_requests, monitoring_keyword_settlements, "
                    "monitoring_run_cells"
                )
                rollback_sql = (
                    ROOT / "scripts" / "rollback_monitoring_cell_retry_2026_07_21.sql"
                ).read_text(encoding="utf-8")
                cursor.execute(rollback_sql)
                cursor.execute(rollback_sql)
        finally:
            clean_rollback.close()
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()",
                    (database_name,),
                )
                cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
        finally:
            admin.close()
