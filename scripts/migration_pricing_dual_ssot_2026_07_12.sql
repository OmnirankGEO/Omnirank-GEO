-- =====================================================================================
-- Migration: 双价目表定价重构 SSOT (PRICING_DUAL_SSOT_REBUILD_SPEC_2026-07-12 v1.3)
-- Date   : 2026-07-12
-- Nature : ADDITIVE ONLY. Zero destructive change. Fully idempotent (re-runnable).
--          Runs inside server.py::_run_sql_migrations() as ONE autocommit exec →
--          server 会剥离外层 BEGIN;/COMMIT;,整文件作为单条 simple-query 原子执行。
--          禁止 CREATE INDEX CONCURRENTLY(不能在事务内)。全部 IF NOT EXISTS。
--
-- 4 维核验基线(部署前对 prod 逐条核对):
--   ① 列名   : 见下每张表注释
--   ② data_type : 现金一律 INTEGER 分;系数一律 INTEGER bps;时间 TIMESTAMPTZ(UTC)
--   ③ 字段归属 : 新表独立,不改 recharge_orders/point_transactions 语义,仅 ADD COLUMN
--   ④ dry-run : throwaway PG 已真跑(见交付清单)
--
-- 红线遵守:不建税率/税点/开票字段;不碰 billing.py / connection.py / auth 中间件;
--          历史订单不回填伪造价格版本(全部新列可空,历史行 NULL 兼容)。
-- =====================================================================================

-- =====================================================================================
-- 1. pricing_catalog_versions —— 两个现金价目表 + 功能消耗目录 的版本头
--    (§10.2.1 · 草稿/已发布/归档 + 生效区间 + 审批审计)
-- =====================================================================================
CREATE TABLE IF NOT EXISTS pricing_catalog_versions (
    id              BIGSERIAL PRIMARY KEY,
    catalog_type    TEXT NOT NULL CHECK (catalog_type IN ('procurement','retail','feature_consumption')),
    scope_key       TEXT NOT NULL,                 -- procurement: channel_account_code 或 'PLATFORM_BASE';
                                                   -- retail: service_account_code;feature_consumption: 'GLOBAL'
    version_code    TEXT NOT NULL,                 -- 人话版本号 e.g. 'proc-2026-07-12-001'
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
    effective_from  TIMESTAMPTZ,                   -- 发布时写 · UTC · [from, to)
    effective_to    TIMESTAMPTZ,                   -- NULL = 当前开放版本
    reason          TEXT,
    created_by      INTEGER,
    approved_by     INTEGER,
    approved_at     TIMESTAMPTZ,
    published_at    TIMESTAMPTZ,
    archived_at     TIMESTAMPTZ,
    calc_meta_jsonb JSONB,                          -- 发布时的换算/系数元信息(points_per_yuan 等 · 审计)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- @index-guard ux_catalog_version_code ON pricing_catalog_versions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_catalog_version_code' AND i.indrelid = to_regclass('pricing_catalog_versions')) THEN
        NULL;  -- 已在 pricing_catalog_versions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_catalog_version_code' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_versions'))) THEN
        RAISE EXCEPTION '[index-guard] ux_catalog_version_code 已存在但不在 pricing_catalog_versions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_catalog_version_code' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_versions')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_catalog_version_code ON pricing_catalog_versions (catalog_type, scope_key, version_code);
    END IF;
END $idxguard$;
-- §9.4.6 任一时刻同一(type,scope)只能有一个开放 published 版本(DB 级排他保证)
-- @index-guard ux_catalog_published_open ON pricing_catalog_versions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_catalog_published_open' AND i.indrelid = to_regclass('pricing_catalog_versions')) THEN
        NULL;  -- 已在 pricing_catalog_versions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_catalog_published_open' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_versions'))) THEN
        RAISE EXCEPTION '[index-guard] ux_catalog_published_open 已存在但不在 pricing_catalog_versions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_catalog_published_open' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_versions')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_catalog_published_open ON pricing_catalog_versions (catalog_type, scope_key) WHERE status = 'published' AND effective_to IS NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_catalog_versions_lookup ON pricing_catalog_versions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_catalog_versions_lookup' AND i.indrelid = to_regclass('pricing_catalog_versions')) THEN
        NULL;  -- 已在 pricing_catalog_versions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_catalog_versions_lookup' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_versions'))) THEN
        RAISE EXCEPTION '[index-guard] idx_catalog_versions_lookup 已存在但不在 pricing_catalog_versions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_catalog_versions_lookup' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_versions')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_catalog_versions_lookup ON pricing_catalog_versions (catalog_type, scope_key, status);
    END IF;
