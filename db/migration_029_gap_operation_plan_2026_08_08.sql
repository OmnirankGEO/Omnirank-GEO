-- 029 · P4 缺口作战计划 支撑迁移(WO_P4_GAP_PLAN_DEV_2026-08-08 · R6 + Owner 2026-08-08 裁定)
--
-- 本文件是 **两件事合并的一个 additive 包**,Owner 2026-08-08 拍板合并:
--   §1 media_outlets 加 4 列(R6 原「P0 支撑微包」)—— 运营开始录入媒体可进入性核实结果的前置
--   §2 缺口作战计划 4 张新表      —— P4 主包保持零迁移的前提(主包纯代码)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 命名边界(最重要的一条,写在最前面)
-- ══════════════════════════════════════════════════════════════════════════
-- 仓库里**已经有一台同名概念的机器**:`services/article_delivery_plan.py`(1190 行)
-- 及其四张表 geo_article_plan_outbox / geo_article_plan_runs / geo_article_delivery_slots
-- / geo_article_contract_revisions,配套 geo_article_target_question_snapshots。
--
-- 2026-08-08 生产只读实测(SSH · 只读事务 + 必失败 UPDATE 自证):
--     geo_article_delivery_slots      0 行
--     geo_article_plan_runs           0 行
--     geo_article_plan_outbox         0 行
--     geo_article_contract_revisions  0 行
--     omnirank-blue printenv | grep ARTICLE_  →  **空**(全部走 FEATURE_FLAG_DEFAULTS 的 False)
-- 即:那是一台**建好了但全黑**的事件溯源机器。
--
-- 本迁移建的表一律 `gap_` 前缀,**与它零交集**:
--   · 不写它任何一张表  · 不读它的不变式  · 不 import services/article_delivery_plan
-- 理由:那台机器是 outbox + 不可变 plan + append-only 投影的事件溯源设计,
-- 把 P4 作为**第二个写入方**塞进去,等于把它的不变式交给两个互不知情的生产者维护。
-- 若日后 Owner 决定两者合并,合并方向应是 P4 退让并入它,而不是现在就污染它。
--
-- 🔴 全部 additive:不改任何既有列的类型/可空性/默认值,不 DROP,不 UPDATE 任何既有行。
-- 🔴 全部 IF NOT EXISTS,幂等,可重复执行;不依赖任何前置迁移。
-- 🔴 漏跑的后果分两段,方向是**刻意选的**(见各段说明)。

-- ══════════════════════════════════════════════════════════════════════════
-- §1 media_outlets · 媒体可进入性核实字段(R6)
-- ══════════════════════════════════════════════════════════════════════════
-- 现状(2026-08-08 生产实测 27004 行 / 21 列):可进入性只有一个笼统的
-- `geo_confirmed INTEGER 0|1`,既表达不了「能自己发 / 要代发 / 进不去」,
-- 也没有「谁在什么时候核的」。P0 运营核实 Top100 要录入的就是这四列。
--
-- 🔴 漏跑的后果是**响亮的**(刻意选的):任务卡「怎么发」这一行会在读 entry_assessment
--    时抛 UndefinedColumn → 交付计划区块整块报人话错误。刻意选响亮:这四列是
--    P0 运营录入的落点,静默缺失 = 运营录了一整周发现没存进去,比当场报错糟得多。

ALTER TABLE public.media_outlets
    ADD COLUMN IF NOT EXISTS entry_assessment TEXT;
ALTER TABLE public.media_outlets
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE public.media_outlets
    ADD COLUMN IF NOT EXISTS verified_by INTEGER;
ALTER TABLE public.media_outlets
    ADD COLUMN IF NOT EXISTS entry_note TEXT;

