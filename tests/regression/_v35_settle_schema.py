"""[v7 finding1 · P0] V3.5 结算链【生产真实 schema】provisioner —— 供争议裁决结算 / 主路径结算的行为测试。

铁律(老板 NO-GO):资金测试必须用【生产真实 schema(NOT NULL + CHECK)】· 简化桩表会 false-green 漏检
  "缺列生产必崩 / 分轨丢失 / factory 重算" 这类 P0。本文件把 canonical 结算 core 触及的全部表按 prod DDL 建齐:
    agent_inventory_wallets / agent_inventory_transactions(库存双流水)
    customer_agent_credit_wallets / customer_credit_transactions(客户额度分轨 · 7-source CHECK)
    agent_revenue_ledger(收益 ledger · 全列 NOT NULL + CHECK)
    agent_tax_profiles(税 · 空表回落默认)
    recharge_orders(base + 结算快照列)
DDL 取自 scripts/migration_v35_factory_inventory_2026_05_26.sql 与生产 agent_revenue_ledger。
"""
from __future__ import annotations

# 库存双流水
_AGENT_INVENTORY_DDL = """
CREATE TABLE IF NOT EXISTS agent_inventory_wallets (
    agent_user_id INTEGER PRIMARY KEY,
    paid_inventory_points BIGINT NOT NULL DEFAULT 0,
    bonus_inventory_points BIGINT NOT NULL DEFAULT 0,
    frozen_inventory_points BIGINT NOT NULL DEFAULT 0,
    total_purchased_points BIGINT NOT NULL DEFAULT 0,
    total_allocated_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW(),
    CHECK (paid_inventory_points >= 0),
    CHECK (bonus_inventory_points >= 0),
    CHECK (frozen_inventory_points >= 0)
);
CREATE TABLE IF NOT EXISTS agent_inventory_transactions (
    id BIGSERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN (
        'purchase_prepay','purchase_auto','purchase_admin_adjust','allocate_to_customer',
        'allocate_to_customer_offline','revoke_from_customer','admin_adjust','refund_clawback')),
    pool TEXT NOT NULL CHECK (pool IN ('paid','bonus')),
    points BIGINT NOT NULL,
    balance_paid_after BIGINT NOT NULL,
    balance_bonus_after BIGINT NOT NULL,
    related_customer_user_id INTEGER,
    related_order_id TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# 客户额度分轨(含 publish_credit_points)+ 流水(prod 7-source CHECK)
_CUSTOMER_CREDIT_DDL = """
CREATE TABLE IF NOT EXISTS customer_agent_credit_wallets (
    customer_user_id INTEGER PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    tool_credit_points BIGINT NOT NULL DEFAULT 0,
    publish_credit_points BIGINT NOT NULL DEFAULT 0,
    bonus_credit_points BIGINT NOT NULL DEFAULT 0,
    total_purchased_points BIGINT NOT NULL DEFAULT 0,
    total_consumed_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT NOW(),
    CHECK (tool_credit_points >= 0),
    CHECK (publish_credit_points >= 0),
    CHECK (bonus_credit_points >= 0)
);
CREATE TABLE IF NOT EXISTS customer_credit_transactions (
    id BIGSERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL,
    agent_user_id INTEGER NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('allocate','consume','refund','revoke')),
    pool TEXT NOT NULL CHECK (pool IN ('tool','publish','bonus')),
    points BIGINT NOT NULL,
    balance_tool_after BIGINT NOT NULL,
    balance_publish_after BIGINT NOT NULL,
    balance_bonus_after BIGINT NOT NULL,
    feature_code TEXT,
    related_order_id TEXT,
    source TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

# customer_credit_transactions.source = prod 现值 7 值
_CUSTOMER_CREDIT_SOURCE_CHECK = """
ALTER TABLE customer_credit_transactions DROP CONSTRAINT IF EXISTS customer_credit_transactions_source_check;
ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_source_check
  CHECK (source = ANY (ARRAY['online_payment','offline_allocation','admin_adjust','tool_consume',
                             'refund_revoke','tool_fail_refund','agent_rebate']));
"""

