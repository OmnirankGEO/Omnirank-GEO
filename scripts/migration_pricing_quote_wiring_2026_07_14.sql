-- =====================================================================================
-- Pricing quote wiring / immutable snapshot hardening
-- Date   : 2026-07-14
-- Nature : ADDITIVE ONLY · idempotent · legacy rows remain readable.
--
-- This migration deliberately does not enable any pricing flag and does not backfill
-- historical rows with invented catalog/quote truth.  Constraints that cover legacy
-- data are installed NOT VALID: PostgreSQL enforces them for all new/changed rows while
-- pre-existing exceptions remain visible to the readiness gate for explicit cleanup.
-- =====================================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- Source provenance on published entries and a complete immutable quote snapshot.
ALTER TABLE pricing_catalog_entries
    ADD COLUMN IF NOT EXISTS source_ref_jsonb JSONB;

ALTER TABLE price_quotes
    ADD COLUMN IF NOT EXISTS pricing_snapshot_jsonb JSONB;

-- -------------------------------------------------------------------------------------
-- Named checks.  Numeric/code/FK checks use NOT VALID for legacy compatibility; they
-- still reject invalid new writes immediately.  The two new JSON columns can be safely
-- validated because all historical values begin as NULL.
-- -------------------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_catalog_entry_source_ref_object'
           AND conrelid = 'pricing_catalog_entries'::regclass
    ) THEN
        ALTER TABLE pricing_catalog_entries
            ADD CONSTRAINT chk_catalog_entry_source_ref_object
            CHECK (source_ref_jsonb IS NULL OR jsonb_typeof(source_ref_jsonb) = 'object')
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_catalog_entry_money_nonnegative'
           AND conrelid = 'pricing_catalog_entries'::regclass
    ) THEN
        ALTER TABLE pricing_catalog_entries
            ADD CONSTRAINT chk_catalog_entry_money_nonnegative
            CHECK (
                base_price_cents >= 0
                AND multiplier_bps > 0
                AND final_price_cents >= 0
                AND (cost_floor_cents IS NULL OR cost_floor_cents >= 0)
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_price_quote_snapshot_object'
           AND conrelid = 'price_quotes'::regclass
    ) THEN
        ALTER TABLE price_quotes
            ADD CONSTRAINT chk_price_quote_snapshot_object
            CHECK (pricing_snapshot_jsonb IS NULL OR jsonb_typeof(pricing_snapshot_jsonb) = 'object')
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_price_quote_money_nonnegative'
           AND conrelid = 'price_quotes'::regclass
    ) THEN
        ALTER TABLE price_quotes
            ADD CONSTRAINT chk_price_quote_money_nonnegative
            CHECK (
                base_price_cents >= 0
                AND final_price_cents >= 0
                AND (effective_multiplier_bps IS NULL OR effective_multiplier_bps > 0)
                AND (upstream_cost_basis_cents IS NULL OR upstream_cost_basis_cents >= 0)
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_price_quote_snapshot_catalog_id'
           AND conrelid = 'price_quotes'::regclass
    ) THEN
        ALTER TABLE price_quotes
            ADD CONSTRAINT chk_price_quote_snapshot_catalog_id
            CHECK (pricing_snapshot_jsonb IS NULL OR catalog_version_id IS NOT NULL)
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_price_quote_status_fields_consistent'
           AND conrelid = 'price_quotes'::regclass
    ) THEN
        ALTER TABLE price_quotes
            ADD CONSTRAINT chk_price_quote_status_fields_consistent
            CHECK (
                (
                    status = 'consumed'
                    AND used_order_id IS NOT NULL
                    AND consumed_at IS NOT NULL
                )
                OR (
                    status <> 'consumed'
                    AND used_order_id IS NULL
                    AND consumed_at IS NULL
                )
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_channel_relationship_version_nonblank'
           AND conrelid = 'channel_pricing_relationships'::regclass
    ) THEN
        ALTER TABLE channel_pricing_relationships
            ADD CONSTRAINT chk_channel_relationship_version_nonblank
            CHECK (btrim(relationship_version) <> '') NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_channel_relationship_multiplier_floor'
           AND conrelid = 'channel_pricing_relationships'::regclass
    ) THEN
        ALTER TABLE channel_pricing_relationships
            ADD CONSTRAINT chk_channel_relationship_multiplier_floor
            CHECK (cost_multiplier_bps >= 10000) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_channel_revenue_nonnegative'
           AND conrelid = 'channel_revenue_ledger'::regclass
    ) THEN
        ALTER TABLE channel_revenue_ledger
            ADD CONSTRAINT chk_channel_revenue_nonnegative
            CHECK (channel_revenue_cents >= 0) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_public_service_code_format'
           AND conrelid = 'public_account_codes'::regclass
    ) THEN
        ALTER TABLE public_account_codes
            ADD CONSTRAINT chk_public_service_code_format
            CHECK (
                service_account_code IS NULL
                OR service_account_code ~ '^SV-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$'
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_public_channel_code_format'
           AND conrelid = 'public_account_codes'::regclass
    ) THEN
        ALTER TABLE public_account_codes
            ADD CONSTRAINT chk_public_channel_code_format
            CHECK (
                channel_account_code IS NULL
                OR channel_account_code ~ '^CH-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$'
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_recharge_pricing_snapshot_object'
           AND conrelid = 'recharge_orders'::regclass
    ) THEN
        ALTER TABLE recharge_orders
            ADD CONSTRAINT chk_recharge_pricing_snapshot_object
            CHECK (pricing_snapshot_jsonb IS NULL OR jsonb_typeof(pricing_snapshot_jsonb) = 'object')
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_recharge_quote_anchor_complete'
           AND conrelid = 'recharge_orders'::regclass
    ) THEN
        ALTER TABLE recharge_orders
            ADD CONSTRAINT chk_recharge_quote_anchor_complete
            CHECK (
                price_quote_id IS NULL
                OR (
                    pricing_catalog_version IS NOT NULL
                    AND pricing_snapshot_jsonb IS NOT NULL
                )
            ) NOT VALID;
    END IF;
