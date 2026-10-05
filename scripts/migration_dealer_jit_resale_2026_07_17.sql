-- Dealer JIT resale, promotion attribution, and funds conservation (2026-07-17)
-- Additive/idempotent only. Production owner: ROLE=prestart single-flight.
-- This migration does not enable flags and does not backfill historical orders.

CREATE TABLE IF NOT EXISTS dealer_resale_promotions (
    campaign_id                    TEXT PRIMARY KEY,
    funding_scope                 TEXT NOT NULL,
    sponsor_user_id               INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    seller_user_id                INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    product_code                  TEXT NOT NULL,
    root_catalog_version          TEXT NOT NULL,
    promotion_discount_bps        INTEGER NOT NULL,
    eligibility_jsonb             JSONB NOT NULL DEFAULT '{}'::jsonb,
    status                        TEXT NOT NULL DEFAULT 'draft',
    campaign_version              TEXT NOT NULL,
    row_version                   BIGINT NOT NULL DEFAULT 1,
    started_at                    TIMESTAMPTZ NOT NULL,
    expires_at                    TIMESTAMPTZ NOT NULL,
    created_by                    INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    updated_by                    INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_resale_promo_identity CHECK (
        BTRIM(campaign_id)<>'' AND BTRIM(product_code)<>''
        AND BTRIM(root_catalog_version)<>'' AND BTRIM(campaign_version)<>''
    ),
    CONSTRAINT chk_dealer_resale_promo_scope CHECK (
        (funding_scope='PLATFORM' AND sponsor_user_id IS NULL AND seller_user_id IS NULL)
        OR (funding_scope='SELLER' AND seller_user_id IS NOT NULL
            AND sponsor_user_id=seller_user_id)
    ),
    CONSTRAINT chk_dealer_resale_promo_discount CHECK (
        promotion_discount_bps BETWEEN 1 AND 10000
    ),
    CONSTRAINT chk_dealer_resale_promo_window CHECK (started_at < expires_at),
    CONSTRAINT chk_dealer_resale_promo_status CHECK (
        status IN ('draft','active','ended','archived')
    ),
    CONSTRAINT chk_dealer_resale_promo_version CHECK (row_version >= 1),
    CONSTRAINT chk_dealer_resale_promo_eligibility CHECK (
        jsonb_typeof(eligibility_jsonb)='object'
    )
);

-- @index-guard ux_dealer_resale_active_promotion ON dealer_resale_promotions unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_dealer_resale_active_promotion' AND i.indrelid = to_regclass('public.dealer_resale_promotions')) THEN
        NULL;  -- 已在 public.dealer_resale_promotions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_dealer_resale_active_promotion' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_dealer_resale_active_promotion 已存在但不在 public.dealer_resale_promotions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_dealer_resale_active_promotion' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_dealer_resale_active_promotion ON public.dealer_resale_promotions ( funding_scope, COALESCE(seller_user_id,0), product_code, root_catalog_version ) WHERE status='active';
    END IF;
END $idxguard$;
-- @index-guard idx_dealer_resale_promotion_window ON dealer_resale_promotions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_resale_promotion_window' AND i.indrelid = to_regclass('public.dealer_resale_promotions')) THEN
        NULL;  -- 已在 public.dealer_resale_promotions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_resale_promotion_window' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_resale_promotion_window 已存在但不在 public.dealer_resale_promotions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_resale_promotion_window' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_resale_promotion_window ON public.dealer_resale_promotions (status, started_at, expires_at);
    END IF;
END $idxguard$;

ALTER TABLE dealer_inventory_lots
    ADD COLUMN IF NOT EXISTS root_order_id TEXT REFERENCES recharge_orders(id) ON DELETE RESTRICT;
ALTER TABLE dealer_inventory_lots
    ADD COLUMN IF NOT EXISTS source_hop_seq INTEGER;
ALTER TABLE dealer_inventory_lots
    ADD COLUMN IF NOT EXISTS standard_reference_cents BIGINT;
