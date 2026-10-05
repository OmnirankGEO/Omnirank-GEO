-- ============================================================================
-- 034 · GEO 图文创作与发布 · 合同身份 / 幂等 / 逐项资金 additive schema
--   工单 WO_GEO_IMAGE_NOTE_EXECUTION_2026-08-17 · 规格 02 §3 / §13
--   基线:生产尖 ecce9985293467bcc6765798609a7b2882e0b8a8(2026-08-17 state.sh 实测)
--
-- 🔴 纪律(工单 §2 禁区,逐条对照):
--   · **additive 零 DML**。全文件无 UPDATE / DELETE / INSERT。prestart 无条件重放,
--     所有语句 IF NOT EXISTS 幂等。历史行一律留 NULL,不靠 ADD DEFAULT 改旧行语义。
--   · **不碰 geo_article_* 六表**。dormant slot sidecar 的激活口径由窄 RFC
--     (docs/AI-CONTEXT/RFC_GEO_IMAGE_NOTE_SLOT_SIDECAR_ACTIVATION_2026-08-17.md)
--     单独申请;Review 未批复前本迁移对那六表零改动。
--   · 表名以**现尖实测**为准:生产任务表真名是 `geo_douyin_post_tasks`
--     (规格 02 §3.5 写的 `geo_douyin_tasks` 在生产不存在 —— 2026-08-17 只读枚举 pg_tables 实证)。
--
-- 🔴 为什么 billing_mode / settlement_authority **不给 DEFAULT 也不 backfill**:
--   规格 §13 原文要求把现有行 backfill 成 'deduct_upfront'+'legacy_refund',那是 DML,
--   与工单 §2「迁移 additive 零 DML」冲突 —— 工单优先。改用「NULL = legacy」口径:
--     · 新链 SQL 必须显式匹配 billing_mode = 'freeze_per_item'(NULL 匹配不上,天然不穿透);
--     · legacy 退款/清理 SQL 必须写 (billing_mode IS NULL OR billing_mode = 'deduct_upfront')。
--   这样零 DML 与「NULL 老行不得误穿透新链」两条同时成立。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. geo_douyin_posts · 商业交付身份 + 付款方/操作者 + 选题 + 成品版本
-- ---------------------------------------------------------------------------
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS tenant_owner_user_id BIGINT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS payer_user_id        BIGINT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS actor_user_id        BIGINT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS payer_policy_snapshot JSONB;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS quote_id             BIGINT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS contract_revision_id BIGINT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS delivery_slot_key    UUID;
-- counts_toward_contract 刻意 **nullable 且无 DEFAULT**:规格 02 §3.2 明令
-- 「旧行不靠 ADD DEFAULT 自动改语义」。NULL/false = 不计合同,新 contract 行显式写 true。
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS counts_toward_contract BOOLEAN;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS source_mode          TEXT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS production_batch_id  UUID;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS batch_item_request_id UUID;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS batch_item_ordinal   INTEGER;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS distill_task_id      BIGINT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS topic_ref            TEXT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS topic_snapshot       JSONB;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS active_revision_id   BIGINT;
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS active_generation_task_id BIGINT;
-- generation_epoch 是**唯一**需要 NOT NULL DEFAULT 的列:迟到任务的 CAS 谓词直接读它,
-- NULL 会让 `WHERE generation_epoch = ?` 恒不命中 ⇒ 所有写入静默丢失(fail-open,最坏方向)。
-- ADD COLUMN ... DEFAULT 在 PG11+ 是元数据操作,不重写表、不是 DML。
ALTER TABLE geo_douyin_posts ADD COLUMN IF NOT EXISTS generation_epoch BIGINT NOT NULL DEFAULT 0;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_douyin_posts_source_mode') THEN
        ALTER TABLE geo_douyin_posts ADD CONSTRAINT ck_geo_douyin_posts_source_mode
            CHECK (source_mode IS NULL OR source_mode IN ('contract', 'manual', 'legacy'));
    END IF;
END $$;

-- 一个 slot 同时最多一个 active 成品(规格 02 §3.2)。partial unique 只约束
-- 「已绑 slot 且未软删」的行 —— superseded/删除的保留 lineage,不占 active 名额。
-- @index-guard uq_geo_douyin_posts_active_slot ON geo_douyin_posts unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_posts_active_slot' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_posts_active_slot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_posts_active_slot 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_posts_active_slot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_posts_active_slot ON public.geo_douyin_posts (delivery_slot_key) WHERE delivery_slot_key IS NOT NULL AND deleted_at IS NULL;
    END IF;
END $idxguard$;

-- @index-guard idx_geo_douyin_posts_quote ON geo_douyin_posts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_posts_quote' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_posts_quote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_posts_quote 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_posts_quote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_posts_quote ON public.geo_douyin_posts (quote_id, contract_revision_id, batch_item_ordinal) WHERE quote_id IS NOT NULL AND deleted_at IS NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_posts_batch ON geo_douyin_posts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_posts_batch' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_posts_batch' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_posts_batch 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_posts_batch' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_posts_batch ON public.geo_douyin_posts (production_batch_id) WHERE production_batch_id IS NOT NULL;
    END IF;
