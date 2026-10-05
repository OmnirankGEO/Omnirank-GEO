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
    -- 付费外部调用幂等锚:本 worker 首次真实 provider POST 前持久化(同 lease CAS)。
    -- 非空 + 事件被重领 = 上个 worker 已发起付费调用但未收尾(kill-9/租约丢失,结果未知)→
    -- 重领 worker 一律路由 pending_review(人工),绝不自动再次付费猜测重试。
    paid_call_started_at   TIMESTAMPTZ,
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
ALTER TABLE geo_observation_events ADD COLUMN IF NOT EXISTS paid_call_started_at   TIMESTAMPTZ;
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

-- 统一集成基础设施。仍属于本文件这一份观测 migration，避免第二套 schema 所有权。
-- AI-1: 跨 worker 成本预留/账本；AI-3: 洞察任务的幂等与 lease 恢复。
CREATE TABLE IF NOT EXISTS geo_ai_surface_cost_ledger (
    id             BIGSERIAL PRIMARY KEY,
    source_kind    VARCHAR(40)  NOT NULL,
    surface_key    VARCHAR(80)  NOT NULL,
    amount_micros  BIGINT       NOT NULL,
    call_count     BIGINT       NOT NULL DEFAULT 1,
    is_estimated   BOOLEAN      NOT NULL DEFAULT FALSE,
    request_id     VARCHAR(200) NOT NULL,
    round_id       VARCHAR(64),
    recorded_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE SEQUENCE IF NOT EXISTS geo_ai_surface_cost_ledger_id_seq;
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS id BIGINT;
ALTER SEQUENCE geo_ai_surface_cost_ledger_id_seq OWNED BY geo_ai_surface_cost_ledger.id;
ALTER TABLE geo_ai_surface_cost_ledger
    ALTER COLUMN id SET DEFAULT nextval('geo_ai_surface_cost_ledger_id_seq'::regclass),
    ALTER COLUMN id SET NOT NULL;
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS source_kind VARCHAR(40);
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS surface_key VARCHAR(80);
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS amount_micros BIGINT;
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS call_count BIGINT NOT NULL DEFAULT 1;
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS is_estimated BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS request_id VARCHAR(200);
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS round_id VARCHAR(64);
ALTER TABLE geo_ai_surface_cost_ledger ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE geo_ai_surface_cost_ledger
    ALTER COLUMN source_kind DROP DEFAULT,
    ALTER COLUMN source_kind SET NOT NULL,
    ALTER COLUMN surface_key DROP DEFAULT,
    ALTER COLUMN surface_key SET NOT NULL,
    ALTER COLUMN amount_micros DROP DEFAULT,
    ALTER COLUMN amount_micros SET NOT NULL,
    ALTER COLUMN call_count SET DEFAULT 1,
    ALTER COLUMN call_count SET NOT NULL,
    ALTER COLUMN is_estimated SET DEFAULT FALSE,
    ALTER COLUMN is_estimated SET NOT NULL,
    ALTER COLUMN request_id DROP DEFAULT,
    ALTER COLUMN request_id SET NOT NULL,
    ALTER COLUMN round_id DROP DEFAULT,
    ALTER COLUMN round_id DROP NOT NULL,
    ALTER COLUMN recorded_at SET DEFAULT NOW(),
    ALTER COLUMN recorded_at SET NOT NULL;
-- @index-guard idx_ai_surface_cost_ledger_src_time ON geo_ai_surface_cost_ledger plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_surface_cost_ledger_src_time' AND i.indrelid = to_regclass('public.geo_ai_surface_cost_ledger')) THEN
        NULL;  -- 已在 public.geo_ai_surface_cost_ledger 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_surface_cost_ledger_src_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_surface_cost_ledger_src_time 已存在但不在 public.geo_ai_surface_cost_ledger 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_surface_cost_ledger_src_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_surface_cost_ledger_src_time ON public.geo_ai_surface_cost_ledger (source_kind, recorded_at);
    END IF;
END $idxguard$;
-- @index-guard idx_ai_surface_cost_ledger_round ON geo_ai_surface_cost_ledger plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_surface_cost_ledger_round' AND i.indrelid = to_regclass('public.geo_ai_surface_cost_ledger')) THEN
        NULL;  -- 已在 public.geo_ai_surface_cost_ledger 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_surface_cost_ledger_round' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_surface_cost_ledger_round 已存在但不在 public.geo_ai_surface_cost_ledger 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_surface_cost_ledger_round' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_surface_cost_ledger_round ON public.geo_ai_surface_cost_ledger (round_id);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS geo_ai_surface_cost_reservations (
    token          VARCHAR(64)  PRIMARY KEY,
    source_kind    VARCHAR(40)  NOT NULL,
    surface_key    VARCHAR(80)  NOT NULL,
    amount_micros  BIGINT       NOT NULL,
    call_count     BIGINT       NOT NULL DEFAULT 1,
    request_id     VARCHAR(200) NOT NULL,
    round_id       VARCHAR(64),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
ALTER TABLE geo_ai_surface_cost_reservations ADD COLUMN IF NOT EXISTS token VARCHAR(64);
ALTER TABLE geo_ai_surface_cost_reservations ADD COLUMN IF NOT EXISTS source_kind VARCHAR(40);
ALTER TABLE geo_ai_surface_cost_reservations ADD COLUMN IF NOT EXISTS amount_micros BIGINT;
ALTER TABLE geo_ai_surface_cost_reservations ADD COLUMN IF NOT EXISTS call_count BIGINT NOT NULL DEFAULT 1;
ALTER TABLE geo_ai_surface_cost_reservations ADD COLUMN IF NOT EXISTS surface_key VARCHAR(80) NOT NULL DEFAULT '';
ALTER TABLE geo_ai_surface_cost_reservations ADD COLUMN IF NOT EXISTS request_id VARCHAR(200);
ALTER TABLE geo_ai_surface_cost_reservations ADD COLUMN IF NOT EXISTS round_id VARCHAR(64);
ALTER TABLE geo_ai_surface_cost_reservations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE geo_ai_surface_cost_reservations
    ALTER COLUMN token DROP DEFAULT,
    ALTER COLUMN token SET NOT NULL,
    ALTER COLUMN source_kind DROP DEFAULT,
    ALTER COLUMN source_kind SET NOT NULL,
    ALTER COLUMN surface_key DROP DEFAULT,
    ALTER COLUMN surface_key SET NOT NULL,
    ALTER COLUMN amount_micros DROP DEFAULT,
    ALTER COLUMN amount_micros SET NOT NULL,
    ALTER COLUMN call_count SET DEFAULT 1,
    ALTER COLUMN call_count SET NOT NULL,
    ALTER COLUMN request_id DROP DEFAULT,
    ALTER COLUMN request_id SET NOT NULL,
    ALTER COLUMN round_id DROP DEFAULT,
    ALTER COLUMN round_id DROP NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL;
-- @index-guard idx_ai_surface_cost_res_src ON geo_ai_surface_cost_reservations plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_surface_cost_res_src' AND i.indrelid = to_regclass('public.geo_ai_surface_cost_reservations')) THEN
        NULL;  -- 已在 public.geo_ai_surface_cost_reservations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_surface_cost_res_src' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_surface_cost_res_src 已存在但不在 public.geo_ai_surface_cost_reservations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_surface_cost_res_src' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_surface_cost_res_src ON public.geo_ai_surface_cost_reservations (source_kind);
    END IF;
END $idxguard$;
-- @index-guard idx_ai_surface_cost_res_created ON geo_ai_surface_cost_reservations plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_surface_cost_res_created' AND i.indrelid = to_regclass('public.geo_ai_surface_cost_reservations')) THEN
        NULL;  -- 已在 public.geo_ai_surface_cost_reservations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_surface_cost_res_created' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_surface_cost_res_created 已存在但不在 public.geo_ai_surface_cost_reservations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_surface_cost_res_created' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_surface_cost_res_created ON public.geo_ai_surface_cost_reservations (created_at);
    END IF;
END $idxguard$;
-- @index-guard idx_ai_surface_cost_res_round ON geo_ai_surface_cost_reservations plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_ai_surface_cost_res_round' AND i.indrelid = to_regclass('public.geo_ai_surface_cost_reservations')) THEN
        NULL;  -- 已在 public.geo_ai_surface_cost_reservations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_ai_surface_cost_res_round' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_ai_surface_cost_res_round 已存在但不在 public.geo_ai_surface_cost_reservations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_ai_surface_cost_res_round' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_ai_surface_cost_res_round ON public.geo_ai_surface_cost_reservations (round_id);
    END IF;
END $idxguard$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_ai_surface_cost_ledger_pkey'
          AND conrelid='geo_ai_surface_cost_ledger'::regclass
    ) THEN
        ALTER TABLE geo_ai_surface_cost_ledger
            ADD CONSTRAINT geo_ai_surface_cost_ledger_pkey PRIMARY KEY (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_ai_surface_cost_ledger_amount_nonneg'
          AND conrelid='geo_ai_surface_cost_ledger'::regclass
    ) THEN
        ALTER TABLE geo_ai_surface_cost_ledger
            ADD CONSTRAINT geo_ai_surface_cost_ledger_amount_nonneg
            CHECK (amount_micros >= 0 AND call_count >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_ai_surface_cost_ledger_request_uniq'
          AND conrelid='geo_ai_surface_cost_ledger'::regclass
    ) THEN
        ALTER TABLE geo_ai_surface_cost_ledger
            ADD CONSTRAINT geo_ai_surface_cost_ledger_request_uniq UNIQUE (request_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_ai_surface_cost_reservations_pkey'
          AND conrelid='geo_ai_surface_cost_reservations'::regclass
    ) THEN
        ALTER TABLE geo_ai_surface_cost_reservations
            ADD CONSTRAINT geo_ai_surface_cost_reservations_pkey PRIMARY KEY (token);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_ai_surface_cost_res_amount_nonneg'
          AND conrelid='geo_ai_surface_cost_reservations'::regclass
    ) THEN
        ALTER TABLE geo_ai_surface_cost_reservations
            ADD CONSTRAINT geo_ai_surface_cost_res_amount_nonneg
            CHECK (amount_micros >= 0 AND call_count >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_ai_surface_cost_res_request_uniq'
          AND conrelid='geo_ai_surface_cost_reservations'::regclass
    ) THEN
        ALTER TABLE geo_ai_surface_cost_reservations
            ADD CONSTRAINT geo_ai_surface_cost_res_request_uniq UNIQUE (request_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS geo_observation_insight_jobs (
    job_id          TEXT PRIMARY KEY,
    input_hash      CHAR(64) NOT NULL,
    owner_user_id   BIGINT NOT NULL,
    brand_id        BIGINT NOT NULL,
    request_id      TEXT,
    state           TEXT NOT NULL DEFAULT 'pending'
        CONSTRAINT geo_obs_insight_state_chk
        CHECK (state IN ('pending','running','completed','failed')),
    result_state    TEXT
        CONSTRAINT geo_obs_insight_result_state_chk
        CHECK (result_state IS NULL OR result_state IN ('ok','insufficient')),
    provider        TEXT NOT NULL DEFAULT 'deepseek',
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    summary         TEXT,
    evidence_refs   JSONB NOT NULL DEFAULT '[]'::jsonb,
    allowed_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_code      TEXT,
    error_detail    TEXT,
    input_token     INTEGER,
    output_token    INTEGER,
    cost_micros     BIGINT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    lease_token     UUID,
    lease_until     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    CONSTRAINT geo_obs_insight_scope_hash_uk UNIQUE (owner_user_id, brand_id, input_hash)
);
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS job_id TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS input_hash CHAR(64);
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS owner_user_id BIGINT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS brand_id BIGINT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS request_id TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS result_state TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'deepseek';
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS prompt_version TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS schema_version TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS allowed_actions JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS error_code TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS error_detail TEXT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS input_token INTEGER;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS output_token INTEGER;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS cost_micros BIGINT;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS lease_token UUID;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE geo_observation_insight_jobs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE geo_observation_insight_jobs
    ALTER COLUMN job_id DROP DEFAULT,
    ALTER COLUMN job_id SET NOT NULL,
    ALTER COLUMN input_hash DROP DEFAULT,
    ALTER COLUMN input_hash SET NOT NULL,
    ALTER COLUMN owner_user_id DROP DEFAULT,
    ALTER COLUMN owner_user_id SET NOT NULL,
    ALTER COLUMN brand_id DROP DEFAULT,
    ALTER COLUMN brand_id SET NOT NULL,
    ALTER COLUMN request_id DROP DEFAULT,
    ALTER COLUMN request_id DROP NOT NULL,
    ALTER COLUMN state SET DEFAULT 'pending',
    ALTER COLUMN state SET NOT NULL,
    ALTER COLUMN result_state DROP DEFAULT,
    ALTER COLUMN result_state DROP NOT NULL,
    ALTER COLUMN provider SET DEFAULT 'deepseek',
    ALTER COLUMN provider SET NOT NULL,
    ALTER COLUMN model DROP DEFAULT,
    ALTER COLUMN model SET NOT NULL,
    ALTER COLUMN prompt_version DROP DEFAULT,
    ALTER COLUMN prompt_version SET NOT NULL,
    ALTER COLUMN schema_version DROP DEFAULT,
    ALTER COLUMN schema_version SET NOT NULL,
    ALTER COLUMN summary DROP DEFAULT,
    ALTER COLUMN summary DROP NOT NULL,
    ALTER COLUMN evidence_refs SET DEFAULT '[]'::jsonb,
    ALTER COLUMN evidence_refs SET NOT NULL,
    ALTER COLUMN allowed_actions SET DEFAULT '[]'::jsonb,
    ALTER COLUMN allowed_actions SET NOT NULL,
    ALTER COLUMN error_code DROP DEFAULT,
    ALTER COLUMN error_code DROP NOT NULL,
    ALTER COLUMN error_detail DROP DEFAULT,
    ALTER COLUMN error_detail DROP NOT NULL,
    ALTER COLUMN input_token DROP DEFAULT,
    ALTER COLUMN input_token DROP NOT NULL,
    ALTER COLUMN output_token DROP DEFAULT,
    ALTER COLUMN output_token DROP NOT NULL,
    ALTER COLUMN cost_micros DROP DEFAULT,
    ALTER COLUMN cost_micros DROP NOT NULL,
    ALTER COLUMN attempts SET DEFAULT 0,
    ALTER COLUMN attempts SET NOT NULL,
    ALTER COLUMN lease_token DROP DEFAULT,
    ALTER COLUMN lease_token DROP NOT NULL,
    ALTER COLUMN lease_until DROP DEFAULT,
    ALTER COLUMN lease_until DROP NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET NOT NULL,
    ALTER COLUMN completed_at DROP DEFAULT,
    ALTER COLUMN completed_at DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_observation_insight_jobs_pkey'
          AND conrelid='geo_observation_insight_jobs'::regclass
    ) THEN
        ALTER TABLE geo_observation_insight_jobs
            ADD CONSTRAINT geo_observation_insight_jobs_pkey PRIMARY KEY (job_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_obs_insight_state_chk'
          AND conrelid='geo_observation_insight_jobs'::regclass
    ) THEN
        ALTER TABLE geo_observation_insight_jobs
            ADD CONSTRAINT geo_obs_insight_state_chk
            CHECK (state IN ('pending','running','completed','failed'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='geo_obs_insight_result_state_chk'
          AND conrelid='geo_observation_insight_jobs'::regclass
    ) THEN
        ALTER TABLE geo_observation_insight_jobs
            ADD CONSTRAINT geo_obs_insight_result_state_chk
            CHECK (result_state IS NULL OR result_state IN ('ok','insufficient'));
    END IF;
    -- The aggregate-basis migration upgrades idempotency to include a durable
    -- snapshot_id.  On a full manifest re-run this legacy migration executes
    -- first; it must not recreate the old three-column UNIQUE and reject two
    -- legitimate snapshots of the same fact pack.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='geo_observation_insight_jobs'
           AND column_name='snapshot_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='geo_obs_insight_scope_hash_uk'
           AND conrelid='public.geo_observation_insight_jobs'::regclass
    ) THEN
        ALTER TABLE public.geo_observation_insight_jobs
            ADD CONSTRAINT geo_obs_insight_scope_hash_uk
            UNIQUE (owner_user_id, brand_id, input_hash);
    END IF;
