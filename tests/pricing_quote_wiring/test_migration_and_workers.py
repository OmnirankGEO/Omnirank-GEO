"""Migration lifecycle, guard tamper detection, and four-process catalog evidence."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg2
import psycopg2.extras

from db.connection import get_db
from services.pricing_readiness import _check_schema

from helpers import publish_procurement


ROOT = Path(__file__).resolve().parents[2]
EXACT_URL = (
    "postgresql://pricing_test:pricing_test_pw@127.0.0.1:55444/"
    "omnirank_pricing_quote_test"
)

MIGRATION_BASE_DDL = """
CREATE TABLE system_settings (
  key VARCHAR(100) PRIMARY KEY, value TEXT, value_type VARCHAR(20),
  description TEXT, updated_at TIMESTAMPTZ DEFAULT NOW(), updated_by INTEGER
);
CREATE TABLE users (id INTEGER PRIMARY KEY, is_active INTEGER DEFAULT 1);
CREATE TABLE recharge_orders (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
  base_points BIGINT NOT NULL DEFAULT 0, bonus_points BIGINT NOT NULL DEFAULT 0,
  payment_method TEXT, payment_status TEXT DEFAULT 'pending', payment_id TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(), paid_at TIMESTAMPTZ,
  pricing_snapshot_jsonb JSONB
);
CREATE TABLE point_transactions (
  id BIGSERIAL PRIMARY KEY, user_id INTEGER, type TEXT, point_type TEXT,
  amount BIGINT, balance_after BIGINT, created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE customer_credit_transactions (
  id BIGSERIAL PRIMARY KEY, customer_user_id INTEGER, agent_user_id INTEGER,
  type TEXT, pool TEXT, points BIGINT, created_at TIMESTAMPTZ DEFAULT NOW()
);
"""


def _load_migration(filename: str, *, strip_transaction: bool = False) -> str:
    text = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
    text = re.sub(r"^\s*\\[a-zA-Z_]+.*$", "", text, flags=re.MULTILINE)
    if strip_transaction:
        text = re.sub(r"^\s*BEGIN\s*;\s*$", "", text, flags=re.MULTILINE | re.IGNORECASE)
        text = re.sub(r"^\s*COMMIT\s*;\s*$", "", text, flags=re.MULTILINE | re.IGNORECASE)
    return text


DUAL_SQL = _load_migration("migration_pricing_dual_ssot_2026_07_12.sql")
WIRING_SQL = _load_migration("migration_pricing_quote_wiring_2026_07_14.sql")
WIRING_BODY = _load_migration(
    "migration_pricing_quote_wiring_2026_07_14.sql", strip_transaction=True
)


def _nested_schema():
    schema = f"pricing_migration_{os.getpid()}_{secrets.token_hex(4)}"
    conn = psycopg2.connect(EXACT_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f'CREATE SCHEMA "{schema}"')
    cur.execute(f'SET search_path TO "{schema}"')
    cur.execute(MIGRATION_BASE_DDL)
    return schema, conn, cur


def _drop_nested(schema: str, conn, cur) -> None:
    try:
        if not conn.autocommit:
            conn.rollback()
            conn.autocommit = True
        cur.execute("SET search_path TO public")
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        cur.close()
        conn.close()


def test_migration_fresh_then_second_run_is_idempotent_and_fully_guarded():
    schema, conn, cur = _nested_schema()
    try:
        cur.execute(DUAL_SQL)
        cur.execute(WIRING_SQL)
        cur.execute(WIRING_SQL)

        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema=%s AND (
                 (table_name='pricing_catalog_entries' AND column_name='source_ref_jsonb') OR
                 (table_name='price_quotes' AND column_name='pricing_snapshot_jsonb')
               ) ORDER BY column_name""",
            (schema,),
        )
        assert [row["column_name"] for row in cur.fetchall()] == [
            "pricing_snapshot_jsonb",
            "source_ref_jsonb",
        ]
        cur.execute(
            """SELECT conname, convalidated FROM pg_constraint
               WHERE connamespace=%s::regnamespace
                 AND conname IN (
                   'fk_price_quotes_catalog_version_id',
                   'fk_recharge_orders_price_quote_id',
                   'fk_channel_relationship_buyer_user',
                   'fk_channel_relationship_upstream_user',
                   'chk_price_quote_snapshot_catalog_id',
                   'chk_channel_relationship_multiplier_floor',
                   'chk_channel_revenue_nonnegative'
                 )""",
            (schema,),
        )
        guards = {row["conname"]: row["convalidated"] for row in cur.fetchall()}
        assert len(guards) == 7
        assert all(guards.values())
        cur.execute(
            """SELECT COUNT(*) AS c FROM pg_trigger t
               JOIN pg_class c ON c.oid=t.tgrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=%s AND NOT t.tgisinternal
                 AND t.tgname IN (
                   'trg_pricing_catalog_entry_draft_only',
                   'trg_pricing_catalog_version_immutable',
                   'trg_price_quote_immutable',
                   'trg_recharge_order_pricing_immutable'
                 )""",
            (schema,),
        )
        assert cur.fetchone()["c"] == 4
    finally:
        _drop_nested(schema, conn, cur)


