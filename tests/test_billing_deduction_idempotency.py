"""Real PostgreSQL proofs for opt-in deduct_points idempotency.

These tests use an isolated schema inside TEST_DATABASE_URL.  They exercise
PostgreSQL row locking and transaction commit behaviour; no production-shaped
result is mocked for concurrency or hard-exit recovery.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from fastapi import HTTPException


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "scripts/migration_billing_deduction_idempotency_2026_07_19.sql"
)


def _test_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL", "")
    database_name = dsn.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    assert dsn and ("test" in database_name or "throwaway" in database_name), (
        "billing idempotency tests require a throwaway TEST_DATABASE_URL"
    )
    return dsn


def _connect(dsn: str, schema: str | None = None):
    options = f"-c search_path={schema},public" if schema else None
    return psycopg2.connect(
        dsn,
        options=options,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _apply_migration(conn) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    sql = re.sub(
        r"^\s*(BEGIN|COMMIT)\s*;\s*$",
        "",
        sql,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    conn.commit()
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute(sql)
    conn.autocommit = False


@pytest.fixture
def billing_pg_schema(monkeypatch):
    dsn = _test_dsn()
    schema = f"billing_idem_{uuid.uuid4().hex[:16]}"
    admin = _connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()

    conn = _connect(dsn, schema)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE user_wallets (
                user_id INTEGER PRIMARY KEY,
                paid_points BIGINT NOT NULL DEFAULT 0,
                bonus_points BIGINT NOT NULL DEFAULT 0,
                commission_points BIGINT NOT NULL DEFAULT 0,
                deduction_preference TEXT NOT NULL DEFAULT 'default',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE customer_agent_credit_wallets (
                customer_user_id INTEGER PRIMARY KEY,
                agent_user_id INTEGER NOT NULL,
                tool_credit_points BIGINT NOT NULL DEFAULT 0,
                publish_credit_points BIGINT NOT NULL DEFAULT 0,
                bonus_credit_points BIGINT NOT NULL DEFAULT 0,
                total_purchased_points BIGINT NOT NULL DEFAULT 0,
                total_consumed_points BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE point_transactions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                point_type TEXT NOT NULL,
                amount BIGINT NOT NULL,
                balance_after BIGINT NOT NULL,
                feature_code TEXT,
                description TEXT,
                order_id TEXT,
                brand_id INTEGER,
                source TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE customer_credit_transactions (
                id BIGSERIAL PRIMARY KEY,
                customer_user_id INTEGER NOT NULL,
                agent_user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                pool TEXT NOT NULL,
                points BIGINT NOT NULL,
                balance_tool_after BIGINT NOT NULL,
                balance_publish_after BIGINT NOT NULL,
                balance_bonus_after BIGINT NOT NULL,
                feature_code TEXT,
                related_order_id TEXT,
                source TEXT,
                description TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE bonus_clawback_pending (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                recharge_order_id TEXT NOT NULL,
                amount_due BIGINT NOT NULL,
                amount_settled BIGINT DEFAULT 0,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                settled_at TIMESTAMP
            );
            CREATE TABLE service_fee_clawback_pending (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source_order_id TEXT NOT NULL,
                amount_due DECIMAL(10,2) NOT NULL,
                amount_settled DECIMAL(10,2) DEFAULT 0,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                settled_at TIMESTAMP
            );
            INSERT INTO user_wallets
                (user_id, paid_points, bonus_points, commission_points)
            VALUES (91001, 1000, 0, 0);
            INSERT INTO customer_agent_credit_wallets
                (customer_user_id, agent_user_id, tool_credit_points)
            VALUES (91002, 92002, 1000);
            """
        )
    conn.commit()
    _apply_migration(conn)
    conn.close()

    import db.auth_db as auth_db
    import db.wallet_db as wallet_db
    import middleware.billing as billing
    import middleware.v3_3_1_debt_offset as debt_offset

    @contextmanager
    def schema_get_db():
        scoped = _connect(dsn, schema)
        try:
            yield scoped
            scoped.commit()
        except Exception:
            scoped.rollback()
            raise
        finally:
            scoped.close()

    monkeypatch.setattr(auth_db, "get_user", lambda _user_id: {"is_admin": False})
    monkeypatch.setattr(
        billing,
        "get_feature_pricing",
        lambda feature_code: {
            "feature_code": feature_code,
            "feature_name": "报告导出",
            "cost_points": 10,
            "requires_paid_points": False,
        },
    )
    monkeypatch.setattr(billing, "get_db", schema_get_db)
    monkeypatch.setattr(wallet_db, "_POINT_TX_HAS_SOURCE", True)
    monkeypatch.setattr(
        debt_offset,
        "is_v3_3_1_enabled",
        lambda: False,
    )

    yield {"dsn": dsn, "schema": schema, "get_db": schema_get_db}

    admin = _connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    admin.close()


def _deduct(user_id: int, key: str, **kwargs):
    import middleware.billing as billing

    return asyncio.run(
        billing.deduct_points(
            user_id,
            "report_export",
            idempotency_key=key,
            **kwargs,
        )
    )


def _fetch_one(env, sql: str, params=()):
    conn = _connect(env["dsn"], env["schema"])
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()
    finally:
        conn.close()


def test_migration_is_fresh_and_repeatable_on_real_postgresql(billing_pg_schema):
    env = billing_pg_schema
    conn = _connect(env["dsn"], env["schema"])
    _apply_migration(conn)
    _apply_migration(conn)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema=%s
               AND table_name='billing_deduction_idempotency'
             ORDER BY ordinal_position
            """,
            (env["schema"],),
        )
        columns = {row["column_name"]: row for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT COUNT(*) AS count
              FROM pg_indexes
             WHERE schemaname=%s
               AND tablename='billing_deduction_idempotency'
               AND indexname='idx_billing_deduction_idempotency_created'
            """,
            (env["schema"],),
        )
        index_count = int(cursor.fetchone()["count"])
    conn.close()

    assert columns["idempotency_key"]["data_type"] == "text"
    assert columns["request_fingerprint"]["data_type"] == "character"
    assert columns["response_jsonb"]["data_type"] == "jsonb"
    assert columns["charge_tx_id"]["data_type"] == "bigint"
    assert columns["refund_pending_at"]["data_type"] == "timestamp with time zone"
    assert index_count == 1


@pytest.mark.parametrize(
    ("user_id", "wallet_table", "balance_column", "transaction_table"),
    [
        (91001, "user_wallets", "paid_points", "point_transactions"),
        (
            91002,
            "customer_agent_credit_wallets",
            "tool_credit_points",
            "customer_credit_transactions",
        ),
    ],
)
def test_twenty_concurrent_retries_charge_once_for_legacy_and_v35(
    billing_pg_schema,
    user_id,
    wallet_table,
    balance_column,
    transaction_table,
):
    key = f"report-export:{user_id}:concurrent-proof"
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _n: _deduct(user_id, key), range(20)))

    charge_ids = {int(result["charge_tx_id"]) for result in results}
    assert len(charge_ids) == 1
    assert {int(result["deducted"]) for result in results} == {10}
    if user_id == 91002:
        assert {result["channel"] for result in results} == {"v35"}

    user_column = "user_id" if wallet_table == "user_wallets" else "customer_user_id"
    row = _fetch_one(
        billing_pg_schema,
        f"SELECT {balance_column} AS balance FROM {wallet_table} WHERE {user_column}=%s",
        (user_id,),
    )
    tx_count = _fetch_one(
        billing_pg_schema,
        f"SELECT COUNT(*) AS count FROM {transaction_table} WHERE "
        f"{'user_id' if transaction_table == 'point_transactions' else 'customer_user_id'}=%s "
        "AND feature_code='report_export' AND type='consume'",
        (user_id,),
    )
    idem = _fetch_one(
        billing_pg_schema,
        "SELECT status, charge_tx_id, deducted FROM billing_deduction_idempotency "
        "WHERE idempotency_key=%s",
        (key,),
    )
    assert int(row["balance"]) == 990
    assert int(tx_count["count"]) == 1
    assert idem["status"] == "charged"
    assert int(idem["charge_tx_id"]) in charge_ids
    assert int(idem["deducted"]) == 10


def test_same_key_with_different_critical_arguments_is_rejected(billing_pg_schema):
    key = "report-export:91001:argument-conflict"
    first = _deduct(91001, key, brand_id=11)
    with pytest.raises(HTTPException) as exc_info:
        _deduct(91001, key, brand_id=11, extra_cost=1)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    with pytest.raises(HTTPException) as brand_conflict:
        _deduct(91001, key, brand_id=12)
    assert brand_conflict.value.status_code == 409
    assert brand_conflict.value.detail["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    row = _fetch_one(
        billing_pg_schema,
        "SELECT paid_points FROM user_wallets WHERE user_id=91001",
    )
    assert int(row["paid_points"]) == 990
    assert first["charge_tx_id"] is not None


def test_omitting_key_preserves_legacy_non_idempotent_behavior(billing_pg_schema):
    import middleware.billing as billing

    first = asyncio.run(billing.deduct_points(91001, "report_export"))
    second = asyncio.run(billing.deduct_points(91001, "report_export"))
    assert first["charge_tx_id"] != second["charge_tx_id"]

    wallet = _fetch_one(
        billing_pg_schema,
        "SELECT paid_points FROM user_wallets WHERE user_id=91001",
    )
    transactions = _fetch_one(
        billing_pg_schema,
        "SELECT COUNT(*) AS count FROM point_transactions WHERE user_id=91001",
    )
    attempts = _fetch_one(
        billing_pg_schema,
        "SELECT COUNT(*) AS count FROM billing_deduction_idempotency",
    )
    assert int(wallet["paid_points"]) == 980
    assert int(transactions["count"]) == 2
    assert int(attempts["count"]) == 0