END $$;
-- @index-guard idx_geo_obs_insight_reclaim ON geo_observation_insight_jobs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_insight_reclaim' AND i.indrelid = to_regclass('public.geo_observation_insight_jobs')) THEN
        NULL;  -- 已在 public.geo_observation_insight_jobs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_insight_reclaim' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_insight_reclaim 已存在但不在 public.geo_observation_insight_jobs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_insight_reclaim' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_insight_reclaim ON public.geo_observation_insight_jobs (state, lease_until);
    END IF;
END $idxguard$;

-- 唯一门:业务键(source 四元组) + source_event_key(SHA-256 幂等门) + event_uuid
-- @index-guard uq_geo_obs_event_business ON geo_observation_events unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_event_business' AND i.indrelid = to_regclass('public.geo_observation_events')) THEN
        NULL;  -- 已在 public.geo_observation_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_event_business' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_event_business 已存在但不在 public.geo_observation_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_event_business' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_event_business ON public.geo_observation_events (source_type, source_table, source_record_id, source_subkey);
    END IF;
END $idxguard$;
-- @index-guard uq_geo_obs_event_key ON geo_observation_events unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_event_key' AND i.indrelid = to_regclass('public.geo_observation_events')) THEN
        NULL;  -- 已在 public.geo_observation_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_event_key' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_event_key 已存在但不在 public.geo_observation_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_event_key' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_event_key ON public.geo_observation_events (source_event_key);
    END IF;
