-- 044 · 防御型 GEO WP5+WP6 · 发布格 / 媒体决策快照 / 发布命令 / 发布 outbox
--
-- 规格:DEFENSIVE_GEO_SYSTEM_INTEGRATION_PLAN_2026-08-20.md @ e710be6c2
--       §11.3(服务端媒体决策五步)/§12.1(资金全序)/§12.2(幂等身份与 slot 八态)
--       /§12.3(outbox 与四个 kill window)/§15.7(发布确认 DTO)
-- RFC :docs/AI-CONTEXT/DEFGEO_W3_RFC_NEW_TABLES_2026-08-21.md(🟡 待 Owner 签)
-- 判据:MED-09/12/13/14/15/16/17/19/20/21、FIN-01..16、API-02/04、POR-06
--
-- 🔴 体内零 DML(本仓 prestart 每次部署无条件重放全部迁移,无追踪表)。
--    全文件零 INSERT / UPDATE / DELETE —— 自证命令见 RFC §5。
-- 🔴 additive-only:零 ALTER 既有表、零 DROP。四张全新表,回滚 = DROP,
--    但表里存的是**已冻结的媒体决策与已发生的资金冻结**,DROP 等于把
--    「我按这个价确认过」从审计里抹掉,不该由部署清单自动做。真要回退请人工执行。
-- 🔴 重放安全:CREATE TABLE/INDEX IF NOT EXISTS;约束走 pg_constraint 存在性判断。
--    2× 复跑第二遍全 no-op。
-- 🔴 漏跑后果**响亮**:端点显式 INSERT 这四张表 → UndefinedTable 当场抛,
--    走 SafeError 受控失败。静默返空会让服务商看到「已锁定媒体方案」而其实什么都没存。


-- ══════════════════════════════════════════════════════════════════════
-- ① defgeo_publish_slots —— 发布格(§12.2)
-- ══════════════════════════════════════════════════════════════════════
-- 一个 slot = 一个「客户已接受的交付格」。slot_id 由服务端从对象血缘确定性派生
-- (tenant + service projection + accepted snapshot + plan_item_key + command_kind),
-- 客户端提交任何 id 都改变不了归属(§12.2 逐字)。
--
-- 🔴 FIN-14 的承重点:``plan_item_key`` **不是全局唯一**。两个 tenant 用同一个
--    plan_item_key 必须落在两个不同的 slot 上 —— 所以 slot 的自然键含 tenant
--    与 accepted snapshot,而不是只有 plan_item_key。
CREATE TABLE IF NOT EXISTS public.defgeo_publish_slots (
    publish_slot_id             VARCHAR(64) PRIMARY KEY,

    tenant_owner_id             INTEGER      NOT NULL,
    service_projection_id       VARCHAR(120) NOT NULL,
    accepted_snapshot_id        BIGINT       NOT NULL,
    plan_item_key               VARCHAR(200) NOT NULL,
    command_kind                VARCHAR(40)  NOT NULL DEFAULT 'media_publication',
    brand_id                    INTEGER      NOT NULL,

    -- §12.2「publish_item_request_id 由该 slot 确定性派生而不是浏览器随机值」。
    publish_item_request_id     VARCHAR(64)  NOT NULL,

    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'defgeo_publish_slots_natural_unique'
                        AND conrelid = 'public.defgeo_publish_slots'::regclass) THEN
        ALTER TABLE public.defgeo_publish_slots
            ADD CONSTRAINT defgeo_publish_slots_natural_unique
            UNIQUE (tenant_owner_id, service_projection_id, accepted_snapshot_id,
                    plan_item_key, command_kind);
    END IF;
    -- 🔴 复合唯一(slot, tenant)本身是冗余的(slot 已是主键),
    --    但它是下面两张子表**复合 FK** 的前提。
    --    没有那两条复合 FK,``defgeo_pds_one_open_per_slot`` 这类
    --    「(tenant, slot) 上唯一」的索引就有一个洞:同一个 slot 上
    --    挂两个不同 tenant 的 open snapshot,两行的索引键不同 ⇒ 都能插进去。
    --    (2026-08-21 由并发跑判据的同窗 agent 实证。当前端点打不到 ——
    --     slot_id 是从 tenant 派生的 —— 但「端点现在打不到」不是约束的理由,
    --     下一个写入方就可能打得到。)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'defgeo_publish_slots_tenant_unique'
                        AND conrelid = 'public.defgeo_publish_slots'::regclass) THEN
        ALTER TABLE public.defgeo_publish_slots
            ADD CONSTRAINT defgeo_publish_slots_tenant_unique
            UNIQUE (publish_slot_id, tenant_owner_id);
    END IF;
END $$;

-- @index-guard idx_defgeo_pslot_tenant ON defgeo_publish_slots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_pslot_tenant' AND i.indrelid = to_regclass('public.defgeo_publish_slots')) THEN
        NULL;  -- 已在 public.defgeo_publish_slots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_pslot_tenant' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_pslot_tenant 已存在但不在 public.defgeo_publish_slots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_pslot_tenant' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_pslot_tenant ON public.defgeo_publish_slots (tenant_owner_id, brand_id);
    END IF;
END $idxguard$;