def test_committed_retry_replays_before_pricing_dependency_changes(
    billing_pg_schema,
    monkeypatch,
):
    import middleware.billing as billing

    key = "report-export:91001:pricing-change-replay"
    first = _deduct(91001, key)

    def pricing_unavailable(_feature_code):
        raise RuntimeError("pricing dependency unavailable after commit")

    monkeypatch.setattr(billing, "get_feature_pricing", pricing_unavailable)
    replayed = _deduct(91001, key)
    assert replayed == first

    wallet = _fetch_one(
        billing_pg_schema,
        "SELECT paid_points FROM user_wallets WHERE user_id=91001",
    )
    transactions = _fetch_one(
        billing_pg_schema,
        "SELECT COUNT(*) AS count FROM point_transactions WHERE user_id=91001",
    )
    assert int(wallet["paid_points"]) == 990
    assert int(transactions["count"]) == 1


def test_idempotency_completion_failure_rolls_back_wallet_transaction_and_attempt(
    billing_pg_schema,
    monkeypatch,
):
    import middleware.billing as billing

    key = "report-export:91001:rollback-proof"

    def injected_failure(*_args, **_kwargs):
        raise RuntimeError("injected completion failure")

    monkeypatch.setattr(billing, "_complete_deduction_idempotency", injected_failure)
    with pytest.raises(RuntimeError, match="injected completion failure"):
        _deduct(91001, key)

    wallet = _fetch_one(
        billing_pg_schema,
        "SELECT paid_points FROM user_wallets WHERE user_id=91001",
    )
    tx = _fetch_one(
        billing_pg_schema,
        "SELECT COUNT(*) AS count FROM point_transactions WHERE user_id=91001",
    )
    idem = _fetch_one(
        billing_pg_schema,
        "SELECT COUNT(*) AS count FROM billing_deduction_idempotency "
        "WHERE idempotency_key=%s",
        (key,),
    )
    assert int(wallet["paid_points"]) == 1000
    assert int(tx["count"]) == 0
    assert int(idem["count"]) == 0


def test_hard_exit_after_database_commit_recovers_pending_debt_receipt(
    billing_pg_schema,
    monkeypatch,
):
    """Kill after charge commit leaves a durable debt event that retry completes."""
    env = billing_pg_schema
    key = "report-export:91001:hard-exit-proof"
    with env["get_db"]() as conn:
        conn.cursor().execute(
            """
            INSERT INTO bonus_clawback_pending
                (user_id,recharge_order_id,amount_due,amount_settled,status)
            VALUES (91001,'kill9-debt',100,0,'pending')
            """
        )
    child_code = r'''
import asyncio
import os
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import db.auth_db as auth_db
import db.wallet_db as wallet_db
import middleware.billing as billing
import middleware.v3_3_1_debt_offset as debt_offset

dsn = os.environ["IDEM_TEST_DSN"]
schema = os.environ["IDEM_TEST_SCHEMA"]

@contextmanager
def commit_then_hard_exit():
    conn = psycopg2.connect(
        dsn,
        options=f"-c search_path={schema},public",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        yield conn
        cur = conn.cursor()
        cur.execute(
            "SELECT status FROM billing_deduction_idempotency "
            "WHERE idempotency_key=%s",
            ("report-export:91001:hard-exit-proof",),
        )
        charged = cur.fetchone()
        conn.commit()
        if charged and charged["status"] == "charged":
            os._exit(137)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

auth_db.get_user = lambda _user_id: {"is_admin": False}
billing.get_feature_pricing = lambda feature_code: {
    "feature_code": feature_code,
    "feature_name": "报告导出",
    "cost_points": 10,
    "requires_paid_points": False,
}
billing.get_db = commit_then_hard_exit
wallet_db._POINT_TX_HAS_SOURCE = True
debt_offset.is_v3_3_1_enabled = lambda: True
def fail_debt_offset(*_args, **_kwargs):
    raise RuntimeError("injected debt offset outage before charge commit")
debt_offset.offset_debts_after_consumption_cursor = fail_debt_offset
asyncio.run(billing.deduct_points(
    91001,
    "report_export",
    idempotency_key="report-export:91001:hard-exit-proof",
))
'''
    child_env = os.environ.copy()
    child_env["IDEM_TEST_DSN"] = env["dsn"]
    child_env["IDEM_TEST_SCHEMA"] = env["schema"]
    process = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 137, (process.stdout, process.stderr)

    import middleware.v3_3_1_debt_offset as debt_offset
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    replayed = _deduct(91001, key)
    wallet = _fetch_one(
        env,
        "SELECT paid_points FROM user_wallets WHERE user_id=91001",
    )
    transactions = _fetch_one(
        env,
        "SELECT COUNT(*) AS count, MIN(id) AS charge_tx_id "
        "FROM point_transactions WHERE user_id=91001 AND feature_code='report_export'",
    )
    attempt = _fetch_one(
        env,
        "SELECT status, charge_tx_id, response_jsonb "
        "FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )

    assert int(wallet["paid_points"]) == 990
    assert int(transactions["count"]) == 1
    assert attempt["status"] == "charged"
    assert int(replayed["charge_tx_id"]) == int(transactions["charge_tx_id"])
    assert int(attempt["charge_tx_id"]) == int(transactions["charge_tx_id"])
    assert int(attempt["response_jsonb"]["charge_tx_id"]) == int(
        transactions["charge_tx_id"]
    )
    debt = _fetch_one(
        env,
        "SELECT amount_settled FROM bonus_clawback_pending "
        "WHERE recharge_order_id='kill9-debt'",
    )
    receipt = _fetch_one(
        env,
        "SELECT status,retry_count FROM billing_debt_offset_outbox WHERE charge_tx_id=%s",
        (int(transactions["charge_tx_id"]),),
    )
    assert int(debt["amount_settled"]) == 10
    assert receipt["status"] == "completed"
    assert int(receipt["retry_count"]) == 1


def _new_schema(dsn: str, prefix: str) -> tuple[str, object]:
    schema = f"{prefix}_{uuid.uuid4().hex[:12]}"
    admin = _connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()
    return schema, _connect(dsn, schema)


def _drop_schema(dsn: str, schema: str) -> None:
    admin = _connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    admin.close()


def test_migration_repairs_empty_partial_table_weak_checks_and_wrong_index():
    dsn = _test_dsn()
    schema, conn = _new_schema(dsn, "billing_partial")
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE billing_deduction_idempotency (
                    user_id INTEGER,
                    status TEXT,
                    response_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
                    CONSTRAINT weak_billing_status CHECK (status IS NULL OR status <> '') NOT VALID
                );
                CREATE INDEX idx_billing_deduction_idempotency_created
                    ON billing_deduction_idempotency(user_id);
                CREATE TABLE billing_debt_offset_outbox (
                    event_key TEXT,
                    updated_at TIMESTAMPTZ,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX idx_billing_debt_offset_pending
                    ON billing_debt_offset_outbox(updated_at);
                ALTER TABLE billing_debt_offset_outbox
                    ADD CONSTRAINT idx_billing_debt_offset_charge UNIQUE(updated_at);
                """
            )
        conn.commit()
        _apply_migration(conn)
        _apply_migration(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT conname, contype, convalidated, pg_get_constraintdef(oid) AS definition
                  FROM pg_constraint
                 WHERE conrelid='billing_deduction_idempotency'::regclass
                 ORDER BY conname
                """
            )
            constraints = {row["conname"]: row for row in cursor.fetchall()}
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname=%s "
                "AND indexname='idx_billing_deduction_idempotency_created'",
                (schema,),
            )
            indexdef = cursor.fetchone()["indexdef"]
            cursor.execute(
                """
                SELECT conname, contype, convalidated, pg_get_constraintdef(oid) AS definition
                  FROM pg_constraint
                 WHERE conrelid='billing_debt_offset_outbox'::regclass
                 ORDER BY conname
                """
            )
            outbox_constraints = {row["conname"]: row for row in cursor.fetchall()}
            cursor.execute(
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s "
                "AND indexname IN ('idx_billing_debt_offset_pending',"
                "'idx_billing_debt_offset_charge')",
                (schema,),
            )
            outbox_indexes = {row["indexname"]: row["indexdef"] for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT table_name,column_name,is_nullable,column_default
                  FROM information_schema.columns
                 WHERE table_schema=%s AND (
                       (table_name='billing_deduction_idempotency'
                        AND column_name='response_jsonb')
                    OR (table_name='billing_debt_offset_outbox'
                        AND column_name='last_error'))
                """,
                (schema,),
            )
            nullable_columns = {
                (row["table_name"], row["column_name"]): row
                for row in cursor.fetchall()
            }
        assert constraints["billing_deduction_idempotency_pkey"]["contype"] == "p"
        assert constraints["billing_deduction_idempotency_status_check"]["convalidated"]
        assert "refund_pending" in constraints[
            "billing_deduction_idempotency_status_check"
        ]["definition"]
        assert "created_at DESC" in indexdef
        assert "user_id" not in indexdef.split("(", 1)[1]
        assert outbox_constraints["billing_debt_offset_outbox_pkey"]["contype"] == "p"
        assert outbox_constraints["billing_debt_offset_outbox_status_check"]["convalidated"]
        assert "cancelled" in outbox_constraints[
            "billing_debt_offset_outbox_status_check"
        ]["definition"]
        assert "reversed" in outbox_constraints[
            "billing_debt_offset_outbox_status_check"
        ]["definition"]
        assert outbox_constraints[
            "billing_debt_offset_outbox_refund_points_check"
        ]["convalidated"]
        assert "next_retry_at, created_at" in outbox_indexes[
            "idx_billing_debt_offset_pending"
        ]
        assert "WHERE (status = 'pending'::text)" in outbox_indexes[
            "idx_billing_debt_offset_pending"
        ]
        assert "UNIQUE INDEX" in outbox_indexes["idx_billing_debt_offset_charge"]
        assert "ledger_type, charge_tx_id" in outbox_indexes[
            "idx_billing_debt_offset_charge"
        ]
        assert nullable_columns[
            ("billing_deduction_idempotency", "response_jsonb")
        ]["is_nullable"] == "YES"
        assert nullable_columns[
            ("billing_deduction_idempotency", "response_jsonb")
        ]["column_default"] is None
        assert nullable_columns[
            ("billing_debt_offset_outbox", "last_error")
        ]["is_nullable"] == "YES"
        assert nullable_columns[
            ("billing_debt_offset_outbox", "last_error")
        ]["column_default"] is None
    finally:
        conn.close()
        _drop_schema(dsn, schema)


def test_migration_fails_closed_on_incompatible_same_name_table_type():
    dsn = _test_dsn()
    schema, conn = _new_schema(dsn, "billing_wrong_type")
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE billing_deduction_idempotency "
                "(idempotency_key INTEGER)"
            )
        conn.commit()
        with pytest.raises(psycopg2.Error, match="incompatible column types"):
            _apply_migration(conn)
    finally:
        conn.close()
        _drop_schema(dsn, schema)


def test_migration_fails_closed_on_orphan_debt_receipt_identity():
    dsn = _test_dsn()
    schema, conn = _new_schema(dsn, "billing_orphan_receipt")
    try:
        _apply_migration(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO billing_debt_offset_outbox
                    (event_key,user_id,feature_code,charge_tx_id,ledger_type,
                     consumed_points,idempotency_key,status,last_error)
                VALUES ('billing_debt_offset:legacy:81231',7,'report_export',81231,
                        'legacy',10,'missing-charge-key','manual','injected orphan')
                """
            )
        conn.commit()
        with pytest.raises(psycopg2.Error, match="orphan/conflicting"):
            _apply_migration(conn)
    finally:
        conn.close()
        _drop_schema(dsn, schema)