ALTER TABLE dealer_inventory_lots
    ADD COLUMN IF NOT EXISTS promotion_campaign_id TEXT REFERENCES dealer_resale_promotions(campaign_id) ON DELETE RESTRICT;
ALTER TABLE dealer_inventory_lots
    ADD COLUMN IF NOT EXISTS promotion_funding_scope TEXT;
ALTER TABLE dealer_inventory_lots
    ADD COLUMN IF NOT EXISTS promotion_snapshot_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE dealer_inventory_lots
    ADD COLUMN IF NOT EXISTS promotion_product_code TEXT;
ALTER TABLE dealer_inventory_lots
    ADD COLUMN IF NOT EXISTS promotion_root_catalog_version TEXT;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_lot_jit_lineage') THEN
        ALTER TABLE dealer_inventory_lots ADD CONSTRAINT chk_dealer_lot_jit_lineage CHECK (
            (root_order_id IS NULL AND source_hop_seq IS NULL)
            OR (root_order_id IS NOT NULL AND source_hop_seq IS NOT NULL AND source_hop_seq >= 0)
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_lot_standard_reference') THEN
        ALTER TABLE dealer_inventory_lots ADD CONSTRAINT chk_dealer_lot_standard_reference CHECK (
            standard_reference_cents IS NULL OR standard_reference_cents > 0
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_lot_promotion_scope') THEN
        ALTER TABLE dealer_inventory_lots ADD CONSTRAINT chk_dealer_lot_promotion_scope CHECK (
            (promotion_campaign_id IS NULL AND promotion_funding_scope IS NULL)
            OR (promotion_campaign_id IS NOT NULL AND promotion_funding_scope IN ('PLATFORM','SELLER'))
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_lot_promotion_snapshot') THEN
        ALTER TABLE dealer_inventory_lots ADD CONSTRAINT chk_dealer_lot_promotion_snapshot CHECK (
            jsonb_typeof(promotion_snapshot_jsonb)='object'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_lot_promotion_lineage_v2') THEN
        ALTER TABLE dealer_inventory_lots ADD CONSTRAINT chk_dealer_lot_promotion_lineage_v2 CHECK (
            (
                promotion_campaign_id IS NULL
                AND promotion_funding_scope IS NULL
                AND promotion_product_code IS NULL
                AND promotion_root_catalog_version IS NULL
            )
            OR (
                promotion_campaign_id IS NOT NULL
                AND promotion_funding_scope IN ('PLATFORM','SELLER')
                AND promotion_product_code IS NOT NULL
                AND promotion_root_catalog_version IS NOT NULL
                AND BTRIM(promotion_product_code)<>''
                AND BTRIM(promotion_root_catalog_version)<>''
                AND promotion_root_catalog_version=pricing_version
            )
        );
    END IF;
END $$;

-- @index-guard ux_dealer_lot_root_hop ON dealer_inventory_lots unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_dealer_lot_root_hop' AND i.indrelid = to_regclass('public.dealer_inventory_lots')) THEN
        NULL;  -- 已在 public.dealer_inventory_lots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_dealer_lot_root_hop' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_dealer_lot_root_hop 已存在但不在 public.dealer_inventory_lots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_dealer_lot_root_hop' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_dealer_lot_root_hop ON public.dealer_inventory_lots (root_order_id, source_hop_seq) WHERE root_order_id IS NOT NULL AND source_hop_seq IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard idx_dealer_lot_promotion_fifo_v2 ON dealer_inventory_lots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_lot_promotion_fifo_v2' AND i.indrelid = to_regclass('public.dealer_inventory_lots')) THEN
        NULL;  -- 已在 public.dealer_inventory_lots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_lot_promotion_fifo_v2' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_lot_promotion_fifo_v2 已存在但不在 public.dealer_inventory_lots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_lot_promotion_fifo_v2' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_lot_promotion_fifo_v2 ON public.dealer_inventory_lots ( owner_agent_user_id, promotion_campaign_id, promotion_product_code, promotion_root_catalog_version, acquired_at, lot_id ) WHERE status='active' AND remaining_points>0;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS dealer_resale_fulfillment_plans (
    plan_id                        TEXT PRIMARY KEY,
    root_order_id                  TEXT NOT NULL UNIQUE REFERENCES recharge_orders(id) ON DELETE RESTRICT,
    quote_id                       TEXT NOT NULL UNIQUE REFERENCES price_quotes(quote_id) ON DELETE RESTRICT,
    order_kind                     TEXT NOT NULL,
    final_seller_user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    final_buyer_user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    points                         BIGINT NOT NULL,
    product_code                   TEXT NOT NULL,
    root_catalog_version           TEXT NOT NULL,
    standard_reference_cents       BIGINT NOT NULL,
    final_sale_amount_cents        BIGINT NOT NULL,
    promotion_snapshot_jsonb       JSONB NOT NULL DEFAULT '{}'::jsonb,
    immutable_snapshot_jsonb       JSONB NOT NULL,
    funds_conservation_jsonb       JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key                TEXT NOT NULL UNIQUE,
    plan_version                   INTEGER NOT NULL DEFAULT 2,
    state                          TEXT NOT NULL DEFAULT 'reserved',
    reserved_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at                     TIMESTAMPTZ,
    cancelled_at                   TIMESTAMPTZ,
    manual_review_reason           TEXT,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_fulfillment_plan_identity CHECK (
        BTRIM(plan_id)<>'' AND BTRIM(product_code)<>''
        AND BTRIM(root_catalog_version)<>'' AND BTRIM(idempotency_key)<>''
    ),
    CONSTRAINT chk_dealer_fulfillment_plan_parties CHECK (
        final_seller_user_id<>final_buyer_user_id
    ),
    CONSTRAINT chk_dealer_fulfillment_plan_values CHECK (
        points>0 AND standard_reference_cents>0 AND final_sale_amount_cents>0
        AND plan_version=2
    ),
    CONSTRAINT chk_dealer_fulfillment_plan_kind CHECK (
        order_kind IN ('B2B','CONSUMER')
    ),
    CONSTRAINT chk_dealer_fulfillment_plan_state CHECK (
        state IN ('reserved','settling','settled','cancelled','manual_review')
    ),
    CONSTRAINT chk_dealer_fulfillment_plan_json CHECK (
        jsonb_typeof(promotion_snapshot_jsonb)='object'
        AND jsonb_typeof(immutable_snapshot_jsonb)='object'
        AND jsonb_typeof(funds_conservation_jsonb)='object'
    )
);

CREATE TABLE IF NOT EXISTS dealer_resale_fulfillment_hops (
    plan_id                        TEXT NOT NULL REFERENCES dealer_resale_fulfillment_plans(plan_id) ON DELETE RESTRICT,
    root_order_id                  TEXT NOT NULL REFERENCES recharge_orders(id) ON DELETE RESTRICT,
    hop_seq                        INTEGER NOT NULL,
    seller_user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    buyer_user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    points                         BIGINT NOT NULL,
    existing_inventory_points      BIGINT NOT NULL,
    jit_shortfall_points           BIGINT NOT NULL,
    standard_reference_cents       BIGINT NOT NULL,
    seller_cost_basis_cents        BIGINT NOT NULL,
    principal_recovery_cents       BIGINT NOT NULL,
    agent_payable_cents            BIGINT NOT NULL,
    sale_amount_cents              BIGINT NOT NULL,
    margin_cents                   BIGINT NOT NULL,
    edge_multiplier_bps            INTEGER NOT NULL,
    relationship_version           TEXT,
    source_kind                    TEXT NOT NULL,
    promotion_campaign_id          TEXT REFERENCES dealer_resale_promotions(campaign_id) ON DELETE RESTRICT,
    promotion_discount_bps         INTEGER NOT NULL DEFAULT 10000,
    promotion_funding_scope        TEXT,
    promotion_sponsor_user_id      INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    promotion_snapshot_jsonb       JSONB NOT NULL DEFAULT '{}'::jsonb,
    state                          TEXT NOT NULL DEFAULT 'reserved',
    transfer_id                    TEXT UNIQUE,
    acquired_lot_id                TEXT REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    idempotency_key                TEXT NOT NULL UNIQUE,
    settled_at                     TIMESTAMPTZ,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (plan_id, hop_seq),
    CONSTRAINT ux_dealer_fulfillment_root_hop UNIQUE (root_order_id, hop_seq),
    CONSTRAINT chk_dealer_fulfillment_hop_parties CHECK (seller_user_id<>buyer_user_id),
    CONSTRAINT chk_dealer_fulfillment_hop_values CHECK (
        hop_seq>=0 AND points>0 AND existing_inventory_points>=0
        AND jit_shortfall_points>=0
        AND existing_inventory_points+jit_shortfall_points=points
        AND standard_reference_cents>0
    ),
    CONSTRAINT chk_dealer_fulfillment_hop_money CHECK (
        seller_cost_basis_cents>0 AND sale_amount_cents>0 AND margin_cents>=0
        AND sale_amount_cents=seller_cost_basis_cents+margin_cents
    ),
    CONSTRAINT chk_dealer_fulfillment_hop_payout_v2 CHECK (
        principal_recovery_cents>=0
        AND principal_recovery_cents<=seller_cost_basis_cents
        AND (
            (source_kind='platform_root'
             AND principal_recovery_cents=0 AND agent_payable_cents=0)
            OR
            (source_kind='dealer_resale'
             AND agent_payable_cents=principal_recovery_cents+margin_cents)
        )
    ),
    CONSTRAINT chk_dealer_fulfillment_hop_edge CHECK (edge_multiplier_bps>=10000),
    CONSTRAINT chk_dealer_fulfillment_hop_source CHECK (
        source_kind IN ('platform_root','dealer_resale')
    ),
    CONSTRAINT chk_dealer_fulfillment_hop_promotion CHECK (
        promotion_discount_bps BETWEEN 1 AND 10000
        AND (
            (promotion_campaign_id IS NULL AND promotion_funding_scope IS NULL
             AND promotion_sponsor_user_id IS NULL AND promotion_discount_bps=10000)
            OR (promotion_campaign_id IS NOT NULL
                AND promotion_funding_scope IN ('PLATFORM','SELLER'))
        )
        AND jsonb_typeof(promotion_snapshot_jsonb)='object'
    ),
    CONSTRAINT chk_dealer_fulfillment_hop_state CHECK (
        state IN ('reserved','settled','cancelled','manual_review')
    )
);

-- @index-guard idx_dealer_fulfillment_hop_seller ON dealer_resale_fulfillment_hops plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_fulfillment_hop_seller' AND i.indrelid = to_regclass('public.dealer_resale_fulfillment_hops')) THEN
        NULL;  -- 已在 public.dealer_resale_fulfillment_hops 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_fulfillment_hop_seller' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_fulfillment_hop_seller 已存在但不在 public.dealer_resale_fulfillment_hops 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_fulfillment_hop_seller' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_fulfillment_hop_seller ON public.dealer_resale_fulfillment_hops (seller_user_id, created_at DESC);
    END IF;
END $idxguard$;
-- @index-guard idx_dealer_fulfillment_hop_buyer ON dealer_resale_fulfillment_hops plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_fulfillment_hop_buyer' AND i.indrelid = to_regclass('public.dealer_resale_fulfillment_hops')) THEN
        NULL;  -- 已在 public.dealer_resale_fulfillment_hops 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_fulfillment_hop_buyer' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_fulfillment_hop_buyer 已存在但不在 public.dealer_resale_fulfillment_hops 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_fulfillment_hop_buyer' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_fulfillment_hop_buyer ON public.dealer_resale_fulfillment_hops (buyer_user_id, created_at DESC);
    END IF;
END $idxguard$;

ALTER TABLE dealer_resale_fulfillment_hops
    ADD COLUMN IF NOT EXISTS principal_recovery_cents BIGINT;
ALTER TABLE dealer_resale_fulfillment_hops
    ADD COLUMN IF NOT EXISTS agent_payable_cents BIGINT;
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM dealer_resale_fulfillment_hops
        WHERE principal_recovery_cents IS NULL OR agent_payable_cents IS NULL
    ) THEN
        RAISE EXCEPTION
            'existing fulfillment hops lack immutable principal/payable evidence; manual disposition required';
    END IF;
    ALTER TABLE dealer_resale_fulfillment_hops
        ALTER COLUMN principal_recovery_cents SET NOT NULL,
        ALTER COLUMN agent_payable_cents SET NOT NULL;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='chk_dealer_fulfillment_hop_payout_v2'
    ) THEN
        ALTER TABLE dealer_resale_fulfillment_hops
            ADD CONSTRAINT chk_dealer_fulfillment_hop_payout_v2 CHECK (
                principal_recovery_cents IS NOT NULL
                AND agent_payable_cents IS NOT NULL
                AND principal_recovery_cents>=0
                AND principal_recovery_cents<=seller_cost_basis_cents
                AND (
                    (source_kind='platform_root'
                     AND principal_recovery_cents=0 AND agent_payable_cents=0)
                    OR
                    (source_kind='dealer_resale'
                     AND agent_payable_cents=principal_recovery_cents+margin_cents)
                )
            );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS dealer_resale_fulfillment_allocations (
    allocation_id                 BIGSERIAL PRIMARY KEY,
    plan_id                        TEXT NOT NULL,
    hop_seq                        INTEGER NOT NULL,
    allocation_seq                 INTEGER NOT NULL,
    allocation_kind                TEXT NOT NULL,
    source_lot_id                  TEXT REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    source_hop_seq                 INTEGER,
    points                         BIGINT NOT NULL,
    cost_basis_cents               BIGINT NOT NULL,
    promotion_campaign_id          TEXT REFERENCES dealer_resale_promotions(campaign_id) ON DELETE RESTRICT,
    status                          TEXT NOT NULL DEFAULT 'reserved',
    consumed_at                     TIMESTAMPTZ,
    restored_at                     TIMESTAMPTZ,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_dealer_fulfillment_allocation_hop
        FOREIGN KEY (plan_id,hop_seq)
        REFERENCES dealer_resale_fulfillment_hops(plan_id,hop_seq) ON DELETE RESTRICT,
    CONSTRAINT ux_dealer_fulfillment_allocation_seq
        UNIQUE (plan_id,hop_seq,allocation_seq),
    CONSTRAINT chk_dealer_fulfillment_allocation_values CHECK (
        allocation_seq>=0 AND points>0 AND cost_basis_cents>0
    ),
    CONSTRAINT chk_dealer_fulfillment_allocation_kind CHECK (
        (allocation_kind='existing_lot' AND source_lot_id IS NOT NULL AND source_hop_seq IS NULL)
        OR (allocation_kind='jit_incoming' AND source_lot_id IS NULL AND source_hop_seq IS NOT NULL)
    ),
    CONSTRAINT chk_dealer_fulfillment_allocation_state CHECK (
        status IN ('reserved','consumed','cancelled','restored')
    )
);

-- @index-guard idx_dealer_fulfillment_allocation_lot ON dealer_resale_fulfillment_allocations plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_fulfillment_allocation_lot' AND i.indrelid = to_regclass('public.dealer_resale_fulfillment_allocations')) THEN
        NULL;  -- 已在 public.dealer_resale_fulfillment_allocations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_fulfillment_allocation_lot' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_fulfillment_allocation_lot 已存在但不在 public.dealer_resale_fulfillment_allocations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_fulfillment_allocation_lot' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_fulfillment_allocation_lot ON public.dealer_resale_fulfillment_allocations (source_lot_id) WHERE source_lot_id IS NOT NULL;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS dealer_resale_hop_transfer_entries (
    entry_id                        BIGSERIAL PRIMARY KEY,
    plan_id                         TEXT NOT NULL,
    root_order_id                   TEXT NOT NULL REFERENCES recharge_orders(id) ON DELETE RESTRICT,
    hop_seq                         INTEGER NOT NULL,
    entry_seq                       INTEGER NOT NULL,
    transfer_id                     TEXT NOT NULL,
    owner_agent_user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    counterparty_user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    direction                       TEXT NOT NULL,
    points_delta                    BIGINT NOT NULL,
    lot_id                          TEXT REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    cost_basis_cents                BIGINT NOT NULL,
    balance_paid_after              BIGINT NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_dealer_hop_transfer_hop
        FOREIGN KEY (plan_id,hop_seq)
        REFERENCES dealer_resale_fulfillment_hops(plan_id,hop_seq) ON DELETE RESTRICT,
    CONSTRAINT ux_dealer_hop_transfer_entry UNIQUE (plan_id,hop_seq,entry_seq),
    CONSTRAINT ux_dealer_hop_transfer_direction UNIQUE (plan_id,hop_seq,direction),
    CONSTRAINT chk_dealer_hop_transfer_parties CHECK (owner_agent_user_id<>counterparty_user_id),
    CONSTRAINT chk_dealer_hop_transfer_direction CHECK (direction IN ('transfer_out','transfer_in')),
    CONSTRAINT chk_dealer_hop_transfer_sign CHECK (
        (direction='transfer_out' AND points_delta<0)
        OR (direction='transfer_in' AND points_delta>0)
    ),
    CONSTRAINT chk_dealer_hop_transfer_values CHECK (
        cost_basis_cents>=0 AND balance_paid_after>=0 AND BTRIM(transfer_id)<>''
    )
);

CREATE TABLE IF NOT EXISTS dealer_resale_hop_profit_ledger (
    profit_id                       BIGSERIAL PRIMARY KEY,
    plan_id                         TEXT NOT NULL,
    root_order_id                   TEXT NOT NULL REFERENCES recharge_orders(id) ON DELETE RESTRICT,
    hop_seq                         INTEGER NOT NULL,
    seller_user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    buyer_user_id                   INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    seller_cost_basis_cents         BIGINT NOT NULL,
    sale_amount_cents               BIGINT NOT NULL,
    principal_recovery_cents        BIGINT NOT NULL,
    margin_cents                    BIGINT NOT NULL,
    platform_root_revenue_cents     BIGINT NOT NULL DEFAULT 0,
    agent_payable_cents              BIGINT NOT NULL,
    status                          TEXT NOT NULL DEFAULT 'pending',
    available_at                    TIMESTAMPTZ NOT NULL,
    available_since                 TIMESTAMPTZ,
    reversed_at                     TIMESTAMPTZ,
    revenue_ledger_id               BIGINT UNIQUE REFERENCES agent_revenue_ledger(id) ON DELETE RESTRICT,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_dealer_hop_profit_hop
        FOREIGN KEY (plan_id,hop_seq)
        REFERENCES dealer_resale_fulfillment_hops(plan_id,hop_seq) ON DELETE RESTRICT,
    CONSTRAINT ux_dealer_hop_profit UNIQUE (plan_id,hop_seq),
    CONSTRAINT chk_dealer_hop_profit_parties CHECK (seller_user_id<>buyer_user_id),
    CONSTRAINT chk_dealer_hop_profit_money CHECK (
        seller_cost_basis_cents>0 AND sale_amount_cents>0
        AND principal_recovery_cents>=0 AND margin_cents>=0
        AND platform_root_revenue_cents>=0
        AND sale_amount_cents=seller_cost_basis_cents+margin_cents
        AND principal_recovery_cents<=seller_cost_basis_cents
        AND platform_root_revenue_cents<=sale_amount_cents
    ),
    CONSTRAINT chk_dealer_hop_profit_status CHECK (
        status IN ('pending','available','reversed')
    ),
    CONSTRAINT chk_dealer_hop_profit_payout_v2 CHECK (
        (
            platform_root_revenue_cents=sale_amount_cents
            AND principal_recovery_cents=0
            AND agent_payable_cents=0
        )
        OR (
            platform_root_revenue_cents=0
            AND agent_payable_cents=principal_recovery_cents+margin_cents
        )
    )
);

ALTER TABLE dealer_resale_hop_profit_ledger
    ADD COLUMN IF NOT EXISTS available_since TIMESTAMPTZ;
ALTER TABLE dealer_resale_hop_profit_ledger
    ADD COLUMN IF NOT EXISTS revenue_ledger_id BIGINT UNIQUE
    REFERENCES agent_revenue_ledger(id) ON DELETE RESTRICT;
ALTER TABLE dealer_resale_hop_profit_ledger
    ADD COLUMN IF NOT EXISTS agent_payable_cents BIGINT;
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM dealer_resale_hop_profit_ledger
        WHERE agent_payable_cents IS NULL
    ) THEN
        RAISE EXCEPTION
            'existing hop profit rows lack immutable agent payable evidence; manual disposition required';
    END IF;
    ALTER TABLE dealer_resale_hop_profit_ledger
        ALTER COLUMN agent_payable_cents SET NOT NULL;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_hop_profit_payout_v2'
    ) THEN
        ALTER TABLE dealer_resale_hop_profit_ledger
            ADD CONSTRAINT chk_dealer_hop_profit_payout_v2 CHECK (
                agent_payable_cents IS NOT NULL
                AND (
                    (
                        platform_root_revenue_cents=sale_amount_cents
                        AND principal_recovery_cents=0
                        AND agent_payable_cents=0
                    )
                    OR (
                        platform_root_revenue_cents=0
                        AND agent_payable_cents=principal_recovery_cents+margin_cents
                    )
                )
            );
    END IF;