# 生产 agent_revenue_ledger 真实 schema(全列 NOT NULL + CHECK)· DROP 后重建保证约束在场
_REAL_AGENT_REVENUE_LEDGER_DDL = """
DROP TABLE IF EXISTS agent_revenue_ledger;
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
    tax_mode TEXT NOT NULL DEFAULT 'withheld' CHECK (tax_mode IN ('withheld','invoice_provided','exempt_manual')),
    tax_withholding_cents INTEGER NOT NULL DEFAULT 0,
    invoice_status TEXT NOT NULL DEFAULT 'none' CHECK (invoice_status IN ('none','submitted','approved','rejected','refunded')),
    invoice_id INTEGER,
    agent_settlement_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'frozen' CHECK (status IN ('frozen','settled','cancelled')),
    frozen_at TIMESTAMP DEFAULT NOW(),
    settle_at TIMESTAMP NOT NULL,
    settled_at TIMESTAMP,
    settlement_request_id INTEGER,
    manual_review_required BOOLEAN DEFAULT FALSE,
    reversed_at TIMESTAMP,
    reversed_by_ledger_id BIGINT,
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

_AGENT_TAX_DDL = """
CREATE TABLE IF NOT EXISTS agent_tax_profiles (
    agent_user_id INTEGER PRIMARY KEY,
    entity_type TEXT,
    default_tax_rate_bps INTEGER,
    default_tax_mode TEXT,
    tax_id TEXT,
    invoice_capability TEXT,
    notes TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);
"""

# recharge_orders(base · 若 conftest 已建则仅补结算快照列)
_RECHARGE_ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS recharge_orders (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    base_points BIGINT NOT NULL DEFAULT 0,
    bonus_points BIGINT NOT NULL DEFAULT 0,
    payment_method TEXT,
    payment_status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW()
);
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS agent_user_id INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS factory_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS gateway_fee_bps INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS gateway_fee_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_service_fee_bps INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_service_fee_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS agent_margin_before_tax_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS tax_rate_bps INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS tax_withholding_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS agent_revenue_cents INTEGER;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_snapshot_jsonb JSONB;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_mode TEXT;
"""

_USER_WALLETS_DDL = """
CREATE TABLE IF NOT EXISTS user_wallets (
    user_id INTEGER PRIMARY KEY, paid_points INTEGER DEFAULT 0, bonus_points INTEGER DEFAULT 0,
    commission_points INTEGER DEFAULT 0, frozen_points INTEGER DEFAULT 0,
    total_recharged BIGINT DEFAULT 0, updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT, is_admin BOOLEAN DEFAULT FALSE);
"""

_ALL = (_USER_WALLETS_DDL, _AGENT_INVENTORY_DDL, _CUSTOMER_CREDIT_DDL, _CUSTOMER_CREDIT_SOURCE_CHECK,
        _REAL_AGENT_REVENUE_LEDGER_DDL, _AGENT_TAX_DDL, _RECHARGE_ORDERS_DDL)


def provision_v35_settlement_schema(cur) -> None:
    """在 throwaway 库建齐 V3.5 结算链全部真实 schema(幂等)· 传入的 cursor 需已在事务/autocommit。"""
    for ddl in _ALL:
        cur.execute(ddl)
    from db.dispute_escrow_db import init_dispute_escrow_tables
    init_dispute_escrow_tables(cur)


# 结算链会被写入 / 需清理的表(测试 teardown DELETE · 不 DROP)
SETTLE_TABLES = (
    "dispute_escrow", "user_wallets", "customer_agent_credit_wallets", "customer_credit_transactions",
    "agent_inventory_wallets", "agent_inventory_transactions", "agent_revenue_ledger", "recharge_orders",
)
