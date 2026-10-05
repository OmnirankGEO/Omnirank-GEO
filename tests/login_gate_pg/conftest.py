"""真 PostgreSQL 16 隔离 schema · 登录协议门禁 + 零售目录守卫(2026-07-29)

为什么必须上真库:本单三条锁验的都是"写进去了没有 / 拦住了没有" ——
`agreement_signatures` 的 ON CONFLICT 幂等、`ux_catalog_published_open` 排他索引、
零条目守卫的"拒绝时一行都不写",假 cursor 一律证不了。

列名与类型全部对齐 2026-07-29 生产 information_schema 实读结果,
不是照着代码猜的(SQL 4 维核验 · 维度 1/2)。

每个 pytest 进程独占一个 schema,search_path 只指向它,不碰 public。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import quote

import psycopg2
import psycopg2.extras
import pytest

THROWAWAY_URL = os.environ.get(
    "LOGIN_GATE_PG_URL",
    "postgresql://geo_admin:test@127.0.0.1:55990/test_geo_agentscope",
)

SCHEMA = f"login_gate_{os.getpid()}_{secrets.token_hex(4)}"

with psycopg2.connect(THROWAWAY_URL) as _bootstrap:
    _bootstrap.autocommit = True
    with _bootstrap.cursor() as _cur:
        _cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

SCOPED_URL = f"{THROWAWAY_URL}?options={quote(f'-csearch_path={SCHEMA}', safe='')}"
os.environ["DATABASE_URL"] = SCOPED_URL

# 与生产 information_schema 逐列对齐(2026-07-29 实读)。
BASE_DDL = """
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE,
    password_hash TEXT,
    is_active INTEGER DEFAULT 1,
    phone_verified BOOLEAN DEFAULT FALSE
);
CREATE TABLE agreement_signatures (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    agreement_type VARCHAR(50) NOT NULL,
    agreement_version VARCHAR(50) NOT NULL,
    signed_at TIMESTAMP DEFAULT NOW(),
    ip_address VARCHAR(64),
    user_agent TEXT,
    content_hash TEXT,
    evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX idx_agreement_signatures_unique
    ON agreement_signatures (user_id, agreement_type, agreement_version);
CREATE TABLE pricing_catalog_versions (
    id BIGSERIAL PRIMARY KEY,
    catalog_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    effective_from TIMESTAMPTZ,
    effective_to TIMESTAMPTZ,
    reason TEXT,
    created_by INTEGER,
    approved_by INTEGER,
    approved_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    calc_meta_jsonb JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX ux_catalog_version_code
    ON pricing_catalog_versions (catalog_type, scope_key, version_code);
CREATE UNIQUE INDEX ux_catalog_published_open
    ON pricing_catalog_versions (catalog_type, scope_key)
    WHERE status = 'published' AND effective_to IS NULL;
CREATE TABLE pricing_catalog_entries (
    id BIGSERIAL PRIMARY KEY,
    version_id BIGINT NOT NULL,
    product_code TEXT NOT NULL,
    base_price_cents INTEGER NOT NULL,
    multiplier_bps INTEGER NOT NULL DEFAULT 10000,
    final_price_cents INTEGER NOT NULL,
    paid_points BIGINT NOT NULL,
    bonus_points BIGINT NOT NULL DEFAULT 0,
    cost_floor_cents INTEGER,
    usage_example_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_ref_jsonb JSONB,
    UNIQUE (version_id, product_code)
);
"""


def _connect():
    conn = psycopg2.connect(SCOPED_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


@pytest.fixture(scope="session", autouse=True)
def _schema():
    conn = _connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(BASE_DDL)
        # 门禁计数表由本单迁移创建 —— 直接跑真迁移文件,顺带证明它能落地且幂等。
        sql = (
            Path(__file__).resolve().parents[2]
            / "scripts" / "migration_agreement_gate_observability_2026_07_29.sql"
        ).read_text(encoding="utf-8")
        # 迁移刻意 pin 到 public(生产就该这样);隔离 schema 下把限定名与
        # information_schema 断言里的 schema 字面量一并换成本 schema。
        scoped = sql.replace("public.", f'"{SCHEMA}".').replace("'public'", f"'{SCHEMA}'")
        cur.execute(scoped)
        cur.execute(scoped)  # 幂等:连跑两次不得抛
    conn.close()
    yield
    conn = _connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA "{SCHEMA}" CASCADE')
    conn.close()


@pytest.fixture
def pg():
    conn = _connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        for table in (
            "registration_agreement_gate_events",
            "pricing_catalog_entries", "pricing_catalog_versions",
            "agreement_signatures", "users",
        ):
            cur.execute(f'TRUNCATE TABLE "{SCHEMA}".{table} RESTART IDENTITY CASCADE')
    yield conn
    conn.close()
