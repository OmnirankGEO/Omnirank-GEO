-- migration_geo_research_selfserve_2026_07_05.sql
-- R 批 (GEO 调研自助) · U1 基础层 · 代理自助单行业调研 schema 地基
-- 创建时间: 2026-07-05
-- 来源: 已验证 blob 45973f82c3262b20e9e4ceb40cde0d3f4216960b
-- 内容: 3 块 —— (a) 扩 triggered_by CHECK 加 'selfserve'
--                (b) NEW geo_research_selfserve_queue (自助任务/队列 · P0-2 全字段持久化)
--                (c) NEW geo_research_industry_aliases (P1-6 行业归并别名沉淀)
--
-- 纪律: 全部 additive · 幂等可重跑 (CREATE IF NOT EXISTS / DROP CONSTRAINT IF EXISTS / ALTER IF EXISTS)
--       不 DROP/TRUNCATE 数据表 · 不碰 billing/connection/auth · 不碰四张公共池表
--       queue/aliases 是任务/映射表(非公共素材池),可含 user_id

BEGIN;

-- ============================================================
-- (a) 扩 geo_research_round.triggered_by CHECK 加 'selfserve'
--     表已存在(生产) → 必须显式 ALTER,CREATE IF NOT EXISTS 不会改既有表约束。
--     约束是 inline 匿名 CHECK → Postgres 自动名 = geo_research_round_triggered_by_check。
--     ALTER TABLE IF EXISTS: 冷库/CI 未建表时静默跳过,生产已存在表时正常 DROP+ADD。
-- ============================================================
ALTER TABLE IF EXISTS geo_research_round
    DROP CONSTRAINT IF EXISTS geo_research_round_triggered_by_check;
ALTER TABLE IF EXISTS geo_research_round
    ADD CONSTRAINT geo_research_round_triggered_by_check
    CHECK (triggered_by IN ('cron', 'manual', 'missed_cron_recovery', 'selfserve'));

-- ============================================================
-- (b) NEW geo_research_selfserve_queue —— 自助调研任务/队列
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_selfserve_queue (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    brand_id BIGINT,
    industry_raw TEXT NOT NULL,
    industry_id BIGINT,
    industry_key TEXT,
    round_id VARCHAR(50),
    freeze_id BIGINT,
    freeze_table VARCHAR(20),
    billing_exempt BOOLEAN DEFAULT FALSE,
    price_points INTEGER NOT NULL,
    prompt_snapshot JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'queued', 'running', 'completed', 'failed', 'timeout', 'cancelled')),
    failed_reason TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
ALTER TABLE IF EXISTS geo_research_selfserve_queue
    ADD COLUMN IF NOT EXISTS billing_exempt BOOLEAN DEFAULT FALSE;
ALTER TABLE IF EXISTS geo_research_selfserve_queue
    DROP CONSTRAINT IF EXISTS geo_research_selfserve_queue_status_check;
ALTER TABLE IF EXISTS geo_research_selfserve_queue
    ADD CONSTRAINT geo_research_selfserve_queue_status_check
    CHECK (status IN ('pending', 'queued', 'running', 'completed', 'failed', 'timeout', 'cancelled'));
-- @drop-index-guard uq_selfserve_active_idem ON geo_research_selfserve_queue if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_selfserve_active_idem' AND i.indrelid = to_regclass('geo_research_selfserve_queue')) THEN
        DROP INDEX IF EXISTS uq_selfserve_active_idem;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_selfserve_active_idem' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] uq_selfserve_active_idem 不在 geo_research_selfserve_queue 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_selfserve_active_idem' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
-- @index-guard uq_selfserve_active_idem ON geo_research_selfserve_queue unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_selfserve_active_idem' AND i.indrelid = to_regclass('public.geo_research_selfserve_queue')) THEN
        NULL;  -- 已在 public.geo_research_selfserve_queue 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_selfserve_active_idem' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_selfserve_active_idem 已存在但不在 public.geo_research_selfserve_queue 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_selfserve_active_idem' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_selfserve_active_idem ON public.geo_research_selfserve_queue (idempotency_key) WHERE status IN ('pending', 'queued', 'running');
    END IF;
END $idxguard$;
-- @index-guard idx_selfserve_user ON geo_research_selfserve_queue plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_selfserve_user' AND i.indrelid = to_regclass('public.geo_research_selfserve_queue')) THEN
        NULL;  -- 已在 public.geo_research_selfserve_queue 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_selfserve_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_selfserve_user 已存在但不在 public.geo_research_selfserve_queue 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_selfserve_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_selfserve_user ON public.geo_research_selfserve_queue (user_id);
    END IF;
END $idxguard$;
-- @index-guard idx_selfserve_industry_key ON geo_research_selfserve_queue plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_selfserve_industry_key' AND i.indrelid = to_regclass('public.geo_research_selfserve_queue')) THEN
        NULL;  -- 已在 public.geo_research_selfserve_queue 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_selfserve_industry_key' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_selfserve_industry_key 已存在但不在 public.geo_research_selfserve_queue 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_selfserve_industry_key' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_selfserve_industry_key ON public.geo_research_selfserve_queue (industry_key);
    END IF;
