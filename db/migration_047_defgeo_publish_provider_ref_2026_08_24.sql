-- ============================================================================
-- 047 · 防御型 GEO 发布命令的**供应商单据引用**(包E · §12.3 窗口③④)
-- ============================================================================
-- 缺这两列会发生什么(不是理论风险,是窗口④的实现前提):
--   worker 外调成功后拿到上游单号,但库里**没有地方放**。于是
--   「provider 已接受」这件事只剩一个 canonical state 字符串,
--   没有任何键能把它和上游那张单对上 ——
--   §12.3 第 4 窗口要求「canonical delivery outcome 已持久化后…
--   reconciler 从该 outcome 原子完成剩余状态」,而 outcome 要从上游取回来,
--   取回来就要有单号。没有单号只剩两条路:盲重传(赌对方幂等)或永远挂着。
--   两条都被 §12.1 逐字禁掉。
--
--   provider_order_ref       上游单号(order_sn)。**不是**给客户看的字段,
--                            只用于回执核对与人工核验(Z-1 队列)。
--   provider_last_polled_at  上一次向上游核对结果的时刻。它让「久无回执」
--                            这个判断有分母 —— 没有它,"轮询过没有"只能靠猜。
--
-- 🔴 additive-only:两列都 IF NOT EXISTS + 可空,既有行不动、既有 CHECK 不改。
-- 🔴 体内零 DML:prestart 每次部署无条件重放全部迁移(无追踪表)。
-- 🔴 不碰 funding_state / command_state / canonical_publication_state 三个 CHECK
--    —— 供应商单号是**外部引用**,不是第四条状态轴。
-- ============================================================================

ALTER TABLE public.defgeo_publish_commands
    ADD COLUMN IF NOT EXISTS provider_order_ref VARCHAR(120);

ALTER TABLE public.defgeo_publish_commands
    ADD COLUMN IF NOT EXISTS provider_last_polled_at TIMESTAMPTZ;

-- 回执轮询的支撑索引:只索引**已外调但还没拿到终态**的命令。
-- @index-guard idx_defgeo_pcmd_awaiting_outcome ON defgeo_publish_commands plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_pcmd_awaiting_outcome' AND i.indrelid = to_regclass('public.defgeo_publish_commands')) THEN
        NULL;  -- 已在 public.defgeo_publish_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_pcmd_awaiting_outcome' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_pcmd_awaiting_outcome 已存在但不在 public.defgeo_publish_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_pcmd_awaiting_outcome' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_pcmd_awaiting_outcome ON public.defgeo_publish_commands (provider_last_polled_at NULLS FIRST) WHERE external_start_at IS NOT NULL AND canonical_publication_state IN ('queued', 'submitting', 'reported_success_unverified');
    END IF;
END $idxguard$;

-- ── 反查自证(不是注释,是会 RAISE 的断言)────────────────────────────────
DO $$
DECLARE missing text;
BEGIN
    SELECT string_agg(c, ', ') INTO missing
      FROM unnest(ARRAY['provider_order_ref', 'provider_last_polled_at']) AS c
     WHERE NOT EXISTS (
         SELECT 1 FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name = 'defgeo_publish_commands'
            AND column_name = c
     );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION '[反查] 047 供应商引用列缺失: %', missing;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = 'public' AND indexname = 'idx_defgeo_pcmd_awaiting_outcome' AND tablename = 'defgeo_publish_commands'
    ) THEN
        RAISE EXCEPTION '[反查] 047 回执轮询索引缺失 —— 轮询会全表扫';
    END IF;

    -- 两列都必须可空:NULL 分别表示"还没外调过"与"还没轮询过",
    -- 加 NOT NULL 等于把这两个语义抹掉。
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'defgeo_publish_commands'
           AND column_name IN ('provider_order_ref', 'provider_last_polled_at')
           AND is_nullable = 'NO'
    ) THEN
        RAISE EXCEPTION '[反查] 047 的列被加了 NOT NULL —— "还没发生过"就无法表达了';
    END IF;
END $$;
