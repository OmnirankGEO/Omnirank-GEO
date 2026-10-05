CREATE TABLE roles (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    is_system BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id),
    module TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'read',
    UNIQUE(role_id,module,level)
);

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT 'throwaway',
    display_name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    permission_version INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    avatar_url TEXT,
    phone TEXT,
    phone_verified BOOLEAN NOT NULL DEFAULT FALSE,
    email TEXT,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMP
);

CREATE TABLE user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id),
    role_id INTEGER NOT NULL REFERENCES roles(id),
    PRIMARY KEY(user_id,role_id)
);

CREATE TABLE agreement_signatures (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    agreement_type VARCHAR(50) NOT NULL,
    agreement_version VARCHAR(20) NOT NULL,
    content_hash TEXT,
    signed_at TIMESTAMP DEFAULT NOW(),
    ip_address VARCHAR(45),
    user_agent TEXT,
    evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX idx_agreement_signatures_unique
    ON agreement_signatures(user_id,agreement_type,agreement_version);

-- brands: 见 verify_local.BASE_SQL —— 由 db.brands_schema.brands_schema_sql()
-- (生产 SSOT 出口的纯 SQL 形态) 拼在本文件前面,不在这里手搓。

CREATE TABLE whitelabel_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    company_name TEXT,
    logo_url TEXT,
    slogan TEXT,
    contact_name TEXT,
    contact_phone TEXT,
    contact_wechat TEXT,
    contact_email TEXT,
    brand_color TEXT,
    product_name TEXT,
    favicon_url TEXT,
    whitelabel_mode TEXT DEFAULT 'none',
    whitelabel_status TEXT DEFAULT 'locked',
    unlocked_by_admin BOOLEAN DEFAULT FALSE
);

CREATE TABLE user_clients (
    user_id INTEGER NOT NULL REFERENCES users(id),
    brand_id INTEGER NOT NULL REFERENCES brands(id),
    PRIMARY KEY(user_id,brand_id)
);

CREATE TABLE pricing_catalog_versions (
    id BIGSERIAL PRIMARY KEY,
    catalog_type TEXT NOT NULL CHECK (catalog_type IN ('procurement','retail','feature_consumption')),
    scope_key TEXT NOT NULL,
    version_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(catalog_type,scope_key,version_code)
);
CREATE UNIQUE INDEX ux_catalog_published_open
    ON pricing_catalog_versions(catalog_type,scope_key)
    WHERE status='published' AND effective_to IS NULL;

CREATE TABLE pricing_catalog_entries (
    id BIGSERIAL PRIMARY KEY,
    version_id BIGINT NOT NULL REFERENCES pricing_catalog_versions(id) ON DELETE CASCADE,
    product_code TEXT NOT NULL,
    base_price_cents INTEGER NOT NULL DEFAULT 0,
    multiplier_bps INTEGER NOT NULL DEFAULT 10000,
    final_price_cents INTEGER NOT NULL DEFAULT 0,
    paid_points BIGINT NOT NULL DEFAULT 0,
    bonus_points BIGINT NOT NULL DEFAULT 0,
    cost_floor_cents INTEGER,
    usage_example_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(version_id,product_code)
);

