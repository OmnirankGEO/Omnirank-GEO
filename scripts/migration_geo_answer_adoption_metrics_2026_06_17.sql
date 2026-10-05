-- GEO answer adoption metrics shadow table · 2026-06-17
-- Idempotent, additive only. No destructive SQL.

CREATE TABLE IF NOT EXISTS geo_answer_adoption_metrics (
    id BIGSERIAL PRIMARY KEY,
    source_url TEXT NOT NULL,
    domain VARCHAR(300),
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    engine VARCHAR(40) NOT NULL DEFAULT '',
    prompt_id TEXT NOT NULL DEFAULT '',
    round_id VARCHAR(80) NOT NULL DEFAULT '',
    signal_tier VARCHAR(40) NOT NULL,
    answer_adopted BOOLEAN NOT NULL DEFAULT FALSE,
    explicit_cited BOOLEAN NOT NULL DEFAULT FALSE,
    search_exposed BOOLEAN NOT NULL DEFAULT FALSE,
    reference_only BOOLEAN NOT NULL DEFAULT FALSE,
    rejected_noise BOOLEAN NOT NULL DEFAULT FALSE,
    source_position INTEGER DEFAULT 0,
    total_sources_in_answer INTEGER DEFAULT 1,
    normalized_credit NUMERIC(12,6) NOT NULL DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_url, industry_key, engine, prompt_id, signal_tier, round_id)
);

CREATE INDEX IF NOT EXISTS idx_geo_answer_metrics_industry
    ON geo_answer_adoption_metrics(industry_key, normalized_credit DESC);

CREATE INDEX IF NOT EXISTS idx_geo_answer_metrics_tier
    ON geo_answer_adoption_metrics(signal_tier);

CREATE INDEX IF NOT EXISTS idx_geo_answer_metrics_domain
    ON geo_answer_adoption_metrics(domain);