def test_migration_upgrades_completed_rows_to_charged_lifecycle():
    dsn = _test_dsn()
    schema, conn = _new_schema(dsn, "billing_old_status")
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE billing_deduction_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    feature_code TEXT NOT NULL,
                    total_cost BIGINT NOT NULL,
                    extra_cost BIGINT NOT NULL DEFAULT 0,
                    brand_id INTEGER,
                    request_fingerprint CHAR(64) NOT NULL,
                    status TEXT NOT NULL DEFAULT 'in_progress'
                        CHECK (status IN ('in_progress','completed')),
                    response_jsonb JSONB,
                    charge_tx_id BIGINT,
                    ledger_type TEXT,
                    deducted BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                );
                INSERT INTO billing_deduction_idempotency
                    (idempotency_key,user_id,feature_code,total_cost,
                     request_fingerprint,status,response_jsonb,charge_tx_id,
                     ledger_type,deducted,completed_at)
                VALUES ('old-completed',7,'report_export',10,%s,'completed',
                        '{"success":true,"deducted":10,"charge_tx_id":91}'::jsonb,
                        91,'legacy',10,NOW());
                """,
                ("a" * 64,),
            )
        conn.commit()
        _apply_migration(conn)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM billing_deduction_idempotency "
                "WHERE idempotency_key='old-completed'"
            )
            assert cursor.fetchone()["status"] == "charged"
            cursor.execute(
                "SELECT status,last_error FROM billing_debt_offset_outbox "
                "WHERE idempotency_key='old-completed'"
            )
            receipt = cursor.fetchone()
            assert receipt["status"] == "manual"
            assert receipt["last_error"] == "pre_migration_charge_requires_debt_audit"
    finally:
        conn.close()
        _drop_schema(dsn, schema)


def test_refunded_charge_key_never_replays_as_paid(billing_pg_schema):
    import middleware.billing as billing

    key = "report-export:91001:refunded-no-replay"
    charge = _deduct(91001, key)
    result = asyncio.run(
        billing.refund_points(
            91001,
            "report_export",
            reason="PDF 生成失败",
            charge_tx_id=int(charge["charge_tx_id"]),
            ledger_type="legacy",
        )
    )
    assert result["success"] is True
    assert _fetch_one(
        billing_pg_schema,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refunded"

    with pytest.raises(HTTPException) as exc_info:
        _deduct(91001, key)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "IDEMPOTENCY_CHARGE_REFUNDED"
    assert int(
        _fetch_one(
            billing_pg_schema,
            "SELECT paid_points FROM user_wallets WHERE user_id=91001",
        )["paid_points"]
    ) == 1000


def test_v35_refunded_charge_key_never_replays_as_paid(billing_pg_schema):
    import middleware.billing as billing

    key = "report-export:91002:v35-refunded-no-replay"
    charge = _deduct(91002, key)
    result = asyncio.run(
        billing.refund_points(
            91002,
            "report_export",
            reason="PDF 生成失败",
            charge_tx_id=int(charge["charge_tx_id"]),
            ledger_type="v35",
        )
    )
    assert result["success"] is True
    assert _fetch_one(
        billing_pg_schema,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refunded"
    with pytest.raises(HTTPException) as exc_info:
        _deduct(91002, key)
    assert exc_info.value.detail["code"] == "IDEMPOTENCY_CHARGE_REFUNDED"
    assert int(
        _fetch_one(
            billing_pg_schema,
            "SELECT tool_credit_points FROM customer_agent_credit_wallets "
            "WHERE customer_user_id=91002",
        )["tool_credit_points"]
    ) == 1000


@pytest.mark.parametrize(
    ("user_id", "ledger_type", "wallet_table", "wallet_id_column", "balance_column", "tx_table", "tx_user_column", "refund_amount_column"),
    [
        (
            91001,
            "legacy",
            "user_wallets",
            "user_id",
            "paid_points",
            "point_transactions",
            "user_id",
            "amount",
        ),
        (
            91002,
            "v35",
            "customer_agent_credit_wallets",
            "customer_user_id",
            "tool_credit_points",
            "customer_credit_transactions",
            "customer_user_id",
            "points",
        ),
    ],
)
def test_partial_refund_uses_cumulative_target_and_can_continue_to_full(
    billing_pg_schema,
    user_id,
    ledger_type,
    wallet_table,
    wallet_id_column,
    balance_column,
    tx_table,
    tx_user_column,
    refund_amount_column,
):
    """A prior partial refund must not make the remaining charge disappear.

    ``amount`` is a cumulative target when no refund_request_id is supplied:
    target 4 refunds four, target 6 refunds only the missing two, and ``None``
    reaches the full original charge.  Replaying any reached target is a no-op.
    """
    import middleware.billing as billing

    key = f"report-export:{user_id}:partial-target"
    charge = _deduct(user_id, key)
    charge_tx_id = int(charge["charge_tx_id"])

    first = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="partial target four",
            amount=4,
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )
    second = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="raise cumulative target to six",
            amount=6,
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )
    replay_six = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="retry cumulative target six",
            amount=6,
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )

    assert first["success"] is True
    assert first["refunded"] == 4
    assert first["confirmed_refunded"] == 4
    assert first.get("already_refunded") is False
    assert second["success"] is True
    assert second["refunded"] == 2
    assert second["confirmed_refunded"] == 6
    assert second.get("already_refunded") is False
    assert replay_six["success"] is True
    assert replay_six["refunded"] == 0
    assert replay_six["confirmed_refunded"] == 6
    assert replay_six["already_refunded"] is True
    assert int(
        _fetch_one(
            billing_pg_schema,
            f"SELECT {balance_column} AS balance FROM {wallet_table} "
            f"WHERE {wallet_id_column}=%s",
            (user_id,),
        )["balance"]
    ) == 996
    assert _fetch_one(
        billing_pg_schema,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refund_pending"

    finish = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="finish full refund",
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )
    replay_full = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="retry full refund",
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )

    assert finish["success"] is True
    assert finish["refunded"] == 4
    assert finish["confirmed_refunded"] == 10
    assert finish.get("already_refunded") is False
    assert replay_full["success"] is True
    assert replay_full["refunded"] == 0
    assert replay_full["confirmed_refunded"] == 10
    assert replay_full["already_refunded"] is True
    assert int(
        _fetch_one(
            billing_pg_schema,
            f"SELECT {balance_column} AS balance FROM {wallet_table} "
            f"WHERE {wallet_id_column}=%s",
            (user_id,),
        )["balance"]
    ) == 1000
    assert int(
        _fetch_one(
            billing_pg_schema,
            f"SELECT COALESCE(SUM({refund_amount_column}),0)::bigint AS refunded "
            f"FROM {tx_table} WHERE {tx_user_column}=%s AND type='refund'",
            (user_id,),
        )["refunded"]
    ) == 10
    assert _fetch_one(
        billing_pg_schema,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refunded"


@pytest.mark.parametrize(
    ("user_id", "ledger_type", "wallet_table", "wallet_id_column", "balance_column", "tx_table", "tx_user_column", "refund_amount_column"),
    [
        (91001, "legacy", "user_wallets", "user_id", "paid_points", "point_transactions", "user_id", "amount"),
        (91002, "v35", "customer_agent_credit_wallets", "customer_user_id", "tool_credit_points", "customer_credit_transactions", "customer_user_id", "points"),
    ],
)
def test_twenty_concurrent_partial_and_full_targets_never_under_or_over_refund(
    billing_pg_schema,
    user_id,
    ledger_type,
    wallet_table,
    wallet_id_column,
    balance_column,
    tx_table,
    tx_user_column,
    refund_amount_column,
):
    import middleware.billing as billing

    key = f"report-export:{user_id}:twenty-partial-targets"
    charge = _deduct(user_id, key)
    charge_tx_id = int(charge["charge_tx_id"])

    def refund_to(target):
        return asyncio.run(
            billing.refund_points(
                user_id,
                "report_export",
                reason=f"concurrent cumulative target {target}",
                amount=target,
                charge_tx_id=charge_tx_id,
                ledger_type=ledger_type,
            )
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        partial_results = list(pool.map(lambda _: refund_to(6), range(20)))
    assert all(result["success"] is True for result in partial_results)
    assert sum(int(result["refunded"]) for result in partial_results) == 6
    assert all(int(result["confirmed_refunded"]) == 6 for result in partial_results)
    assert int(
        _fetch_one(
            billing_pg_schema,
            f"SELECT {balance_column} AS balance FROM {wallet_table} "
            f"WHERE {wallet_id_column}=%s",
            (user_id,),
        )["balance"]
    ) == 996

    with ThreadPoolExecutor(max_workers=20) as pool:
        full_results = list(pool.map(lambda _: refund_to(None), range(20)))
    assert all(result["success"] is True for result in full_results)
    assert sum(int(result["refunded"]) for result in full_results) == 4
    assert all(int(result["confirmed_refunded"]) == 10 for result in full_results)
    assert int(
        _fetch_one(
            billing_pg_schema,
            f"SELECT {balance_column} AS balance FROM {wallet_table} "
            f"WHERE {wallet_id_column}=%s",
            (user_id,),
        )["balance"]
    ) == 1000
    assert int(
        _fetch_one(
            billing_pg_schema,
            f"SELECT COALESCE(SUM({refund_amount_column}),0)::bigint AS refunded "
            f"FROM {tx_table} WHERE {tx_user_column}=%s AND type='refund'",
            (user_id,),
        )["refunded"]
    ) == 10


@pytest.mark.parametrize(("user_id", "ledger_type"), [(91001, "legacy"), (91002, "v35")])
def test_cumulative_partial_refund_preserves_split_pool_accounting(
    billing_pg_schema,
    monkeypatch,
    user_id,
    ledger_type,
):
    import middleware.billing as billing
    import services.channel_tier as channel_tier

    monkeypatch.setattr(channel_tier, "is_channel_tier_enabled", lambda _cursor: False)

    with billing_pg_schema["get_db"]() as conn:
        cursor = conn.cursor()
        if ledger_type == "legacy":
            cursor.execute(
                "UPDATE user_wallets SET bonus_points=4 WHERE user_id=%s",
                (user_id,),
            )
        else:
            cursor.execute(
                "UPDATE customer_agent_credit_wallets SET bonus_credit_points=4 "
                "WHERE customer_user_id=%s",
                (user_id,),
            )

    key = f"report-export:{user_id}:split-pool-partial"
    charge = _deduct(user_id, key)
    charge_tx_id = int(charge["charge_tx_id"])
    first = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="split pool target five",
            amount=5,
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )
    finish = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="split pool full target",
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )
    replay = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="split pool full replay",
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )

    assert first["refunded"] == 5
    assert first["confirmed_refunded"] == 5
    assert finish["refunded"] == 5
    assert finish["confirmed_refunded"] == 10
    assert replay["refunded"] == 0
    assert replay["already_refunded"] is True
    if ledger_type == "legacy":
        wallet = _fetch_one(
            billing_pg_schema,
            "SELECT paid_points,bonus_points FROM user_wallets WHERE user_id=%s",
            (user_id,),
        )
        assert int(wallet["paid_points"]) == 1000
        assert int(wallet["bonus_points"]) == 4
        over_refunded = _fetch_one(
            billing_pg_schema,
            """
            SELECT COUNT(*) AS count
              FROM point_transactions c
             WHERE c.user_id=%s AND c.type='consume' AND c.feature_code='report_export'
               AND (SELECT COALESCE(SUM(r.amount),0) FROM point_transactions r
                     WHERE r.user_id=c.user_id AND r.type='refund'
                       AND r.order_id=c.id::text) > ABS(c.amount)
            """,
            (user_id,),
        )
    else:
        wallet = _fetch_one(
            billing_pg_schema,
            "SELECT tool_credit_points,bonus_credit_points "
            "FROM customer_agent_credit_wallets WHERE customer_user_id=%s",
            (user_id,),
        )
        assert int(wallet["tool_credit_points"]) == 1000
        assert int(wallet["bonus_credit_points"]) == 4
        over_refunded = _fetch_one(
            billing_pg_schema,
            """
            SELECT COUNT(*) AS count
              FROM customer_credit_transactions c
             WHERE c.customer_user_id=%s AND c.type='consume'
               AND c.feature_code='report_export'
               AND (SELECT COALESCE(SUM(r.points),0) FROM customer_credit_transactions r
                     WHERE r.customer_user_id=c.customer_user_id AND r.type='refund'
                       AND r.source='tool_fail_refund'
                       AND r.related_order_id=c.id::text) > ABS(c.points)
            """,
            (user_id,),
        )
    assert int(over_refunded["count"]) == 0


@pytest.mark.parametrize(("user_id", "ledger_type"), [(91001, "legacy"), (91002, "v35")])
def test_over_refunded_split_leg_fails_closed_without_cross_pool_compensation(
    billing_pg_schema,
    monkeypatch,
    user_id,
    ledger_type,
):
    """One over-refunded leg cannot be hidden by another under-refunded leg."""
    import middleware.billing as billing
    import services.channel_tier as channel_tier

    monkeypatch.setattr(channel_tier, "is_channel_tier_enabled", lambda _cursor: False)
    with billing_pg_schema["get_db"]() as conn:
        cursor = conn.cursor()
        if ledger_type == "legacy":
            cursor.execute(
                "UPDATE user_wallets SET bonus_points=4 WHERE user_id=%s",
                (user_id,),
            )
        else:
            cursor.execute(
                "UPDATE customer_agent_credit_wallets SET bonus_credit_points=4 "
                "WHERE customer_user_id=%s",
                (user_id,),
            )

    key = f"report-export:{user_id}:over-refunded-leg"
    charge = _deduct(user_id, key)
    charge_tx_id = int(charge["charge_tx_id"])
    with billing_pg_schema["get_db"]() as conn:
        cursor = conn.cursor()
        if ledger_type == "legacy":
            cursor.execute(
                "SELECT id FROM point_transactions WHERE user_id=%s "
                "AND feature_code='report_export' AND type='consume' "
                "AND point_type='bonus'",
                (user_id,),
            )
            bonus_consume_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "UPDATE user_wallets SET bonus_points=bonus_points+5 WHERE user_id=%s",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO point_transactions
                    (user_id,type,point_type,amount,balance_after,feature_code,
                     description,order_id,source)
                VALUES (%s,'refund','bonus',5,5,'other_feature',
                        'injected over-refunded leg',%s,'balance_deduction')
                """,
                (user_id, str(bonus_consume_id)),
            )
        else:
            cursor.execute(
                "SELECT id FROM customer_credit_transactions "
                "WHERE customer_user_id=%s AND feature_code='report_export' "
                "AND type='consume' AND pool='bonus'",
                (user_id,),
            )
            bonus_consume_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "UPDATE customer_agent_credit_wallets "
                "SET bonus_credit_points=bonus_credit_points+5, "
                "total_consumed_points=GREATEST(0,total_consumed_points-5) "
                "WHERE customer_user_id=%s",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO customer_credit_transactions
                    (customer_user_id,agent_user_id,type,pool,points,
                     balance_tool_after,balance_publish_after,balance_bonus_after,
                     related_order_id,source,description)
                VALUES (%s,92002,'refund','bonus',5,994,0,5,%s,
                        'tool_fail_refund','injected over-refunded leg')
                """,
                (user_id, str(bonus_consume_id)),
            )

    with pytest.raises(RuntimeError, match="refund leg exceeds original charge"):
        asyncio.run(
            billing.refund_points(
                user_id,
                "report_export",
                reason="must fail closed on pool drift",
                charge_tx_id=charge_tx_id,
                ledger_type=ledger_type,
            )
        )

    assert _fetch_one(
        billing_pg_schema,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "charged"
    if ledger_type == "legacy":
        wallet = _fetch_one(
            billing_pg_schema,
            "SELECT paid_points,bonus_points FROM user_wallets WHERE user_id=%s",
            (user_id,),
        )
        assert int(wallet["paid_points"]) == 994
        assert int(wallet["bonus_points"]) == 5
    else:
        wallet = _fetch_one(
            billing_pg_schema,
            "SELECT tool_credit_points,bonus_credit_points "
            "FROM customer_agent_credit_wallets WHERE customer_user_id=%s",
            (user_id,),
        )
        assert int(wallet["tool_credit_points"]) == 994
        assert int(wallet["bonus_credit_points"]) == 5


def _insert_v35_cross_feature_refund_pollution(env, related_order_id: str) -> None:
    """Add a net-zero refund for another feature inside the same order group."""
    with env["get_db"]() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO customer_credit_transactions
                (customer_user_id,agent_user_id,type,pool,points,
                 balance_tool_after,balance_publish_after,balance_bonus_after,
                 feature_code,related_order_id,source,description)
            VALUES (91002,92002,'consume','tool',-6,984,0,0,
                    'other_feature',%s,'tool_consume','cross-feature consume')
            RETURNING id
            """,
            (related_order_id,),
        )
        other_consume_id = int(cursor.fetchone()["id"])
        cursor.execute(
            """
            INSERT INTO customer_credit_transactions
                (customer_user_id,agent_user_id,type,pool,points,
                 balance_tool_after,balance_publish_after,balance_bonus_after,
                 feature_code,related_order_id,source,description)
            VALUES (91002,92002,'refund','tool',6,990,0,0,
                    'other_feature',%s,'tool_fail_refund','cross-feature refund')
            """,
            (str(other_consume_id),),
        )


