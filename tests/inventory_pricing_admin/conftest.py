"""服务商进货价目表后台化：throwaway PostgreSQL 最小真实 schema。"""

import json
import os
from pathlib import Path

import psycopg2
import pytest


DDL = """
DROP TABLE IF EXISTS audit_logs CASCADE;
DROP TABLE IF EXISTS agent_pricing_overrides CASCADE;
DROP TABLE IF EXISTS agent_channel_tier_state CASCADE;
DROP TABLE IF EXISTS agent_tier_change_log CASCADE;
DROP TABLE IF EXISTS bonus_grants CASCADE;
DROP TABLE IF EXISTS founder_seats CASCADE;
DROP TABLE IF EXISTS agent_inventory_transactions CASCADE;
DROP TABLE IF EXISTS agent_inventory_wallets CASCADE;
DROP TABLE IF EXISTS customer_credit_transactions CASCADE;
DROP TABLE IF EXISTS customer_agent_credit_wallets CASCADE;
DROP TABLE IF EXISTS test_inventory CASCADE;
DROP TABLE IF EXISTS refund_work_order_attachments CASCADE;
DROP TABLE IF EXISTS refund_work_orders CASCADE;
DROP TABLE IF EXISTS recharge_orders CASCADE;
DROP TABLE IF EXISTS user_notifications CASCADE;
DROP TABLE IF EXISTS user_wallets CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS system_settings CASCADE;
CREATE TABLE system_settings (
  key VARCHAR(100) PRIMARY KEY, value TEXT, value_type VARCHAR(20) DEFAULT 'string',
  description TEXT, updated_at TIMESTAMP DEFAULT NOW(), updated_by INTEGER
);
CREATE TABLE users (
  id INTEGER PRIMARY KEY, username TEXT, display_name TEXT,
  is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE user_wallets (user_id INTEGER PRIMARY KEY, agent_level INTEGER DEFAULT 0);
CREATE TABLE user_notifications (
  id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, type VARCHAR(30) NOT NULL,
  title VARCHAR(200), content TEXT, link VARCHAR(300), is_read BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY, user_id INTEGER, username TEXT, action TEXT NOT NULL,
  module TEXT, entity_type TEXT, entity_id INTEGER, summary TEXT,
  before_snapshot TEXT, after_snapshot TEXT, ip_address TEXT,
  request_id TEXT, reason TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE agent_pricing_overrides (
  agent_user_id INTEGER PRIMARY KEY, quote_markup_override NUMERIC(4,2),
  sku_markup_override NUMERIC(4,2), wholesale_numer INTEGER,
  wholesale_denom INTEGER, note TEXT, updated_at TIMESTAMP DEFAULT NOW(),
  updated_by INTEGER
);
CREATE TABLE recharge_orders (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
  base_points BIGINT NOT NULL DEFAULT 0, bonus_points BIGINT NOT NULL DEFAULT 0,
  payment_method TEXT, payment_status TEXT DEFAULT 'pending', payment_id TEXT,
  paid_at TIMESTAMP, created_at TIMESTAMP DEFAULT NOW(), order_type TEXT,
  refund_status TEXT, refunded_amount_cents INTEGER,
  refund_completed_at TIMESTAMP,
  pricing_snapshot_jsonb JSONB, pricing_catalog_version TEXT, settlement_mode TEXT,
  -- #169 · 与生产对齐(prod_schema_2026-09-05.sql:15254)。
  -- 🔴 chk 一起带上:少了它,夹具会接受生产**拒绝**的值,
  --    判据于是证明了一件生产上不成立的事。
  actual_payment_channel TEXT
    CONSTRAINT chk_recharge_actual_payment_channel CHECK (
      actual_payment_channel IS NULL OR actual_payment_channel IN
      ('wechat_jsapi','wechat_native','xunhupay','manual_bank','unknown')),
  -- #169 · 迁移 058 的三列。
  payment_url_mobile TEXT, payment_url_qrcode TEXT, code_url TEXT
);
-- ⚠️ 本夹具建的 recharge_orders 仍只有生产 42 列中的 21 列(实测,#169)。
--    缺的多是资金列(agent_revenue_cents / gateway_fee_cents / tax_withholding_cents
--    / settlement_snapshot_jsonb / idempotency_key …)。今天补这 4 列,是因为
--    有人第一次碰到它们 —— **不代表其余 21 列的缺口已经处理**。
--    在本包里"跑绿"不等于在生产上成立:夹具接不住的字段,判据也钉不住。
CREATE TABLE refund_work_orders (
  id BIGSERIAL PRIMARY KEY, source_order_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  requested_refund_cents INTEGER NOT NULL DEFAULT 0,
  payout_proof_url TEXT
);
CREATE TABLE refund_work_order_attachments (
  id BIGSERIAL PRIMARY KEY,
  work_order_id BIGINT NOT NULL REFERENCES refund_work_orders(id) ON DELETE CASCADE,
  evidence_type TEXT NOT NULL DEFAULT 'other'
);
CREATE TABLE agent_channel_tier_state (
  agent_user_id INTEGER PRIMARY KEY, channel_tier TEXT NOT NULL DEFAULT 'none',
  rolling_12m_yuan NUMERIC(14,2) NOT NULL DEFAULT 0,
  is_founder BOOLEAN NOT NULL DEFAULT FALSE, founder_rank INTEGER,
  first_order_done BOOLEAN NOT NULL DEFAULT FALSE, last_evaluated_at TIMESTAMP,
  tier_effective_at TIMESTAMP DEFAULT NOW(), created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(), tier_override TEXT, tier_override_until TIMESTAMP,
  tier_override_by INTEGER, tier_override_note TEXT
);
CREATE TABLE agent_tier_change_log (
  id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL, from_tier TEXT,
  to_tier TEXT NOT NULL, direction TEXT NOT NULL, rolling_12m_yuan NUMERIC(14,2) DEFAULT 0,
  trigger_source TEXT NOT NULL, related_order_id TEXT, idempotency_key TEXT UNIQUE NOT NULL,
  note TEXT, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE founder_seats (
  id INTEGER PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0, cap INTEGER NOT NULL DEFAULT 10,
  updated_at TIMESTAMP DEFAULT NOW()
);
INSERT INTO founder_seats(id,used,cap) VALUES (1,0,10);
CREATE TABLE agent_inventory_wallets (
  agent_user_id INTEGER PRIMARY KEY, paid_inventory_points INTEGER NOT NULL DEFAULT 0,
  bonus_inventory_points INTEGER NOT NULL DEFAULT 0, frozen_inventory_points INTEGER NOT NULL DEFAULT 0,
  total_purchased_points BIGINT NOT NULL DEFAULT 0, total_allocated_points BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE agent_inventory_transactions (
  id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER NOT NULL, type TEXT NOT NULL, pool TEXT NOT NULL,
  points INTEGER NOT NULL, balance_paid_after INTEGER NOT NULL, balance_bonus_after INTEGER NOT NULL,
  related_customer_user_id INTEGER, related_order_id TEXT, description TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE customer_agent_credit_wallets (
  customer_user_id INTEGER PRIMARY KEY, agent_user_id INTEGER NOT NULL,
  tool_credit_points INTEGER NOT NULL DEFAULT 0, publish_credit_points INTEGER NOT NULL DEFAULT 0,
  bonus_credit_points INTEGER NOT NULL DEFAULT 0, total_purchased_points BIGINT NOT NULL DEFAULT 0,
  total_consumed_points BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE customer_credit_transactions (
  id BIGSERIAL PRIMARY KEY, customer_user_id INTEGER NOT NULL, agent_user_id INTEGER NOT NULL,
  type TEXT NOT NULL, pool TEXT NOT NULL, points INTEGER NOT NULL,
  balance_tool_after INTEGER NOT NULL, balance_publish_after INTEGER NOT NULL,
  balance_bonus_after INTEGER NOT NULL, feature_code TEXT, related_order_id TEXT,
  source TEXT, description TEXT, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE bonus_grants (
  id BIGSERIAL PRIMARY KEY, grant_key TEXT UNIQUE NOT NULL, owner_type TEXT NOT NULL,
  owner_id INTEGER NOT NULL, pool TEXT NOT NULL DEFAULT 'bonus', granted_points BIGINT NOT NULL,
  consumed_points BIGINT NOT NULL DEFAULT 0, frozen_points BIGINT NOT NULL DEFAULT 0,
  grant_type TEXT NOT NULL, tier_at_grant TEXT, bonus_rate_used NUMERIC(5,4),
  related_order_id TEXT, parent_grant_id BIGINT, status TEXT NOT NULL DEFAULT 'active',
  granted_at TIMESTAMP DEFAULT NOW(), expires_at TIMESTAMP NOT NULL, frozen_at TIMESTAMP,
  renewed_at TIMESTAMP, note TEXT, created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE test_inventory (
  agent_user_id INTEGER PRIMARY KEY, paid_points BIGINT NOT NULL DEFAULT 0,
  bonus_points BIGINT NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0
);
"""


