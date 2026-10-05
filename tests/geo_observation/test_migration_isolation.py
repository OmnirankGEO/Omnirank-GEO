"""迁移隔离矩阵(独立 DB · 不碰主套件 public schema):fresh/2x/partial-repair/tamper-detect/
convalidated/index-predicate/R6/R11/privacy 反查全部 fail-closed。
"""
from __future__ import annotations

import re
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_MIGRATION = (ROOT / "scripts" / "migration_geo_observation_v1_2026_07_17.sql").read_text(encoding="utf-8")
MODE_MIGRATION = (ROOT / "scripts" / "migration_geo_observation_collection_mode_2026_07_20.sql").read_text(encoding="utf-8")
BASIS_MIGRATION = (ROOT / "scripts" / "migration_geo_observation_aggregate_basis_2026_07_20.sql").read_text(encoding="utf-8")
A698_BASIS_MIGRATION = (
    ROOT / "tests" / "fixtures" / "geo_observation_aggregate_basis_a698.sql.fixture"
).read_text(encoding="utf-8")
MIGRATIONS = [BASE_MIGRATION, MODE_MIGRATION, BASIS_MIGRATION]
ADMIN_DSN = "postgresql://postgres:test@localhost:55532/postgres"
MIGTEST_DSN = "postgresql://postgres:test@localhost:55532/geo_observation_migtest"


def _apply(cur):
    for migration in MIGRATIONS:
        s = re.sub(r"^\s*\\[a-zA-Z_]+.*$", "", migration, flags=re.MULTILINE)
        s = re.sub(r"^\s*(BEGIN|COMMIT)\s*;\s*$", "", s, flags=re.MULTILINE | re.IGNORECASE)
        cur.execute(s)


def _apply_sql(cur, migration):
    s = re.sub(r"^\s*\\[a-zA-Z_]+.*$", "", migration, flags=re.MULTILINE)
    s = re.sub(r"^\s*(BEGIN|COMMIT)\s*;\s*$", "", s, flags=re.MULTILINE | re.IGNORECASE)
    cur.execute(s)


@pytest.fixture(scope="module")
def migdb():
    admin = psycopg2.connect(ADMIN_DSN); admin.autocommit = True
    admin.cursor().execute("DROP DATABASE IF EXISTS geo_observation_migtest WITH (FORCE)")
    admin.cursor().execute("CREATE DATABASE geo_observation_migtest")
    admin.close()
    yield MIGTEST_DSN
    admin = psycopg2.connect(ADMIN_DSN); admin.autocommit = True
    admin.cursor().execute("DROP DATABASE IF EXISTS geo_observation_migtest WITH (FORCE)")
    admin.close()


@pytest.fixture()
def fresh_cur(migdb):
    conn = psycopg2.connect(migdb); conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE"); cur.execute("CREATE SCHEMA public")
    yield cur
    conn.close()


def test_A_fresh(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name LIKE 'geo_observation_%' "
        "ORDER BY table_name"
    )
    assert [row[0] for row in fresh_cur.fetchall()] == [
        "geo_observation_aggregate_bucket_revision",
        "geo_observation_aggregate_refresh_manifest",
        "geo_observation_aggregates",
        "geo_observation_audit",
        "geo_observation_contributor_buckets",
        "geo_observation_eligibility_epoch",
        "geo_observation_events",
        "geo_observation_gold_eval",
        "geo_observation_insight_jobs",
        "geo_observation_policy",
        "geo_observation_signals",
    ]


def test_B_2x_idempotent(fresh_cur):
    _apply(fresh_cur)
    _apply(fresh_cur)   # 无报错


def test_B0_transaction_rollback_then_forward(fresh_cur):
    """A prestart failure may roll back the whole migration; the next run must recover."""
    conn = fresh_cur.connection
    conn.autocommit = False
    _apply(fresh_cur)
    fresh_cur.execute("SELECT to_regclass('geo_observation_policy')")
    assert fresh_cur.fetchone()[0] == "geo_observation_policy"
    conn.rollback()

    fresh_cur.execute("SELECT to_regclass('geo_observation_policy')")
    assert fresh_cur.fetchone()[0] is None
    _apply(fresh_cur)
    conn.commit()
    fresh_cur.execute(
        "SELECT collection_mode FROM geo_observation_policy WHERE singleton_id=1"
    )
    assert fresh_cur.fetchone()[0] == "existing_collectors_reconciled"


def test_B1_collection_mode_default_and_constraint(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "SELECT collection_mode FROM geo_observation_policy WHERE singleton_id=1"
    )
    assert fresh_cur.fetchone()[0] == "existing_collectors_reconciled"
    fresh_cur.execute(
        "SELECT is_nullable,column_default FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='geo_observation_policy' "
        "AND column_name='collection_mode'"
    )
    nullable, default = fresh_cur.fetchone()
    assert nullable == "NO"
    assert "existing_collectors_reconciled" in default
    fresh_cur.execute(
        "SELECT convalidated,pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname='chk_geo_observation_collection_mode' "
        "AND conrelid='geo_observation_policy'::regclass"
    )
    validated, definition = fresh_cur.fetchone()
    assert validated is True
    assert "native_sampling_driver" in definition


