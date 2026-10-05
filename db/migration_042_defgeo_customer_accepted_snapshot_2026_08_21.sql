-- 042 · 防御型 GEO WP4 · 客户接受快照指针 + accepted 审计事实(additive)
--
-- 规格:DEFENSIVE_GEO_SYSTEM_INTEGRATION_PLAN_2026-08-20.md @ e710be6c2 §3.2
--       「增加 customer_confirmed_snapshot_id,不能从仍可变化的 active pointer
--         猜客户接受了哪一版;该 pointer 落在持有 quote_id 的确认 owner row 上,
--         并以复合 FK (customer_confirmed_snapshot_id, quote_id)
--         → quote_pricing_snapshots(id, quote_id) 保证不能把 A quote 的
--         snapshot 指给 B quote」
-- 判据:ACT-08 / ACT-09 / ACT-13 / ACT-14。
--
-- 🔴 体内零 DML。本仓 prestart **每次部署无条件重放全部迁移**(无追踪表),
--    迁移里的任何 INSERT/UPDATE/DELETE 都是常驻地雷。本文件只有 DDL。
--
-- 🔴 为什么不建新表:§3.2 把 pointer 指定落在「持有 quote_id 的确认 owner row」上。
--    实测 keyword_selection_sessions 正是那一行(quote_id INTEGER NOT NULL
--    且 UNIQUE(quote_id)),因此「同一 quote 只有一个有效接受事件」由现役唯一约束
--    天然保证,不需要第二张审计表。§0.5.4「本规格零 CREATE TABLE」也因此不被触犯。
--
-- 🔴 复合 FK 的必要性(不是洁癖):现役 quote_pricing_snapshots.snapshot_hash
--    只覆盖两个 JSONB payload,**不覆盖 quote_id/brand_id/version**
--    (services/quote_pricing_snapshot.py:65-67 实测)。两个不同 quote 只要
--    payload 相同,hash 就逐字相同 —— 所以 hash **挡不住**跨 quote 错指,
--    只有这条复合 FK 挡得住。
--
--    FK 默认 MATCH SIMPLE:customer_confirmed_snapshot_id 为 NULL 时不校验,
--    正是「尚未确认」该有的语义;一旦写入非空值,就必须是本 quote 自己的快照。

-- 1) 客户确认的那一版快照(不是 active pointer —— active 还会继续变)
ALTER TABLE public.keyword_selection_sessions
    ADD COLUMN IF NOT EXISTS customer_confirmed_snapshot_id BIGINT;

-- 2) accepted 审计事实(§3.2:snapshot_hash / token purpose / actor /
--    request hash / accepted_at 五项同时保存)
ALTER TABLE public.keyword_selection_sessions
    ADD COLUMN IF NOT EXISTS customer_confirmed_snapshot_hash CHARACTER(64);
ALTER TABLE public.keyword_selection_sessions
    ADD COLUMN IF NOT EXISTS customer_confirmed_token_purpose TEXT;
ALTER TABLE public.keyword_selection_sessions
    ADD COLUMN IF NOT EXISTS customer_confirmed_actor TEXT;
ALTER TABLE public.keyword_selection_sessions
    ADD COLUMN IF NOT EXISTS customer_confirmed_request_hash CHARACTER(64);
ALTER TABLE public.keyword_selection_sessions
    ADD COLUMN IF NOT EXISTS customer_confirmed_at TIMESTAMPTZ;

-- [工单 E3-4 · P1-8 · 2026-08-26 · Codex 二审] 补两列**不可重建**的受理事实。
--
-- 二审逐字:"当前确认只写 snapshot id/hash/accepted_at,缺 canonical request、
-- token purpose/subject、actor 等不可重建事实"。亲核之后订正两处:
--   · purpose / actor / request_hash 三列**本来就在**(上面刚建的),但全仓
--     零写入方、零读取方 —— 它们是**死列**。所以那三项要修的是应用代码,
--     不是 DDL(报告把它整体说成"缺列"是不准的);
--   · 真正没有列的只有两项:token **subject**,以及 canonical request 的
--     **正文**(只有 hash 列,而 hash 事后重建不出请求内容)。
--
-- 🔴 只用 ADD COLUMN IF NOT EXISTS —— prestart 每次部署无条件重放全部迁移,
--    additive 加列是重放安全的。
-- 🔴 **刻意不**把这几列并进下面那条 group_complete CHECK:
--    存量已确认行(三件组齐、审计列空)会让 ADD CONSTRAINT 当场失败 ⇒
--    prestart 非零退出 ⇒ 部署 halt。存量行的处置是**读时判定**
--    (services/defensive_geo/acceptance_evidence.classify_acceptance
--     标 legacy_unproven),不是回填 —— 回填等于伪造受理证据。
ALTER TABLE public.keyword_selection_sessions
    ADD COLUMN IF NOT EXISTS customer_confirmed_token_subject TEXT;