END $$;

-- New JSON columns start NULL for every historical row, so these two checks can become
-- fully validated without inventing data.  Other NOT VALID checks await an explicit
-- governance cleanup and remain observable through pg_constraint.convalidated.
ALTER TABLE pricing_catalog_entries
    VALIDATE CONSTRAINT chk_catalog_entry_source_ref_object;
ALTER TABLE price_quotes
    VALIDATE CONSTRAINT chk_price_quote_snapshot_object;

-- -------------------------------------------------------------------------------------
-- Referential and exactly-once fences.  FKs are NOT VALID to preserve any historical
-- orphan for audit; every new quote/order must reference a real immutable version/quote.
-- -------------------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_price_quotes_catalog_version_id'
           AND conrelid = 'price_quotes'::regclass
    ) THEN
        ALTER TABLE price_quotes
            ADD CONSTRAINT fk_price_quotes_catalog_version_id
            FOREIGN KEY (catalog_version_id)
            REFERENCES pricing_catalog_versions(id)
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_recharge_orders_price_quote_id'
           AND conrelid = 'recharge_orders'::regclass
    ) THEN
        ALTER TABLE recharge_orders
            ADD CONSTRAINT fk_recharge_orders_price_quote_id
            FOREIGN KEY (price_quote_id)
            REFERENCES price_quotes(quote_id)
            ON DELETE RESTRICT
            NOT VALID;
    END IF;


    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_channel_relationship_buyer_user'
           AND conrelid = 'channel_pricing_relationships'::regclass
    ) THEN
        ALTER TABLE channel_pricing_relationships
            ADD CONSTRAINT fk_channel_relationship_buyer_user
            FOREIGN KEY (buyer_dealer_id)
            REFERENCES users(id)
            ON DELETE RESTRICT
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_channel_relationship_upstream_user'
           AND conrelid = 'channel_pricing_relationships'::regclass
    ) THEN
        ALTER TABLE channel_pricing_relationships
            ADD CONSTRAINT fk_channel_relationship_upstream_user
            FOREIGN KEY (upstream_channel_account_id)
            REFERENCES users(id)
            ON DELETE RESTRICT
            NOT VALID;
    END IF;