-- 受控枚举。🔴 **不新增第四套域名分级**(R5):本列只回答「我方能不能进去发」这一个问题,
--    与「这个域名权威度几档」是正交的两个轴。权威度一律现读 domain_authority_cache.tier
--    的现役五档(services/domain_authority_ai.py:33-39 _VALID_TIERS),本表不复制它。
-- NULL = 尚未核实(27004 行存量全部落在这一档),不是「进不去」。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_media_outlets_entry_assessment'
           AND conrelid = 'public.media_outlets'::regclass
    ) THEN
        ALTER TABLE public.media_outlets
            ADD CONSTRAINT chk_media_outlets_entry_assessment
            CHECK (entry_assessment IS NULL OR entry_assessment IN (
                'self_service',   -- 运营可自行发布
                'mediated',       -- 需代发渠道安排
                'unreachable',    -- 已核实进不去
                'unknown'         -- 已看过但结论不确定(与 NULL「没人看过」区分)
            ));
    END IF;
END $$;

-- 运营核实台面按「未核实优先」排序,给部分索引
-- @index-guard idx_media_outlets_entry_assessment ON media_outlets plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_media_outlets_entry_assessment' AND i.indrelid = to_regclass('public.media_outlets')) THEN
        NULL;  -- 已在 public.media_outlets 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_media_outlets_entry_assessment' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_media_outlets_entry_assessment 已存在但不在 public.media_outlets 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_media_outlets_entry_assessment' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_media_outlets_entry_assessment ON public.media_outlets (entry_assessment);
    END IF;
END $idxguard$;
-- @index-guard idx_media_outlets_unverified ON media_outlets plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_media_outlets_unverified' AND i.indrelid = to_regclass('public.media_outlets')) THEN
        NULL;  -- 已在 public.media_outlets 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_media_outlets_unverified' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_media_outlets_unverified 已存在但不在 public.media_outlets 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_media_outlets_unverified' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_media_outlets_unverified ON public.media_outlets (ai_coverage_count DESC) WHERE entry_assessment IS NULL;
    END IF;
END $idxguard$;

COMMENT ON COLUMN public.media_outlets.entry_assessment IS
    '我方可进入性核实结论(P0 运营录入):self_service|mediated|unreachable|unknown;'
    'NULL=尚未核实。🔴 与域名权威度分级正交 —— 权威度现读 domain_authority_cache.tier 五档,'
    '本列不复制、不派生、不构成第四套分级。';
COMMENT ON COLUMN public.media_outlets.verified_at IS '本次可进入性核实时间(运营录入)';
COMMENT ON COLUMN public.media_outlets.verified_by IS '核实人 user_id(运营录入)';
COMMENT ON COLUMN public.media_outlets.entry_note IS '核实备注人话(如「需对接商务,报价按月」)。🔴 会展示给运营,禁写内部采购成本/供应商名';

-- ══════════════════════════════════════════════════════════════════════════
-- §2 缺口作战计划 · 四张新表
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 漏跑的后果是**响亮的**(刻意选的):交付计划区块加载时读 gap_plan_snapshots
--    抛 UndefinedTable → 端点返回错误合同(code=snapshot_unavailable + 人话 + 重新获取)。
--    刻意选响亮:这是一个**新增区块**,静默返空会让运营以为「这个客户没缺口」——
--    那是一句假结论,比一句「暂时取不到,重新获取」坏得多。
--    ⚠️ 但它只影响这个新区块:报价页其余部分、写作中心、小榜的既有能力全部不受影响
--    (P4 前端对该区块单独 fail-soft,不阻断主链)。

