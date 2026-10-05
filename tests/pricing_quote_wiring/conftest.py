"""Isolated PostgreSQL fixture for the pricing quote wiring acceptance suite.

The suite is intentionally pinned to the disposable database created for this
task.  Every pytest process receives a private schema and the application pool
connects with that schema in ``search_path``; no public-schema or production
state is touched.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote

import psycopg2
import psycopg2.extras
import pytest

from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块


ROOT = Path(__file__).resolve().parents[2]
EXACT_THROWAWAY_URL = (
    "postgresql://pricing_test:pricing_test_pw@127.0.0.1:55444/"
    "omnirank_pricing_quote_test"
)

configured_url = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
if configured_url != EXACT_THROWAWAY_URL:
    raise RuntimeError(
        "pricing_quote_wiring tests are locked to the task throwaway PostgreSQL; "
        f"expected {EXACT_THROWAWAY_URL!r}, got {configured_url!r}"
    )

SCHEMA = f"pricing_wiring_{os.getpid()}_{secrets.token_hex(4)}"

# Create the namespace before business modules are imported during collection,
# then point db.connection's process-local pool at that namespace only.
with psycopg2.connect(EXACT_THROWAWAY_URL) as bootstrap_conn:
    bootstrap_conn.autocommit = True
    with bootstrap_conn.cursor() as bootstrap_cur:
        bootstrap_cur.execute(f'CREATE SCHEMA "{SCHEMA}"')

SCOPED_DATABASE_URL = (
    f"{EXACT_THROWAWAY_URL}?options={quote(f'-csearch_path={SCHEMA}', safe='')}"
)
os.environ["DATABASE_URL"] = SCOPED_DATABASE_URL


BASE_DDL = """
CREATE TABLE _migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE system_settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT,
    value_type VARCHAR(20) DEFAULT 'string',
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    updated_by INTEGER
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    phone TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    agent_sku_markup_ratio NUMERIC(4,2)
);

-- brands: 见 isolated_pricing_schema 里的 ensure_brands_schema()（生产 SSOT 出口）。

