-- ============================================================================
-- 双供应商同媒体择优路由(软文 + 自媒体)· 工单 WORKORDER_MEDIA_PROVIDER_ROUTING_2026-08-02
-- 基线:生产尖 328cad0f(2026-08-02 现取,四容器双信源一致)
--
-- 全部 additive + 幂等,可反复连跑。不动任何存量列语义、不动 is_active。
--
-- 🔴 三条设计约束(改本文件前先读懂):
--   1. `mhz_publish_order_items.media_id` 语义**不变** = "用户买的那个媒体"
--      —— 它是计费(_recompute_publish_charge)/去重(check_duplicate_submission)/
--      展示/退款(item:<id>)的共同锚点。改它会一次性波及四条链。
--      实际投递去向另存 routed_provider / routed_media_id(可空,存量行 NULL = 未改道)。
--   2. `hidden_by_dedupe` 与 `is_active` **分开**:is_active 是上游同步字段,
--      动它会被下一次同步覆盖(工单 §3.3)。
--   3. λ(routing_saving_share_to_user)本期恒 0 = 售价零变化。落配置只为留口子。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. 等价映射表:同一媒体在两家的对应关系 + 择优结论
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_provider_equivalence (
    id                     SERIAL PRIMARY KEY,
    -- 'mhz_media'(软文) / 'mhz_wemedia'(自媒体)。短视频不在范围:快易播不做短视频。
    table_name             VARCHAR(32)  NOT NULL,

    -- 两侧【我方主键】。kyb 侧带 1 亿偏移(config.ID_OFFSET)。
    mhz_media_id           INTEGER      NOT NULL,
    kyb_media_id           INTEGER      NOT NULL,

    -- 两侧【上游真实 id】。🔴 下单一律用这个,不是我方主键(工单红线 3)。
    -- mhz 侧无偏移,provider_media_id == mhz_media_id;冗余存一份是为了让 A4 判据
    -- 能直接对着表核,不必在验收时反推偏移。
    mhz_provider_media_id  INTEGER      NOT NULL,
    kyb_provider_media_id  INTEGER      NOT NULL,

    -- 建表时的成本快照(元)。价格会随两家调价漂,所以映射表必须能重算刷新,
    -- 快照只用于审计"当时按什么价做的决定",**不参与下单**。
    mhz_price              NUMERIC(12,2),
    kyb_price              NUMERIC(12,2),
    saving_yuan            NUMERIC(12,2),   -- mhz_price - kyb_price,可为负(盒子更便宜)

    -- 取便宜的那家。🔴 A2 反向对照就是查这一列:341 条"盒子更便宜"必须是 'mhz'。
    preferred_provider     VARCHAR(8)   NOT NULL,

    -- auto_low_diff  : 价差 ≤ ¥200,直接自动
    -- auto_fans_ok   : 价差 > ¥200 且粉丝佐证通过
    -- human_approved : 高价且佐证不通过,人工核过才进表
    confidence             VARCHAR(24)  NOT NULL,

    -- 匹配依据留痕(名字 + 归一化域名),便于事后复盘"为什么把这两条判成同一个"
    matched_name           TEXT,
    matched_domain         TEXT,

    approved_by            INTEGER,
    approved_at            TIMESTAMP,

    is_enabled             BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMP    NOT NULL DEFAULT NOW(),

    CONSTRAINT media_provider_equivalence_table_ck
        CHECK (table_name IN ('mhz_media', 'mhz_wemedia')),
    CONSTRAINT media_provider_equivalence_pref_ck
        CHECK (preferred_provider IN ('mhz', 'kyb')),
    CONSTRAINT media_provider_equivalence_conf_ck
        CHECK (confidence IN ('auto_low_diff', 'auto_fans_ok', 'human_approved'))
);

-- 一条 mhz 媒体在一张表里只能有一条生效映射(生成器 DISTINCT ON 取最低价那条)。
-- @index-guard uq_media_provider_equivalence_mhz ON media_provider_equivalence unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_media_provider_equivalence_mhz' AND i.indrelid = to_regclass('public.media_provider_equivalence')) THEN
        NULL;  -- 已在 public.media_provider_equivalence 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_media_provider_equivalence_mhz' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_media_provider_equivalence_mhz 已存在但不在 public.media_provider_equivalence 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_media_provider_equivalence_mhz' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_media_provider_equivalence_mhz ON public.media_provider_equivalence (table_name, mhz_media_id);
    END IF;
END $idxguard$;

-- 路由热路径:按(表, mhz 主键)查。上面的唯一索引已覆盖,不再另建。
-- 反查(某条 kyb 被哪些 mhz 指向)在目录去重时要用。
-- @index-guard ix_media_provider_equivalence_kyb ON media_provider_equivalence plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ix_media_provider_equivalence_kyb' AND i.indrelid = to_regclass('public.media_provider_equivalence')) THEN
        NULL;  -- 已在 public.media_provider_equivalence 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ix_media_provider_equivalence_kyb' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ix_media_provider_equivalence_kyb 已存在但不在 public.media_provider_equivalence 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ix_media_provider_equivalence_kyb' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX ix_media_provider_equivalence_kyb ON public.media_provider_equivalence (table_name, kyb_media_id);
    END IF;
END $idxguard$;

