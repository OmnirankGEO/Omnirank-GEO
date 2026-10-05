-- ============================================================================
-- GEO 统一观测飞轮 vNext · AI-2 治理账本 additive migration (contract v1.4)
-- 单份 migration(R10):events / signals / contributor_buckets / audit / policy / aggregates
-- 幂等双通道:CREATE TABLE IF NOT EXISTS 内联全列+命名约束;已有(半成品)表走 ADD COLUMN IF NOT EXISTS
--   + 命名 CHECK 补块 + partial unique index。2× 复跑零报错。prestart 单飞;web/cron 只读 readiness。
-- 红线:仅建表/建约束(additive · 零 billing 触碰 · 无跨包强 FK)。晋升开关默认 false(R11)。
-- 末尾"可执行反查断言"(FF · 非注释):列/类型/NOT NULL/命名 CHECK 全枚举定义/partial predicate/
--   convalidated/唯一门/scope 隐私约束 任一不满足→RAISE→迁移非零退出。
-- ============================================================================

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 1) geo_observation_events — 来源登记与处理状态(不存公共可读原始答案)      ║
-- ╚══════════════════════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS geo_observation_events (
    id                     BIGSERIAL PRIMARY KEY,
    event_uuid             UUID NOT NULL,
    schema_version         VARCHAR(40) NOT NULL DEFAULT 'geo-observation-v1',
    source_type            TEXT NOT NULL
        CONSTRAINT chk_geo_obs_event_source_type CHECK (source_type IN ('research_round','paid_diagnosis','recurring_monitoring')),
    source_table           TEXT NOT NULL,
    source_record_id       TEXT NOT NULL,
    source_subkey          TEXT NOT NULL,
    source_event_key       CHAR(64) NOT NULL,
    owner_user_id          BIGINT,
    brand_id               BIGINT,
    industry_key           TEXT,
    prompt_fingerprint     CHAR(64),
    platform_key           TEXT NOT NULL,
    provider_key           TEXT NOT NULL,
    model_key              TEXT NOT NULL,
    model_revision         TEXT,
    search_provider        TEXT,
    surface_key            TEXT NOT NULL
        CONSTRAINT chk_geo_obs_event_surface_key CHECK (surface_key IN (
            'doubao_ark_api_search','qwen_dashscope_search','deepseek_native_no_search',
            'deepseek_native_with_search','deepseek_metaso_proxy','deepseek_dashscope_search_legacy',
            'yuanbao_app_verified','tencent_wsa_search','yuanbao_hy3_tokenhub','manual_app_capture','other_explicit')),
    search_mode            TEXT,
    search_enabled         BOOLEAN,
    search_query_count     INTEGER
        CONSTRAINT chk_geo_obs_event_sqc CHECK (search_query_count IS NULL OR search_query_count >= 0),
    country_code           CHAR(2) NOT NULL DEFAULT 'CN',
    region_key             TEXT,
    session_mode           TEXT NOT NULL
        CONSTRAINT chk_geo_obs_event_session_mode CHECK (session_mode IN ('clean','anonymous','accounted','unknown')),
    run_index              SMALLINT NOT NULL DEFAULT 1,
    answer_hash            CHAR(64),
    source_terminal_state  TEXT,
    processing_state       TEXT NOT NULL DEFAULT 'pending'
        CONSTRAINT chk_geo_obs_event_processing_state CHECK (processing_state IN (
            'pending','processing','pending_review','promoted','private_only','rejected','withdrawn','error')),
    rejection_codes        JSONB NOT NULL DEFAULT '[]'::jsonb,
    consent_policy_version TEXT,
    processing_purpose     TEXT NOT NULL DEFAULT 'geo_aggregate_learning',
    promotion_legal_basis  TEXT,
    retention_until        TIMESTAMPTZ,
    legal_hold             BOOLEAN NOT NULL DEFAULT FALSE,
    withdrawn_at           TIMESTAMPTZ,
    attempts               SMALLINT NOT NULL DEFAULT 0,
    lease_token            UUID,
    lease_until            TIMESTAMPTZ,
    observed_at            TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 已有(半成品)表升级双通道(新装均 no-op)
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS event_uuid             UUID;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS schema_version         VARCHAR(40) NOT NULL DEFAULT 'geo-observation-v1';
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS source_type            TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS source_table           TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS source_record_id       TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS source_subkey          TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS source_event_key       CHAR(64);
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS owner_user_id          BIGINT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS brand_id               BIGINT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS industry_key           TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS prompt_fingerprint     CHAR(64);
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS platform_key           TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS provider_key           TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS model_key              TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS model_revision         TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS search_provider        TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS surface_key            TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS search_mode            TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS search_enabled         BOOLEAN;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS search_query_count     INTEGER;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS country_code           CHAR(2) NOT NULL DEFAULT 'CN';
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS region_key             TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS session_mode           TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS run_index              SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS answer_hash            CHAR(64);
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS source_terminal_state  TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS processing_state       TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS rejection_codes        JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS consent_policy_version TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS processing_purpose     TEXT NOT NULL DEFAULT 'geo_aggregate_learning';
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS promotion_legal_basis  TEXT;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS retention_until        TIMESTAMPTZ;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS legal_hold             BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS withdrawn_at           TIMESTAMPTZ;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS attempts               SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS lease_token            UUID;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS lease_until            TIMESTAMPTZ;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS observed_at            TIMESTAMPTZ;
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- 命名 CHECK 补块(已有表升级 · 新装内联故 IF NOT EXISTS 跳过)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_event_source_type' AND conrelid='geo_observation_events'::regclass) THEN
        ALTER TABLE geo_observation_events ADD CONSTRAINT chk_geo_obs_event_source_type CHECK (source_type IN ('research_round','paid_diagnosis','recurring_monitoring'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_event_surface_key' AND conrelid='geo_observation_events'::regclass) THEN
        ALTER TABLE geo_observation_events ADD CONSTRAINT chk_geo_obs_event_surface_key CHECK (surface_key IN (
            'doubao_ark_api_search','qwen_dashscope_search','deepseek_native_no_search','deepseek_native_with_search',
            'deepseek_metaso_proxy','deepseek_dashscope_search_legacy','yuanbao_app_verified','tencent_wsa_search',
            'yuanbao_hy3_tokenhub','manual_app_capture','other_explicit'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_event_session_mode' AND conrelid='geo_observation_events'::regclass) THEN
        ALTER TABLE geo_observation_events ADD CONSTRAINT chk_geo_obs_event_session_mode CHECK (session_mode IN ('clean','anonymous','accounted','unknown'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_event_processing_state' AND conrelid='geo_observation_events'::regclass) THEN
        ALTER TABLE geo_observation_events ADD CONSTRAINT chk_geo_obs_event_processing_state CHECK (processing_state IN (
            'pending','processing','pending_review','promoted','private_only','rejected','withdrawn','error'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_event_sqc' AND conrelid='geo_observation_events'::regclass) THEN
        ALTER TABLE geo_observation_events ADD CONSTRAINT chk_geo_obs_event_sqc CHECK (search_query_count IS NULL OR search_query_count >= 0);
    END IF;
END $$;

-- 唯一门:业务键(source 四元组) + source_event_key(SHA-256 幂等门) + event_uuid
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_obs_event_business ON geo_observation_events (source_type, source_table, source_record_id, source_subkey);
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_obs_event_key ON geo_observation_events (source_event_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_obs_event_uuid ON geo_observation_events (event_uuid);
-- claim 索引(可领取态) + lease 恢复索引
CREATE INDEX IF NOT EXISTS idx_geo_obs_event_claimable ON geo_observation_events (processing_state, created_at)
    WHERE processing_state IN ('pending','processing');
CREATE INDEX IF NOT EXISTS idx_geo_obs_event_lease ON geo_observation_events (lease_until)
    WHERE lease_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_geo_obs_event_owner_brand ON geo_observation_events (owner_user_id, brand_id);

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 2) geo_observation_signals — 匿名结构化事实(禁 owner/brand/原文/自定义问题)║
-- ╚══════════════════════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS geo_observation_signals (
    signal_id               BIGSERIAL PRIMARY KEY,
    event_id                BIGINT NOT NULL
        CONSTRAINT fk_geo_obs_signal_event REFERENCES geo_observation_events(id),
    industry_key            TEXT NOT NULL,
    prompt_family_key       TEXT NOT NULL,
    prompt_intent           TEXT NOT NULL
        CONSTRAINT chk_geo_obs_signal_intent CHECK (prompt_intent IN (
            'awareness','category_recommendation','comparison','evaluation','transaction','risk','branded','other')),
    is_branded_prompt       BOOLEAN NOT NULL,
    platform_key            TEXT NOT NULL,
    provider_key            TEXT NOT NULL,
    model_key               TEXT NOT NULL,
    model_revision          TEXT,
    surface_key             TEXT NOT NULL
        CONSTRAINT chk_geo_obs_signal_surface CHECK (surface_key IN (
            'doubao_ark_api_search','qwen_dashscope_search','deepseek_native_no_search','deepseek_native_with_search',
            'deepseek_metaso_proxy','deepseek_dashscope_search_legacy','yuanbao_app_verified','tencent_wsa_search',
            'yuanbao_hy3_tokenhub','manual_app_capture','other_explicit')),
    search_provider         TEXT,
    response_status         TEXT NOT NULL
        CONSTRAINT chk_geo_obs_signal_response CHECK (response_status IN ('answered','refused','timeout','error','unknown','budget_blocked')),
    target_outcome          TEXT NOT NULL
        CONSTRAINT chk_geo_obs_signal_outcome CHECK (target_outcome IN (
            'recommended','conditionally_recommended','candidate_only','mentioned_only','criteria_only',
            'refused_no_evidence','refused_risk','not_mentioned','entity_ambiguous','engine_error')),
    target_position         INTEGER
        CONSTRAINT chk_geo_obs_signal_position CHECK (target_position IS NULL OR target_position > 0),
    sentiment               TEXT NOT NULL
        CONSTRAINT chk_geo_obs_signal_sentiment CHECK (sentiment IN ('positive','neutral','negative','mixed','unknown')),
    competitor_count        INTEGER NOT NULL DEFAULT 0
        CONSTRAINT chk_geo_obs_signal_competitor CHECK (competitor_count >= 0),
    source_domains          JSONB NOT NULL DEFAULT '[]'::jsonb,
    citation_count          INTEGER NOT NULL DEFAULT 0
        CONSTRAINT chk_geo_obs_signal_citation CHECK (citation_count >= 0),
    source_count            INTEGER NOT NULL DEFAULT 0
        CONSTRAINT chk_geo_obs_signal_sourcecount CHECK (source_count >= 0),
    search_query_count      INTEGER
        CONSTRAINT chk_geo_obs_signal_sqc CHECK (search_query_count IS NULL OR search_query_count >= 0),
    search_query_theme_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    quality_score_bps       INTEGER NOT NULL
        CONSTRAINT chk_geo_obs_signal_quality_bps CHECK (quality_score_bps BETWEEN 0 AND 10000),
    base_weight_bps         INTEGER NOT NULL
        CONSTRAINT chk_geo_obs_signal_base_bps CHECK (base_weight_bps BETWEEN 0 AND 10000),
    effective_weight_bps    INTEGER NOT NULL
        CONSTRAINT chk_geo_obs_signal_eff_bps CHECK (effective_weight_bps BETWEEN 0 AND 10000),
    confidence_bps          INTEGER NOT NULL
        CONSTRAINT chk_geo_obs_signal_conf_bps CHECK (confidence_bps BETWEEN 0 AND 10000),
    observed_at             TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE geo_observation_signals ADD COLUMN IF NOT EXISTS model_revision TEXT;
ALTER TABLE geo_observation_signals ADD COLUMN IF NOT EXISTS search_provider TEXT;
ALTER TABLE geo_observation_signals ADD COLUMN IF NOT EXISTS search_query_count INTEGER;
-- 一事件最多一条规范晋升信号
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_obs_signal_event ON geo_observation_signals (event_id);
CREATE INDEX IF NOT EXISTS idx_geo_obs_signal_industry ON geo_observation_signals (industry_key, prompt_family_key, platform_key);

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 3) geo_observation_contributor_buckets — 受限 HMAC 桶(防刷/k匿名/撤回/审计)║
-- ║    唯一门(R6/Q3):user+brand+family+platform+date(不含 source_type)一票   ║
-- ╚══════════════════════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS geo_observation_contributor_buckets (
    id                        BIGSERIAL PRIMARY KEY,
    event_id                  BIGINT NOT NULL
        CONSTRAINT fk_geo_obs_bucket_event REFERENCES geo_observation_events(id),
    contributor_user_bucket   TEXT NOT NULL,
    contributor_brand_bucket  TEXT NOT NULL,
    bucket_key_version        INTEGER NOT NULL DEFAULT 1,
    contribution_date         DATE NOT NULL,
    prompt_family_key         TEXT NOT NULL,
    platform_key              TEXT NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE geo_observation_contributor_buckets ADD COLUMN IF NOT EXISTS bucket_key_version INTEGER NOT NULL DEFAULT 1;
-- 一事件最多一桶
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_obs_bucket_event ON geo_observation_contributor_buckets (event_id);
-- ★ 公共投票唯一门(R6 · 不含 source_type):同 user/brand/题族/平台/自然日最多一票
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_obs_contributor_vote ON geo_observation_contributor_buckets (
    contributor_user_bucket, contributor_brand_bucket, prompt_family_key, platform_key, contribution_date);

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 4) geo_observation_audit — 每次 claim/核验/脱敏/拒绝/裁决/晋升/撤销留痕     ║
-- ║    event_id 可空(policy/系统级审计);audit_event_key 唯一(禁 request_id 单列)║
-- ╚══════════════════════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS geo_observation_audit (
    id               BIGSERIAL PRIMARY KEY,
    event_id         BIGINT
        CONSTRAINT fk_geo_obs_audit_event REFERENCES geo_observation_events(id),
    action           TEXT NOT NULL,
    operator_type    TEXT NOT NULL
        CONSTRAINT chk_geo_obs_audit_operator CHECK (operator_type IN ('system','admin')),
    operator_id      TEXT,
    before_json      JSONB,
    after_json       JSONB,
    reason_codes     JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_json    JSONB,
    request_id       TEXT,
    audit_event_key  CHAR(64) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_obs_audit_key ON geo_observation_audit (audit_event_key);
CREATE INDEX IF NOT EXISTS idx_geo_obs_audit_event ON geo_observation_audit (event_id, created_at DESC);

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 5) geo_observation_policy — 业务策略配置 SSOT(单行 · CAS · 晋升开关默认 false)║
-- ╚══════════════════════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS geo_observation_policy (
    singleton_id             SMALLINT PRIMARY KEY DEFAULT 1
        CONSTRAINT chk_geo_obs_policy_singleton CHECK (singleton_id = 1),
    policy_version           INTEGER NOT NULL DEFAULT 1
        CONSTRAINT chk_geo_obs_policy_version CHECK (policy_version >= 1),
    policy_json              JSONB NOT NULL,
    -- 晋升治理 SSOT(P1-1/P1-4):法务依据/同意版本/金标准门都在 DB,worker 每次读新鲜值 + CAS,关闸立即生效
    promotion_legal_basis    TEXT,                                 -- 法务批准的依据码;NULL=未批→一律 private_only
    consent_policy_version   TEXT,                                 -- 适用的同意/告知版本
    outcome_gold_gate_passed BOOLEAN NOT NULL DEFAULT FALSE,       -- outcome 金标准(macro-F1)是否已验证;false→confirmed_mention 只入 pending_review
    gold_dataset_version     TEXT,                                 -- 金标准数据集版本
    gold_macro_f1_bps        INTEGER,                              -- 记录达门的 macro-F1(0..10000)
    updated_by               TEXT,
    updated_reason           TEXT,
    last_request_id          TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE geo_observation_policy ADD COLUMN IF NOT EXISTS promotion_legal_basis    TEXT;
ALTER TABLE geo_observation_policy ADD COLUMN IF NOT EXISTS consent_policy_version   TEXT;
ALTER TABLE geo_observation_policy ADD COLUMN IF NOT EXISTS outcome_gold_gate_passed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE geo_observation_policy ADD COLUMN IF NOT EXISTS gold_dataset_version     TEXT;
ALTER TABLE geo_observation_policy ADD COLUMN IF NOT EXISTS gold_macro_f1_bps        INTEGER;
-- 安全默认策略(R11:全部 feature_flags=false;权重取 SPEC §8.3 默认;仅 shadow,不接管生产)
INSERT INTO geo_observation_policy (singleton_id, policy_version, policy_json, updated_by, updated_reason)
VALUES (1, 1, '{
  "policy_version": "v1-migration-default",
  "platforms": [
    {"platform_key":"doubao","enabled":true,"base_weight_bps":2500,"surface_key":"doubao_ark_api_search","legacy_read_only":false},
    {"platform_key":"qwen","enabled":true,"base_weight_bps":2500,"surface_key":"qwen_dashscope_search","legacy_read_only":false},
    {"platform_key":"deepseek","enabled":true,"base_weight_bps":2500,"surface_key":"deepseek_dashscope_search_legacy","legacy_read_only":false},
    {"platform_key":"yuanbao","enabled":true,"base_weight_bps":2500,"surface_key":"yuanbao_hy3_tokenhub","legacy_read_only":false},
    {"platform_key":"kimi","enabled":false,"base_weight_bps":0,"surface_key":null,"legacy_read_only":true}
  ],
  "source_base_weights_bps": {"research": 10000, "paid_diagnosis": 4000, "monitoring": 7000},
  "sampling_budget": {"max_calls_per_round": 5000, "max_calls_per_day": 20000, "max_cost_micros_per_day": 100000000, "max_retry_calls_per_request": 2},
  "max_single_brand_share_bps": 1000,
  "public_min_independent_brands": 3,
  "public_min_source_types": 2,
  "retention_days": 365,
  "anomaly_confirmation_numerator": 2,
  "anomaly_confirmation_denominator": 3,
  "feature_flags": {"ingest_enabled": false, "promotion_enabled": false, "aggregation_enabled": false, "product_enabled": false}
}'::jsonb, 'migration', 'safe default: all promotion switches false pending legal + admin CAS (R11)')
ON CONFLICT (singleton_id) DO NOTHING;

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 6) geo_observation_aggregates — 影子聚合(AI-2 只建表/readiness;AI-3 写公式)║
-- ║    字段/约束/索引 = 机器契约 aggregate_schema 冻结                          ║
-- ╚══════════════════════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS geo_observation_aggregates (
    aggregate_id                         BIGSERIAL PRIMARY KEY,
    aggregate_key                        CHAR(64) NOT NULL,
    contract_version                     TEXT NOT NULL,
    aggregation_version                  TEXT NOT NULL,
    metric_version                       TEXT NOT NULL,
    policy_version                       TEXT NOT NULL,
    scope_type                           TEXT NOT NULL
        CONSTRAINT chk_geo_obs_agg_scope CHECK (scope_type IN ('private_brand','public_industry','admin_shadow')),
    owner_user_id                        BIGINT,
    brand_id                             BIGINT,
    industry_key                         TEXT NOT NULL,
    bucket_granularity                   TEXT NOT NULL
        CONSTRAINT chk_geo_obs_agg_granularity CHECK (bucket_granularity IN ('day','week','month')),
    bucket_start                         DATE NOT NULL,
    bucket_end                           DATE NOT NULL,
    prompt_family_key                    TEXT,
    prompt_intent                        TEXT,
    is_branded_prompt                    BOOLEAN,
    platform_key                         TEXT,
    surface_key                          TEXT,
    model_revision                       TEXT,
    search_enabled                       BOOLEAN,
    source_type                          TEXT
        CONSTRAINT chk_geo_obs_agg_source_type CHECK (source_type IS NULL OR source_type IN ('research_round','paid_diagnosis','recurring_monitoring')),
    search_query_theme                   TEXT,
    valid_observations                   BIGINT NOT NULL DEFAULT 0,
    independent_user_buckets             BIGINT NOT NULL DEFAULT 0,
    independent_brand_buckets            BIGINT NOT NULL DEFAULT 0,
    independent_source_types             BIGINT NOT NULL DEFAULT 0,
    weighted_denominator_micros          BIGINT NOT NULL DEFAULT 0,
    recommended_count                    BIGINT NOT NULL DEFAULT 0,
    conditionally_recommended_count      BIGINT NOT NULL DEFAULT 0,
    candidate_only_count                 BIGINT NOT NULL DEFAULT 0,
    mentioned_only_count                 BIGINT NOT NULL DEFAULT 0,
    criteria_only_count                  BIGINT NOT NULL DEFAULT 0,
    refused_no_evidence_count            BIGINT NOT NULL DEFAULT 0,
    refused_risk_count                   BIGINT NOT NULL DEFAULT 0,
    not_mentioned_count                  BIGINT NOT NULL DEFAULT 0,
    presence_rate_bps                    INTEGER NOT NULL DEFAULT 0,
    explicit_recommendation_rate_bps     INTEGER NOT NULL DEFAULT 0,
    conditional_recommendation_rate_bps  INTEGER NOT NULL DEFAULT 0,
    candidate_rate_bps                   INTEGER NOT NULL DEFAULT 0,
    mentioned_only_rate_bps              INTEGER NOT NULL DEFAULT 0,
    criteria_only_rate_bps               INTEGER NOT NULL DEFAULT 0,
    refusal_no_evidence_rate_bps         INTEGER NOT NULL DEFAULT 0,
    refusal_risk_rate_bps                INTEGER NOT NULL DEFAULT 0,
    not_mentioned_rate_bps               INTEGER NOT NULL DEFAULT 0,
    refusal_rate_bps                     INTEGER NOT NULL DEFAULT 0,
    citation_rate_bps                    INTEGER NOT NULL DEFAULT 0,
    source_visibility_rate_bps           INTEGER NOT NULL DEFAULT 0,
    evidence_coverage_rate_bps           INTEGER NOT NULL DEFAULT 0,
    rank_top1_count                      BIGINT NOT NULL DEFAULT 0,
    rank_top3_count                      BIGINT NOT NULL DEFAULT 0,
    rank_top5_count                      BIGINT NOT NULL DEFAULT 0,
    rank_not_listed_count                BIGINT NOT NULL DEFAULT 0,
    avg_position_milli                   BIGINT,
    share_of_voice_bps                   INTEGER,
    volatility_bps                       INTEGER NOT NULL DEFAULT 0,
    confirmed_change                     BOOLEAN NOT NULL DEFAULT FALSE,
    trend_confidence_bps                 INTEGER NOT NULL DEFAULT 0,
    model_shift_index_bps                INTEGER NOT NULL DEFAULT 0,
    stability_status                     TEXT NOT NULL DEFAULT 'insufficient'
        CONSTRAINT chk_geo_obs_agg_stability CHECK (stability_status IN ('stable','watch','insufficient','shifted')),
    intrinsic_memory_rate_bps            INTEGER,
    retrieval_selection_rate_bps         INTEGER,
    input_watermark                      TIMESTAMPTZ NOT NULL,
    computed_at                          TIMESTAMPTZ NOT NULL,
    created_at                           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- scope 隐私约束(契约 table_checks)
    CONSTRAINT chk_geo_obs_agg_private_scope CHECK (
        scope_type <> 'private_brand' OR (owner_user_id IS NOT NULL AND brand_id IS NOT NULL)),
    CONSTRAINT chk_geo_obs_agg_public_scope CHECK (
        scope_type <> 'public_industry' OR (owner_user_id IS NULL AND brand_id IS NULL)),
    CONSTRAINT chk_geo_obs_agg_bucket_order CHECK (bucket_end >= bucket_start),
    -- 非负 count/denominator
    CONSTRAINT chk_geo_obs_agg_counts_nonneg CHECK (
        valid_observations >= 0 AND independent_user_buckets >= 0 AND independent_brand_buckets >= 0
        AND independent_source_types >= 0 AND weighted_denominator_micros >= 0
        AND recommended_count >= 0 AND conditionally_recommended_count >= 0 AND candidate_only_count >= 0
        AND mentioned_only_count >= 0 AND criteria_only_count >= 0 AND refused_no_evidence_count >= 0
        AND refused_risk_count >= 0 AND not_mentioned_count >= 0
        AND rank_top1_count >= 0 AND rank_top3_count >= 0 AND rank_top5_count >= 0 AND rank_not_listed_count >= 0
        AND (avg_position_milli IS NULL OR avg_position_milli >= 0)),
    -- bps 0..10000(含可空)
    CONSTRAINT chk_geo_obs_agg_bps_range CHECK (
        presence_rate_bps BETWEEN 0 AND 10000 AND explicit_recommendation_rate_bps BETWEEN 0 AND 10000
        AND conditional_recommendation_rate_bps BETWEEN 0 AND 10000 AND candidate_rate_bps BETWEEN 0 AND 10000
        AND mentioned_only_rate_bps BETWEEN 0 AND 10000 AND criteria_only_rate_bps BETWEEN 0 AND 10000
        AND refusal_no_evidence_rate_bps BETWEEN 0 AND 10000 AND refusal_risk_rate_bps BETWEEN 0 AND 10000
        AND not_mentioned_rate_bps BETWEEN 0 AND 10000 AND refusal_rate_bps BETWEEN 0 AND 10000
        AND citation_rate_bps BETWEEN 0 AND 10000 AND source_visibility_rate_bps BETWEEN 0 AND 10000
        AND evidence_coverage_rate_bps BETWEEN 0 AND 10000 AND volatility_bps BETWEEN 0 AND 10000
        AND trend_confidence_bps BETWEEN 0 AND 10000 AND model_shift_index_bps BETWEEN 0 AND 10000
        AND (share_of_voice_bps IS NULL OR share_of_voice_bps BETWEEN 0 AND 10000)
        AND (intrinsic_memory_rate_bps IS NULL OR intrinsic_memory_rate_bps BETWEEN 0 AND 10000)
        AND (retrieval_selection_rate_bps IS NULL OR retrieval_selection_rate_bps BETWEEN 0 AND 10000))
);
-- 契约 required_indexes
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_obs_agg_key ON geo_observation_aggregates (aggregate_key);
CREATE INDEX IF NOT EXISTS idx_geo_obs_agg_scope_bucket ON geo_observation_aggregates (scope_type, bucket_granularity, bucket_start);
CREATE INDEX IF NOT EXISTS idx_geo_obs_agg_private ON geo_observation_aggregates (owner_user_id, brand_id, bucket_start)
    WHERE scope_type = 'private_brand';
CREATE INDEX IF NOT EXISTS idx_geo_obs_agg_public ON geo_observation_aggregates (industry_key, bucket_granularity, bucket_start)
    WHERE scope_type = 'public_industry';

-- [审修] 半成品升级路径的 ADD COLUMN IF NOT EXISTS 不带 NOT NULL → 若 events 表为空则 SET NOT NULL 自愈,
--   使升级后 schema 与 fresh 契约一致(表非空的异常漂移由 readiness NOT NULL 反查 fail-closed 兜住)。
DO $$
DECLARE
    c text;
    core text[] := ARRAY['event_uuid','source_type','source_table','source_record_id','source_subkey',
        'source_event_key','platform_key','provider_key','model_key','surface_key','session_mode','observed_at'];
BEGIN
    IF to_regclass('geo_observation_events') IS NULL THEN RETURN; END IF;
    IF (SELECT count(*) FROM geo_observation_events) = 0 THEN
        FOREACH c IN ARRAY core LOOP
            IF EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='geo_observation_events' AND column_name=c AND is_nullable='YES') THEN
                EXECUTE format('ALTER TABLE geo_observation_events ALTER COLUMN %I SET NOT NULL', c);
            END IF;
        END LOOP;
    END IF;
END $$;

-- 迁移 marker(仅审计追溯 · 非 skip 依据)
CREATE TABLE IF NOT EXISTS _migration_markers (marker TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, note TEXT);
INSERT INTO _migration_markers (marker, note) VALUES ('geo_observation_v1_2026_07_17', 'AI-2 governance ledger additive migration')
    ON CONFLICT (marker) DO NOTHING;

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 可执行反查断言(FF · 失败即 RAISE → 迁移/prestart 非零退出)                ║
-- ╚══════════════════════════════════════════════════════════════════════════╝
DO $$
DECLARE
    v text;
    ckdef text;
    idxdef text;
    surfaces text[] := ARRAY['doubao_ark_api_search','qwen_dashscope_search','deepseek_native_no_search',
        'deepseek_native_with_search','deepseek_metaso_proxy','deepseek_dashscope_search_legacy',
        'yuanbao_app_verified','tencent_wsa_search','yuanbao_hy3_tokenhub','manual_app_capture','other_explicit'];
    outcomes text[] := ARRAY['recommended','conditionally_recommended','candidate_only','mentioned_only','criteria_only',
        'refused_no_evidence','refused_risk','not_mentioned','entity_ambiguous','engine_error'];
    proc_states text[] := ARRAY['pending','processing','pending_review','promoted','private_only','rejected','withdrawn','error'];
    src_types text[] := ARRAY['research_round','paid_diagnosis','recurring_monitoring'];
    resp_states text[] := ARRAY['answered','refused','timeout','error','unknown','budget_blocked'];
    intents text[] := ARRAY['awareness','category_recommendation','comparison','evaluation','transaction','risk','branded','other'];
    scopes text[] := ARRAY['private_brand','public_industry','admin_shadow'];
    stabilities text[] := ARRAY['stable','watch','insufficient','shifted'];
BEGIN
    -- 0) 六表存在
    IF to_regclass('geo_observation_events') IS NULL THEN RAISE EXCEPTION '[反查] geo_observation_events 缺失'; END IF;
    IF to_regclass('geo_observation_signals') IS NULL THEN RAISE EXCEPTION '[反查] geo_observation_signals 缺失'; END IF;
    IF to_regclass('geo_observation_contributor_buckets') IS NULL THEN RAISE EXCEPTION '[反查] contributor_buckets 缺失'; END IF;
    IF to_regclass('geo_observation_audit') IS NULL THEN RAISE EXCEPTION '[反查] geo_observation_audit 缺失'; END IF;
    IF to_regclass('geo_observation_policy') IS NULL THEN RAISE EXCEPTION '[反查] geo_observation_policy 缺失'; END IF;
    IF to_regclass('geo_observation_aggregates') IS NULL THEN RAISE EXCEPTION '[反查] geo_observation_aggregates 缺失'; END IF;

    -- 1) events 关键列/类型/NOT NULL
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='source_event_key' AND data_type='character' AND character_maximum_length=64) THEN
        RAISE EXCEPTION '[反查] events.source_event_key 非 char(64)'; END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='event_uuid' AND data_type='uuid') THEN
        RAISE EXCEPTION '[反查] events.event_uuid 非 uuid'; END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='rejection_codes' AND data_type='jsonb') THEN
        RAISE EXCEPTION '[反查] events.rejection_codes 非 jsonb'; END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='observed_at' AND data_type='timestamp with time zone') THEN
        RAISE EXCEPTION '[反查] events.observed_at 非 timestamptz'; END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='processing_purpose' AND is_nullable='YES') THEN
        RAISE EXCEPTION '[反查] events.processing_purpose 应 NOT NULL'; END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='legal_hold' AND is_nullable='YES') THEN
        RAISE EXCEPTION '[反查] events.legal_hold 应 NOT NULL'; END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND is_nullable='YES'
               AND column_name IN ('event_uuid','source_type','source_table','source_record_id','source_subkey',
                   'source_event_key','platform_key','provider_key','model_key','surface_key','session_mode','observed_at')) THEN
        RAISE EXCEPTION '[反查] events 核心列存在可空(半成品升级漂移未自愈,含数据无法自动收紧,人工订正)';
    END IF;

    -- 2) events 四命名 CHECK 含全枚举
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_event_surface_key' AND conrelid='geo_observation_events'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_geo_obs_event_surface_key 缺失'; END IF;
    FOREACH v IN ARRAY surfaces LOOP IF strpos(ckdef, quote_literal(v))=0 THEN RAISE EXCEPTION '[反查] event surface CHECK 缺 %:%', v, ckdef; END IF; END LOOP;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_event_processing_state' AND conrelid='geo_observation_events'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_geo_obs_event_processing_state 缺失'; END IF;
    FOREACH v IN ARRAY proc_states LOOP IF strpos(ckdef, quote_literal(v))=0 THEN RAISE EXCEPTION '[反查] event processing_state CHECK 缺 %:%', v, ckdef; END IF; END LOOP;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_event_source_type' AND conrelid='geo_observation_events'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_geo_obs_event_source_type 缺失'; END IF;
    FOREACH v IN ARRAY src_types LOOP IF strpos(ckdef, quote_literal(v))=0 THEN RAISE EXCEPTION '[反查] event source_type CHECK 缺 %:%', v, ckdef; END IF; END LOOP;

    -- 3) events 唯一门(business 四元组 + source_event_key + uuid)
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('uq_geo_obs_event_business'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%source_type%' OR idxdef NOT ILIKE '%source_table%'
       OR idxdef NOT ILIKE '%source_record_id%' OR idxdef NOT ILIKE '%source_subkey%' THEN
        RAISE EXCEPTION '[反查] uq_geo_obs_event_business 缺失/列不符:%', idxdef; END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('uq_geo_obs_event_key'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%source_event_key%' THEN
        RAISE EXCEPTION '[反查] uq_geo_obs_event_key 缺失:%', idxdef; END IF;
    -- claimable partial index predicate
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('idx_geo_obs_event_claimable'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%WHERE%' OR idxdef NOT ILIKE '%pending%' OR idxdef NOT ILIKE '%processing%' THEN
        RAISE EXCEPTION '[反查] idx_geo_obs_event_claimable partial predicate 不符:%', idxdef; END IF;

    -- 4) signals 命名 CHECK 全枚举 + FK convalidated + 一事件一信号唯一
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_signal_outcome' AND conrelid='geo_observation_signals'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_geo_obs_signal_outcome 缺失'; END IF;
    FOREACH v IN ARRAY outcomes LOOP IF strpos(ckdef, quote_literal(v))=0 THEN RAISE EXCEPTION '[反查] signal outcome CHECK 缺 %:%', v, ckdef; END IF; END LOOP;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_signal_response' AND conrelid='geo_observation_signals'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_geo_obs_signal_response 缺失'; END IF;
    FOREACH v IN ARRAY resp_states LOOP IF strpos(ckdef, quote_literal(v))=0 THEN RAISE EXCEPTION '[反查] signal response CHECK 缺 %:%', v, ckdef; END IF; END LOOP;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_signal_intent' AND conrelid='geo_observation_signals'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_geo_obs_signal_intent 缺失'; END IF;
    FOREACH v IN ARRAY intents LOOP IF strpos(ckdef, quote_literal(v))=0 THEN RAISE EXCEPTION '[反查] signal intent CHECK 缺 %:%', v, ckdef; END IF; END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_obs_signal_event' AND conrelid='geo_observation_signals'::regclass
                   AND contype='f' AND confrelid='geo_observation_events'::regclass AND convalidated) THEN
        RAISE EXCEPTION '[反查] fk_geo_obs_signal_event 缺失/未引用 events/未 VALIDATE'; END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('uq_geo_obs_signal_event'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%event_id%' THEN
        RAISE EXCEPTION '[反查] uq_geo_obs_signal_event 缺失/非唯一:%', idxdef; END IF;
    -- signals 绝不含 owner/brand 列(隐私硬门)
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_signals' AND column_name IN ('owner_user_id','brand_id')) THEN
        RAISE EXCEPTION '[反查] geo_observation_signals 违规含 owner_user_id/brand_id 列(公共层禁)'; END IF;

    -- 5) contributor_buckets 公共投票唯一门(R6:5 列 · 不含 source_type)
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('uq_geo_obs_contributor_vote'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%'
       OR idxdef NOT ILIKE '%contributor_user_bucket%' OR idxdef NOT ILIKE '%contributor_brand_bucket%'
       OR idxdef NOT ILIKE '%prompt_family_key%' OR idxdef NOT ILIKE '%platform_key%' OR idxdef NOT ILIKE '%contribution_date%' THEN
        RAISE EXCEPTION '[反查] uq_geo_obs_contributor_vote 缺失/列不符(R6):%', idxdef; END IF;
    IF idxdef ILIKE '%source_type%' THEN RAISE EXCEPTION '[反查] uq_geo_obs_contributor_vote 违规含 source_type(R6 禁):%', idxdef; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_obs_bucket_event' AND conrelid='geo_observation_contributor_buckets'::regclass AND contype='f' AND convalidated) THEN
        RAISE EXCEPTION '[反查] fk_geo_obs_bucket_event 缺失/未 VALIDATE'; END IF;

    -- 6) audit:event_id 可空 + audit_event_key 唯一 + operator CHECK
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_audit' AND column_name='event_id' AND is_nullable='NO') THEN
        RAISE EXCEPTION '[反查] audit.event_id 应可空(policy/系统级审计)'; END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('uq_geo_obs_audit_key'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%audit_event_key%' THEN
        RAISE EXCEPTION '[反查] uq_geo_obs_audit_key 缺失:%', idxdef; END IF;

    -- 7) policy:singleton CHECK + 默认行存在 + feature_flags 全 false(R11)
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_policy_singleton' AND conrelid='geo_observation_policy'::regclass);
    IF ckdef IS NULL OR strpos(ckdef,'1')=0 THEN RAISE EXCEPTION '[反查] chk_geo_obs_policy_singleton 缺失/不符:%', ckdef; END IF;
    IF NOT EXISTS (SELECT 1 FROM geo_observation_policy WHERE singleton_id=1) THEN RAISE EXCEPTION '[反查] policy 默认行缺失'; END IF;
    IF (SELECT (policy_json->'feature_flags'->>'promotion_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1) IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION '[反查][R11] policy 默认 promotion_enabled 必须为 false'; END IF;
    IF (SELECT (policy_json->'feature_flags'->>'aggregation_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1) IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION '[反查][R11] policy 默认 aggregation_enabled 必须为 false'; END IF;
    IF (SELECT (policy_json->'feature_flags'->>'ingest_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1) IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION '[反查][R11] policy 默认 ingest_enabled 必须为 false'; END IF;
    IF (SELECT (policy_json->'feature_flags'->>'product_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1) IS DISTINCT FROM FALSE THEN
        RAISE EXCEPTION '[反查][R11] policy 默认 product_enabled 必须为 false'; END IF;

    -- 8) aggregates:scope 隐私 CHECK + 全枚举 + bps + 唯一/partial index + 关键类型
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_agg_scope' AND conrelid='geo_observation_aggregates'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_geo_obs_agg_scope 缺失'; END IF;
    FOREACH v IN ARRAY scopes LOOP IF strpos(ckdef, quote_literal(v))=0 THEN RAISE EXCEPTION '[反查] agg scope CHECK 缺 %:%', v, ckdef; END IF; END LOOP;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='chk_geo_obs_agg_stability' AND conrelid='geo_observation_aggregates'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_geo_obs_agg_stability 缺失'; END IF;
    FOREACH v IN ARRAY stabilities LOOP IF strpos(ckdef, quote_literal(v))=0 THEN RAISE EXCEPTION '[反查] agg stability CHECK 缺 %:%', v, ckdef; END IF; END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_agg_private_scope' AND conrelid='geo_observation_aggregates'::regclass) THEN
        RAISE EXCEPTION '[反查] chk_geo_obs_agg_private_scope 缺失'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_agg_public_scope' AND conrelid='geo_observation_aggregates'::regclass) THEN
        RAISE EXCEPTION '[反查] chk_geo_obs_agg_public_scope 缺失'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_agg_bps_range' AND conrelid='geo_observation_aggregates'::regclass) THEN
        RAISE EXCEPTION '[反查] chk_geo_obs_agg_bps_range 缺失'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_agg_counts_nonneg' AND conrelid='geo_observation_aggregates'::regclass) THEN
        RAISE EXCEPTION '[反查] chk_geo_obs_agg_counts_nonneg 缺失'; END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_aggregates' AND column_name='aggregate_key' AND data_type='character' AND character_maximum_length=64) THEN
        RAISE EXCEPTION '[反查] aggregates.aggregate_key 非 char(64)'; END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_aggregates' AND column_name='weighted_denominator_micros' AND data_type='bigint') THEN
        RAISE EXCEPTION '[反查] aggregates.weighted_denominator_micros 非 bigint'; END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('uq_geo_obs_agg_key'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%aggregate_key%' THEN
        RAISE EXCEPTION '[反查] uq_geo_obs_agg_key 缺失:%', idxdef; END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('idx_geo_obs_agg_private'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%WHERE%' OR idxdef NOT ILIKE '%private_brand%' THEN
        RAISE EXCEPTION '[反查] idx_geo_obs_agg_private partial predicate 不符:%', idxdef; END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('idx_geo_obs_agg_public'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%WHERE%' OR idxdef NOT ILIKE '%public_industry%' THEN
        RAISE EXCEPTION '[反查] idx_geo_obs_agg_public partial predicate 不符:%', idxdef; END IF;

    RAISE NOTICE '[geo_observation_v1 迁移] 反查断言全部通过(6 表/列类型/NOT NULL/全枚举 CHECK/FK convalidated/唯一门/partial predicate/R6 无 source_type/R11 晋升开关默认 false/signals 无 owner-brand)';
END $$;
