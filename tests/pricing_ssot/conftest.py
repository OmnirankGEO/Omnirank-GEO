"""tests/pricing_ssot — 双价目表 SSOT 测试包 conftest。

依赖根 conftest 已把 DATABASE_URL 切到 TEST_DATABASE_URL(含 'test')。
session 级建最小 base 表 + 跑本批 migration;每测试清本批表。
"""
import os
import re
from pathlib import Path

import psycopg2
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MIG = _ROOT / "scripts" / "migration_pricing_dual_ssot_2026_07_12.sql"
_WIRING_MIG = _ROOT / "scripts" / "migration_pricing_quote_wiring_2026_07_14.sql"

# 本批 migration 会 ALTER 的既有表(测试库最小重建 · throwaway 库先 DROP 保证 schema 新鲜)
_BASE_DDL = """
DROP TABLE IF EXISTS recharge_orders CASCADE;
DROP TABLE IF EXISTS point_transactions CASCADE;
DROP TABLE IF EXISTS customer_credit_transactions CASCADE;
DROP TABLE IF EXISTS agent_pricing_overrides CASCADE;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT;
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT,
    is_system INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);
CREATE TABLE IF NOT EXISTS user_wallets (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    agent_level INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS system_settings (key VARCHAR(100) PRIMARY KEY, value TEXT, value_type VARCHAR(20) DEFAULT 'string', description TEXT, updated_at TIMESTAMP DEFAULT NOW(), updated_by INTEGER);
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
CREATE TABLE IF NOT EXISTS recharge_orders (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL, base_points BIGINT NOT NULL DEFAULT 0, bonus_points BIGINT NOT NULL DEFAULT 0, payment_method TEXT, payment_status TEXT DEFAULT 'pending', payment_id TEXT, created_at TIMESTAMP DEFAULT NOW(), paid_at TIMESTAMP, pricing_snapshot_jsonb JSONB, order_type TEXT, factory_cents INTEGER, agent_user_id INTEGER, sku_template_id INTEGER, override_id INTEGER, binding_source TEXT, source_token TEXT, commission_version TEXT, refund_status TEXT, refund_requested_at TIMESTAMP, refund_completed_at TIMESTAMP, refunded_amount_cents INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS point_transactions (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, type TEXT NOT NULL, point_type TEXT NOT NULL, amount BIGINT NOT NULL, balance_after BIGINT NOT NULL, feature_code TEXT, description TEXT, order_id TEXT, created_at TIMESTAMP DEFAULT NOW());
CREATE TABLE IF NOT EXISTS customer_credit_transactions (id BIGSERIAL PRIMARY KEY, customer_user_id INTEGER NOT NULL, agent_user_id INTEGER NOT NULL, type TEXT NOT NULL, pool TEXT NOT NULL, points INTEGER NOT NULL, balance_tool_after INTEGER NOT NULL DEFAULT 0, balance_publish_after INTEGER NOT NULL DEFAULT 0, balance_bonus_after INTEGER NOT NULL DEFAULT 0, feature_code TEXT, related_order_id TEXT, source TEXT, description TEXT, created_at TIMESTAMP DEFAULT NOW());
CREATE TABLE IF NOT EXISTS sku_templates (id SERIAL PRIMARY KEY, template_code TEXT UNIQUE NOT NULL, sku_type TEXT, default_name TEXT, points_granted INTEGER, wholesale_cents INTEGER, suggested_retail_cents INTEGER, is_active BOOLEAN DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS feature_pricing (feature_code TEXT PRIMARY KEY, feature_name TEXT, cost_points INTEGER NOT NULL, cost_compute NUMERIC(6,2) DEFAULT 0, requires_paid_points BOOLEAN DEFAULT FALSE, is_active BOOLEAN DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS customer_agent_bindings (
    id SERIAL PRIMARY KEY,
    customer_user_id INTEGER UNIQUE NOT NULL,
    agent_user_id INTEGER NOT NULL,
    binding_source TEXT,
    source_token TEXT,
    bound_at TIMESTAMP DEFAULT NOW(),
    dispute_status TEXT,
    dispute_note TEXT,
    admin_override_user_id INTEGER,
    admin_override_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS refund_work_orders (
    id BIGSERIAL PRIMARY KEY, source_order_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS refund_work_order_attachments (
    id BIGSERIAL PRIMARY KEY,
    work_order_id BIGINT NOT NULL REFERENCES refund_work_orders(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL DEFAULT 'other', uploaded_at TIMESTAMP DEFAULT NOW()
);
"""