CREATE TABLE user_wallets (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    paid_points BIGINT NOT NULL DEFAULT 0 CHECK (paid_points >= 0),
    bonus_points BIGINT NOT NULL DEFAULT 0 CHECK (bonus_points >= 0),
    commission_points BIGINT NOT NULL DEFAULT 0 CHECK (commission_points >= 0),
    frozen_points BIGINT NOT NULL DEFAULT 0 CHECK (frozen_points >= 0),
    total_recharged BIGINT NOT NULL DEFAULT 0,
    agent_level INTEGER NOT NULL DEFAULT 0,
    deduction_preference VARCHAR(20) NOT NULL DEFAULT 'default',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

CREATE TABLE point_freezes (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    feature_code TEXT NOT NULL,
    amount_total BIGINT NOT NULL,
    amount_bonus BIGINT NOT NULL DEFAULT 0,
    amount_commission BIGINT NOT NULL DEFAULT 0,
    amount_paid BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'frozen' CHECK (status IN ('frozen','committed','released')),
    task_ref TEXT,
    brand_id INTEGER,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    committed_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ
);

CREATE TABLE feature_pricing (
    feature_code TEXT PRIMARY KEY,
    feature_name TEXT NOT NULL,
    cost_points INTEGER NOT NULL,
    cost_compute NUMERIC(12,2) NOT NULL DEFAULT 0,
    requires_paid_points BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE customer_agent_credit_wallets (
    customer_user_id INTEGER PRIMARY KEY REFERENCES users(id)
);

-- Deliberately partial legacy artifact tables. The organization migration must
-- repair every missing nullable isolation column before adding constraints.
CREATE TABLE client_profiles (id BIGSERIAL PRIMARY KEY, brand_id INTEGER, is_deleted INTEGER DEFAULT 0, deleted_at TIMESTAMPTZ, archived_with_brand_at TIMESTAMPTZ);
CREATE TABLE client_materials (id BIGSERIAL PRIMARY KEY, brand_id INTEGER);
CREATE TABLE diagnosis_records (id BIGSERIAL PRIMARY KEY, brand_id INTEGER, is_deleted BOOLEAN DEFAULT FALSE, deleted_at TIMESTAMPTZ, archived_with_brand_at TIMESTAMPTZ);
CREATE TABLE quotes (id BIGSERIAL PRIMARY KEY, brand_id INTEGER, status TEXT DEFAULT 'draft', status_before_archive TEXT, deleted_at TIMESTAMPTZ, archive_reason TEXT, archived_with_brand_at TIMESTAMPTZ, archived_by_user_id INTEGER);
CREATE TABLE keyword_selection_sessions (id BIGSERIAL PRIMARY KEY, brand_id INTEGER, quote_id BIGINT REFERENCES quotes(id), status TEXT DEFAULT 'pending', status_before_archive TEXT, archived_at TIMESTAMPTZ, archived_with_brand_at TIMESTAMPTZ, archived_by_user_id INTEGER);
CREATE TABLE article_generations (id BIGSERIAL PRIMARY KEY);
CREATE TABLE articles (id BIGSERIAL PRIMARY KEY, quote_id BIGINT REFERENCES quotes(id));
CREATE TABLE media_publications (id BIGSERIAL PRIMARY KEY);
CREATE TABLE monitoring_tasks (id BIGSERIAL PRIMARY KEY, brand_id INTEGER);
CREATE TABLE monitoring_reports (id BIGSERIAL PRIMARY KEY, brand_id INTEGER);
CREATE TABLE publish_orders (id BIGSERIAL PRIMARY KEY);

-- The unified application initializes monitoring during ``server`` import.
-- Keep the organization fixture isolated while also reproducing the
-- public-qualified confirmed-keyword contract pinned by monitoring readiness.
CREATE TABLE confirmed_keywords (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER REFERENCES quotes(id),
    keyword TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS public.confirmed_keywords (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER,
    keyword TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- The unified monitoring settlement fence is public-qualified. Organization
-- HTTP tests fake the provider but still exercise the real terminal settlement
-- and dispatch-evidence reads, so preserve the columns those boundaries use.
CREATE TABLE IF NOT EXISTS public.monitoring_run_cells (
    id BIGSERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL,
    brand_id INTEGER,
    settlement_reference VARCHAR(160),
    provider_dispatched_at TIMESTAMPTZ,
    fulfillment_state VARCHAR(24) NOT NULL DEFAULT 'reserved',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.monitoring_keyword_settlements (
    settlement_reference VARCHAR(160) PRIMARY KEY,
    state VARCHAR(32) NOT NULL DEFAULT 'reserved'
);