ALTER TABLE public.keyword_selection_sessions
    ADD COLUMN IF NOT EXISTS customer_confirmed_request JSONB;

-- 3) 复合 FK:不能把 A quote 的 snapshot 指给 B quote。
--    ADD CONSTRAINT 没有 IF NOT EXISTS,用 DO 块补幂等(prestart 会重放本文件)。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'keyword_selection_sessions_customer_confirmed_fk'
           AND conrelid = 'public.keyword_selection_sessions'::regclass
    ) THEN
        ALTER TABLE public.keyword_selection_sessions
            ADD CONSTRAINT keyword_selection_sessions_customer_confirmed_fk
            FOREIGN KEY (customer_confirmed_snapshot_id, quote_id)
            REFERENCES public.quote_pricing_snapshots (id, quote_id);
    END IF;
END $$;

-- 4) hash 形态闭集(与现役 quote_pricing_snapshots.snapshot_hash 同一形态)。
--    NULL 放行 = 尚未确认;非空必须是 64 位小写十六进制。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'keyword_selection_sessions_confirmed_hash_shape'
           AND conrelid = 'public.keyword_selection_sessions'::regclass
    ) THEN
        ALTER TABLE public.keyword_selection_sessions
            ADD CONSTRAINT keyword_selection_sessions_confirmed_hash_shape
            CHECK (
                customer_confirmed_snapshot_hash IS NULL
                OR customer_confirmed_snapshot_hash ~ '^[0-9a-f]{64}$'
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'keyword_selection_sessions_confirmed_request_hash_shape'
           AND conrelid = 'public.keyword_selection_sessions'::regclass
    ) THEN
        ALTER TABLE public.keyword_selection_sessions
            ADD CONSTRAINT keyword_selection_sessions_confirmed_request_hash_shape
            CHECK (
                customer_confirmed_request_hash IS NULL
                OR customer_confirmed_request_hash ~ '^[0-9a-f]{64}$'
            );
    END IF;
END $$;

-- 5) accepted 事实要么整组齐、要么整组空 —— 不许出现「有指针没审计」的半状态。
--    ACT-07「任一步异常均无可见半状态」在库层的那一半。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'keyword_selection_sessions_confirmed_group_complete'
           AND conrelid = 'public.keyword_selection_sessions'::regclass
    ) THEN
        ALTER TABLE public.keyword_selection_sessions
            ADD CONSTRAINT keyword_selection_sessions_confirmed_group_complete
            CHECK (
                (customer_confirmed_snapshot_id IS NULL
                 AND customer_confirmed_snapshot_hash IS NULL
                 AND customer_confirmed_at IS NULL)
                OR
                (customer_confirmed_snapshot_id IS NOT NULL
                 AND customer_confirmed_snapshot_hash IS NOT NULL
                 AND customer_confirmed_at IS NOT NULL)
            );
    END IF;
END $$;

-- 6) 查询支撑:按已确认快照反查会话(reconciler 与 activation 恢复都要走)。
-- @index-guard idx_kss_customer_confirmed_snapshot ON keyword_selection_sessions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_kss_customer_confirmed_snapshot' AND i.indrelid = to_regclass('public.keyword_selection_sessions')) THEN
        NULL;  -- 已在 public.keyword_selection_sessions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_kss_customer_confirmed_snapshot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_kss_customer_confirmed_snapshot 已存在但不在 public.keyword_selection_sessions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_kss_customer_confirmed_snapshot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_kss_customer_confirmed_snapshot ON public.keyword_selection_sessions (customer_confirmed_snapshot_id) WHERE customer_confirmed_snapshot_id IS NOT NULL;
    END IF;
END $idxguard$;