def test_v35_confirmed_refund_ignores_other_feature_in_same_order_group(
    billing_pg_schema,
):
    import middleware.billing as billing

    key = "report-export:91002:cross-feature-refund"
    charge = _deduct(91002, key)
    charge_tx_id = int(charge["charge_tx_id"])
    _insert_v35_cross_feature_refund_pollution(
        billing_pg_schema,
        str(charge["order_id"]),
    )

    result = asyncio.run(
        billing.refund_points(
            91002,
            "report_export",
            reason="only this feature may confirm",
            amount=4,
            charge_tx_id=charge_tx_id,
            ledger_type="v35",
        )
    )

    assert result["success"] is True
    assert result["refunded"] == 4
    assert result["confirmed_refunded"] == 4
    assert result["already_refunded"] is False
    assert _fetch_one(
        billing_pg_schema,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refund_pending"
    receipt = _fetch_one(
        billing_pg_schema,
        "SELECT status,refunded_points FROM billing_debt_offset_outbox "
        "WHERE ledger_type='v35' AND charge_tx_id=%s",
        (charge_tx_id,),
    )
    assert receipt["status"] not in ("cancelled", "reversed")
    assert int(receipt["refunded_points"]) == 4


def test_recovery_worker_rejects_cross_feature_refund_evidence(
    billing_pg_schema,
    monkeypatch,
):
    import db.fund_recovery_db as fund_recovery_db
    import middleware.billing as billing

    env = billing_pg_schema
    monkeypatch.setattr(
        fund_recovery_db,
        "_get_conn",
        lambda: _connect(env["dsn"], env["schema"]),
    )
    with env["get_db"]() as conn:
        fund_recovery_db.init_fund_recovery_tables(conn.cursor())

    key = "report-export:91002:cross-feature-recovery"
    charge = _deduct(91002, key)
    charge_tx_id = int(charge["charge_tx_id"])
    partial = asyncio.run(
        billing.refund_points(
            91002,
            "report_export",
            reason="legitimate partial refund",
            amount=4,
            charge_tx_id=charge_tx_id,
            ledger_type="v35",
        )
    )
    assert partial["confirmed_refunded"] == 4
    _insert_v35_cross_feature_refund_pollution(env, str(charge["order_id"]))
    order_id = fund_recovery_db.create_report_export_refund_recovery(
        "report_export",
        "refund",
        ref_key="diagnosis:cross-feature-recovery",
        user_id=91002,
        feature_code="report_export",
        charge_tx_id=charge_tx_id,
        amount_points=10,
        reason="must require exact feature evidence",
        payload={"idempotency_key": key},
        ledger_type="v35",
    )
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "UPDATE fund_recovery_orders SET status='processing', "
            "claim_token='cross-feature-proof',claimed_at=NOW() WHERE id=%s",
            (order_id,),
        )

    with pytest.raises(RuntimeError, match="refund evidence is incomplete"):
        fund_recovery_db.resolve_recovery_order_by_worker(
            order_id,
            "cross-feature-proof",
            note="polluted evidence must not resolve",
        )
    assert _fetch_one(
        env,
        "SELECT status FROM fund_recovery_orders WHERE id=%s",
        (order_id,),
    )["status"] == "processing"
    assert _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refund_pending"


