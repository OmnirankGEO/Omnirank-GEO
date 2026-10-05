-- P0-0 媒体成本版本化快照表(2026-06-13)
-- 纯新增 ADD TABLE · 幂等(CREATE IF NOT EXISTS)· 不触碰任何报价/扣费表。
-- append-only:snapshot_version 含内容 hash(tools/media_cost_ssot.build_snapshot_payload),
--   同内容→同 version(写入幂等 no-op);不同内容→不同 version(新行)。禁止同 version 静默覆盖不同内容。

CREATE TABLE IF NOT EXISTS media_cost_snapshot (
    id              SERIAL PRIMARY KEY,
    snapshot_version TEXT NOT NULL,
    schema_version  TEXT NOT NULL DEFAULT 'mcs_v1',
    source          TEXT NOT NULL DEFAULT '',
    generated_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    payload         JSONB NOT NULL,
    payload_sha256  TEXT,
    row_count       INTEGER,
    geo_count       INTEGER,
    platform_media_markup NUMERIC(6,3),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

-- @index-guard uq_media_cost_snapshot_version ON media_cost_snapshot unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_media_cost_snapshot_version' AND i.indrelid = to_regclass('public.media_cost_snapshot')) THEN
        NULL;  -- 已在 public.media_cost_snapshot 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_media_cost_snapshot_version' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_media_cost_snapshot_version 已存在但不在 public.media_cost_snapshot 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_media_cost_snapshot_version' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_media_cost_snapshot_version ON public.media_cost_snapshot (snapshot_version);
    END IF;
END $idxguard$;
-- @index-guard idx_media_cost_snapshot_active ON media_cost_snapshot plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_media_cost_snapshot_active' AND i.indrelid = to_regclass('public.media_cost_snapshot')) THEN
        NULL;  -- 已在 public.media_cost_snapshot 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_media_cost_snapshot_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_media_cost_snapshot_active 已存在但不在 public.media_cost_snapshot 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_media_cost_snapshot_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_media_cost_snapshot_active ON public.media_cost_snapshot (is_active, generated_at DESC);
    END IF;
END $idxguard$;

COMMENT ON TABLE media_cost_snapshot IS
    'P0-0 媒体成本版本化快照(append-only · snapshot_version 含内容 hash · 禁同版本静默覆盖不同内容 · 来源 mhz_media GEO 池)';