_MY_TABLES = [
    "refund_work_order_attachments", "refund_work_orders",
    "channel_revenue_ledger", "channel_pricing_relationships", "price_quotes",
    "pricing_catalog_entries", "pricing_catalog_versions", "feature_consumption_versions",
    "public_account_codes",
]


def _strip_like_server(sql: str) -> str:
    sql = re.sub(r'^\s*\\[a-zA-Z_]+.*$', '', sql, flags=re.MULTILINE)
    sql = re.sub(r'^\s*BEGIN\s*;\s*$', '', sql, flags=re.MULTILINE | re.IGNORECASE)
    sql = re.sub(r'^\s*COMMIT\s*;\s*$', '', sql, flags=re.MULTILINE | re.IGNORECASE)
    return sql


@pytest.fixture(scope="session", autouse=True)
def _setup_schema():
    url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    # DROP 本批新表(throwaway 库)保证 migration 的最新约束(CHECK/索引)真生效
    for t in _MY_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    cur.execute(_BASE_DDL)
    # [v8 reconciliation] geofix throwaway 库可能已有一个【无 value_type】的 system_settings(GEO init 建的)·
    #   上面的 CREATE TABLE IF NOT EXISTS 对它是 no-op → migration 写 value_type 会 UndefinedColumn。补 ALTER 对齐。
    cur.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS value_type VARCHAR(20) DEFAULT 'string'")
    cur.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS updated_by INTEGER")
    cur.execute(_strip_like_server(_MIG.read_text(encoding="utf-8")))
    cur.execute(_strip_like_server(_WIRING_MIG.read_text(encoding="utf-8")))
    cur.execute(
        """INSERT INTO users(id,is_active) VALUES
             (1,1),(2,1),(3,1),(4,1),(5,1),(6,1),(7,1),(8,1),(9,1),(10,1),
             (11,1),(12,1),(20,1),(30,1),(999,1),(12345,1)
           ON CONFLICT(id) DO NOTHING"""
    )
    cur.execute(
        """INSERT INTO user_wallets(user_id,agent_level) VALUES
             (10,1),(20,1),(30,1)
           ON CONFLICT(user_id) DO UPDATE SET agent_level=EXCLUDED.agent_level"""
    )
    # seed feature_pricing for usage_examples
    cur.execute("""INSERT INTO feature_pricing(feature_code,feature_name,cost_points)
                   VALUES ('article_gen','文章生成',390),('monitor_single','监测单次',130),('geo_diagnosis','GEO诊断',650)
                   ON CONFLICT (feature_code) DO NOTHING""")
    cur.execute("""INSERT INTO sku_templates(template_code,sku_type,default_name,points_granted,wholesale_cents)
                   VALUES ('credit_basic','credit_pack','入门算力包',195000,120000)
                   ON CONFLICT (template_code) DO NOTHING""")
    conn.close()
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    for t in _MY_TABLES:
        cur.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    cur.execute("TRUNCATE agent_pricing_overrides")
    cur.execute("DELETE FROM recharge_orders")
    cur.execute("DELETE FROM user_roles")
    cur.execute(
        """INSERT INTO user_wallets(user_id,agent_level) VALUES
             (10,1),(20,1),(30,1)
           ON CONFLICT(user_id) DO UPDATE SET agent_level=EXCLUDED.agent_level"""
    )
    cur.execute(
        """INSERT INTO public_account_codes(user_id,channel_account_code) VALUES
             (8,'CH-ABCDEFGH'),(20,'CH-JKLMNPQR'),(30,'CH-STUVWXYZ')
           ON CONFLICT(user_id) DO UPDATE
             SET channel_account_code=EXCLUDED.channel_account_code"""
    )
    # 每测试重置三总闸为 false(防其他脚本遗留 'true' 污染)+ 失效进程缓存
    cur.execute("""UPDATE system_settings SET value='false'
                   WHERE key IN ('PRICING_DUAL_SSOT_ENABLED','CHANNEL_PRICING_ENABLED','PRICING_QUOTE_REQUIRED')""")
    conn.close()
    try:
        from config import pricing_ssot_flags
        pricing_ssot_flags.invalidate()
        from services import config_epoch
        config_epoch.read_config_epoch(force=True)
    except Exception:
        pass
    yield