COMMENT ON TABLE media_provider_equivalence IS
    '同媒体双供应商等价映射 + 择优结论。映射落 DB 不硬编码 —— 两家会调价,必须能重算刷新。';
COMMENT ON COLUMN media_provider_equivalence.kyb_provider_media_id IS
    '快易播上游真实 media id。下单用它,不能用我方主键(主键有 1 亿偏移)。';

-- ---------------------------------------------------------------------------
-- 2. 目录去重列。**不动 is_active**(上游同步字段,动了会被覆盖)。
-- ---------------------------------------------------------------------------
ALTER TABLE mhz_media    ADD COLUMN IF NOT EXISTS hidden_by_dedupe BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE mhz_wemedia  ADD COLUMN IF NOT EXISTS hidden_by_dedupe BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN mhz_media.hidden_by_dedupe IS
    '同名同域两家都在架时隐藏重复条目(Owner 选 B:留 mhz 那条,售价不变,成本走 kyb)。'
    '与 is_active 分开:is_active 由上游同步覆盖。';

-- 目录列表的热路径过滤(is_active + 未被去重隐藏)
-- @index-guard ix_mhz_media_visible ON mhz_media plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ix_mhz_media_visible' AND i.indrelid = to_regclass('public.mhz_media')) THEN
        NULL;  -- 已在 public.mhz_media 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ix_mhz_media_visible' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ix_mhz_media_visible 已存在但不在 public.mhz_media 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ix_mhz_media_visible' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX ix_mhz_media_visible ON public.mhz_media (is_active, hidden_by_dedupe);
    END IF;
END $idxguard$;
-- @index-guard ix_mhz_wemedia_visible ON mhz_wemedia plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ix_mhz_wemedia_visible' AND i.indrelid = to_regclass('public.mhz_wemedia')) THEN
        NULL;  -- 已在 public.mhz_wemedia 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ix_mhz_wemedia_visible' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ix_mhz_wemedia_visible 已存在但不在 public.mhz_wemedia 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ix_mhz_wemedia_visible' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX ix_mhz_wemedia_visible ON public.mhz_wemedia (is_active, hidden_by_dedupe);
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 3. 订单项:记录"实际投递去向"
--
-- 🔴 为什么必须加这两列(本单最容易漏的地雷):
--    services/kuaiyibo/status_sync.py::_open_items() 靠
--    `is_kuaiyibo_local_id(media_id)` 判这条 item 走哪家。若改道后仍只存原 media_id,
--    则改道去 kyb 的单**永远不会被 kyb 状态回流扫到** → 卡在 submitted →
--    24 小时后被 refund_stale_orders 判"未发布成功"退款,而稿其实已发出去、
--    kyb 那边的钱我们也付了。既亏钱又亏交付。
--
--    存量行恒 NULL = 未改道,所有既有查询逐位不变。
-- ---------------------------------------------------------------------------
ALTER TABLE mhz_publish_order_items
    ADD COLUMN IF NOT EXISTS routed_provider  VARCHAR(8);
ALTER TABLE mhz_publish_order_items
    ADD COLUMN IF NOT EXISTS routed_media_id  INTEGER;
-- 实际外采成本(元)。与 cost_yuan 分开:cost_yuan 是"用户买的那条媒体的价"
-- (毛利分母 + 售价锚),routed_cost_yuan 是"我们真付给供应商的钱"。
-- 改道后两者不再相等,自记账(provider_spend_ledger)必须用后者,否则
-- 「靠自记账判断该不该给快易播充值」会被高估的成本带偏。
ALTER TABLE mhz_publish_order_items
    ADD COLUMN IF NOT EXISTS routed_cost_yuan NUMERIC(12,2);

COMMENT ON COLUMN mhz_publish_order_items.routed_media_id IS
    '实际投递去向的我方主键(改道时才填,NULL=未改道)。'
    '渠道识别请用 COALESCE(routed_media_id, media_id),不要只看 media_id。';

-- @index-guard ix_mhz_items_routed ON mhz_publish_order_items plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ix_mhz_items_routed' AND i.indrelid = to_regclass('public.mhz_publish_order_items')) THEN
        NULL;  -- 已在 public.mhz_publish_order_items 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ix_mhz_items_routed' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ix_mhz_items_routed 已存在但不在 public.mhz_publish_order_items 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ix_mhz_items_routed' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX ix_mhz_items_routed ON public.mhz_publish_order_items (routed_media_id) WHERE routed_media_id IS NOT NULL;
    END IF;
END $idxguard$;

-- ---------------------------------------------------------------------------
-- 4. λ 让利系数配置位。本期恒 '0' = 售价与现状逐字相同。
--    只落配置、不实现价格变化逻辑(工单 §3.3.1)。
--    🔴 将来启用 λ>0 必须配套做 7 天价格锁,否则售价随两家成本差实时漂,
--       违反仓内铁律「价格稳定感 ≥ 价格精确度」。
-- ---------------------------------------------------------------------------
INSERT INTO mhz_config (key, value)
VALUES ('routing_saving_share_to_user', '0')
ON CONFLICT (key) DO NOTHING;

-- 路由总开关。A8 回滚判据:关掉后路由与目录行为逐位回到现状。
INSERT INTO mhz_config (key, value)
VALUES ('media_provider_routing_enabled', '0')
ON CONFLICT (key) DO NOTHING;
