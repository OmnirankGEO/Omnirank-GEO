-- 043 · 防御型 GEO WP4 · 激活 outbox(窄 RFC 已批 2026-08-21)
--
-- 规格:DEFENSIVE_GEO_SYSTEM_INTEGRATION_PLAN_2026-08-20.md @ e710be6c2
--       §3.1(五里程碑)/§3.2(不可变确认合同)/§12.3(outbox 与 reconciler 四个 kill window)
-- RFC :docs/AI-CONTEXT/DEFGEO_W2_RFC_ACTIVATION_OUTBOX_2026-08-21.md(三问全批)
-- 判据:ACT-06 / ACT-07 / ACT-11 / ACT-13 / ACT-14 / ACT-15
--
-- 🔴 体内零 DML。prestart 每次部署无条件重放全部迁移(无追踪表)。
-- 🔴 零资金列。激活本身 execution freeze = 0(ACT-06/ACT-13 逐字),
--    表里不出现任何 points/金额/wallet 列 —— 免得日后有人往里塞,
--    塞进来就等于把"预扣执行算力"混进 activation 事务(ACT-07 明令禁止)。

CREATE TABLE IF NOT EXISTS public.defgeo_activation_outbox (
    id                      BIGSERIAL PRIMARY KEY,

    -- ---- 幂等 root ----------------------------------------------------
    -- §3.2「同一 accepted snapshot 至多激活一个 live service contract/
    --       activation root;重放返回原对象」。
    -- 🔴 ACT-06「20 并发只产生一个」由下面那条 UNIQUE 保证,**不靠应用层**:
    --    应用层的 SELECT-then-INSERT 在并发下必然有窗口,唯一约束没有。
    accepted_snapshot_id    BIGINT      NOT NULL,
    quote_id                INTEGER     NOT NULL,
    brand_id                INTEGER     NOT NULL,

    -- ---- 事件事实 ------------------------------------------------------
    event_kind              VARCHAR(64) NOT NULL,
    -- 冻结的 accepted 快照指纹。与 keyword_selection_sessions 上那份同源,
    -- 便于 reconciler 在不回读 latest 的前提下核对绑定对象没有漂移。
    accepted_snapshot_hash  CHARACTER(64) NOT NULL,
    -- 服务端派生的租户/发起人身份(§3.3 执行时冻结并重验的那一组)。
    -- 只存不可变标识,不存权限判定结果 —— 权限每次执行都要重验。
    tenant_owner_id         INTEGER,
    actor_user_id           INTEGER,
    actor_membership_id     BIGINT,
    approval_id             BIGINT,

    -- ---- 队列语义(形态照抄现役 geo_article_plan_outbox 已被验证的那一组)----
    status                  VARCHAR(32)  NOT NULL DEFAULT 'pending',
    attempt_count           INTEGER      NOT NULL DEFAULT 0,
    available_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    claimed_at              TIMESTAMPTZ,
    claim_token             VARCHAR(80),
    last_error              TEXT,
    occurred_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    observed_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- ---- §12.3 kill window 3 ------------------------------------------
    -- 「worker 外调前写 canonical external-start marker;恢复时任一 marker
    --   存在均不得盲目二次外调」。marker 一旦写下永不清除 ——
    --   清除它就等于把"我可能已经外调过"这条事实抹掉。
    external_start_marker   TEXT,
    external_start_at       TIMESTAMPTZ,

    -- ---- 物化终态 ------------------------------------------------------
    materialized_at         TIMESTAMPTZ,

    CONSTRAINT defgeo_activation_outbox_status_closed CHECK (
        status IN ('pending', 'claimed', 'materialized', 'failed', 'needs_review')
    ),
    CONSTRAINT defgeo_activation_outbox_hash_shape CHECK (
        accepted_snapshot_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT defgeo_activation_outbox_attempts_sane CHECK (attempt_count >= 0),
    -- 物化终态必须带时间戳;反之带了时间戳就必须是终态。半状态不可表达。
    CONSTRAINT defgeo_activation_outbox_materialized_group CHECK (
        (status = 'materialized' AND materialized_at IS NOT NULL)
        OR (status <> 'materialized' AND materialized_at IS NULL)
    ),
    -- 外调 marker 与其时间戳同生同死。
    CONSTRAINT defgeo_activation_outbox_external_group CHECK (
        (external_start_marker IS NULL AND external_start_at IS NULL)
        OR (external_start_marker IS NOT NULL AND external_start_at IS NOT NULL)
    )
);

-- 🔴 ACT-06 的全部承重都在这一行。
--    同一 accepted snapshot 只能有一条 activation root,并发第 2..20 个请求
--    会在数据库层撞 unique violation,由调用方翻译成「返回原对象」。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'defgeo_activation_outbox_root_unique'
           AND conrelid = 'public.defgeo_activation_outbox'::regclass
    ) THEN
        ALTER TABLE public.defgeo_activation_outbox
            ADD CONSTRAINT defgeo_activation_outbox_root_unique
            UNIQUE (accepted_snapshot_id, quote_id);
    END IF;
END $$;

-- 绑定必须指向**本 quote 自己的**那一版快照(与 042 同一道理:
-- snapshot_hash 不覆盖 quote_id,挡跨 quote 的只有复合 FK)。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'defgeo_activation_outbox_snapshot_fk'
           AND conrelid = 'public.defgeo_activation_outbox'::regclass
    ) THEN
        ALTER TABLE public.defgeo_activation_outbox
            ADD CONSTRAINT defgeo_activation_outbox_snapshot_fk
            FOREIGN KEY (accepted_snapshot_id, quote_id)
            REFERENCES public.quote_pricing_snapshots (id, quote_id);
    END IF;
END $$;

-- claim 循环取数路径:按 status + available_at 领取。
-- @index-guard idx_defgeo_activation_outbox_claimable ON defgeo_activation_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_activation_outbox_claimable' AND i.indrelid = to_regclass('public.defgeo_activation_outbox')) THEN
        NULL;  -- 已在 public.defgeo_activation_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_activation_outbox_claimable' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_activation_outbox_claimable 已存在但不在 public.defgeo_activation_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_activation_outbox_claimable' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_activation_outbox_claimable ON public.defgeo_activation_outbox (status, available_at) WHERE status IN ('pending', 'claimed');
    END IF;
END $idxguard$;

-- reconciler 反查路径:§12.3「已收款但 activation 未物化」。
-- @index-guard idx_defgeo_activation_outbox_unmaterialized ON defgeo_activation_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_activation_outbox_unmaterialized' AND i.indrelid = to_regclass('public.defgeo_activation_outbox')) THEN
        NULL;  -- 已在 public.defgeo_activation_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_activation_outbox_unmaterialized' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_activation_outbox_unmaterialized 已存在但不在 public.defgeo_activation_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_activation_outbox_unmaterialized' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_activation_outbox_unmaterialized ON public.defgeo_activation_outbox (quote_id) WHERE materialized_at IS NULL;
    END IF;
END $idxguard$;
