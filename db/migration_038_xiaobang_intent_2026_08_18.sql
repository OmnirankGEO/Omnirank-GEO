-- ════════════════════════════════════════════════════════════════════════════
-- 038 · 小榜 vNext 意图协调层(WO_XIAOBANG_VNEXT_EXECUTION_2026-08-18 · WP1)
--
-- 建两张**纯新表**,零 DML,不碰任何既有表的任何一列:
--   ① xiaobang_operation_intents      —— prepare 产生的不可变操作意图 + 协调态
--   ② xiaobang_confirmation_receipts  —— confirm 的单次消费确认回执
--
-- 🔴 为什么不能窄扩现有表(T0 census 结论,规格 §9.2 要求先证再建):
--    · gap_assistant_audit(migration_029 §2.5)是**留痕表**:只有
--      assistant_request_id / facts_hash / output_hash / action_ids,
--      没有 intent 身份、没有 revision、没有确认回执、没有有效期、
--      没有 execution 关联。它的幂等边界是 (assistant_request_id, snapshot_version),
--      承载不了「同 prepare_request_id + 同 canonical input 必须返回同一个 intent」。
--      把协调态塞进留痕表 = 用业务终态字段承载助手协调态,§20.1 明令禁止。
--    · organization_approval_requests 是**组织审批**表,不是意图表;本层只
--      引用它的 id(approval_request_id),绝不复制它的状态机。
--    · publish_idempotency_keys 是发布端点的响应缓存(check-then-insert),
--      §16.1 已判定它承载不了 claim state / payload hash / 恢复水位。
--
-- 🔴 顺序无依赖:两张全新表,不引用任何既有表的外键,可排在清单任何位置。
--    (刻意不加 FK:organization/brand 的软删与租户迁移会让 FK 变成部署期地雷,
--     而归属校验本来就必须在**每个请求**里现做,不能靠 FK 假装做过。)
--
-- 🔴 漏跑的后果是**响亮的**(刻意选的方向):五阶段端点在 prepare 时显式
--    INSERT 这两张表 → UndefinedTable 当场抛出、端点 500。
--    这是新增能力,静默返空会让用户看到"小榜准备好了"而其实什么都没存 ——
--    一句假结论比一句"暂时不可用"坏得多。影响面被限制在新端点内,
--    小榜既有的 /context /chat /parse-image 一行都不受影响。
--
-- 🔴 重放安全(prestart 每次部署无条件重放全部迁移,无追踪表):
--    全 CREATE TABLE/INDEX IF NOT EXISTS + DO $$ 包 CHECK,×2 幂等。
--
-- 🔴 回滚 = DROP 这两张新表,零业务残留(本迁移不写任何既有表)。
--    但**不**由部署清单自动做:表里存的是用户已确认过的意图与回执,
--    DROP 掉等于把"我点过确认"这件事从审计里抹掉。真要回退请人工执行。
-- ════════════════════════════════════════════════════════════════════════════