def test_B1_collection_mode_weakened_constraint_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE geo_observation_policy "
        "DROP CONSTRAINT chk_geo_observation_collection_mode"
    )
    fresh_cur.execute(
        "ALTER TABLE geo_observation_policy "
        "ADD CONSTRAINT chk_geo_observation_collection_mode CHECK (TRUE)"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(fresh_cur)


def test_B1a_aggregate_basis_same_name_check_true_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregates "
        "DROP CONSTRAINT chk_geo_obs_agg_policy_basis"
    )
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregates "
        "ADD CONSTRAINT chk_geo_obs_agg_policy_basis CHECK (TRUE)"
    )
    with pytest.raises(psycopg2.Error):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_B1a2_manifest_same_name_check_true_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregate_refresh_manifest "
        "DROP CONSTRAINT chk_geo_obs_agg_manifest_scope"
    )
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregate_refresh_manifest "
        "ADD CONSTRAINT chk_geo_obs_agg_manifest_scope CHECK (TRUE)"
    )
    with pytest.raises(psycopg2.Error):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_B1a3_eligibility_epoch_same_name_check_true_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE geo_observation_eligibility_epoch "
        "DROP CONSTRAINT chk_geo_obs_eligibility_epoch_nonnegative"
    )
    fresh_cur.execute(
        "ALTER TABLE geo_observation_eligibility_epoch "
        "ADD CONSTRAINT chk_geo_obs_eligibility_epoch_nonnegative CHECK (TRUE)"
    )
    with pytest.raises(psycopg2.Error):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_B1b_wrong_table_same_name_constraint_does_not_mask_real_gate(fresh_cur):
    _apply_sql(fresh_cur, BASE_MIGRATION)
    _apply_sql(fresh_cur, MODE_MIGRATION)
    fresh_cur.execute("CREATE TABLE aggregate_basis_decoy(v TEXT)")
    fresh_cur.execute(
        "ALTER TABLE aggregate_basis_decoy ADD CONSTRAINT "
        "chk_geo_obs_agg_policy_basis CHECK (TRUE)"
    )
    _apply_sql(fresh_cur, BASIS_MIGRATION)
    fresh_cur.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname='chk_geo_obs_agg_policy_basis' "
        "AND conrelid='geo_observation_aggregates'::regclass"
    )
    assert "policy_basis_hash" in fresh_cur.fetchone()[0]


