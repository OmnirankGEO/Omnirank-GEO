-- ============================================================================
-- 026 · 媒体域名人话目录(media_domain_directory)
--      工单 WO_MEDIA_BOARD_UX_CLOSURE_2026-08-05 §1
--
-- 存在的理由:发布投放媒体榜上 licai.cofool.com / 05wang.com / chinapp.com /
--   news.koolearn.com 这类**裸域名直出**,点击拿裸域名去中文名索引的媒体库里搜必然空结果
--   = 死点击。前后端两份写死映射表(services/media_effectiveness_board.py 的
--   _DOMAIN_DISPLAY_NAMES 40 条 + frontend GENERAL_MEDIA_LABELS 13 条)盖不住长尾。
--
-- 本表 = 域名 → 中文名 + 一句话简介 的**落库缓存**,由 LLM 蒸馏填充(幂等 upsert),
--   读侧只在出榜时 join。纯 additive:表不存在 / 表为空 → 榜完全退化为当前行为
--   (显示裸域名),绝不阻断主链。
--
-- 🔴 zh_name 允许为空串:那是**蒸馏跑过但没能确定**的负缓存(配合 attempts 计数),
--    防止每次出榜都对同一批查不出来的长尾域名重复烧 LLM。读侧只认非空 zh_name。
-- ============================================================================

CREATE TABLE IF NOT EXISTS media_domain_directory (
    domain          VARCHAR(200) PRIMARY KEY,
    zh_name         TEXT        NOT NULL DEFAULT '',
    one_liner       TEXT        NOT NULL DEFAULT '',
    source          VARCHAR(20) NOT NULL DEFAULT 'llm',
    model           VARCHAR(60),
    attempts        SMALLINT    NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- source:'llm' 蒸馏 / 'builtin' 代码内置表回填 / 'admin' 人工订正(人工优先级最高,
-- 蒸馏 upsert 明确跳过 admin 行 —— 见 services/media_domain_directory.upsert_entries)。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'media_domain_directory_source_check'
    ) THEN
        ALTER TABLE media_domain_directory
            ADD CONSTRAINT media_domain_directory_source_check
            CHECK (source IN ('llm', 'builtin', 'admin'));
    END IF;
END $$;

-- 既存表(早批 bootstrap 建过窄 schema)补列,与本文件同步。
ALTER TABLE media_domain_directory ADD COLUMN IF NOT EXISTS one_liner       TEXT        NOT NULL DEFAULT '';
ALTER TABLE media_domain_directory ADD COLUMN IF NOT EXISTS model           VARCHAR(60);
ALTER TABLE media_domain_directory ADD COLUMN IF NOT EXISTS attempts        SMALLINT    NOT NULL DEFAULT 0;
ALTER TABLE media_domain_directory ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE media_domain_directory ADD COLUMN IF NOT EXISTS updated_at      TIMESTAMPTZ NOT NULL DEFAULT now();

-- 只有"已确定中文名"的行才会被读侧 join 到,单独走部分索引。
-- @index-guard idx_media_domain_directory_named ON media_domain_directory plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_media_domain_directory_named' AND i.indrelid = to_regclass('public.media_domain_directory')) THEN
        NULL;  -- 已在 public.media_domain_directory 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_media_domain_directory_named' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_media_domain_directory_named 已存在但不在 public.media_domain_directory 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_media_domain_directory_named' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_media_domain_directory_named ON public.media_domain_directory (domain) WHERE zh_name <> '';
    END IF;
END $idxguard$;
-- @index-guard idx_media_domain_directory_source ON media_domain_directory plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_media_domain_directory_source' AND i.indrelid = to_regclass('public.media_domain_directory')) THEN
        NULL;  -- 已在 public.media_domain_directory 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_media_domain_directory_source' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_media_domain_directory_source 已存在但不在 public.media_domain_directory 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_media_domain_directory_source' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_media_domain_directory_source ON public.media_domain_directory (source);
    END IF;
END $idxguard$;

COMMENT ON TABLE  media_domain_directory IS '媒体域名人话目录:域名→中文名+一句话简介(LLM 蒸馏落库缓存 · 只服务展示层,不进任何评分)';
COMMENT ON COLUMN media_domain_directory.zh_name IS '中文媒体名;空串 = 蒸馏跑过但未能确定(负缓存,读侧不用)';
COMMENT ON COLUMN media_domain_directory.attempts IS '蒸馏尝试次数,配合空 zh_name 防止长尾域名被反复烧 LLM';
