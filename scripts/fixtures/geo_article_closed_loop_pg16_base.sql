-- Isolated PostgreSQL 16 base fixture for the article closed-loop sidecar.
-- It intentionally mirrors the production integer identities verified by Deploy.

CREATE SCHEMA IF NOT EXISTS geo_article_closed_loop_test;
SET search_path TO geo_article_closed_loop_test, public;

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
    brand_name TEXT,
    industry TEXT,
    status TEXT,
    service_status TEXT,
    source_type TEXT,
    paid_amount REAL,
    writing_status TEXT,
    confirmed_at TIMESTAMP,
    paid_at TIMESTAMP,
    service_start_date DATE,
    service_end_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE confirmed_keywords (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER REFERENCES quotes(id),
    keyword TEXT NOT NULL,
    required_articles INTEGER DEFAULT 1,
    monitoring_query TEXT,
    status TEXT DEFAULT 'pending',
    is_core BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE keyword_selection_sessions (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER UNIQUE NOT NULL REFERENCES quotes(id),
    status TEXT NOT NULL,
    confirmed_at TEXT,
    payment_received_at TEXT,
    updated_at TEXT
);

CREATE TABLE article_generations (
    id SERIAL PRIMARY KEY,
    content TEXT
);

CREATE TABLE topics (
    id SERIAL PRIMARY KEY,
    keyword_id INTEGER REFERENCES confirmed_keywords(id),
    quote_id INTEGER REFERENCES quotes(id),
    original_keyword TEXT,
    optimized_title TEXT,
    article_style TEXT,
    style_code TEXT,
    cluster_id INTEGER,
    user_choice TEXT,
    user_choice_source TEXT,
    is_optimize BOOLEAN DEFAULT FALSE,
    fail_reason TEXT,
    article_id INTEGER,
    status TEXT DEFAULT 'draft',
    writing_started_at TIMESTAMP,
    confirmed_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE articles (
    id SERIAL PRIMARY KEY,
    topic_id INTEGER REFERENCES topics(id),
    quote_id INTEGER REFERENCES quotes(id),
    title TEXT,
    content TEXT,
    evidence_manifest_hash CHARACTER(64),
    current_content_hash CHARACTER(64),
    style_code TEXT,
    style TEXT,
    style_family CHARACTER VARYING(64),
    article_review JSONB,
    article_review_status TEXT,
    article_human_review_status CHARACTER VARYING(40),
    publication_profile CHARACTER VARYING(64) NOT NULL DEFAULT 'standard',
    platform_review JSONB,
    publication_snapshot_at TIMESTAMPTZ,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE topics
    ADD CONSTRAINT topics_article_id_articles_fk
    FOREIGN KEY (article_id) REFERENCES articles(id);

CREATE TABLE geo_article_review_events (
    id BIGSERIAL PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    actor_user_id INTEGER NOT NULL,
    decision CHARACTER VARYING(40) NOT NULL,
    reason TEXT NOT NULL,
    machine_review_status CHARACTER VARYING(40),
    machine_review_version CHARACTER VARYING(100),
    reviewed_content_hash CHARACTER(64),
    evidence_manifest_hash CHARACTER(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