-- ══════════════════════════════════════════════════════════════════════
-- ② defgeo_publish_decision_snapshots —— 冻结的媒体决策(§11.3 / §15.7)
-- ══════════════════════════════════════════════════════════════════════
-- 🔴 ``frozen_payload`` 是**不可变**的:一旦写入,字节与 canonical_hash 永不改变
--    (§15.7「snapshot 字节与 canonicalHash 永不随时间、库存、服务态、余额或审批变化」)。
--    不可变由下面的 trigger 强制,不靠调用方自律 —— 自律的那一半永远会有人绕过。
-- 🔴 lifecycle 五值 CHECK。**没有第六个值**,也没有 settlement 语义 ——
--    §3.5 禁第二套 settlement enum,本表的 lifecycle 只讲「这份方案还能不能确认」。
CREATE TABLE IF NOT EXISTS public.defgeo_publish_decision_snapshots (
    decision_snapshot_id        VARCHAR(64) PRIMARY KEY,
    publish_slot_id             VARCHAR(64)  NOT NULL
        REFERENCES public.defgeo_publish_slots (publish_slot_id),

    snapshot_version            INTEGER      NOT NULL,
    canonical_hash              CHARACTER(64) NOT NULL,
    -- 冻结面全量字节(JCS 化之前的结构)。读面只从这里取,不回查活表。
    frozen_payload              JSONB        NOT NULL,

    lifecycle                   VARCHAR(16)  NOT NULL DEFAULT 'open',
    expires_at                  TIMESTAMPTZ  NOT NULL,

    -- 被替代时的 exact successor(§15.7「任何 superseded 都必须带非空 exact
    -- successor id/hash 与 supersessionKind」)。三列同生同灭,见 CHECK。
    superseded_by_snapshot_id   VARCHAR(64),
    superseded_by_snapshot_hash CHARACTER(64),
    supersession_kind           VARCHAR(20),
    -- override/replacement 的父子血缘(MED-19)。
    parent_snapshot_id          VARCHAR(64),

    -- consumed 时指向那条 command(§15.7 lifecycle=consumed 分支)。
    consumed_command_id         VARCHAR(64),

    -- 幂等:同 key 同 canonical payload 重放同一 snapshot;异 payload 409。
    idempotency_key             VARCHAR(200) NOT NULL,
    request_canonical_hash      CHARACTER(64) NOT NULL,

    tenant_owner_id             INTEGER      NOT NULL,
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    lifecycle_changed_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pds_lifecycle'
                        AND conrelid = 'public.defgeo_publish_decision_snapshots'::regclass) THEN
        ALTER TABLE public.defgeo_publish_decision_snapshots
            ADD CONSTRAINT chk_defgeo_pds_lifecycle
            CHECK (lifecycle IN ('open','expired','superseded','cancelled','consumed'));
    END IF;
    -- superseded 必须三项齐全;非 superseded 必须三项全空。
    -- 半套(有 id 没 hash)会让「查看 exact successor」变成一个死 CTA。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pds_successor'
                        AND conrelid = 'public.defgeo_publish_decision_snapshots'::regclass) THEN
        ALTER TABLE public.defgeo_publish_decision_snapshots
            ADD CONSTRAINT chk_defgeo_pds_successor
            CHECK (
                (lifecycle = 'superseded'
                 AND superseded_by_snapshot_id IS NOT NULL
                 AND superseded_by_snapshot_hash IS NOT NULL
                 AND supersession_kind IN ('new_preview','override','replacement'))
                OR
                (lifecycle <> 'superseded'
                 AND superseded_by_snapshot_id IS NULL
                 AND superseded_by_snapshot_hash IS NULL
                 AND supersession_kind IS NULL)
            );
    END IF;
    -- 🔴 snapshot 的 tenant 必须**就是** slot 的 tenant。见 slots 那条复合唯一
    --    上方的注释:少了它,`(tenant, slot) 上唯一` 的 partial unique 有洞。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'defgeo_pds_slot_tenant_fk'
                        AND conrelid = 'public.defgeo_publish_decision_snapshots'::regclass) THEN
        ALTER TABLE public.defgeo_publish_decision_snapshots
            ADD CONSTRAINT defgeo_pds_slot_tenant_fk
            FOREIGN KEY (publish_slot_id, tenant_owner_id)
            REFERENCES public.defgeo_publish_slots (publish_slot_id, tenant_owner_id);
    END IF;
    -- consumed 必须带 command;非 consumed 不得带。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pds_consumed'
                        AND conrelid = 'public.defgeo_publish_decision_snapshots'::regclass) THEN
        ALTER TABLE public.defgeo_publish_decision_snapshots
            ADD CONSTRAINT chk_defgeo_pds_consumed
            CHECK (
                (lifecycle = 'consumed' AND consumed_command_id IS NOT NULL)
                OR (lifecycle <> 'consumed' AND consumed_command_id IS NULL)
            );
    END IF;
END $$;

-- 🔴🔴 §12.2 逐字要求的 PG16 partial unique:
--    「(tenant_owner_id, publish_slot_id) WHERE lifecycle='open'」
--    —— 任何时刻同 slot 不能有两个可 confirm 的 snapshot。
--    并发两个不同 Idempotency-Key 的 preview 由**这条约束**收敛,
--    不靠应用层 SELECT-then-INSERT(那在并发下必然有窗口)。
-- @index-guard defgeo_pds_one_open_per_slot ON defgeo_publish_decision_snapshots unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'defgeo_pds_one_open_per_slot' AND i.indrelid = to_regclass('public.defgeo_publish_decision_snapshots')) THEN
        NULL;  -- 已在 public.defgeo_publish_decision_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'defgeo_pds_one_open_per_slot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] defgeo_pds_one_open_per_slot 已存在但不在 public.defgeo_publish_decision_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'defgeo_pds_one_open_per_slot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX defgeo_pds_one_open_per_slot ON public.defgeo_publish_decision_snapshots (tenant_owner_id, publish_slot_id) WHERE lifecycle = 'open';
    END IF;
END $idxguard$;

-- 幂等 root:同 key + 同 canonical payload → 同一行。
-- @index-guard defgeo_pds_idem_root ON defgeo_publish_decision_snapshots unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'defgeo_pds_idem_root' AND i.indrelid = to_regclass('public.defgeo_publish_decision_snapshots')) THEN
        NULL;  -- 已在 public.defgeo_publish_decision_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'defgeo_pds_idem_root' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] defgeo_pds_idem_root 已存在但不在 public.defgeo_publish_decision_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'defgeo_pds_idem_root' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX defgeo_pds_idem_root ON public.defgeo_publish_decision_snapshots (tenant_owner_id, publish_slot_id, idempotency_key, request_canonical_hash);
    END IF;
END $idxguard$;

-- @index-guard idx_defgeo_pds_slot ON defgeo_publish_decision_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_pds_slot' AND i.indrelid = to_regclass('public.defgeo_publish_decision_snapshots')) THEN
        NULL;  -- 已在 public.defgeo_publish_decision_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_pds_slot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_pds_slot 已存在但不在 public.defgeo_publish_decision_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_pds_slot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_pds_slot ON public.defgeo_publish_decision_snapshots (publish_slot_id, snapshot_version DESC);
    END IF;
END $idxguard$;

-- 🔴 不可变强制:冻结面三列一旦写入不得更改(§15.7)。
--    只允许改 lifecycle / successor / consumed / 时间戳这些 **live 面** 列。
CREATE OR REPLACE FUNCTION public.defgeo_pds_freeze_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.frozen_payload IS DISTINCT FROM OLD.frozen_payload
       OR NEW.canonical_hash IS DISTINCT FROM OLD.canonical_hash
       OR NEW.snapshot_version IS DISTINCT FROM OLD.snapshot_version
       OR NEW.publish_slot_id IS DISTINCT FROM OLD.publish_slot_id
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION
          'defgeo_publish_decision_snapshots 的冻结面不可变(payload/hash/version/slot/expiry);'
          '媒体方案变了必须新建 revision,不是原地改(§15.7 / MED-14)';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_defgeo_pds_freeze_guard'
          AND tgrelid = 'public.defgeo_publish_decision_snapshots'::regclass
    ) THEN
        CREATE TRIGGER trg_defgeo_pds_freeze_guard
            BEFORE UPDATE ON public.defgeo_publish_decision_snapshots
            FOR EACH ROW EXECUTE FUNCTION public.defgeo_pds_freeze_guard();
    END IF;
END $$;


