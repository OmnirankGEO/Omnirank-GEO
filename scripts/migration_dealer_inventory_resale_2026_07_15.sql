-- Dealer inventory resale SSOT (2026-07-15)
-- Additive/idempotent only. DDL owner: ROLE=prestart single-flight migration manifest.
-- This migration seeds the independent flag FALSE and never infers/backfills acquisition cost.

INSERT INTO system_settings(key, value, value_type, description)
VALUES (
    'DEALER_INVENTORY_RESALE_ENABLED', 'false', 'boolean',
    '经销商逐级库存转售总闸（默认关闭；只允许 readiness 全绿后另行审批开启）'
)
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS dealer_resale_global_settings (
    singleton_id                 SMALLINT PRIMARY KEY DEFAULT 1,
    default_downstream_markup_bps INTEGER NOT NULL DEFAULT 10000,
    platform_seller_user_id      INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    settings_version             TEXT NOT NULL DEFAULT 'dealer-resale-v1',
    row_version                  BIGINT NOT NULL DEFAULT 1,
    updated_by                   INTEGER,
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_resale_global_singleton CHECK (singleton_id = 1),
    CONSTRAINT chk_dealer_resale_default_markup CHECK (default_downstream_markup_bps >= 10000),
    CONSTRAINT chk_dealer_resale_settings_version CHECK (BTRIM(settings_version) <> ''),
    CONSTRAINT chk_dealer_resale_settings_row_version CHECK (row_version >= 1)
);

