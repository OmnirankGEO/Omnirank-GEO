-- M3 后端第一档 migration · CTO-15.15-M3-Lead · 2026-04-26
-- 任务 2.1 + 2.2 表创建(2.3 复用 audit_logs 不动 schema)
-- 全幂等(IF NOT EXISTS)· 无破坏性 · 可重跑

-- ============================================================
-- m3_quick_records · QuickRecordChip "刚做了什么" 入库
-- ============================================================
CREATE TABLE IF NOT EXISTS m3_quick_records (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    record_type TEXT NOT NULL,
    note TEXT,
    metadata JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- @index-guard idx_m3_quick_records_brand ON m3_quick_records plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_m3_quick_records_brand' AND i.indrelid = to_regclass('public.m3_quick_records')) THEN
        NULL;  -- 已在 public.m3_quick_records 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_m3_quick_records_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_m3_quick_records_brand 已存在但不在 public.m3_quick_records 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_m3_quick_records_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_m3_quick_records_brand ON public.m3_quick_records (brand_id, created_at DESC);
    END IF;
END $idxguard$;

-- @index-guard idx_m3_quick_records_user ON m3_quick_records plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_m3_quick_records_user' AND i.indrelid = to_regclass('public.m3_quick_records')) THEN
        NULL;  -- 已在 public.m3_quick_records 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_m3_quick_records_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_m3_quick_records_user 已存在但不在 public.m3_quick_records 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_m3_quick_records_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_m3_quick_records_user ON public.m3_quick_records (user_id, created_at DESC);
    END IF;
END $idxguard$;

-- ============================================================
-- m3_brand_notes · ClientNoteCard 私人备注(per user × brand)
-- ============================================================
CREATE TABLE IF NOT EXISTS m3_brand_notes (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_m3_brand_notes_brand_user UNIQUE (brand_id, user_id)
);

-- @index-guard idx_m3_brand_notes_brand ON m3_brand_notes plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_m3_brand_notes_brand' AND i.indrelid = to_regclass('public.m3_brand_notes')) THEN
        NULL;  -- 已在 public.m3_brand_notes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_m3_brand_notes_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_m3_brand_notes_brand 已存在但不在 public.m3_brand_notes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_m3_brand_notes_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_m3_brand_notes_brand ON public.m3_brand_notes (brand_id);
    END IF;
END $idxguard$;

-- ============================================================
-- 完成标记
-- ============================================================
SELECT 'M3 后端第一档 schema 迁移完成' AS status, NOW() AS applied_at;