@pytest.fixture(scope="session", autouse=True)
def inventory_pricing_schema():
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg2.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            migration = (Path(__file__).resolve().parents[2] / "scripts" / "migration_agent_inventory_bigint_cutover_2026_07_14.sql").read_text(encoding="utf-8")
            cur.execute(migration)
            cur.execute(migration)
            notification_migration = (
                Path(__file__).resolve().parents[2]
                / "scripts"
                / "migration_notification_outbox_2026_07_17.sql"
            ).read_text(encoding="utf-8")
            cur.execute(notification_migration)
            cur.execute(notification_migration)
    yield


@pytest.fixture(autouse=True)
def reset_inventory_pricing_db(inventory_pricing_schema):
    defaults = {
        "agent_purchase_catalog_version": "agent-purchase-v1",
        "agent_purchase_options": [
            {"option_id": "apo_seed1000", "amount_cents": 100000, "is_enabled": True, "sort_order": 10, "reward_eligible": True},
            {"option_id": "apo_seed5000", "amount_cents": 500000, "is_enabled": True, "sort_order": 20, "reward_eligible": True},
        ],
        "wholesale_numer": 225,
        "wholesale_denom": 325,
        "agent_purchase_bonus_rate": 0.05,
    }
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg2.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE recharge_orders DROP CONSTRAINT IF EXISTS "
                "check_agent_inventory_writer_generation_required"
            )
            cur.execute(
                "SELECT setval('agent_inventory_writer_generation_fence_seq', 1, TRUE)"
            )
            for table in ("audit_logs", "test_inventory", "refund_work_order_attachments",
                          "refund_work_orders", "bonus_grants", "agent_inventory_transactions",
                          "agent_inventory_wallets", "customer_credit_transactions", "customer_agent_credit_wallets",
                          "agent_tier_change_log", "agent_channel_tier_state",
                          "agent_pricing_overrides", "recharge_orders", "user_wallets", "users", "system_settings"):
                cur.execute(f"DELETE FROM {table}")
            cur.execute("UPDATE founder_seats SET used=0,cap=10")
            cur.execute("INSERT INTO users(id,username) VALUES (7,'agent7'),(99,'admin')")
            cur.execute("INSERT INTO user_wallets(user_id,agent_level) VALUES (7,1),(99,0)")
            cur.execute(
                "INSERT INTO system_settings(key,value,value_type) VALUES "
                "('pricing_config',%s,'json'),('pricing_config_epoch','1','integer'),"
                "('CHANNEL_TIER_ENABLED','false','boolean')",
                (json.dumps(defaults),),
            )
    from config.pricing_config import invalidate_pricing_config_cache
    from services.config_epoch import accept_committed_epoch
    invalidate_pricing_config_cache()
    accept_committed_epoch(1)
    yield
    invalidate_pricing_config_cache()