-- ══════════════════════════════════════════════════════════════════════
-- ③ defgeo_publish_commands —— 发布命令 + 资金句柄 + canonical 发布事实
-- ══════════════════════════════════════════════════════════════════════
-- 🔴 资金句柄四元组(freeze_id / freeze_task_ref / freeze_backend / payer_user_id)
--    **持久化**下来,commit/release 时原样回传 —— 形态照抄现役 diagnosis_runs
--    已被生产验证的那一组(services/diagnosis_runs.py::_do_settlement)。
--    不持久化的后果:结算时要"重新算出"该退给谁,而那正是跨表错路由的来源。
-- 🔴 本表**不新建 settlement enum**:``funding_state`` 只表达资金方向
--    (§3.5 三条状态轴分开),``canonical_publication_state`` 表达发布事实,
--    两者由 services/defensive_geo/publish/publish_settlement.py 的真值表交叉校验。
CREATE TABLE IF NOT EXISTS public.defgeo_publish_commands (
    publish_command_id          VARCHAR(64) PRIMARY KEY,
    publish_slot_id             VARCHAR(64)  NOT NULL
        REFERENCES public.defgeo_publish_slots (publish_slot_id),
    decision_snapshot_id        VARCHAR(64)  NOT NULL
        REFERENCES public.defgeo_publish_decision_snapshots (decision_snapshot_id),
    decision_snapshot_hash      CHARACTER(64) NOT NULL,
    command_canonical_hash      CHARACTER(64) NOT NULL,

    -- ---- 血缘(MED-15 retry-child / MED-19 override)----------------------
    parent_command_id           VARCHAR(64),
    command_generation          INTEGER      NOT NULL DEFAULT 1,
    lineage_kind                VARCHAR(20)  NOT NULL DEFAULT 'root',

    -- ---- 身份 -----------------------------------------------------------
    tenant_owner_id             INTEGER      NOT NULL,
    actor_user_id               INTEGER      NOT NULL,
    brand_id                    INTEGER      NOT NULL,
    publish_item_request_id     VARCHAR(64)  NOT NULL,
    article_revision_id         VARCHAR(120) NOT NULL,
    article_hash                VARCHAR(120) NOT NULL,
    public_media_key            CHARACTER(64) NOT NULL,
    canonical_root_domain_key   CHARACTER(64) NOT NULL,

    -- ---- 资金 -----------------------------------------------------------
    funding_policy              VARCHAR(32)  NOT NULL,
    principal_kind              VARCHAR(24)  NOT NULL,
    payer_user_id               INTEGER,
    exact_settlement_points     INTEGER      NOT NULL,
    freeze_id                   BIGINT,
    freeze_task_ref             VARCHAR(120) NOT NULL,
    freeze_backend              VARCHAR(16),
    organization_charge_ref     VARCHAR(120),
    approval_ref                VARCHAR(120),
    sponsor_policy_ref          VARCHAR(120),
    platform_cost_ref           VARCHAR(120),
    funding_state               VARCHAR(32)  NOT NULL DEFAULT 'frozen',

    -- ---- canonical 发布事实(§12.1)----------------------------------------
    command_state               VARCHAR(32)  NOT NULL DEFAULT 'queued',
    canonical_publication_state VARCHAR(40)  NOT NULL DEFAULT 'not_started',
    raw_state_source_table      VARCHAR(64),
    raw_state_source_column     VARCHAR(64),
    raw_state_value             TEXT,
    url_verification_state      VARCHAR(32),
    url_availability_state      VARCHAR(32),
    public_url                  TEXT,
    -- 🔴 external-start marker(§12.3「worker 外调前写 canonical external-start
    --    marker;恢复时任一 marker 存在均不得盲目二次外调」)。
    --    它在**本表**而不是 outbox 表,因为恢复路径读的是 command。
    external_start_at           TIMESTAMPTZ,
    external_start_token        VARCHAR(80),
    provider_call_count         INTEGER      NOT NULL DEFAULT 0,

    -- ---- 法律门(MED-11 / MED-18)-----------------------------------------
    legal_rule_id               VARCHAR(120),
    legal_rule_version          VARCHAR(40),
    legal_passage_ref           TEXT,
    legal_passage_excerpt       TEXT,

    -- ---- 下架 / 替代 policy(MED-20)---------------------------------------
    replacement_policy_ref      VARCHAR(120),

    -- ---- 幂等与投影 ------------------------------------------------------
    idempotency_key             VARCHAR(200) NOT NULL,
    request_canonical_hash      CHARACTER(64) NOT NULL,
    status_version              INTEGER      NOT NULL DEFAULT 1,
    status_reason               TEXT,

    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    settled_at                  TIMESTAMPTZ
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pcmd_funding_state'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT chk_defgeo_pcmd_funding_state
            CHECK (funding_state IN ('frozen','committed','released',
                                     'pending_reconciliation','quarantined','exempt_recorded'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pcmd_command_state'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT chk_defgeo_pcmd_command_state
            CHECK (command_state IN ('accepted','queued','running','settlement_pending',
                                     'needs_action','completed','failed','cancelled','quarantined'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pcmd_pub_state'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT chk_defgeo_pcmd_pub_state
            CHECK (canonical_publication_state IN (
                'not_started','queued','submitting','reported_success_unverified',
                'verified_published','retracted','rejected_no_effect','failed_no_effect',
                'rejected_unknown','failed_unknown','unknown','conflict'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pcmd_funding_policy'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT chk_defgeo_pcmd_funding_policy
            CHECK (funding_policy IN ('personal_wallet','organization_budget',
                                      'admin_platform_ledger','sponsor_platform_ledger'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pcmd_lineage'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT chk_defgeo_pcmd_lineage
            CHECK (
                (lineage_kind = 'root' AND parent_command_id IS NULL AND command_generation = 1)
                OR (lineage_kind IN ('retry_child','legal_repair','replacement')
                    AND parent_command_id IS NOT NULL AND command_generation > 1)
            );
    END IF;
    -- 🔴 资金句柄不得半套:**两条 charged 腿**(personal_wallet / organization_budget)
    --    只要金额非 0,就必须有可定位的冻结句柄。缺句柄的 frozen 行是
    --    「钱冻了但不知道退给谁」—— 结算时只能转人工。
    --    ⚠️ 第一版谓词只写了 personal_wallet,而注释说的是 personal/organization
    --       —— 500 分的 organization_budget 无句柄行库照收(2026-08-21 由同窗
    --       agent 实证)。注释与谓词不一致时,**承重的永远是谓词**;
    --       所以这里改谓词,不是改注释。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pcmd_freeze_handle'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT chk_defgeo_pcmd_freeze_handle
            CHECK (
                funding_policy NOT IN ('personal_wallet', 'organization_budget')
                OR exact_settlement_points = 0
                OR (freeze_id IS NOT NULL AND freeze_backend IS NOT NULL AND payer_user_id IS NOT NULL)
            );
    END IF;
    -- 🔴 command 的 tenant 也必须就是 slot 的 tenant(同 snapshot 那条的理由)。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'defgeo_pcmd_slot_tenant_fk'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT defgeo_pcmd_slot_tenant_fk
            FOREIGN KEY (publish_slot_id, tenant_owner_id)
            REFERENCES public.defgeo_publish_slots (publish_slot_id, tenant_owner_id);
    END IF;
    -- 🔴 平台成本两格的 fundingState 恒为 exempt_recorded(§15.7 表格末列)。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pcmd_platform_state'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT chk_defgeo_pcmd_platform_state
            CHECK (
                principal_kind <> 'platform_cost_center'
                OR funding_state = 'exempt_recorded'
            );
    END IF;
    -- 🔴 external-start 至多一次:有 token 必有时间戳,反之亦然。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pcmd_external_start'
                        AND conrelid = 'public.defgeo_publish_commands'::regclass) THEN
        ALTER TABLE public.defgeo_publish_commands
            ADD CONSTRAINT chk_defgeo_pcmd_external_start
            CHECK ((external_start_at IS NULL) = (external_start_token IS NULL));
    END IF;
END $$;

-- 🔴🔴 §12.2「PG16 至少约束同 slot 最多一个**非终态** command」。
--    终态集合 = completed/failed/cancelled。其余(accepted/queued/running/
--    settlement_pending/needs_action/quarantined)都算在途。
--
-- 🔴 **一处例外,写在谓词里而不是靠应用层记得**:法律门命中的 item
--    (legal_rule_id 非空 且 funding_state='released')对外仍显示
--    commandState='needs_action'(§15.7 LegalNoEffectStatusProjection 逐字),
--    但它在 slot 全序里**已经终结** —— §12.2 明确它「只能先局部修复,随后以
--    新 article revision/decision/confirm」。若不把它排除在"在途"之外,
--    修复完的新 command 会撞这条唯一索引,法律格就成了一条**死路**
--    (§0.5.6 铁律:任何阻塞必须自带解决方案)。
--    收窄条件是两项**同时**成立:有 rule 命中 **且** 钱已经退干净。
--    只满足其一(例如命中了但还没退)仍算在途 —— 钱没落地不许开新的。
-- @index-guard defgeo_pcmd_one_live_per_slot ON defgeo_publish_commands unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'defgeo_pcmd_one_live_per_slot' AND i.indrelid = to_regclass('public.defgeo_publish_commands')) THEN
        NULL;  -- 已在 public.defgeo_publish_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'defgeo_pcmd_one_live_per_slot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] defgeo_pcmd_one_live_per_slot 已存在但不在 public.defgeo_publish_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'defgeo_pcmd_one_live_per_slot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX defgeo_pcmd_one_live_per_slot ON public.defgeo_publish_commands (publish_slot_id) WHERE command_state NOT IN ('completed','failed','cancelled') AND NOT (legal_rule_id IS NOT NULL AND funding_state = 'released');
    END IF;
END $idxguard$;

-- 🔴🔴 §12.2「一个 canonical active fulfillment owner;普通路径最多一个
--    committed fulfillment generation」。committed 且已核实发布 = 该格已履约。
-- @index-guard defgeo_pcmd_one_committed_fulfillment ON defgeo_publish_commands unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'defgeo_pcmd_one_committed_fulfillment' AND i.indrelid = to_regclass('public.defgeo_publish_commands')) THEN
        NULL;  -- 已在 public.defgeo_publish_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'defgeo_pcmd_one_committed_fulfillment' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] defgeo_pcmd_one_committed_fulfillment 已存在但不在 public.defgeo_publish_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'defgeo_pcmd_one_committed_fulfillment' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX defgeo_pcmd_one_committed_fulfillment ON public.defgeo_publish_commands (publish_slot_id) WHERE funding_state = 'committed' AND canonical_publication_state IN ('verified_published','retracted');
    END IF;
END $idxguard$;

-- 幂等 root。
-- @index-guard defgeo_pcmd_idem_root ON defgeo_publish_commands unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'defgeo_pcmd_idem_root' AND i.indrelid = to_regclass('public.defgeo_publish_commands')) THEN
        NULL;  -- 已在 public.defgeo_publish_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'defgeo_pcmd_idem_root' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] defgeo_pcmd_idem_root 已存在但不在 public.defgeo_publish_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'defgeo_pcmd_idem_root' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX defgeo_pcmd_idem_root ON public.defgeo_publish_commands (tenant_owner_id, publish_slot_id, idempotency_key, request_canonical_hash);
    END IF;
END $idxguard$;

-- 🔴 同一 parent 至多一个 live child(§15.7 retry-child「同 parent 已有 live child 拒绝」)。
-- @index-guard defgeo_pcmd_one_live_child ON defgeo_publish_commands unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'defgeo_pcmd_one_live_child' AND i.indrelid = to_regclass('public.defgeo_publish_commands')) THEN
        NULL;  -- 已在 public.defgeo_publish_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'defgeo_pcmd_one_live_child' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] defgeo_pcmd_one_live_child 已存在但不在 public.defgeo_publish_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'defgeo_pcmd_one_live_child' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX defgeo_pcmd_one_live_child ON public.defgeo_publish_commands (parent_command_id) WHERE parent_command_id IS NOT NULL AND command_state NOT IN ('completed','failed','cancelled');
    END IF;
END $idxguard$;

-- @index-guard idx_defgeo_pcmd_settlement_queue ON defgeo_publish_commands plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_pcmd_settlement_queue' AND i.indrelid = to_regclass('public.defgeo_publish_commands')) THEN
        NULL;  -- 已在 public.defgeo_publish_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_pcmd_settlement_queue' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_pcmd_settlement_queue 已存在但不在 public.defgeo_publish_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_pcmd_settlement_queue' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_pcmd_settlement_queue ON public.defgeo_publish_commands (funding_state, updated_at) WHERE funding_state IN ('pending_reconciliation','quarantined');
    END IF;
END $idxguard$;


-- ══════════════════════════════════════════════════════════════════════
-- ④ defgeo_publish_outbox —— 与业务事务同提交的出队信箱(§12.3)
-- ══════════════════════════════════════════════════════════════════════
-- 形态与 043 的 activation outbox 同源(队列语义那一组列逐字照抄),
-- 差别只在:本表**带资金相关性**,所以 kill window ② 的判据要数它的增量。
CREATE TABLE IF NOT EXISTS public.defgeo_publish_outbox (
    id                      BIGSERIAL PRIMARY KEY,
    publish_command_id      VARCHAR(64)  NOT NULL
        REFERENCES public.defgeo_publish_commands (publish_command_id),
    publish_slot_id         VARCHAR(64)  NOT NULL,
    event_kind              VARCHAR(64)  NOT NULL DEFAULT 'publish_command_created',

    status                  VARCHAR(32)  NOT NULL DEFAULT 'pending',
    attempt_count           INTEGER      NOT NULL DEFAULT 0,
    available_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    claimed_at              TIMESTAMPTZ,
    claim_token             VARCHAR(80),
    last_error              TEXT,
    occurred_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    observed_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_pout_status'
                        AND conrelid = 'public.defgeo_publish_outbox'::regclass) THEN
        ALTER TABLE public.defgeo_publish_outbox
            ADD CONSTRAINT chk_defgeo_pout_status
            CHECK (status IN ('pending','claimed','dispatched','failed','needs_review'));
    END IF;
    -- 一条 command 只入队一次(kill window ② 的「资金只冻结一次、outbox 只一条」)。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'defgeo_pout_command_unique'
                        AND conrelid = 'public.defgeo_publish_outbox'::regclass) THEN
        ALTER TABLE public.defgeo_publish_outbox
            ADD CONSTRAINT defgeo_pout_command_unique
            UNIQUE (publish_command_id, event_kind);
    END IF;