END $idxguard$;
-- @index-guard uq_geo_obs_event_uuid ON geo_observation_events unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_event_uuid' AND i.indrelid = to_regclass('public.geo_observation_events')) THEN
        NULL;  -- 已在 public.geo_observation_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_event_uuid' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_event_uuid 已存在但不在 public.geo_observation_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_event_uuid' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_event_uuid ON public.geo_observation_events (event_uuid);
    END IF;
END $idxguard$;
-- claim 索引(可领取态) + lease 恢复索引
-- @index-guard idx_geo_obs_event_claimable ON geo_observation_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_event_claimable' AND i.indrelid = to_regclass('public.geo_observation_events')) THEN
        NULL;  -- 已在 public.geo_observation_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_event_claimable' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_event_claimable 已存在但不在 public.geo_observation_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_event_claimable' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_event_claimable ON public.geo_observation_events (processing_state, created_at) WHERE processing_state IN ('pending','processing');
    END IF;
END $idxguard$;
-- @index-guard idx_geo_obs_event_lease ON geo_observation_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_event_lease' AND i.indrelid = to_regclass('public.geo_observation_events')) THEN
        NULL;  -- 已在 public.geo_observation_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_event_lease' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_event_lease 已存在但不在 public.geo_observation_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_event_lease' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_event_lease ON public.geo_observation_events (lease_until) WHERE lease_until IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_obs_event_owner_brand ON geo_observation_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_event_owner_brand' AND i.indrelid = to_regclass('public.geo_observation_events')) THEN
        NULL;  -- 已在 public.geo_observation_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_event_owner_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_event_owner_brand 已存在但不在 public.geo_observation_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_event_owner_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_event_owner_brand ON public.geo_observation_events (owner_user_id, brand_id);
    END IF;