INSERT INTO dealer_resale_global_settings(singleton_id)
VALUES (1)
ON CONFLICT (singleton_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS dealer_resale_policies (
    seller_user_id               INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
    downstream_markup_bps        INTEGER NOT NULL DEFAULT 10000,
    authorized_min_markup_bps    INTEGER NOT NULL DEFAULT 10000,
    authorized_max_markup_bps    INTEGER NOT NULL DEFAULT 30000,
    policy_version               TEXT NOT NULL,
    row_version                  BIGINT NOT NULL DEFAULT 1,
    updated_by                   INTEGER,
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_resale_policy_markup CHECK (downstream_markup_bps >= 10000),
    CONSTRAINT chk_dealer_resale_policy_bounds CHECK (
        authorized_min_markup_bps >= 10000
        AND authorized_max_markup_bps >= authorized_min_markup_bps
        AND downstream_markup_bps BETWEEN authorized_min_markup_bps AND authorized_max_markup_bps
    ),
    CONSTRAINT chk_dealer_resale_policy_version CHECK (BTRIM(policy_version) <> ''),
    CONSTRAINT chk_dealer_resale_policy_row_version CHECK (row_version >= 1)
);

CREATE TABLE IF NOT EXISTS dealer_inventory_lots (
    lot_id                        TEXT PRIMARY KEY,
    owner_agent_user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    source_order_id               TEXT REFERENCES recharge_orders(id) ON DELETE RESTRICT,
    source_transfer_id            TEXT,
    original_points               BIGINT NOT NULL,
    remaining_points              BIGINT NOT NULL,
    reserved_points               BIGINT NOT NULL DEFAULT 0,
    acquisition_cost_cents        BIGINT NOT NULL,
    remaining_cost_cents          BIGINT NOT NULL,
    reserved_cost_cents           BIGINT NOT NULL DEFAULT 0,
    acquired_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status                        TEXT NOT NULL DEFAULT 'active',
    source_kind                   TEXT NOT NULL,
    pricing_version               TEXT NOT NULL,
    quote_id                      TEXT REFERENCES price_quotes(quote_id) ON DELETE RESTRICT,
    evidence_jsonb                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_lot_id_nonblank CHECK (BTRIM(lot_id) <> ''),
    CONSTRAINT chk_dealer_lot_points_positive CHECK (original_points > 0),
    CONSTRAINT chk_dealer_lot_acquisition_cost_positive CHECK (acquisition_cost_cents > 0),
    CONSTRAINT chk_dealer_lot_points_conservation CHECK (
        remaining_points >= 0 AND reserved_points >= 0
        AND remaining_points + reserved_points <= original_points
    ),
    CONSTRAINT chk_dealer_lot_cost_conservation CHECK (
        acquisition_cost_cents >= 0 AND remaining_cost_cents >= 0 AND reserved_cost_cents >= 0
        AND remaining_cost_cents + reserved_cost_cents <= acquisition_cost_cents
    ),
    CONSTRAINT chk_dealer_lot_status CHECK (status IN ('active','consumed','refund_pending','reversed')),
    CONSTRAINT chk_dealer_lot_source_kind CHECK (
        source_kind IN ('manufacturer_origin','platform_purchase','direct_resale','opening_balance')
    ),
    CONSTRAINT chk_dealer_lot_pricing_version CHECK (BTRIM(pricing_version) <> ''),
    CONSTRAINT chk_dealer_lot_evidence_object CHECK (jsonb_typeof(evidence_jsonb) = 'object')
);

-- @index-guard ux_dealer_lot_source_order ON dealer_inventory_lots unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_dealer_lot_source_order' AND i.indrelid = to_regclass('public.dealer_inventory_lots')) THEN
        NULL;  -- 已在 public.dealer_inventory_lots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_dealer_lot_source_order' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_dealer_lot_source_order 已存在但不在 public.dealer_inventory_lots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_dealer_lot_source_order' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_dealer_lot_source_order ON public.dealer_inventory_lots (source_order_id) WHERE source_order_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_dealer_lot_owner_fifo ON dealer_inventory_lots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_lot_owner_fifo' AND i.indrelid = to_regclass('public.dealer_inventory_lots')) THEN
        NULL;  -- 已在 public.dealer_inventory_lots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_lot_owner_fifo' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_lot_owner_fifo 已存在但不在 public.dealer_inventory_lots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_lot_owner_fifo' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_lot_owner_fifo ON public.dealer_inventory_lots (owner_agent_user_id, acquired_at, lot_id) WHERE status = 'active' AND remaining_points > 0;
    END IF;
END $idxguard$;

-- 厂家是供应链第一位真实卖方，不是“无限库存”特例。原始批次只能由 admin
-- 以显式成本、版本、证据和幂等键发行；后续厂家→一级仍走同一 FIFO 转售事务。
CREATE TABLE IF NOT EXISTS dealer_manufacturer_lot_issuances (
    issue_id                       TEXT PRIMARY KEY,
    lot_id                         TEXT NOT NULL UNIQUE REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    platform_seller_user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    points                         BIGINT NOT NULL,
    acquisition_cost_cents         BIGINT NOT NULL,
    pricing_version                TEXT NOT NULL,
    idempotency_key                TEXT NOT NULL UNIQUE,
    evidence_jsonb                 JSONB NOT NULL,
    issued_by                      INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    issued_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_manufacturer_issue_values CHECK (
        points > 0 AND acquisition_cost_cents > 0 AND BTRIM(pricing_version) <> ''
        AND BTRIM(idempotency_key) <> ''
    ),
    CONSTRAINT chk_dealer_manufacturer_issue_evidence CHECK (
        jsonb_typeof(evidence_jsonb)='object' AND evidence_jsonb <> '{}'::jsonb
    )
);

CREATE TABLE IF NOT EXISTS dealer_resale_orders (
    order_id                       TEXT PRIMARY KEY REFERENCES recharge_orders(id) ON DELETE RESTRICT,
    seller_user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    buyer_user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    points                         BIGINT NOT NULL,
    seller_lot_allocations         JSONB NOT NULL,
    seller_cost_basis_cents        BIGINT NOT NULL,
    sale_amount_cents              BIGINT NOT NULL,
    margin_cents                   BIGINT NOT NULL,
    downstream_markup_bps          INTEGER NOT NULL,
    pricing_version                TEXT NOT NULL,
    quote_id                       TEXT NOT NULL REFERENCES price_quotes(quote_id) ON DELETE RESTRICT,
    relationship_version           TEXT,
    payment_collector              TEXT NOT NULL DEFAULT 'PLATFORM',
    refund_responsible_user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    source_kind                    TEXT NOT NULL,
    state                          TEXT NOT NULL DEFAULT 'reserved',
    transfer_id                    TEXT,
    reserved_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paid_at                        TIMESTAMPTZ,
    refund_deadline                TIMESTAMPTZ,
    refunded_at                    TIMESTAMPTZ,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_dealer_resale_order_quote UNIQUE (quote_id),
    CONSTRAINT ux_dealer_resale_order_transfer UNIQUE (transfer_id),
    CONSTRAINT chk_dealer_resale_order_parties CHECK (seller_user_id <> buyer_user_id),
    CONSTRAINT chk_dealer_resale_order_points CHECK (points > 0),
    CONSTRAINT chk_dealer_resale_order_allocations CHECK (jsonb_typeof(seller_lot_allocations) = 'array'),
    CONSTRAINT chk_dealer_resale_order_money CHECK (
        seller_cost_basis_cents >= 0 AND sale_amount_cents > 0 AND margin_cents >= 0
        AND sale_amount_cents = seller_cost_basis_cents + margin_cents
    ),
    CONSTRAINT chk_dealer_resale_order_cost_positive CHECK (seller_cost_basis_cents > 0),
    CONSTRAINT chk_dealer_resale_order_markup CHECK (downstream_markup_bps >= 10000),
    CONSTRAINT chk_dealer_resale_order_versions CHECK (BTRIM(pricing_version) <> ''),
    CONSTRAINT chk_dealer_resale_order_collector CHECK (payment_collector = 'PLATFORM'),
    CONSTRAINT chk_dealer_resale_order_refund_owner CHECK (refund_responsible_user_id = seller_user_id),
    CONSTRAINT chk_dealer_resale_order_source CHECK (source_kind IN ('platform_purchase','direct_resale')),
    CONSTRAINT chk_dealer_resale_order_source_allocations CHECK (
        jsonb_array_length(seller_lot_allocations)>0
    ),
    CONSTRAINT chk_dealer_resale_order_state CHECK (state IN ('reserved','paid','refund_pending','refunded','cancelled')),
    CONSTRAINT chk_dealer_resale_order_paid_fields CHECK (
        (state IN ('reserved','cancelled') AND paid_at IS NULL AND refund_deadline IS NULL AND transfer_id IS NULL)
        OR (state IN ('paid','refund_pending','refunded') AND paid_at IS NOT NULL AND refund_deadline IS NOT NULL AND transfer_id IS NOT NULL)
    )
);

-- @index-guard idx_dealer_resale_orders_seller ON dealer_resale_orders plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_resale_orders_seller' AND i.indrelid = to_regclass('public.dealer_resale_orders')) THEN
        NULL;  -- 已在 public.dealer_resale_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_resale_orders_seller' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_resale_orders_seller 已存在但不在 public.dealer_resale_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_resale_orders_seller' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_resale_orders_seller ON public.dealer_resale_orders (seller_user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_dealer_resale_orders_buyer ON dealer_resale_orders plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_resale_orders_buyer' AND i.indrelid = to_regclass('public.dealer_resale_orders')) THEN
        NULL;  -- 已在 public.dealer_resale_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_resale_orders_buyer' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_resale_orders_buyer 已存在但不在 public.dealer_resale_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_resale_orders_buyer' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_resale_orders_buyer ON public.dealer_resale_orders (buyer_user_id, created_at DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS dealer_inventory_lot_allocations (
    allocation_id                  BIGSERIAL PRIMARY KEY,
    order_id                       TEXT NOT NULL REFERENCES dealer_resale_orders(order_id) ON DELETE RESTRICT,
    seller_lot_id                  TEXT NOT NULL REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    allocation_seq                 INTEGER NOT NULL,
    points                         BIGINT NOT NULL,
    cost_basis_cents               BIGINT NOT NULL,
    status                         TEXT NOT NULL DEFAULT 'reserved',
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at                    TIMESTAMPTZ,
    restored_at                    TIMESTAMPTZ,
    CONSTRAINT ux_dealer_lot_allocation UNIQUE (order_id, seller_lot_id),
    CONSTRAINT ux_dealer_lot_allocation_seq UNIQUE (order_id, allocation_seq),
    CONSTRAINT chk_dealer_lot_allocation_values CHECK (allocation_seq >= 0 AND points > 0 AND cost_basis_cents >= 0),
    CONSTRAINT chk_dealer_lot_allocation_status CHECK (status IN ('reserved','consumed','restored','cancelled'))
);

CREATE TABLE IF NOT EXISTS dealer_inventory_transfers (
    transfer_id                    TEXT PRIMARY KEY,
    order_id                       TEXT NOT NULL UNIQUE REFERENCES dealer_resale_orders(order_id) ON DELETE RESTRICT,
    seller_user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    buyer_user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    points                         BIGINT NOT NULL,
    seller_cost_basis_cents        BIGINT NOT NULL,
    sale_amount_cents              BIGINT NOT NULL,
    margin_cents                   BIGINT NOT NULL,
    state                          TEXT NOT NULL DEFAULT 'settled',
    settled_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    refunded_at                    TIMESTAMPTZ,
    CONSTRAINT chk_dealer_transfer_parties CHECK (seller_user_id <> buyer_user_id),
    CONSTRAINT chk_dealer_transfer_points CHECK (points > 0),
    CONSTRAINT chk_dealer_transfer_money CHECK (
        seller_cost_basis_cents >= 0 AND sale_amount_cents >= 0 AND margin_cents >= 0
        AND sale_amount_cents = seller_cost_basis_cents + margin_cents
    ),
    CONSTRAINT chk_dealer_transfer_cost_positive CHECK (seller_cost_basis_cents > 0),
    CONSTRAINT chk_dealer_transfer_state CHECK (state IN ('settled','refunded'))
);

CREATE TABLE IF NOT EXISTS dealer_inventory_transfer_entries (
    entry_id                        BIGSERIAL PRIMARY KEY,
    transfer_id                    TEXT NOT NULL REFERENCES dealer_inventory_transfers(transfer_id) ON DELETE RESTRICT,
    owner_agent_user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    counterparty_user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    direction                      TEXT NOT NULL,
    points_delta                   BIGINT NOT NULL,
    lot_id                         TEXT REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    cost_basis_cents               BIGINT NOT NULL,
    balance_paid_after             BIGINT NOT NULL,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_dealer_transfer_entry UNIQUE (transfer_id, owner_agent_user_id, direction),
    CONSTRAINT chk_dealer_transfer_direction CHECK (direction IN ('transfer_out','transfer_in','refund_out','refund_in')),
    CONSTRAINT chk_dealer_transfer_delta_sign CHECK (
        (direction IN ('transfer_out','refund_out') AND points_delta < 0)
        OR (direction IN ('transfer_in','refund_in') AND points_delta > 0)
    ),
    CONSTRAINT chk_dealer_transfer_entry_cost CHECK (cost_basis_cents >= 0 AND balance_paid_after >= 0)
);

-- @index-guard idx_dealer_transfer_entries_owner ON dealer_inventory_transfer_entries plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_transfer_entries_owner' AND i.indrelid = to_regclass('public.dealer_inventory_transfer_entries')) THEN
        NULL;  -- 已在 public.dealer_inventory_transfer_entries 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_transfer_entries_owner' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_transfer_entries_owner 已存在但不在 public.dealer_inventory_transfer_entries 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_transfer_entries_owner' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_transfer_entries_owner ON public.dealer_inventory_transfer_entries (owner_agent_user_id, created_at DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS dealer_resale_profit_ledger (
    profit_id                       BIGSERIAL PRIMARY KEY,
    order_id                       TEXT NOT NULL UNIQUE REFERENCES dealer_resale_orders(order_id) ON DELETE RESTRICT,
    seller_user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    buyer_user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    seller_cost_basis_cents        BIGINT NOT NULL,
    sale_amount_cents              BIGINT NOT NULL,
    margin_cents                   BIGINT NOT NULL,
    status                         TEXT NOT NULL DEFAULT 'pending',
    available_at                   TIMESTAMPTZ NOT NULL,
    available_since                TIMESTAMPTZ,
    reversed_at                    TIMESTAMPTZ,
    revenue_ledger_id              BIGINT UNIQUE REFERENCES agent_revenue_ledger(id) ON DELETE RESTRICT,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_profit_parties CHECK (seller_user_id <> buyer_user_id),
    CONSTRAINT chk_dealer_profit_money CHECK (
        seller_cost_basis_cents >= 0 AND sale_amount_cents >= 0 AND margin_cents >= 0
        AND sale_amount_cents = seller_cost_basis_cents + margin_cents
    ),
    CONSTRAINT chk_dealer_profit_cost_positive CHECK (seller_cost_basis_cents > 0),
    CONSTRAINT chk_dealer_profit_status CHECK (status IN ('pending','available','reversed'))
);

-- @index-guard idx_dealer_profit_maturity ON dealer_resale_profit_ledger plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_profit_maturity' AND i.indrelid = to_regclass('public.dealer_resale_profit_ledger')) THEN
        NULL;  -- 已在 public.dealer_resale_profit_ledger 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_profit_maturity' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_profit_maturity 已存在但不在 public.dealer_resale_profit_ledger 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_profit_maturity' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_profit_maturity ON public.dealer_resale_profit_ledger (available_at, profit_id) WHERE status = 'pending';
    END IF;
END $idxguard$;
-- @index-guard idx_dealer_profit_seller ON dealer_resale_profit_ledger plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_profit_seller' AND i.indrelid = to_regclass('public.dealer_resale_profit_ledger')) THEN
        NULL;  -- 已在 public.dealer_resale_profit_ledger 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_profit_seller' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_profit_seller 已存在但不在 public.dealer_resale_profit_ledger 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_profit_seller' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_profit_seller ON public.dealer_resale_profit_ledger (seller_user_id, created_at DESC);
    END IF;
END $idxguard$;

ALTER TABLE dealer_resale_profit_ledger
    ADD COLUMN IF NOT EXISTS revenue_ledger_id BIGINT UNIQUE
    REFERENCES agent_revenue_ledger(id) ON DELETE RESTRICT;

-- 服务商向普通客户销售是独立的消费者交易，不把客户伪装成经销商，也不给客户建库存批次。
-- 零售报价消费时先冻结服务商 FIFO 批次；支付回调只消费本表的不可变快照。
CREATE TABLE IF NOT EXISTS dealer_consumer_sales (
    order_id                       TEXT PRIMARY KEY REFERENCES recharge_orders(id) ON DELETE RESTRICT,
    seller_user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    consumer_user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    points                         BIGINT NOT NULL,
    seller_lot_allocations         JSONB NOT NULL,
    seller_cost_basis_cents        BIGINT NOT NULL,
    sale_amount_cents              BIGINT NOT NULL,
    margin_cents                   BIGINT NOT NULL,
    downstream_markup_bps          INTEGER NOT NULL,
    pricing_version                TEXT NOT NULL,
    quote_id                       TEXT NOT NULL UNIQUE REFERENCES price_quotes(quote_id) ON DELETE RESTRICT,
    payment_collector              TEXT NOT NULL DEFAULT 'PLATFORM',
    refund_responsible_user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    digital_goods_policy_code      TEXT NOT NULL,
    digital_goods_acknowledged_at  TIMESTAMPTZ NOT NULL,
    state                          TEXT NOT NULL DEFAULT 'reserved',
    transfer_id                    TEXT UNIQUE,
    revenue_ledger_id              BIGINT UNIQUE REFERENCES agent_revenue_ledger(id) ON DELETE RESTRICT,
    reserved_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paid_at                        TIMESTAMPTZ,
    refunded_at                    TIMESTAMPTZ,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_consumer_sale_parties CHECK (seller_user_id <> consumer_user_id),
    CONSTRAINT chk_dealer_consumer_sale_points CHECK (points > 0),
    CONSTRAINT chk_dealer_consumer_sale_allocations CHECK (
        jsonb_typeof(seller_lot_allocations)='array'
        AND jsonb_array_length(seller_lot_allocations)>0
    ),
    CONSTRAINT chk_dealer_consumer_sale_money CHECK (
        seller_cost_basis_cents >= 0 AND sale_amount_cents > 0 AND margin_cents >= 0
        AND sale_amount_cents = seller_cost_basis_cents + margin_cents
    ),
    CONSTRAINT chk_dealer_consumer_sale_cost_positive CHECK (seller_cost_basis_cents > 0),
    CONSTRAINT chk_dealer_consumer_sale_markup CHECK (downstream_markup_bps >= 10000),
    CONSTRAINT chk_dealer_consumer_sale_version CHECK (BTRIM(pricing_version) <> ''),
    CONSTRAINT chk_dealer_consumer_sale_collector CHECK (payment_collector='PLATFORM'),
    CONSTRAINT chk_dealer_consumer_sale_refund_owner CHECK (refund_responsible_user_id=seller_user_id),
    CONSTRAINT chk_dealer_consumer_sale_policy CHECK (BTRIM(digital_goods_policy_code) <> ''),
    CONSTRAINT chk_dealer_consumer_sale_state CHECK (
        state IN ('reserved','paid','refund_pending','refunded','manual_review','cancelled')
    ),
    CONSTRAINT chk_dealer_consumer_sale_paid_fields CHECK (
        (state IN ('reserved','cancelled') AND paid_at IS NULL AND transfer_id IS NULL AND revenue_ledger_id IS NULL)
        OR (state IN ('paid','refund_pending','refunded','manual_review')
            AND paid_at IS NOT NULL AND transfer_id IS NOT NULL AND revenue_ledger_id IS NOT NULL)
    )
);

-- @index-guard idx_dealer_consumer_sales_seller ON dealer_consumer_sales plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_consumer_sales_seller' AND i.indrelid = to_regclass('public.dealer_consumer_sales')) THEN
        NULL;  -- 已在 public.dealer_consumer_sales 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_consumer_sales_seller' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_consumer_sales_seller 已存在但不在 public.dealer_consumer_sales 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_consumer_sales_seller' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_consumer_sales_seller ON public.dealer_consumer_sales (seller_user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_dealer_consumer_sales_consumer ON dealer_consumer_sales plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_consumer_sales_consumer' AND i.indrelid = to_regclass('public.dealer_consumer_sales')) THEN
        NULL;  -- 已在 public.dealer_consumer_sales 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_consumer_sales_consumer' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_consumer_sales_consumer 已存在但不在 public.dealer_consumer_sales 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_consumer_sales_consumer' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_consumer_sales_consumer ON public.dealer_consumer_sales (consumer_user_id, created_at DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS dealer_consumer_lot_allocations (
    allocation_id                  BIGSERIAL PRIMARY KEY,
    order_id                       TEXT NOT NULL REFERENCES dealer_consumer_sales(order_id) ON DELETE RESTRICT,
    seller_lot_id                  TEXT NOT NULL REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    allocation_seq                 INTEGER NOT NULL,
    points                         BIGINT NOT NULL,
    cost_basis_cents               BIGINT NOT NULL,
    status                         TEXT NOT NULL DEFAULT 'reserved',
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at                    TIMESTAMPTZ,
    restored_at                    TIMESTAMPTZ,
    CONSTRAINT ux_dealer_consumer_allocation UNIQUE (order_id, seller_lot_id),
    CONSTRAINT ux_dealer_consumer_allocation_seq UNIQUE (order_id, allocation_seq),
    CONSTRAINT chk_dealer_consumer_allocation_values CHECK (
        allocation_seq >= 0 AND points > 0 AND cost_basis_cents >= 0
    ),
    CONSTRAINT chk_dealer_consumer_allocation_status CHECK (
        status IN ('reserved','consumed','restored','cancelled')
    )
);

CREATE TABLE IF NOT EXISTS dealer_consumer_transfer_entries (
    entry_id                        BIGSERIAL PRIMARY KEY,
    transfer_id                    TEXT NOT NULL,
    order_id                       TEXT NOT NULL REFERENCES dealer_consumer_sales(order_id) ON DELETE RESTRICT,
    seller_user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    consumer_user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    direction                      TEXT NOT NULL,
    points_delta                   BIGINT NOT NULL,
    cost_basis_cents               BIGINT NOT NULL,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_dealer_consumer_transfer_side UNIQUE (transfer_id, direction),
    CONSTRAINT fk_dealer_consumer_transfer_sale
        FOREIGN KEY (transfer_id) REFERENCES dealer_consumer_sales(transfer_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT chk_dealer_consumer_transfer_direction CHECK (
        direction IN ('seller_out','consumer_in','consumer_refund_out','seller_refund_in')
    ),
    CONSTRAINT chk_dealer_consumer_transfer_sign CHECK (
        (direction IN ('seller_out','consumer_refund_out') AND points_delta < 0)
        OR (direction IN ('consumer_in','seller_refund_in') AND points_delta > 0)
    ),
    CONSTRAINT chk_dealer_consumer_transfer_cost CHECK (cost_basis_cents >= 0)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='fk_dealer_lot_source_transfer'
    ) THEN
        ALTER TABLE dealer_inventory_lots
            ADD CONSTRAINT fk_dealer_lot_source_transfer
            FOREIGN KEY (source_transfer_id) REFERENCES dealer_inventory_transfers(transfer_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='fk_dealer_order_transfer'
    ) THEN
        ALTER TABLE dealer_resale_orders
            ADD CONSTRAINT fk_dealer_order_transfer
            FOREIGN KEY (transfer_id) REFERENCES dealer_inventory_transfers(transfer_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS dealer_resale_refunds (
    refund_id                       TEXT PRIMARY KEY,
    order_id                       TEXT NOT NULL UNIQUE REFERENCES dealer_resale_orders(order_id) ON DELETE RESTRICT,
    requested_by_user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    responsible_seller_user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status                          TEXT NOT NULL DEFAULT 'requested',
    reason                          TEXT NOT NULL DEFAULT '',
    requested_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deadline_at                     TIMESTAMPTZ NOT NULL,
    external_channel               TEXT,
    external_refund_id             TEXT,
    completed_at                    TIMESTAMPTZ,
    rejected_at                     TIMESTAMPTZ,
    rejection_reason               TEXT,
    audit_jsonb                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT chk_dealer_refund_status CHECK (status IN ('requested','processing','completed','rejected','manual_review')),
    CONSTRAINT chk_dealer_refund_audit_object CHECK (jsonb_typeof(audit_jsonb) = 'object')
);

-- @index-guard idx_dealer_refunds_status ON dealer_resale_refunds plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_refunds_status' AND i.indrelid = to_regclass('public.dealer_resale_refunds')) THEN
        NULL;  -- 已在 public.dealer_resale_refunds 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_refunds_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_refunds_status 已存在但不在 public.dealer_resale_refunds 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_refunds_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_refunds_status ON public.dealer_resale_refunds (status, requested_at);
    END IF;
END $idxguard$;
-- @index-guard ux_dealer_resale_external_ref_global ON dealer_resale_refunds unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_dealer_resale_external_ref_global' AND i.indrelid = to_regclass('public.dealer_resale_refunds')) THEN
        NULL;  -- 已在 public.dealer_resale_refunds 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_dealer_resale_external_ref_global' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_dealer_resale_external_ref_global 已存在但不在 public.dealer_resale_refunds 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_dealer_resale_external_ref_global' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_dealer_resale_external_ref_global ON public.dealer_resale_refunds (LOWER(BTRIM(external_channel)), BTRIM(external_refund_id)) WHERE external_channel IS NOT NULL AND external_refund_id IS NOT NULL;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS consumer_refund_cases (
    case_id                         TEXT PRIMARY KEY,
    consumer_user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    responsible_service_user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    source_order_id                  TEXT NOT NULL REFERENCES dealer_consumer_sales(order_id) ON DELETE RESTRICT,
    policy_code                      TEXT NOT NULL,
    digital_goods_acknowledged_at    TIMESTAMPTZ NOT NULL,
    status                           TEXT NOT NULL DEFAULT 'requested',
    reason                           TEXT NOT NULL DEFAULT '',
    execution_jsonb                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_consumer_refund_parties CHECK (consumer_user_id <> responsible_service_user_id),
    CONSTRAINT chk_consumer_refund_policy CHECK (BTRIM(policy_code) <> ''),
    CONSTRAINT chk_consumer_refund_execution CHECK (jsonb_typeof(execution_jsonb)='object'),
    CONSTRAINT chk_consumer_refund_status CHECK (status IN ('requested','service_review','platform_execution','completed','rejected','manual_review')),
    CONSTRAINT ux_consumer_refund_source UNIQUE (source_order_id)
);

ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS execution_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE agent_inventory_transactions
    ADD COLUMN IF NOT EXISTS transfer_id TEXT;
ALTER TABLE agent_inventory_transactions
    ADD COLUMN IF NOT EXISTS lot_id TEXT;
ALTER TABLE agent_inventory_transactions
    ADD COLUMN IF NOT EXISTS counterparty_user_id INTEGER;
ALTER TABLE agent_inventory_transactions
    ADD COLUMN IF NOT EXISTS cost_basis_cents BIGINT;

-- Production already has this legacy CHECK. The resale writer is unusable unless
-- the prestart migration widens that exact named constraint before web starts.
ALTER TABLE agent_inventory_transactions
    DROP CONSTRAINT IF EXISTS agent_inventory_transactions_type_check;
ALTER TABLE agent_inventory_transactions
    ADD CONSTRAINT agent_inventory_transactions_type_check CHECK (type IN (
        'purchase_prepay', 'purchase_auto', 'purchase_admin_adjust',
        'purchase_from_commission', 'allocate_to_customer',
        'allocate_to_customer_offline', 'revoke_from_customer',
        'admin_adjust', 'refund_clawback',
        'manufacturer_origin_in', 'resale_transfer_out', 'resale_transfer_in',
        'consumer_sale_out', 'consumer_refund_in',
        'resale_refund_out', 'resale_refund_in'
    ));

-- @index-guard idx_agent_inventory_transactions_transfer ON agent_inventory_transactions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_agent_inventory_transactions_transfer' AND i.indrelid = to_regclass('public.agent_inventory_transactions')) THEN
        NULL;  -- 已在 public.agent_inventory_transactions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_agent_inventory_transactions_transfer' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_agent_inventory_transactions_transfer 已存在但不在 public.agent_inventory_transactions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_agent_inventory_transactions_transfer' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_agent_inventory_transactions_transfer ON public.agent_inventory_transactions (transfer_id) WHERE transfer_id IS NOT NULL;
    END IF;
END $idxguard$;

CREATE OR REPLACE FUNCTION dealer_resale_order_immutable_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF ROW(
        NEW.seller_user_id, NEW.buyer_user_id, NEW.points, NEW.seller_lot_allocations,
        NEW.seller_cost_basis_cents, NEW.sale_amount_cents, NEW.margin_cents,
        NEW.downstream_markup_bps, NEW.pricing_version, NEW.quote_id,
        NEW.relationship_version, NEW.payment_collector, NEW.refund_responsible_user_id,
        NEW.source_kind
    ) IS DISTINCT FROM ROW(
        OLD.seller_user_id, OLD.buyer_user_id, OLD.points, OLD.seller_lot_allocations,
        OLD.seller_cost_basis_cents, OLD.sale_amount_cents, OLD.margin_cents,
        OLD.downstream_markup_bps, OLD.pricing_version, OLD.quote_id,
        OLD.relationship_version, OLD.payment_collector, OLD.refund_responsible_user_id,
        OLD.source_kind
    ) THEN
        RAISE EXCEPTION 'dealer resale order immutable snapshot cannot change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_dealer_resale_order_immutable'
          AND tgrelid = 'dealer_resale_orders'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_dealer_resale_order_immutable
        BEFORE UPDATE ON dealer_resale_orders
        FOR EACH ROW EXECUTE FUNCTION dealer_resale_order_immutable_guard();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION dealer_consumer_sale_immutable_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF ROW(
        NEW.seller_user_id, NEW.consumer_user_id, NEW.points, NEW.seller_lot_allocations,
        NEW.seller_cost_basis_cents, NEW.sale_amount_cents, NEW.margin_cents,
        NEW.downstream_markup_bps, NEW.pricing_version, NEW.quote_id,
        NEW.payment_collector, NEW.refund_responsible_user_id,
        NEW.digital_goods_policy_code, NEW.digital_goods_acknowledged_at
    ) IS DISTINCT FROM ROW(
        OLD.seller_user_id, OLD.consumer_user_id, OLD.points, OLD.seller_lot_allocations,
        OLD.seller_cost_basis_cents, OLD.sale_amount_cents, OLD.margin_cents,
        OLD.downstream_markup_bps, OLD.pricing_version, OLD.quote_id,
        OLD.payment_collector, OLD.refund_responsible_user_id,
        OLD.digital_goods_policy_code, OLD.digital_goods_acknowledged_at
    ) THEN
        RAISE EXCEPTION 'dealer consumer sale immutable snapshot cannot change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname='trg_dealer_consumer_sale_immutable'
          AND tgrelid='dealer_consumer_sales'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_dealer_consumer_sale_immutable
        BEFORE UPDATE ON dealer_consumer_sales
        FOR EACH ROW EXECUTE FUNCTION dealer_consumer_sale_immutable_guard();
    END IF;
END $$;

-- Final prestart integrity pass. Idempotent DDL must not accept an existing
-- NOT VALID constraint or a narrow money/points column.
DO $$
DECLARE r RECORD;
DECLARE bad TEXT;
DECLARE contract_count INTEGER;
DECLARE contract_digest TEXT;
DECLARE index_contract_count INTEGER;
DECLARE index_contract_digest TEXT;
BEGIN
    SELECT c.oid::regclass AS rel, pc.conname INTO r
        FROM pg_constraint pc JOIN pg_class c ON c.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=current_schema() AND NOT pc.convalidated
          AND c.relname IN (
            'dealer_resale_global_settings','dealer_resale_policies','dealer_inventory_lots',
            'dealer_manufacturer_lot_issuances','dealer_resale_orders',
            'dealer_inventory_lot_allocations','dealer_inventory_transfers',
            'dealer_inventory_transfer_entries','dealer_resale_profit_ledger',
            'dealer_consumer_sales','dealer_consumer_lot_allocations',
            'dealer_consumer_transfer_entries','dealer_resale_refunds'
          )
        LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'pre-existing NOT VALID constraint must be reviewed before prestart: %.%',
            r.rel, r.conname;
    END IF;
    -- Do not silently validate pre-existing drift during prestart.
    SELECT string_agg(v.t || '.' || v.c, ', ') INTO bad
      FROM (VALUES
        ('dealer_resale_global_settings','row_version'),
        ('dealer_resale_policies','row_version'),
        ('dealer_inventory_lots','original_points'),
        ('dealer_inventory_lots','remaining_points'),
        ('dealer_inventory_lots','reserved_points'),
        ('dealer_inventory_lots','acquisition_cost_cents'),
        ('dealer_inventory_lots','remaining_cost_cents'),
        ('dealer_inventory_lots','reserved_cost_cents'),
        ('dealer_manufacturer_lot_issuances','points'),
        ('dealer_manufacturer_lot_issuances','acquisition_cost_cents'),
        ('dealer_resale_orders','points'),
        ('dealer_resale_orders','seller_cost_basis_cents'),
        ('dealer_resale_orders','sale_amount_cents'),
        ('dealer_resale_orders','margin_cents'),
        ('dealer_inventory_lot_allocations','points'),
        ('dealer_inventory_lot_allocations','cost_basis_cents'),
        ('dealer_inventory_transfers','points'),
        ('dealer_inventory_transfers','seller_cost_basis_cents'),
        ('dealer_inventory_transfers','sale_amount_cents'),
        ('dealer_inventory_transfers','margin_cents'),
        ('dealer_inventory_transfer_entries','points_delta'),
        ('dealer_inventory_transfer_entries','cost_basis_cents'),
        ('dealer_inventory_transfer_entries','balance_paid_after'),
        ('dealer_consumer_sales','points'),
        ('dealer_consumer_sales','seller_cost_basis_cents'),
        ('dealer_consumer_sales','sale_amount_cents'),
        ('dealer_consumer_sales','margin_cents'),
        ('dealer_consumer_lot_allocations','points'),
        ('dealer_consumer_lot_allocations','cost_basis_cents'),
        ('dealer_consumer_transfer_entries','points_delta'),
        ('dealer_consumer_transfer_entries','cost_basis_cents'),
        ('dealer_resale_profit_ledger','seller_cost_basis_cents'),
        ('dealer_resale_profit_ledger','sale_amount_cents'),
        ('dealer_resale_profit_ledger','margin_cents')
      ) AS v(t,c)
      LEFT JOIN information_schema.columns x
        ON x.table_schema=current_schema() AND x.table_name=v.t
       AND x.column_name=v.c AND x.data_type='bigint' AND x.is_nullable='NO'
     WHERE x.column_name IS NULL;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'dealer resale schema column shape mismatch: %', bad;
    END IF;
    SELECT COUNT(*),
           md5(string_agg(
               t.relname || ':' || pc.conname || ':' || pc.contype::text || ':' ||
               replace(replace(
                   regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g'),
                   '"',''
               ),'public.',''),
               '|' ORDER BY t.relname,pc.conname
           ))
      INTO contract_count,contract_digest
      FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
      JOIN pg_namespace n ON n.oid=t.relnamespace
     WHERE n.nspname=current_schema() AND pc.conname=ANY(ARRAY[
        'chk_consumer_refund_execution','chk_consumer_refund_parties','chk_consumer_refund_policy','chk_consumer_refund_status',
        'chk_dealer_consumer_allocation_status','chk_dealer_consumer_allocation_values','chk_dealer_consumer_sale_allocations','chk_dealer_consumer_sale_collector',
        'chk_dealer_consumer_sale_cost_positive','chk_dealer_consumer_sale_markup','chk_dealer_consumer_sale_money','chk_dealer_consumer_sale_paid_fields',
        'chk_dealer_consumer_sale_parties','chk_dealer_consumer_sale_points','chk_dealer_consumer_sale_policy','chk_dealer_consumer_sale_refund_owner',
        'chk_dealer_consumer_sale_state','chk_dealer_consumer_sale_version','chk_dealer_consumer_transfer_cost','chk_dealer_consumer_transfer_direction',
        'chk_dealer_consumer_transfer_sign','chk_dealer_lot_acquisition_cost_positive','chk_dealer_lot_allocation_status','chk_dealer_lot_allocation_values',
        'chk_dealer_lot_cost_conservation','chk_dealer_lot_evidence_object','chk_dealer_lot_id_nonblank','chk_dealer_lot_points_conservation',
        'chk_dealer_lot_points_positive','chk_dealer_lot_pricing_version','chk_dealer_lot_source_kind','chk_dealer_lot_status',
        'chk_dealer_manufacturer_issue_evidence','chk_dealer_manufacturer_issue_values','chk_dealer_profit_cost_positive','chk_dealer_profit_money',
        'chk_dealer_profit_parties','chk_dealer_profit_status','chk_dealer_refund_audit_object','chk_dealer_refund_status',
        'chk_dealer_resale_default_markup','chk_dealer_resale_global_singleton','chk_dealer_resale_order_allocations','chk_dealer_resale_order_collector',
        'chk_dealer_resale_order_cost_positive','chk_dealer_resale_order_markup','chk_dealer_resale_order_money','chk_dealer_resale_order_paid_fields',
        'chk_dealer_resale_order_parties','chk_dealer_resale_order_points','chk_dealer_resale_order_refund_owner','chk_dealer_resale_order_source',
        'chk_dealer_resale_order_source_allocations','chk_dealer_resale_order_state','chk_dealer_resale_order_versions','chk_dealer_resale_policy_bounds',
        'chk_dealer_resale_policy_markup','chk_dealer_resale_policy_row_version','chk_dealer_resale_policy_version','chk_dealer_resale_settings_row_version',
        'chk_dealer_resale_settings_version','chk_dealer_transfer_cost_positive','chk_dealer_transfer_delta_sign','chk_dealer_transfer_direction',
        'chk_dealer_transfer_entry_cost','chk_dealer_transfer_money','chk_dealer_transfer_parties','chk_dealer_transfer_points',
        'chk_dealer_transfer_state','fk_dealer_consumer_transfer_sale','fk_dealer_lot_source_transfer','fk_dealer_order_transfer',
        'ux_consumer_refund_source','ux_dealer_consumer_allocation','ux_dealer_consumer_allocation_seq','ux_dealer_consumer_transfer_side',
        'ux_dealer_lot_allocation','ux_dealer_lot_allocation_seq','ux_dealer_resale_order_quote','ux_dealer_resale_order_transfer',
        'ux_dealer_transfer_entry'
     ]);
    IF contract_count<>81 OR contract_digest<>'f67e1224454d33c8c123f013ae547f04' THEN
        RAISE EXCEPTION 'dealer resale exact constraint contract mismatch';
    END IF;
    SELECT COUNT(*),
           md5(string_agg(
               t.relname || ':' || i.relname || ':' || x.indnkeyatts::text || ':' ||
               replace(replace(
                   regexp_replace(lower(pg_get_indexdef(i.oid)),'\s','','g'),
                   '"',''
               ),'public.','') || ':' ||
               replace(replace(
                   regexp_replace(lower(COALESCE(pg_get_expr(x.indpred,x.indrelid),'')),'\s','','g'),
                   '"',''
               ),'public.',''),
               '|' ORDER BY t.relname,i.relname
           ))
      INTO index_contract_count,index_contract_digest
      FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
      JOIN pg_class t ON t.oid=x.indrelid
      JOIN pg_namespace n ON n.oid=t.relnamespace
     WHERE n.nspname=current_schema() AND i.relname=ANY(ARRAY[
        'ux_dealer_lot_source_order','ux_dealer_resale_external_ref_global'
     ]) AND x.indisunique AND x.indisvalid;
    IF index_contract_count<>2 OR index_contract_digest<>'073798e5e6ebb0c8ebb80fce561d51d3' THEN
        RAISE EXCEPTION 'dealer resale exact index contract mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='dealer_inventory_transfers'
          AND pc.conname='chk_dealer_transfer_points' AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%points>0%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='dealer_inventory_transfers'
          AND pc.conname='chk_dealer_transfer_money' AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%sale_amount_cents=(seller_cost_basis_cents+margin_cents)%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='dealer_resale_orders'
          AND pc.conname='chk_dealer_resale_order_money' AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%sale_amount_cents=(seller_cost_basis_cents+margin_cents)%'
    ) THEN
        RAISE EXCEPTION 'dealer resale critical CHECK definition mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
        JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='dealer_resale_refunds'
          AND i.relname='ux_dealer_resale_external_ref_global'
          AND x.indisunique AND x.indisvalid
          AND x.indnkeyatts=2
          AND regexp_replace(lower(pg_get_indexdef(i.oid)),'\s','','g')
              LIKE '%(lower(btrim(external_channel)),btrim(external_refund_id))%'
          AND regexp_replace(lower(pg_get_expr(x.indpred,x.indrelid)),'\s','','g')
              LIKE '%external_channelisnotnull%'
          AND regexp_replace(lower(pg_get_expr(x.indpred,x.indrelid)),'\s','','g')
              LIKE '%external_refund_idisnotnull%'
    ) THEN
        RAISE EXCEPTION 'dealer external refund proof unique index missing or invalid';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid=g.tgrelid
        JOIN pg_proc p ON p.oid=g.tgfoid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE n.nspname=current_schema() AND t.relname='dealer_resale_orders'
          AND g.tgname='trg_dealer_resale_order_immutable' AND g.tgenabled IN ('O','A')
          AND g.tgtype=19 AND p.proname='dealer_resale_order_immutable_guard'
          AND pn.nspname=current_schema()
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid=g.tgrelid
        JOIN pg_proc p ON p.oid=g.tgfoid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE n.nspname=current_schema() AND t.relname='dealer_consumer_sales'
          AND g.tgname='trg_dealer_consumer_sale_immutable' AND g.tgenabled IN ('O','A')
          AND g.tgtype=19 AND p.proname='dealer_consumer_sale_immutable_guard'
          AND pn.nspname=current_schema()
    ) THEN
        RAISE EXCEPTION 'dealer immutable trigger definition mismatch';
    END IF;
END $$;