-- ─────────────────────────────────────────────────────────────────
-- 1. 操作意图(协调态 · 不是业务终态)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xiaobang_operation_intents (
    id                            BIGSERIAL   PRIMARY KEY,
    intent_id                     TEXT        NOT NULL,

    -- 身份与归属。每个阶段都会按当前请求重验,这里存的是**当时**的快照,
    -- 用于比对漂移,不作为授权凭证。
    tenant_owner_id               INTEGER     NOT NULL,
    actor_user_id                 INTEGER     NOT NULL,
    payer_user_id                 INTEGER,
    organization_id               INTEGER,
    membership_version            TEXT,
    assignment_authority_version  TEXT,
    approval_policy_version       TEXT,
    permission_version            TEXT,

    -- operation 与 schema 版本
    operation_id                  TEXT        NOT NULL,
    operation_version             INTEGER     NOT NULL DEFAULT 1,
    registry_version              TEXT        NOT NULL,
    request_schema_version        TEXT        NOT NULL DEFAULT '1',
    response_schema_version       TEXT        NOT NULL DEFAULT '1',

    -- 两档确认制(规格 §8.1)。写进行里是为了让「给纯算力操作套确认门」
    -- 这种错配在数据层也留得下证据,不只活在内存对象里。
    side_effect                   TEXT        NOT NULL,
    confirmation_mode             TEXT        NOT NULL,

    -- 幂等与绑定
    interaction_id                TEXT,
    prepare_request_id            TEXT        NOT NULL,
    canonical_input_hash          CHAR(64)    NOT NULL,
    payload_hash                  CHAR(64)    NOT NULL,
    object_manifest_hash          CHAR(64)    NOT NULL,
    compute_quote_hash            CHAR(64),

    -- 双时钟(规格 §10.2 · P0-C):价格锁短 TTL 与审批等待窗口是**两只表**。
    -- 价格锁到期只代表要重新报价,不代表 intent 失效。
    pricing_version               TEXT,
    compute_quote_amount          INTEGER,
    compute_quote_unit            TEXT        NOT NULL DEFAULT '算力',
    compute_quote_expires_at      TIMESTAMPTZ,
    approval_window_expires_at    TIMESTAMPTZ,

    -- 协调态 + CAS
    intent_state                  TEXT        NOT NULL DEFAULT 'prepared',
    intent_revision               INTEGER     NOT NULL DEFAULT 1,

    -- 脱敏预览与理由(只存服务端事实,不存模型正文/供应商/成本)
    preview                       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    reason_facts                  JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- 现役对象引用(只持引用,不复制领域终态)
    approval_request_id           INTEGER,
    execution_id                  TEXT,
    execution_request_id          TEXT,
    domain_ref                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    last_observed_at              TIMESTAMPTZ,

    cancel_request_id             TEXT,
    superseded_by_intent_id       TEXT,

    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    -- 枚举写死在库里,应用层改一个字符也逃不过。CHECK 加成 NOT VALID 会让
    -- 存量行绕过 —— 这两张表是新表,零存量,直接 VALID。
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_xb_intent_state'
    ) THEN
        ALTER TABLE xiaobang_operation_intents
            ADD CONSTRAINT ck_xb_intent_state CHECK (intent_state IN (
                'prepared', 'awaiting_confirmation', 'confirmed',
                'approval_pending', 'approval_rejected', 'executable',
                'execution_linked', 'cancelled', 'expired', 'superseded'
            ));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_xb_intent_side_effect'
    ) THEN
        ALTER TABLE xiaobang_operation_intents
            ADD CONSTRAINT ck_xb_intent_side_effect
            CHECK (side_effect IN ('external', 'compute_only'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_xb_intent_confirm_mode'
    ) THEN
        ALTER TABLE xiaobang_operation_intents
            ADD CONSTRAINT ck_xb_intent_confirm_mode
            CHECK (confirmation_mode IN ('required_user_click', 'silent_with_notice'));
    END IF;
    -- 🔴 两档映射也写进库:external 只能配 required_user_click,
    --    compute_only 只能配 silent_with_notice。应用层已经拦一道(注册期),
    --    这里是第二道 —— 「给纯算力操作套确认门」的变异要在**两层**都红。
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_xb_intent_side_effect_confirm_map'
    ) THEN
        ALTER TABLE xiaobang_operation_intents
            ADD CONSTRAINT ck_xb_intent_side_effect_confirm_map CHECK (
                (side_effect = 'external'     AND confirmation_mode = 'required_user_click')
             OR (side_effect = 'compute_only' AND confirmation_mode = 'silent_with_notice')
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_xb_intent_revision_positive'
    ) THEN
        ALTER TABLE xiaobang_operation_intents
            ADD CONSTRAINT ck_xb_intent_revision_positive CHECK (intent_revision >= 1);
    END IF;