END $idxguard$;

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
-- @index-guard uq_geo_obs_signal_event ON geo_observation_signals unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_signal_event' AND i.indrelid = to_regclass('public.geo_observation_signals')) THEN
        NULL;  -- 已在 public.geo_observation_signals 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_signal_event' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_signal_event 已存在但不在 public.geo_observation_signals 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_signal_event' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_signal_event ON public.geo_observation_signals (event_id);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_obs_signal_industry ON geo_observation_signals plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_signal_industry' AND i.indrelid = to_regclass('public.geo_observation_signals')) THEN
        NULL;  -- 已在 public.geo_observation_signals 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_signal_industry' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_signal_industry 已存在但不在 public.geo_observation_signals 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_signal_industry' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_signal_industry ON public.geo_observation_signals (industry_key, prompt_family_key, platform_key);
    END IF;
END $idxguard$;

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
-- @index-guard uq_geo_obs_bucket_event ON geo_observation_contributor_buckets unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_bucket_event' AND i.indrelid = to_regclass('public.geo_observation_contributor_buckets')) THEN
        NULL;  -- 已在 public.geo_observation_contributor_buckets 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_bucket_event' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_bucket_event 已存在但不在 public.geo_observation_contributor_buckets 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_bucket_event' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_bucket_event ON public.geo_observation_contributor_buckets (event_id);
    END IF;