END $idxguard$;
-- @index-guard idx_selfserve_status ON geo_research_selfserve_queue plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_selfserve_status' AND i.indrelid = to_regclass('public.geo_research_selfserve_queue')) THEN
        NULL;  -- 已在 public.geo_research_selfserve_queue 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_selfserve_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_selfserve_status 已存在但不在 public.geo_research_selfserve_queue 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_selfserve_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_selfserve_status ON public.geo_research_selfserve_queue (status);
    END IF;
END $idxguard$;
COMMENT ON TABLE geo_research_selfserve_queue IS 'R 批 - 代理自助单行业调研任务队列(P0-2 全字段持久化 · 崩溃可靠 commit/release)';
COMMENT ON COLUMN geo_research_selfserve_queue.industry_key IS '归一键,与发布榜查询/验收同源';
COMMENT ON COLUMN geo_research_selfserve_queue.freeze_table IS '冻结表标识(legacy|v35),worker commit/release 回传 billing 免猜表';
COMMENT ON COLUMN geo_research_selfserve_queue.billing_exempt IS '[R#1] admin/零成本合法免费;worker 据此区分「freeze_id 缺失=孤儿(不应免费跑)」vs「豁免(合法免费完成)」';
COMMENT ON COLUMN geo_research_selfserve_queue.status IS '[R#1] pending=不可派发(冻结回填+promote 前);queued=可派发;running/completed/failed/timeout/cancelled';
COMMENT ON COLUMN geo_research_selfserve_queue.idempotency_key IS '幂等键;活跃态(pending/queued/running)由 uq_selfserve_active_idem 保唯一防双击';

-- ============================================================
-- (c) NEW geo_research_industry_aliases —— 行业归并别名沉淀
-- ============================================================
CREATE TABLE IF NOT EXISTS geo_research_industry_aliases (
    id BIGSERIAL PRIMARY KEY,
    normalized_alias TEXT NOT NULL UNIQUE,
    industry_id BIGINT NOT NULL REFERENCES geo_research_industries(id),
    confidence NUMERIC(4,3),
    resolved_by VARCHAR(10) NOT NULL
        CHECK (resolved_by IN ('llm', 'admin')),
    reviewed_by BIGINT,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
-- @index-guard idx_selfserve_alias_industry ON geo_research_industry_aliases plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_selfserve_alias_industry' AND i.indrelid = to_regclass('public.geo_research_industry_aliases')) THEN
        NULL;  -- 已在 public.geo_research_industry_aliases 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_selfserve_alias_industry' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_selfserve_alias_industry 已存在但不在 public.geo_research_industry_aliases 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_selfserve_alias_industry' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_selfserve_alias_industry ON public.geo_research_industry_aliases (industry_id);
    END IF;
END $idxguard$;
-- @index-guard idx_selfserve_alias_active ON geo_research_industry_aliases plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_selfserve_alias_active' AND i.indrelid = to_regclass('public.geo_research_industry_aliases')) THEN
        NULL;  -- 已在 public.geo_research_industry_aliases 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_selfserve_alias_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_selfserve_alias_active 已存在但不在 public.geo_research_industry_aliases 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_selfserve_alias_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_selfserve_alias_active ON public.geo_research_industry_aliases (active) WHERE active;
    END IF;
END $idxguard$;
COMMENT ON TABLE geo_research_industry_aliases IS 'R 批 - 行业归并别名沉淀(P1-6 · 用户行业原文标准化 → 标准 geo_research_industries)';
COMMENT ON COLUMN geo_research_industry_aliases.normalized_alias IS 'resolver 查找键 = 应用层 _normalize_alias_text(去空白/全半角/lower) 后的原文';
COMMENT ON COLUMN geo_research_industry_aliases.updated_at IS '由应用层 UPDATE 时维护(无 trigger)';

-- 三个资金/派发关键 CHECK 必须存在且已验证；NOT VALID 或缺约束均阻断 prestart。
DO $$
DECLARE
    invalid_constraints TEXT[];
BEGIN
    SELECT ARRAY_AGG(expected.name ORDER BY expected.name)
      INTO invalid_constraints
      FROM (VALUES
          ('geo_research_round_triggered_by_check', 'geo_research_round'::regclass),
          ('geo_research_selfserve_queue_status_check', 'geo_research_selfserve_queue'::regclass),
          ('geo_research_industry_aliases_resolved_by_check', 'geo_research_industry_aliases'::regclass)
      ) AS expected(name, relation_oid)
     WHERE NOT EXISTS (
          SELECT 1
            FROM pg_constraint c
           WHERE c.conname = expected.name
             AND c.conrelid = expected.relation_oid
             AND c.contype = 'c'
             AND c.convalidated = TRUE
     );

    IF invalid_constraints IS NOT NULL THEN
        RAISE EXCEPTION 'selfserve schema CHECK missing or NOT VALID: %', invalid_constraints;
    END IF;
END $$;

-- ============================================================
-- migration marker 登记 (幂等)
-- ============================================================
CREATE TABLE IF NOT EXISTS _migration_markers (
    marker TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    note TEXT
);
INSERT INTO _migration_markers (marker, applied_at, note)
VALUES ('geo_research_selfserve_v1_2026_07_05', NOW(),
        'R 批 U1: triggered_by+selfserve · geo_research_selfserve_queue · geo_research_industry_aliases')
ON CONFLICT (marker) DO NOTHING;

COMMIT;
