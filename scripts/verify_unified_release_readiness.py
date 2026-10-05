"""Read-only readiness gate for the unified 2026-07-21 release."""

from __future__ import annotations

import json
import os

import psycopg2
import psycopg2.extras

from services.organization_schema_contract import (
    PRODUCTION_REANCHOR_COUNTS,
    PRODUCTION_REANCHOR_FINGERPRINT,
    match_catalog_variant,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _organization_catalog_matches(organization: dict) -> bool:
    return match_catalog_variant(
        organization["actual_counts"],
        organization["actual_fingerprint"],
    ) is not None


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    _require(bool(dsn), "DATABASE_URL is required")
    with psycopg2.connect(
        dsn,
        cursor_factory=psycopg2.extras.RealDictCursor,
    ) as conn:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SHOW server_version_num")
            version_num = int(cur.fetchone()["server_version_num"])
            _require(160000 <= version_num < 170000, f"PostgreSQL 16 required, got {version_num}")

            # The old active image still fingerprints these frozen tables while
            # migration-before-cutover runs. Any drift here makes blue/green unsafe.
            from services.organization_schema_contract import (
                EXPECTED_COUNTS,
                EXPECTED_FINGERPRINT,
                catalog_fingerprint,
            )

            organization = catalog_fingerprint(cur)
            _require(
                _organization_catalog_matches(organization),
                "organization catalog differs from frozen fresh and clean production reanchor",
            )

            from services.admin_cross_tenant_schema import verify_admin_cross_tenant_schema
            from services.article_closed_loop_schema_contract import assert_schema_ready as assert_closed_loop
            from services.geo_article_v14_schema_contract import assert_schema_ready as assert_article_v14
            from db.monitoring_db import (
                assert_monitoring_cell_retry_ready,
                assert_monitoring_identity_review_ready,
                assert_monitoring_product_matrix_ready,
            )

            verify_admin_cross_tenant_schema(cur)
            assert_monitoring_product_matrix_ready(cur)
            assert_monitoring_identity_review_ready(cur)
            assert_monitoring_cell_retry_ready(cur)
            assert_article_v14(cur)
            assert_closed_loop(cur)

            # [统一 R3 §七] 白标 backoffice 作用域：与 migration 自验/prestart/runtime
            # 同一 DB 合同函数；release 场景基表必须就位（require_settings=True）。
            from services.whitelabel_backoffice_schema_contract import (
                assert_whitelabel_backoffice_schema_ready,
            )

            assert_whitelabel_backoffice_schema_ready(cur, require_settings=True)

            provider_columns = {
                "component_id", "submit_state", "poll_state", "submit_guard_token",
                "last_heartbeat_at", "provider_result_jsonb", "materialization_state",
                "resolution_state", "resolved_at",
            }
            cur.execute(
                """SELECT column_name FROM information_schema.columns
                     WHERE table_schema='public'
                       AND table_name='marketing_material_generation_attempts'"""
            )
            actual_provider_columns = {row["column_name"] for row in cur.fetchall()}
            _require(
                provider_columns <= actual_provider_columns,
                f"GEO provider attempt columns missing: {sorted(provider_columns - actual_provider_columns)}",
            )

            cur.execute(
                """SELECT conname,convalidated FROM pg_catalog.pg_constraint
                     WHERE conrelid='public.marketing_material_generation_attempts'::regclass
                       AND conname=ANY(%s)""",
                ([
                    "marketing_attempt_submit_state_ck",
                    "marketing_attempt_poll_state_ck",
                    "marketing_attempt_materialization_state_ck",
                    "marketing_attempt_resolution_state_ck",
                ],),
            )
            provider_checks = {row["conname"] for row in cur.fetchall() if row["convalidated"]}
            _require(len(provider_checks) == 4, "GEO provider attempt checks incomplete")

            article_columns = {
                "generation_request_id", "generation_revision", "generation_operation",
                "generation_error_code", "generation_error_message", "generation_retryable",
                "generation_failure_phase", "generation_refund_status",
            }
            cur.execute(
                """SELECT column_name FROM information_schema.columns
                     WHERE table_schema='public' AND table_name='topics'"""
            )
            actual_article_columns = {row["column_name"] for row in cur.fetchall()}
            _require(
                article_columns <= actual_article_columns,
                f"article generation columns missing: {sorted(article_columns - actual_article_columns)}",
            )
            cur.execute("SELECT to_regclass('public.article_generation_revision_events') AS relation")
            _require(cur.fetchone()["relation"] is not None, "article generation revision ledger missing")

    print(json.dumps({
        "status": "ready",
        "postgresql": version_num,
        "organization_variant": organization["matched_variant"],
        "organization_fingerprint": organization["actual_fingerprint"],
        "provider_attempt_columns": len(provider_columns),
        "article_generation_columns": len(article_columns),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