END $idxguard$;
-- ★ 公共投票唯一门(R6 · 不含 source_type):同 user/brand/题族/平台/自然日最多一票
-- @index-guard uq_geo_obs_contributor_vote ON geo_observation_contributor_buckets unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_contributor_vote' AND i.indrelid = to_regclass('public.geo_observation_contributor_buckets')) THEN
        NULL;  -- 已在 public.geo_observation_contributor_buckets 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_contributor_vote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_contributor_vote 已存在但不在 public.geo_observation_contributor_buckets 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_contributor_vote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_contributor_vote ON public.geo_observation_contributor_buckets ( contributor_user_bucket, contributor_brand_bucket, prompt_family_key, platform_key, contribution_date);
    END IF;
END $idxguard$;

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
-- @index-guard uq_geo_obs_audit_key ON geo_observation_audit unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_audit_key' AND i.indrelid = to_regclass('public.geo_observation_audit')) THEN
        NULL;  -- 已在 public.geo_observation_audit 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_audit_key' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_audit_key 已存在但不在 public.geo_observation_audit 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_audit_key' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_audit_key ON public.geo_observation_audit (audit_event_key);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_obs_audit_event ON geo_observation_audit plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_audit_event' AND i.indrelid = to_regclass('public.geo_observation_audit')) THEN
        NULL;  -- 已在 public.geo_observation_audit 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_audit_event' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_audit_event 已存在但不在 public.geo_observation_audit 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_audit_event' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_audit_event ON public.geo_observation_audit (event_id, created_at DESC);
    END IF;
END $idxguard$;

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
    outcome_gold_gate_passed BOOLEAN NOT NULL DEFAULT FALSE,       -- outcome 金标准门(服务端按不可变评估记录派生·禁前端直接提交 true);false→confirmed_mention 只入 pending_review
    gold_dataset_version     TEXT,                                 -- 达门评估的数据集版本
    gold_macro_f1_bps        INTEGER,                              -- 达门评估的 macro-F1(0..10000·契约§601 须 >=9000)
    gold_sample_count        INTEGER,                              -- 达门评估的双人复核样本数(契约§601 须 >=100)
    gold_high_risk_false_reco INTEGER,                             -- 达门评估的高风险相似品牌误推荐数(契约§601 须 =0)
    gold_report_hash         TEXT,                                 -- 不可变评估报告哈希(指向 geo_observation_gold_eval)
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
ALTER TABLE geo_observation_policy ADD COLUMN IF NOT EXISTS gold_sample_count        INTEGER;
ALTER TABLE geo_observation_policy ADD COLUMN IF NOT EXISTS gold_high_risk_false_reco INTEGER;
ALTER TABLE geo_observation_policy ADD COLUMN IF NOT EXISTS gold_report_hash         TEXT;
-- P1-1:金标准门 DB 层硬约束 —— outcome_gold_gate_passed=TRUE 只允许在证据满足契约§601 阈值时(样本≥100·macro-F1≥9000·高风险误推荐=0·数据集版本与报告哈希齐备)。
--   任何裸 UPDATE 都无法把 true 写进无证据的行 → 关掉"自我批准可信布尔值"通道(服务端仍从不可变评估记录派生,见 policy.record_gold_evaluation)。
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_obs_policy_gold_gate'
                 AND conrelid='geo_observation_policy'::regclass) THEN
    -- NULL 安全:CHECK 只在表达式为 FALSE 时拒(TRUE/NULL 都放行)→ 整数比较必 COALESCE 兜底,
    --   否则 NULL 证据下 `FALSE OR NULL`=NULL 会放行强开门。COALESCE 兜哨兵值让阈值不满足时确定为 FALSE。
    ALTER TABLE geo_observation_policy ADD CONSTRAINT ck_geo_obs_policy_gold_gate CHECK (
      outcome_gold_gate_passed = FALSE OR (
        COALESCE(gold_sample_count, -1) >= 100
        AND COALESCE(gold_macro_f1_bps, -1) >= 9000
        AND COALESCE(gold_high_risk_false_reco, -1) = 0
        AND gold_dataset_version IS NOT NULL AND gold_report_hash IS NOT NULL));
  END IF;
END $$;
-- P1-1:不可变金标准评估记录(append-only·服务端计算 gate_passed 留痕·policy 派生值指向本表 report_hash)
CREATE TABLE IF NOT EXISTS geo_observation_gold_eval (
    id                    BIGSERIAL PRIMARY KEY,
    dataset_version       TEXT NOT NULL,
    sample_count          INTEGER NOT NULL
        CONSTRAINT chk_geo_obs_gold_sample CHECK (sample_count >= 0),
    macro_f1_bps          INTEGER NOT NULL
        CONSTRAINT chk_geo_obs_gold_f1 CHECK (macro_f1_bps >= 0 AND macro_f1_bps <= 10000),
    high_risk_false_reco  INTEGER NOT NULL
        CONSTRAINT chk_geo_obs_gold_hr CHECK (high_risk_false_reco >= 0),
    report_hash           TEXT NOT NULL,
    gate_passed           BOOLEAN NOT NULL,   -- 服务端按阈值计算的派生结果(留痕·非输入)
        -- gate_passed 必与指标一致(与 compute_gold_gate_passed 同阈值):即便裸 INSERT 也无法造"指标不达标却 passed=true"的行
        CONSTRAINT chk_geo_obs_gold_derived CHECK (
            gate_passed = (sample_count >= 100 AND macro_f1_bps >= 9000 AND high_risk_false_reco = 0
                           AND length(dataset_version) > 0 AND length(report_hash) > 0)),
    created_by            TEXT,
    created_reason        TEXT,
    request_id            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- @index-guard uq_geo_obs_gold_eval ON geo_observation_gold_eval unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_gold_eval' AND i.indrelid = to_regclass('public.geo_observation_gold_eval')) THEN
        NULL;  -- 已在 public.geo_observation_gold_eval 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_gold_eval' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_gold_eval 已存在但不在 public.geo_observation_gold_eval 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_gold_eval' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_gold_eval ON public.geo_observation_gold_eval (dataset_version, report_hash);
    END IF;
END $idxguard$;
-- 既有表补 gate_passed 一致性 CHECK(CREATE TABLE IF NOT EXISTS 不会给存量表加约束 → 幂等/升级补挂)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_geo_obs_gold_derived'
                 AND conrelid='geo_observation_gold_eval'::regclass) THEN
    ALTER TABLE geo_observation_gold_eval ADD CONSTRAINT chk_geo_obs_gold_derived CHECK (
        gate_passed = (sample_count >= 100 AND macro_f1_bps >= 9000 AND high_risk_false_reco = 0
                       AND length(dataset_version) > 0 AND length(report_hash) > 0));
  END IF;