END $idxguard$;
-- 批内幂等项身份:同一批次内 item_request_id 唯一(规格 02 §3.7「item request key 物理作用域」)
-- @index-guard uq_geo_douyin_posts_batch_item ON geo_douyin_posts unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_posts_batch_item' AND i.indrelid = to_regclass('public.geo_douyin_posts')) THEN
        NULL;  -- 已在 public.geo_douyin_posts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_posts_batch_item' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_posts_batch_item 已存在但不在 public.geo_douyin_posts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_posts_batch_item' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_posts_batch_item ON public.geo_douyin_posts (production_batch_id, batch_item_request_id) WHERE production_batch_id IS NOT NULL AND batch_item_request_id IS NOT NULL;
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 2. geo_douyin_post_revisions · 不可变成品版本(规格 02 §3.3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geo_douyin_post_revisions (
    post_revision_id    BIGSERIAL PRIMARY KEY,
    geo_post_id         BIGINT NOT NULL REFERENCES geo_douyin_posts(id),
    revision_no         INTEGER NOT NULL,
    base_revision_id    BIGINT,
    created_by          INTEGER NOT NULL,
    operation_kind      TEXT NOT NULL,
    title               TEXT,
    body                TEXT,
    hashtags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    contact_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    cards_snapshot      JSONB NOT NULL DEFAULT '[]'::jsonb,
    asset_manifest      JSONB NOT NULL DEFAULT '{}'::jsonb,
    manifest_hash       CHARACTER(64),
    topic_snapshot_hash CHARACTER(64),
    render_input_hash   CHARACTER(64),
    style_catalog_version TEXT,
    status              TEXT NOT NULL DEFAULT 'staged',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at        TIMESTAMPTZ,
    CONSTRAINT ck_geo_douyin_post_rev_status
        CHECK (status IN ('staged', 'active', 'superseded', 'failed')),
    CONSTRAINT ck_geo_douyin_post_rev_operation
        CHECK (operation_kind IN ('create', 'edit', 'regenerate', 'redraw', 'restyle')),
    CONSTRAINT ck_geo_douyin_post_rev_no CHECK (revision_no >= 1)
);
-- @index-guard uq_geo_douyin_post_rev_no ON geo_douyin_post_revisions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_post_rev_no' AND i.indrelid = to_regclass('public.geo_douyin_post_revisions')) THEN
        NULL;  -- 已在 public.geo_douyin_post_revisions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_post_rev_no' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_post_rev_no 已存在但不在 public.geo_douyin_post_revisions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_post_rev_no' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_post_rev_no ON public.geo_douyin_post_revisions (geo_post_id, revision_no);
    END IF;
END $idxguard$;
-- 同一 post 同时最多一个 active revision(CAS 切换的物理保证)
-- @index-guard uq_geo_douyin_post_rev_active ON geo_douyin_post_revisions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_post_rev_active' AND i.indrelid = to_regclass('public.geo_douyin_post_revisions')) THEN
        NULL;  -- 已在 public.geo_douyin_post_revisions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_post_rev_active' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_post_rev_active 已存在但不在 public.geo_douyin_post_revisions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_post_rev_active' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_post_rev_active ON public.geo_douyin_post_revisions (geo_post_id) WHERE status = 'active';
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 3. geo_douyin_production_batches · 制作草稿/批次头(规格 02 §3.6)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geo_douyin_production_batches (
    batch_id              UUID PRIMARY KEY,
    tenant_owner_user_id  BIGINT NOT NULL,
    payer_user_id         BIGINT NOT NULL,
    actor_user_id         BIGINT NOT NULL,
    payer_policy_snapshot JSONB,
    brand_id              INTEGER,
    quote_id              BIGINT,
    contract_revision_id  BIGINT,
    etag_revision         INTEGER NOT NULL DEFAULT 1,
    request_id            UUID,
    request_hash          CHARACTER(64),
    price_snapshot        JSONB NOT NULL DEFAULT '{}'::jsonb,
    expected_total_price_points INTEGER,
    status                TEXT NOT NULL DEFAULT 'draft',
    approval_request_id   TEXT,
    approval_policy_version TEXT,
    approval_payload_hash CHARACTER(64),
    created_by            INTEGER NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_geo_douyin_batch_status CHECK (status IN (
        'draft', 'pending_approval', 'accepted', 'processing',
        'partial_success', 'completed', 'failed', 'cancelled', 'needs_action')),
    CONSTRAINT ck_geo_douyin_batch_etag CHECK (etag_revision >= 1)
);
-- @index-guard uq_geo_douyin_batch_request ON geo_douyin_production_batches unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_batch_request' AND i.indrelid = to_regclass('public.geo_douyin_production_batches')) THEN
        NULL;  -- 已在 public.geo_douyin_production_batches 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_batch_request' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_batch_request 已存在但不在 public.geo_douyin_production_batches 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_batch_request' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_batch_request ON public.geo_douyin_production_batches (tenant_owner_user_id, request_id) WHERE request_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_batch_quote ON geo_douyin_production_batches plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_batch_quote' AND i.indrelid = to_regclass('public.geo_douyin_production_batches')) THEN
        NULL;  -- 已在 public.geo_douyin_production_batches 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_batch_quote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_batch_quote 已存在但不在 public.geo_douyin_production_batches 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_batch_quote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_batch_quote ON public.geo_douyin_production_batches (quote_id, created_at DESC) WHERE quote_id IS NOT NULL;
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 4. geo_douyin_publish_artifacts · 持久化发布素材(规格 02 §7.1 / §11.3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS geo_douyin_publish_artifacts (
    prepared_artifact_id  BIGSERIAL PRIMARY KEY,
    geo_post_id           BIGINT NOT NULL REFERENCES geo_douyin_posts(id),
    post_revision_id      BIGINT NOT NULL,
    tenant_owner_user_id  BIGINT NOT NULL,
    request_id            UUID NOT NULL,
    request_hash          CHARACTER(64) NOT NULL,
    manifest_hash         CHARACTER(64),
    state                 TEXT NOT NULL DEFAULT 'preparing',
    card_statuses         JSONB NOT NULL DEFAULT '[]'::jsonb,
    lease_owner           TEXT,
    lease_expires_at      TIMESTAMPTZ,
    heartbeat_at          TIMESTAMPTZ,
    external_started_at   TIMESTAMPTZ,
    status_version        INTEGER NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_geo_douyin_artifact_state
        CHECK (state IN ('preparing', 'ready', 'failed', 'unknown'))
);
-- @index-guard uq_geo_douyin_artifact_request ON geo_douyin_publish_artifacts unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_artifact_request' AND i.indrelid = to_regclass('public.geo_douyin_publish_artifacts')) THEN
        NULL;  -- 已在 public.geo_douyin_publish_artifacts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_artifact_request' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_artifact_request 已存在但不在 public.geo_douyin_publish_artifacts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_artifact_request' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_artifact_request ON public.geo_douyin_publish_artifacts (tenant_owner_user_id, request_id);
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_artifact_post ON geo_douyin_publish_artifacts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_artifact_post' AND i.indrelid = to_regclass('public.geo_douyin_publish_artifacts')) THEN
        NULL;  -- 已在 public.geo_douyin_publish_artifacts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_artifact_post' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_artifact_post 已存在但不在 public.geo_douyin_publish_artifacts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_artifact_post' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_artifact_post ON public.geo_douyin_publish_artifacts (geo_post_id, post_revision_id);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 5. geo_douyin_post_tasks · 幂等 / 批次 / 租约 / 结算(规格 02 §3.5)
--    🔴 真名是 post_tasks(不是规格写的 geo_douyin_tasks),已带 task_ref + freeze_id
-- ---------------------------------------------------------------------------
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS request_id            UUID;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS idempotency_key       TEXT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS endpoint              TEXT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS production_batch_id   UUID;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS batch_item_request_id UUID;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS batch_item_ordinal    INTEGER;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS request_hash          CHARACTER(64);
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS base_revision_id      BIGINT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS generation_epoch      BIGINT NOT NULL DEFAULT 0;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS request_snapshot      JSONB;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS lease_owner           TEXT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS lease_expires_at      TIMESTAMPTZ;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS heartbeat_at          TIMESTAMPTZ;
-- 🔴 [WP3 返修 2026-08-17] 这一列是**崩溃恢复的全部判别力所在**,第一版漏了。
--    规格 02 §8.3:「服务商 external-start 前后分别记录状态;返回后、DB 终态前崩溃时
--    先 sync,不盲重投」。没有它,「租约过期可重跑」与「已调过供应商不能重跑」
--    在 DB 里长得一模一样,reconciler 只能二选一:全重跑(重复外调+重复扣费)
--    或全不跑(任务永久卡死)——两个都是错的。
--    是本包自己的 durable worker 判据把这个漏列顶出来的(模块整个围绕它设计,
--    却没建列 ⇒ 上生产即 UndefinedColumn)。
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS external_started_at   TIMESTAMPTZ;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS superseded_at         TIMESTAMPTZ;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS result_hash           CHARACTER(64);
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS settlement_status     TEXT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS settlement_authority  TEXT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS organization_charge_link_id BIGINT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS execution_id          TEXT;
-- direct_freeze 完整句柄(规格 02 §3.7:不同 freeze 表的相同数字 id 会碰撞,必须整组冻结)
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS freeze_table          TEXT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS payer_user_id         BIGINT;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS reserved_amount       INTEGER;
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS physical_split_snapshot JSONB;
-- 🔴 [返工 2026-08-18 · Codex P1-6] 制作任务也要记 billing_mode。
--    没有它,「这一行的钱是 freeze_per_item 还是老 deduct_upfront」只能靠
--    settlement_authority 反推 —— 而反推在 legacy_refund 那一档是歧义的。
--    退款/清理/对账三处 SQL 都要按这一列分流,列不存在就只能各自猜。
--    additive:无 DEFAULT 无 backfill,存量行为 NULL(= 老口径,谓词天然匹配)。
ALTER TABLE geo_douyin_post_tasks ADD COLUMN IF NOT EXISTS billing_mode TEXT;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_douyin_task_settlement_status') THEN
        ALTER TABLE geo_douyin_post_tasks ADD CONSTRAINT ck_geo_douyin_task_settlement_status
            CHECK (settlement_status IS NULL OR settlement_status IN (
                'exempt', 'frozen', 'settlement_pending', 'committed',
                'released', 'quarantined', 'manual'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_douyin_task_settlement_authority') THEN
        ALTER TABLE geo_douyin_post_tasks ADD CONSTRAINT ck_geo_douyin_task_settlement_authority
            CHECK (settlement_authority IS NULL OR settlement_authority IN (
                'organization_charge', 'direct_freeze', 'admin_exempt', 'legacy_refund'));
    END IF;
    -- 权威句柄互斥 + 各自完整(规格 02 §3.7 末段)。
    -- 🔴 admin_exempt 也要求 direct 整组非空 —— 裁定 P0-6 已把它从「零记账」改成
    --    「对平台直营账号 136 做真实 freeze/commit」,所以它持有的是 136 账户的 direct handle。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_douyin_task_authority_shape') THEN
        ALTER TABLE geo_douyin_post_tasks ADD CONSTRAINT ck_geo_douyin_task_authority_shape
            CHECK (
                settlement_authority IS NULL
                OR (settlement_authority = 'organization_charge'
                    AND organization_charge_link_id IS NOT NULL
                    AND freeze_id IS NULL AND freeze_table IS NULL)
                OR (settlement_authority IN ('direct_freeze', 'admin_exempt')
                    AND organization_charge_link_id IS NULL
                    AND freeze_id IS NOT NULL AND freeze_table IS NOT NULL
                    AND payer_user_id IS NOT NULL AND reserved_amount IS NOT NULL)
                OR settlement_authority = 'legacy_refund'
            );
    END IF;
END $$;

-- principal + endpoint + idempotency key 唯一(规格 02 §3.5「至少需要」)
-- @index-guard uq_geo_douyin_task_idem ON geo_douyin_post_tasks unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_task_idem' AND i.indrelid = to_regclass('public.geo_douyin_post_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_post_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_task_idem' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_task_idem 已存在但不在 public.geo_douyin_post_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_task_idem' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_task_idem ON public.geo_douyin_post_tasks (user_id, endpoint, idempotency_key) WHERE idempotency_key IS NOT NULL AND endpoint IS NOT NULL;
    END IF;
END $idxguard$;
-- 同 post 同时最多一个 active generation
-- @index-guard uq_geo_douyin_task_active_generation ON geo_douyin_post_tasks unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_task_active_generation' AND i.indrelid = to_regclass('public.geo_douyin_post_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_post_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_task_active_generation' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_task_active_generation 已存在但不在 public.geo_douyin_post_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_task_active_generation' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_task_active_generation ON public.geo_douyin_post_tasks (post_id) WHERE status IN ('pending', 'running') AND superseded_at IS NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_douyin_task_lease ON geo_douyin_post_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_douyin_task_lease' AND i.indrelid = to_regclass('public.geo_douyin_post_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_post_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_douyin_task_lease' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_douyin_task_lease 已存在但不在 public.geo_douyin_post_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_douyin_task_lease' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_douyin_task_lease ON public.geo_douyin_post_tasks (status, lease_expires_at) WHERE status IN ('pending', 'running');
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 6. geo_douyin_distill_tasks · quote 作用域选题身份(规格 02 §3.4)
-- ---------------------------------------------------------------------------
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS tenant_owner_user_id BIGINT;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS quote_id             BIGINT;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS contract_revision_id BIGINT;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS request_id           UUID;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS request_hash         CHARACTER(64);
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS endpoint             TEXT;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS requested_slot_keys  JSONB;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS result_snapshot_hash CHARACTER(64);
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS topic_ref_version    TEXT;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS retry_of_task_id     BIGINT;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS attempt_no           INTEGER;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS lease_owner          TEXT;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS lease_expires_at     TIMESTAMPTZ;
ALTER TABLE geo_douyin_distill_tasks ADD COLUMN IF NOT EXISTS heartbeat_at         TIMESTAMPTZ;

-- 永久唯一 (owner, endpoint, request_id) —— 规格 02 §3.4。
-- 🔴 既有 `uq_geo_douyin_distill_inflight` 是 **brand-wide** inflight 唯一:同一品牌两张
--    报价不能并发蒸馏。规格要求换成 contract-scoped。但**替换既有约束会改变 legacy
--    manual lane 的行为**,属于行为变更而非 additive ⇒ 本迁移只**新增** contract-scoped
--    的部分唯一索引(只约束带 quote_id 的新链行),旧约束原样保留给 manual lane。
--    等 RFC 批复 + cutover 完成后再单独评审是否收回旧约束。
-- @index-guard uq_geo_douyin_distill_request ON geo_douyin_distill_tasks unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_distill_request' AND i.indrelid = to_regclass('public.geo_douyin_distill_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_distill_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_distill_request' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_distill_request 已存在但不在 public.geo_douyin_distill_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_distill_request' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_distill_request ON public.geo_douyin_distill_tasks (tenant_owner_user_id, endpoint, request_id) WHERE request_id IS NOT NULL AND endpoint IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard uq_geo_douyin_distill_contract_inflight ON geo_douyin_distill_tasks unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_douyin_distill_contract_inflight' AND i.indrelid = to_regclass('public.geo_douyin_distill_tasks')) THEN
        NULL;  -- 已在 public.geo_douyin_distill_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_douyin_distill_contract_inflight' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_douyin_distill_contract_inflight 已存在但不在 public.geo_douyin_distill_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_douyin_distill_contract_inflight' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_douyin_distill_contract_inflight ON public.geo_douyin_distill_tasks (quote_id, contract_revision_id) WHERE quote_id IS NOT NULL AND status IN ('pending', 'running');
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 7. publish_idempotency_keys · 升为窄 command 协调 header(规格 02 §3.7)
--    不另建订单引擎:root/retry/legacy 三种记录同表,靠 record_kind 分。
-- ---------------------------------------------------------------------------
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS record_kind      TEXT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS command_id       TEXT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS root_request_id  TEXT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS coordination_state TEXT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS root_version     INTEGER;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS request_hash     CHARACTER(64);
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS tenant_owner_user_id BIGINT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS principal_user_id BIGINT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS payer_user_id    BIGINT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS actor_user_id    BIGINT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS actor_kind       TEXT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS organization_id  BIGINT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS membership_id    BIGINT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS membership_version INTEGER;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS payer_policy_snapshot JSONB;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS approval_request_id TEXT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS approval_policy_version TEXT;
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS approval_payload_hash CHARACTER(64);
ALTER TABLE publish_idempotency_keys ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_publish_idem_record_kind') THEN
        ALTER TABLE publish_idempotency_keys ADD CONSTRAINT ck_publish_idem_record_kind
            CHECK (record_kind IS NULL OR record_kind IN ('legacy', 'root', 'retry'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_publish_idem_coordination_state') THEN
        ALTER TABLE publish_idempotency_keys ADD CONSTRAINT ck_publish_idem_coordination_state
            CHECK (coordination_state IS NULL OR coordination_state IN (
                'claimed', 'pending_approval', 'materialized', 'coordination_failed'));
    END IF;
    -- root 有非空 command_id 且 root_request_id 为空;retry 反之。legacy 两者皆空。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_publish_idem_kind_shape') THEN
        ALTER TABLE publish_idempotency_keys ADD CONSTRAINT ck_publish_idem_kind_shape
            CHECK (
                record_kind IS NULL
                OR (record_kind = 'legacy' AND command_id IS NULL AND root_request_id IS NULL)
                OR (record_kind = 'root'   AND command_id IS NOT NULL AND root_request_id IS NULL)
                OR (record_kind = 'retry'  AND command_id IS NULL AND root_request_id IS NOT NULL)
            );
    END IF;
END $$;

-- @index-guard uq_publish_idem_command_id ON publish_idempotency_keys unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_publish_idem_command_id' AND i.indrelid = to_regclass('public.publish_idempotency_keys')) THEN
        NULL;  -- 已在 public.publish_idempotency_keys 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_publish_idem_command_id' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_publish_idem_command_id 已存在但不在 public.publish_idempotency_keys 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_publish_idem_command_id' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_publish_idem_command_id ON public.publish_idempotency_keys (command_id) WHERE command_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_publish_idem_root ON publish_idempotency_keys plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_publish_idem_root' AND i.indrelid = to_regclass('public.publish_idempotency_keys')) THEN
        NULL;  -- 已在 public.publish_idempotency_keys 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_publish_idem_root' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_publish_idem_root 已存在但不在 public.publish_idempotency_keys 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_publish_idem_root' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_publish_idem_root ON public.publish_idempotency_keys (root_request_id) WHERE root_request_id IS NOT NULL;
    END IF;
END $idxguard$;

-- 🔴 composite FK 的物理落点:retry 只能指向 record_kind='root' 的行,禁止指 retry/legacy、
--    禁止成链成环(规格 02 §3.7)。PG 要求被引用侧有匹配的唯一约束,故先建
--    (request_id, record_kind) 唯一,再让 retry 的 (root_request_id, 'root') 指向它。
-- @index-guard uq_publish_idem_request_kind ON publish_idempotency_keys unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_publish_idem_request_kind' AND i.indrelid = to_regclass('public.publish_idempotency_keys')) THEN
        NULL;  -- 已在 public.publish_idempotency_keys 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_publish_idem_request_kind' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_publish_idem_request_kind 已存在但不在 public.publish_idempotency_keys 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_publish_idem_request_kind' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_publish_idem_request_kind ON public.publish_idempotency_keys (request_id, record_kind);
    END IF;
END $idxguard$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_publish_idem_retry_root') THEN
        ALTER TABLE publish_idempotency_keys
            ADD COLUMN IF NOT EXISTS root_kind TEXT
                GENERATED ALWAYS AS (CASE WHEN root_request_id IS NULL THEN NULL ELSE 'root' END) STORED;
        ALTER TABLE publish_idempotency_keys ADD CONSTRAINT fk_publish_idem_retry_root
            FOREIGN KEY (root_request_id, root_kind)
            REFERENCES publish_idempotency_keys (request_id, record_kind)
            NOT VALID;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 8. mhz_short_video_drafts · command 归属 + 来源身份(规格 02 §3.7)
-- ---------------------------------------------------------------------------
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS command_request_id   TEXT;
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS source_kind          TEXT;
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS source_id            TEXT;
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS source_payload_hash  CHARACTER(64);
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS source_receipt_id    TEXT;
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS source_post_revision_id BIGINT;
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS prepared_artifact_id BIGINT;
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS manifest_hash        CHARACTER(64);
ALTER TABLE mhz_short_video_drafts ADD COLUMN IF NOT EXISTS provider_payload_snapshot JSONB;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_draft_source_kind') THEN
        ALTER TABLE mhz_short_video_drafts ADD CONSTRAINT ck_mhz_draft_source_kind
            CHECK (source_kind IS NULL OR source_kind IN ('managed_geo_post', 'legacy_manual_draft'));
    END IF;
END $$;
-- @index-guard idx_mhz_draft_command ON mhz_short_video_drafts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mhz_draft_command' AND i.indrelid = to_regclass('public.mhz_short_video_drafts')) THEN
        NULL;  -- 已在 public.mhz_short_video_drafts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mhz_draft_command' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mhz_draft_command 已存在但不在 public.mhz_short_video_drafts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mhz_draft_command' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mhz_draft_command ON public.mhz_short_video_drafts (command_request_id) WHERE command_request_id IS NOT NULL;
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 9. mhz_publish_orders · command 归属 + 服务端解析的付款方
-- ---------------------------------------------------------------------------
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS command_request_id TEXT;
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS payer_user_id      BIGINT;
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS actor_user_id      BIGINT;
ALTER TABLE mhz_publish_orders ADD COLUMN IF NOT EXISTS payer_policy_snapshot JSONB;
-- @index-guard idx_mhz_orders_command ON mhz_publish_orders plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mhz_orders_command' AND i.indrelid = to_regclass('public.mhz_publish_orders')) THEN
        NULL;  -- 已在 public.mhz_publish_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mhz_orders_command' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mhz_orders_command 已存在但不在 public.mhz_publish_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mhz_orders_command' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mhz_orders_command ON public.mhz_publish_orders (command_request_id) WHERE command_request_id IS NOT NULL;
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 10. mhz_publish_order_items · 逐项身份 / 价格指纹 / 资金权威 / 账号日容量
-- ---------------------------------------------------------------------------
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS command_request_id     TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS source_geo_post_id     BIGINT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS source_post_revision_id BIGINT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS prepared_artifact_id   BIGINT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS manifest_hash          CHARACTER(64);
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS item_request_id        UUID;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS attempt_root_id        TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS attempt_no             INTEGER;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS retry_of_item_id       INTEGER;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS retry_claim_state      TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS retry_claim_token      TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS retry_claim_version    INTEGER;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS status_version         INTEGER;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS terminal_at            TIMESTAMPTZ;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS last_authoritative_event_at TIMESTAMPTZ;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS price_snapshot         JSONB;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS publish_price_fingerprint TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS feature_code           TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS billing_mode           TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS settlement_authority   TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS settlement_status      TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS organization_charge_link_id BIGINT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS execution_id           TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS freeze_id              BIGINT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS freeze_table           TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS payer_user_id          BIGINT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS reserved_amount        INTEGER;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS physical_split_snapshot JSONB;
-- 🔴 [返工 2026-08-18 · Codex P1-6] direct freeze 的**完整句柄**里有 task_ref,
--    而这张表原来没有这一列 ⇒ commit/release 只能靠 item id 现拼一个 task_ref,
--    与 freeze 时用的那个不是同一个值 ⇒ 对账键对不上。
--    句柄"整组"的意思是六格一格不能少(规格 §3.7)。
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS task_ref TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS external_started_at    TIMESTAMPTZ;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS lease_owner            TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS lease_expires_at       TIMESTAMPTZ;
-- 账号日容量(规格 02 §3.7 末段:按冻结 business timezone 分日)
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS capacity_date          DATE;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS capacity_timezone      TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS capacity_valid_until   TIMESTAMPTZ;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS capacity_state         TEXT;
-- 发布后可用性(规格 02 §11.1:published 不被覆写,下架只追加 availability)
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS availability           TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS retracted_at           TIMESTAMPTZ;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS retraction_kind        TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS replaced_by_source     TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS replaced_by_source_id  TEXT;
ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS published_at_tz        TIMESTAMPTZ;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_billing_mode') THEN
        ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_billing_mode
            CHECK (billing_mode IS NULL OR billing_mode IN ('deduct_upfront', 'freeze_per_item'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_settlement_authority') THEN
        ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_settlement_authority
            CHECK (settlement_authority IS NULL OR settlement_authority IN (
                'organization_charge', 'direct_freeze', 'admin_exempt', 'legacy_refund'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_settlement_status') THEN
        ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_settlement_status
            CHECK (settlement_status IS NULL OR settlement_status IN (
                'exempt', 'frozen', 'settlement_pending', 'committed',
                'released', 'quarantined', 'manual'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_capacity_state') THEN
        ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_capacity_state
            CHECK (capacity_state IS NULL OR capacity_state IN ('reserved', 'consumed', 'released'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_availability') THEN
        ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_availability
            CHECK (availability IS NULL OR availability IN ('active', 'retracted', 'replaced'));
    END IF;
    -- 🔴 资金终态只能走一种(规格 02 §8.2):freeze_per_item 禁止再触发 legacy refund。
    --    形态锁在 schema 层,不靠调用方自觉。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_authority_shape') THEN
        ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_authority_shape
            CHECK (
                settlement_authority IS NULL
                OR (settlement_authority = 'organization_charge'
                    AND organization_charge_link_id IS NOT NULL
                    AND freeze_id IS NULL AND freeze_table IS NULL)
                OR (settlement_authority IN ('direct_freeze', 'admin_exempt')
                    AND organization_charge_link_id IS NULL
                    AND freeze_id IS NOT NULL AND freeze_table IS NOT NULL
                    AND payer_user_id IS NOT NULL AND reserved_amount IS NOT NULL)
                OR settlement_authority = 'legacy_refund'
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_freeze_mode_pairing') THEN
        ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_freeze_mode_pairing
            CHECK (
                billing_mode IS DISTINCT FROM 'freeze_per_item'
                OR settlement_authority IN ('organization_charge', 'direct_freeze', 'admin_exempt')
            );
    END IF;
    -- 🔴 [返工 2026-08-18 · Codex P1-6] freeze_per_item ⇒ 句柄必须**整组**齐备。
    --    只约束 settlement_authority 不够:一条 authority='direct_freeze' 却
    --    没有 task_ref / reserved_amount / physical_split_snapshot 的行,
    --    看起来"有记账"、实际 commit/release 都做不了 —— 半个句柄比没有句柄更危险。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mhz_item_freeze_handle_complete') THEN
        ALTER TABLE mhz_publish_order_items ADD CONSTRAINT ck_mhz_item_freeze_handle_complete
            CHECK (
                billing_mode IS DISTINCT FROM 'freeze_per_item'
                OR settlement_authority = 'organization_charge'
                OR (task_ref IS NOT NULL
                    AND reserved_amount IS NOT NULL
                    AND physical_split_snapshot IS NOT NULL)
            );
    END IF;
END $$;

-- 同一 attempt_root 的 attempt_no 唯一 + 同时最多一个 live attempt(规格 02 §3.7)
-- @index-guard uq_mhz_item_attempt ON mhz_publish_order_items unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_mhz_item_attempt' AND i.indrelid = to_regclass('public.mhz_publish_order_items')) THEN
        NULL;  -- 已在 public.mhz_publish_order_items 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_mhz_item_attempt' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_mhz_item_attempt 已存在但不在 public.mhz_publish_order_items 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_mhz_item_attempt' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_mhz_item_attempt ON public.mhz_publish_order_items (attempt_root_id, attempt_no) WHERE attempt_root_id IS NOT NULL AND attempt_no IS NOT NULL;
    END IF;
END $idxguard$;
-- 同一 active post revision 最多一个非终态/有效发布根(规格 02 §7.3 · 一篇一账号的物理保证)
--
-- 🔴 [返工 2026-08-18 · Codex P0-11] 谓词从 **AND** 改成 **OR**。
--    规格原话是「非终态**或**当前仍有效」,原来写成了「非终态**且**有效」:
--
--      · 一条 `published + availability='active'` 的项,一旦落了 `terminal_at`
--        (提交流程终结 ≠ 作品下线),就**脱离**唯一索引;
--      · 于是同一个 post revision 可以再建**第二个**有效发布根 ——
--        「一篇一账号」的物理保证在最常见的成功路径上失效。
--
--    AND 与 OR 在这里不是收紧/放宽的关系:AND 版索引覆盖的是「刚提交还没终结」
--    这段**窗口**,OR 版覆盖的是「还没终结 **或** 现在还在线」这个**集合**。
--    前者会在成功之后放手,后者不会。
--
--    ⚠️ **这是 schema 行为变更,不是 additive**:索引覆盖的行集变大了,
--    存量数据里若已存在同一 revision 的两条「已终结但仍 active」的项,
--    重放会 UniqueViolation。034 尚未上线(整包未发车),生产存量 = 0 行,
--    因此本次为零风险;判据见 `test_chain4_publish_identity_pg16.py` 的重放用例。
--    🔴 重放安全:prestart **每次部署无条件重放全部迁移**(本仓无迁移追踪表)。
--    所以不能裸 `DROP INDEX` + `CREATE` —— 那会让**每一次部署**都出现一段
--    「唯一约束不存在」的窗口,而那段窗口里的并发写不受约束。
--    这里先比对 `pg_get_indexdef`,只有定义**真的不同**时才重建;
--    第二遍重放什么都不做(判据 `test_034_replay_does_not_rebuild_live_root_index`
--    用 oid 是否变化来证明)。
DO $$
DECLARE
    current_def text;
BEGIN
    -- 🔴 必须绑 indrelid:不绑表时,这里读到的可能是**别的表上同名索引**的定义,
    --    而下面那条 DROP 会把那张无辜表的索引删掉(按名删,同样不绑表)。
    --    绑上之后:别的表上的同名索引读不到 ⇒ current_def IS NULL ⇒ 不 DROP,
    --    交给下方 @index-guard 去响亮报错。
    SELECT pg_get_indexdef(c.oid) INTO current_def
      FROM pg_class c
      JOIN pg_index ix ON ix.indexrelid = c.oid
     WHERE c.relname = 'uq_mhz_item_live_revision_root'
       AND ix.indrelid = to_regclass('public.mhz_publish_order_items');
    IF current_def IS NOT NULL AND current_def NOT LIKE '%OR (availability IS NULL)%' THEN
        -- @drop-index-guard uq_mhz_item_live_revision_root ON mhz_publish_order_items strict
        IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                    WHERE c.relname = 'uq_mhz_item_live_revision_root' AND i.indrelid = to_regclass('mhz_publish_order_items')) THEN
            DROP INDEX uq_mhz_item_live_revision_root;
        ELSIF EXISTS (SELECT 1 FROM pg_class c
                       WHERE c.relname = 'uq_mhz_item_live_revision_root' AND c.relnamespace = current_schema()::regnamespace) THEN
            RAISE EXCEPTION '[drop-index-guard] uq_mhz_item_live_revision_root 不在 mhz_publish_order_items 上(实际宿主:%)—— 拒绝删掉别的表的索引',
                (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
                   LEFT JOIN pg_index i ON i.indexrelid = c.oid
                   LEFT JOIN pg_class t ON t.oid = i.indrelid
                  WHERE c.relname = 'uq_mhz_item_live_revision_root' AND c.relnamespace = current_schema()::regnamespace)
                USING ERRCODE = 'wrong_object_type';
        ELSE
            RAISE EXCEPTION 'index "uq_mhz_item_live_revision_root" does not exist'
                USING ERRCODE = 'undefined_object';  -- 与老形态(不带 IF EXISTS)同口径
        END IF;
        current_def := NULL;
    END IF;
    IF current_def IS NULL THEN
        -- @index-guard uq_mhz_item_live_revision_root ON mhz_publish_order_items unique
        IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                    WHERE c.relname = 'uq_mhz_item_live_revision_root' AND i.indrelid = to_regclass('public.mhz_publish_order_items')) THEN
            NULL;  -- 已在 public.mhz_publish_order_items 上 → 幂等跳过
        ELSIF EXISTS (SELECT 1 FROM pg_class c
                       WHERE c.relname = 'uq_mhz_item_live_revision_root' AND c.relnamespace = 'public'::regnamespace) THEN
            RAISE EXCEPTION '[index-guard] uq_mhz_item_live_revision_root 已存在但不在 public.mhz_publish_order_items 上(实际宿主:%)—— 拒绝静默跳过',
                (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
                   LEFT JOIN pg_index i ON i.indexrelid = c.oid
                   LEFT JOIN pg_class t ON t.oid = i.indrelid
                  WHERE c.relname = 'uq_mhz_item_live_revision_root' AND c.relnamespace = 'public'::regnamespace)
                USING ERRCODE = 'duplicate_object';
        ELSE
            CREATE UNIQUE INDEX uq_mhz_item_live_revision_root ON public.mhz_publish_order_items (source_post_revision_id) WHERE source_post_revision_id IS NOT NULL AND (terminal_at IS NULL OR availability IS NULL OR availability = 'active');
        END IF;
    END IF;
END $$;
-- @index-guard uq_mhz_item_command_item ON mhz_publish_order_items unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_mhz_item_command_item' AND i.indrelid = to_regclass('public.mhz_publish_order_items')) THEN
        NULL;  -- 已在 public.mhz_publish_order_items 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_mhz_item_command_item' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_mhz_item_command_item 已存在但不在 public.mhz_publish_order_items 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_mhz_item_command_item' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_mhz_item_command_item ON public.mhz_publish_order_items (command_request_id, item_request_id) WHERE command_request_id IS NOT NULL AND item_request_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_mhz_item_capacity ON mhz_publish_order_items plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mhz_item_capacity' AND i.indrelid = to_regclass('public.mhz_publish_order_items')) THEN
        NULL;  -- 已在 public.mhz_publish_order_items 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mhz_item_capacity' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mhz_item_capacity 已存在但不在 public.mhz_publish_order_items 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mhz_item_capacity' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mhz_item_capacity ON public.mhz_publish_order_items (capacity_date, media_type, media_id) WHERE capacity_state IN ('reserved', 'consumed');
    END IF;
END $idxguard$;
-- @index-guard idx_mhz_item_command ON mhz_publish_order_items plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_mhz_item_command' AND i.indrelid = to_regclass('public.mhz_publish_order_items')) THEN
        NULL;  -- 已在 public.mhz_publish_order_items 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_mhz_item_command' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_mhz_item_command 已存在但不在 public.mhz_publish_order_items 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_mhz_item_command' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_mhz_item_command ON public.mhz_publish_order_items (command_request_id) WHERE command_request_id IS NOT NULL;
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 11. media_publications · 人工发布证据 canonical lineage(规格 02 §3.7 末)
-- ---------------------------------------------------------------------------
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS tenant_owner_user_id BIGINT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS source_geo_post_id   BIGINT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS source_post_revision_id BIGINT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS delivery_slot_key    UUID;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS normalized_url       TEXT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS normalized_url_hash  CHARACTER(64);
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS url_normalization_version TEXT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS published_at_tz      TIMESTAMPTZ;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS retracted_at         TIMESTAMPTZ;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS retraction_kind      TEXT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS replaced_by_source   TEXT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS replaced_by_source_id TEXT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS evidence_receipt_id  TEXT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS evidence_hash        CHARACTER(64);
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS body_proof           BOOLEAN;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS time_precision       TEXT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS request_id           TEXT;
ALTER TABLE media_publications ADD COLUMN IF NOT EXISTS request_hash         CHARACTER(64);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_pub_time_precision') THEN
        ALTER TABLE media_publications ADD CONSTRAINT ck_media_pub_time_precision
            CHECK (time_precision IS NULL OR time_precision IN ('exact', 'date', 'legacy_unknown'));
    END IF;
END $$;
-- 同 URL 不同 quote 必须按 lineage 隔离(规格 02 §3.7);只约束未撤稿的行
-- @index-guard uq_media_pub_active_url ON media_publications unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_media_pub_active_url' AND i.indrelid = to_regclass('public.media_publications')) THEN
        NULL;  -- 已在 public.media_publications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_media_pub_active_url' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_media_pub_active_url 已存在但不在 public.media_publications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_media_pub_active_url' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_media_pub_active_url ON public.media_publications (tenant_owner_user_id, quote_id, normalized_url_hash, url_normalization_version) WHERE retracted_at IS NULL AND normalized_url_hash IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard uq_media_pub_request ON media_publications unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_media_pub_request' AND i.indrelid = to_regclass('public.media_publications')) THEN
        NULL;  -- 已在 public.media_publications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_media_pub_request' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_media_pub_request 已存在但不在 public.media_publications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_media_pub_request' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_media_pub_request ON public.media_publications (request_id) WHERE request_id IS NOT NULL;
    END IF;
END $idxguard$;