END $$;

-- Clean/fresh databases leave migration fully validated.  A legacy exception never
-- blocks additive deployment: its constraint remains NOT VALID (but still protects new
-- writes), and readiness reports convalidated=false until governance cleans the row.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pricing_catalog_entries
         WHERE base_price_cents < 0
            OR multiplier_bps <= 0
            OR final_price_cents < 0
            OR (cost_floor_cents IS NOT NULL AND cost_floor_cents < 0)
    ) THEN
        ALTER TABLE pricing_catalog_entries
            VALIDATE CONSTRAINT chk_catalog_entry_money_nonnegative;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM price_quotes
         WHERE base_price_cents < 0
            OR final_price_cents < 0
            OR (effective_multiplier_bps IS NOT NULL AND effective_multiplier_bps <= 0)
            OR (upstream_cost_basis_cents IS NOT NULL AND upstream_cost_basis_cents < 0)
    ) THEN
        ALTER TABLE price_quotes
            VALIDATE CONSTRAINT chk_price_quote_money_nonnegative;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM price_quotes
         WHERE pricing_snapshot_jsonb IS NOT NULL AND catalog_version_id IS NULL
    ) THEN
        ALTER TABLE price_quotes
            VALIDATE CONSTRAINT chk_price_quote_snapshot_catalog_id;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM price_quotes
         WHERE (
                 status = 'consumed'
                 AND (used_order_id IS NULL OR consumed_at IS NULL)
               )
            OR (
                 status <> 'consumed'
                 AND (used_order_id IS NOT NULL OR consumed_at IS NOT NULL)
               )
    ) THEN
        ALTER TABLE price_quotes
            VALIDATE CONSTRAINT chk_price_quote_status_fields_consistent;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM channel_pricing_relationships
         WHERE btrim(relationship_version) = ''
    ) THEN
        ALTER TABLE channel_pricing_relationships
            VALIDATE CONSTRAINT chk_channel_relationship_version_nonblank;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM channel_pricing_relationships
         WHERE cost_multiplier_bps < 10000
    ) THEN
        ALTER TABLE channel_pricing_relationships
            VALIDATE CONSTRAINT chk_channel_relationship_multiplier_floor;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM channel_revenue_ledger
         WHERE channel_revenue_cents < 0
    ) THEN
        ALTER TABLE channel_revenue_ledger
            VALIDATE CONSTRAINT chk_channel_revenue_nonnegative;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public_account_codes
         WHERE service_account_code IS NOT NULL
           AND service_account_code !~ '^SV-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$'
    ) THEN
        ALTER TABLE public_account_codes
            VALIDATE CONSTRAINT chk_public_service_code_format;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public_account_codes
         WHERE channel_account_code IS NOT NULL
           AND channel_account_code !~ '^CH-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{8}$'
    ) THEN
        ALTER TABLE public_account_codes
            VALIDATE CONSTRAINT chk_public_channel_code_format;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM recharge_orders
         WHERE pricing_snapshot_jsonb IS NOT NULL
           AND jsonb_typeof(pricing_snapshot_jsonb) <> 'object'
    ) THEN
        ALTER TABLE recharge_orders
            VALIDATE CONSTRAINT chk_recharge_pricing_snapshot_object;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM recharge_orders
         WHERE price_quote_id IS NOT NULL
           AND (pricing_catalog_version IS NULL OR pricing_snapshot_jsonb IS NULL)
    ) THEN
        ALTER TABLE recharge_orders
            VALIDATE CONSTRAINT chk_recharge_quote_anchor_complete;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM price_quotes q
          LEFT JOIN pricing_catalog_versions v ON v.id = q.catalog_version_id
         WHERE q.catalog_version_id IS NOT NULL AND v.id IS NULL
    ) THEN
        ALTER TABLE price_quotes
            VALIDATE CONSTRAINT fk_price_quotes_catalog_version_id;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM recharge_orders o
          LEFT JOIN price_quotes q ON q.quote_id = o.price_quote_id
         WHERE o.price_quote_id IS NOT NULL AND q.quote_id IS NULL
    ) THEN
        ALTER TABLE recharge_orders
            VALIDATE CONSTRAINT fk_recharge_orders_price_quote_id;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM channel_pricing_relationships r
          LEFT JOIN users u ON u.id = r.buyer_dealer_id
         WHERE u.id IS NULL
    ) THEN
        ALTER TABLE channel_pricing_relationships
            VALIDATE CONSTRAINT fk_channel_relationship_buyer_user;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM channel_pricing_relationships r
          LEFT JOIN users u ON u.id = r.upstream_channel_account_id
         WHERE u.id IS NULL
    ) THEN
        ALTER TABLE channel_pricing_relationships
            VALIDATE CONSTRAINT fk_channel_relationship_upstream_user;
    END IF;