END $$;
-- P1:DB 级 append-only(不再只靠代码约定)——UPDATE/DELETE 一律 RAISE,评估记录不可改写/删除。
--   函数体固定为下述规范文本(readiness/migration 反查用 pg_get_functiondef 精确 pin,篡改函数体即被检出);
--   故意不用 % / TG_OP(避免占位符,保证 pin 串稳定;消息文本对 append-only 语义无影响)。
CREATE OR REPLACE FUNCTION geo_obs_gold_eval_immutable() RETURNS trigger AS $imm$
BEGIN
    RAISE EXCEPTION 'geo_observation_gold_eval is append-only immutable; UPDATE/DELETE forbidden';
END;
$imm$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_geo_obs_gold_eval_immutable ON geo_observation_gold_eval;
CREATE TRIGGER trg_geo_obs_gold_eval_immutable
    BEFORE UPDATE OR DELETE ON geo_observation_gold_eval
    FOR EACH ROW EXECUTE FUNCTION geo_obs_gold_eval_immutable();
-- P1:policy 金标准指针精确引用不可变评估行(gate 派生自它);gate=TRUE 时 CHECK 要求两列非空 → FK 必被强制。
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_obs_policy_gold_eval'
                 AND conrelid='geo_observation_policy'::regclass) THEN
    ALTER TABLE geo_observation_policy ADD CONSTRAINT fk_geo_obs_policy_gold_eval
      FOREIGN KEY (gold_dataset_version, gold_report_hash)
      REFERENCES geo_observation_gold_eval (dataset_version, report_hash);
  END IF;
END $$;
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
-- @index-guard uq_geo_obs_agg_key ON geo_observation_aggregates unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_agg_key' AND i.indrelid = to_regclass('public.geo_observation_aggregates')) THEN
        NULL;  -- 已在 public.geo_observation_aggregates 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_agg_key' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_agg_key 已存在但不在 public.geo_observation_aggregates 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_agg_key' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_agg_key ON public.geo_observation_aggregates (aggregate_key);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_obs_agg_scope_bucket ON geo_observation_aggregates plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_agg_scope_bucket' AND i.indrelid = to_regclass('public.geo_observation_aggregates')) THEN
        NULL;  -- 已在 public.geo_observation_aggregates 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_agg_scope_bucket' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_agg_scope_bucket 已存在但不在 public.geo_observation_aggregates 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_agg_scope_bucket' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_agg_scope_bucket ON public.geo_observation_aggregates (scope_type, bucket_granularity, bucket_start);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_obs_agg_private ON geo_observation_aggregates plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_agg_private' AND i.indrelid = to_regclass('public.geo_observation_aggregates')) THEN
        NULL;  -- 已在 public.geo_observation_aggregates 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_agg_private' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_agg_private 已存在但不在 public.geo_observation_aggregates 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_agg_private' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_agg_private ON public.geo_observation_aggregates (owner_user_id, brand_id, bucket_start) WHERE scope_type = 'private_brand';
    END IF;
END $idxguard$;
-- @index-guard idx_geo_obs_agg_public ON geo_observation_aggregates plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_agg_public' AND i.indrelid = to_regclass('public.geo_observation_aggregates')) THEN
        NULL;  -- 已在 public.geo_observation_aggregates 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_agg_public' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_agg_public 已存在但不在 public.geo_observation_aggregates 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_agg_public' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_agg_public ON public.geo_observation_aggregates (industry_key, bucket_granularity, bucket_start) WHERE scope_type = 'public_industry';
    END IF;
END $idxguard$;

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

