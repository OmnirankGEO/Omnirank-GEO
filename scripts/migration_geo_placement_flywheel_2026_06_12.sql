-- GEO placement data flywheel full build (Phase 1 + Phase 2 shadow foundation)
-- Date: 2026-06-12
-- Policy: idempotent only. No destructive SQL operations.

DO $$
BEGIN
    IF to_regclass('public.geo_research_raw') IS NOT NULL THEN
        ALTER TABLE geo_research_raw
            ADD COLUMN IF NOT EXISTS is_answer_cited BOOLEAN DEFAULT FALSE;
        ALTER TABLE geo_research_raw
            ADD COLUMN IF NOT EXISTS adoption_rank INTEGER;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS geo_industry_taxonomy (
    id BIGSERIAL PRIMARY KEY,
    industry_key VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL,
    aliases JSONB DEFAULT '[]'::jsonb,
    parent_key VARCHAR(100),
    active BOOLEAN DEFAULT TRUE,
    source VARCHAR(50) DEFAULT 'system',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_geo_industry_taxonomy_active
    ON geo_industry_taxonomy(active, industry_key);

CREATE TABLE IF NOT EXISTS geo_media_entities (
    id BIGSERIAL PRIMARY KEY,
    entity_key VARCHAR(80) NOT NULL UNIQUE,
    canonical_name VARCHAR(300) NOT NULL,
    domain VARCHAR(300),
    aliases JSONB DEFAULT '[]'::jsonb,
    entity_type VARCHAR(40) DEFAULT 'media_site',
    reference_status VARCHAR(40) DEFAULT 'reference_only',
    ownership_scope VARCHAR(40) DEFAULT 'unknown',
    home_url TEXT,
    tags JSONB DEFAULT '{}'::jsonb,
    confidence NUMERIC(5,4) DEFAULT 0.7000,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_geo_media_entities_domain ON geo_media_entities(domain);
CREATE INDEX IF NOT EXISTS idx_geo_media_entities_status ON geo_media_entities(reference_status);

CREATE TABLE IF NOT EXISTS geo_media_inventory_mappings (
    id BIGSERIAL PRIMARY KEY,
    entity_id BIGINT NOT NULL REFERENCES geo_media_entities(id) ON DELETE CASCADE,
    media_source VARCHAR(30) NOT NULL,
    inventory_id BIGINT NOT NULL,
    media_name TEXT,
    price_yuan NUMERIC(12,2),
    price_points BIGINT,
    inventory_status VARCHAR(40) DEFAULT 'active',
    match_method VARCHAR(40),
    match_confidence NUMERIC(5,4) DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (media_source, inventory_id)
);
CREATE INDEX IF NOT EXISTS idx_geo_media_inventory_entity ON geo_media_inventory_mappings(entity_id);
CREATE INDEX IF NOT EXISTS idx_geo_media_inventory_source ON geo_media_inventory_mappings(media_source, inventory_id);

CREATE TABLE IF NOT EXISTS geo_media_citation_rollups (
    id BIGSERIAL PRIMARY KEY,
    entity_key VARCHAR(80) NOT NULL,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    engine VARCHAR(40) NOT NULL DEFAULT 'all',
    signal_tier VARCHAR(40) NOT NULL,
    answer_credit NUMERIC(12,6) DEFAULT 0,
    answer_adopted_count INTEGER DEFAULT 0,
    cited_count INTEGER DEFAULT 0,
    search_only_count INTEGER DEFAULT 0,
    reference_only_count INTEGER DEFAULT 0,
    prompt_count INTEGER DEFAULT 0,
    source_count_total INTEGER DEFAULT 0,
    evidence JSONB DEFAULT '{}'::jsonb,
    version VARCHAR(80) NOT NULL,
    last_seen_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (entity_key, industry_key, engine, signal_tier, version)
);
CREATE INDEX IF NOT EXISTS idx_geo_media_rollups_entity ON geo_media_citation_rollups(entity_key, industry_key);
CREATE INDEX IF NOT EXISTS idx_geo_media_rollups_weight ON geo_media_citation_rollups(answer_credit DESC);

CREATE TABLE IF NOT EXISTS media_entity_score_snapshots (
    id BIGSERIAL PRIMARY KEY,
    entity_id BIGINT NOT NULL REFERENCES geo_media_entities(id) ON DELETE CASCADE,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    score_version VARCHAR(80) NOT NULL,
    shadow_score NUMERIC(6,2) NOT NULL,
    evidence_score NUMERIC(6,2),
    quality_score NUMERIC(6,2),
    inventory_score NUMERIC(6,2),
    outcome_score NUMERIC(6,2),
    reference_status VARCHAR(40) DEFAULT 'reference_only',
    is_purchasable BOOLEAN DEFAULT FALSE,
    reasons JSONB DEFAULT '[]'::jsonb,
    evidence JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (entity_id, industry_key, score_version)
);
CREATE INDEX IF NOT EXISTS idx_media_entity_scores_industry
    ON media_entity_score_snapshots(industry_key, shadow_score DESC);
CREATE INDEX IF NOT EXISTS idx_media_entity_scores_purchasable
    ON media_entity_score_snapshots(is_purchasable, shadow_score DESC);

CREATE TABLE IF NOT EXISTS geo_research_source_signals (
    id BIGSERIAL PRIMARY KEY,
    source_url TEXT NOT NULL,
    url_hash CHAR(40),
    domain VARCHAR(300),
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    engine VARCHAR(40) NOT NULL,
    prompt_id TEXT,
    signal_tier VARCHAR(40) NOT NULL,
    source_position INTEGER DEFAULT 0,
    total_sources_in_answer INTEGER DEFAULT 1,
    balanced_weight NUMERIC(12,6) NOT NULL DEFAULT 0,
    answer_mentioned_brand BOOLEAN DEFAULT FALSE,
    round_id VARCHAR(80),
    article_id BIGINT,
    metadata JSONB DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_url, industry_key, engine, prompt_id, signal_tier, round_id)
);
CREATE INDEX IF NOT EXISTS idx_geo_source_signals_industry
    ON geo_research_source_signals(industry_key, balanced_weight DESC);
CREATE INDEX IF NOT EXISTS idx_geo_source_signals_domain ON geo_research_source_signals(domain);
CREATE INDEX IF NOT EXISTS idx_geo_source_signals_tier ON geo_research_source_signals(signal_tier);

CREATE TABLE IF NOT EXISTS geo_source_quality_snapshots (
    id BIGSERIAL PRIMARY KEY,
    source_url TEXT NOT NULL,
    article_id BIGINT,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    quality_score NUMERIC(6,2),
    risk_tags JSONB DEFAULT '[]'::jsonb,
    usable_for_writing BOOLEAN DEFAULT FALSE,
    quality_components JSONB DEFAULT '{}'::jsonb,
    version VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_url, industry_key, version)
);
CREATE INDEX IF NOT EXISTS idx_geo_source_quality_industry
    ON geo_source_quality_snapshots(industry_key, quality_score DESC);

