-- WP12 P2-6 · 两周写作有效性复核报告存档表
--
-- 只存报告本身:一行 = 一期报告快照(JSONB)。本迁移不改任何业务数据、不建触发器、
-- 不翻任何 feature flag,也**不会**写文体配比 —— 配比涉及收入结构,只能由有权限的
-- 人在系统设置里改(Master SSOT v2.4 ⑥ / v2.3 ⑥「只出报告不自动改配比」)。
--
-- 幂等:全部 IF NOT EXISTS,可连跑。

CREATE TABLE IF NOT EXISTS writing_effectiveness_reports (
    id             BIGSERIAL PRIMARY KEY,
    report_key     VARCHAR(80)  NOT NULL,
    report_version VARCHAR(80)  NOT NULL DEFAULT 'geo-writing-effectiveness-report-v1.0',
    window_days    INTEGER      NOT NULL DEFAULT 90,
    industry       VARCHAR(100) NOT NULL DEFAULT '',
    payload        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    generated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- @index-guard uq_writing_effectiveness_reports_key ON writing_effectiveness_reports unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_writing_effectiveness_reports_key' AND i.indrelid = to_regclass('public.writing_effectiveness_reports')) THEN
        NULL;  -- 已在 public.writing_effectiveness_reports 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_writing_effectiveness_reports_key' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_writing_effectiveness_reports_key 已存在但不在 public.writing_effectiveness_reports 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_writing_effectiveness_reports_key' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_writing_effectiveness_reports_key ON public.writing_effectiveness_reports (report_key);
    END IF;
END $idxguard$;
-- @index-guard idx_writing_effectiveness_reports_generated ON writing_effectiveness_reports plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_writing_effectiveness_reports_generated' AND i.indrelid = to_regclass('public.writing_effectiveness_reports')) THEN
        NULL;  -- 已在 public.writing_effectiveness_reports 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_writing_effectiveness_reports_generated' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_writing_effectiveness_reports_generated 已存在但不在 public.writing_effectiveness_reports 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_writing_effectiveness_reports_generated' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_writing_effectiveness_reports_generated ON public.writing_effectiveness_reports (generated_at DESC);
    END IF;
END $idxguard$;

COMMENT ON TABLE writing_effectiveness_reports IS
    'WP12 P2-6 两周复核报告存档(各文体/长度档/域/引擎 vs 引用率与引用排名 + 北极星"我方已发布 URL 被引数");只读观测,不驱动任何自动配比调整';