@pytest.mark.parametrize(
    ("user_id", "ledger_type", "wallet_table", "wallet_id_column", "balance_column", "tx_table", "tx_user_column", "refund_amount_column"),
    [
        (91001, "legacy", "user_wallets", "user_id", "paid_points", "point_transactions", "user_id", "amount"),
        (91002, "v35", "customer_agent_credit_wallets", "customer_user_id", "tool_credit_points", "customer_credit_transactions", "customer_user_id", "points"),
    ],
)
def test_recovery_worker_continues_a_partial_refund_to_its_full_target(
    billing_pg_schema,
    monkeypatch,
    user_id,
    ledger_type,
    wallet_table,
    wallet_id_column,
    balance_column,
    tx_table,
    tx_user_column,
    refund_amount_column,
):
    import db.fund_recovery_db as fund_recovery_db
    import middleware.billing as billing
    from services.fund_recovery_processor import process_pending

    env = billing_pg_schema
    monkeypatch.setattr(
        fund_recovery_db,
        "_get_conn",
        lambda: _connect(env["dsn"], env["schema"]),
    )
    with env["get_db"]() as conn:
        fund_recovery_db.init_fund_recovery_tables(conn.cursor())

    key = f"report-export:{user_id}:partial-recovery"
    charge = _deduct(user_id, key)
    charge_tx_id = int(charge["charge_tx_id"])
    partial = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="response lost after partial refund",
            amount=4,
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )
    assert partial["confirmed_refunded"] == 4

    order_id = fund_recovery_db.create_report_export_refund_recovery(
        "report_export",
        "refund",
        ref_key=f"diagnosis:partial-recovery:{ledger_type}",
        user_id=user_id,
        feature_code="report_export",
        charge_tx_id=charge_tx_id,
        amount_points=10,
        reason="complete exact export refund",
        payload={"idempotency_key": key},
        ledger_type=ledger_type,
    )
    stats = asyncio.run(process_pending(limit=1))

    assert stats["claimed"] == 1
    assert stats["resolved"] == 1
    assert _fetch_one(
        env,
        "SELECT status FROM fund_recovery_orders WHERE id=%s",
        (order_id,),
    )["status"] == "resolved"
    assert _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refunded"
    assert int(
        _fetch_one(
            env,
            f"SELECT {balance_column} AS balance FROM {wallet_table} "
            f"WHERE {wallet_id_column}=%s",
            (user_id,),
        )["balance"]
    ) == 1000
    assert int(
        _fetch_one(
            env,
            f"SELECT COALESCE(SUM({refund_amount_column}),0)::bigint AS refunded "
            f"FROM {tx_table} WHERE {tx_user_column}=%s AND type='refund'",
            (user_id,),
        )["refunded"]
    ) == 10


@pytest.mark.parametrize(
    ("user_id", "ledger_type", "wallet_table", "wallet_id_column", "balance_column"),
    [
        (91001, "legacy", "user_wallets", "user_id", "paid_points"),
        (91002, "v35", "customer_agent_credit_wallets", "customer_user_id", "tool_credit_points"),
    ],
)
def test_kill_after_partial_refund_commit_can_raise_target_without_double_refund(
    billing_pg_schema,
    user_id,
    ledger_type,
    wallet_table,
    wallet_id_column,
    balance_column,
):
    import middleware.billing as billing

    env = billing_pg_schema
    key = f"report-export:{user_id}:partial-kill9"
    charge = _deduct(user_id, key)
    charge_tx_id = int(charge["charge_tx_id"])
    child_code = r'''
import asyncio
import os
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import db.wallet_db as wallet_db
import middleware.billing as billing

dsn = os.environ["IDEM_TEST_DSN"]
schema = os.environ["IDEM_TEST_SCHEMA"]
user_id = int(os.environ["IDEM_REFUND_USER"])
ledger_type = os.environ["IDEM_REFUND_LEDGER"]
charge_tx_id = int(os.environ["IDEM_REFUND_CHARGE"])

@contextmanager
def commit_then_hard_exit():
    conn = psycopg2.connect(
        dsn,
        options=f"-c search_path={schema},public",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        yield conn
        conn.commit()
        os._exit(137)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

billing.get_db = commit_then_hard_exit
wallet_db._POINT_TX_HAS_SOURCE = True
asyncio.run(billing.refund_points(
    user_id,
    "report_export",
    reason="partial refund commit before kill",
    amount=4,
    charge_tx_id=charge_tx_id,
    ledger_type=ledger_type,
))
'''
    child_env = os.environ.copy()
    child_env.update(
        {
            "IDEM_TEST_DSN": env["dsn"],
            "IDEM_TEST_SCHEMA": env["schema"],
            "IDEM_REFUND_USER": str(user_id),
            "IDEM_REFUND_LEDGER": ledger_type,
            "IDEM_REFUND_CHARGE": str(charge_tx_id),
        }
    )
    process = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 137, (process.stdout, process.stderr)
    assert int(
        _fetch_one(
            env,
            f"SELECT {balance_column} AS balance FROM {wallet_table} "
            f"WHERE {wallet_id_column}=%s",
            (user_id,),
        )["balance"]
    ) == 994

    continued = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="raise target after lost response",
            amount=6,
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )
    replay = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="replay raised target",
            amount=6,
            charge_tx_id=charge_tx_id,
            ledger_type=ledger_type,
        )
    )
    assert continued["refunded"] == 2
    assert continued["confirmed_refunded"] == 6
    assert replay["refunded"] == 0
    assert replay["already_refunded"] is True
    assert int(
        _fetch_one(
            env,
            f"SELECT {balance_column} AS balance FROM {wallet_table} "
            f"WHERE {wallet_id_column}=%s",
            (user_id,),
        )["balance"]
    ) == 996