END $$;

-- @index-guard uq_xb_intent_id ON xiaobang_operation_intents unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_xb_intent_id' AND i.indrelid = to_regclass('public.xiaobang_operation_intents')) THEN
        NULL;  -- 已在 public.xiaobang_operation_intents 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_xb_intent_id' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_xb_intent_id 已存在但不在 public.xiaobang_operation_intents 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_xb_intent_id' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_xb_intent_id ON public.xiaobang_operation_intents (intent_id);
    END IF;
END $idxguard$;

-- 🔴 prepare 幂等的**唯一**保证物。规格 §10.2 原文:「并发 prepare 也必须依靠
--    数据库唯一约束/原子 claim,而不是先查后插」。仓内先例:
--    billing_deduction_idempotency 的 INSERT ... ON CONFLICT DO NOTHING RETURNING。
-- @index-guard uq_xb_intent_prepare_idem ON xiaobang_operation_intents unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_xb_intent_prepare_idem' AND i.indrelid = to_regclass('public.xiaobang_operation_intents')) THEN
        NULL;  -- 已在 public.xiaobang_operation_intents 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_xb_intent_prepare_idem' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_xb_intent_prepare_idem 已存在但不在 public.xiaobang_operation_intents 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_xb_intent_prepare_idem' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_xb_intent_prepare_idem ON public.xiaobang_operation_intents (tenant_owner_id, operation_id, prepare_request_id);
    END IF;
END $idxguard$;