-- ─────────────────────────────────────────────────────────────────
-- 2.1 决策快照(A1 统一决策源)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gap_plan_snapshots (
    snapshot_id           TEXT        PRIMARY KEY,      -- dps_<sha256 前 32 位>,内容寻址
    quote_id              INTEGER     NOT NULL,
    brand_id              INTEGER,
    owner_user_id         INTEGER,                      -- 生成时的归属快照(仅留痕,鉴权仍现算)
    -- 版本三件套(合同 §8「规则版本、数据版本和生成时间」)
    snapshot_version      TEXT        NOT NULL,         -- 对外契约版本,如 gap-plan-v1
    rule_version          TEXT        NOT NULL,         -- 规则版本(编译器改了就变)
    data_version          CHAR(64)    NOT NULL,         -- 数据版本 = 源事实规范化后的 sha256
    authority_generation  INTEGER     NOT NULL,         -- 代际,同 quote 内单调递增
    -- 目标问题
    display_query         TEXT,
    observed_at           TIMESTAMPTZ,
    -- 容量(🔴 口径来源必须落库明示,禁止事后靠猜)
    capacity_authorized   INTEGER     NOT NULL DEFAULT 0,
    capacity_reserved     INTEGER     NOT NULL DEFAULT 0,
    capacity_available    INTEGER     NOT NULL DEFAULT 0,
    capacity_source       TEXT        NOT NULL,         -- 如 'quotes.total_articles'
    -- 载荷
    summary_jsonb         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    payload_jsonb         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    generated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 代际唯一:同一个报价不允许出现两个同代际快照
-- @index-guard uq_gap_plan_snapshot_quote_generation ON gap_plan_snapshots unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_gap_plan_snapshot_quote_generation' AND i.indrelid = to_regclass('public.gap_plan_snapshots')) THEN
        NULL;  -- 已在 public.gap_plan_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_gap_plan_snapshot_quote_generation' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_gap_plan_snapshot_quote_generation 已存在但不在 public.gap_plan_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_gap_plan_snapshot_quote_generation' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_gap_plan_snapshot_quote_generation ON public.gap_plan_snapshots (quote_id, authority_generation);
    END IF;
END $idxguard$;
-- @index-guard idx_gap_plan_snapshots_quote_time ON gap_plan_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_gap_plan_snapshots_quote_time' AND i.indrelid = to_regclass('public.gap_plan_snapshots')) THEN
        NULL;  -- 已在 public.gap_plan_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_gap_plan_snapshots_quote_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_gap_plan_snapshots_quote_time 已存在但不在 public.gap_plan_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_gap_plan_snapshots_quote_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_gap_plan_snapshots_quote_time ON public.gap_plan_snapshots (quote_id, generated_at DESC);
    END IF;
END $idxguard$;

COMMENT ON TABLE gap_plan_snapshots IS
    '缺口作战计划决策快照(P4 · WO_P4_GAP_PLAN_DEV_2026-08-08 §A1)。报价页 / 任务卡 / 小榜'
    '三个入口只消费这一份,禁止各自重算。🔴 与 geo_article_plan_runs 无关也不互通 —— '
    '那是另一台(当前全黑的)事件溯源机器,见本迁移文件头部命名边界说明。';
COMMENT ON COLUMN gap_plan_snapshots.capacity_source IS
    '容量口径来源明示。🔴 存在的理由:生产实测报价 372 的 quotes.total_articles=0,'
    '但 confirmed_keywords.required_articles=15、articles 表已有 136 篇 —— 三个数不一样。'
    '不落库写清用的是哪一个,半年后没人能复现「当时为什么判成容量 0」。';

-- ─────────────────────────────────────────────────────────────────
-- 2.2 逐篇计划项(A1 逐篇三元组)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gap_plan_items (
    id                    BIGSERIAL   PRIMARY KEY,
    snapshot_id           TEXT        NOT NULL REFERENCES gap_plan_snapshots(snapshot_id) ON DELETE CASCADE,
    plan_item_id          TEXT        NOT NULL,        -- 展示态稳定标识,如 Q372-P01
    quote_id              INTEGER     NOT NULL,
    ordinal               INTEGER     NOT NULL,
    -- 三元组:问题 · 内容形态 · 目标网站
    target_question       TEXT        NOT NULL,
    content_form          TEXT,                        -- 文体族(style_family 口径)
    target_platform       TEXT,                        -- 内部平台键;对外匿名化在服务层做
    target_domain         TEXT,
    -- 状态三轴(与 03-gap-operation-labels 字典的 statuses 键一一对应)
    gap_code              TEXT,                        -- attack_absence / entity_gap / ...
    access_code           TEXT,                        -- self_service_publishable / ...
    allocation_code       TEXT        NOT NULL,        -- ready_to_execute / ..._capacity_zero / ...
    duplicate_of_item_id  TEXT,                        -- 重复覆盖指向哪一篇
    -- 🔴 合流桥占位列(2026-08-08 加):计划项 ↔ 交付槽 的将来锚点。
    --    现在**恒 NULL**,本包一行都不写它,也没有任何读侧消费它。
    --    存在的唯一理由:容量口径已并轨到 services/article_capacity_contract,
    --    而那份合同裁定「容量占用单位 = topics(交付槽)」。等 P4 计划项真正
    --    落成交付槽的那一天(reserved_articles 从恒 0 变成真值),两边要能对上号 ——
    --    那时补的是数据,不是 schema,不必再动一次表。
    --    🔴 它现在是**死元数据**,这一点写在这里由 Review 明知:
    --       additive 的 NULL 列没有运行时代价,而事后加列要跟一次迁移窗口。
    --       若 Review 判定"死元数据一律不许进",删掉本列即可,P4 行为零变化。
    delivery_slot_id      UUID,
    -- 人话三行(为什么写 / 写什么 / 怎么发)
    rationale_jsonb       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- 冻结给写作中心的规格(B6 预填只读这一份)
    frozen_spec_jsonb     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 幂等补列(若更早版本的本迁移已建过表 —— 与 027 的做法一致)
ALTER TABLE public.gap_plan_items
    ADD COLUMN IF NOT EXISTS delivery_slot_id UUID;

-- @index-guard uq_gap_plan_item_snapshot ON gap_plan_items unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_gap_plan_item_snapshot' AND i.indrelid = to_regclass('public.gap_plan_items')) THEN
        NULL;  -- 已在 public.gap_plan_items 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_gap_plan_item_snapshot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_gap_plan_item_snapshot 已存在但不在 public.gap_plan_items 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_gap_plan_item_snapshot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_gap_plan_item_snapshot ON public.gap_plan_items (snapshot_id, plan_item_id);
    END IF;
END $idxguard$;
-- @index-guard idx_gap_plan_items_quote_item ON gap_plan_items plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_gap_plan_items_quote_item' AND i.indrelid = to_regclass('public.gap_plan_items')) THEN
        NULL;  -- 已在 public.gap_plan_items 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_gap_plan_items_quote_item' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_gap_plan_items_quote_item 已存在但不在 public.gap_plan_items 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_gap_plan_items_quote_item' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_gap_plan_items_quote_item ON public.gap_plan_items (quote_id, plan_item_id);
    END IF;
END $idxguard$;

COMMENT ON TABLE gap_plan_items IS
    '逐篇计划项。plan_item_id 是**跨代际稳定的展示标识**(深链 /writing?plan_item_id=Q372-P01 '
    '不因重算而失效),代际校验单独由 authority_generation 负责 —— 两件事分开,'
    '否则「链接不烂」与「代际能过期」二选一。';

-- ─────────────────────────────────────────────────────────────────
-- 2.3 发布物 + 四段证据(A3 填链接 · 证据链四格)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gap_plan_publications (
    id                        BIGSERIAL   PRIMARY KEY,
    quote_id                  INTEGER     NOT NULL,
    plan_item_id              TEXT        NOT NULL,
    publication_url           TEXT        NOT NULL,
    publication_url_normalized TEXT       NOT NULL,
    publication_domain        TEXT,
    published_at              TIMESTAMPTZ NOT NULL,
    -- 四段证据。🔴 只有 published 可由人工填链接点亮;其余三段**只能**由 P5 回查数据写。
    --    这条不变式在服务层由白名单强制,DB 这里给 CHECK 兜底(不允许跳级)。
    evidence_published        BOOLEAN     NOT NULL DEFAULT TRUE,
    evidence_indexed          BOOLEAN     NOT NULL DEFAULT FALSE,
    evidence_cited            BOOLEAN     NOT NULL DEFAULT FALSE,
    evidence_recommended      BOOLEAN     NOT NULL DEFAULT FALSE,
    evidence_detail_jsonb     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    last_observed_at          TIMESTAMPTZ,
    submitted_by              INTEGER,
    idempotency_key           TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 一篇计划项一条发布记录(重复提交同一 URL 走幂等更新,不产生第二行)
-- @index-guard uq_gap_plan_publication_item ON gap_plan_publications unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_gap_plan_publication_item' AND i.indrelid = to_regclass('public.gap_plan_publications')) THEN
        NULL;  -- 已在 public.gap_plan_publications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_gap_plan_publication_item' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_gap_plan_publication_item 已存在但不在 public.gap_plan_publications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_gap_plan_publication_item' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_gap_plan_publication_item ON public.gap_plan_publications (quote_id, plan_item_id);
    END IF;
END $idxguard$;
-- 幂等键唯一(A3 idempotency_key)
-- @index-guard uq_gap_plan_publication_idem ON gap_plan_publications unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_gap_plan_publication_idem' AND i.indrelid = to_regclass('public.gap_plan_publications')) THEN
        NULL;  -- 已在 public.gap_plan_publications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_gap_plan_publication_idem' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_gap_plan_publication_idem 已存在但不在 public.gap_plan_publications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_gap_plan_publication_idem' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_gap_plan_publication_idem ON public.gap_plan_publications (idempotency_key) WHERE idempotency_key IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_gap_plan_publications_domain ON gap_plan_publications plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_gap_plan_publications_domain' AND i.indrelid = to_regclass('public.gap_plan_publications')) THEN
        NULL;  -- 已在 public.gap_plan_publications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_gap_plan_publications_domain' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_gap_plan_publications_domain 已存在但不在 public.gap_plan_publications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_gap_plan_publications_domain' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_gap_plan_publications_domain ON public.gap_plan_publications (publication_domain);
    END IF;
END $idxguard$;

-- 证据链不允许跳级:被引用必先收录,进入推荐必先被引用
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_gap_plan_evidence_monotonic'
           AND conrelid = 'public.gap_plan_publications'::regclass
    ) THEN
        ALTER TABLE public.gap_plan_publications
            ADD CONSTRAINT chk_gap_plan_evidence_monotonic
            CHECK (
                (NOT evidence_indexed     OR evidence_published)
            AND (NOT evidence_cited       OR evidence_indexed)
            AND (NOT evidence_recommended OR evidence_cited)
            );
    END IF;
END $$;

COMMENT ON TABLE gap_plan_publications IS
    '发布物与证据链四格(已发布→已收录→被 AI 引用→客户进入推荐)。'
    '🔴 P5 未上线前只有 evidence_published 会为真,其余三格恒 false —— '
    '前端必须显示成「回查将在发布后 7 天起自动进行」,不是故障态(WO R9)。';

-- ─────────────────────────────────────────────────────────────────
-- 2.4 回查队列(A3 · 7/14/30)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gap_plan_checkbacks (
    id                BIGSERIAL   PRIMARY KEY,
    publication_id    BIGINT      NOT NULL REFERENCES gap_plan_publications(id) ON DELETE CASCADE,
    quote_id          INTEGER     NOT NULL,
    plan_item_id      TEXT        NOT NULL,
    due_day           SMALLINT    NOT NULL,          -- 7 / 14 / 30
    due_at            TIMESTAMPTZ NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'pending',  -- pending|done|skipped
    result_jsonb      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    executed_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- @index-guard uq_gap_plan_checkback_slot ON gap_plan_checkbacks unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_gap_plan_checkback_slot' AND i.indrelid = to_regclass('public.gap_plan_checkbacks')) THEN
        NULL;  -- 已在 public.gap_plan_checkbacks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_gap_plan_checkback_slot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_gap_plan_checkback_slot 已存在但不在 public.gap_plan_checkbacks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_gap_plan_checkback_slot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_gap_plan_checkback_slot ON public.gap_plan_checkbacks (publication_id, due_day);
    END IF;
END $idxguard$;
-- @index-guard idx_gap_plan_checkbacks_due ON gap_plan_checkbacks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_gap_plan_checkbacks_due' AND i.indrelid = to_regclass('public.gap_plan_checkbacks')) THEN
        NULL;  -- 已在 public.gap_plan_checkbacks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_gap_plan_checkbacks_due' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_gap_plan_checkbacks_due 已存在但不在 public.gap_plan_checkbacks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_gap_plan_checkbacks_due' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_gap_plan_checkbacks_due ON public.gap_plan_checkbacks (due_at) WHERE status = 'pending';
    END IF;
END $idxguard$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_gap_plan_checkback_shape'
           AND conrelid = 'public.gap_plan_checkbacks'::regclass
    ) THEN
        ALTER TABLE public.gap_plan_checkbacks
            ADD CONSTRAINT chk_gap_plan_checkback_shape
            CHECK (due_day IN (7, 14, 30) AND status IN ('pending', 'done', 'skipped'));
    END IF;
END $$;

COMMENT ON TABLE gap_plan_checkbacks IS
    '7/14/30 天回查队列。🔴 P4 **只登记不执行** —— 真正的调度器归 P5。'
    '本表在 P5 上线前会持续积累 pending 行,这是预期状态,不是积压告警。';

-- ─────────────────────────────────────────────────────────────────
-- 2.5 小榜留痕(C3 四件套之「留痕」· 合同 §9.1)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gap_assistant_audit (
    id                    BIGSERIAL   PRIMARY KEY,
    assistant_request_id  TEXT        NOT NULL,
    snapshot_id           TEXT,
    snapshot_version      TEXT,
    authority_generation  INTEGER,
    quote_id              INTEGER,
    brand_id              INTEGER,
    actor_user_id         INTEGER,
    facts_hash            CHAR(64),                  -- 事实摘要哈希
    action_ids            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    output_hash           CHAR(64),
    operation_map_version TEXT,
    degraded              BOOLEAN     NOT NULL DEFAULT FALSE,
    degrade_reason        TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 幂等边界 = assistant_request_id + snapshot_version(合同 §9.1 原文)
-- @index-guard uq_gap_assistant_audit_idem ON gap_assistant_audit unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_gap_assistant_audit_idem' AND i.indrelid = to_regclass('public.gap_assistant_audit')) THEN
        NULL;  -- 已在 public.gap_assistant_audit 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_gap_assistant_audit_idem' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_gap_assistant_audit_idem 已存在但不在 public.gap_assistant_audit 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_gap_assistant_audit_idem' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_gap_assistant_audit_idem ON public.gap_assistant_audit (assistant_request_id, COALESCE(snapshot_version, ''));
    END IF;
END $idxguard$;
-- @index-guard idx_gap_assistant_audit_quote ON gap_assistant_audit plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_gap_assistant_audit_quote' AND i.indrelid = to_regclass('public.gap_assistant_audit')) THEN
        NULL;  -- 已在 public.gap_assistant_audit 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_gap_assistant_audit_quote' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_gap_assistant_audit_quote 已存在但不在 public.gap_assistant_audit 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_gap_assistant_audit_quote' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_gap_assistant_audit_quote ON public.gap_assistant_audit (quote_id, created_at DESC);
    END IF;
END $idxguard$;

COMMENT ON TABLE gap_assistant_audit IS
    '小榜运营建议留痕。🔴 禁止写入:密钥、完整提示词、跨租户正文、模型/供应商名。'
    'facts_hash / output_hash 是哈希不是正文 —— 事后要复现只能靠同代际快照重放。';

-- ══════════════════════════════════════════════════════════════════════════
-- 验收(部署后手工跑,确认迁移真的跑了 —— 全绿才算 migrated)
--   SELECT count(*) FROM information_schema.columns
--    WHERE table_name='media_outlets'
--      AND column_name IN ('entry_assessment','verified_at','verified_by','entry_note');  -- 期望 4
--   SELECT to_regclass('public.gap_plan_snapshots');     -- 非空
--   SELECT to_regclass('public.gap_plan_items');         -- 非空
--   SELECT to_regclass('public.gap_plan_publications');  -- 非空
--   SELECT to_regclass('public.gap_plan_checkbacks');    -- 非空
--   SELECT to_regclass('public.gap_assistant_audit');    -- 非空
-- 反向对照(证明上面那条 count 不是恒真):
--   SELECT count(*) FROM information_schema.columns
--    WHERE table_name='media_outlets' AND column_name='entry_assessment_ghost';           -- 期望 0
-- ══════════════════════════════════════════════════════════════════════════
