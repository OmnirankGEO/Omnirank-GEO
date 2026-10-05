-- [R2-2 · 2026-08-15] monitoring_outcome_backfill_journal —— 历史回填旧值账本。
--
-- 91,592 行 legacy 的 target_outcome 回填是**大面积重写历史解释**,且生产回滚
-- 窗口已关闭(§6.9)只能前向修复 —— 每个待改行必须先落 旧值/旧版本/批次 ID/
-- 分类输入摘要,并配可执行恢复脚本(scripts/restore_monitoring_outcome_backfill_
-- 2026_08_15.py)。账本是恢复脚本的**真依赖**:拆掉账本写入,恢复必然失败
--(R2-2 反向判据)。
--
-- 幂等:全 IF NOT EXISTS;无数据 UPDATE(prestart 重放安全)。
-- 漏跑后果**响亮**(刻意):回填脚本无条件 INSERT 本表,缺表 → UndefinedTable
-- 抛出、回填整批失败 —— 「改了历史但没记旧值」正是本单要堵死的形态,
-- 绝不允许静默降级成无账本改写。

CREATE TABLE IF NOT EXISTS monitoring_outcome_backfill_journal (
    id BIGSERIAL PRIMARY KEY,
    batch_id TEXT NOT NULL,
    monitoring_result_id BIGINT NOT NULL,
    old_target_outcome VARCHAR(40),
    old_resolver_confidence NUMERIC(5,4),
    old_resolver_version VARCHAR(80),
    new_target_outcome VARCHAR(40) NOT NULL,
    new_resolver_confidence NUMERIC(5,4),
    new_resolver_version VARCHAR(80) NOT NULL,
    -- 分类输入摘要:不存正文原文,存判定所需的形态位 + full_response sha256/长度
    input_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    restored_at TIMESTAMPTZ
);

-- 同一行同一批只记一次(重跑幂等的账本面);不同批各记各的(历史可追)。
-- @index-guard uq_mob_journal_result_batch ON monitoring_outcome_backfill_journal unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_mob_journal_result_batch' AND i.indrelid = to_regclass('public.monitoring_outcome_backfill_journal')) THEN
        NULL;  -- 已在 public.monitoring_outcome_backfill_journal 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_mob_journal_result_batch' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_mob_journal_result_batch 已存在但不在 public.monitoring_outcome_backfill_journal 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_mob_journal_result_batch' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_mob_journal_result_batch ON public.monitoring_outcome_backfill_journal (monitoring_result_id, batch_id);
    END IF;
END $idxguard$;
-- @index-guard idx_mob_journal_batch ON monitoring_outcome_backfill_journal plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mob_journal_batch' AND i.indrelid = to_regclass('public.monitoring_outcome_backfill_journal')) THEN
        NULL;  -- 已在 public.monitoring_outcome_backfill_journal 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mob_journal_batch' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mob_journal_batch 已存在但不在 public.monitoring_outcome_backfill_journal 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mob_journal_batch' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mob_journal_batch ON public.monitoring_outcome_backfill_journal (batch_id);
    END IF;
END $idxguard$;

COMMENT ON TABLE monitoring_outcome_backfill_journal IS
    'R2-2 历史 outcome 回填旧值账本:恢复脚本按 batch_id 反做;restored_at 记恢复痕。';