def test_B1c_aggregate_basis_wrong_type_or_nullable_fails_closed(fresh_cur):
    _apply_sql(fresh_cur, BASE_MIGRATION)
    _apply_sql(fresh_cur, MODE_MIGRATION)
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregates ADD COLUMN policy_basis_hash BIGINT"
    )
    with pytest.raises(psycopg2.Error):
        _apply_sql(fresh_cur, BASIS_MIGRATION)

    fresh_cur.execute("DROP SCHEMA public CASCADE")
    fresh_cur.execute("CREATE SCHEMA public")
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregates ALTER COLUMN policy_basis_hash SET NOT NULL"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_B1d_aggregate_basis_wrong_same_name_index_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute("DROP INDEX idx_geo_obs_agg_policy_basis_scope_bucket")
    fresh_cur.execute(
        "CREATE INDEX idx_geo_obs_agg_policy_basis_scope_bucket "
        "ON geo_observation_aggregates(policy_basis_hash) "
        "WHERE scope_type='public_industry'"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_B1d1_receipt_gin_wrong_same_name_index_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute("DROP INDEX public.idx_geo_obs_bucket_revision_receipt_gin")
    fresh_cur.execute(
        "CREATE INDEX idx_geo_obs_bucket_revision_receipt_gin "
        "ON public.geo_observation_aggregate_bucket_revision(bucket_start)"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply_sql(fresh_cur, BASIS_MIGRATION)

def test_B1d2_manifest_wrong_type_or_default_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregate_refresh_manifest "
        "ALTER COLUMN policy_version TYPE BIGINT USING 1"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply_sql(fresh_cur, BASIS_MIGRATION)

    fresh_cur.execute("DROP SCHEMA public CASCADE")
    fresh_cur.execute("CREATE SCHEMA public")
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregate_refresh_manifest "
        "ALTER COLUMN updated_at DROP DEFAULT"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_B1d3_manifest_wrong_same_name_unique_index_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute("DROP INDEX uq_geo_obs_agg_manifest_cell")
    fresh_cur.execute(
        "CREATE UNIQUE INDEX uq_geo_obs_agg_manifest_cell "
        "ON geo_observation_aggregate_refresh_manifest(policy_basis_hash,scope_type) "
        "WHERE bucket_granularity='day'"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_B2_reapply_after_gate_opened_idempotent(fresh_cur):
    """P1-1 修复净增量:金标准门被合法开启(gate=TRUE + 达阈值证据)后,migration 重跑仍幂等不报错。

    回归上一版缺陷:7b 反查曾断言"当前 gate 必须 false" + 功能反查裸 UPDATE gate=TRUE(达阈值行不违反 CHECK)→
    开门后每次 prestart 重跑都中止,永久阻断部署。修复后:不断言运行态 + 功能反查强制 NULL 证据。
    """
    _apply(fresh_cur)
    # 合法开门:先落不可变评估行(FK fk_geo_obs_policy_gold_eval 要求 policy 指针指向真实评估),再置 policy 派生态
    fresh_cur.execute("""INSERT INTO geo_observation_gold_eval
        (dataset_version, sample_count, macro_f1_bps, high_risk_false_reco, report_hash, gate_passed)
        VALUES ('ds', 120, 9300, 0, 'rh_'||repeat('a',16), TRUE)""")
    fresh_cur.execute("""UPDATE geo_observation_policy
                            SET outcome_gold_gate_passed=TRUE, gold_sample_count=120, gold_macro_f1_bps=9300,
                                gold_high_risk_false_reco=0, gold_dataset_version='ds', gold_report_hash='rh_'||repeat('a',16)
                          WHERE singleton_id=1""")
    fresh_cur.execute("SELECT outcome_gold_gate_passed FROM geo_observation_policy WHERE singleton_id=1")
    assert fresh_cur.fetchone()[0] is True   # 门确已合法开启
    _apply(fresh_cur)   # 重跑:不得因 gate=TRUE 报错(幂等)
    fresh_cur.execute("SELECT outcome_gold_gate_passed, gold_sample_count FROM geo_observation_policy WHERE singleton_id=1")
    row = fresh_cur.fetchone()
    assert row[0] is True and row[1] == 120   # 功能反查 NULL-证据 UPDATE 被 CHECK 原子回滚,门值不变


def test_C_partial_schema_self_heal(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute("DROP TABLE geo_observation_signals CASCADE")
    _apply(fresh_cur)   # 重建
    fresh_cur.execute("SELECT to_regclass('geo_observation_signals')")
    assert fresh_cur.fetchone()[0] is not None


def test_C2_integration_tables_partial_schema_self_heal(fresh_cur):
    """统一集成表可修复兼容旧表；无法推断的非空业务列仍保持 fail-closed。"""
    fresh_cur.execute("""
        CREATE TABLE geo_ai_surface_cost_ledger (
            id BIGSERIAL PRIMARY KEY,
            source_kind VARCHAR(40) NOT NULL,
            surface_key VARCHAR(80) NOT NULL,
            amount_micros BIGINT NOT NULL,
            request_id VARCHAR(200) NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        INSERT INTO geo_ai_surface_cost_ledger
            (source_kind, surface_key, amount_micros, request_id)
        VALUES ('monitoring', 'doubao_ark_api_search', 20, 'legacy-ledger-row');

        CREATE TABLE geo_ai_surface_cost_reservations (
            token VARCHAR(64) PRIMARY KEY,
            source_kind VARCHAR(40) NOT NULL,
            amount_micros BIGINT NOT NULL,
            request_id VARCHAR(200) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        INSERT INTO geo_ai_surface_cost_reservations
            (token, source_kind, amount_micros, request_id)
        VALUES ('legacy-token', 'research', 30, 'legacy-reservation-row');

        CREATE TABLE geo_observation_insight_jobs (job_id TEXT);
    """)
    _apply(fresh_cur)

    from services.geo_observation.integration import verify_integration_schema

    verify_integration_schema(fresh_cur)
    fresh_cur.execute(
        "SELECT call_count, is_estimated FROM geo_ai_surface_cost_ledger "
        "WHERE request_id='legacy-ledger-row'"
    )
    assert fresh_cur.fetchone() == (1, False)
    fresh_cur.execute(
        "SELECT surface_key, call_count FROM geo_ai_surface_cost_reservations "
        "WHERE request_id='legacy-reservation-row'"
    )
    assert fresh_cur.fetchone() == ("", 1)


def _expect_raise(cur, tamper_sql):
    _apply(cur)
    cur.execute(tamper_sql)
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(cur)


def test_G_tamper_check_definition(fresh_cur):
    _expect_raise(fresh_cur, "ALTER TABLE geo_observation_signals DROP CONSTRAINT chk_geo_obs_signal_outcome")


def test_H_convalidated_false(fresh_cur):
    _expect_raise(fresh_cur,
        "ALTER TABLE geo_observation_signals DROP CONSTRAINT fk_geo_obs_signal_event;"
        "ALTER TABLE geo_observation_signals ADD CONSTRAINT fk_geo_obs_signal_event "
        "FOREIGN KEY (event_id) REFERENCES geo_observation_events(id) NOT VALID")


def test_I_wrong_index_predicate(fresh_cur):
    _expect_raise(fresh_cur,
        "DROP INDEX idx_geo_obs_agg_private;"
        "CREATE INDEX idx_geo_obs_agg_private ON geo_observation_aggregates(owner_user_id,brand_id,bucket_start) "
        "WHERE scope_type='public_industry'")   # 错 predicate


def test_R6_source_type_in_vote_gate(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute("ALTER TABLE geo_observation_contributor_buckets ADD COLUMN source_type TEXT")
    fresh_cur.execute("DROP INDEX uq_geo_obs_contributor_vote")
    fresh_cur.execute("CREATE UNIQUE INDEX uq_geo_obs_contributor_vote ON geo_observation_contributor_buckets"
                      "(contributor_user_bucket,contributor_brand_bucket,prompt_family_key,platform_key,contribution_date,source_type)")
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(fresh_cur)


def test_B1a4_snapshot_checks_same_name_true_fail_closed(fresh_cur):
    _apply(fresh_cur)
    for table, constraint in (
        ("geo_observation_events", "chk_geo_obs_event_promotion_seq"),
        ("geo_observation_aggregate_refresh_manifest", "chk_geo_obs_agg_manifest_fingerprints"),
    ):
        fresh_cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        fresh_cur.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK (TRUE)"
        )
        with pytest.raises(psycopg2.errors.RaiseException):
            _apply(fresh_cur)
        fresh_cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        if table == "geo_observation_events":
            fresh_cur.execute(
                "ALTER TABLE geo_observation_events ADD CONSTRAINT chk_geo_obs_event_promotion_seq "
                "CHECK (processing_state <> 'promoted' OR promotion_seq IS NOT NULL)"
            )
        else:
            fresh_cur.execute(
                "ALTER TABLE geo_observation_aggregate_refresh_manifest "
                "ADD CONSTRAINT chk_geo_obs_agg_manifest_fingerprints CHECK ("
                "expected_scope_cell_fingerprint ~ '^[0-9a-f]{64}$' AND "
                "aggregate_key_fingerprint ~ '^[0-9a-f]{64}$')"
            )


@pytest.mark.parametrize("table,constraint", [
    ("geo_observation_aggregate_bucket_revision", "chk_geo_obs_bucket_revision_scope"),
    ("geo_observation_aggregate_bucket_revision", "chk_geo_obs_bucket_revision_granularity"),
    ("geo_observation_aggregate_bucket_revision", "chk_geo_obs_bucket_revision_epoch"),
])
def test_new_snapshot_governance_same_name_check_true_fails_closed(
    fresh_cur, table, constraint,
):
    _apply(fresh_cur)
    fresh_cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
    fresh_cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK (TRUE)")
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(fresh_cur)


def test_insight_same_name_weak_check_is_repaired_to_exact_definition(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_insight_jobs "
        "DROP CONSTRAINT chk_geo_obs_insight_snapshot"
    )
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_insight_jobs "
        "ADD CONSTRAINT chk_geo_obs_insight_snapshot CHECK (TRUE)"
    )
    _apply_sql(fresh_cur, BASIS_MIGRATION)
    fresh_cur.execute(
        "SELECT lower(regexp_replace(pg_get_constraintdef(oid),'\\s+','','g')) "
        "FROM pg_constraint WHERE conname='chk_geo_obs_insight_snapshot' "
        "AND conrelid='public.geo_observation_insight_jobs'::regclass"
    )
    definition = fresh_cur.fetchone()[0]
    assert definition != "check(true)"
    assert "aggregate_input_watermarkisnotnull" in definition
    assert "aggregate_metric_versionisnotnull" in definition


def test_B1a5_promotion_sequence_wrong_type_nullable_or_index_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute("DROP INDEX uq_geo_obs_event_promotion_seq")
    fresh_cur.execute(
        "CREATE UNIQUE INDEX uq_geo_obs_event_promotion_seq "
        "ON geo_observation_events(id) WHERE id IS NOT NULL"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(fresh_cur)
    fresh_cur.execute("DROP INDEX uq_geo_obs_event_promotion_seq")
    fresh_cur.execute(
        "CREATE UNIQUE INDEX uq_geo_obs_event_promotion_seq "
        "ON geo_observation_events(promotion_seq) WHERE promotion_seq IS NOT NULL"
    )
    fresh_cur.execute(
        "ALTER TABLE geo_observation_aggregates "
        "ALTER COLUMN promotion_sequence_watermark DROP NOT NULL"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(fresh_cur)


@pytest.mark.parametrize("tamper", [
    "ALTER SEQUENCE public.geo_observation_promotion_seq INCREMENT BY 2",
    "ALTER SEQUENCE public.geo_observation_promotion_seq CYCLE",
    "ALTER SEQUENCE public.geo_observation_promotion_seq MAXVALUE 1000",
])
def test_promotion_sequence_definition_tamper_fails_closed(fresh_cur, tamper):
    _apply(fresh_cur)
    fresh_cur.execute(tamper)
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(fresh_cur)


def test_integer_promotion_sequence_lure_fails_closed(fresh_cur):
    _apply_sql(fresh_cur, BASE_MIGRATION)
    _apply_sql(fresh_cur, MODE_MIGRATION)
    fresh_cur.execute(
        "CREATE SEQUENCE public.geo_observation_promotion_seq AS INTEGER"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_wrong_schema_same_name_cannot_shadow_public_sequence(fresh_cur):
    _apply_sql(fresh_cur, BASE_MIGRATION)
    _apply_sql(fresh_cur, MODE_MIGRATION)
    fresh_cur.execute("CREATE SCHEMA IF NOT EXISTS lure")
    fresh_cur.execute("CREATE SEQUENCE lure.geo_observation_promotion_seq AS INTEGER CYCLE")
    _apply_sql(fresh_cur, BASIS_MIGRATION)
    fresh_cur.execute(
        "SELECT data_type::text,increment_by,cycle FROM pg_sequences "
        "WHERE schemaname='public' AND sequencename='geo_observation_promotion_seq'"
    )
    assert fresh_cur.fetchone() == ("bigint", 1, False)


def test_promotion_sequence_is_called_false_fails_readiness(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "SELECT setval('public.geo_observation_promotion_seq',1,FALSE)"
    )
    from psycopg2.extras import RealDictCursor
    from services.geo_observation.readiness import (
        ObservationSchemaNotReady, verify_geo_observation_schema,
    )
    with fresh_cur.connection.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(ObservationSchemaNotReady, match="严格大于"):
            verify_geo_observation_schema(cur)


def test_unlogged_promotion_sequence_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER SEQUENCE public.geo_observation_promotion_seq SET UNLOGGED"
    )
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_withdrawal_trigger_ignores_temp_search_path_lure(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "CREATE TEMP TABLE geo_observation_aggregate_bucket_revision ("
        "scope_type text,bucket_granularity text,bucket_start date,epoch bigint,"
        "dirty boolean,updated_at timestamptz)"
    )
    fresh_cur.execute("SET search_path=pg_temp,public")
    fresh_cur.execute(
        """INSERT INTO public.geo_observation_events(
               event_uuid,source_type,source_table,source_record_id,source_subkey,
               source_event_key,owner_user_id,brand_id,industry_key,platform_key,
               provider_key,model_key,surface_key,session_mode,processing_state,observed_at)
             VALUES(gen_random_uuid(),'paid_diagnosis','src','1','0',%s,5,6,'test',
                    'deepseek','deepseek','deepseek','deepseek_native_no_search',
                    'clean','pending','2026-07-20T01:00:00Z') RETURNING id""",
        ("a" * 64,),
    )
    event_id = fresh_cur.fetchone()[0]
    fresh_cur.execute(
        "UPDATE public.geo_observation_events SET processing_state='promoted' WHERE id=%s",
        (event_id,),
    )
    fresh_cur.execute(
        "UPDATE public.geo_observation_events SET processing_state='withdrawn',"
        "withdrawn_at=NOW() WHERE id=%s", (event_id,),
    )
    fresh_cur.execute(
        "SELECT COUNT(*) FROM public.geo_observation_aggregate_bucket_revision"
    )
    assert fresh_cur.fetchone()[0] == 3
    fresh_cur.execute("SELECT COUNT(*) FROM pg_temp.geo_observation_aggregate_bucket_revision")
    assert fresh_cur.fetchone()[0] == 0


def test_readiness_pins_public_constraint_despite_lure_first_search_path(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute("CREATE SCHEMA IF NOT EXISTS lure")
    fresh_cur.execute(
        "DROP TABLE IF EXISTS lure.geo_observation_aggregate_bucket_revision"
    )
    fresh_cur.execute(
        "CREATE TABLE lure.geo_observation_aggregate_bucket_revision ("
        "scope_type text CONSTRAINT chk_geo_obs_bucket_revision_scope CHECK (TRUE))"
    )
    fresh_cur.execute("SET search_path=lure,public")
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_aggregate_bucket_revision "
        "DROP CONSTRAINT chk_geo_obs_bucket_revision_scope"
    )
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_aggregate_bucket_revision "
        "ADD CONSTRAINT chk_geo_obs_bucket_revision_scope CHECK (TRUE)"
    )
    from psycopg2.extras import RealDictCursor
    from services.geo_observation.readiness import (
        ObservationSchemaNotReady, verify_geo_observation_schema,
    )
    with fresh_cur.connection.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(ObservationSchemaNotReady, match="bucket_revision_scope"):
            verify_geo_observation_schema(cur)


def _insert_snapshot_job(
    cur, *, job_id: str, snapshot_id: str, null_column=None,
    promotion_sequence_watermark: int = 1,
):
    values = {
        "snapshot_id": snapshot_id,
        "policy_basis_hash": "b" * 64,
        "scope_type": "private_brand",
        "bucket_granularity": "day",
        "bucket_start": "2026-07-20",
        "bucket_epoch": 0,
        "promotion_sequence_watermark": promotion_sequence_watermark,
        "aggregate_input_watermark": "2026-07-20T01:00:00Z",
        "aggregate_contract_version": "contract-v1",
        "aggregate_aggregation_version": "aggregation-v1",
        "aggregate_metric_version": "metric-v1",
    }
    if null_column is not None:
        values[null_column] = None
    columns = list(values)
    cur.execute(
        "INSERT INTO public.geo_observation_insight_jobs("
        "job_id,input_hash,owner_user_id,brand_id,state,model,prompt_version,schema_version,"
        + ",".join(columns) + ") VALUES(%s,%s,1,2,'completed','m','p','s',"
        + ",".join(["%s"] * len(columns)) + ")",
        (job_id, "c" * 64, *(values[column] for column in columns)),
    )


def _apply_a698_schema(cur):
    _apply_sql(cur, BASE_MIGRATION)
    _apply_sql(cur, MODE_MIGRATION)
    _apply_sql(cur, A698_BASIS_MIGRATION)


def _insert_a698_manifest(
    cur, *, metric_version: str = "metric-v1", manifest_key: str = "m1",
):
    cur.execute(
        """INSERT INTO public.geo_observation_aggregate_refresh_manifest(
               manifest_key,policy_basis_hash,policy_version,contract_version,
               aggregation_version,metric_version,scope_type,bucket_granularity,
               bucket_start,bucket_end,input_watermark,eligibility_epoch,
               promotion_sequence_watermark,expected_scope_cell_count,
               expected_scope_cell_fingerprint,aggregate_key_fingerprint,
               eligible_observation_count,overall_cell_count,aggregate_row_count,
               completed_at)
             VALUES(%s,%s,'20','contract-v1','aggregation-v1',%s,
                    'private_brand','day','2026-07-20','2026-07-20',
                    '2026-07-20T01:00:00Z',0,1,1,%s,%s,1,1,1,
                    '2026-07-20T02:00:00Z')""",
        (manifest_key, "b" * 64, metric_version, "d" * 64, "e" * 64),
    )


def _insert_a698_snapshot_job(
    cur, *, job_id: str = "legacy-snapshot", snapshot_id: str = "1" * 64,
):
    cur.execute(
        """INSERT INTO public.geo_observation_insight_jobs(
               job_id,input_hash,owner_user_id,brand_id,state,model,
               prompt_version,schema_version,summary,snapshot_id,
               policy_basis_hash,scope_type,bucket_granularity,bucket_start,
               bucket_epoch,promotion_sequence_watermark)
             VALUES(%s,%s,1,2,'completed','m','p','s','legacy summary',%s,%s,
                    'private_brand','day','2026-07-20',0,1)""",
        (job_id, "c" * 64, snapshot_id, "b" * 64),
    )


def test_a698_completed_snapshot_is_provably_upgraded(fresh_cur):
    _apply_a698_schema(fresh_cur)
    _insert_a698_manifest(fresh_cur)
    _insert_a698_snapshot_job(fresh_cur)

    _apply_sql(fresh_cur, BASIS_MIGRATION)

    fresh_cur.execute(
        """SELECT state,snapshot_id::text,aggregate_input_watermark,
                  aggregate_contract_version,aggregate_aggregation_version,
                  aggregate_metric_version
             FROM public.geo_observation_insight_jobs
            WHERE job_id='legacy-snapshot'"""
    )
    row = fresh_cur.fetchone()
    assert row[0] == "completed"
    from datetime import date, datetime, timezone
    from services.geo_observation_analytics.explain import InsightSnapshot
    expected_snapshot = InsightSnapshot(
        policy_basis_hash="b" * 64,
        scope_type="private_brand",
        bucket_granularity="day",
        bucket_start=date(2026, 7, 20),
        bucket_epoch=0,
        promotion_sequence_watermark=1,
        aggregate_input_watermark=datetime(2026, 7, 20, 1, tzinfo=timezone.utc),
        aggregate_contract_version="contract-v1",
        aggregate_aggregation_version="aggregation-v1",
        aggregate_metric_version="metric-v1",
    )
    assert row[1] == expected_snapshot.snapshot_id()
    assert row[2].isoformat() == "2026-07-20T01:00:00+00:00"
    assert row[3:] == ("contract-v1", "aggregation-v1", "metric-v1")


def test_a698_duplicate_jobs_are_retired_before_snapshot_id_canonicalization(fresh_cur):
    _apply_a698_schema(fresh_cur)
    _insert_a698_manifest(fresh_cur)
    _insert_a698_snapshot_job(
        fresh_cur, job_id="legacy-snapshot-a", snapshot_id="1" * 64,
    )
    _insert_a698_snapshot_job(
        fresh_cur, job_id="legacy-snapshot-b", snapshot_id="2" * 64,
    )

    _apply_sql(fresh_cur, BASIS_MIGRATION)
    fresh_cur.execute(
        "SELECT state,snapshot_id,error_detail FROM public.geo_observation_insight_jobs "
        "WHERE job_id LIKE 'legacy-snapshot-%' ORDER BY job_id"
    )
    assert fresh_cur.fetchall() == [
        ("result_unknown", None, "duplicate legacy snapshot identity is ambiguous"),
        ("result_unknown", None, "duplicate legacy snapshot identity is ambiguous"),
    ]


@pytest.mark.parametrize("manifest_mode", ["missing", "ambiguous", "conflicting_partial"])
def test_a698_unprovable_snapshot_fails_closed(fresh_cur, manifest_mode):
    _apply_a698_schema(fresh_cur)
    if manifest_mode in ("ambiguous", "conflicting_partial"):
        _insert_a698_manifest(fresh_cur, metric_version="metric-v1", manifest_key="m1")
    if manifest_mode == "ambiguous":
        _insert_a698_manifest(fresh_cur, metric_version="metric-v2", manifest_key="m2")
    _insert_a698_snapshot_job(fresh_cur)
    if manifest_mode == "conflicting_partial":
        fresh_cur.execute(
            "ALTER TABLE public.geo_observation_insight_jobs "
            "ADD COLUMN aggregate_metric_version TEXT"
        )
        fresh_cur.execute(
            "UPDATE public.geo_observation_insight_jobs "
            "SET aggregate_metric_version='conflicting-version'"
        )

    _apply_sql(fresh_cur, BASIS_MIGRATION)

    fresh_cur.execute(
        """SELECT state,summary,snapshot_id,policy_basis_hash,
                  aggregate_input_watermark,error_code,error_detail
             FROM public.geo_observation_insight_jobs
            WHERE job_id='legacy-snapshot'"""
    )
    row = fresh_cur.fetchone()
    assert row[:5] == ("result_unknown", None, None, None, None)
    assert row[5] == "SEMANTIC_INSIGHT_UNAVAILABLE"
    assert row[6] == "legacy snapshot lineage cannot be uniquely proven"


def test_arbitrary_named_legacy_unique_is_removed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_insight_jobs "
        "ADD CONSTRAINT random_legacy_scope_unique "
        "UNIQUE(owner_user_id,brand_id,input_hash)"
    )
    _apply_sql(fresh_cur, BASIS_MIGRATION)
    _insert_snapshot_job(
        fresh_cur, job_id="random-snap-1", snapshot_id="4" * 64,
        promotion_sequence_watermark=1,
    )
    _insert_snapshot_job(
        fresh_cur, job_id="random-snap-2", snapshot_id="5" * 64,
        promotion_sequence_watermark=2,
    )


def test_reversed_legacy_unique_and_include_index_are_normalized(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_insight_jobs "
        "DROP CONSTRAINT geo_obs_insight_scope_snapshot_uk"
    )
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_insight_jobs "
        "ADD CONSTRAINT reversed_legacy_unique "
        "UNIQUE(input_hash,brand_id,owner_user_id)"
    )
    fresh_cur.execute(
        "CREATE UNIQUE INDEX reversed_snapshot_include ON "
        "public.geo_observation_insight_jobs(snapshot_id,input_hash,brand_id,owner_user_id) "
        "INCLUDE(state)"
    )
    _apply_sql(fresh_cur, BASIS_MIGRATION)
    fresh_cur.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid="
        "'public.geo_observation_insight_jobs'::regclass AND contype='u'"
    )
    assert fresh_cur.fetchall() == [("geo_obs_insight_scope_snapshot_uk",)]


def test_unknown_extra_unique_fails_closed(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_insight_jobs "
        "ADD CONSTRAINT random_unknown_unique "
        "UNIQUE(owner_user_id,brand_id,input_hash,state)"
    )
    with pytest.raises(psycopg2.errors.RaiseException, match="未知额外 UNIQUE"):
        _apply_sql(fresh_cur, BASIS_MIGRATION)


def test_unlogged_bucket_revision_fails_migration_and_readiness(fresh_cur):
    _apply(fresh_cur)
    fresh_cur.execute(
        "ALTER TABLE public.geo_observation_aggregate_bucket_revision SET UNLOGGED"
    )
    with pytest.raises(psycopg2.errors.RaiseException, match="permanent table"):
        _apply_sql(fresh_cur, BASIS_MIGRATION)

    from psycopg2.extras import RealDictCursor
    from services.geo_observation.readiness import (
        ObservationSchemaNotReady, verify_geo_observation_schema,
    )
    with fresh_cur.connection.cursor(cursor_factory=RealDictCursor) as cur:
        with pytest.raises(ObservationSchemaNotReady, match="permanent table"):
            verify_geo_observation_schema(cur)


def test_full_migration_2x_after_cross_snapshot_rows(fresh_cur):
    _apply(fresh_cur)
    _insert_snapshot_job(
        fresh_cur, job_id="job-snap-1", snapshot_id="1" * 64,
        promotion_sequence_watermark=1,
    )
    _insert_snapshot_job(
        fresh_cur, job_id="job-snap-2", snapshot_id="2" * 64,
        promotion_sequence_watermark=2,
    )
    _apply(fresh_cur)
    fresh_cur.execute(
        "SELECT COUNT(*) FROM public.geo_observation_insight_jobs WHERE owner_user_id=1"
    )
    assert fresh_cur.fetchone()[0] == 2


def test_fully_populated_legacy_snapshot_id_is_canonicalized(fresh_cur):
    from datetime import date, datetime, timezone
    from services.geo_observation_analytics.explain import InsightSnapshot

    _apply(fresh_cur)
    _insert_snapshot_job(
        fresh_cur, job_id="job-old-timezone-hash", snapshot_id="7" * 64,
    )

    _apply_sql(fresh_cur, BASIS_MIGRATION)

    expected = InsightSnapshot(
        policy_basis_hash="b" * 64,
        scope_type="private_brand",
        bucket_granularity="day",
        bucket_start=date(2026, 7, 20),
        bucket_epoch=0,
        promotion_sequence_watermark=1,
        aggregate_input_watermark=datetime(2026, 7, 20, 1, tzinfo=timezone.utc),
        aggregate_contract_version="contract-v1",
        aggregate_aggregation_version="aggregation-v1",
        aggregate_metric_version="metric-v1",
    ).snapshot_id()
    fresh_cur.execute(
        "SELECT state,snapshot_id::text FROM public.geo_observation_insight_jobs "
        "WHERE job_id='job-old-timezone-hash'"
    )
    assert fresh_cur.fetchone() == ("completed", expected)


def test_duplicate_legacy_snapshot_identities_fail_closed(fresh_cur):
    _apply(fresh_cur)
    _insert_snapshot_job(
        fresh_cur, job_id="job-legacy-duplicate-a", snapshot_id="8" * 64,
    )
    _insert_snapshot_job(
        fresh_cur, job_id="job-legacy-duplicate-b", snapshot_id="9" * 64,
    )

    _apply_sql(fresh_cur, BASIS_MIGRATION)

    fresh_cur.execute(
        """SELECT state,snapshot_id,summary,error_code,error_detail
             FROM public.geo_observation_insight_jobs
            WHERE job_id LIKE 'job-legacy-duplicate-%'
            ORDER BY job_id"""
    )
    rows = fresh_cur.fetchall()
    assert rows == [
        (
            "result_unknown", None, None, "SEMANTIC_INSIGHT_UNAVAILABLE",
            "duplicate legacy snapshot identity is ambiguous",
        ),
        (
            "result_unknown", None, None, "SEMANTIC_INSIGHT_UNAVAILABLE",
            "duplicate legacy snapshot identity is ambiguous",
        ),
    ]


@pytest.mark.parametrize("null_column", [
    "snapshot_id", "policy_basis_hash", "scope_type", "bucket_granularity",
    "bucket_start", "bucket_epoch", "promotion_sequence_watermark",
    "aggregate_input_watermark", "aggregate_contract_version",
    "aggregate_aggregation_version", "aggregate_metric_version",
])
def test_completed_snapshot_rejects_every_null_lineage_field(fresh_cur, null_column):
    _apply(fresh_cur)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_snapshot_job(
            fresh_cur, job_id=f"job-null-{null_column}", snapshot_id="3" * 64,
            null_column=null_column,
        )

def test_R11_promotion_default_true(fresh_cur):
    # pristine seed(policy_version=1)被篡改成 flag=true → 反查拦下(raw jsonb_set 不 bump version)
    _expect_raise(fresh_cur,
        "UPDATE geo_observation_policy SET policy_json=jsonb_set(policy_json,'{feature_flags,promotion_enabled}','true') WHERE singleton_id=1")


def test_R11_reapply_after_legit_enable_idempotent(fresh_cur):
    """R11 修复净增量:经 CAS 治理合法启用开关(policy_version bump>1)后 migration 重跑仍幂等。

    回归:上一版 R11 反查断言 policy_json flag 恒 false,一旦经 PUT /policy 合法开启(必 bump version)→
    每次 prestart 重跑 RAISE 永久阻断部署。修复后仅对 pristine seed(version=1)校验。
    (update_policy 必 bump version;此处 raw SQL 同置 flag + version=2 模拟合法 CAS 启用。)
    """
    _apply(fresh_cur)
    fresh_cur.execute("""UPDATE geo_observation_policy
                            SET policy_json=jsonb_set(policy_json,'{feature_flags,promotion_enabled}','true'),
                                policy_version=2
                          WHERE singleton_id=1""")
    _apply(fresh_cur)   # version=2 → R11 pristine 校验跳过 → 不报错(幂等)
    fresh_cur.execute("SELECT (policy_json->'feature_flags'->>'promotion_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1")
    assert fresh_cur.fetchone()[0] is True   # 合法启用保持


def test_privacy_signals_owner_leak(fresh_cur):
    _expect_raise(fresh_cur, "ALTER TABLE geo_observation_signals ADD COLUMN owner_user_id BIGINT")


def test_runtime_sql_always_qualifies_observation_truth_tables():
    """Readiness pins ``public``; runtime SQL must not then follow search_path.

    Contract SQL fixtures are intentionally excluded: this gate covers Python
    statements that execute after startup, including product/admin reads.
    """
    roots = [
        ROOT / "services" / "geo_observation",
        ROOT / "services" / "geo_observation_analytics",
    ]
    files = [
        *(path for root in roots for path in root.rglob("*.py")),
        ROOT / "api" / "geo_observation_product_api.py",
        ROOT / "api" / "geo_observation_admin_api.py",
    ]
    unqualified = re.compile(
        r"(?i)\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM|"
        r"LOCK\s+TABLE|REFERENCES)\s+geo_observation_|\bON\s+geo_observation_"
    )
    hits = []
    for path in files:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if unqualified.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{line_no}:{line.strip()}")
    assert hits == [], "runtime SQL may be hijacked by search_path:\n" + "\n".join(hits)