END $$;

-- A relationship version is an OCC token for one buyer.  A persisted quote may only be
-- attached to one order even if application idempotency is accidentally bypassed.  As
-- with NOT VALID constraints, dirty legacy duplicates leave the index absent (readiness
-- false) rather than aborting an otherwise additive deployment.
DO $$
BEGIN
    IF NOT EXISTS (
           SELECT 1
             FROM pg_index x
             JOIN pg_class i ON i.oid = x.indexrelid
            WHERE x.indrelid = 'channel_pricing_relationships'::regclass
              AND i.relname = 'ux_channel_rel_buyer_version'
       )
       AND NOT EXISTS (
           SELECT 1
             FROM channel_pricing_relationships
            GROUP BY buyer_dealer_id, relationship_version
           HAVING COUNT(*) > 1
       ) THEN
        CREATE UNIQUE INDEX ux_channel_rel_buyer_version
            ON channel_pricing_relationships(buyer_dealer_id, relationship_version);
    END IF;

    IF NOT EXISTS (
           SELECT 1
             FROM pg_index x
             JOIN pg_class i ON i.oid = x.indexrelid
            WHERE x.indrelid = 'recharge_orders'::regclass
              AND i.relname = 'ux_recharge_orders_price_quote'
       )
       AND NOT EXISTS (
           SELECT 1
             FROM recharge_orders
            WHERE price_quote_id IS NOT NULL
            GROUP BY price_quote_id
           HAVING COUNT(*) > 1
       ) THEN
        CREATE UNIQUE INDEX ux_recharge_orders_price_quote
            ON recharge_orders(price_quote_id)
            WHERE price_quote_id IS NOT NULL;
    END IF;
END $$;

-- @index-guard idx_catalog_versions_source_fingerprint ON pricing_catalog_versions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_catalog_versions_source_fingerprint' AND i.indrelid = to_regclass('pricing_catalog_versions')) THEN
        NULL;  -- 已在 pricing_catalog_versions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_catalog_versions_source_fingerprint' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_versions'))) THEN
        RAISE EXCEPTION '[index-guard] idx_catalog_versions_source_fingerprint 已存在但不在 pricing_catalog_versions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_catalog_versions_source_fingerprint' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('pricing_catalog_versions')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_catalog_versions_source_fingerprint ON pricing_catalog_versions ( catalog_type, scope_key, ((calc_meta_jsonb ->> 'source_fingerprint')) ) WHERE calc_meta_jsonb ? 'source_fingerprint';
    END IF;
