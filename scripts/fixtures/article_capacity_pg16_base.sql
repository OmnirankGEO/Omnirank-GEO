-- P1 篇数容量合同 · 隔离测试 fixture(PostgreSQL 16)
--
-- 🔴 列的形状**逐列照抄 2026-08-08 生产只读取证结果**(information_schema),不是照抄别的 fixture:
--    · confirmed_keywords.required_articles = INTEGER, NULLABLE, DEFAULT 1(生产实测)
--    · quotes.owner_user_id NULLABLE / paid_amount REAL / deleted_at 存在
--    · quote_pricing_snapshots 的 15 列与生产同名同型(snapshot_hash 是定长 character)
--    fixture 字段偏离生产真实形态 = 锁在假形状上跑绿,是最贵的一种假绿。

CREATE SCHEMA IF NOT EXISTS article_capacity_test;
SET search_path TO article_capacity_test, public;

CREATE TABLE brands (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id INTEGER,
    is_deleted BOOLEAN DEFAULT FALSE
);

CREATE TABLE quotes (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER REFERENCES brands(id),
    owner_user_id INTEGER,
    status TEXT DEFAULT 'draft',
    service_status TEXT DEFAULT 'pending',
    source_type TEXT DEFAULT 'agent_quote',
    paid_amount REAL,
    writing_status TEXT DEFAULT 'pending',
    total_articles INTEGER,
    active_pricing_snapshot_id BIGINT,
    deleted_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT NOW(),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE confirmed_keywords (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER REFERENCES quotes(id),
    keyword TEXT NOT NULL,
    required_articles INTEGER DEFAULT 1,     -- 生产实测:NULLABLE + DEFAULT 1
    monitoring_query TEXT,
    status TEXT DEFAULT 'pending',
    is_core BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE topics (
    id SERIAL PRIMARY KEY,
    keyword_id INTEGER REFERENCES confirmed_keywords(id),
    quote_id INTEGER REFERENCES quotes(id),
    article_id INTEGER,
    status TEXT DEFAULT 'pending',
    -- [WO_225-c1 §8.3] 媒体桶。与 db/migration_061_* 和
    --   db/diagnosis_db.init_db() 的自愈 DDL **同名同型同 CHECK**。
    --   🔴 本仓有三套 topics 的表定义(迁移 / 自愈 DDL / 本夹具),
    --      只改前两套的话,本夹具建出来的表缺这一列,读它的代码在判据里
    --      抛 UndefinedColumn —— 看起来像修法把判据打崩了,其实是第三套没跟上。
    media_bucket VARCHAR(40)
        CHECK (media_bucket IS NULL OR media_bucket IN (
            'focus_media_anchor',
            'industry_platform_coverage',
            'douyin_doubao_only')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE articles (
    id SERIAL PRIMARY KEY,
    topic_id INTEGER NOT NULL,
    quote_id INTEGER REFERENCES quotes(id),
    status TEXT DEFAULT 'draft',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE keyword_selection_sessions (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER UNIQUE NOT NULL REFERENCES quotes(id),
    brand_id INTEGER,
    token TEXT,
    status TEXT NOT NULL,
    pricing_data TEXT,
    clusters_data TEXT,
    pending_keywords TEXT,
    active_pricing_snapshot_id BIGINT,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE quote_pricing_snapshots (
    id BIGSERIAL PRIMARY KEY,
    quote_id INTEGER NOT NULL,
    brand_id INTEGER NOT NULL,
    selection_session_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    actor_user_id INTEGER,
    actor_membership_id BIGINT,
    previous_coefficient NUMERIC,
    coefficient NUMERIC,
    reason TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    pricing_snapshot JSONB NOT NULL,
    clusters_snapshot JSONB,
    snapshot_hash CHARACTER(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX ux_quote_pricing_snapshots_version
    ON quote_pricing_snapshots (quote_id, version);
