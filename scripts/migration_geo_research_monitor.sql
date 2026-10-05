-- migration_geo_research_monitor.sql
-- GEO 调研监测工具 7 张新表 + 2 张辅助表
-- 创建时间: 2026-05-06
-- spec: docs/superpowers/specs/2026-05-06-research-monitor-integration-design.md
--
-- 执行命令(部署 CTO):
--   docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope < scripts/migration_geo_research_monitor.sql
--
-- 验证命令:
--   docker exec omnirank-db psql -U geo_admin -d geo_agentscope -c "\dt geo_research_*"
--   docker exec omnirank-db psql -U geo_admin -d geo_agentscope -c "\di geo_research_*"
--   docker exec omnirank-db psql -U geo_admin -d geo_agentscope -c "SELECT marker, applied_at FROM _migration_markers WHERE marker = 'research_monitor_v1_initial'"
--
-- 回滚命令:
--   docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope < scripts/rollback_geo_research_monitor.sql
--   (回滚脚本按依赖倒序 DROP 9 张表 + 删 _migration_markers 中的 marker)

BEGIN;

-- ============================================================
-- 0. 既有 GEO 原始引用表依赖 (主系统 init_db 原本创建; 独立 CI/冷库也要自洽)
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_raw (
    id BIGSERIAL PRIMARY KEY,
    industry TEXT NOT NULL,
    query TEXT NOT NULL,
    engine TEXT NOT NULL,
    cited_platform TEXT NOT NULL,
    cite_position INTEGER DEFAULT 0,
    cite_url TEXT,
    cite_title TEXT,
    cite_excerpt TEXT,
    answer_text TEXT,
    batch_id TEXT,
    researcher TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- @index-guard idx_geo_raw_industry ON geo_research_raw plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_raw_industry' AND i.indrelid = to_regclass('public.geo_research_raw')) THEN
        NULL;  -- 已在 public.geo_research_raw 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_raw_industry' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_raw_industry 已存在但不在 public.geo_research_raw 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_raw_industry' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_raw_industry ON public.geo_research_raw (industry);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_raw_engine ON geo_research_raw plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_raw_engine' AND i.indrelid = to_regclass('public.geo_research_raw')) THEN
        NULL;  -- 已在 public.geo_research_raw 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_raw_engine' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_raw_engine 已存在但不在 public.geo_research_raw 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_raw_engine' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_raw_engine ON public.geo_research_raw (engine);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_raw_query ON geo_research_raw plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_raw_query' AND i.indrelid = to_regclass('public.geo_research_raw')) THEN
        NULL;  -- 已在 public.geo_research_raw 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_raw_query' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_raw_query 已存在但不在 public.geo_research_raw 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_raw_query' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_raw_query ON public.geo_research_raw (query, engine);
    END IF;
END $idxguard$;

-- ============================================================
-- 1. 行业列表 (admin 可 CRUD,软删)
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_industries (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,           -- 跟 geo_research_raw.industry 字符串对齐
    slug VARCHAR(100) NOT NULL UNIQUE,           -- 英文 slug 用于 URL/路径
    sort_order INTEGER DEFAULT 0,
    weight DECIMAL(4,2) DEFAULT 1.0,             -- 跑批权重 (future,先写死 1.0)
    active BOOLEAN DEFAULT TRUE,                 -- 软删用
    ai_seed_done BOOLEAN DEFAULT FALSE,          -- 初次 prompts 种子是否完成
    version INTEGER DEFAULT 1,                   -- 乐观锁: 每次 PATCH/DELETE 自增
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()         -- 由应用层 UPDATE 时维护(无 trigger)
);
COMMENT ON TABLE geo_research_industries IS 'GEO 调研监测 - 行业列表 (admin 可 CRUD,软删)';
COMMENT ON COLUMN geo_research_industries.name IS '行业名,跟 geo_research_raw.industry 字符串对齐';
COMMENT ON COLUMN geo_research_industries.version IS '乐观锁版本号,每次 PATCH/DELETE 自增';
COMMENT ON COLUMN geo_research_industries.updated_at IS '由应用层 UPDATE 时维护(无 trigger)';

-- ============================================================
-- 2. per-行业 prompts (admin 可 CRUD,软删)
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_prompts (
    id BIGSERIAL PRIMARY KEY,
    industry_id BIGINT NOT NULL REFERENCES geo_research_industries(id),
    prompt_text TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    source VARCHAR(20) DEFAULT 'manual'
        CHECK (source IN ('manual', 'seed_history', 'ai_generated')),
    version INTEGER DEFAULT 1,                   -- 乐观锁: 每次 PATCH/DELETE 自增
    is_sensitive BOOLEAN DEFAULT FALSE,          -- 品牌相关 prompts 标 sensitive,降级时优先保留
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()         -- 由应用层 UPDATE 时维护(无 trigger)
);
-- @index-guard idx_geo_research_prompts_industry_active ON geo_research_prompts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_prompts_industry_active' AND i.indrelid = to_regclass('public.geo_research_prompts')) THEN
        NULL;  -- 已在 public.geo_research_prompts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_prompts_industry_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_prompts_industry_active 已存在但不在 public.geo_research_prompts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_prompts_industry_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_prompts_industry_active ON public.geo_research_prompts (industry_id, active);
    END IF;
END $idxguard$;
COMMENT ON TABLE geo_research_prompts IS 'GEO 调研监测 - per-行业 prompts (admin 可 CRUD,软删)';
COMMENT ON COLUMN geo_research_prompts.is_sensitive IS '品牌相关 prompts 标 sensitive,预算降级时优先保留';
COMMENT ON COLUMN geo_research_prompts.version IS '乐观锁版本号';
COMMENT ON COLUMN geo_research_prompts.updated_at IS '由应用层 UPDATE 时维护(无 trigger)';

-- ============================================================
-- 3. 跑批主记录 + 进度
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_round (
    id BIGSERIAL PRIMARY KEY,
    round_id VARCHAR(50) NOT NULL UNIQUE,        -- e.g. "round_20260516_020000"
    batch_id VARCHAR(50) NOT NULL,               -- 写入 geo_research_raw 用的批次 ID
    triggered_by VARCHAR(30) NOT NULL
        CHECK (triggered_by IN ('cron', 'manual', 'missed_cron_recovery', 'selfserve')),
    triggered_user_id BIGINT,                    -- 手动触发时的 admin id
    industries_filter JSONB,                     -- null = 全行业 / [1,3] = 仅这几个
    status VARCHAR(20) NOT NULL
        CHECK (status IN ('pending', 'running', 'partial_success', 'failed', 'cancelled', 'completed', 'failed_resumable')),
    current_stage VARCHAR(40),                   -- 当前 stage 名称
    progress_json JSONB,                         -- {fetched:1240, total:1700, errors:8, ...}
    snapshot_json JSONB,                         -- 跑批启动时深拷贝的 industries + prompts 快照
    last_heartbeat_at TIMESTAMPTZ,               -- 心跳,用于检测 stale running (>5min 无更新视为崩溃)
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error_message TEXT,                          -- 整轮异常时
    summary_json JSONB,                          -- 完成后摘要 {raw_inserted, articles_crawled, ...}
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- @index-guard idx_geo_research_round_status_started ON geo_research_round plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_round_status_started' AND i.indrelid = to_regclass('public.geo_research_round')) THEN
        NULL;  -- 已在 public.geo_research_round 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_round_status_started' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_round_status_started 已存在但不在 public.geo_research_round 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_round_status_started' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_round_status_started ON public.geo_research_round (status, started_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_round_batch_id ON geo_research_round plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_round_batch_id' AND i.indrelid = to_regclass('public.geo_research_round')) THEN
        NULL;  -- 已在 public.geo_research_round 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_round_batch_id' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_round_batch_id 已存在但不在 public.geo_research_round 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_round_batch_id' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_round_batch_id ON public.geo_research_round (batch_id);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_round_heartbeat ON geo_research_round plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_round_heartbeat' AND i.indrelid = to_regclass('public.geo_research_round')) THEN
        NULL;  -- 已在 public.geo_research_round 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_round_heartbeat' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_round_heartbeat 已存在但不在 public.geo_research_round 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_round_heartbeat' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_round_heartbeat ON public.geo_research_round (last_heartbeat_at) WHERE status = 'running';
    END IF;
END $idxguard$;
COMMENT ON TABLE geo_research_round IS 'GEO 调研监测 - 跑批主记录与进度';
COMMENT ON COLUMN geo_research_round.snapshot_json IS '跑批启动时深拷贝的 industries+prompts 快照,运行中改动不影响本轮';
COMMENT ON COLUMN geo_research_round.last_heartbeat_at IS '心跳时间,用于服务器重启检测 stale 跑批 (lease 5min)';

-- ============================================================
-- 4. 单条 prompt × 平台 调用日志 (用于失败重试 + 统计 + 调试)
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_round_call (
    id BIGSERIAL PRIMARY KEY,
    round_id VARCHAR(50) NOT NULL REFERENCES geo_research_round(round_id) ON DELETE CASCADE,
    industry_id BIGINT,
    prompt_id BIGINT,
    prompt_text TEXT,                            -- 冗余,因为 prompt 可能跑完之后被改/删
    platform VARCHAR(20) NOT NULL
        CHECK (platform IN ('doubao', 'deepseek', 'qwen', 'kimi')),
    status VARCHAR(20) NOT NULL
        CHECK (status IN ('pending', 'success', 'failed', 'skipped')),
    attempts SMALLINT DEFAULT 0,
    raw_response_oss_key VARCHAR(500),           -- 原始 API 返回归档 OSS (失败排查)
    citations_count INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
-- @index-guard idx_geo_research_round_call_round_status ON geo_research_round_call plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_round_call_round_status' AND i.indrelid = to_regclass('public.geo_research_round_call')) THEN
        NULL;  -- 已在 public.geo_research_round_call 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_round_call_round_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_round_call_round_status 已存在但不在 public.geo_research_round_call 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_round_call_round_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_round_call_round_status ON public.geo_research_round_call (round_id, status);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_round_call_failed ON geo_research_round_call plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_round_call_failed' AND i.indrelid = to_regclass('public.geo_research_round_call')) THEN
        NULL;  -- 已在 public.geo_research_round_call 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_round_call_failed' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_round_call_failed 已存在但不在 public.geo_research_round_call 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_round_call_failed' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_round_call_failed ON public.geo_research_round_call (status) WHERE status = 'failed';
    END IF;
END $idxguard$;
COMMENT ON TABLE geo_research_round_call IS 'GEO 调研监测 - 单条 prompt × 平台调用日志,单条 commit 保证可恢复';

-- ============================================================
-- 5. 爬取的文章 (URL 全局唯一,只爬一次,跨轮命中复用)
--    article 是"全局长期素材库" + "审核状态机的载体",不是"轮次产物"
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_articles (
    id BIGSERIAL PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    url_hash CHAR(40) NOT NULL UNIQUE,           -- SHA1(url)
    domain VARCHAR(200) NOT NULL,
    title TEXT,
    primary_industry VARCHAR(100),               -- 首次见到时所属行业 (反向冗余,跨轮不变)

    oss_key_raw VARCHAR(500),                    -- 原始爬取版本 markdown
    oss_key_cleaned VARCHAR(500),                -- LLM 清洗后版本 markdown
    raw_char_count INTEGER,
    cleaned_char_count INTEGER,                  -- 字数过滤用此字段

    domain_tier VARCHAR(20) DEFAULT 'gray'
        CHECK (domain_tier IN ('whitelist', 'gray', 'blacklist')),
    cleanliness_score INTEGER,                   -- 0-100, LLM 评分 (清洗后再评)
    score_reason TEXT,                           -- LLM 给分的理由

    -- 清洗状态机 (独立)
    clean_status VARCHAR(20) DEFAULT 'pending'
        CHECK (clean_status IN ('pending', 'cleaned', 'failed')),
    clean_model VARCHAR(50),                     -- qwen-turbo / qwen3-max
    clean_attempts SMALLINT DEFAULT 0,           -- 重洗次数 (上限 3)
    last_cleaned_at TIMESTAMPTZ,

    -- 审核状态机 (独立, 跟 clean_status 正交)
    review_status VARCHAR(20) DEFAULT 'crawled'
        CHECK (review_status IN ('crawled', 'pending_review', 'auto_skipped', 'approved', 'rejected')),
    review_note TEXT,
    reviewed_by VARCHAR(50),
    reviewed_at TIMESTAMPTZ,
    reference_article_id BIGINT,                 -- approved 时填: 对应 reference_articles.id; FK 在表末尾按需加(reference_articles 是另一子系统的表,可能未先建)

    first_seen_round_id VARCHAR(50),             -- 反向冗余,从 citations 表也能查
    last_seen_at TIMESTAMPTZ DEFAULT NOW(),      -- 最近一次被引用的时间
    fetched_at TIMESTAMPTZ DEFAULT NOW(),
    expired BOOLEAN DEFAULT FALSE,               -- 180 天后软删标记
    expired_at TIMESTAMPTZ,                      -- 标记 expired 的时间 (debug 用)

    -- 行级锁字段 (审核操作并发保护)
    locked_by VARCHAR(50),                       -- 当前持锁的 admin id
    locked_at TIMESTAMPTZ,                       -- 锁获取时间, 超过 5min 自动释放

    -- 正文去重 (优化 2)
    content_hash CHAR(64),                       -- sha256(cleaned_content[:1000]),正文去重用
    is_duplicate BOOLEAN DEFAULT FALSE,          -- 是否为转载/重复正文
    primary_article_id BIGINT REFERENCES geo_research_articles(id) ON DELETE SET NULL,
                                                 -- 若 is_duplicate=true,指向首发 article 的 id; 自引用,删主时置 NULL

    -- v3/v4 运行时字段
    total_citation_count INTEGER NOT NULL DEFAULT 0, -- 累计被引用次数,stage 7 前重算
    score_attempts SMALLINT NOT NULL DEFAULT 0       -- LLM 评分失败次数,满阈值后 auto_skipped
);
-- @index-guard idx_geo_research_articles_review_status_score ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_review_status_score' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_review_status_score' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_review_status_score 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_review_status_score' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_review_status_score ON public.geo_research_articles (review_status, cleanliness_score DESC);
    END IF;
END $idxguard$;
COMMENT ON INDEX idx_geo_research_articles_review_status_score IS '复合索引,前缀已覆盖按 review_status 单独查';
-- @index-guard idx_geo_research_articles_clean_status ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_clean_status' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_clean_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_clean_status 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_clean_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_clean_status ON public.geo_research_articles (clean_status);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_articles_domain ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_domain' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_domain' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_domain 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_domain' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_domain ON public.geo_research_articles (domain);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_articles_industry ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_industry' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_industry' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_industry 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_industry' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_industry ON public.geo_research_articles (primary_industry);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_articles_expired_fetched ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_expired_fetched' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_expired_fetched' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_expired_fetched 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_expired_fetched' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_expired_fetched ON public.geo_research_articles (expired, fetched_at);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_articles_locked ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_locked' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_locked' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_locked 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_locked' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_locked ON public.geo_research_articles (locked_by) WHERE locked_by IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_articles_content_hash ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_content_hash' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_content_hash' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_content_hash 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_content_hash' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_content_hash ON public.geo_research_articles (content_hash) WHERE content_hash IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_articles_primary_article ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_primary_article' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_primary_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_primary_article 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_primary_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_primary_article ON public.geo_research_articles (primary_article_id) WHERE primary_article_id IS NOT NULL;
    END IF;
END $idxguard$;

-- 老库幂等升级: CREATE TABLE IF NOT EXISTS 不会给既有表补新增列。
-- 必须先补列,再创建/注释依赖这些列的 v3/v4 索引与字段。
ALTER TABLE geo_research_articles
    ADD COLUMN IF NOT EXISTS total_citation_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE geo_research_articles
    ADD COLUMN IF NOT EXISTS score_attempts SMALLINT NOT NULL DEFAULT 0;

-- @index-guard idx_geo_research_articles_pending_top ON geo_research_articles plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_articles_pending_top' AND i.indrelid = to_regclass('public.geo_research_articles')) THEN
        NULL;  -- 已在 public.geo_research_articles 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_articles_pending_top' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_articles_pending_top 已存在但不在 public.geo_research_articles 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_articles_pending_top' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_articles_pending_top ON public.geo_research_articles (review_status, total_citation_count DESC, cleaned_char_count DESC) WHERE review_status = 'pending_review';
    END IF;
END $idxguard$;
COMMENT ON TABLE geo_research_articles IS 'GEO 调研监测 - 爬取的文章 (URL 全局唯一,跨轮命中复用)';
COMMENT ON COLUMN geo_research_articles.content_hash IS 'sha256(cleaned_content[:1000]),用于正文去重';
COMMENT ON COLUMN geo_research_articles.is_duplicate IS '是否为转载/重复正文,审核默认只显示 is_duplicate=false';
COMMENT ON COLUMN geo_research_articles.primary_article_id IS '若 is_duplicate=true,指向首发 article 的 id';
COMMENT ON COLUMN geo_research_articles.locked_by IS '审核操作行级锁:当前持锁 admin id,>5min 自动释放';
COMMENT ON COLUMN geo_research_articles.total_citation_count IS '累计被引用次数(跨多轮多 AI),由 round_runner stage 7 之前重算';
COMMENT ON COLUMN geo_research_articles.score_attempts IS '累计 LLM 评分失败次数,满阈值后 review_status 软着 auto_skipped 防卡死';

-- ============================================================
-- 6. 文章引用关联表 (多对多,每轮新增,article 不重复爬)
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_article_citations (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES geo_research_articles(id) ON DELETE CASCADE,
    round_id VARCHAR(50) NOT NULL REFERENCES geo_research_round(round_id) ON DELETE CASCADE,
    industry_id BIGINT REFERENCES geo_research_industries(id),
    prompt_id BIGINT REFERENCES geo_research_prompts(id),
    platform VARCHAR(20) NOT NULL
        CHECK (platform IN ('doubao', 'deepseek', 'qwen', 'kimi')),
    raw_id BIGINT,                               -- 关联到 geo_research_raw 的 id (可选)
    rank_in_response SMALLINT,                   -- AI 回答里的引用次序
    cited_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (article_id, round_id, prompt_id, platform)  -- 同一 article 在同一 round/prompt/platform 不重复
);
-- @index-guard idx_geo_research_citations_article ON geo_research_article_citations plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_citations_article' AND i.indrelid = to_regclass('public.geo_research_article_citations')) THEN
        NULL;  -- 已在 public.geo_research_article_citations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_citations_article' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_citations_article 已存在但不在 public.geo_research_article_citations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_citations_article' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_citations_article ON public.geo_research_article_citations (article_id);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_citations_round ON geo_research_article_citations plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_citations_round' AND i.indrelid = to_regclass('public.geo_research_article_citations')) THEN
        NULL;  -- 已在 public.geo_research_article_citations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_citations_round' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_citations_round 已存在但不在 public.geo_research_article_citations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_citations_round' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_citations_round ON public.geo_research_article_citations (round_id);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_citations_round_platform ON geo_research_article_citations plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_citations_round_platform' AND i.indrelid = to_regclass('public.geo_research_article_citations')) THEN
        NULL;  -- 已在 public.geo_research_article_citations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_citations_round_platform' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_citations_round_platform 已存在但不在 public.geo_research_article_citations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_citations_round_platform' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_citations_round_platform ON public.geo_research_article_citations (round_id, platform);
    END IF;
END $idxguard$;
COMMENT ON TABLE geo_research_article_citations IS 'GEO 调研监测 - 文章引用关联表 (多对多,每轮新增)';

-- ============================================================
-- 7. 审核日志 (审计 + 30 分钟撤销窗口实现)
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_review_log (
    id BIGSERIAL PRIMARY KEY,
    article_id BIGINT NOT NULL REFERENCES geo_research_articles(id) ON DELETE RESTRICT,
                                                 -- RESTRICT: 防误删 article 把审计记录带走
    action VARCHAR(20) NOT NULL
        CHECK (action IN ('approve', 'approve_edited', 'reject', 'reclean', 'undo', 'bulk_approve', 'bulk_reject')),
    prev_review_status VARCHAR(20) NOT NULL,     -- 操作前的状态(撤销用)
    new_review_status VARCHAR(20) NOT NULL,
    reference_article_id_before BIGINT,          -- approve 前若已有 ref 则保留(异常修正用)
    reference_article_id_after BIGINT,           -- approve 时新创建的 ref id
    reason VARCHAR(40),                          -- reject 原因 dropdown 值
    note TEXT,                                   -- admin 备注
    edited_content_oss_key VARCHAR(500),         -- approve_edited 时存 OSS 留底
    operator_id VARCHAR(50) NOT NULL,            -- admin user id
    operator_name VARCHAR(100),                  -- 冗余, 防 user 被删后追溯
    operated_at TIMESTAMPTZ DEFAULT NOW(),
    bulk_id VARCHAR(50),                         -- 批量操作 ID (同一次 bulk-action 的 N 条 log 共享同一 bulk_id),单条操作为 NULL
    undone_by VARCHAR(50),                       -- 该操作是否已被撤销
    undone_at TIMESTAMPTZ
);
-- @index-guard idx_geo_research_review_log_article_time ON geo_research_review_log plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_review_log_article_time' AND i.indrelid = to_regclass('public.geo_research_review_log')) THEN
        NULL;  -- 已在 public.geo_research_review_log 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_review_log_article_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_review_log_article_time 已存在但不在 public.geo_research_review_log 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_review_log_article_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_review_log_article_time ON public.geo_research_review_log (article_id, operated_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_review_log_operator_time ON geo_research_review_log plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_review_log_operator_time' AND i.indrelid = to_regclass('public.geo_research_review_log')) THEN
        NULL;  -- 已在 public.geo_research_review_log 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_review_log_operator_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_review_log_operator_time 已存在但不在 public.geo_research_review_log 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_review_log_operator_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_review_log_operator_time ON public.geo_research_review_log (operator_id, operated_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_review_log_undoable ON geo_research_review_log plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_review_log_undoable' AND i.indrelid = to_regclass('public.geo_research_review_log')) THEN
        NULL;  -- 已在 public.geo_research_review_log 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_review_log_undoable' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_review_log_undoable 已存在但不在 public.geo_research_review_log 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_review_log_undoable' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_review_log_undoable ON public.geo_research_review_log (operated_at, undone_by) WHERE undone_by IS NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_review_log_bulk_id ON geo_research_review_log plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_review_log_bulk_id' AND i.indrelid = to_regclass('public.geo_research_review_log')) THEN
        NULL;  -- 已在 public.geo_research_review_log 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_review_log_bulk_id' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_review_log_bulk_id 已存在但不在 public.geo_research_review_log 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_review_log_bulk_id' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_review_log_bulk_id ON public.geo_research_review_log (bulk_id) WHERE bulk_id IS NOT NULL;
    END IF;
END $idxguard$;
COMMENT ON TABLE geo_research_review_log IS 'GEO 调研监测 - 审核日志 (审计 + 30 分钟撤销窗口实现)';
COMMENT ON COLUMN geo_research_review_log.bulk_id IS '批量操作 ID,同一次 bulk-action 的 N 条 log 共享,单条操作为 NULL';

-- ============================================================
-- 8. 跑批成本日志表(优化 4 预算熔断用)
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_cost_log (
    id BIGSERIAL PRIMARY KEY,
    round_id VARCHAR(50),
    item VARCHAR(50) NOT NULL,
    amount_yuan DECIMAL(10, 4) NOT NULL,
    request_count INTEGER DEFAULT 0,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);
-- @index-guard idx_geo_research_cost_log_round ON geo_research_cost_log plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_cost_log_round' AND i.indrelid = to_regclass('public.geo_research_cost_log')) THEN
        NULL;  -- 已在 public.geo_research_cost_log 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_cost_log_round' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_cost_log_round 已存在但不在 public.geo_research_cost_log 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_cost_log_round' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_cost_log_round ON public.geo_research_cost_log (round_id);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_research_cost_log_recorded_at ON geo_research_cost_log plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_research_cost_log_recorded_at' AND i.indrelid = to_regclass('public.geo_research_cost_log')) THEN
        NULL;  -- 已在 public.geo_research_cost_log 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_research_cost_log_recorded_at' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_research_cost_log_recorded_at 已存在但不在 public.geo_research_cost_log 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_research_cost_log_recorded_at' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_research_cost_log_recorded_at ON public.geo_research_cost_log (recorded_at);
    END IF;
END $idxguard$;
-- 月度汇总查询用 WHERE recorded_at >= date_trunc('month', NOW()) AND recorded_at < date_trunc('month', NOW()) + interval '1 month',走该索引
COMMENT ON TABLE geo_research_cost_log IS 'GEO 调研监测 - 跑批成本日志(预算熔断 + 月度汇总用)';

-- ============================================================
-- 9. 系统配置表(熔断阈值 / 月度预算上限 / TTL 等可调参数)
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_config (
    key VARCHAR(100) PRIMARY KEY,
    value_json JSONB NOT NULL,
    description TEXT,
    updated_by VARCHAR(50),
    updated_at TIMESTAMPTZ DEFAULT NOW()         -- 由应用层 UPDATE 时维护(无 trigger)
);
COMMENT ON TABLE geo_research_config IS 'GEO 调研监测 - 系统配置(熔断阈值/预算上限/TTL 等运行时可调参数)';
COMMENT ON COLUMN geo_research_config.updated_at IS '由应用层 UPDATE 时维护(无 trigger)';

-- 默认配置项
INSERT INTO geo_research_config (key, value_json, description) VALUES
    ('circuit_breaker_consecutive', '50', '连续失败 N 次熔断'),
    ('circuit_breaker_rate', '0.5', '整体失败率阈值(超过即熔断)'),
    ('circuit_breaker_min_processed', '100', '至少跑了 N 次才开始判失败率'),
    ('budget_per_round_yuan', '350', '单轮预算上限(元)'),
    ('budget_per_month_yuan', '1000', '月度预算上限(元)'),
    ('article_oss_ttl_days', '180', 'OSS 文章保留天数'),
    ('clean_attempts_max', '3', '单篇文章最大重洗次数'),
    ('undo_window_minutes', '30', '撤销操作时间窗口(分钟)'),
    ('lock_stale_minutes', '5', '行级锁 stale 自动释放时间(分钟)'),
    ('article_min_chars_for_review', '3000', '文章清洗后最少字数才推送审核')
ON CONFLICT (key) DO NOTHING;

-- ============================================================
-- 条件 FK: reference_articles 由另一子系统 (writing_hall) 创建
-- 该表存在时才补 FK;不存在时仅留 column,不阻塞本 migration
-- ============================================================
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'reference_articles')
       AND NOT EXISTS (
           SELECT 1 FROM information_schema.table_constraints
           WHERE constraint_name = 'fk_geo_research_articles_reference_article_id'
       )
    THEN
        ALTER TABLE geo_research_articles
            ADD CONSTRAINT fk_geo_research_articles_reference_article_id
            FOREIGN KEY (reference_article_id) REFERENCES reference_articles(id) ON DELETE SET NULL;
    END IF;
END$$;

-- ============================================================
-- migration marker 登记 (幂等)
-- ============================================================
CREATE TABLE IF NOT EXISTS _migration_markers (
    marker TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    note TEXT
);
INSERT INTO _migration_markers (marker, applied_at, note)
VALUES ('research_monitor_v1_initial', NOW(), 'GEO 调研监测 9 张表初版 (industries/prompts/round/round_call/articles/citations/review_log/cost_log/config)')
ON CONFLICT (marker) DO NOTHING;

COMMIT;