END $idxguard$;

-- =====================================================================================
-- 2. pricing_catalog_entries —— 现金目录明细(procurement/retail 统一存储 · §10.2.2)
--    它是两个逻辑 SSOT 的统一实现,不是第三套价目表;整数分/整数 bps;发布后不可改。
-- =====================================================================================
CREATE TABLE IF NOT EXISTS pricing_catalog_entries (
    id                    BIGSERIAL PRIMARY KEY,
    version_id            BIGINT NOT NULL REFERENCES pricing_catalog_versions(id) ON DELETE CASCADE,
    product_code          TEXT NOT NULL,            -- = sku_templates.template_code(共享商品规格)
    base_price_cents      INTEGER NOT NULL,         -- procurement: 平台基准×系数前基准;retail: 有效成本基准
    multiplier_bps        INTEGER NOT NULL DEFAULT 10000,  -- 系数 bps(10000=1.0)· procurement 合同系数 / retail 客户加价系数
    final_price_cents     INTEGER NOT NULL,         -- 已发布最终现金价(整数分 · 全链只取整一次的结果)
    paid_points           BIGINT NOT NULL,          -- 到账可售/可用算力
    bonus_points          BIGINT NOT NULL DEFAULT 0,-- 促销赠送(procurement 促销 · 不计成本 · 不可转售)
    cost_floor_cents      INTEGER,                  -- retail: 有效成本底线(低于此拒绝 · §5.2)
    usage_example_version TEXT,                     -- retail: usage_examples 依据的功能消耗版本
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(version_id, product_code),
    CHECK (final_price_cents >= 0),
    CHECK (paid_points >= 0),
    CHECK (bonus_points >= 0)
);
-- @index-guard idx_catalog_entries_version ON pricing_catalog_entries plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_catalog_entries_version' AND i.indrelid = to_regclass('pricing_catalog_entries')) THEN
        NULL;  -- 已在 pricing_catalog_entries 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_catalog_entries_version' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_entries'))) THEN
        RAISE EXCEPTION '[index-guard] idx_catalog_entries_version 已存在但不在 pricing_catalog_entries 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_catalog_entries_version' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_entries')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_catalog_entries_version ON pricing_catalog_entries (version_id);
    END IF;
END $idxguard$;
-- @index-guard idx_catalog_entries_product ON pricing_catalog_entries plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_catalog_entries_product' AND i.indrelid = to_regclass('pricing_catalog_entries')) THEN
        NULL;  -- 已在 pricing_catalog_entries 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_catalog_entries_product' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_entries'))) THEN
        RAISE EXCEPTION '[index-guard] idx_catalog_entries_product 已存在但不在 pricing_catalog_entries 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_catalog_entries_product' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_entries')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_catalog_entries_product ON pricing_catalog_entries (product_code);
    END IF;
END $idxguard$;

