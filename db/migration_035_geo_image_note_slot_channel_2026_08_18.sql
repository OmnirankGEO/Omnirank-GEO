-- ============================================================================
-- 035 · GEO 图文交付槽位:渠道 / bucket / 履约态 + 事件种类扩充
--   窄 RFC `RFC_GEO_IMAGE_NOTE_SLOT_SIDECAR_ACTIVATION_2026-08-17.md`
--   Review-CTO 2026-08-17 裁定 R1/R2/R3 **全部批准**,附加约束 A-D 随批复生效。
--
-- 🔴🔴 **本迁移不是纯 additive**(附加约束 B 的口径):
--   §3 的 `ck_geo_article_slot_event_kind` 是**原子替换**,属 schema **行为变更** ——
--   往 CHECK 加允许值会放宽既有约束,本仓已明确「加允许值不算 additive」。
--   因此它必须:① 幂等可重放(prestart 无条件重放是本仓地雷);
--   ② 重放 ×2 后该 CHECK **定义唯一且为新版**;③ 有绕应用直接写的变异测试。
--   §1/§2 的三列与三个新 CHECK 仍是纯 additive。
--
-- 🔴 零 DML:全文件无 UPDATE / DELETE / INSERT。
--   `delivery_channel` **不给 DEFAULT** —— 历史行留 NULL,语义按下面的非对称口径:
--     · 文章侧读:`(delivery_channel IS NULL OR delivery_channel='article')`
--       (035 之前只有文章一种,NULL 即文章;这是**向后兼容**方向)
--     · 图文侧读/claim:`delivery_channel='douyin_image_note'`(显式相等,NULL 天然不命中)
--   给 DEFAULT 'article' 会**改写历史行语义**,而给 DEFAULT 'douyin_image_note'
--   会把文章槽位一夜之间变成图文槽位 —— 两个方向都错,所以不给。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. 三列 additive(RFC R2:channel 是 ordinal 冻结后的不可变属性)
-- ---------------------------------------------------------------------------
ALTER TABLE geo_article_delivery_slots
    ADD COLUMN IF NOT EXISTS delivery_channel  character varying(32);
ALTER TABLE geo_article_delivery_slots
    ADD COLUMN IF NOT EXISTS media_mix_bucket  character varying(40);
ALTER TABLE geo_article_delivery_slots
    ADD COLUMN IF NOT EXISTS fulfillment_state character varying(24);
-- 图文渠道的 active 交付对象(RFC R2 表格)。nullable —— 文章槽位永远为空。
ALTER TABLE geo_article_delivery_slots
    ADD COLUMN IF NOT EXISTS geo_post_id BIGINT;
-- 服务端签发的完整选题引用
ALTER TABLE geo_article_delivery_slots
    ADD COLUMN IF NOT EXISTS topic_ref TEXT;

