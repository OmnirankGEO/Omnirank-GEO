-- 服务商分销链路打通 · 工单 WO_INVENTORY_POINTS_DEADLOCK_2026-08-12
-- ===========================================================================
-- 🔴 本仓 prestart **每次部署无条件重放全部迁移 SQL**(无追踪表),
--    因此这里只允许**幂等 DDL**,不许有任何数据 UPDATE ——
--    迁移里的数据 UPDATE 是常驻地雷(2026-08-09 实证)。
--
-- 只新增两张表 + 索引,不动任何既有表结构、不动任何既有 CHECK。
-- 特别是 `agent_inventory_transactions_type_check`:工单红线明令不许动,
-- 本包用到的 admin_adjust / purchase_admin_adjust / allocate_to_customer_offline
-- **早已在白名单里**。
--
-- 🔴 [R1 返修 2026-08-12] search_path 必须 pin,且**两半一起做**:
--    ① 顶部 `SET LOCAL search_path`;② 所有对象名 `public.` 精确限定。
--    起因是一个真实的假绿:测试 conftest 在**同一条 psycopg2 连接**上先灌
--    `pg_dump --schema-only` 再灌本迁移,而 pg_dump 头部有
--    `SELECT pg_catalog.set_config('search_path','',false)` —— 它把该连接的
--    search_path 清空并**持续毒化后续语句**。dump 自己全是限定名所以照跑,
--    本迁移原来是裸名 → `InvalidSchemaName: no schema has been selected to create in`。
--    (我第一轮是用 psql 分两次会话手工预灌的,每次都是全新 search_path,
--     所以 bootstrap 这条路**从来没被真正跑过**,28 全绿是在预灌库上得到的。)
--    ⚠️ 只加 SET 不限定同样不行:search_path 首位是 pg_catalog,裸名建表会打到 pg_catalog。
--    ⚠️ prestart 是 autocommit(`scripts/prestart.py:63`),`SET LOCAL` 在其中是 no-op
--       (只发一条 warning),真正起作用的是 `public.` 限定;SET LOCAL 保的是
--       将来 prestart 改成事务式执行的那一天。两半各保一种场景,缺一不可。
-- ===========================================================================

SET LOCAL search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- 1. admin 库存动作留痕(工单验收判据 1:留痕必须带操作人与原因)
--    `agent_inventory_transactions` 只有 description,没有操作人字段;
--    与其往资金流水表上加列,不如单独一张治理留痕表 —— 不碰资金表结构。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.agent_inventory_admin_actions (
    id                BIGSERIAL PRIMARY KEY,
    action            TEXT      NOT NULL,
    agent_user_id     INTEGER   NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    customer_user_id  INTEGER            REFERENCES public.users(id) ON DELETE RESTRICT,
    paid_points       BIGINT    NOT NULL DEFAULT 0,
    bonus_points      BIGINT    NOT NULL DEFAULT 0,
    related_order_id  TEXT,
    operator_user_id  INTEGER   NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    operator_username TEXT,
    reason            TEXT      NOT NULL,
    request_id        TEXT      NOT NULL,
    ip_address        TEXT,
    before_snapshot   JSONB     NOT NULL DEFAULT '{}'::jsonb,
    after_snapshot    JSONB     NOT NULL DEFAULT '{}'::jsonb,
    evidence_jsonb    JSONB     NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'chk_agent_inv_admin_action'
           AND conrelid = 'public.agent_inventory_admin_actions'::regclass
    ) THEN
        ALTER TABLE public.agent_inventory_admin_actions
            ADD CONSTRAINT chk_agent_inv_admin_action CHECK (
                action = ANY (ARRAY[
                    'adjust_increase', 'adjust_decrease',
                    'allocate_to_customer', 'self_use_conversion'
                ])
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'chk_agent_inv_admin_reason'
           AND conrelid = 'public.agent_inventory_admin_actions'::regclass
    ) THEN
        ALTER TABLE public.agent_inventory_admin_actions
            ADD CONSTRAINT chk_agent_inv_admin_reason CHECK (
                pg_catalog.char_length(pg_catalog.btrim(reason)) BETWEEN 2 AND 500
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'chk_agent_inv_admin_points'
           AND conrelid = 'public.agent_inventory_admin_actions'::regclass
    ) THEN
        ALTER TABLE public.agent_inventory_admin_actions
            ADD CONSTRAINT chk_agent_inv_admin_points CHECK (
                paid_points >= 0 AND bonus_points >= 0
                AND (paid_points + bonus_points) > 0
            );
    END IF;
END $$;