-- =====================================================================================
-- 3. price_quotes —— 一次短期有效的后端报价(§9.2 · 不可枚举随机 ID + 计算哈希)
--    绑定买方/卖方/币种/用途;一报价只能开一单;支付重试复用订单不重复消费报价。
-- =====================================================================================
CREATE TABLE IF NOT EXISTS price_quotes (
    quote_id                     TEXT PRIMARY KEY,   -- 随机不可枚举(secrets 生成 · 非自增)
    quote_type                   TEXT NOT NULL CHECK (quote_type IN ('procurement','retail')),
    currency                     TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    catalog_version              TEXT NOT NULL,
    catalog_version_id           BIGINT,
    channel_relationship_version TEXT,               -- procurement/渠道归属订单必填
    seller_policy_version        TEXT,
    product_code                 TEXT NOT NULL,
    quantity                     INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
    seller_legal_entity_id       TEXT NOT NULL DEFAULT 'PLATFORM',  -- 在线订单固定平台主体
    buyer_user_id                INTEGER NOT NULL,
    channel_account_code         TEXT,
    channel_beneficiary_user_id  INTEGER,            -- 仅后端/财务可见 · 客户接口不返
    payment_collector            TEXT NOT NULL DEFAULT 'PLATFORM',
    points_granted               BIGINT NOT NULL CHECK (points_granted >= 0),
    bonus_points                 BIGINT NOT NULL DEFAULT 0 CHECK (bonus_points >= 0),
    base_price_cents             INTEGER NOT NULL,
    effective_multiplier_bps     INTEGER,
    final_price_cents            INTEGER NOT NULL CHECK (final_price_cents >= 0),
    upstream_cost_basis_cents    INTEGER,            -- [残留①] 进货报价钉死直属上游有效成本 · 渠道收益=final−此值 · retail 为 NULL
    expires_at                   TIMESTAMPTZ NOT NULL,
    calculation_hash             TEXT NOT NULL,
    status                       TEXT NOT NULL DEFAULT 'issued'
                                   CHECK (status IN ('issued','consumed','expired','cancelled')),
    used_order_id                TEXT,
    idempotency_key              TEXT,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at                  TIMESTAMPTZ
);
-- @index-guard idx_price_quotes_buyer ON price_quotes plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_price_quotes_buyer' AND i.indrelid = to_regclass('price_quotes')) THEN
        NULL;  -- 已在 price_quotes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_price_quotes_buyer' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('price_quotes'))) THEN
        RAISE EXCEPTION '[index-guard] idx_price_quotes_buyer 已存在但不在 price_quotes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_price_quotes_buyer' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('price_quotes')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_price_quotes_buyer ON price_quotes (buyer_user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_price_quotes_status ON price_quotes plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_price_quotes_status' AND i.indrelid = to_regclass('price_quotes')) THEN
        NULL;  -- 已在 price_quotes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_price_quotes_status' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('price_quotes'))) THEN
        RAISE EXCEPTION '[index-guard] idx_price_quotes_status 已存在但不在 price_quotes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_price_quotes_status' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('price_quotes')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_price_quotes_status ON price_quotes (status);
    END IF;
END $idxguard$;
-- 一个报价只能创建一个订单(§9.2.3)—— 硬不变量,唯一索引兜底
-- @index-guard ux_price_quotes_used_order ON price_quotes unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_price_quotes_used_order' AND i.indrelid = to_regclass('price_quotes')) THEN
        NULL;  -- 已在 price_quotes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_price_quotes_used_order' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('price_quotes'))) THEN
        RAISE EXCEPTION '[index-guard] ux_price_quotes_used_order 已存在但不在 price_quotes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_price_quotes_used_order' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('price_quotes')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_price_quotes_used_order ON price_quotes (used_order_id) WHERE used_order_id IS NOT NULL;
    END IF;
END $idxguard$;
-- 幂等键仅供 SELECT 去重查找(非唯一)· 幂等由应用层 SELECT-guard 尽力实现,不做硬 UNIQUE
--   (审核 P1:唯一约束 + "只匹配 issued" 的 guard 会在 key 复用/过期后落到 INSERT → 500 死锁;
--    真正的资金幂等硬约束是订单侧 (user_id, idempotency_key) 与报价侧 used_order_id 唯一)
-- @index-guard idx_price_quotes_idem ON price_quotes plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_price_quotes_idem' AND i.indrelid = to_regclass('price_quotes')) THEN
        NULL;  -- 已在 price_quotes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_price_quotes_idem' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('price_quotes'))) THEN
        RAISE EXCEPTION '[index-guard] idx_price_quotes_idem 已存在但不在 price_quotes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_price_quotes_idem' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('price_quotes')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_price_quotes_idem ON price_quotes (buyer_user_id, quote_type, idempotency_key) WHERE idempotency_key IS NOT NULL;
    END IF;
END $idxguard$;

-- =====================================================================================
-- 4. channel_pricing_relationships —— 经销商直属渠道价格关系 SSOT(§5.1 · P0-7)
--    admin 管理 · 默认空(空=平台直营扁平模型=现状 · 零回归)。不从 referral 派生。
--    同一经销商同一时刻只能有一个有效直属渠道(DB 级排他);禁止自己归属自己。
-- =====================================================================================
CREATE TABLE IF NOT EXISTS channel_pricing_relationships (
    id                          BIGSERIAL PRIMARY KEY,
    buyer_dealer_id             INTEGER NOT NULL,
    upstream_channel_account_id INTEGER NOT NULL,    -- 渠道收益受益人(内部 user_id)
    relationship_version        TEXT NOT NULL,
    cost_multiplier_bps         INTEGER NOT NULL DEFAULT 10000,  -- 该渠道给下级的合同系数(bps · 相对上游有效成本)
    status                      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
    effective_from             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    effective_to               TIMESTAMPTZ,
    reason                      TEXT,
    approved_by                 INTEGER,
    created_by                  INTEGER,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at                 TIMESTAMPTZ,
    CHECK (buyer_dealer_id <> upstream_channel_account_id),   -- 禁止自己归属自己
    CHECK (cost_multiplier_bps >= 10000)                     -- 上游赚差价模型：允许零差价，禁止低于上游成本
);
-- §5.1 同一经销商同一时刻只能有一个有效直属渠道账号
-- @index-guard ux_channel_rel_active ON channel_pricing_relationships unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_channel_rel_active' AND i.indrelid = to_regclass('channel_pricing_relationships')) THEN
        NULL;  -- 已在 channel_pricing_relationships 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_channel_rel_active' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('channel_pricing_relationships'))) THEN
        RAISE EXCEPTION '[index-guard] ux_channel_rel_active 已存在但不在 channel_pricing_relationships 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_channel_rel_active' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('channel_pricing_relationships')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_channel_rel_active ON channel_pricing_relationships (buyer_dealer_id) WHERE status = 'active' AND effective_to IS NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_channel_rel_upstream ON channel_pricing_relationships plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_channel_rel_upstream' AND i.indrelid = to_regclass('channel_pricing_relationships')) THEN
        NULL;  -- 已在 channel_pricing_relationships 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_channel_rel_upstream' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('channel_pricing_relationships'))) THEN
        RAISE EXCEPTION '[index-guard] idx_channel_rel_upstream 已存在但不在 channel_pricing_relationships 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_channel_rel_upstream' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('channel_pricing_relationships')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_channel_rel_upstream ON channel_pricing_relationships (upstream_channel_account_id);
    END IF;
END $idxguard$;
-- @index-guard idx_channel_rel_buyer ON channel_pricing_relationships plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_channel_rel_buyer' AND i.indrelid = to_regclass('channel_pricing_relationships')) THEN
        NULL;  -- 已在 channel_pricing_relationships 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_channel_rel_buyer' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('channel_pricing_relationships'))) THEN
        RAISE EXCEPTION '[index-guard] idx_channel_rel_buyer 已存在但不在 channel_pricing_relationships 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_channel_rel_buyer' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('channel_pricing_relationships')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_channel_rel_buyer ON channel_pricing_relationships (buyer_dealer_id, status);
    END IF;
END $idxguard$;

-- =====================================================================================
-- 5. channel_revenue_ledger —— 直属渠道收益台账(§5.1 · P0-7)
--    平台是在线订单卖方/收款方;渠道账号只是收益受益人。一订单一条(幂等)。无税字段。
-- =====================================================================================
CREATE TABLE IF NOT EXISTS channel_revenue_ledger (
    id                          BIGSERIAL PRIMARY KEY,
    channel_beneficiary_user_id INTEGER NOT NULL,
    buyer_dealer_id             INTEGER NOT NULL,
    recharge_order_id           TEXT NOT NULL,
    relationship_version        TEXT,
    price_quote_id              TEXT,
    catalog_version             TEXT,
    upstream_cost_basis_cents   INTEGER NOT NULL,    -- 上游有效成本(渠道账号的进货基准)
    buyer_paid_cents            INTEGER NOT NULL,    -- 下级实付(平台收款)
    channel_revenue_cents       INTEGER NOT NULL,    -- 渠道收益 = buyer_paid - upstream_cost_basis
    platform_seller             TEXT NOT NULL DEFAULT 'PLATFORM',
    status                      TEXT NOT NULL DEFAULT 'recorded'
                                  CHECK (status IN ('recorded','reversed')),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reversed_at                 TIMESTAMPTZ,
    UNIQUE(recharge_order_id),
    -- 审核 P2:§15.3 守恒恒等式 schema 级强制(渠道收益 = 下级实付 − 上游成本)
    CHECK (channel_revenue_cents = buyer_paid_cents - upstream_cost_basis_cents),
    CHECK (channel_revenue_cents >= 0)
);
-- @index-guard idx_channel_revenue_beneficiary ON channel_revenue_ledger plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_channel_revenue_beneficiary' AND i.indrelid = to_regclass('channel_revenue_ledger')) THEN
        NULL;  -- 已在 channel_revenue_ledger 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_channel_revenue_beneficiary' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('channel_revenue_ledger'))) THEN
        RAISE EXCEPTION '[index-guard] idx_channel_revenue_beneficiary 已存在但不在 channel_revenue_ledger 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_channel_revenue_beneficiary' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('channel_revenue_ledger')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_channel_revenue_beneficiary ON channel_revenue_ledger (channel_beneficiary_user_id, created_at DESC);
    END IF;
END $idxguard$;

-- =====================================================================================
-- 6. public_account_codes —— 随机不可枚举的对外账号编号(§8.1 / §16 Q32)
--    普通用户只见 service_account_code;经销商只见 channel_account_code。
--    绝不用递增 user_id;不可反查实名(实名仅 admin/财务经权限接口)。
-- =====================================================================================
CREATE TABLE IF NOT EXISTS public_account_codes (
    user_id              INTEGER PRIMARY KEY,
    service_account_code TEXT UNIQUE,               -- 'SV-XXXXXX'
    channel_account_code TEXT UNIQUE,               -- 'CH-XXXXXX'
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =====================================================================================
-- 7. feature_consumption_versions —— 功能消耗目录版本快照(§6.5)
--    发布时快照整张 feature_pricing;历史扣费按 consumption_version 可复原当时口径。
-- =====================================================================================
CREATE TABLE IF NOT EXISTS feature_consumption_versions (
    id               BIGSERIAL PRIMARY KEY,
    version_code     TEXT NOT NULL UNIQUE,          -- 'consume-2026-07-12-001'
    status           TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','archived')),
    snapshot_jsonb   JSONB NOT NULL,                -- {feature_code: {cost_points, requires_paid_points, ...}}
    effective_from   TIMESTAMPTZ,
    effective_to     TIMESTAMPTZ,
    reason           TEXT,
    created_by       INTEGER,
    approved_by      INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- @index-guard ux_feature_consume_published_open ON feature_consumption_versions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_feature_consume_published_open' AND i.indrelid = to_regclass('feature_consumption_versions')) THEN
        NULL;  -- 已在 feature_consumption_versions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_feature_consume_published_open' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('feature_consumption_versions'))) THEN
        RAISE EXCEPTION '[index-guard] ux_feature_consume_published_open 已存在但不在 feature_consumption_versions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_feature_consume_published_open' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('feature_consumption_versions')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_feature_consume_published_open ON feature_consumption_versions (status) WHERE status = 'published' AND effective_to IS NULL;
    END IF;
END $idxguard$;

-- =====================================================================================
-- 8. 现有表最小 ADD COLUMN(§10.2 · 全部可空 · 历史行 NULL 兼容 · 不伪造版本)
-- =====================================================================================
-- recharge_orders: 报价引用 + 目录版本 + 结算快照分离(P0-2) + 订单幂等 + 退款状态(P0-14)
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS price_quote_id TEXT;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS pricing_catalog_version TEXT;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS settlement_snapshot_jsonb JSONB;  -- 结算证据 · 与 pricing_snapshot_jsonb 分列
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS refund_status TEXT;               -- P0-14: NULL / pending_review(渠道已退现金待人工冲内账) / pending / approved / processed / completed / failed / rejected(与既有 V3.5 退款状态机同域)
-- §9.2.4 (buyer_user_id, idempotency_key) 唯一(部分索引 · 幂等 ADD)
-- @index-guard ux_recharge_orders_idem ON recharge_orders unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_recharge_orders_idem' AND i.indrelid = to_regclass('recharge_orders')) THEN
        NULL;  -- 已在 recharge_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_recharge_orders_idem' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('recharge_orders'))) THEN
        RAISE EXCEPTION '[index-guard] ux_recharge_orders_idem 已存在但不在 recharge_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_recharge_orders_idem' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('recharge_orders')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_recharge_orders_idem ON recharge_orders (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_recharge_orders_quote ON recharge_orders plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_recharge_orders_quote' AND i.indrelid = to_regclass('recharge_orders')) THEN
        NULL;  -- 已在 recharge_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_recharge_orders_quote' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('recharge_orders'))) THEN
        RAISE EXCEPTION '[index-guard] idx_recharge_orders_quote 已存在但不在 recharge_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_recharge_orders_quote' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('recharge_orders')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_recharge_orders_quote ON recharge_orders (price_quote_id) WHERE price_quote_id IS NOT NULL;
    END IF;
END $idxguard$;

-- point_transactions / customer_credit_transactions: 功能消耗版本锚(§1.2#12 · 可空)
ALTER TABLE point_transactions ADD COLUMN IF NOT EXISTS consumption_version TEXT;
ALTER TABLE customer_credit_transactions ADD COLUMN IF NOT EXISTS consumption_version TEXT;

-- =====================================================================================
-- 9. seed: 全新总闸 flag(默认关 · §14 · 部署后再验证再翻)
--    读取方 config.pricing_ssot_flags;默认关 → 现有链路零行为变化(宽进严出)。
-- =====================================================================================
INSERT INTO system_settings (key, value, value_type, description)
VALUES
  ('PRICING_DUAL_SSOT_ENABLED', 'false', 'boolean', '双价目表报价/下单总闸(默认关·关时走旧价路径)'),
  ('CHANNEL_PRICING_ENABLED',   'false', 'boolean', '直属渠道分级收益总闸(默认关·关时扁平只认第一层)'),
  ('PRICING_QUOTE_REQUIRED',    'false', 'boolean', '下单强制 price_quote_id 总闸(默认关·灰度切流后翻)')
ON CONFLICT (key) DO NOTHING;