-- ---------------------------------------------------------------------------
-- 2. 三个新 CHECK(纯 additive:新列上从无约束到有约束,不放宽任何既有约束)
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_article_slot_delivery_channel') THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT ck_geo_article_slot_delivery_channel
            CHECK (delivery_channel IS NULL OR delivery_channel IN ('article', 'douyin_image_note'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_article_slot_media_mix_bucket') THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT ck_geo_article_slot_media_mix_bucket
            CHECK (media_mix_bucket IS NULL OR media_mix_bucket IN (
                'focus_media_anchor', 'industry_platform_coverage', 'douyin_doubao_only'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_article_slot_fulfillment_state') THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT ck_geo_article_slot_fulfillment_state
            CHECK (fulfillment_state IS NULL OR fulfillment_state IN (
                'open', 'claimed', 'generating', 'ready'));
    END IF;
    -- 🔴 图文槽位才允许绑 geo_post_id。文章槽位绑图文成品 = 渠道串了。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_geo_article_slot_post_channel') THEN
        ALTER TABLE geo_article_delivery_slots ADD CONSTRAINT ck_geo_article_slot_post_channel
            CHECK (geo_post_id IS NULL OR delivery_channel = 'douyin_image_note');
    END IF;
END $$;

-- 图文渠道:一个 slot 最多绑一个 active 成品
-- @index-guard uq_geo_article_slot_geo_post ON geo_article_delivery_slots unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_article_slot_geo_post' AND i.indrelid = to_regclass('public.geo_article_delivery_slots')) THEN
        NULL;  -- 已在 public.geo_article_delivery_slots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_article_slot_geo_post' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_article_slot_geo_post 已存在但不在 public.geo_article_delivery_slots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_article_slot_geo_post' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_article_slot_geo_post ON public.geo_article_delivery_slots (geo_post_id) WHERE geo_post_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_article_slot_channel_state ON geo_article_delivery_slots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_article_slot_channel_state' AND i.indrelid = to_regclass('public.geo_article_delivery_slots')) THEN
        NULL;  -- 已在 public.geo_article_delivery_slots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_article_slot_channel_state' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_article_slot_channel_state 已存在但不在 public.geo_article_delivery_slots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_article_slot_channel_state' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_article_slot_channel_state ON public.geo_article_delivery_slots (quote_id, delivery_channel, fulfillment_state, contract_ordinal);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 3. 🔴 事件种类原子替换(**schema 行为变更** · 附加约束 B)
--
--   既有 9 种:created/blocked/unblocked/cancelled/superseded/reassigned/
--             topic_linked/article_linked/publication_locked
--   新增 5 种:slot_claimed/slot_released/generation_started/post_linked/ready
--
--   形态:**先按定义比对再决定是否 DROP**,而不是无条件 DROP+ADD。
--   无条件 DROP+ADD 在重放时会有一个"约束不存在"的窗口 —— prestart 无条件重放
--   时若恰好并发写入,那一瞬间的写是不受约束的。按定义比对后,
--   第二遍重放**什么都不做**(定义已相同),窗口只在真正需要变更时出现一次。
-- ---------------------------------------------------------------------------
-- 🔴 判"是不是已经是新版"用**语义**而不是全字符串比对。
--   第一版拼了一整条期望定义去比,当场被自己的重放判据顶红:
--   PG 的 `pg_get_constraintdef` 实际渲染成 `'x'::character varying::text`(**双 cast**)
--   并省掉外层 `::text[]`,与手写字符串永远不等 ⇒ **每次重放都 DROP 重建**,
--   正是本段注释声称要避免的那个"无约束窗口"。
--   全字符串比对还跨 PG 版本脆(渲染细节会变)。改判「五个新种类在不在定义里」——
--   这是它作为封闭集合真正要回答的问题,且对渲染差异免疫。
DO $$
DECLARE
    _cur text;
    _is_new boolean;
BEGIN
    SELECT pg_get_constraintdef(oid, true) INTO _cur
      FROM pg_constraint WHERE conname = 'ck_geo_article_slot_event_kind';

    _is_new := _cur IS NOT NULL
           AND _cur LIKE '%slot_claimed%'
           AND _cur LIKE '%slot_released%'
           AND _cur LIKE '%generation_started%'
           AND _cur LIKE '%post_linked%'
           AND _cur LIKE '%''ready''%';

    IF _cur IS NOT NULL AND NOT _is_new THEN
        ALTER TABLE geo_article_delivery_slot_events
            DROP CONSTRAINT ck_geo_article_slot_event_kind;
        _cur := NULL;
    END IF;

    IF _cur IS NULL THEN
        ALTER TABLE geo_article_delivery_slot_events
            ADD CONSTRAINT ck_geo_article_slot_event_kind
            CHECK (event_kind::text = ANY (ARRAY[
                'created'::character varying, 'blocked'::character varying,
                'unblocked'::character varying, 'cancelled'::character varying,
                'superseded'::character varying, 'reassigned'::character varying,
                'topic_linked'::character varying, 'article_linked'::character varying,
                'publication_locked'::character varying, 'slot_claimed'::character varying,
                'slot_released'::character varying, 'generation_started'::character varying,
                'post_linked'::character varying, 'ready'::character varying]::text[]));
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. quotes 的图文 enrollment 标记(RFC R1 第 4 前置)
--   复用既有 `article_plan_writing_mode` 列,**不新增列**:
--   `= 'image_note_contract'` 且 `article_plan_enrolled_at IS NOT NULL` 才算 enrolled。
--   零 DML ⇒ 存量 411 张 quote 全部 **未** enrolled(实测 enrolled = 0/411),
--   与 RFC「存量零自动 enroll」逐字一致。
-- ---------------------------------------------------------------------------
-- @index-guard idx_quotes_image_note_enrolled ON quotes plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_quotes_image_note_enrolled' AND i.indrelid = to_regclass('public.quotes')) THEN
        NULL;  -- 已在 public.quotes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_quotes_image_note_enrolled' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_quotes_image_note_enrolled 已存在但不在 public.quotes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_quotes_image_note_enrolled' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_quotes_image_note_enrolled ON public.quotes (article_plan_writing_mode, article_plan_enrolled_at) WHERE article_plan_writing_mode = 'image_note_contract';
    END IF;
END $idxguard$;