CREATE TABLE roles (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id),
    role_id INTEGER NOT NULL REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE user_notifications (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    type VARCHAR(50) NOT NULL DEFAULT 'system',
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    link VARCHAR(500),
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE user_wallets (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    paid_points BIGINT NOT NULL DEFAULT 0,
    bonus_points BIGINT NOT NULL DEFAULT 0,
    total_recharged BIGINT NOT NULL DEFAULT 0,
    agent_level INTEGER NOT NULL DEFAULT 0,
    agent_tier TEXT,
    deduction_preference TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE agent_pricing_overrides (
    agent_user_id INTEGER PRIMARY KEY,
    quote_markup_override NUMERIC(4,2),
    sku_markup_override NUMERIC(4,2),
    wholesale_numer INTEGER,
    wholesale_denom INTEGER,
    note TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER
);

CREATE TABLE sku_templates (
    id SERIAL PRIMARY KEY,
    template_code TEXT UNIQUE NOT NULL,
    sku_type TEXT,
    default_name TEXT,
    default_subtitle TEXT,
    recommended_use_jsonb JSONB,
    points_granted BIGINT,
    wholesale_cents INTEGER,
    suggested_retail_cents INTEGER,
    is_active BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE agent_sku_overrides (
    id SERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    sku_template_id INTEGER,
    retail_cents INTEGER NOT NULL,
    custom_name TEXT,
    custom_subtitle TEXT,
    custom_sales_pitch TEXT,
    custom_scene TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    margin_warning TEXT,
    sort_order INTEGER DEFAULT 0,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE customer_agent_bindings (
    id SERIAL PRIMARY KEY,
    customer_user_id INTEGER UNIQUE NOT NULL,
    agent_user_id INTEGER NOT NULL,
    binding_source TEXT,
    source_token TEXT,
    bound_at TIMESTAMPTZ DEFAULT NOW(),
    dispute_status TEXT,
    dispute_note TEXT
);

CREATE TABLE referral_links (
    id BIGSERIAL PRIMARY KEY,
    referrer_id INTEGER,
    referred_id INTEGER,
    level INTEGER,
    commission_rate NUMERIC(4,3),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (referrer_id, referred_id)
);

CREATE TABLE invite_codes (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    code TEXT UNIQUE NOT NULL,
    code_type TEXT NOT NULL DEFAULT 'user',
    expires_at TIMESTAMP,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    revoked_at TIMESTAMP,
    revoked_reason TEXT,
    used_by_user_id INTEGER,
    used_at TIMESTAMP
);

CREATE TABLE purchase_agreement_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    agreement_type TEXT NOT NULL,
    agreement_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_address TEXT,
    user_agent TEXT,
    surface TEXT NOT NULL,
    evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE referral_codes (
    user_id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL
);

CREATE TABLE recharge_orders (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    base_points BIGINT NOT NULL DEFAULT 0,
    bonus_points BIGINT NOT NULL DEFAULT 0,
    payment_method TEXT,
    payment_status TEXT DEFAULT 'pending',
    payment_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    paid_at TIMESTAMPTZ,
    order_type TEXT,
    agent_user_id INTEGER,
    sku_template_id INTEGER,
    override_id INTEGER,
    binding_source TEXT,
    source_token TEXT,
    pricing_snapshot_jsonb JSONB,
    settlement_snapshot_jsonb JSONB,
    settlement_mode TEXT,
    factory_cents INTEGER,
    gateway_fee_bps INTEGER,
    gateway_fee_cents INTEGER,
    settlement_service_fee_bps INTEGER,
    settlement_service_fee_cents INTEGER,
    agent_margin_before_tax_cents INTEGER,
    tax_rate_bps INTEGER,
    tax_withholding_cents INTEGER,
    agent_revenue_cents INTEGER,
    refund_status TEXT,
    agent_inventory_writer_generation INTEGER,
    agent_inventory_legacy_eligible BOOLEAN
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
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE customer_credit_transactions (
    id BIGSERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL,
    agent_user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    pool TEXT NOT NULL,
    points BIGINT NOT NULL,
    balance_tool_after BIGINT NOT NULL DEFAULT 0,
    balance_publish_after BIGINT NOT NULL DEFAULT 0,
    balance_bonus_after BIGINT NOT NULL DEFAULT 0,
    feature_code TEXT,
    related_order_id TEXT,
    source TEXT,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE customer_agent_credit_wallets (
    customer_user_id INTEGER PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    tool_credit_points BIGINT NOT NULL DEFAULT 0,
    publish_credit_points BIGINT NOT NULL DEFAULT 0,
    bonus_credit_points BIGINT NOT NULL DEFAULT 0,
    total_purchased_points BIGINT NOT NULL DEFAULT 0,
    total_consumed_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CHECK (tool_credit_points >= 0),
    CHECK (publish_credit_points >= 0),
    CHECK (bonus_credit_points >= 0)
);

CREATE TABLE agent_inventory_wallets (
    agent_user_id INTEGER PRIMARY KEY,
    paid_inventory_points BIGINT NOT NULL DEFAULT 0,
    bonus_inventory_points BIGINT NOT NULL DEFAULT 0,
    frozen_inventory_points BIGINT NOT NULL DEFAULT 0,
    total_purchased_points BIGINT NOT NULL DEFAULT 0,
    total_allocated_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CHECK (paid_inventory_points >= 0),
    CHECK (bonus_inventory_points >= 0),
    CHECK (frozen_inventory_points >= 0)
);

CREATE TABLE agent_inventory_transactions (
    id BIGSERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    pool TEXT NOT NULL,
    points BIGINT NOT NULL,
    balance_paid_after BIGINT NOT NULL,
    balance_bonus_after BIGINT NOT NULL,
    related_customer_user_id INTEGER,
    related_order_id TEXT,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE agent_tax_profiles (
    agent_user_id INTEGER PRIMARY KEY,
    entity_type TEXT,
    default_tax_rate_bps INTEGER,
    default_tax_mode TEXT,
    tax_id TEXT,
    invoice_capability TEXT,
    notes TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE agent_rebate_config (
    agent_user_id INTEGER PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    rebate_rate NUMERIC NOT NULL DEFAULT 0,
    max_rebate_points_per_order BIGINT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE agent_revenue_ledger (
    id BIGSERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('recharge','refund_clawback','tax_refund','adjust')),
    recharge_order_id TEXT,
    customer_user_id INTEGER,
    customer_paid_cents INTEGER NOT NULL,
    factory_cents INTEGER NOT NULL,
    gateway_fee_bps INTEGER NOT NULL DEFAULT 90,
    gateway_fee_cents INTEGER NOT NULL DEFAULT 0,
    settlement_service_fee_bps INTEGER NOT NULL DEFAULT 190,
    settlement_service_fee_cents INTEGER NOT NULL DEFAULT 0,
    agent_margin_before_tax_cents INTEGER NOT NULL,
    tax_rate_bps INTEGER NOT NULL DEFAULT 600,
    tax_mode TEXT NOT NULL DEFAULT 'withheld'
        CHECK (tax_mode IN ('withheld','invoice_provided','exempt_manual')),
    tax_withholding_cents INTEGER NOT NULL DEFAULT 0,
    invoice_status TEXT NOT NULL DEFAULT 'none'
        CHECK (invoice_status IN ('none','submitted','approved','rejected','refunded')),
    invoice_id INTEGER,
    agent_settlement_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'frozen'
        CHECK (status IN ('frozen','settled','cancelled')),
    frozen_at TIMESTAMPTZ DEFAULT NOW(),
    settle_at TIMESTAMPTZ NOT NULL,
    settled_at TIMESTAMPTZ,
    settlement_request_id INTEGER,
    manual_review_required BOOLEAN DEFAULT FALSE,
    reversed_at TIMESTAMPTZ,
    reversed_by_ledger_id BIGINT,
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""


def migration_sql(filename: str, *, strip_transaction: bool = False) -> str:
    """Load a repository migration and remove psql-only metacommands.

    ``strip_transaction`` is used only by rollback tests that need to own the
    outer transaction themselves.
    """

    text = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
    text = re.sub(r"^\s*\\[a-zA-Z_]+.*$", "", text, flags=re.MULTILINE)
    if strip_transaction:
        text = re.sub(r"^\s*BEGIN\s*;\s*$", "", text, flags=re.MULTILINE | re.IGNORECASE)
        text = re.sub(r"^\s*COMMIT\s*;\s*$", "", text, flags=re.MULTILINE | re.IGNORECASE)
    return text


DUAL_MIGRATION = "migration_pricing_dual_ssot_2026_07_12.sql"
RETAIL_SKU_MIGRATION = "migration_agent_retail_sku_decoupling_2026_07_17.sql"
WIRING_MIGRATION = "migration_pricing_quote_wiring_2026_07_14.sql"
NOTIFICATION_MIGRATION = "migration_notification_outbox_2026_07_17.sql"


def seed_business_rows(cur) -> None:
    cur.execute(
        """
        INSERT INTO users(id, username, is_active) VALUES
          (100, 'dealer-100', 1), (200, 'channel-200', 1),
          (300, 'channel-300', 1), (400, 'customer-400', 1),
          (500, 'customer-500', 1)
        ON CONFLICT (id) DO NOTHING
        """
    )
    cur.execute(
        """
        INSERT INTO user_wallets(user_id, agent_level) VALUES
          (100, 1), (200, 1), (300, 1), (400, 0), (500, 0)
        ON CONFLICT (user_id) DO NOTHING
        """
    )
    cur.execute(
        """
        INSERT INTO sku_templates(
          id, template_code, sku_type, default_name, points_granted,
          wholesale_cents, suggested_retail_cents, is_active
        ) VALUES (1, 'credit_basic', 'credit_pack', '基础算力包', 195000, 135000, 180000, TRUE)
        ON CONFLICT (id) DO NOTHING
        """
    )
    cur.execute(
        """
        INSERT INTO system_settings(key, value, value_type, description) VALUES
          ('PRICING_DUAL_SSOT_ENABLED', 'false', 'boolean', 'test flag'),
          ('CHANNEL_PRICING_ENABLED', 'false', 'boolean', 'test flag'),
          ('PRICING_QUOTE_REQUIRED', 'false', 'boolean', 'test flag'),
          ('CHANNEL_TIER_ENABLED', 'false', 'boolean', 'test flag'),
          ('V35_FACTORY_INVENTORY_ENABLED', 'false', 'boolean', 'test flag'),
          ('LEGACY_REFERRAL_V32_ENABLED', 'disabled', 'string', 'test flag'),
          ('pricing_config_epoch', '0', 'integer', 'test epoch')
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """
    )


@pytest.fixture(scope="session", autouse=True)
def isolated_pricing_schema():
    conn = psycopg2.connect(SCOPED_DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    # [R5 ⑤ 2026-08-20] brands 走生产 SSOT 出口（手搓版 3 列 vs 生产 32 列）。
    #   🔴 本目录的 DSN 安全栓(EXACT_THROWAWAY_URL / 每进程私有 schema)**一个字没动**:
    #      改的是 brands 这张表的 DDL 来源,不是连哪个库。
    #   顺带丢掉手搓版的 `owner_user_id NOT NULL REFERENCES users(id)` —— 生产实查
    #   brands.owner_user_id 是 integer / nullable / 无 FK,手搓版比生产严。
    ensure_brands_schema(cur)
    cur.execute(BASE_DDL)
    cur.execute(migration_sql(DUAL_MIGRATION))
    seed_business_rows(cur)
    cur.execute(migration_sql(RETAIL_SKU_MIGRATION))
    # The migration contract is explicitly idempotent; fresh setup executes it
    # twice so a non-idempotent future edit cannot hide behind a one-shot test.
    cur.execute(migration_sql(RETAIL_SKU_MIGRATION))
    cur.execute(migration_sql(WIRING_MIGRATION))
    cur.execute(migration_sql(NOTIFICATION_MIGRATION))
    cur.execute(migration_sql(NOTIFICATION_MIGRATION))
    cur.close()
    conn.close()
    yield

    # Close application pool before dropping its private namespace.
    try:
        from db.connection import close_pool

        close_pool()
    except Exception:
        pass
    with psycopg2.connect(EXACT_THROWAWAY_URL) as cleanup_conn:
        cleanup_conn.autocommit = True
        with cleanup_conn.cursor() as cleanup_cur:
            cleanup_cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')


@pytest.fixture(autouse=True)
def clean_business_state(isolated_pricing_schema):
    """Reset only the private schema between acceptance cases."""

    conn = psycopg2.connect(SCOPED_DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """
        TRUNCATE TABLE
          notification_outbox, user_notifications,
          channel_revenue_ledger, channel_pricing_relationships,
          agent_revenue_ledger, agent_rebate_config, agent_tax_profiles,
          recharge_orders, price_quotes, pricing_catalog_entries,
          pricing_catalog_versions, public_account_codes,
          feature_consumption_versions, agent_inventory_transactions,
          agent_inventory_wallets, point_transactions,
          customer_credit_transactions, customer_agent_credit_wallets,
          customer_agent_bindings,
          agent_pricing_overrides,
          agent_sku_overrides, referral_links, invite_codes, referral_codes,
          purchase_agreement_acceptances,
          brands, user_roles, roles
        RESTART IDENTITY CASCADE
        """
    )
    cur.execute("DELETE FROM user_wallets")
    cur.execute("DELETE FROM users")
    cur.execute("DELETE FROM sku_templates")
    cur.execute(
        """DELETE FROM system_settings
           WHERE key IN (
             'PRICING_DUAL_SSOT_ENABLED', 'CHANNEL_PRICING_ENABLED',
             'PRICING_QUOTE_REQUIRED', 'CHANNEL_TIER_ENABLED',
             'V35_FACTORY_INVENTORY_ENABLED', 'LEGACY_REFERRAL_V32_ENABLED',
             'pricing_config_epoch'
           )"""
    )
    seed_business_rows(cur)
    cur.close()
    conn.close()

    from config import pricing_ssot_flags

    pricing_ssot_flags.invalidate()
    yield
    pricing_ssot_flags.invalidate()


@pytest.fixture
def raw_conn():
    conn = psycopg2.connect(SCOPED_DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
