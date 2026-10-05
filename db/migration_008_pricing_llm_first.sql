-- =====================================================================
-- Migration 008 v2 · 报价管线 LLM-first 改造支撑层
-- =====================================================================
-- 老板 2026-05-12 Codex round-3 P0 拍板:
--   独立表 keyword_price_cache_llm · 不动老 keyword_price_cache
--   理由:DB migration 后切老镜像 · 老代码不知道新表存在 · 完全无感
--        代码 rollback 不再依赖 DB rollback · 部署回滚安全
--
-- 范围:
--   1. system_settings 表(若不存在 · 兼容建表)
--   2. CREATE TABLE keyword_price_cache_llm(独立表)
--   3. system_settings 5 个 feature flag(默认 OFF · 不影响生产)
--
-- 老 keyword_price_cache 表 · 0 改动 · 0 影响
-- 幂等 · 重跑 0 ERROR
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. system_settings 表(若 V3.3.1 migration 未上 · 兼容建表)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
    key          VARCHAR(100) PRIMARY KEY,
    value        TEXT,
    value_type   VARCHAR(20) DEFAULT 'string',
    description  TEXT,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by   INTEGER
);

-- ---------------------------------------------------------------------
-- 2. keyword_price_cache_llm · LLM-first 独立 cache 表
--    Codex round-3 P0:独立表防 migration 让代码 rollback 失效
--    Codex round-3 P1:加 business_scope_hash 防跨 business_scope 串台
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS keyword_price_cache_llm (
    id                   SERIAL PRIMARY KEY,
    brand_name           TEXT NOT NULL,
    keyword              TEXT NOT NULL,
    industry             TEXT,
    city                 TEXT,
    -- round-3 P1:防跨 business_scope 污染
    -- round-4 P2:NOT NULL DEFAULT 'no_scope' DB 层硬约束(Postgres NULL ≠ NULL · UNIQUE 会允许重复 NULL · 加 NOT NULL 让 UNIQUE 真正生效)
    business_scope_hash  TEXT NOT NULL DEFAULT 'no_scope',
    -- LLM 输出主字段
    intent               TEXT,
    funnel_stage         TEXT,
    value_score          REAL,                     -- 0-5 浮点
    entry_price          INTEGER,                  -- 整数元 · -1 = 不报价
    standard_price       INTEGER,
    flagship_price       INTEGER,
    should_quote         BOOLEAN NOT NULL DEFAULT TRUE,
    llm_reason_zh        TEXT,
    business_line        TEXT,
    needs_review         BOOLEAN NOT NULL DEFAULT FALSE,
    review_reason        TEXT,
    -- 时间管理
    cached_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at           TIMESTAMP,
    -- UNIQUE 单品牌 + 关键词 + business_scope_hash 隔离
    -- Phase 2 先 brand-specific · 跨品牌不复用(老板 round-3 拍板)
    UNIQUE (brand_name, keyword, business_scope_hash)
);

-- @index-guard idx_kpc_llm_brand_keyword ON keyword_price_cache_llm plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_kpc_llm_brand_keyword' AND i.indrelid = to_regclass('public.keyword_price_cache_llm')) THEN
        NULL;  -- 已在 public.keyword_price_cache_llm 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_kpc_llm_brand_keyword' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_kpc_llm_brand_keyword 已存在但不在 public.keyword_price_cache_llm 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_kpc_llm_brand_keyword' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_kpc_llm_brand_keyword ON public.keyword_price_cache_llm (brand_name, keyword);
    END IF;
END $idxguard$;

-- @index-guard idx_kpc_llm_expires ON keyword_price_cache_llm plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_kpc_llm_expires' AND i.indrelid = to_regclass('public.keyword_price_cache_llm')) THEN
        NULL;  -- 已在 public.keyword_price_cache_llm 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_kpc_llm_expires' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_kpc_llm_expires 已存在但不在 public.keyword_price_cache_llm 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_kpc_llm_expires' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_kpc_llm_expires ON public.keyword_price_cache_llm (expires_at);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------
-- 3. Feature flags(默认 OFF · 不影响生产)
-- ---------------------------------------------------------------------
INSERT INTO system_settings (key, value, value_type, description) VALUES
    ('LLM_FIRST_PRICING_ENABLED', 'false', 'bool',
     'LLM-first 报价总开关 · false 时走老公式分支 · 老板 2026-05-12 P0'),
    ('LLM_FIRST_PRICING_AGENT_WHITELIST', '[]', 'json',
     'LLM-first 灰度代理白名单 · JSON 数组 user_id list · 空数组 = 不灰度'),
    ('LLM_FIRST_PRICING_FALLBACK_TO_FORMULA', 'true', 'bool',
     'LLM 失败时是否降级到老公式 · 默认 true 不阻断业务'),
    ('LLM_FIRST_PRICING_SOFT_GUARD_MIN_YUAN', '300', 'int',
     '价格软护栏下限(标准版) · 低于此值 needs_review=true · 不覆盖判断'),
    ('LLM_FIRST_PRICING_SOFT_GUARD_MAX_YUAN', '8000', 'int',
     '价格软护栏上限(标准版) · 高于此值 needs_review=true · 不覆盖判断')
ON CONFLICT (key) DO UPDATE SET
    description = EXCLUDED.description,
    updated_at = CURRENT_TIMESTAMP;

COMMIT;

-- =====================================================================
-- 验收 SQL(部署后跑)
-- =====================================================================
-- 1. 新独立表已建
--    \d keyword_price_cache_llm · 列含 brand_name / keyword / business_scope_hash /
--    intent / funnel_stage / value_score / entry_price / standard_price / flagship_price /
--    should_quote / llm_reason_zh / business_line / needs_review / cached_at / expires_at
-- 2. 复合 UNIQUE 已建
--    SELECT conname FROM pg_constraint WHERE conrelid='keyword_price_cache_llm'::regclass AND contype='u';
--    必须见 (brand_name, keyword, business_scope_hash) UNIQUE
-- 3. flag 已注入
--    SELECT key, value FROM system_settings WHERE key LIKE 'LLM_FIRST_PRICING%';
--    必须见 5 行 · 全 default 安全值
-- 4. 老 keyword_price_cache 表完全不变
--    \d keyword_price_cache · UNIQUE 仍是 (brand_name, keyword) · 无 LLM 字段
--    SELECT conname FROM pg_constraint WHERE conrelid='keyword_price_cache'::regclass AND contype='u';
--    必须见 (brand_name, keyword) UNIQUE · 不见 pricing_engine_version

-- =====================================================================
-- 回滚 SQL · rollback_008_pricing_llm_first.sql
-- DROP TABLE keyword_price_cache_llm + 关 flag · 老表 0 影响
-- 代码回滚不依赖 DB rollback · 老镜像 + 新 DB schema 共存安全
-- =====================================================================
