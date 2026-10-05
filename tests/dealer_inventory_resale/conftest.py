"""Exact throwaway PostgreSQL fixture for dealer-resale fund tests."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor


DEFAULT_TEST_URL = "postgresql://dealer_test:dealer_test_pw@127.0.0.1:55545/dealer_resale_test"
TEST_URL = os.environ.get("DEALER_RESALE_TEST_DATABASE_URL", DEFAULT_TEST_URL).split("?", 1)[0]
configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
parsed = urlparse(TEST_URL)
if (
    configured != TEST_URL
    or parsed.scheme not in {"postgres", "postgresql"}
    or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
    or parsed.path.lstrip("/") != "dealer_resale_test"
    or int(parsed.port or 0) < 55000
    or os.environ.get("ALLOW_DESTRUCTIVE_TEST_DB") != "1"
):
    raise RuntimeError(
        "dealer_inventory_resale tests require an explicit destructive-test opt-in and a "
        "dedicated local high-port dealer_resale_test PostgreSQL; "
        f"selected={TEST_URL!r}, TEST_DATABASE_URL={configured!r}"
    )
os.environ["DATABASE_URL"] = TEST_URL


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "scripts" / "migration_dealer_inventory_resale_2026_07_15.sql"
JIT_MIGRATION = ROOT / "scripts" / "migration_dealer_jit_resale_2026_07_17.sql"
REFUND_AGREEMENT_MIGRATION = ROOT / "scripts" / "migration_direct_service_refund_agreements_2026_07_15.sql"
NOTIFICATION_MIGRATION = ROOT / "scripts" / "migration_notification_outbox_2026_07_17.sql"
MINTING_MIGRATION = ROOT / "scripts" / "migration_ondemand_minting_2026_07_29.sql"
ROLLBACK = ROOT / "scripts" / "rollback_dealer_inventory_resale_2026_07_15.sql"
JIT_ROLLBACK = ROOT / "scripts" / "rollback_dealer_jit_resale_2026_07_17.sql"


BASELINE_SQL = r"""
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT,
  is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE roles (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE user_roles (
  user_id INTEGER NOT NULL REFERENCES users(id),
  role_id INTEGER NOT NULL REFERENCES roles(id),
  PRIMARY KEY(user_id, role_id)
);
INSERT INTO roles(id,name) VALUES (1,'admin');
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
CREATE TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER,
  username TEXT,
  action TEXT NOT NULL,
  module TEXT,
  entity_type TEXT,
  entity_id INTEGER,
  summary TEXT,
  before_snapshot TEXT,
  after_snapshot TEXT,
  ip_address TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE user_wallets (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  agent_level INTEGER NOT NULL DEFAULT 1,
  paid_points BIGINT NOT NULL DEFAULT 0,
  bonus_points BIGINT NOT NULL DEFAULT 0,
  total_recharged BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE system_settings (
  key TEXT PRIMARY KEY,
  value TEXT,
  value_type TEXT,
  description TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
CREATE TABLE pricing_catalog_versions (
  id BIGSERIAL PRIMARY KEY,
  catalog_type TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  version_code TEXT NOT NULL,
  status TEXT NOT NULL,
  effective_to TIMESTAMPTZ,
  published_at TIMESTAMPTZ,
  calc_meta_jsonb JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE pricing_catalog_entries (
  id BIGSERIAL PRIMARY KEY,
  version_id BIGINT NOT NULL REFERENCES pricing_catalog_versions(id),
  product_code TEXT NOT NULL,
  base_price_cents INTEGER NOT NULL,
  multiplier_bps INTEGER NOT NULL DEFAULT 10000,
  final_price_cents INTEGER NOT NULL,
  paid_points BIGINT NOT NULL,
  bonus_points BIGINT NOT NULL DEFAULT 0,
  source_ref_jsonb JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE price_quotes (
  quote_id TEXT PRIMARY KEY,
  quote_type TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  catalog_version TEXT NOT NULL,
  catalog_version_id BIGINT,
  channel_relationship_version TEXT,
  seller_policy_version TEXT,
  product_code TEXT NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 1,
  seller_legal_entity_id TEXT NOT NULL DEFAULT 'PLATFORM',
  buyer_user_id INTEGER NOT NULL,
  channel_account_code TEXT,
  channel_beneficiary_user_id INTEGER,
  payment_collector TEXT NOT NULL DEFAULT 'PLATFORM',
  points_granted BIGINT NOT NULL,
  bonus_points BIGINT NOT NULL DEFAULT 0,
  base_price_cents INTEGER NOT NULL,
  effective_multiplier_bps INTEGER NOT NULL DEFAULT 10000,
  final_price_cents INTEGER NOT NULL,
  upstream_cost_basis_cents INTEGER,
  expires_at TIMESTAMPTZ NOT NULL,
  calculation_hash TEXT,
  status TEXT NOT NULL DEFAULT 'issued',
  idempotency_key TEXT,
  used_order_id TEXT,
  pricing_snapshot_jsonb JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  consumed_at TIMESTAMPTZ
);
CREATE TABLE recharge_orders (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  amount_cents INTEGER NOT NULL,
  base_points BIGINT NOT NULL,
  bonus_points BIGINT NOT NULL DEFAULT 0,
  payment_method TEXT,
  payment_status TEXT NOT NULL DEFAULT 'pending',
  payment_id TEXT,
  paid_at TIMESTAMPTZ,
  order_type TEXT,
  agent_user_id INTEGER,
  pricing_snapshot_jsonb JSONB,
  settlement_snapshot_jsonb JSONB,
  pricing_catalog_version TEXT,
  price_quote_id TEXT,
  idempotency_key TEXT,
  agent_inventory_writer_generation INTEGER,
  settlement_mode TEXT,
  refund_status TEXT,
  refund_requested_at TIMESTAMPTZ,
  refund_completed_at TIMESTAMPTZ,
  refunded_amount_cents INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE point_transactions (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
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
CREATE TABLE agent_inventory_wallets (
  agent_user_id INTEGER PRIMARY KEY REFERENCES users(id),
  paid_inventory_points BIGINT NOT NULL DEFAULT 0,
  bonus_inventory_points BIGINT NOT NULL DEFAULT 0,
  frozen_inventory_points BIGINT NOT NULL DEFAULT 0,
  total_purchased_points BIGINT NOT NULL DEFAULT 0,
  total_allocated_points BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (paid_inventory_points >= 0),
  CHECK (bonus_inventory_points >= 0),
  CHECK (frozen_inventory_points >= 0)
);
CREATE TABLE agent_inventory_transactions (
  id BIGSERIAL PRIMARY KEY,
  agent_user_id INTEGER NOT NULL REFERENCES users(id),
  type TEXT NOT NULL,
  pool TEXT NOT NULL,
  points BIGINT NOT NULL,
  balance_paid_after BIGINT NOT NULL,
  balance_bonus_after BIGINT NOT NULL,
  related_customer_user_id INTEGER,
  related_order_id TEXT,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT agent_inventory_transactions_type_check CHECK (type IN (
    'purchase_prepay', 'purchase_auto', 'purchase_admin_adjust',
    'purchase_from_commission', 'allocate_to_customer',
    'allocate_to_customer_offline', 'revoke_from_customer',
    'admin_adjust', 'refund_clawback'
  ))
);
CREATE TABLE bonus_grants (
  id BIGSERIAL PRIMARY KEY,
  grant_key TEXT UNIQUE NOT NULL,
  owner_type TEXT NOT NULL,
  owner_id INTEGER NOT NULL,
  pool TEXT NOT NULL DEFAULT 'bonus',
  granted_points BIGINT NOT NULL,
  consumed_points BIGINT NOT NULL DEFAULT 0,
  frozen_points BIGINT NOT NULL DEFAULT 0,
  grant_type TEXT NOT NULL,
  tier_at_grant TEXT,
  bonus_rate_used NUMERIC(5,4),
  related_order_id TEXT,
  parent_grant_id BIGINT,
  status TEXT NOT NULL DEFAULT 'active',
  granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  frozen_at TIMESTAMPTZ,
  renewed_at TIMESTAMPTZ,
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE customer_agent_credit_wallets (
  customer_user_id INTEGER PRIMARY KEY REFERENCES users(id),
  agent_user_id INTEGER NOT NULL REFERENCES users(id),
  tool_credit_points BIGINT NOT NULL DEFAULT 0,
  publish_credit_points BIGINT NOT NULL DEFAULT 0,
  bonus_credit_points BIGINT NOT NULL DEFAULT 0,
  total_purchased_points BIGINT NOT NULL DEFAULT 0,
  total_consumed_points BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE customer_credit_transactions (
  id BIGSERIAL PRIMARY KEY,
  customer_user_id INTEGER NOT NULL REFERENCES users(id),
  agent_user_id INTEGER NOT NULL REFERENCES users(id),
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
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT customer_credit_transactions_source_check CHECK (source IN (
    'online_payment', 'offline_allocation', 'admin_adjust', 'tool_consume',
    'refund_revoke', 'agent_rebate', 'tool_fail_refund',
    'diagnosis_delivery_refund'
  ))
);
CREATE TABLE agent_revenue_ledger (
  id BIGSERIAL PRIMARY KEY,
  agent_user_id INTEGER NOT NULL REFERENCES users(id),
  source TEXT NOT NULL,
  recharge_order_id TEXT,
  customer_user_id INTEGER,
  customer_paid_cents INTEGER NOT NULL,
  factory_cents INTEGER NOT NULL,
  gateway_fee_bps INTEGER NOT NULL DEFAULT 0,
  gateway_fee_cents INTEGER NOT NULL DEFAULT 0,
  settlement_service_fee_bps INTEGER NOT NULL DEFAULT 0,
  settlement_service_fee_cents INTEGER NOT NULL DEFAULT 0,
  agent_margin_before_tax_cents INTEGER NOT NULL,
  tax_rate_bps INTEGER NOT NULL DEFAULT 0,
  tax_mode TEXT NOT NULL DEFAULT 'withheld',
  tax_withholding_cents INTEGER NOT NULL DEFAULT 0,
  agent_settlement_cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'frozen',
  frozen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  settle_at TIMESTAMPTZ NOT NULL,
  settled_at TIMESTAMPTZ,
  manual_review_required BOOLEAN NOT NULL DEFAULT FALSE,
  reversed_at TIMESTAMPTZ,
  reversed_by_ledger_id BIGINT,
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE public_account_codes (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  service_account_code TEXT UNIQUE,
  channel_account_code TEXT UNIQUE
);
CREATE TABLE channel_pricing_relationships (
  id BIGSERIAL PRIMARY KEY,
  buyer_dealer_id INTEGER NOT NULL REFERENCES users(id),
  upstream_channel_account_id INTEGER NOT NULL REFERENCES users(id),
  relationship_version TEXT NOT NULL,
  cost_multiplier_bps INTEGER NOT NULL DEFAULT 10000,
  status TEXT NOT NULL DEFAULT 'active',
  effective_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  effective_to TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX ux_test_active_direct_seller
  ON channel_pricing_relationships(buyer_dealer_id)
  WHERE status='active' AND effective_to IS NULL;
CREATE TABLE channel_revenue_ledger (
  id BIGSERIAL PRIMARY KEY,
  recharge_order_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'recorded',
  reversed_at TIMESTAMPTZ
);
CREATE TABLE user_social_subscriptions (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  plan_id TEXT NOT NULL DEFAULT 'personal',
  channel TEXT NOT NULL DEFAULT 'wechat',
  billing_cycle TEXT NOT NULL DEFAULT 'monthly',
  order_id TEXT UNIQUE,
  started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP + INTERVAL '30 days',
  auto_renew BOOLEAN NOT NULL DEFAULT TRUE,
  cancelled_at TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE user_social_entitlements (
  id BIGSERIAL PRIMARY KEY,
  subscription_id BIGINT NOT NULL REFERENCES user_social_subscriptions(id),
  light_chat_used INTEGER NOT NULL DEFAULT 0,
  pro_write_used INTEGER NOT NULL DEFAULT 0,
  super_write_used INTEGER NOT NULL DEFAULT 0,
  web_search_used INTEGER NOT NULL DEFAULT 0,
  video_minutes_used INTEGER NOT NULL DEFAULT 0,
  rewrite_used INTEGER NOT NULL DEFAULT 0,
  video_breakdown_used INTEGER NOT NULL DEFAULT 0,
  author_breakdown_used INTEGER NOT NULL DEFAULT 0,
  review_used INTEGER NOT NULL DEFAULT 0,
  monthly_plan_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE agreement_signatures (
  id BIGSERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  agreement_type TEXT NOT NULL,
  agreement_version TEXT NOT NULL,
  signed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ip_address TEXT,
  user_agent TEXT,
  UNIQUE(user_id, agreement_type, agreement_version)
);
CREATE TABLE agent_factory_agreements (
  id BIGSERIAL PRIMARY KEY,
  agent_user_id INTEGER NOT NULL REFERENCES users(id),
  version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'signed',
  content_hash TEXT,
  signed_at TIMESTAMPTZ,
  UNIQUE(agent_user_id, version)
);
"""


def connect():
    return psycopg2.connect(TEST_URL, cursor_factory=RealDictCursor)


@pytest.fixture(scope="session", autouse=True)
def fresh_schema():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT pg_try_advisory_lock(hashtext('dealer-resale-pytest-session')) AS locked"
    )
    if not bool(cur.fetchone()["locked"]):
        cur.close()
        conn.close()
        raise RuntimeError(
            "dealer-resale throwaway PostgreSQL is already owned by another pytest session"
        )
    try:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        cur.execute(BASELINE_SQL)
        migration_sql = MIGRATION.read_text(encoding="utf-8")
        cur.execute(migration_sql)
        cur.execute(migration_sql)  # required idempotency pass 2
        refund_agreement_sql = REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8")
        cur.execute(refund_agreement_sql)
        cur.execute(refund_agreement_sql)  # required idempotency pass 2
        jit_migration_sql = JIT_MIGRATION.read_text(encoding="utf-8")
        cur.execute(jit_migration_sql)
        cur.execute(jit_migration_sql)  # required idempotency pass 2
        notification_sql = NOTIFICATION_MIGRATION.read_text(encoding="utf-8")
        cur.execute(notification_sql)
        cur.execute(notification_sql)  # required idempotency pass 2
        minting_sql = MINTING_MIGRATION.read_text(encoding="utf-8")
        cur.execute(minting_sql)
        cur.execute(minting_sql)  # required idempotency pass 2
        cur.execute(
            """INSERT INTO pricing_catalog_versions
               (catalog_type,scope_key,version_code,status,published_at,calc_meta_jsonb)
               VALUES (
                 'procurement','PLATFORM_BASE','proc-test-v1','published',NOW(),
                 '{"pricing_config_snapshot":{"wholesale_numer":50,"wholesale_denom":100,
                    "agent_purchase_bonus_rate":0,"bonus_validity_months":12,
                    "founding":{"cap":10,"min_first_order_yuan":500,
                    "first_order_extra_bonus":0},"agent_tier_config":{}}}'::jsonb
               )"""
        )
        cur.execute(
            """INSERT INTO pricing_catalog_entries
               (version_id,product_code,base_price_cents,final_price_cents,paid_points,
                bonus_points,source_ref_jsonb)
               SELECT id,'WATER',50,50,100,0,
                 '{"kind":"agent_purchase_option","option_id":"WATER","amount_cents":50,
                    "option":{"option_id":"WATER","amount_cents":50,
                    "reward_eligible":false}}'::jsonb
               FROM pricing_catalog_versions"""
        )
        yield
    finally:
        cur.execute("SELECT pg_advisory_unlock(hashtext('dealer-resale-pytest-session'))")
        cur.close()
        conn.close()


@pytest.fixture(autouse=True)
def reset_business_rows():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """TRUNCATE notification_outbox, user_notifications,
                  external_refund_proof_registry, subscription_refund_cases, user_social_entitlements, service_refund_cash_jobs, service_refund_funding_work_orders,
                  service_refund_liability_ledger, service_refund_reserve_accounts,
                  consumer_refund_lot_restorations, purchase_agreement_acceptances,
                  agreement_signatures, agent_factory_agreements, consumer_refund_cases, dealer_consumer_transfer_entries,
                  dealer_consumer_lot_allocations, dealer_consumer_sales,
                  dealer_consumer_jit_refund_lots, dealer_resale_hop_profit_ledger, dealer_resale_hop_transfer_entries,
                  dealer_resale_fulfillment_allocations, dealer_resale_fulfillment_hops,
                  dealer_resale_fulfillment_plans,
                  dealer_resale_refunds, dealer_resale_profit_ledger,
                  dealer_inventory_transfer_entries, dealer_inventory_transfers,
                  dealer_inventory_lot_allocations, dealer_resale_orders,
                  dealer_inventory_lots, dealer_resale_promotions, dealer_resale_policies,
                  agent_revenue_ledger, customer_credit_transactions,
                  customer_agent_credit_wallets, point_transactions,
                  bonus_grants, agent_inventory_transactions, agent_inventory_wallets,
                  channel_revenue_ledger, channel_pricing_relationships, public_account_codes,
                  recharge_orders, price_quotes, agent_pricing_overrides,
                  audit_logs, user_wallets, users
           RESTART IDENTITY CASCADE"""
    )
    cur.execute(
        """INSERT INTO dealer_resale_global_settings
           (singleton_id,platform_seller_user_id,default_downstream_markup_bps,
            settings_version,row_version,updated_by,updated_at)
           VALUES (1,NULL,10000,'dealer-resale-v1',1,NULL,NOW())
           ON CONFLICT(singleton_id) DO UPDATE SET
             platform_seller_user_id=NULL, default_downstream_markup_bps=10000,
             settings_version='dealer-resale-v1', row_version=1,
             updated_by=NULL, updated_at=NOW()"""
    )
    cur.execute(
        "UPDATE system_settings SET value='true' WHERE key='DEALER_INVENTORY_RESALE_ENABLED'"
    )
    cur.execute(
        """INSERT INTO system_settings(key,value,value_type)
           VALUES ('PRICING_DUAL_SSOT_ENABLED','false','boolean'),
                  ('PRICING_QUOTE_REQUIRED','false','boolean'),
                  ('CHANNEL_PRICING_ENABLED','false','boolean')
           ON CONFLICT(key) DO UPDATE SET value='false',value_type='boolean'"""
    )
    # 按需铸造护栏配置每个用例重置为空(缺 key = 用生产默认值);
    # 需要特定单位成本的用例由 _issue_manufacturer 按其发行单价现场写入。
    cur.execute("DELETE FROM system_settings WHERE key='inventory_minting_guard_config'")
    cur.close()
    conn.close()
    yield


@pytest.fixture
def db_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