END $$;

-- @index-guard idx_defgeo_pout_claimable ON defgeo_publish_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_pout_claimable' AND i.indrelid = to_regclass('public.defgeo_publish_outbox')) THEN
        NULL;  -- 已在 public.defgeo_publish_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_pout_claimable' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_pout_claimable 已存在但不在 public.defgeo_publish_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_pout_claimable' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_pout_claimable ON public.defgeo_publish_outbox (status, available_at) WHERE status IN ('pending','claimed');
    END IF;
END $idxguard$;


-- ══════════════════════════════════════════════════════════════════════
-- ⑤ defgeo_settlement_review_entries —— Z-1 资金核验队列的**留痕**表
-- ══════════════════════════════════════════════════════════════════════
-- §0.5.6 Z-1:「全部处置留痕入账本」。
-- 队列本身是 defgeo_publish_commands 上的**投影**(funding_state ∈
-- pending_reconciliation/quarantined),不另存一份状态 —— 存两份必然对不上。
-- 本表只记「谁在什么时候对哪条 command 做了什么处置 / 提交了什么凭证」。
CREATE TABLE IF NOT EXISTS public.defgeo_settlement_review_entries (
    id                      BIGSERIAL PRIMARY KEY,
    publish_command_id      VARCHAR(64)  NOT NULL
        REFERENCES public.defgeo_publish_commands (publish_command_id),

    -- 四类条目:admin 三种处置 + 服务商提交凭证。
    entry_kind              VARCHAR(32)  NOT NULL,
    actor_user_id           INTEGER      NOT NULL,
    -- 'platform_admin' | 'service_provider' —— 收口权在平台,服务商只能提交材料。
    actor_role              VARCHAR(32)  NOT NULL,
    -- 「维持隔离」必填理由(Z-1 逐字)。CHECK 在下面。
    reason                  TEXT,
    evidence_payload        JSONB,
    -- 处置前后的资金态,便于对账复算。
    funding_state_before    VARCHAR(32)  NOT NULL,
    funding_state_after     VARCHAR(32),
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_sre_kind'
                        AND conrelid = 'public.defgeo_settlement_review_entries'::regclass) THEN
        ALTER TABLE public.defgeo_settlement_review_entries
            ADD CONSTRAINT chk_defgeo_sre_kind
            CHECK (entry_kind IN ('admin_commit','admin_release','admin_hold','provider_evidence'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_sre_role'
                        AND conrelid = 'public.defgeo_settlement_review_entries'::regclass) THEN
        ALTER TABLE public.defgeo_settlement_review_entries
            ADD CONSTRAINT chk_defgeo_sre_role
            CHECK (actor_role IN ('platform_admin','service_provider'));
    END IF;
    -- 🔴 「维持隔离」必填理由。没有理由的「先放着」等于把死路写进账本。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_sre_hold_reason'
                        AND conrelid = 'public.defgeo_settlement_review_entries'::regclass) THEN
        ALTER TABLE public.defgeo_settlement_review_entries
            ADD CONSTRAINT chk_defgeo_sre_hold_reason
            CHECK (entry_kind <> 'admin_hold' OR (reason IS NOT NULL AND length(btrim(reason)) >= 4));
    END IF;
    -- 🔴 服务商只能提交凭证,动不了钱(收口权在平台)。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_sre_provider_readonly'
                        AND conrelid = 'public.defgeo_settlement_review_entries'::regclass) THEN
        ALTER TABLE public.defgeo_settlement_review_entries
            ADD CONSTRAINT chk_defgeo_sre_provider_readonly
            CHECK (actor_role <> 'service_provider' OR entry_kind = 'provider_evidence');
    END IF;