END $$;

ALTER TABLE dealer_consumer_sales
    ADD COLUMN IF NOT EXISTS principal_recovery_cents BIGINT;
ALTER TABLE dealer_consumer_sales
    ADD COLUMN IF NOT EXISTS agent_payable_cents BIGINT;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_consumer_sale_payout_v2'
    ) THEN
        ALTER TABLE dealer_consumer_sales
            ADD CONSTRAINT chk_dealer_consumer_sale_payout_v2 CHECK (
                (
                    principal_recovery_cents IS NULL
                    AND agent_payable_cents IS NULL
                )
                OR (
                    principal_recovery_cents>=0
                    AND principal_recovery_cents<=seller_cost_basis_cents
                    AND agent_payable_cents=principal_recovery_cents+margin_cents
                    AND agent_payable_cents<=sale_amount_cents
                )
            );
    END IF;
END $$;

-- @index-guard idx_dealer_hop_profit_maturity ON dealer_resale_hop_profit_ledger plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_hop_profit_maturity' AND i.indrelid = to_regclass('public.dealer_resale_hop_profit_ledger')) THEN
        NULL;  -- 已在 public.dealer_resale_hop_profit_ledger 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_hop_profit_maturity' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_hop_profit_maturity 已存在但不在 public.dealer_resale_hop_profit_ledger 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_hop_profit_maturity' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_hop_profit_maturity ON public.dealer_resale_hop_profit_ledger (status,available_at,profit_id);
    END IF;