END $idxguard$;

-- -------------------------------------------------------------------------------------
-- Published catalog immutability.
--   * entries may only be inserted/updated/deleted while their version is draft;
--   * a published header may only transition once to archived (the normal new-version
--     publish operation); archived headers cannot be mutated or deleted.
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION pricing_catalog_entry_draft_only_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    target_version_id BIGINT;
    target_status TEXT;
    source_status TEXT;
    locked_version RECORD;
BEGIN
    -- Lock every affected parent in deterministic id order before inspecting status.
    -- The row lock is held until the entry transaction ends, so a concurrent publish
    -- must wait and can never commit between this draft check and the entry commit.
    IF TG_OP = 'UPDATE' THEN
        FOR locked_version IN
            SELECT id
              FROM pricing_catalog_versions
             WHERE id = ANY(ARRAY[OLD.version_id, NEW.version_id]::BIGINT[])
             ORDER BY id
             FOR UPDATE
        LOOP
            NULL;
        END LOOP;
    ELSE
        target_version_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.version_id ELSE NEW.version_id END;
        SELECT id
          INTO target_version_id
          FROM pricing_catalog_versions
         WHERE id = target_version_id
         FOR UPDATE;
    END IF;

    -- An UPDATE must be fenced on both sides.  Checking only NEW.version_id
    -- would let a caller remove an entry from immutable published history by
    -- moving it into a draft version.
    IF TG_OP = 'UPDATE' THEN
        SELECT status INTO source_status
          FROM pricing_catalog_versions
         WHERE id = OLD.version_id;
        IF source_status IS NULL THEN
            RAISE EXCEPTION 'pricing catalog version % does not exist', OLD.version_id
                USING ERRCODE = '23503';
        END IF;
        IF source_status <> 'draft' THEN
            RAISE EXCEPTION 'pricing catalog entries are immutable once version % is %',
                OLD.version_id, source_status
                USING ERRCODE = '55000';
        END IF;
    END IF;

    target_version_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.version_id ELSE NEW.version_id END;
    SELECT status INTO target_status
      FROM pricing_catalog_versions
     WHERE id = target_version_id;

    IF target_status IS NULL THEN
        RAISE EXCEPTION 'pricing catalog version % does not exist', target_version_id
            USING ERRCODE = '23503';
    END IF;
    IF target_status <> 'draft' THEN
        RAISE EXCEPTION 'pricing catalog entries are immutable once version % is %',
            target_version_id, target_status
            USING ERRCODE = '55000';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_pricing_catalog_entry_draft_only'
           AND tgrelid = 'pricing_catalog_entries'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_pricing_catalog_entry_draft_only
        BEFORE INSERT OR UPDATE OR DELETE ON pricing_catalog_entries
        FOR EACH ROW EXECUTE FUNCTION pricing_catalog_entry_draft_only_guard();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION pricing_catalog_version_immutable_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'published/archived pricing catalog version % is immutable', OLD.id
                USING ERRCODE = '55000';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'archived' THEN
        RAISE EXCEPTION 'archived pricing catalog version % is immutable', OLD.id
            USING ERRCODE = '55000';
    END IF;

    IF OLD.status = 'published' THEN
        IF NEW.status <> 'archived'
           OR NEW.effective_to IS NULL
           OR NEW.archived_at IS NULL
           OR (
                to_jsonb(NEW) - 'status' - 'effective_to' - 'archived_at' - 'updated_at'
              ) IS DISTINCT FROM (
                to_jsonb(OLD) - 'status' - 'effective_to' - 'archived_at' - 'updated_at'
              ) THEN
            RAISE EXCEPTION 'published pricing catalog version % only permits archive transition', OLD.id
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_pricing_catalog_version_immutable'
           AND tgrelid = 'pricing_catalog_versions'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_pricing_catalog_version_immutable
        BEFORE UPDATE OR DELETE ON pricing_catalog_versions
        FOR EACH ROW EXECUTE FUNCTION pricing_catalog_version_immutable_guard();
    END IF;