CREATE TABLE IF NOT EXISTS writing_style_signal_events (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT,
    source_url TEXT,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    signal_tier VARCHAR(40) NOT NULL,
    balanced_weight NUMERIC(12,6) DEFAULT 0,
    engine VARCHAR(40),
    prompt_id TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_writing_style_signal_industry
    ON writing_style_signal_events(industry_key, balanced_weight DESC);

CREATE TABLE IF NOT EXISTS writing_style_feature_snapshots (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT,
    source_url TEXT,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    style_family VARCHAR(60) NOT NULL,
    intent_type VARCHAR(60),
    content_type VARCHAR(60),
    features JSONB NOT NULL,
    feature_version VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (article_id, source_url, feature_version)
);
CREATE INDEX IF NOT EXISTS idx_writing_style_features_industry
    ON writing_style_feature_snapshots(industry_key, style_family);

CREATE TABLE IF NOT EXISTS writing_strategy_versions (
    id BIGSERIAL PRIMARY KEY,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    strategy_version VARCHAR(240) NOT NULL,
    status VARCHAR(40) NOT NULL DEFAULT 'shadow',
    style_family VARCHAR(60),
    guidance TEXT,
    common_elements JSONB DEFAULT '[]'::jsonb,
    evidence_score NUMERIC(12,4),
    outcome_score NUMERIC(12,4),
    confidence NUMERIC(5,4),
    guardrails JSONB DEFAULT '[]'::jsonb,
    source_summary JSONB DEFAULT '{}'::jsonb,
    reviewed_by BIGINT,
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    activated_at TIMESTAMPTZ,
    activated_by BIGINT,
    archived_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (industry_key, strategy_version)
);
ALTER TABLE writing_strategy_versions
    ADD COLUMN IF NOT EXISTS review_note TEXT;
ALTER TABLE writing_strategy_versions
    ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE writing_strategy_versions
    ADD COLUMN IF NOT EXISTS activated_by BIGINT;
ALTER TABLE writing_strategy_versions
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE writing_strategy_versions
    ALTER COLUMN strategy_version TYPE VARCHAR(240);
CREATE INDEX IF NOT EXISTS idx_writing_strategy_versions_status
    ON writing_strategy_versions(status, industry_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_writing_strategy_one_active_per_industry
    ON writing_strategy_versions(industry_key)
 WHERE status = 'active';

CREATE TABLE IF NOT EXISTS writing_strategy_audit_events (
    id BIGSERIAL PRIMARY KEY,
    strategy_id BIGINT REFERENCES writing_strategy_versions(id) ON DELETE SET NULL,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    action VARCHAR(40) NOT NULL,
    actor_id BIGINT,
    from_status VARCHAR(40),
    to_status VARCHAR(40),
    note TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_writing_strategy_audit_strategy
    ON writing_strategy_audit_events(strategy_id, created_at DESC);

CREATE TABLE IF NOT EXISTS writing_strategy_assignments (
    id BIGSERIAL PRIMARY KEY,
    strategy_id BIGINT NOT NULL REFERENCES writing_strategy_versions(id) ON DELETE CASCADE,
    brand_id BIGINT,
    quote_id BIGINT,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    assignment_status VARCHAR(40) DEFAULT 'shadow',
    assigned_by BIGINT,
    assigned_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_writing_strategy_assignments_brand
    ON writing_strategy_assignments(brand_id, quote_id);

CREATE TABLE IF NOT EXISTS writing_strategy_outcome_events (
    id BIGSERIAL PRIMARY KEY,
    strategy_id BIGINT REFERENCES writing_strategy_versions(id) ON DELETE SET NULL,
    article_id BIGINT,
    brand_id BIGINT,
    quote_id BIGINT,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    publish_status VARCHAR(60),
    ai_citations_delta_30d INTEGER DEFAULT 0,
    monitoring_brand_score_delta_30d NUMERIC(8,2) DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE writing_strategy_outcome_events
    ADD COLUMN IF NOT EXISTS industry_key VARCHAR(100) NOT NULL DEFAULT 'general';
CREATE INDEX IF NOT EXISTS idx_writing_strategy_outcomes_strategy
    ON writing_strategy_outcome_events(strategy_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_writing_strategy_outcomes_industry
    ON writing_strategy_outcome_events(industry_key, observed_at DESC);

CREATE TABLE IF NOT EXISTS geo_recommendation_predictions (
    id BIGSERIAL PRIMARY KEY,
    scope_key VARCHAR(200) NOT NULL,
    prediction_type VARCHAR(60) NOT NULL,
    predicted_payload JSONB NOT NULL,
    version VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS geo_recommendation_outcomes (
    id BIGSERIAL PRIMARY KEY,
    prediction_id BIGINT REFERENCES geo_recommendation_predictions(id) ON DELETE SET NULL,
    scope_key VARCHAR(200) NOT NULL,
    outcome_payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ DEFAULT NOW()
);
