-- ============================================================================
-- 045 · 防御型 GEO 正式诊断 run 的**执行派发标记**(门三 G9)
-- ============================================================================
-- 事故形态(门库实证 2026-08-22):
--   confirm 把 run 推进 running、冻住 7800 算力,**然后没有任何执行器接手**。
--   run_b6ad4e77… 在 running 态挂了 23 分钟,心跳一次没动过,钱一直冻着。
--   现役 sweeper 本来会在心跳超 5 分钟时收成 release_pending 退款 ——
--   但那是"收尸",不是"干活":客户付了钱,报告永远不会出现。
--
-- 本迁移只加**两列**,让执行器能原子领取一条"已确认但从没人执行"的 run:
--   defgeo_dispatched_at      §12.3 的 canonical external-start marker。
--                             「恢复时任一 marker 存在均不得盲目二次外调」——
--                             领取 = CAS 这一列从 NULL 到 NOW(),天然互斥。
--   defgeo_dispatch_attempts  失败重投计数。**它是让 run 能被收敛的那一半**:
--                             派发反复失败时次数会涨,超阈值后执行器不再领取,
--                             run 于是落回 sweeper 的判死窗口被退款。
--                             没有这个计数,一个坏 run 会被无限重投、永远冻着。
--
-- 🔴 additive-only:两列都 IF NOT EXISTS + 有默认值,既有行不动、既有 CHECK 不改。
-- 🔴 体内零 DML:prestart 每次部署无条件重放全部迁移(无追踪表),
--    迁移里的 DML 是常驻地雷。回填(如果将来需要)走一次性脚本。
-- 🔴 不碰 billing_mode / run_status 两个 CHECK —— 派发是执行面,不是资金面,
--    不许因为它多出第二套状态。
-- ============================================================================

ALTER TABLE diagnosis_runs
    ADD COLUMN IF NOT EXISTS defgeo_dispatched_at TIMESTAMPTZ;

ALTER TABLE diagnosis_runs
    ADD COLUMN IF NOT EXISTS defgeo_dispatch_attempts INTEGER NOT NULL DEFAULT 0;

-- 领取查询的支撑索引:只索引**还没派发过的活跃 run**,索引体积与 run 总量无关。
-- @index-guard idx_diag_runs_defgeo_undispatched ON diagnosis_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_diag_runs_defgeo_undispatched' AND i.indrelid = to_regclass('public.diagnosis_runs')) THEN
        NULL;  -- 已在 public.diagnosis_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_diag_runs_defgeo_undispatched' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_diag_runs_defgeo_undispatched 已存在但不在 public.diagnosis_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_diag_runs_defgeo_undispatched' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_diag_runs_defgeo_undispatched ON public.diagnosis_runs (created_at) WHERE defgeo_dispatched_at IS NULL AND run_status = 'running';
    END IF;
END $idxguard$;

-- ── 反查自证(不是注释,是会 RAISE 的断言)────────────────────────────────
DO $$
DECLARE missing text;
BEGIN
    SELECT string_agg(c, ', ') INTO missing
      FROM unnest(ARRAY['defgeo_dispatched_at', 'defgeo_dispatch_attempts']) AS c
     WHERE NOT EXISTS (
         SELECT 1 FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name = 'diagnosis_runs'
            AND column_name = c
     );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION '[反查] 045 派发列缺失: %', missing;
    END IF;

    -- 🔴 必须绑 tablename:索引名只在 schema 内唯一、**不绑表**。
    --    别的表上有同名索引时,不绑表的这条反查会判"在",而 diagnosis_runs 上
    --    其实一条都没有 —— 领取查询照样全表扫,而 readiness 报绿。
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = 'public' AND indexname = 'idx_diag_runs_defgeo_undispatched'
           AND tablename = 'diagnosis_runs'
    ) THEN
        RAISE EXCEPTION '[反查] 045 领取索引不在 diagnosis_runs 上 —— 领取查询会全表扫';
    END IF;

    -- 派发列**不许**带 NOT NULL 约束到 dispatched_at:NULL 就是"还没派发"这个语义本身。
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'diagnosis_runs'
           AND column_name = 'defgeo_dispatched_at' AND is_nullable = 'NO'
    ) THEN
        RAISE EXCEPTION '[反查] defgeo_dispatched_at 被加了 NOT NULL —— "还没派发"就无法表达了';
    END IF;
END $$;