-- P1-3:强化枚举 CHECK 反查(会话临时函数)—— 抵御 substring 假绿。
--   ① 全枚举在场(值级必要条件)② 精确规范骨架匹配(去字面量+去类型转换+去空白 == CHECK((col=ANY(ARRAY[N-1 逗号])))):
--   任何结构偏离(TRUE OR / <> ANY 运算符换 / ARRAY[col,...] 自引用恒真 / 额外非法枚举 / 错列)都改变骨架 → RAISE 非零退出。
CREATE OR REPLACE FUNCTION pg_temp._geo_assert_enum(p_conname text, p_rel regclass, p_col text, p_enums text[])
RETURNS void AS $fn$
DECLARE ckdef text; skeleton text; expected text; v text;
BEGIN
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname=p_conname AND conrelid=p_rel);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] % 缺失', p_conname; END IF;
    FOREACH v IN ARRAY p_enums LOOP
        IF strpos(ckdef, quote_literal(v)) = 0 THEN RAISE EXCEPTION '[反查] % 缺枚举 %:%', p_conname, v, ckdef; END IF;
    END LOOP;
    -- 精确规范骨架匹配(最稳):去字面量 + 去类型转换 + 去空白 → 必须恰等于 CHECK((col=ANY(ARRAY[N-1 逗号])))。
    --   任何结构偏离(运算符换 <> ANY / 自引用恒真 ARRAY[col,...] / 额外 token OR·TRUE·IS NULL / 额外元素 / 错列)都改变骨架被拦。
    skeleton := regexp_replace(ckdef, '''[^'']*''', '', 'g');
    skeleton := regexp_replace(skeleton, '::(text\[\]|text|character varying|varchar|bpchar)', '', 'g');
    skeleton := regexp_replace(skeleton, '\s+', '', 'g');
    expected := 'CHECK((' || p_col || '=ANY(ARRAY[' || repeat(',', array_length(p_enums,1)-1) || '])))';
    IF skeleton <> expected THEN
        RAISE EXCEPTION '[反查] % 非规范 = ANY 成员式(疑弱化/篡改):期望 % 实际 % 原文 %', p_conname, expected, skeleton, ckdef; END IF;
END;
$fn$ LANGUAGE plpgsql;

