-- Minimal PostgreSQL 16 integration fixture for the GEO article v1.4 package.
-- Test-only: this schema is isolated from application/production schemas.

CREATE SCHEMA IF NOT EXISTS geo_article_v14_full_20260719;
SET search_path TO geo_article_v14_full_20260719, public;

CREATE TABLE IF NOT EXISTS article_generations (
    id SERIAL PRIMARY KEY,
    content TEXT
);

CREATE TABLE IF NOT EXISTS topics (
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT
);

CREATE TABLE IF NOT EXISTS quotes (
    id BIGSERIAL PRIMARY KEY,
    brand_id BIGINT,
    industry TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id BIGSERIAL PRIMARY KEY,
    topic_id BIGINT REFERENCES topics(id),
    quote_id BIGINT REFERENCES quotes(id),
    title TEXT,
    content TEXT,
    version INTEGER DEFAULT 1,
    style TEXT,
    style_code TEXT,
    first_published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mhz_publish_orders (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT REFERENCES articles(id),
    article_title TEXT
);

CREATE TABLE IF NOT EXISTS mhz_publish_order_items (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES mhz_publish_orders(id),
    status TEXT,
    publish_url TEXT,
    published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS publish_orders (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT REFERENCES articles(id),
    article_title TEXT,
    article_content TEXT
);

CREATE TABLE IF NOT EXISTS publish_order_items (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES publish_orders(id),
    status TEXT,
    publish_url TEXT,
    published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS media_publications (
    id BIGSERIAL PRIMARY KEY,
    quote_id BIGINT,
    article_id BIGINT REFERENCES articles(id),
    platform_url TEXT,
    publish_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS publish_records (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT REFERENCES articles(id),
    status TEXT,
    draft_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS monitoring_tasks (
    id BIGSERIAL PRIMARY KEY,
    brand_id BIGINT
);

CREATE TABLE IF NOT EXISTS monitoring_results (
    id BIGSERIAL PRIMARY KEY,
    task_id BIGINT REFERENCES monitoring_tasks(id),
    tested_at TIMESTAMPTZ,
    search_citations JSONB
);

CREATE TABLE IF NOT EXISTS geo_research_raw (
    id BIGSERIAL PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS geo_research_prompts (
    id BIGSERIAL PRIMARY KEY,
    industry_id BIGINT NOT NULL,
    prompt_text TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    source TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS geo_research_articles (
    id BIGSERIAL PRIMARY KEY,
    url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS geo_research_source_signals (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT REFERENCES geo_research_articles(id),
    source_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS llm_call_log (
    id BIGSERIAL PRIMARY KEY,
    caller TEXT,
    estimated_cost NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
