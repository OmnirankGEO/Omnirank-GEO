"""Isolated PostgreSQL fixtures for admin user governance tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg2
import pytest


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")


def _assert_safe_test_database() -> None:
    if not TEST_DATABASE_URL:
        raise RuntimeError("TEST_DATABASE_URL is required for admin user governance PostgreSQL tests")
    database_name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if "test" not in database_name:
        raise RuntimeError(f"unsafe TEST_DATABASE_URL database name: {database_name!r}")


BASE_SCHEMA = r"""
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT 'test',
    display_name TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    permission_version INTEGER DEFAULT 1,
    must_change_password INTEGER DEFAULT 0,
    phone TEXT,
    company TEXT,
    referred_by_agent_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    last_active_at TIMESTAMP
);
CREATE TABLE roles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT,
    is_system INTEGER DEFAULT 0
);
CREATE TABLE role_permissions (
    id SERIAL PRIMARY KEY,
    role_id INTEGER NOT NULL,
    module TEXT NOT NULL,
    level TEXT NOT NULL
);
CREATE TABLE user_roles (user_id INTEGER NOT NULL, role_id INTEGER NOT NULL, PRIMARY KEY(user_id, role_id));
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
CREATE TABLE user_clients (user_id INTEGER NOT NULL, brand_id INTEGER NOT NULL, PRIMARY KEY(user_id, brand_id));
CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE user_wallets (
    user_id INTEGER PRIMARY KEY,
    paid_points BIGINT NOT NULL DEFAULT 0,
    bonus_points BIGINT NOT NULL DEFAULT 0,
    total_recharged BIGINT NOT NULL DEFAULT 0,
    agent_level INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE system_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    value_type TEXT,
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE referral_links (
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    commission_rate NUMERIC(4,3) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(referrer_id, referred_id)
);
CREATE TABLE customer_agent_bindings (
    id SERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL UNIQUE,
    agent_user_id INTEGER NOT NULL,
    binding_source TEXT NOT NULL,
    source_token TEXT,
    bound_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    dispute_status TEXT,
    dispute_note TEXT,
    admin_override_user_id INTEGER,
    admin_override_at TIMESTAMP
);
CREATE TABLE customer_agent_binding_disputes (
    id BIGSERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL,
    old_agent_user_id INTEGER NOT NULL,
    new_agent_user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    admin_user_id INTEGER,
    admin_decision TEXT,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);
CREATE TABLE brands (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id INTEGER,
    industry TEXT,
    -- 🔴 [WO-D-R2 ② 2026-08-20] 这两列不是本包要用的业务列,是**让 SSOT 跑得起来的前置**:
    --   init_db() 建 idx_brands_company / idx_brands_industry 时会读它们
    --   (db/diagnosis_db.py:596-597),而全仓**没有任何**
    --   `_safe_add_column(cursor, "brands", ...)` —— brands 是唯一一张没有自愈补列的表。
    --   于是任何手搓得比生产窄的 brands 都会让 init_db 当场 UndefinedColumn 炸掉。
    --   (同一个病在 WO-D ② 的 publish_center 夹具上已犯过一次。)
    industry_category TEXT,
    company_name TEXT,
    status TEXT,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE public_account_codes (
    user_id INTEGER PRIMARY KEY,
    service_account_code TEXT UNIQUE,
    channel_account_code TEXT UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE pricing_catalog_versions (
    id BIGSERIAL PRIMARY KEY,
    catalog_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version_code TEXT NOT NULL,
    status TEXT NOT NULL,
    effective_to TIMESTAMPTZ
);
CREATE TABLE pricing_catalog_entries (
    id BIGSERIAL PRIMARY KEY,
    version_id BIGINT NOT NULL REFERENCES pricing_catalog_versions(id),
    product_code TEXT NOT NULL,
    base_price_cents INTEGER NOT NULL,
    multiplier_bps INTEGER NOT NULL,
    final_price_cents INTEGER NOT NULL,
    paid_points BIGINT NOT NULL,
    bonus_points BIGINT NOT NULL DEFAULT 0,
    cost_floor_cents INTEGER,
    source_ref_jsonb JSONB
);
CREATE TABLE channel_pricing_relationships (
    id BIGSERIAL PRIMARY KEY,
    buyer_dealer_id INTEGER NOT NULL,
    upstream_channel_account_id INTEGER NOT NULL,
    relationship_version TEXT NOT NULL,
    cost_multiplier_bps INTEGER NOT NULL DEFAULT 10000,
    status TEXT NOT NULL DEFAULT 'active',
    effective_from TIMESTAMPTZ DEFAULT NOW(),
    effective_to TIMESTAMPTZ,
    reason TEXT,
    approved_by INTEGER,
    created_by INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);
CREATE TABLE channel_revenue_ledger (
    id BIGSERIAL PRIMARY KEY,
    channel_beneficiary_user_id INTEGER NOT NULL,
    buyer_dealer_id INTEGER NOT NULL,
    recharge_order_id TEXT NOT NULL,
    channel_revenue_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded'
        CHECK (status IN ('recorded','reversed')),
    reversed_at TIMESTAMPTZ
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
CREATE TABLE agent_inventory_wallets (
    agent_user_id INTEGER PRIMARY KEY,
    paid_inventory_points BIGINT NOT NULL DEFAULT 0,
    bonus_inventory_points BIGINT NOT NULL DEFAULT 0,
    frozen_inventory_points BIGINT NOT NULL DEFAULT 0,
    total_purchased_points BIGINT NOT NULL DEFAULT 0,
    total_allocated_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE customer_agent_credit_wallets (
    customer_user_id INTEGER PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    tool_credit_points BIGINT NOT NULL DEFAULT 0,
    publish_credit_points BIGINT NOT NULL DEFAULT 0,
    bonus_credit_points BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE dealer_resale_global_settings (
    singleton_id SMALLINT PRIMARY KEY DEFAULT 1,
    platform_seller_user_id INTEGER
);
CREATE TABLE dealer_inventory_lots (
    lot_id TEXT PRIMARY KEY, owner_agent_user_id INTEGER NOT NULL,
    remaining_points BIGINT NOT NULL DEFAULT 0, reserved_points BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE dealer_resale_orders (
    order_id TEXT PRIMARY KEY, seller_user_id INTEGER NOT NULL,
    refund_responsible_user_id INTEGER NOT NULL, state TEXT NOT NULL
);
CREATE TABLE dealer_consumer_sales (
    order_id TEXT PRIMARY KEY, seller_user_id INTEGER NOT NULL,
    refund_responsible_user_id INTEGER NOT NULL, state TEXT NOT NULL
);
CREATE TABLE dealer_resale_profit_ledger (
    profit_id BIGSERIAL PRIMARY KEY, seller_user_id INTEGER NOT NULL, status TEXT NOT NULL,
    margin_cents BIGINT NOT NULL DEFAULT 1, revenue_ledger_id BIGINT
);
CREATE TABLE dealer_resale_fulfillment_plans (
    plan_id TEXT PRIMARY KEY, final_seller_user_id INTEGER NOT NULL,
    final_buyer_user_id INTEGER NOT NULL, state TEXT NOT NULL
);
CREATE TABLE dealer_resale_fulfillment_hops (
    plan_id TEXT NOT NULL, hop_seq INTEGER NOT NULL, seller_user_id INTEGER NOT NULL,
    buyer_user_id INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY(plan_id,hop_seq)
);
CREATE TABLE dealer_resale_fulfillment_allocations (
    allocation_id BIGSERIAL PRIMARY KEY, plan_id TEXT NOT NULL, hop_seq INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE dealer_resale_hop_profit_ledger (
    profit_id BIGSERIAL PRIMARY KEY, plan_id TEXT NOT NULL, hop_seq INTEGER NOT NULL,
    seller_user_id INTEGER NOT NULL, agent_payable_cents BIGINT NOT NULL, status TEXT NOT NULL,
    revenue_ledger_id BIGINT
);
CREATE TABLE refund_work_orders (
    id BIGSERIAL PRIMARY KEY, agent_user_id INTEGER, status TEXT NOT NULL
);
CREATE TABLE consumer_refund_cases (
    case_id TEXT PRIMARY KEY, responsible_service_user_id INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE service_refund_reserve_accounts (
    service_user_id INTEGER PRIMARY KEY, available_cents BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE service_refund_liability_ledger (
    liability_id BIGSERIAL PRIMARY KEY, service_user_id INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE service_refund_funding_work_orders (
    work_order_id TEXT PRIMARY KEY, service_user_id INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE service_refund_cash_jobs (
    cash_job_id TEXT PRIMARY KEY, responsible_service_user_id INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE recharge_orders (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    order_type TEXT,
    agent_user_id INTEGER,
    payment_status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMP
);
CREATE TABLE dispute_escrow (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE,
    dispute_id BIGINT,
    customer_user_id INTEGER NOT NULL,
    order_agent_user_id INTEGER NOT NULL,
    bound_agent_user_id INTEGER,
    status TEXT NOT NULL DEFAULT 'held'
        CHECK (status IN ('held','settled','refunded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE agent_revenue_ledger (
    id BIGSERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    agent_settlement_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'frozen'
        CHECK (status IN ('frozen','settled','cancelled')),
    reversed_at TIMESTAMP
);
CREATE TABLE agent_settlement_requests (
    id SERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    request_amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','paid','rejected'))
);
CREATE TABLE agent_settlement_request_items (
    id BIGSERIAL PRIMARY KEY,
    settlement_request_id INTEGER NOT NULL REFERENCES agent_settlement_requests(id),
    ledger_id BIGINT NOT NULL REFERENCES agent_revenue_ledger(id),
    locked_amount_cents INTEGER NOT NULL
);
CREATE TABLE agent_commission_redemption_requests (
    id SERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE agent_commission_redemption_items (
    id SERIAL PRIMARY KEY,
    redemption_request_id INTEGER NOT NULL,
    ledger_id BIGINT NOT NULL,
    locked_amount_cents INTEGER NOT NULL
);
CREATE TABLE withdrawal_requests (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE point_transactions (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    point_type TEXT,
    amount BIGINT NOT NULL,
    balance_after BIGINT,
    feature_code TEXT,
    description TEXT,
    order_id TEXT,
    brand_id INTEGER,
    source TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE organizations (
    id BIGSERIAL PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
-- [补充工单 2026-08-06] 这个桩原来只有 4 列,而生产的 organization_memberships 有 18 列。
-- 用户列表接上账号来源之后,`list_admin_users()` 每次都会读 is_owner / role_id / joined_at ——
-- 桩缺列 → 整个列表接口在测试里当场 UndefinedColumn,而生产完全正常。
-- 🔴 补的是**桩**不是代码:列名与类型已用生产 information_schema 核过
--    (SQL 4 维核验的列名 / data_type / 字段归属三维),不是照着代码猜的。
CREATE TABLE organization_memberships (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role_id BIGINT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    is_owner BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE organization_invites (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NOT NULL REFERENCES organizations(id),
    status TEXT NOT NULL DEFAULT 'pending',
    accepted_membership_id BIGINT REFERENCES organization_memberships(id)
);
CREATE TABLE diagnosis_records (
    -- 🔴 [WO-D-R2 ② 2026-08-20] 下面这些列不是本包的业务列,是**让 SSOT(init_db)
    --   跑得完的前置**:init_db 会给它们建索引(db/diagnosis_db.py 的 CREATE INDEX 段),
    --   夹具手搓的表比生产窄一列,init_db 就在那一行抛 UndefinedColumn 中断。
    --   这 5 个(表,列)组合是**机械枚举**出来的:把 init_db 所有 CREATE INDEX 的
    --   (表,列)与本库 information_schema 求差,不是逐个试出来的。
    industry_category TEXT,
    brand_recognition_level TEXT,
    id BIGSERIAL PRIMARY KEY,
    brand_id BIGINT NOT NULL REFERENCES brands(id),
    brand_name TEXT NOT NULL,
    industry TEXT,
    total_score INTEGER,
    level TEXT,
    result_visibility TEXT,
    web_search_score INTEGER,
    platform_score INTEGER,
    content_quality_score INTEGER,
    authority_score INTEGER,
    ai_visibility_score INTEGER,
    ai_citation_score INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE quotes (
    id BIGSERIAL PRIMARY KEY, brand_id BIGINT NOT NULL REFERENCES brands(id),
    brand_name TEXT, status TEXT, internal_cost NUMERIC, profit NUMERIC,
    public_token TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), confirmed_at TIMESTAMPTZ
);
CREATE TABLE client_access_tokens (
    -- 🔴 [WO-D-R2 ② 2026-08-20] 生产这张表有 id(db/monitoring_db.py:1911 SERIAL PRIMARY KEY),
    --   夹具手搓时漏了。生产读路径 ORDER BY t.created_at DESC, t.id DESC ⇒ 直接
    --   `UndefinedColumn: column t.id does not exist`,3 条判据因此红,与被测代码无关。
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT NOT NULL REFERENCES quotes(id),
    brand_name TEXT,
    token TEXT NOT NULL UNIQUE,
    is_active SMALLINT NOT NULL DEFAULT 1,
    -- 🔴 [WO-D-R2 ② 2026-08-20] 生产是 DATE(db/monitoring_db.py:1916),夹具原写 TIMESTAMPTZ。
    --   类型漂移的后果不是「差不多」:快照序列化拿到 datetime 而不是 date,
    --   直接 `TypeError: Object of type datetime is not JSON serializable`。
    expires_at DATE,
    last_access_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE confirmed_keywords (
    status TEXT,
    id BIGSERIAL PRIMARY KEY, quote_id BIGINT NOT NULL REFERENCES quotes(id), keyword TEXT
);
CREATE TABLE extra_keywords (
    id BIGSERIAL PRIMARY KEY, quote_id BIGINT NOT NULL REFERENCES quotes(id), keyword TEXT
);
CREATE TABLE article_generations (
    diagnosis_id BIGINT,
    task_id TEXT,
    id BIGSERIAL PRIMARY KEY, brand_id BIGINT NOT NULL REFERENCES brands(id), quote_id BIGINT,
    status TEXT, article_count INTEGER, provider_secret TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), completed_at TIMESTAMPTZ
);
CREATE TABLE articles (
    id BIGSERIAL PRIMARY KEY, brand_id BIGINT NOT NULL REFERENCES brands(id), quote_id BIGINT,
    title TEXT, status TEXT, sensitive_body TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), published_at TIMESTAMPTZ
);
CREATE TABLE media_publications (
    id BIGSERIAL PRIMARY KEY, brand_id BIGINT NOT NULL REFERENCES brands(id), article_id BIGINT,
    status TEXT, media_name TEXT, upstream_token TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), published_at TIMESTAMPTZ
);
CREATE TABLE monitoring_tasks (
    id BIGSERIAL PRIMARY KEY, brand_id BIGINT NOT NULL REFERENCES brands(id), status TEXT,
    task_name TEXT, platform TEXT, provider_token TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), completed_at TIMESTAMPTZ
);
CREATE TABLE monitoring_reports (
    id BIGSERIAL PRIMARY KEY, brand_id BIGINT NOT NULL REFERENCES brands(id), status TEXT,
    title TEXT, overall_score INTEGER, visibility_rate NUMERIC, internal_cost NUMERIC,
    created_at TIMESTAMPTZ DEFAULT NOW(), completed_at TIMESTAMPTZ
);
"""


TABLES = [
    "admin_provider_downgrade_plans",
    "admin_demo_access_events",
    "admin_demo_case_grants",
    "admin_demo_cases",
    "admin_cross_tenant_audits",
    "admin_governance_subject_versions",
    "organization_invite_accept_receipts",
    "organization_invites",
    "organization_memberships",
    "organizations",
    "monitoring_reports",
    "monitoring_tasks",
    "media_publications",
    "articles",
    "article_generations",
    "extra_keywords",
    "confirmed_keywords",
    "client_access_tokens",
    "quotes",
    "diagnosis_records",
    "notification_outbox",
    "user_notifications",
    "pricing_catalog_entries",
    "pricing_catalog_versions",
    "customer_agent_binding_history",
    "admin_user_governance_audits",
    "admin_user_governance_versions",
    "audit_logs",
    "point_transactions",
    "withdrawal_requests",
    "agent_commission_redemption_items",
    "agent_commission_redemption_requests",
    "agent_settlement_request_items",
    "agent_settlement_requests",
    "agent_revenue_ledger",
    "dispute_escrow",
    "recharge_orders",
    "service_refund_cash_jobs",
    "service_refund_funding_work_orders",
    "service_refund_liability_ledger",
    "service_refund_reserve_accounts",
    "consumer_refund_cases",
    "refund_work_orders",
    "dealer_resale_hop_profit_ledger",
    "dealer_resale_fulfillment_allocations",
    "dealer_resale_fulfillment_hops",
    "dealer_resale_fulfillment_plans",
    "dealer_resale_profit_ledger",
    "dealer_consumer_sales",
    "dealer_resale_orders",
    "dealer_inventory_lots",
    "dealer_resale_global_settings",
    "customer_agent_credit_wallets",
    "agent_inventory_wallets",
    "agent_pricing_overrides",
    "channel_revenue_ledger",
    "channel_pricing_relationships",
    "public_account_codes",
    "brands",
    "customer_agent_binding_disputes",
    "customer_agent_bindings",
    "referral_links",
    "user_clients",
    "role_permissions",
    "user_roles",
    "user_wallets",
    "roles",
    "system_settings",
    "users",
]


def _apply_governance_migration(conn) -> None:
    scripts = (
        "migration_admin_user_governance_2026_07_15.sql",
        "migration_admin_user_governance_extensions_2026_07_19.sql",
        "migration_admin_cross_tenant_governance_2026_07_21.sql",
    )
    with conn.cursor() as cur:
        for script in scripts:
            cur.execute((ROOT / "scripts" / script).read_text(encoding="utf-8"))
    conn.commit()


def _apply_notification_migration(conn) -> None:
    sql = (ROOT / "scripts" / "migration_notification_outbox_2026_07_17.sql").read_text(
        encoding="utf-8"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _seed(conn) -> None:
    users = [
        (1, "admin_one", "平台管理员一", 1, None, None),
        (2, "admin_two", "平台管理员二", 1, None, None),
        (28, "provider_28", "深圳超长企业名称示例服务商二十八号", 1, "深圳长名称科技有限公司", None),
        (102, "inviter_102", "邀请来源一百零二", 1, "邀请来源公司", None),
        (123, "provider_123", "历史服务商一二三", 1, "历史服务主体", None),
        (124, "customer_124", "历史证据缺失客户", 1, "客户主体", None),
        (129, "customer_129", "双关系客户一二九", 1, None, 102),
        (130, "customer_130", "双关系客户一三零", 1, None, 102),
        (131, "customer_131", "双关系客户一三一", 1, None, 102),
        (132, "demo_132", "明日演示账号一三二", 1, "演示账号主体", None),
        (200, "provider_200", "候选服务商二百", 1, "候选服务主体", None),
        (300, "customer_300", "待签约普通用户三百", 1, "待签约客户主体", None),
    ]
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO system_settings(key,value,value_type)
               VALUES ('DEALER_INVENTORY_RESALE_ENABLED','false','boolean')"""
        )
        cur.execute(
            "INSERT INTO dealer_resale_global_settings(singleton_id,platform_seller_user_id) VALUES (1,NULL)"
        )
        cur.executemany(
            """INSERT INTO users(id,username,display_name,is_active,company,referred_by_agent_id)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            users,
        )
        cur.executemany(
            "INSERT INTO roles(id,name,display_name) VALUES (%s,%s,%s)",
            [(1, "admin", "超级管理员"), (2, "social_ops", "普通用户"),
             (3, "geo_writer", "GEO编辑（未启用）")],
        )
        cur.executemany(
            "INSERT INTO role_permissions(role_id,module,level) VALUES (%s,%s,%s)",
            [(2, "dashboard", "read"), (3, "writing", "write")],
        )
        cur.executemany(
            "INSERT INTO user_roles(user_id,role_id) VALUES (%s,%s)",
            [(1, 1), (1, 2), (2, 1), (2, 2), (124, 2), (124, 3),
             (28, 2), (102, 2), (123, 2), (129, 2), (130, 2), (131, 2), (132, 2), (200, 2),
             (300, 2)],
        )
        wallet_rows = []
        for user_id, *_ in users:
            level = 1 if user_id in {28, 102, 123, 200} else 0
            wallet_rows.append((user_id, 1000 + user_id, 50, 5000, level))
        cur.executemany(
            """INSERT INTO user_wallets(user_id,paid_points,bonus_points,total_recharged,agent_level)
               VALUES (%s,%s,%s,%s,%s)""",
            wallet_rows,
        )
        cur.executemany(
            """INSERT INTO referral_links(referrer_id,referred_id,level,commission_rate,created_at)
               VALUES (%s,%s,1,0,'2026-07-15 10:00:00')""",
            [(102, 129), (102, 130), (102, 131)],
        )
        cur.execute(
            """INSERT INTO customer_agent_bindings(
                   id,customer_user_id,agent_user_id,binding_source,bound_at)
               VALUES (30,124,123,'admin_manual','2026-07-14 09:00:00')"""
        )
        cur.executemany(
            """INSERT INTO customer_agent_bindings(
                   customer_user_id,agent_user_id,binding_source,source_token,bound_at)
               VALUES (%s,28,'invite_code','INV-28',%s)""",
            [(129, "2026-07-15 10:03:58"), (130, "2026-07-15 10:03:20"), (131, "2026-07-15 10:04:16")],
        )
        cur.executemany(
            """INSERT INTO public_account_codes(user_id,service_account_code,channel_account_code)
               VALUES (%s,%s,%s)""",
            [(28, "SV-AAA028", "CH-AAA028"), (102, "SV-AAA102", "CH-AAA102"),
             (123, "SV-AAA123", "CH-AAA123"), (200, "SV-AAA200", "CH-AAA200")],
        )
        cur.execute(
            """INSERT INTO pricing_catalog_versions(
                   catalog_type,scope_key,version_code,status,effective_to)
               VALUES ('retail','SV-AAA200','retail-ready-v1','published',NULL)
               RETURNING id"""
        )
        retail_version_id = int(cur.fetchone()[0])
        source_ref = {
            "agent_user_id": 200,
            "sku_template_id": 1,
            "override_id": 701,
            "template_code": "svc-ready",
            "sku_type": "tool",
            "wholesale_cents": 8000,
            "retail_cents": 10000,
            "points_granted": 1000,
            "tool_points": 1000,
            "publish_points": 0,
        }
        cur.execute(
            """INSERT INTO pricing_catalog_entries(
                   version_id,product_code,base_price_cents,multiplier_bps,
                   final_price_cents,paid_points,bonus_points,cost_floor_cents,
                   source_ref_jsonb)
               VALUES (%s,'svc-ready',10000,10000,10000,1000,0,8000,%s::jsonb)""",
            (retail_version_id, json.dumps(source_ref)),
        )
        cur.execute(
            """INSERT INTO channel_pricing_relationships(
                   buyer_dealer_id,upstream_channel_account_id,relationship_version,reason)
               VALUES (200,28,'channel-rel-v1','测试直属渠道关系')"""
        )
        cur.execute(
            """INSERT INTO brands(id,name,owner_user_id,industry,status)
               VALUES (501,'这是一个用于验证超长品牌名称换行而不是跑马灯的品牌',124,'企业服务','active')"""
        )
        cur.executemany(
            """INSERT INTO brands(id,name,owner_user_id,industry,status)
               VALUES (%s,'浙江岱林',%s,%s,'active')""",
            [(601, 124, "生命科学"), (602, 129, "工业设备")],
        )
        cur.execute("INSERT INTO user_clients(user_id,brand_id) VALUES (124,601)")
        cur.executemany(
            """INSERT INTO diagnosis_records(
                   id,brand_id,brand_name,industry,total_score,level,result_visibility,
                   web_search_score,platform_score,content_quality_score,authority_score,
                   ai_visibility_score,ai_citation_score,created_at)
               VALUES (%s,%s,'浙江岱林',%s,%s,'A','published',80,81,82,83,84,85,%s)""",
            [
                (9601, 601, "生命科学", 88, "2026-07-20 10:00:00+00"),
                (9602, 602, "工业设备", 79, "2026-07-20 11:00:00+00"),
            ],
        )
        cur.execute(
            """INSERT INTO quotes(id,brand_id,brand_name,status,internal_cost,profit,public_token)
               VALUES (9701,601,'浙江岱林','confirmed',1200,3800,'never-expose-token')"""
        )
        cur.execute(
            """INSERT INTO client_access_tokens(quote_id,brand_name,token,is_active,expires_at)
               VALUES (9701,'浙江岱林','DAILINPORTAL2026',1,'2027-07-21 00:00:00+00')"""
        )
        cur.execute("INSERT INTO article_generations(id,brand_id,quote_id,status,article_count,provider_secret) VALUES (9801,601,9701,'completed',3,'provider-secret')")
        cur.execute("INSERT INTO articles(id,brand_id,quote_id,title,status,sensitive_body) VALUES (9811,601,9701,'浙江岱林行业洞察','published','敏感正文不进入演示快照')")
        cur.execute("INSERT INTO media_publications(id,brand_id,article_id,status,media_name,upstream_token) VALUES (9821,601,9811,'published','行业媒体','upstream-secret')")
        cur.execute("INSERT INTO monitoring_tasks(id,brand_id,status,task_name,platform,provider_token) VALUES (9831,601,'completed','品牌可见度监测','主流平台','monitor-secret')")
        cur.execute("INSERT INTO monitoring_reports(id,brand_id,status,title,overall_score,visibility_rate,internal_cost) VALUES (9841,601,'completed','周度监测报告',86,0.72,450)")
        cur.execute(
            """INSERT INTO recharge_orders(id,user_id,amount_cents,payment_status,created_at)
               VALUES ('ORDER-124',124,12800,'paid','2026-07-15 11:00:00')"""
        )
        cur.execute(
            """INSERT INTO point_transactions(user_id,type,amount,description,created_at)
               VALUES (124,'consume',-100,'诊断服务','2026-07-15 11:30:00')"""
        )
    conn.commit()


@pytest.fixture(scope="session", autouse=True)
def isolated_postgres_schema():
    _assert_safe_test_database()
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(BASE_SCHEMA)
    conn.commit()
    _apply_governance_migration(conn)
    _apply_governance_migration(conn)
    _apply_notification_migration(conn)
    _apply_notification_migration(conn)
    conn.close()

    # 🔴 [WO-D-R2 ② 2026-08-20] 让 SSOT 的自愈 DDL **在这里一次跑完**,不要留到测试中途。
    #
    # 定案(pg_locks 机械取证,不是猜):
    #   持锁者  AccessShareLock on quotes/brands —— 该事务里**只有**
    #           services/portal_token_authority.py:65 那条 SELECT(全仓唯一出处);
    #           借用栈实测 = test_cross_tenant_governance.py:462 → :448 regenerate
    #           → portal_token_authority.py:65 → contextlib.__enter__ → connection.py:207 get_db
    #   被挡者  ALTER TABLE quotes ADD COLUMN organization_id BIGINT
    #           ← db/diagnosis_db.py:205-222 的 organization 自愈列循环
    #           ← init_db(),它在 **import 时**就会跑(diagnosis_db.py:8573)
    #              以及 app 启动时跑(server.py:7914)
    #   ⇒ 一个读事务没归还 + 一条真要 ACCESS EXCLUSIVE 的 ALTER = 互等。
    #     实测 3/3 挂满 900s 上限(全新库 · 无并发干扰),不是慢,是真挡住。
    #
    # 那条 ALTER **为什么有活干**:上面的 BASE_SCHEMA 手搓了 quotes,没有 organization_id。
    # init_db 自愈的 10 张表里,BASE_SCHEMA 手搓了 7 张 —— 所以只补 quotes 一列没用,
    # 挡点会挪到 articles / monitoring_tasks 等下一张表上。
    #
    # 修法 = 走 SSOT,而且**放对位置**:
    #   在这里(夹具建完表、**任何用例都还没借连接**)把 init_db() 跑一遍,
    #   7 张表的自愈列一次补齐;此后 import 时/启动时的 init_db() 全是 no-op,
    #   不再取 ACCESS EXCLUSIVE,也就无从与任何读事务互等。
    #   🔴 上一轮我猜「先把库 init_db 一遍就行」被实测推翻 —— 因为那是在 pytest 之前跑,
    #      而本夹具紧接着 DROP SCHEMA public CASCADE 把它全删了。位置错,不是方向错。
    from db.diagnosis_db import init_db as _ssot_init_db

    _ssot_init_db()

    from db import connection
    connection.close_pool()
    connection.DATABASE_URL = TEST_DATABASE_URL
    yield
    connection.close_pool()


@pytest.fixture(autouse=True)
def reset_governance_data(isolated_postgres_schema):
    conn = psycopg2.connect(TEST_DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE " + ",".join(TABLES) + " RESTART IDENTITY CASCADE")
    conn.commit()
    _seed(conn)
    conn.close()
    yield