-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ 可执行反查断言(FF · 失败即 RAISE → 迁移/prestart 非零退出)                ║
-- ╚══════════════════════════════════════════════════════════════════════════╝
DO $$
DECLARE
    v text;
    ckdef text;
    idxdef text;
    sentiments text[] := ARRAY['positive','neutral','negative','mixed','unknown'];
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
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='paid_call_started_at' AND data_type='timestamp with time zone' AND is_nullable='YES') THEN
        RAISE EXCEPTION '[反查] events.paid_call_started_at 缺失/非可空 timestamptz(付费幂等锚)'; END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='processing_purpose' AND is_nullable='YES') THEN
        RAISE EXCEPTION '[反查] events.processing_purpose 应 NOT NULL'; END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND column_name='legal_hold' AND is_nullable='YES') THEN
        RAISE EXCEPTION '[反查] events.legal_hold 应 NOT NULL'; END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='geo_observation_events' AND is_nullable='YES'
               AND column_name IN ('event_uuid','source_type','source_table','source_record_id','source_subkey',
                   'source_event_key','platform_key','provider_key','model_key','surface_key','session_mode','observed_at')) THEN
        RAISE EXCEPTION '[反查] events 核心列存在可空(半成品升级漂移未自愈,含数据无法自动收紧,人工订正)';
    END IF;

    -- 2) events 三命名 CHECK 全枚举 + 反重言式(P1-3 强化门)
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_event_surface_key', 'geo_observation_events'::regclass, 'surface_key', surfaces);
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_event_processing_state', 'geo_observation_events'::regclass, 'processing_state', proc_states);
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_event_source_type', 'geo_observation_events'::regclass, 'source_type', src_types);

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

    -- 4) signals 命名 CHECK 全枚举 + 反重言式(P1-3 强化门) + FK convalidated + 一事件一信号唯一
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_signal_outcome', 'geo_observation_signals'::regclass, 'target_outcome', outcomes);
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_signal_response', 'geo_observation_signals'::regclass, 'response_status', resp_states);
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_signal_intent', 'geo_observation_signals'::regclass, 'prompt_intent', intents);
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_signal_sentiment', 'geo_observation_signals'::regclass, 'sentiment', sentiments);
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
    -- R11 只对 pristine seed(policy_version=1,未经任何 CAS 治理变更)校验默认关闭:catches 篡改的 seed。
    --   合法启用走 update_policy(PUT /policy)必 bump version>1 → 跳过,避免"开关开启后每次 prestart 重跑 RAISE"破坏幂等(修复净增量 P1)。
    --   env 覆盖开关不写 policy_json(policy_json 恒 false),故不受影响。
    IF (SELECT policy_version FROM geo_observation_policy WHERE singleton_id=1) = 1 THEN
        IF (SELECT (policy_json->'feature_flags'->>'promotion_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1) IS DISTINCT FROM FALSE THEN
            RAISE EXCEPTION '[反查][R11] pristine seed promotion_enabled 必须为 false'; END IF;
        IF (SELECT (policy_json->'feature_flags'->>'aggregation_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1) IS DISTINCT FROM FALSE THEN
            RAISE EXCEPTION '[反查][R11] pristine seed aggregation_enabled 必须为 false'; END IF;
        IF (SELECT (policy_json->'feature_flags'->>'ingest_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1) IS DISTINCT FROM FALSE THEN
            RAISE EXCEPTION '[反查][R11] pristine seed ingest_enabled 必须为 false'; END IF;
        IF (SELECT (policy_json->'feature_flags'->>'product_enabled')::boolean FROM geo_observation_policy WHERE singleton_id=1) IS DISTINCT FROM FALSE THEN
            RAISE EXCEPTION '[反查][R11] pristine seed product_enabled 必须为 false'; END IF;
    END IF;
    -- 7b) P1-1 金标准门:ck_geo_obs_policy_gold_gate 存在 + gold_eval 表 + 功能反查(强开门无证据必被拒)。
    --   注意:不断言"当前 gate 值=false"——门被合法开启(record_gold_evaluation 写达阈值证据)后 gate=TRUE 是正常运行态,
    --   本 migration 每次 prestart 重跑,若断言运行态会在开门后永久阻断部署(幂等破坏)。默认关闭由列 DEFAULT FALSE + seed 保证,非反查职责。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_geo_obs_policy_gold_gate' AND conrelid='geo_observation_policy'::regclass) THEN
        RAISE EXCEPTION '[反查][P1-1] ck_geo_obs_policy_gold_gate 缺失'; END IF;
    IF to_regclass('geo_observation_gold_eval') IS NULL THEN RAISE EXCEPTION '[反查][P1-1] geo_observation_gold_eval 表缺失'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_geo_obs_gold_eval_immutable'
                   AND tgrelid='geo_observation_gold_eval'::regclass AND NOT tgisinternal
                   AND tgenabled IN ('O','A')) THEN  -- 存在≠生效:DISABLE('D')/replica-only('R')都算关
        RAISE EXCEPTION '[反查][P1-1] gold_eval append-only 触发器缺失或被 DISABLE(记录可被改写/删除)'; END IF;
    -- session_replication_role=replica 会让 'O' 用户触发器在主库不触发(append-only/FK 全体失效)——本 migration 每次
    --   DROP+CREATE 触发器可自愈 DISABLE/函数体篡改,但治不了这个 GUC,故此处显式断言(readiness 另有语义功能反查兜底)。
    IF current_setting('session_replication_role') <> 'origin' THEN
        RAISE EXCEPTION '[反查][P1-1] session_replication_role=% 非 origin:append-only/FK 触发器此模式不生效',
              current_setting('session_replication_role'); END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_geo_obs_policy_gold_eval'
                   AND conrelid='geo_observation_policy'::regclass AND contype='f') THEN
        RAISE EXCEPTION '[反查][P1-1] policy→gold_eval 外键缺失(策略指针可指向不存在的评估)'; END IF;
    -- 定义级精确匹配(与 readiness._GOLD_GATE_CHECK_NORM 同):pg_get_constraintdef 去空白后必等规范阈值式。
    --   比单向量功能反查稳(单探针会被"探针可过、别处放行"的部分弱化/降阈值绕过);post-migration 重跑亦检出漂移。
    ckdef := regexp_replace((SELECT pg_get_constraintdef(oid) FROM pg_constraint
                              WHERE conname='ck_geo_obs_policy_gold_gate' AND conrelid='geo_observation_policy'::regclass),
                            '\s+', '', 'g');
    IF ckdef <> 'CHECK(((outcome_gold_gate_passed=false)OR((COALESCE(gold_sample_count,''-1''::integer)>=100)'
               'AND(COALESCE(gold_macro_f1_bps,''-1''::integer)>=9000)AND(COALESCE(gold_high_risk_false_reco,''-1''::integer)=0)'
               'AND(gold_dataset_versionISNOTNULL)AND(gold_report_hashISNOTNULL))))' THEN
        RAISE EXCEPTION '[反查][P1-1] ck_geo_obs_policy_gold_gate 定义被篡改/弱化(非规范阈值式):%', ckdef; END IF;
    -- gate_passed 一致性 CHECK 定义级精确匹配(同名换 CHECK(TRUE) → 变;这些约束 IF NOT EXISTS 不自愈,重跑必须核定义)
    ckdef := regexp_replace((SELECT pg_get_constraintdef(oid) FROM pg_constraint
                              WHERE conname='chk_geo_obs_gold_derived' AND conrelid='geo_observation_gold_eval'::regclass),
                            '\s+', '', 'g');
    IF ckdef <> 'CHECK((gate_passed=((sample_count>=100)AND(macro_f1_bps>=9000)AND(high_risk_false_reco=0)'
               'AND(length(dataset_version)>0)AND(length(report_hash)>0))))' THEN
        RAISE EXCEPTION '[反查][P1-1] chk_geo_obs_gold_derived 定义被篡改(可伪造指标不达标却 passed=true):%', ckdef; END IF;
    -- policy→gold_eval 外键定义级精确匹配(改指伪表/错列 → 变)
    ckdef := regexp_replace((SELECT pg_get_constraintdef(oid) FROM pg_constraint
                              WHERE conname='fk_geo_obs_policy_gold_eval' AND conrelid='geo_observation_policy'::regclass),
                            '\s+', '', 'g');
    IF ckdef <> 'FOREIGNKEY(gold_dataset_version,gold_report_hash)REFERENCESgeo_observation_gold_eval(dataset_version,report_hash)' THEN
        RAISE EXCEPTION '[反查][P1-1] fk_geo_obs_policy_gold_eval 定义被篡改(策略指针可指伪表/错列):%', ckdef; END IF;

    -- 8) aggregates:scope 隐私 CHECK + 全枚举 + bps + 唯一/partial index + 关键类型
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_agg_scope', 'geo_observation_aggregates'::regclass, 'scope_type', scopes);
    PERFORM pg_temp._geo_assert_enum('chk_geo_obs_agg_stability', 'geo_observation_aggregates'::regclass, 'stability_status', stabilities);
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