END $idxguard$;
-- @index-guard idx_dealer_hop_profit_seller ON dealer_resale_hop_profit_ledger plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_dealer_hop_profit_seller' AND i.indrelid = to_regclass('public.dealer_resale_hop_profit_ledger')) THEN
        NULL;  -- 已在 public.dealer_resale_hop_profit_ledger 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_dealer_hop_profit_seller' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_dealer_hop_profit_seller 已存在但不在 public.dealer_resale_hop_profit_ledger 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_dealer_hop_profit_seller' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_dealer_hop_profit_seller ON public.dealer_resale_hop_profit_ledger (seller_user_id,created_at DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS dealer_consumer_jit_refund_lots (
    case_id              TEXT PRIMARY KEY REFERENCES consumer_refund_cases(case_id) ON DELETE RESTRICT,
    source_order_id      TEXT NOT NULL UNIQUE REFERENCES dealer_consumer_sales(order_id) ON DELETE RESTRICT,
    seller_user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    restored_lot_id      TEXT NOT NULL UNIQUE REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    points               BIGINT NOT NULL,
    cost_basis_cents     BIGINT NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_dealer_consumer_jit_refund_values CHECK (
        points>0 AND cost_basis_cents>0
    )
);

CREATE OR REPLACE FUNCTION dealer_resale_fulfillment_plan_immutable_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(
        NEW.plan_id,NEW.root_order_id,NEW.quote_id,NEW.order_kind,
        NEW.final_seller_user_id,NEW.final_buyer_user_id,NEW.points,
        NEW.product_code,NEW.root_catalog_version,NEW.standard_reference_cents,
        NEW.final_sale_amount_cents,NEW.promotion_snapshot_jsonb,
        NEW.immutable_snapshot_jsonb,NEW.funds_conservation_jsonb,
        NEW.idempotency_key,NEW.plan_version
    ) IS DISTINCT FROM ROW(
        OLD.plan_id,OLD.root_order_id,OLD.quote_id,OLD.order_kind,
        OLD.final_seller_user_id,OLD.final_buyer_user_id,OLD.points,
        OLD.product_code,OLD.root_catalog_version,OLD.standard_reference_cents,
        OLD.final_sale_amount_cents,OLD.promotion_snapshot_jsonb,
        OLD.immutable_snapshot_jsonb,OLD.funds_conservation_jsonb,
        OLD.idempotency_key,OLD.plan_version
    ) THEN
        RAISE EXCEPTION 'dealer resale fulfillment plan immutable snapshot cannot change';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION dealer_resale_fulfillment_hop_immutable_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(
        NEW.plan_id,NEW.root_order_id,NEW.hop_seq,NEW.seller_user_id,
        NEW.buyer_user_id,NEW.points,NEW.existing_inventory_points,
        NEW.jit_shortfall_points,NEW.standard_reference_cents,
        NEW.seller_cost_basis_cents,NEW.principal_recovery_cents,
        NEW.agent_payable_cents,NEW.sale_amount_cents,NEW.margin_cents,
        NEW.edge_multiplier_bps,NEW.relationship_version,NEW.source_kind,
        NEW.promotion_campaign_id,NEW.promotion_discount_bps,
        NEW.promotion_funding_scope,NEW.promotion_sponsor_user_id,
        NEW.promotion_snapshot_jsonb,NEW.idempotency_key
    ) IS DISTINCT FROM ROW(
        OLD.plan_id,OLD.root_order_id,OLD.hop_seq,OLD.seller_user_id,
        OLD.buyer_user_id,OLD.points,OLD.existing_inventory_points,
        OLD.jit_shortfall_points,OLD.standard_reference_cents,
        OLD.seller_cost_basis_cents,OLD.principal_recovery_cents,
        OLD.agent_payable_cents,OLD.sale_amount_cents,OLD.margin_cents,
        OLD.edge_multiplier_bps,OLD.relationship_version,OLD.source_kind,
        OLD.promotion_campaign_id,OLD.promotion_discount_bps,
        OLD.promotion_funding_scope,OLD.promotion_sponsor_user_id,
        OLD.promotion_snapshot_jsonb,OLD.idempotency_key
    ) THEN
        RAISE EXCEPTION 'dealer resale fulfillment hop immutable snapshot cannot change';
    END IF;
    RETURN NEW;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname='trg_dealer_fulfillment_plan_immutable'
          AND tgrelid='dealer_resale_fulfillment_plans'::regclass
    ) THEN
        CREATE TRIGGER trg_dealer_fulfillment_plan_immutable
        BEFORE UPDATE ON dealer_resale_fulfillment_plans
        FOR EACH ROW EXECUTE FUNCTION dealer_resale_fulfillment_plan_immutable_guard();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname='trg_dealer_fulfillment_hop_immutable'
          AND tgrelid='dealer_resale_fulfillment_hops'::regclass
    ) THEN
        CREATE TRIGGER trg_dealer_fulfillment_hop_immutable
        BEFORE UPDATE ON dealer_resale_fulfillment_hops
        FOR EACH ROW EXECUTE FUNCTION dealer_resale_fulfillment_hop_immutable_guard();
    END IF;
END $$;