def test_debt_offset_receipt_is_exactly_once_for_charge(
    billing_pg_schema,
    monkeypatch,
):
    import middleware.v3_3_1_debt_offset as debt_offset

    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    with billing_pg_schema["get_db"]() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bonus_clawback_pending
                (user_id,recharge_order_id,amount_due,amount_settled,status)
            VALUES (91001,'debt-once',100,0,'pending')
            """
        )
    key = "report-export:91001:debt-exactly-once"
    first = _deduct(91001, key)
    second = _deduct(91001, key)
    assert first == second
    debt = _fetch_one(
        billing_pg_schema,
        "SELECT amount_settled,status FROM bonus_clawback_pending "
        "WHERE recharge_order_id='debt-once'",
    )
    receipt = _fetch_one(
        billing_pg_schema,
        "SELECT status,retry_count FROM billing_debt_offset_outbox "
        "WHERE charge_tx_id=%s AND ledger_type='legacy'",
        (int(first["charge_tx_id"]),),
    )
    assert int(debt["amount_settled"]) == 10
    assert debt["status"] == "partial_settled"
    assert receipt["status"] == "completed"


def test_pending_debt_offset_is_recovered_on_idempotent_retry(
    billing_pg_schema,
    monkeypatch,
):
    import middleware.v3_3_1_debt_offset as debt_offset

    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    with billing_pg_schema["get_db"]() as conn:
        conn.cursor().execute(
            """
            INSERT INTO bonus_clawback_pending
                (user_id,recharge_order_id,amount_due,amount_settled,status)
            VALUES (91001,'debt-recovery',100,0,'pending')
            """
        )
    real_cursor_helper = debt_offset.offset_debts_after_consumption_cursor

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("injected debt table outage")

    monkeypatch.setattr(
        debt_offset,
        "offset_debts_after_consumption_cursor",
        unavailable,
    )
    key = "report-export:91001:debt-retry-recovery"
    first = _deduct(91001, key)
    pending = _fetch_one(
        billing_pg_schema,
        "SELECT status,retry_count FROM billing_debt_offset_outbox WHERE charge_tx_id=%s",
        (int(first["charge_tx_id"]),),
    )
    assert pending["status"] == "pending"
    assert int(pending["retry_count"]) == 1

    # One immediate request replay is allowed to close the commit/response kill
    # window. Further retries must honor durable backoff instead of exhausting
    # the row to manual while the same dependency is still unavailable.
    assert _deduct(91001, key) == first
    assert int(
        _fetch_one(
            billing_pg_schema,
            "SELECT retry_count FROM billing_debt_offset_outbox WHERE charge_tx_id=%s",
            (int(first["charge_tx_id"]),),
        )["retry_count"]
    ) == 2
    assert _deduct(91001, key) == first
    assert int(
        _fetch_one(
            billing_pg_schema,
            "SELECT retry_count FROM billing_debt_offset_outbox WHERE charge_tx_id=%s",
            (int(first["charge_tx_id"]),),
        )["retry_count"]
    ) == 2

    monkeypatch.setattr(
        debt_offset,
        "offset_debts_after_consumption_cursor",
        real_cursor_helper,
    )
    with billing_pg_schema["get_db"]() as conn:
        conn.cursor().execute(
            "UPDATE billing_debt_offset_outbox SET next_retry_at=NOW() "
            "WHERE charge_tx_id=%s",
            (int(first["charge_tx_id"]),),
        )
    replayed = _deduct(91001, key)
    assert replayed == first
    debt = _fetch_one(
        billing_pg_schema,
        "SELECT amount_settled FROM bonus_clawback_pending "
        "WHERE recharge_order_id='debt-recovery'",
    )
    receipt = _fetch_one(
        billing_pg_schema,
        "SELECT status FROM billing_debt_offset_outbox WHERE charge_tx_id=%s",
        (int(first["charge_tx_id"]),),
    )
    assert int(debt["amount_settled"]) == 10
    assert receipt["status"] == "completed"


def test_report_refund_recovery_atomically_blocks_replay_and_resolution_syncs(
    billing_pg_schema,
    monkeypatch,
):
    import db.fund_recovery_db as fund_recovery_db
    from services.report_export_billing import ReportExportCharge, ReportExportRefundError

    env = billing_pg_schema
    monkeypatch.setattr(
        fund_recovery_db,
        "_get_conn",
        lambda: _connect(env["dsn"], env["schema"]),
    )
    with env["get_db"]() as conn:
        fund_recovery_db.init_fund_recovery_tables(conn.cursor())

    key = "report-export:91001:refund-pending-block"
    charged = _deduct(91001, key)
    charge = ReportExportCharge(
        user_id=91001,
        diagnosis_id=7719,
        charged=True,
        charge_tx_id=int(charged["charge_tx_id"]),
        ledger_type="legacy",
        deducted=10,
        idempotency_key=key,
    )
    with pytest.raises(ValueError, match="amount must match exact charge"):
        fund_recovery_db.create_report_export_refund_recovery(
            "report_export",
            "refund",
            ref_key="diagnosis:7719",
            user_id=91001,
            feature_code="report_export",
            charge_tx_id=int(charged["charge_tx_id"]),
            amount_points=9,
            payload={"idempotency_key": key},
            ledger_type="legacy",
        )
    assert _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "charged"
    with env["get_db"]() as conn:
        conn.cursor().execute(
            """
            INSERT INTO fund_recovery_orders
                (source,ref_key,user_id,feature_code,charge_tx_id,amount_points,
                 kind,status,payload,ledger_type)
            VALUES ('report_export','diagnosis:7719',91001,'report_export',%s,10,
                    'refund','failed','{}'::jsonb,'legacy')
            """,
            (int(charged["charge_tx_id"]),),
        )

    async def refund_unavailable(*_args, **_kwargs):
        return {"success": False, "reason": "injected refund outage"}

    with pytest.raises(ReportExportRefundError) as exc_info:
        charge.refund("PDF 生成失败", refund_func=refund_unavailable)
    order_id = int(exc_info.value.recovery_order_id)
    state = _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )
    work_order = _fetch_one(
        env,
        "SELECT status,payload FROM fund_recovery_orders WHERE id=%s",
        (order_id,),
    )
    assert state["status"] == "refund_pending"
    assert work_order["status"] == "pending"
    assert work_order["payload"]["idempotency_key"] == key
    assert int(
        _fetch_one(
            env,
            "SELECT COUNT(*) AS count FROM fund_recovery_orders "
            "WHERE source='report_export' AND charge_tx_id=%s",
            (int(charged["charge_tx_id"]),),
        )["count"]
    ) == 2
    with pytest.raises(HTTPException) as pending_replay:
        _deduct(91001, key)
    assert pending_replay.value.detail["code"] == "IDEMPOTENCY_CHARGE_REFUND_PENDING"

    with env["get_db"]() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE fund_recovery_orders
               SET status='processing',claim_token='refund-sync-token',claimed_at=NOW()
             WHERE id=%s
            """,
            (order_id,),
        )
    with pytest.raises(RuntimeError, match="refund evidence is incomplete"):
        fund_recovery_db.resolve_recovery_order_by_worker(
            order_id,
            "refund-sync-token",
            note="unproven refund must not resolve",
        )
    assert _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refund_pending"
    assert _fetch_one(
        env,
        "SELECT status FROM fund_recovery_orders WHERE id=%s",
        (order_id,),
    )["status"] == "processing"

    with env["get_db"]() as conn:
        conn.cursor().execute(
            """
            INSERT INTO point_transactions
                (user_id,type,point_type,amount,balance_after,feature_code,
                 description,order_id,source)
            VALUES (91001,'refund','paid',5,995,'report_export',
                    'partial refund evidence',%s,'balance_deduction')
            """,
            (str(charged["charge_tx_id"]),),
        )
    with pytest.raises(RuntimeError, match="refund evidence is incomplete"):
        fund_recovery_db.resolve_recovery_order_by_worker(
            order_id,
            "refund-sync-token",
            note="partial refund evidence must not close the charge",
        )
    assert _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refund_pending"
    with env["get_db"]() as conn:
        conn.cursor().execute(
            """
            INSERT INTO point_transactions
                (user_id,type,point_type,amount,balance_after,feature_code,
                 description,order_id,source)
            VALUES (91001,'refund','paid',5,1000,'report_export',
                    'remaining refund evidence',%s,'balance_deduction')
            """,
            (str(charged["charge_tx_id"]),),
        )
    assert fund_recovery_db.resolve_recovery_order_by_worker(
        order_id,
        "refund-sync-token",
        note="refund ledger evidence confirmed",
    )
    assert _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refunded"
    assert _fetch_one(
        env,
        "SELECT status FROM fund_recovery_orders WHERE id=%s",
        (order_id,),
    )["status"] == "resolved"