END $$;

-- @index-guard idx_defgeo_sre_command ON defgeo_settlement_review_entries plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_sre_command' AND i.indrelid = to_regclass('public.defgeo_settlement_review_entries')) THEN
        NULL;  -- 已在 public.defgeo_settlement_review_entries 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_sre_command' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_sre_command 已存在但不在 public.defgeo_settlement_review_entries 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_sre_command' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_sre_command ON public.defgeo_settlement_review_entries (publish_command_id, created_at DESC);
    END IF;
END $idxguard$;


-- ══════════════════════════════════════════════════════════════════════
-- ⑥ defgeo_provider_execution_budgets —— provider-private 执行预算快照(§3.4)
-- ══════════════════════════════════════════════════════════════════════
-- 🔴 **不存 reserved/committed 的运行值**。那两个数由
--    ``defgeo_publish_commands`` 的 SUM 现算(见 publish_funding.py)。
--    存两份必然有一份是陈旧的,而陈旧的那份会让 cap 守恒判据在错误的一侧全绿。
-- 🔴 customer offer hash 与 provider budget hash **字段范围完全分开**(§3.4):
--    本表是 provider-private,任何 points cap 都不得进入 customer confirmation hash
--    (FIN-15「任何 points cap 进入 customer offer hash 均拒绝」)。
CREATE TABLE IF NOT EXISTS public.defgeo_provider_execution_budgets (
    execution_budget_snapshot_id VARCHAR(64) PRIMARY KEY,
    budget_version               INTEGER      NOT NULL,

    tenant_owner_id              INTEGER      NOT NULL,
    accepted_snapshot_id         BIGINT       NOT NULL,
    service_projection_id        VARCHAR(120) NOT NULL,

    global_cap_points            INTEGER      NOT NULL,
    -- scope 拆分:内容/发布/监测/百科官网。v1 只用 media_publication 这一格,
    -- 但列按 scope 存,免得日后加 scope 要改表结构。
    scope_key                    VARCHAR(40)  NOT NULL DEFAULT 'media_publication',
    scope_cap_points             INTEGER      NOT NULL,

    funding_policy               VARCHAR(32)  NOT NULL,
    payer_user_id                INTEGER,
    organization_id              BIGINT,
    sponsor_policy_ref           VARCHAR(120),
    approval_ref                 VARCHAR(120),

    budget_hash                  CHARACTER(64) NOT NULL,
    created_at                   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'defgeo_budget_version_unique'
                        AND conrelid = 'public.defgeo_provider_execution_budgets'::regclass) THEN
        ALTER TABLE public.defgeo_provider_execution_budgets
            ADD CONSTRAINT defgeo_budget_version_unique
            UNIQUE (tenant_owner_id, accepted_snapshot_id, service_projection_id,
                    scope_key, budget_version);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_defgeo_budget_nonneg'
                        AND conrelid = 'public.defgeo_provider_execution_budgets'::regclass) THEN
        ALTER TABLE public.defgeo_provider_execution_budgets
            ADD CONSTRAINT chk_defgeo_budget_nonneg
            CHECK (global_cap_points >= 0 AND scope_cap_points >= 0
                   AND scope_cap_points <= global_cap_points);
    END IF;