-- 同一个 X-Request-ID + 同一动作只能落一次:资金动作宁可撞唯一键报错,
-- 也不要悄悄扣第二次(双击/重试的 fail-closed)。
-- @index-guard ux_agent_inv_admin_request ON agent_inventory_admin_actions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_agent_inv_admin_request' AND i.indrelid = to_regclass('public.agent_inventory_admin_actions')) THEN
        NULL;  -- 已在 public.agent_inventory_admin_actions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_agent_inv_admin_request' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_agent_inv_admin_request 已存在但不在 public.agent_inventory_admin_actions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_agent_inv_admin_request' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_agent_inv_admin_request ON public.agent_inventory_admin_actions (request_id, action);
    END IF;
END $idxguard$;
-- @index-guard idx_agent_inv_admin_agent ON agent_inventory_admin_actions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_agent_inv_admin_agent' AND i.indrelid = to_regclass('public.agent_inventory_admin_actions')) THEN
        NULL;  -- 已在 public.agent_inventory_admin_actions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_agent_inv_admin_agent' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_agent_inv_admin_agent 已存在但不在 public.agent_inventory_admin_actions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_agent_inv_admin_agent' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_agent_inv_admin_agent ON public.agent_inventory_admin_actions (agent_user_id, created_at DESC);
    END IF;
END $idxguard$;


-- ---------------------------------------------------------------------------
-- 2. 渠道合作申请(工单 §P0-2:服务商发展下级服务商)
--    渠道关系本身仍然只由 admin_user_governance.change_channel_relationship 写,
--    这张表只承载「申请 → 审批」这一段流程状态。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.channel_partner_requests (
    id                            BIGSERIAL PRIMARY KEY,
    requester_user_id             INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    target_user_id                INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    proposed_cost_multiplier_bps  INTEGER NOT NULL,
    approved_cost_multiplier_bps  INTEGER,
    status                        TEXT    NOT NULL DEFAULT 'pending',
    reason                        TEXT    NOT NULL,
    decision_note                 TEXT,
    decided_by                    INTEGER REFERENCES public.users(id) ON DELETE RESTRICT,
    decided_at                    TIMESTAMPTZ,
    relationship_id               BIGINT,
    request_id                    TEXT    NOT NULL,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'chk_channel_partner_req_status'
           AND conrelid = 'public.channel_partner_requests'::regclass
    ) THEN
        ALTER TABLE public.channel_partner_requests
            ADD CONSTRAINT chk_channel_partner_req_status CHECK (
                status = ANY (ARRAY['pending', 'approved', 'rejected', 'cancelled'])
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'chk_channel_partner_req_bps'
           AND conrelid = 'public.channel_partner_requests'::regclass
    ) THEN
        -- 与 services/channel_pricing.MIN_COST_MULTIPLIER_BPS 同源:
        -- 上游赚差价模型下,下级进货价不得低于上游有效成本。
        ALTER TABLE public.channel_partner_requests
            ADD CONSTRAINT chk_channel_partner_req_bps CHECK (
                proposed_cost_multiplier_bps >= 10000
                AND (approved_cost_multiplier_bps IS NULL
                     OR approved_cost_multiplier_bps >= 10000)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'chk_channel_partner_req_not_self'
           AND conrelid = 'public.channel_partner_requests'::regclass
    ) THEN
        ALTER TABLE public.channel_partner_requests
            ADD CONSTRAINT chk_channel_partner_req_not_self CHECK (
                requester_user_id <> target_user_id
            );
    END IF;
END $$;

-- 一个目标账号同时只允许一条待审批申请:否则两个上游同时申请同一个下级,
-- 审批顺序会决定分润链归谁 —— 那是竞态,不是业务规则。
-- @index-guard ux_channel_partner_req_pending_target ON channel_partner_requests unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_channel_partner_req_pending_target' AND i.indrelid = to_regclass('public.channel_partner_requests')) THEN
        NULL;  -- 已在 public.channel_partner_requests 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_channel_partner_req_pending_target' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_channel_partner_req_pending_target 已存在但不在 public.channel_partner_requests 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_channel_partner_req_pending_target' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_channel_partner_req_pending_target ON public.channel_partner_requests (target_user_id) WHERE status = 'pending';
    END IF;
END $idxguard$;
-- @index-guard idx_channel_partner_req_requester ON channel_partner_requests plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_channel_partner_req_requester' AND i.indrelid = to_regclass('public.channel_partner_requests')) THEN
        NULL;  -- 已在 public.channel_partner_requests 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_channel_partner_req_requester' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_channel_partner_req_requester 已存在但不在 public.channel_partner_requests 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_channel_partner_req_requester' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_channel_partner_req_requester ON public.channel_partner_requests (requester_user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_channel_partner_req_status ON channel_partner_requests plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_channel_partner_req_status' AND i.indrelid = to_regclass('public.channel_partner_requests')) THEN
        NULL;  -- 已在 public.channel_partner_requests 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_channel_partner_req_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_channel_partner_req_status 已存在但不在 public.channel_partner_requests 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_channel_partner_req_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_channel_partner_req_status ON public.channel_partner_requests (status, created_at DESC);
    END IF;
END $idxguard$;