def test_two_workers_dispatch_one_debt_offset_exactly_once(
    billing_pg_schema,
    monkeypatch,
):
    import middleware.v3_3_1_debt_offset as debt_offset
    import services.billing_debt_offset_outbox as outbox

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    monkeypatch.setattr(outbox, "get_db", env["get_db"])
    with env["get_db"]() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bonus_clawback_pending
                (user_id,recharge_order_id,amount_due,amount_settled,status)
            VALUES (91001,'two-worker-debt',100,0,'pending')
            """
        )
        cursor.execute(
            """
            INSERT INTO billing_debt_offset_outbox
                (event_key,user_id,feature_code,charge_tx_id,ledger_type,
                 consumed_points,status)
            VALUES ('billing_debt_offset:legacy:999991',91001,'report_export',
                    999991,'legacy',10,'pending')
            """
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _n: outbox.process_pending_debt_offsets(1), range(2)))
    assert sum(result["completed"] for result in results) == 1
    assert int(
        _fetch_one(
            env,
            "SELECT amount_settled FROM bonus_clawback_pending "
            "WHERE recharge_order_id='two-worker-debt'",
        )["amount_settled"]
    ) == 10
    assert _fetch_one(
        env,
        "SELECT status FROM billing_debt_offset_outbox "
        "WHERE event_key='billing_debt_offset:legacy:999991'",
    )["status"] == "completed"


@pytest.mark.parametrize(
    ("user_id", "ledger_type"),
    [(91001, "legacy"), (91002, "v35")],
)
def test_full_refund_cancels_pending_debt_offset_before_scheduler(
    billing_pg_schema,
    monkeypatch,
    user_id,
    ledger_type,
):
    """A fully refunded charge must never advance debt after the refund commits."""
    import middleware.billing as billing
    import middleware.v3_3_1_debt_offset as debt_offset
    import services.billing_debt_offset_outbox as outbox

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    real_offset = debt_offset.offset_debts_after_consumption_cursor

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("injected pending debt offset")

    monkeypatch.setattr(
        debt_offset,
        "offset_debts_after_consumption_cursor",
        unavailable,
    )
    monkeypatch.setattr(outbox, "get_db", env["get_db"])
    debt_ref = f"refund-before-scheduler-{ledger_type}"
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "INSERT INTO bonus_clawback_pending "
            "(user_id,recharge_order_id,amount_due,amount_settled,status) "
            "VALUES (%s,%s,100,0,'pending')",
            (user_id, debt_ref),
        )

    key = f"report-export:{user_id}:refund-before-scheduler"
    charged = _deduct(user_id, key)
    assert _fetch_one(
        env,
        "SELECT status FROM billing_debt_offset_outbox "
        "WHERE ledger_type=%s AND charge_tx_id=%s",
        (ledger_type, int(charged["charge_tx_id"])),
    )["status"] == "pending"

    monkeypatch.setattr(
        debt_offset,
        "offset_debts_after_consumption_cursor",
        real_offset,
    )
    refunded = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="export failed",
            charge_tx_id=int(charged["charge_tx_id"]),
            ledger_type=ledger_type,
        )
    )
    assert refunded["success"] is True
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "UPDATE billing_debt_offset_outbox SET next_retry_at=NOW() "
            "WHERE ledger_type=%s AND charge_tx_id=%s",
            (ledger_type, int(charged["charge_tx_id"])),
        )
    outbox.process_pending_debt_offsets(10)

    debt = _fetch_one(
        env,
        "SELECT amount_settled,status FROM bonus_clawback_pending "
        "WHERE recharge_order_id=%s",
        (debt_ref,),
    )
    receipt = _fetch_one(
        env,
        "SELECT status FROM billing_debt_offset_outbox "
        "WHERE ledger_type=%s AND charge_tx_id=%s",
        (ledger_type, int(charged["charge_tx_id"])),
    )
    assert int(debt["amount_settled"]) == 0
    assert debt["status"] == "pending"
    assert receipt["status"] == "cancelled"


@pytest.mark.parametrize(
    ("user_id", "ledger_type"),
    [(91001, "legacy"), (91002, "v35")],
)
def test_full_refund_exactly_reverses_completed_debt_receipt(
    billing_pg_schema,
    monkeypatch,
    user_id,
    ledger_type,
):
    """Refund must reverse the immutable receipt, not recalculate current debts."""
    import middleware.billing as billing
    import middleware.v3_3_1_debt_offset as debt_offset

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    debt_ref = f"refund-after-offset-{ledger_type}"
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "INSERT INTO bonus_clawback_pending "
            "(user_id,recharge_order_id,amount_due,amount_settled,status) "
            "VALUES (%s,%s,100,0,'pending')",
            (user_id, debt_ref),
        )

    key = f"report-export:{user_id}:refund-after-offset"
    charged = _deduct(user_id, key)
    assert int(
        _fetch_one(
            env,
            "SELECT amount_settled FROM bonus_clawback_pending "
            "WHERE recharge_order_id=%s",
            (debt_ref,),
        )["amount_settled"]
    ) == 10

    first = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="export failed",
            charge_tx_id=int(charged["charge_tx_id"]),
            ledger_type=ledger_type,
        )
    )
    second = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="duplicate retry",
            charge_tx_id=int(charged["charge_tx_id"]),
            ledger_type=ledger_type,
        )
    )
    assert first["success"] is True
    assert second["success"] is True
    debt = _fetch_one(
        env,
        "SELECT amount_settled,status FROM bonus_clawback_pending "
        "WHERE recharge_order_id=%s",
        (debt_ref,),
    )
    receipt = _fetch_one(
        env,
        "SELECT status,result_jsonb FROM billing_debt_offset_outbox "
        "WHERE ledger_type=%s AND charge_tx_id=%s",
        (ledger_type, int(charged["charge_tx_id"])),
    )
    assert int(debt["amount_settled"]) == 0
    assert debt["status"] == "pending"
    assert receipt["status"] == "reversed"


@pytest.mark.parametrize(
    ("user_id", "ledger_type"),
    [(91001, "legacy"), (91002, "v35")],
)
def test_twenty_refund_scheduler_racers_converge_to_zero_debt(
    billing_pg_schema,
    monkeypatch,
    user_id,
    ledger_type,
):
    """Any lock winner produces the same zero-debt, one-refund terminal state."""
    import middleware.billing as billing
    import middleware.v3_3_1_debt_offset as debt_offset
    import services.billing_debt_offset_outbox as outbox

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    real_offset = debt_offset.offset_debts_after_consumption_cursor
    monkeypatch.setattr(
        debt_offset,
        "offset_debts_after_consumption_cursor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("leave receipt pending for race")
        ),
    )
    monkeypatch.setattr(outbox, "get_db", env["get_db"])
    debt_ref = f"refund-worker-race-{ledger_type}"
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "INSERT INTO bonus_clawback_pending "
            "(user_id,recharge_order_id,amount_due,amount_settled,status) "
            "VALUES (%s,%s,100,0,'pending')",
            (user_id, debt_ref),
        )
    key = f"report-export:{user_id}:refund-worker-race"
    charged = _deduct(user_id, key)
    monkeypatch.setattr(
        debt_offset,
        "offset_debts_after_consumption_cursor",
        real_offset,
    )
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "UPDATE billing_debt_offset_outbox SET next_retry_at=NOW() "
            "WHERE ledger_type=%s AND charge_tx_id=%s",
            (ledger_type, int(charged["charge_tx_id"])),
        )

    def race_action(index: int):
        if index % 2:
            return asyncio.run(
                billing.refund_points(
                    user_id,
                    "report_export",
                    reason="concurrent export failure",
                    charge_tx_id=int(charged["charge_tx_id"]),
                    ledger_type=ledger_type,
                )
            )
        return outbox.process_pending_debt_offsets(1)

    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(race_action, range(20)))

    debt = _fetch_one(
        env,
        "SELECT amount_settled,status FROM bonus_clawback_pending "
        "WHERE recharge_order_id=%s",
        (debt_ref,),
    )
    receipt = _fetch_one(
        env,
        "SELECT status,refunded_points,reversed_points "
        "FROM billing_debt_offset_outbox WHERE ledger_type=%s AND charge_tx_id=%s",
        (ledger_type, int(charged["charge_tx_id"])),
    )
    lifecycle = _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )
    refund_table = (
        "point_transactions" if ledger_type == "legacy" else "customer_credit_transactions"
    )
    user_column = "user_id" if ledger_type == "legacy" else "customer_user_id"
    refund_count = _fetch_one(
        env,
        f"SELECT COUNT(*) AS count FROM {refund_table} "
        f"WHERE {user_column}=%s AND type='refund'",
        (user_id,),
    )
    assert int(debt["amount_settled"]) == 0
    assert debt["status"] == "pending"
    assert receipt["status"] in ("cancelled", "reversed")
    assert int(receipt["refunded_points"]) == 10
    assert lifecycle["status"] == "refunded"
    assert int(refund_count["count"]) == 1


@pytest.mark.parametrize(
    ("user_id", "ledger_type"),
    [(91001, "legacy"), (91002, "v35")],
)
def test_recovery_resolution_reverses_committed_debt_receipt(
    billing_pg_schema,
    monkeypatch,
    user_id,
    ledger_type,
):
    """A refund ledger committed before worker death is reconciled on recovery."""
    import db.fund_recovery_db as fund_recovery_db
    import middleware.v3_3_1_debt_offset as debt_offset

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    monkeypatch.setattr(
        fund_recovery_db,
        "_get_conn",
        lambda: _connect(env["dsn"], env["schema"]),
    )
    with env["get_db"]() as conn:
        cursor = conn.cursor()
        fund_recovery_db.init_fund_recovery_tables(cursor)
        cursor.execute(
            "INSERT INTO bonus_clawback_pending "
            "(user_id,recharge_order_id,amount_due,amount_settled,status) "
            "VALUES (%s,%s,100,0,'pending')",
            (user_id, f"recovery-debt-{ledger_type}"),
        )
    key = f"report-export:{user_id}:recovery-debt"
    charged = _deduct(user_id, key)
    order_id = fund_recovery_db.create_report_export_refund_recovery(
        "report_export",
        "refund",
        ref_key=f"diagnosis:recovery:{ledger_type}",
        user_id=user_id,
        feature_code="report_export",
        charge_tx_id=int(charged["charge_tx_id"]),
        amount_points=10,
        payload={"idempotency_key": key},
        ledger_type=ledger_type,
    )
    with env["get_db"]() as conn:
        cursor = conn.cursor()
        if ledger_type == "legacy":
            cursor.execute(
                "UPDATE user_wallets SET paid_points=paid_points+10 WHERE user_id=%s",
                (user_id,),
            )
            cursor.execute(
                "INSERT INTO point_transactions "
                "(user_id,type,point_type,amount,balance_after,feature_code,"
                "description,order_id,source) "
                "VALUES (%s,'refund','paid',10,1000,'report_export',"
                "'worker response lost',%s,'balance_deduction')",
                (user_id, str(charged["charge_tx_id"])),
            )
        else:
            cursor.execute(
                "UPDATE customer_agent_credit_wallets "
                "SET tool_credit_points=tool_credit_points+10 WHERE customer_user_id=%s",
                (user_id,),
            )
            cursor.execute(
                "INSERT INTO customer_credit_transactions "
                "(customer_user_id,agent_user_id,type,pool,points,"
                "balance_tool_after,balance_publish_after,balance_bonus_after,"
                "feature_code,related_order_id,source,description) "
                "VALUES (%s,92002,'refund','tool',10,1000,0,0,'report_export',"
                "%s,'tool_fail_refund','worker response lost')",
                (user_id, str(charged["charge_tx_id"])),
            )
        cursor.execute(
            "UPDATE fund_recovery_orders SET status='processing',"
            "claim_token='refund-recovery-proof',claimed_at=NOW() WHERE id=%s",
            (order_id,),
        )

    assert fund_recovery_db.resolve_recovery_order_by_worker(
        int(order_id),
        "refund-recovery-proof",
        note="ledger proof confirmed",
    )
    debt = _fetch_one(
        env,
        "SELECT amount_settled,status FROM bonus_clawback_pending "
        "WHERE recharge_order_id=%s",
        (f"recovery-debt-{ledger_type}",),
    )
    receipt = _fetch_one(
        env,
        "SELECT status FROM billing_debt_offset_outbox "
        "WHERE ledger_type=%s AND charge_tx_id=%s",
        (ledger_type, int(charged["charge_tx_id"])),
    )
    assert int(debt["amount_settled"]) == 0
    assert debt["status"] == "pending"
    assert receipt["status"] == "reversed"
    assert _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "refunded"
    assert _fetch_one(
        env,
        "SELECT status FROM fund_recovery_orders WHERE id=%s",
        (order_id,),
    )["status"] == "resolved"


def test_service_fee_refund_reverses_exact_decimal_receipt(
    billing_pg_schema,
    monkeypatch,
):
    import middleware.billing as billing
    import middleware.v3_3_1_debt_offset as debt_offset

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "INSERT INTO service_fee_clawback_pending "
            "(user_id,source_order_id,amount_due,amount_settled,status) "
            "VALUES (91001,'service-fee-reversal',1.00,0,'pending')"
        )
    charged = _deduct(91001, "report-export:91001:service-fee-reversal")
    before = _fetch_one(
        env,
        "SELECT amount_settled FROM service_fee_clawback_pending "
        "WHERE source_order_id='service-fee-reversal'",
    )
    assert str(before["amount_settled"]) == "0.08"
    asyncio.run(
        billing.refund_points(
            91001,
            "report_export",
            reason="export failed",
            charge_tx_id=int(charged["charge_tx_id"]),
            ledger_type="legacy",
        )
    )
    after = _fetch_one(
        env,
        "SELECT amount_settled,status FROM service_fee_clawback_pending "
        "WHERE source_order_id='service-fee-reversal'",
    )
    assert str(after["amount_settled"]) == "0.00"
    assert after["status"] == "pending"


@pytest.mark.parametrize(
    ("user_id", "ledger_type"),
    [(91001, "legacy"), (91002, "v35")],
)
def test_kill_after_refund_commit_retries_without_double_reversal(
    billing_pg_schema,
    monkeypatch,
    user_id,
    ledger_type,
):
    """A hard exit after commit leaves a fully replayable refund terminal state."""
    import middleware.billing as billing
    import middleware.v3_3_1_debt_offset as debt_offset

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    debt_ref = f"refund-kill9-{ledger_type}"
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "INSERT INTO bonus_clawback_pending "
            "(user_id,recharge_order_id,amount_due,amount_settled,status) "
            "VALUES (%s,%s,100,0,'pending')",
            (user_id, debt_ref),
        )
    key = f"report-export:{user_id}:refund-kill9"
    charged = _deduct(user_id, key)

    child_code = r'''
import asyncio
import os
from contextlib import contextmanager
import psycopg2
import psycopg2.extras
import db.wallet_db as wallet_db
import middleware.billing as billing

dsn = os.environ["IDEM_TEST_DSN"]
schema = os.environ["IDEM_TEST_SCHEMA"]
user_id = int(os.environ["IDEM_REFUND_USER"])
ledger_type = os.environ["IDEM_REFUND_LEDGER"]
charge_tx_id = int(os.environ["IDEM_REFUND_CHARGE"])

@contextmanager
def commit_then_hard_exit():
    conn = psycopg2.connect(
        dsn,
        options=f"-c search_path={schema},public",
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        yield conn
        conn.commit()
        os._exit(137)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

billing.get_db = commit_then_hard_exit
wallet_db._POINT_TX_HAS_SOURCE = True
asyncio.run(billing.refund_points(
    user_id,
    "report_export",
    reason="kill after commit",
    charge_tx_id=charge_tx_id,
    ledger_type=ledger_type,
))
'''
    child_env = os.environ.copy()
    child_env.update(
        {
            "IDEM_TEST_DSN": env["dsn"],
            "IDEM_TEST_SCHEMA": env["schema"],
            "IDEM_REFUND_USER": str(user_id),
            "IDEM_REFUND_LEDGER": ledger_type,
            "IDEM_REFUND_CHARGE": str(charged["charge_tx_id"]),
        }
    )
    process = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 137, (process.stdout, process.stderr)

    replay = asyncio.run(
        billing.refund_points(
            user_id,
            "report_export",
            reason="retry after response loss",
            charge_tx_id=int(charged["charge_tx_id"]),
            ledger_type=ledger_type,
        )
    )
    assert replay["success"] is True
    assert replay.get("already_refunded") is True
    debt = _fetch_one(
        env,
        "SELECT amount_settled,status FROM bonus_clawback_pending "
        "WHERE recharge_order_id=%s",
        (debt_ref,),
    )
    receipt = _fetch_one(
        env,
        "SELECT status,reversed_points FROM billing_debt_offset_outbox "
        "WHERE ledger_type=%s AND charge_tx_id=%s",
        (ledger_type, int(charged["charge_tx_id"])),
    )
    refund_table = (
        "point_transactions" if ledger_type == "legacy" else "customer_credit_transactions"
    )
    user_column = "user_id" if ledger_type == "legacy" else "customer_user_id"
    refund_count = _fetch_one(
        env,
        f"SELECT COUNT(*) AS count FROM {refund_table} "
        f"WHERE {user_column}=%s AND type='refund'",
        (user_id,),
    )
    assert int(debt["amount_settled"]) == 0
    assert debt["status"] == "pending"
    assert receipt["status"] == "reversed"
    assert int(receipt["reversed_points"]) == 10
    assert int(refund_count["count"]) == 1


def test_refund_reverses_only_its_receipt_and_migration_preserves_terminal(
    billing_pg_schema,
    monkeypatch,
):
    """Later debt advances stay intact; rerunning migration keeps the evidence."""
    import middleware.billing as billing
    import middleware.v3_3_1_debt_offset as debt_offset

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "INSERT INTO bonus_clawback_pending "
            "(user_id,recharge_order_id,amount_due,amount_settled,status) "
            "VALUES (91001,'two-charge-exact-reversal',100,0,'pending')"
        )
    first = _deduct(91001, "report-export:91001:first-debt-receipt")
    second = _deduct(91001, "report-export:91001:second-debt-receipt")
    assert int(
        _fetch_one(
            env,
            "SELECT amount_settled FROM bonus_clawback_pending "
            "WHERE recharge_order_id='two-charge-exact-reversal'",
        )["amount_settled"]
    ) == 20

    asyncio.run(
        billing.refund_points(
            91001,
            "report_export",
            reason="first export failed",
            charge_tx_id=int(first["charge_tx_id"]),
            ledger_type="legacy",
        )
    )
    assert int(
        _fetch_one(
            env,
            "SELECT amount_settled FROM bonus_clawback_pending "
            "WHERE recharge_order_id='two-charge-exact-reversal'",
        )["amount_settled"]
    ) == 10
    assert _fetch_one(
        env,
        "SELECT status FROM billing_debt_offset_outbox "
        "WHERE ledger_type='legacy' AND charge_tx_id=%s",
        (int(first["charge_tx_id"]),),
    )["status"] == "reversed"
    assert _fetch_one(
        env,
        "SELECT status FROM billing_debt_offset_outbox "
        "WHERE ledger_type='legacy' AND charge_tx_id=%s",
        (int(second["charge_tx_id"]),),
    )["status"] == "completed"

    conn = _connect(env["dsn"], env["schema"])
    _apply_migration(conn)
    _apply_migration(conn)
    conn.close()
    receipt = _fetch_one(
        env,
        "SELECT status,refunded_points,reversed_points,reversal_jsonb "
        "FROM billing_debt_offset_outbox WHERE ledger_type='legacy' AND charge_tx_id=%s",
        (int(first["charge_tx_id"]),),
    )
    assert receipt["status"] == "reversed"
    assert int(receipt["refunded_points"]) == 10
    assert int(receipt["reversed_points"]) == 10
    assert receipt["reversal_jsonb"]["items"]


def test_unreversible_receipt_rolls_back_wallet_refund_and_lifecycle(
    billing_pg_schema,
    monkeypatch,
):
    """Corrupt debt state fails closed without committing a one-sided refund."""
    import middleware.billing as billing
    import middleware.v3_3_1_debt_offset as debt_offset

    env = billing_pg_schema
    monkeypatch.setattr(debt_offset, "is_v3_3_1_enabled", lambda: True)
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "INSERT INTO bonus_clawback_pending "
            "(user_id,recharge_order_id,amount_due,amount_settled,status) "
            "VALUES (91001,'corrupt-reversal-proof',100,0,'pending')"
        )
    key = "report-export:91001:corrupt-reversal-proof"
    charged = _deduct(91001, key)
    with env["get_db"]() as conn:
        conn.cursor().execute(
            "UPDATE bonus_clawback_pending SET amount_settled=0,status='pending' "
            "WHERE recharge_order_id='corrupt-reversal-proof'"
        )

    with pytest.raises(RuntimeError, match="can no longer be reversed exactly"):
        asyncio.run(
            billing.refund_points(
                91001,
                "report_export",
                reason="must roll back",
                charge_tx_id=int(charged["charge_tx_id"]),
                ledger_type="legacy",
            )
        )
    assert int(
        _fetch_one(
            env,
            "SELECT paid_points FROM user_wallets WHERE user_id=91001",
        )["paid_points"]
    ) == 990
    assert int(
        _fetch_one(
            env,
            "SELECT COUNT(*) AS count FROM point_transactions "
            "WHERE user_id=91001 AND type='refund'",
        )["count"]
    ) == 0
    assert _fetch_one(
        env,
        "SELECT status FROM billing_deduction_idempotency WHERE idempotency_key=%s",
        (key,),
    )["status"] == "charged"


def test_scheduler_registers_durable_debt_offset_worker():
    """Deleting the production retry hook must make the billing gate fail."""
    scheduler_source = (
        Path(__file__).resolve().parents[1] / "api" / "scheduler.py"
    ).read_text(encoding="utf-8")

    assert "def _billing_debt_offset_processor_job" in scheduler_source
    assert "process_pending_debt_offsets" in scheduler_source
    assert 'id="billing_debt_offset_processor"' in scheduler_source
    assert "IntervalTrigger(minutes=1)" in scheduler_source