END $$;

-- @index-guard idx_defgeo_budget_lookup ON defgeo_provider_execution_budgets plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_budget_lookup' AND i.indrelid = to_regclass('public.defgeo_provider_execution_budgets')) THEN
        NULL;  -- 已在 public.defgeo_provider_execution_budgets 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_budget_lookup' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_budget_lookup 已存在但不在 public.defgeo_provider_execution_budgets 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_budget_lookup' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_budget_lookup ON public.defgeo_provider_execution_budgets (tenant_owner_id, accepted_snapshot_id, scope_key, budget_version DESC);
    END IF;
END $idxguard$;

-- @readiness-begin 044
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT 23 条 + @index-guard 12 个 + CREATE TRIGGER 1 个 + ADD COLUMN 0 列),
--    不是手挑的"承重"子集 —— 手挑清单漏掉的那一条不会让任何判据变红。
--    期望值 = 真 PG16 上 defgeo_ident_* 六个函数算出的**语义身份**(不是 pg_get_*def 的渲染文本)。
--    🔴 渲染文本会被 pg_dump→pg_restore / ALTER VALIDATE / 跨版本重写(语义等价、文本不等),
--       守全等就会在"第二次部署"必炸;换期望串只是把洞挪到全新库那一侧。
-- 🔴 索引**必须按 (表, 索引名) 判存**:索引名只在 schema 内唯一、不绑表,
--    别的表上有同名索引时"按名判存"会报绿,而目标表上其实一个都没有。
-- 🔴 触发器同理,而且更贵:`pg_trigger.tgname` 同样不绑表,且"它在"证明不了
--    "它拦得住" —— 所以这里核的是 (宿主表, 定义, tgenabled, 函数体 md5) 四样。
-- 🔴 列合同核 (format_type 带 typmod, attnotnull, pg_get_expr(adbin)) 三样:
--    只核 information_schema.data_type 的守卫对「错长度」零判别(varchar(64) 与
--    varchar(255) 的 data_type 都是 character varying),对「错 default」更是零核验。
-- 🔴 仍然零 DML:只 SELECT + RAISE(连 SAVEPOINT+ROLLBACK 的插入探针也不用)。
-- 🔴 下面这批身份函数由生成器**同一份常量**发射到每个 readiness 块里,
--    并且生成期算期望值用的**就是它们** —— 期望侧与运行期侧只有一份实现。
--    CREATE OR REPLACE 是 DDL、幂等,块单独重放也自带函数。
-- ══════════════════════════════════════════════════════════════════════
-- 结构化身份函数 —— readiness 块与生成器**共用同一份实现**
-- ══════════════════════════════════════════════════════════════════════
-- 🔴 [P0 · 2026-09-02] 为什么不再比 `pg_get_*def()` 的字符串:
--    `pg_dump -Fc` → `pg_restore --schema-only` 会把
--      CHECK (col IN (...))  在 varchar 列上从
--      `ANY ((ARRAY[...])::text[])` 重渲染成 `ANY (ARRAY[(...)::text, ...])`
--    —— 语义等价、文本不等。首次部署走 ADD 分支不比对所以过,**第二次起必炸**。
--    把期望串换成还原后那一形是陷阱:全新库(灾备重建/新环境)上又炸,只是把洞挪个位置。
--    去空白/小写也救不了:差异是**结构性**的(括号与 cast 层级)。
--    而且 dump/restore 只是触发重渲染的**路径之一** —— `ALTER … VALIDATE`、
--    跨大版本升级都可能再改渲染形。所以:**不比渲染,比语义身份**。
--
-- 🔴 全程只读系统表,零 DML(迁移体内禁 DML 是本仓铁律;
--    连 SAVEPOINT+ROLLBACK 的插入探针也算 DML)。
--
-- 🔴 五维,缺一漏一类:
--    ① contype/属性  ② conkey 列序集合  ③ convalidated
--    ④ **字面量集合**(排序去重;含数字)—— 抓 IN 闭集被改
--    ⑤ **运算符多重集**(token 计数)—— 抓 `> 0` 被改成 `< 0` 这种
--       字面量集合与 conkey 都不变的漂移。④ 单独用会漏它。
--    ⑤ 取自渲染文本,但只取 **token 多重集**,对括号/cast 层级改写稳定,
--    也不重造表达式树(重造 = 另一套 SQL 解析器,自己会漂)。