-- @index-guard idx_xb_intent_actor ON xiaobang_operation_intents plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_xb_intent_actor' AND i.indrelid = to_regclass('public.xiaobang_operation_intents')) THEN
        NULL;  -- 已在 public.xiaobang_operation_intents 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_xb_intent_actor' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_xb_intent_actor 已存在但不在 public.xiaobang_operation_intents 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_xb_intent_actor' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_xb_intent_actor ON public.xiaobang_operation_intents (actor_user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_xb_intent_execution ON xiaobang_operation_intents plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_xb_intent_execution' AND i.indrelid = to_regclass('public.xiaobang_operation_intents')) THEN
        NULL;  -- 已在 public.xiaobang_operation_intents 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_xb_intent_execution' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_xb_intent_execution 已存在但不在 public.xiaobang_operation_intents 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_xb_intent_execution' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_xb_intent_execution ON public.xiaobang_operation_intents (execution_id) WHERE execution_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_xb_intent_approval ON xiaobang_operation_intents plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_xb_intent_approval' AND i.indrelid = to_regclass('public.xiaobang_operation_intents')) THEN
        NULL;  -- 已在 public.xiaobang_operation_intents 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_xb_intent_approval' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_xb_intent_approval 已存在但不在 public.xiaobang_operation_intents 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_xb_intent_approval' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_xb_intent_approval ON public.xiaobang_operation_intents (approval_request_id) WHERE approval_request_id IS NOT NULL;
    END IF;
END $idxguard$;

COMMENT ON TABLE xiaobang_operation_intents IS
    '小榜五阶段操作意图(协调态)。🔴 这里**不是**业务终态:'
    'queued/running/succeeded 归现役 domain task/order;已冻结/实扣/释放/退款归现役结算。'
    '本表只持引用与最后观察水位。禁写:供应商凭据、完整模型提示、跨租户正文、第二份业务终态。';
COMMENT ON COLUMN xiaobang_operation_intents.compute_quote_expires_at IS
    '价格锁短 TTL。到期只代表需要重新报价,**不**使 intent 或审批请求失效。';
COMMENT ON COLUMN xiaobang_operation_intents.approval_window_expires_at IS
    '审批等待窗口(长,可配)。approval_pending 的失效判据用本列,不用价格锁那只钟。';

-- ─────────────────────────────────────────────────────────────────
-- 2. 确认回执(单次消费 · 不是 bearer 凭证)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS xiaobang_confirmation_receipts (
    id                        BIGSERIAL   PRIMARY KEY,
    receipt_id                TEXT        NOT NULL,
    intent_id                 TEXT        NOT NULL,
    intent_revision           INTEGER     NOT NULL,
    actor_user_id             INTEGER     NOT NULL,

    -- 确认时刻的逐字绑定。execute 进事务后按这三项重验,消除 TOCTOU。
    bound_payload_hash        CHAR(64)    NOT NULL,
    bound_object_manifest_hash CHAR(64)   NOT NULL,
    bound_compute_quote_hash  CHAR(64),

    -- 「真实点击来自哪里」的审计证据。存的是 hash,不是可回放的 challenge 本身;
    -- 回执**不是** bearer 凭证,客户端不携带、不保管。
    challenge_hash            CHAR(64)    NOT NULL,
    user_agent_hash           CHAR(64),

    receipt_expires_at        TIMESTAMPTZ NOT NULL,
    consumed_at               TIMESTAMPTZ,
    consumed_by_execution_request_id TEXT,

    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- @index-guard uq_xb_receipt_id ON xiaobang_confirmation_receipts unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_xb_receipt_id' AND i.indrelid = to_regclass('public.xiaobang_confirmation_receipts')) THEN
        NULL;  -- 已在 public.xiaobang_confirmation_receipts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_xb_receipt_id' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_xb_receipt_id 已存在但不在 public.xiaobang_confirmation_receipts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_xb_receipt_id' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_xb_receipt_id ON public.xiaobang_confirmation_receipts (receipt_id);
    END IF;
END $idxguard$;

-- 🔴 同 intent + 同 revision 只允许**一张**回执。confirm 重放返回同一张,
--    不生成第二张(§19.1 #6)。这条唯一索引就是那句话的实现物。
-- @index-guard uq_xb_receipt_intent_revision ON xiaobang_confirmation_receipts unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_xb_receipt_intent_revision' AND i.indrelid = to_regclass('public.xiaobang_confirmation_receipts')) THEN
        NULL;  -- 已在 public.xiaobang_confirmation_receipts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_xb_receipt_intent_revision' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_xb_receipt_intent_revision 已存在但不在 public.xiaobang_confirmation_receipts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_xb_receipt_intent_revision' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_xb_receipt_intent_revision ON public.xiaobang_confirmation_receipts (intent_id, intent_revision);
    END IF;
END $idxguard$;

-- @index-guard idx_xb_receipt_unconsumed ON xiaobang_confirmation_receipts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_xb_receipt_unconsumed' AND i.indrelid = to_regclass('public.xiaobang_confirmation_receipts')) THEN
        NULL;  -- 已在 public.xiaobang_confirmation_receipts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_xb_receipt_unconsumed' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_xb_receipt_unconsumed 已存在但不在 public.xiaobang_confirmation_receipts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_xb_receipt_unconsumed' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_xb_receipt_unconsumed ON public.xiaobang_confirmation_receipts (intent_id) WHERE consumed_at IS NULL;
    END IF;
END $idxguard$;

COMMENT ON TABLE xiaobang_confirmation_receipts IS
    '小榜确认回执。单次消费,绑定 actor/intent/hash/version/expiry。'
    '🔴 不是 bearer 凭证:execute 在事务内按 intent_id 锁定并消费,'
    '不相信模型或客户端转交的 consent 字段。';

-- ─────────────────────────────────────────────────────────────────
-- 部署后自检(人工跑,两条都必须非空)
--   SELECT to_regclass('public.xiaobang_operation_intents');
--   SELECT to_regclass('public.xiaobang_confirmation_receipts');
-- 反向对照(必须**为空**,证明本迁移零 DML):
--   本文件不含任何 INSERT / UPDATE / DELETE / TRUNCATE 语句。
-- ─────────────────────────────────────────────────────────────────