def test_migration_rollback_removes_all_followup_ddl_then_forward_succeeds():
    schema, conn, cur = _nested_schema()
    try:
        cur.execute(DUAL_SQL)
        conn.autocommit = False
        cur.execute(WIRING_BODY)
        cur.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema=%s AND table_name='price_quotes'
                 AND column_name='pricing_snapshot_jsonb'""",
            (schema,),
        )
        assert cur.fetchone() is not None
        conn.rollback()

        cur.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema=%s AND table_name='price_quotes'
                 AND column_name='pricing_snapshot_jsonb'""",
            (schema,),
        )
        assert cur.fetchone() is None
        conn.rollback()
        conn.autocommit = True
        cur.execute(WIRING_SQL)
        cur.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema=%s AND table_name='price_quotes'
                 AND column_name='pricing_snapshot_jsonb'""",
            (schema,),
        )
        assert cur.fetchone() is not None
    finally:
        _drop_nested(schema, conn, cur)


def test_readiness_detects_constraint_tamper_inside_rollback_only_transaction():
    with get_db() as conn:
        healthy = _check_schema(conn.cursor())
    assert healthy["ready"] is True, healthy["problems"]

    conn = psycopg2.connect(
        os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor
    )
    try:
        cur = conn.cursor()
        cur.execute(
            "ALTER TABLE price_quotes DROP CONSTRAINT fk_price_quotes_catalog_version_id"
        )
        tampered = _check_schema(cur)
        assert tampered["ready"] is False
        assert any("报价目录外键" in problem for problem in tampered["problems"])
        conn.rollback()
    finally:
        conn.close()

    with get_db() as conn:
        restored = _check_schema(conn.cursor())
    assert restored["ready"] is True, restored["problems"]


def test_four_independent_processes_read_the_same_published_version():
    version_id = publish_procurement(version_code="proc-workers-v4")
    code = (
        "import os, psycopg2; "
        "c=psycopg2.connect(os.environ['DATABASE_URL']); "
        "q=c.cursor(); "
        "q.execute(\"SELECT id,version_code FROM pricing_catalog_versions "
        "WHERE catalog_type='procurement' AND scope_key='PLATFORM_BASE' "
        "AND status='published' AND effective_to IS NULL\"); "
        "r=q.fetchone(); print(f'{r[0]}:{r[1]}'); c.close()"
    )

    def read_one(_index: int) -> str:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": os.environ["DATABASE_URL"]},
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        return completed.stdout.strip()

    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(read_one, range(4)))
    assert observed == [f"{version_id}:proc-workers-v4"] * 4