CREATE OR REPLACE FUNCTION public.defgeo_ident_literals(p_expr text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT COALESCE(string_agg(v, ',' ORDER BY v), '')
      FROM (
        SELECT DISTINCT m[1] AS v
          FROM regexp_matches(lower(COALESCE(p_expr, '')), '''([^'']*)''', 'g') AS m
        UNION
        SELECT DISTINCT m[1]
          FROM regexp_matches(lower(COALESCE(p_expr, '')),
                              '(?<![a-z_0-9.''])(-?[0-9]+(?:\.[0-9]+)?)', 'g') AS m
      ) s
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_ops(p_expr text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT COALESCE(string_agg(op || ':' || n::text, ',' ORDER BY op), '')
      FROM (
        SELECT m[1] AS op, count(*) AS n
          FROM regexp_matches(lower(COALESCE(p_expr, '')),
               '(<=|>=|<>|!=|=|<|>|\yany\y|\yall\y|\yin\y|\yand\y|\yor\y|\ynot\y|\yis\y|\ynull\y|\ylike\y|\ysimilar\y|\ybetween\y)',
               'g') AS m
         GROUP BY m[1]
      ) s
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_constraint(p_name text, p_table text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT c.contype::text
        || '|cols=' || COALESCE(
               (SELECT string_agg(a.attname, ',' ORDER BY a.attname)
                  FROM unnest(c.conkey) k
                  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k), '-')
        || '|fk=' || COALESCE(c.confrelid::regclass::text, '-')
        || '|fkcols=' || COALESCE(
               (SELECT string_agg(a.attname, ',' ORDER BY a.attname)
                  FROM unnest(c.confkey) k
                  JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k), '-')
        || '|upd=' || COALESCE(NULLIF(c.confupdtype::text, ''), '-')
        || '|del=' || COALESCE(NULLIF(c.confdeltype::text, ''), '-')
        || '|valid=' || c.convalidated::text
        || '|lits=' || public.defgeo_ident_literals(pg_get_expr(c.conbin, c.conrelid))
        || '|ops=' || public.defgeo_ident_ops(pg_get_expr(c.conbin, c.conrelid))
      FROM pg_constraint c
     WHERE c.conname = p_name AND c.conrelid = to_regclass(p_table)
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_index(p_name text, p_table text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT 'live=' || (i.indisvalid AND i.indisready)::text
        || '|unique=' || i.indisunique::text
        || '|cols=' || COALESCE(
               (SELECT string_agg(a.attname, ',' ORDER BY a.attname)
                  FROM unnest(i.indkey::int2[]) k
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k), '-')
        || '|am=' || am.amname
        || '|predlits=' || public.defgeo_ident_literals(pg_get_expr(i.indpred, i.indrelid))
        || '|predops=' || public.defgeo_ident_ops(pg_get_expr(i.indpred, i.indrelid))
        || '|exprlits=' || public.defgeo_ident_literals(pg_get_expr(i.indexprs, i.indrelid))
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
      JOIN pg_am am ON am.oid = c.relam
     WHERE c.relname = p_name AND i.indrelid = to_regclass(p_table)
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_column(p_table text, p_col text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT 'typ=' || a.atttypid::text
        || '|mod=' || a.atttypmod::text
        || '|notnull=' || a.attnotnull::text
        || '|deflits=' || public.defgeo_ident_literals(pg_get_expr(d.adbin, d.adrelid))
        || '|defops=' || public.defgeo_ident_ops(pg_get_expr(d.adbin, d.adrelid))
      FROM pg_attribute a
      LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE a.attrelid = to_regclass(p_table) AND a.attname = p_col
       AND a.attnum > 0 AND NOT a.attisdropped
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_trigger(p_name text, p_table text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT 'enabled=' || g.tgenabled::text
        || '|type=' || g.tgtype::text
        || '|fn=' || md5(regexp_replace(lower(pg_get_functiondef(g.tgfoid)), '\s+', '', 'g'))
      FROM pg_trigger g
     WHERE g.tgname = p_name AND g.tgrelid = to_regclass(p_table)
       AND NOT g.tgisinternal
$fn$;

DO $readiness044$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('chk_defgeo_budget_nonneg', 'public.defgeo_provider_execution_budgets', 'c|cols=global_cap_points,scope_cap_points|fk=-|fkcols=-|upd= |del= |valid=true|lits=0|ops=<=:1,>=:2,and:2'),
        ('chk_defgeo_pcmd_command_state', 'public.defgeo_publish_commands', 'c|cols=command_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=accepted,cancelled,completed,failed,needs_action,quarantined,queued,running,settlement_pending|ops==:1,any:1'),
        ('chk_defgeo_pcmd_external_start', 'public.defgeo_publish_commands', 'c|cols=external_start_at,external_start_token|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops==:1,is:2,null:2'),
        ('chk_defgeo_pcmd_freeze_handle', 'public.defgeo_publish_commands', 'c|cols=exact_settlement_points,freeze_backend,freeze_id,funding_policy,payer_user_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=0,organization_budget,personal_wallet|ops=<>:1,=:1,all:1,and:2,is:3,not:3,null:3,or:2'),
        ('chk_defgeo_pcmd_funding_policy', 'public.defgeo_publish_commands', 'c|cols=funding_policy|fk=-|fkcols=-|upd= |del= |valid=true|lits=admin_platform_ledger,organization_budget,personal_wallet,sponsor_platform_ledger|ops==:1,any:1'),
        ('chk_defgeo_pcmd_funding_state', 'public.defgeo_publish_commands', 'c|cols=funding_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=committed,exempt_recorded,frozen,pending_reconciliation,quarantined,released|ops==:1,any:1'),
        ('chk_defgeo_pcmd_lineage', 'public.defgeo_publish_commands', 'c|cols=command_generation,lineage_kind,parent_command_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=1,legal_repair,replacement,retry_child,root|ops==:3,>:1,and:4,any:1,is:2,not:1,null:2,or:1'),
        ('chk_defgeo_pcmd_platform_state', 'public.defgeo_publish_commands', 'c|cols=funding_state,principal_kind|fk=-|fkcols=-|upd= |del= |valid=true|lits=exempt_recorded,platform_cost_center|ops=<>:1,=:1,or:1'),
        ('chk_defgeo_pcmd_pub_state', 'public.defgeo_publish_commands', 'c|cols=canonical_publication_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=conflict,failed_no_effect,failed_unknown,not_started,queued,rejected_no_effect,rejected_unknown,reported_success_unverified,retracted,submitting,unknown,verified_published|ops==:1,any:1'),
        ('chk_defgeo_pds_consumed', 'public.defgeo_publish_decision_snapshots', 'c|cols=consumed_command_id,lifecycle|fk=-|fkcols=-|upd= |del= |valid=true|lits=consumed|ops=<>:1,=:1,and:2,is:2,not:1,null:2,or:1'),
        ('chk_defgeo_pds_lifecycle', 'public.defgeo_publish_decision_snapshots', 'c|cols=lifecycle|fk=-|fkcols=-|upd= |del= |valid=true|lits=cancelled,consumed,expired,open,superseded|ops==:1,any:1'),
        ('chk_defgeo_pds_successor', 'public.defgeo_publish_decision_snapshots', 'c|cols=lifecycle,superseded_by_snapshot_hash,superseded_by_snapshot_id,supersession_kind|fk=-|fkcols=-|upd= |del= |valid=true|lits=new_preview,override,replacement,superseded|ops=<>:1,=:2,and:6,any:1,is:5,not:2,null:5,or:1'),
        ('chk_defgeo_pout_status', 'public.defgeo_publish_outbox', 'c|cols=status|fk=-|fkcols=-|upd= |del= |valid=true|lits=claimed,dispatched,failed,needs_review,pending|ops==:1,any:1'),
        ('chk_defgeo_sre_hold_reason', 'public.defgeo_settlement_review_entries', 'c|cols=entry_kind,reason|fk=-|fkcols=-|upd= |del= |valid=true|lits=4,admin_hold|ops=<>:1,>=:1,and:1,is:1,not:1,null:1,or:1'),
        ('chk_defgeo_sre_kind', 'public.defgeo_settlement_review_entries', 'c|cols=entry_kind|fk=-|fkcols=-|upd= |del= |valid=true|lits=admin_commit,admin_hold,admin_release,provider_evidence|ops==:1,any:1'),
        ('chk_defgeo_sre_provider_readonly', 'public.defgeo_settlement_review_entries', 'c|cols=actor_role,entry_kind|fk=-|fkcols=-|upd= |del= |valid=true|lits=provider_evidence,service_provider|ops=<>:1,=:1,or:1'),
        ('chk_defgeo_sre_role', 'public.defgeo_settlement_review_entries', 'c|cols=actor_role|fk=-|fkcols=-|upd= |del= |valid=true|lits=platform_admin,service_provider|ops==:1,any:1'),
        ('defgeo_budget_version_unique', 'public.defgeo_provider_execution_budgets', 'u|cols=accepted_snapshot_id,budget_version,scope_key,service_projection_id,tenant_owner_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops='),
        ('defgeo_pcmd_slot_tenant_fk', 'public.defgeo_publish_commands', 'f|cols=publish_slot_id,tenant_owner_id|fk=defgeo_publish_slots|fkcols=publish_slot_id,tenant_owner_id|upd=a|del=a|valid=true|lits=|ops='),
        ('defgeo_pds_slot_tenant_fk', 'public.defgeo_publish_decision_snapshots', 'f|cols=publish_slot_id,tenant_owner_id|fk=defgeo_publish_slots|fkcols=publish_slot_id,tenant_owner_id|upd=a|del=a|valid=true|lits=|ops='),
        ('defgeo_pout_command_unique', 'public.defgeo_publish_outbox', 'u|cols=event_kind,publish_command_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops='),
        ('defgeo_publish_slots_natural_unique', 'public.defgeo_publish_slots', 'u|cols=accepted_snapshot_id,command_kind,plan_item_key,service_projection_id,tenant_owner_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops='),
        ('defgeo_publish_slots_tenant_unique', 'public.defgeo_publish_slots', 'u|cols=publish_slot_id,tenant_owner_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[044] 约束的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_constraint(r.cname, r.tname), c.convalidated
          INTO actual, ok
          FROM pg_constraint c
         WHERE c.conname = r.cname AND c.conrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION '[044] 约束 % 不在 % 上(可能被同名约束挡在了别的表)',
                r.cname, r.tname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[044] 约束 % 语义身份漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[044] 约束 % 是 NOT VALID —— 存量行从没被验过,守卫只守未来', r.cname;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('defgeo_pcmd_idem_root', 'public.defgeo_publish_commands', 'live=true|unique=true|cols=idempotency_key,publish_slot_id,request_canonical_hash,tenant_owner_id|am=btree|predlits=|predops=|exprlits='),
        ('defgeo_pcmd_one_committed_fulfillment', 'public.defgeo_publish_commands', 'live=true|unique=true|cols=publish_slot_id|am=btree|predlits=committed,retracted,verified_published|predops==:2,and:1,any:1|exprlits='),
        ('defgeo_pcmd_one_live_child', 'public.defgeo_publish_commands', 'live=true|unique=true|cols=parent_command_id|am=btree|predlits=cancelled,completed,failed|predops=<>:1,all:1,and:1,is:1,not:1,null:1|exprlits='),
        ('defgeo_pcmd_one_live_per_slot', 'public.defgeo_publish_commands', 'live=true|unique=true|cols=publish_slot_id|am=btree|predlits=cancelled,completed,failed,released|predops=<>:1,=:1,all:1,and:2,is:1,not:2,null:1|exprlits='),
        ('defgeo_pds_idem_root', 'public.defgeo_publish_decision_snapshots', 'live=true|unique=true|cols=idempotency_key,publish_slot_id,request_canonical_hash,tenant_owner_id|am=btree|predlits=|predops=|exprlits='),
        ('defgeo_pds_one_open_per_slot', 'public.defgeo_publish_decision_snapshots', 'live=true|unique=true|cols=publish_slot_id,tenant_owner_id|am=btree|predlits=open|predops==:1|exprlits='),
        ('idx_defgeo_budget_lookup', 'public.defgeo_provider_execution_budgets', 'live=true|unique=false|cols=accepted_snapshot_id,budget_version,scope_key,tenant_owner_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_pcmd_settlement_queue', 'public.defgeo_publish_commands', 'live=true|unique=false|cols=funding_state,updated_at|am=btree|predlits=pending_reconciliation,quarantined|predops==:1,any:1|exprlits='),
        ('idx_defgeo_pds_slot', 'public.defgeo_publish_decision_snapshots', 'live=true|unique=false|cols=publish_slot_id,snapshot_version|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_pout_claimable', 'public.defgeo_publish_outbox', 'live=true|unique=false|cols=available_at,status|am=btree|predlits=claimed,pending|predops==:1,any:1|exprlits='),
        ('idx_defgeo_pslot_tenant', 'public.defgeo_publish_slots', 'live=true|unique=false|cols=brand_id,tenant_owner_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_sre_command', 'public.defgeo_settlement_review_entries', 'live=true|unique=false|cols=created_at,publish_command_id|am=btree|predlits=|predops=|exprlits=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[044] 索引的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_index(r.cname, r.tname),
               (i.indisvalid AND i.indisready) INTO actual, ok
          FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
         WHERE c.relname = r.cname AND i.indrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[044] 索引 % 不在 % 上 —— 可能被**别的表上的同名索引**挡掉了'
                '(CREATE INDEX IF NOT EXISTS 按名判存不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT t2.relname FROM pg_class c2
                            JOIN pg_index i2 ON i2.indexrelid = c2.oid
                            JOIN pg_class t2 ON t2.oid = i2.indrelid
                           WHERE c2.relname = r.cname
                             AND c2.relnamespace = 'public'::regnamespace), '(没有同名索引)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[044] 索引 % 定义漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[044] 索引 % 的 indisvalid/indisready 不成立 —— '
                'CREATE INDEX CONCURRENTLY 失败留下的壳子文本与正品一模一样,'
                '但它不保证唯一性', r.cname;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('trg_defgeo_pds_freeze_guard', 'public.defgeo_publish_decision_snapshots', 'enabled=O|type=19|fn=35689a90d52f19e75432996c79032a62')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[044] 触发器的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_trigger(r.cname, r.tname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[044] 触发器 % 不在 % 上 —— 「按名判存」会被**别的表上的同名触发器**'
                '骗过(pg_trigger.tgname 不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT c2.relname FROM pg_trigger g2
                            JOIN pg_class c2 ON c2.oid = g2.tgrelid
                           WHERE g2.tgname = r.cname AND NOT g2.tgisinternal
                           LIMIT 1), '(没有同名触发器)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[044] 触发器 % 的定义/启用状态/函数体与预期不符 —— '
                '同名放行触发器、DISABLE 掉的触发器、被换成 RETURN NEW 的函数体,'
                '三种都长成「它在」的样子;期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
    END LOOP;
    RAISE NOTICE '[044] exact schema readiness 通过(0 列 + 23 约束 + 12 索引 + 1 触发器逐字对上)';
END $readiness044$;
-- @readiness-end 044