END $$;

-- -------------------------------------------------------------------------------------
-- Quote immutability and terminal state transitions.  Only status/used_order_id/
-- consumed_at are mutable; all pricing, identity, relationship and snapshot fields are
-- insert-time truth.  This also protects future added quote columns by default.
-- -------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION price_quote_immutable_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'price quote % cannot be deleted', OLD.quote_id
            USING ERRCODE = '55000';
    END IF;

    IF (
        to_jsonb(NEW) - 'status' - 'used_order_id' - 'consumed_at'
       ) IS DISTINCT FROM (
        to_jsonb(OLD) - 'status' - 'used_order_id' - 'consumed_at'
       ) THEN
        RAISE EXCEPTION 'price quote % immutable fields cannot be changed', OLD.quote_id
            USING ERRCODE = '55000';
    END IF;

    IF OLD.status <> 'issued' AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'price quote % is terminal in status %', OLD.quote_id, OLD.status
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'issued' AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
        RAISE EXCEPTION 'terminal price quote % cannot be changed', OLD.quote_id
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'issued' AND NEW.status NOT IN ('issued','consumed','expired','cancelled') THEN
        RAISE EXCEPTION 'invalid price quote status transition for %', OLD.quote_id
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status <> 'consumed'
       AND (NEW.used_order_id IS NOT NULL OR NEW.consumed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'non-consumed price quote % cannot carry consumption fields', OLD.quote_id
            USING ERRCODE = '23514';
    END IF;
    IF NEW.status = 'consumed' AND (NEW.used_order_id IS NULL OR NEW.consumed_at IS NULL) THEN
        RAISE EXCEPTION 'consumed price quote % requires used_order_id and consumed_at', OLD.quote_id
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_price_quote_immutable'
           AND tgrelid = 'price_quotes'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_price_quote_immutable
        BEFORE UPDATE OR DELETE ON price_quotes
        FOR EACH ROW EXECUTE FUNCTION price_quote_immutable_guard();
    END IF;
END $$;

-- Orders created before cutover may receive an explicit one-time backfill from NULL.
-- Once any pricing anchor is present it can never be replaced or cleared.
CREATE OR REPLACE FUNCTION recharge_order_pricing_immutable_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.price_quote_id IS NOT NULL
       AND NEW.price_quote_id IS DISTINCT FROM OLD.price_quote_id THEN
        RAISE EXCEPTION 'recharge order % price_quote_id is immutable', OLD.id
            USING ERRCODE = '55000';
    END IF;
    IF OLD.pricing_catalog_version IS NOT NULL
       AND NEW.pricing_catalog_version IS DISTINCT FROM OLD.pricing_catalog_version THEN
        RAISE EXCEPTION 'recharge order % pricing_catalog_version is immutable', OLD.id
            USING ERRCODE = '55000';
    END IF;
    IF OLD.pricing_snapshot_jsonb IS NOT NULL
       AND NEW.pricing_snapshot_jsonb IS DISTINCT FROM OLD.pricing_snapshot_jsonb THEN
        RAISE EXCEPTION 'recharge order % pricing_snapshot_jsonb is immutable', OLD.id
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_recharge_order_pricing_immutable'
           AND tgrelid = 'recharge_orders'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_recharge_order_pricing_immutable
        BEFORE UPDATE OF price_quote_id, pricing_catalog_version, pricing_snapshot_jsonb
        ON recharge_orders
        FOR EACH ROW EXECUTE FUNCTION recharge_order_pricing_immutable_guard();
    END IF;
END $$;

COMMIT;

-- Rollback is intentionally not automated: removing guards/constraints after quote
-- orders exist weakens financial invariants.  A reviewed rollback may remove only the
-- objects introduced here; it must never rewrite or delete catalog/order history.
