-- Direct-service consumer refund + agreement evidence (2026-07-15)
-- Additive/idempotent only. DDL owner: ROLE=prestart single-flight migration manifest.

-- Widen the deployed named source CHECK before direct-service refund ledgers write.
ALTER TABLE customer_credit_transactions
    DROP CONSTRAINT IF EXISTS customer_credit_transactions_source_check;
ALTER TABLE customer_credit_transactions
    ADD CONSTRAINT customer_credit_transactions_source_check CHECK (source IN (
        'online_payment', 'offline_allocation', 'admin_adjust', 'tool_consume',
        'refund_revoke', 'agent_rebate', 'tool_fail_refund',
        'diagnosis_delivery_refund', 'direct_service_refund'
    ));

ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS reason_category TEXT NOT NULL DEFAULT 'negotiated_other';
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS decision_kind TEXT NOT NULL DEFAULT 'negotiated';
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS eligibility_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS internal_settled_at TIMESTAMPTZ;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS cash_status TEXT NOT NULL DEFAULT 'not_started';
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS refund_amount_cents BIGINT NOT NULL DEFAULT 0;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS revoked_paid_points BIGINT NOT NULL DEFAULT 0;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS revoked_bonus_points BIGINT NOT NULL DEFAULT 0;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS restored_inventory_points BIGINT NOT NULL DEFAULT 0;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS revenue_reversed_cents BIGINT NOT NULL DEFAULT 0;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS row_version BIGINT NOT NULL DEFAULT 1;
ALTER TABLE consumer_refund_cases
    ADD COLUMN IF NOT EXISTS mandatory_evidence_status TEXT NOT NULL DEFAULT 'not_applicable';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_consumer_refund_decision_kind') THEN
        ALTER TABLE consumer_refund_cases ADD CONSTRAINT chk_consumer_refund_decision_kind
            CHECK (decision_kind IN ('mandatory','negotiated'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_consumer_refund_cash_status') THEN
        ALTER TABLE consumer_refund_cases ADD CONSTRAINT chk_consumer_refund_cash_status
            CHECK (cash_status IN ('not_started','queued','provider_processing','completed','failed','manual_review'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_consumer_refund_amounts') THEN
        ALTER TABLE consumer_refund_cases ADD CONSTRAINT chk_consumer_refund_amounts CHECK (
            refund_amount_cents >= 0 AND revoked_paid_points >= 0 AND revoked_bonus_points >= 0
            AND restored_inventory_points >= 0 AND revenue_reversed_cents >= 0 AND row_version >= 1
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_consumer_refund_eligibility_object') THEN
        ALTER TABLE consumer_refund_cases ADD CONSTRAINT chk_consumer_refund_eligibility_object
            CHECK (jsonb_typeof(eligibility_jsonb)='object');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_consumer_refund_mandatory_evidence') THEN
        ALTER TABLE consumer_refund_cases ADD CONSTRAINT chk_consumer_refund_mandatory_evidence
            CHECK (mandatory_evidence_status IN ('not_applicable','claimed','verified','rejected'));
    END IF;
END $$;

ALTER TABLE dealer_consumer_lot_allocations
    ADD COLUMN IF NOT EXISTS refunded_points BIGINT NOT NULL DEFAULT 0;
ALTER TABLE dealer_consumer_lot_allocations
    ADD COLUMN IF NOT EXISTS refunded_cost_cents BIGINT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_consumer_allocation_refunded') THEN
        ALTER TABLE dealer_consumer_lot_allocations
            ADD CONSTRAINT chk_dealer_consumer_allocation_refunded CHECK (
                refunded_points >= 0 AND refunded_points <= points
                AND refunded_cost_cents >= 0 AND refunded_cost_cents <= cost_basis_cents
            );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS consumer_refund_lot_restorations (
    restoration_id              BIGSERIAL PRIMARY KEY,
    case_id                     TEXT NOT NULL REFERENCES consumer_refund_cases(case_id) ON DELETE RESTRICT,
    source_order_id             TEXT NOT NULL REFERENCES dealer_consumer_sales(order_id) ON DELETE RESTRICT,
    seller_lot_id               TEXT NOT NULL REFERENCES dealer_inventory_lots(lot_id) ON DELETE RESTRICT,
    allocation_id               BIGINT NOT NULL REFERENCES dealer_consumer_lot_allocations(allocation_id) ON DELETE RESTRICT,
    points                      BIGINT NOT NULL,
    cost_basis_cents            BIGINT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_consumer_refund_lot_restoration UNIQUE (case_id, allocation_id),
    CONSTRAINT chk_consumer_refund_lot_values CHECK (points > 0 AND cost_basis_cents >= 0)
);

CREATE TABLE IF NOT EXISTS service_refund_reserve_accounts (
    service_user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
    available_cents             BIGINT NOT NULL DEFAULT 0,
    row_version                 BIGINT NOT NULL DEFAULT 1,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_service_refund_reserve_values CHECK (available_cents >= 0 AND row_version >= 1)
);

CREATE TABLE IF NOT EXISTS service_refund_liability_ledger (
    liability_id                BIGSERIAL PRIMARY KEY,
    case_id                     TEXT NOT NULL UNIQUE REFERENCES consumer_refund_cases(case_id) ON DELETE RESTRICT,
    source_order_id             TEXT NOT NULL UNIQUE REFERENCES dealer_consumer_sales(order_id) ON DELETE RESTRICT,
    service_user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    refund_amount_cents         BIGINT NOT NULL,
    pending_settlement_offset_cents BIGINT NOT NULL DEFAULT 0,
    reserve_offset_cents        BIGINT NOT NULL DEFAULT 0,
    negative_settlement_cents   BIGINT NOT NULL DEFAULT 0,
    status                      TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_service_refund_liability_money CHECK (
        refund_amount_cents > 0 AND pending_settlement_offset_cents >= 0
        AND reserve_offset_cents >= 0 AND negative_settlement_cents >= 0
        AND refund_amount_cents = pending_settlement_offset_cents
            + reserve_offset_cents + negative_settlement_cents
    ),
    CONSTRAINT chk_service_refund_liability_status CHECK (
        status IN ('covered','negative_settlement','recovered','closed')
    )
);

CREATE TABLE IF NOT EXISTS service_refund_funding_work_orders (
    work_order_id                TEXT PRIMARY KEY,
    liability_id                 BIGINT NOT NULL UNIQUE REFERENCES service_refund_liability_ledger(liability_id) ON DELETE RESTRICT,
    service_user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    shortage_cents               BIGINT NOT NULL,
    status                       TEXT NOT NULL DEFAULT 'open',
    resolution_jsonb             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at                  TIMESTAMPTZ,
    CONSTRAINT chk_service_refund_funding_shortage CHECK (shortage_cents > 0),
    CONSTRAINT chk_service_refund_funding_status CHECK (status IN ('open','recovering','manual_review','closed')),
    CONSTRAINT chk_service_refund_funding_resolution CHECK (jsonb_typeof(resolution_jsonb)='object')
);

CREATE TABLE IF NOT EXISTS service_refund_cash_jobs (
    cash_job_id                  TEXT PRIMARY KEY,
    case_id                      TEXT NOT NULL UNIQUE REFERENCES consumer_refund_cases(case_id) ON DELETE RESTRICT,
    source_order_id              TEXT NOT NULL UNIQUE REFERENCES dealer_consumer_sales(order_id) ON DELETE RESTRICT,
    consumer_user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    responsible_service_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    amount_cents                 BIGINT NOT NULL,
    original_payment_route       TEXT NOT NULL,
    status                       TEXT NOT NULL DEFAULT 'queued',
    attempt_count                INTEGER NOT NULL DEFAULT 0,
    idempotency_key              TEXT NOT NULL UNIQUE,
    provider_refund_id           TEXT,
    provider_evidence_jsonb      JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error                   TEXT,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at                 TIMESTAMPTZ,
    CONSTRAINT chk_service_refund_cash_amount CHECK (amount_cents > 0 AND attempt_count >= 0),
    CONSTRAINT chk_service_refund_cash_route CHECK (BTRIM(original_payment_route)<>''),
    CONSTRAINT chk_service_refund_cash_status CHECK (
        status IN ('queued','provider_processing','completed','failed','manual_review')
    ),
    CONSTRAINT chk_service_refund_cash_evidence CHECK (jsonb_typeof(provider_evidence_jsonb)='object')
);

-- @index-guard idx_service_refund_cash_jobs_status ON service_refund_cash_jobs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_service_refund_cash_jobs_status' AND i.indrelid = to_regclass('public.service_refund_cash_jobs')) THEN
        NULL;  -- 已在 public.service_refund_cash_jobs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_service_refund_cash_jobs_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_service_refund_cash_jobs_status 已存在但不在 public.service_refund_cash_jobs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_service_refund_cash_jobs_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_service_refund_cash_jobs_status ON public.service_refund_cash_jobs (status, created_at);
    END IF;
END $idxguard$;
-- @index-guard ux_service_refund_provider_ref ON service_refund_cash_jobs unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_service_refund_provider_ref' AND i.indrelid = to_regclass('public.service_refund_cash_jobs')) THEN
        NULL;  -- 已在 public.service_refund_cash_jobs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_service_refund_provider_ref' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_service_refund_provider_ref 已存在但不在 public.service_refund_cash_jobs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_service_refund_provider_ref' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_service_refund_provider_ref ON public.service_refund_cash_jobs (original_payment_route, provider_refund_id) WHERE provider_refund_id IS NOT NULL;
    END IF;
END $idxguard$;
-- @index-guard ux_service_refund_provider_ref_global ON service_refund_cash_jobs unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_service_refund_provider_ref_global' AND i.indrelid = to_regclass('public.service_refund_cash_jobs')) THEN
        NULL;  -- 已在 public.service_refund_cash_jobs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_service_refund_provider_ref_global' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_service_refund_provider_ref_global 已存在但不在 public.service_refund_cash_jobs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_service_refund_provider_ref_global' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_service_refund_provider_ref_global ON public.service_refund_cash_jobs (provider_refund_id) WHERE provider_refund_id IS NOT NULL;
    END IF;
END $idxguard$;

-- Required by proof validation below; repeated later with the remaining refund
-- policy columns for readability and idempotent upgrade of partial installs.
ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS processing_cost_cents BIGINT NOT NULL DEFAULT 0;

-- One external cash proof may close exactly one internal refund leg across the
-- whole platform.  Per-table unique indexes cannot prevent B2B/consumer replay.
CREATE TABLE IF NOT EXISTS external_refund_proof_registry (
    proof_id                    BIGSERIAL PRIMARY KEY,
    provider                    TEXT NOT NULL,
    external_refund_id          TEXT NOT NULL,
    refund_scope                TEXT NOT NULL,
    local_ref                   TEXT NOT NULL,
    amount_cents                BIGINT NOT NULL,
    evidence_jsonb              JSONB NOT NULL DEFAULT '{}'::jsonb,
    claimed_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_external_refund_proof_nonblank CHECK (
        BTRIM(provider)<>'' AND provider=LOWER(BTRIM(provider))
        AND BTRIM(external_refund_id)<>'' AND BTRIM(local_ref)<>''
    ),
    CONSTRAINT chk_external_refund_proof_scope CHECK (
        refund_scope IN ('consumer','dealer_b2b')
    ),
    CONSTRAINT chk_external_refund_proof_provider CHECK (
        provider IN ('wechat','xunhupay')
    ),
    CONSTRAINT chk_external_refund_proof_amount CHECK (amount_cents > 0),
    CONSTRAINT chk_external_refund_proof_evidence CHECK (
        jsonb_typeof(evidence_jsonb)='object'
    ),
    CONSTRAINT ux_external_refund_proof_key UNIQUE (provider, external_refund_id),
    CONSTRAINT ux_external_refund_proof_local UNIQUE (refund_scope, local_ref)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='external_refund_proof_registry'::regclass
           AND conname='chk_external_refund_proof_provider'
    ) THEN
        ALTER TABLE external_refund_proof_registry
            ADD CONSTRAINT chk_external_refund_proof_provider
            CHECK (provider IN ('wechat','xunhupay'));
    END IF;
END $$;

-- The provider is part of a global idempotency namespace.  Refuse to migrate
-- arbitrary aliases because the same provider reference could otherwise be
-- claimed twice under different strings.
DO $$
DECLARE unsupported_provider TEXT;
BEGIN
    WITH raw_providers AS (
        SELECT LOWER(BTRIM(external_channel)) AS provider
          FROM dealer_resale_refunds
         WHERE status='completed' AND external_channel IS NOT NULL
           AND external_refund_id IS NOT NULL
        UNION
        SELECT LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
          FROM dealer_resale_refunds r
          JOIN recharge_orders o ON o.id=r.order_id
         WHERE r.status='completed'
           AND o.refund_status IN ('completed','pending_review')
           AND o.refund_completed_at IS NOT NULL
        UNION
        SELECT LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
          FROM dealer_consumer_sales s
          JOIN recharge_orders o ON o.id=s.order_id
         WHERE s.state='refunded'
           AND o.refund_status IN ('completed','pending_review')
           AND o.refund_completed_at IS NOT NULL
        UNION
        SELECT LOWER(BTRIM(provider_evidence_jsonb->>'provider'))
          FROM service_refund_cash_jobs
         WHERE status='completed' AND provider_refund_id IS NOT NULL
    )
    SELECT provider INTO unsupported_provider
      FROM raw_providers
     WHERE provider IS NOT NULL AND provider<>''
       AND provider NOT IN (
           'wechat','wechat_pay','wechat_jsapi','wechat_native',
           'xunhupay','xunhupay_manual','xunhupay_callback'
       )
     LIMIT 1;
    IF unsupported_provider IS NOT NULL THEN
        RAISE EXCEPTION 'unsupported external refund provider namespace: %', unsupported_provider;
    END IF;
END $$;

-- The refund-proof backfill runs before the later schema contract block.  Add
-- this dispatch field now so the migration can apply the same original-route
-- rule as services.dealer_inventory_resale._trusted_recharge_refund_proof.
ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS actual_payment_channel TEXT;

CREATE OR REPLACE FUNCTION pg_temp.migration_trusted_recharge_refund_proof(
    refund_status_value TEXT,
    refund_completed_at_value TIMESTAMPTZ,
    refunded_amount_value BIGINT,
    expected_amount_value BIGINT,
    actual_payment_channel_value TEXT,
    payment_method_value TEXT,
    settlement_snapshot_value JSONB
) RETURNS BOOLEAN
LANGUAGE SQL IMMUTABLE AS $$
    SELECT
        COALESCE(LOWER(refund_status_value),'') IN ('completed','pending_review')
        AND refund_completed_at_value IS NOT NULL
        AND refunded_amount_value IS NOT DISTINCT FROM expected_amount_value
        AND BTRIM(COALESCE(settlement_snapshot_value->>'provider_refund_id',''))<>''
        AND (
            (
                LOWER(BTRIM(CASE
                    WHEN COALESCE(LOWER(BTRIM(actual_payment_channel_value)),'') NOT IN ('','unknown')
                    THEN actual_payment_channel_value
                    ELSE COALESCE(payment_method_value,'unknown')
                END)) LIKE 'wechat%'
                AND CASE LOWER(BTRIM(settlement_snapshot_value->>'refund_provider'))
                        WHEN 'wechat_pay' THEN 'wechat'
                        WHEN 'wechat_jsapi' THEN 'wechat'
                        WHEN 'wechat_native' THEN 'wechat'
                        WHEN 'xunhupay_manual' THEN 'xunhupay'
                        WHEN 'xunhupay_callback' THEN 'xunhupay'
                        ELSE LOWER(BTRIM(settlement_snapshot_value->>'refund_provider'))
                    END='wechat'
                AND settlement_snapshot_value->>'refund_evidence_kind'
                    IN ('signed_wechat_callback','wechat_refund_api')
            ) OR (
                LOWER(BTRIM(CASE
                    WHEN COALESCE(LOWER(BTRIM(actual_payment_channel_value)),'') NOT IN ('','unknown')
                    THEN actual_payment_channel_value
                    ELSE COALESCE(payment_method_value,'unknown')
                END))='xunhupay'
                AND CASE LOWER(BTRIM(settlement_snapshot_value->>'refund_provider'))
                        WHEN 'wechat_pay' THEN 'wechat'
                        WHEN 'wechat_jsapi' THEN 'wechat'
                        WHEN 'wechat_native' THEN 'wechat'
                        WHEN 'xunhupay_manual' THEN 'xunhupay'
                        WHEN 'xunhupay_callback' THEN 'xunhupay'
                        ELSE LOWER(BTRIM(settlement_snapshot_value->>'refund_provider'))
                END='xunhupay'
                AND settlement_snapshot_value->>'refund_evidence_kind'
                    ='signed_xunhupay_callback'
                AND settlement_snapshot_value->>'provider_refund_reference_kind'
                    ='signed_xunhupay_refund_response'
            )
        )
$$;

-- Freeze every business projection that can claim an external refund proof.
-- The prestart runner executes this whole file as one PostgreSQL transaction,
-- so these locks remain held through the final coverage check and prevent a
-- live writer from changing a proof after preflight but before registration.
LOCK TABLE consumer_refund_cases,
           dealer_consumer_sales,
           dealer_resale_orders,
           dealer_resale_profit_ledger,
           dealer_resale_refunds,
           external_refund_proof_registry,
           recharge_orders,
           service_refund_cash_jobs
    IN SHARE ROW EXCLUSIVE MODE;

-- This migration precedes the additive JIT migration on a fresh database but
-- can be rerun after JIT exists.  Lock that projection when present so the
-- historical proof scan cannot race a final-hop reversal.
DO $$
BEGIN
    IF to_regclass('dealer_resale_hop_profit_ledger') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE dealer_resale_hop_profit_ledger '
                'IN SHARE ROW EXCLUSIVE MODE';
    END IF;
END $$;

-- Legacy one-hop orders use dealer_resale_profit_ledger.  JIT orders reverse
-- only their final hop (ancestor hops are intentionally untouched), so accept
-- that exact terminal when the later additive table is present.
CREATE OR REPLACE FUNCTION pg_temp.migration_b2b_profit_terminal(order_id_value TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    terminal BOOLEAN := FALSE;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM dealer_resale_profit_ledger p
         WHERE p.order_id=order_id_value AND p.status='reversed'
    ) INTO terminal;
    IF terminal OR to_regclass('dealer_resale_hop_profit_ledger') IS NULL THEN
        RETURN terminal;
    END IF;
    EXECUTE
        'SELECT EXISTS ('
        ' SELECT 1 FROM dealer_resale_hop_profit_ledger p'
        ' WHERE p.root_order_id=$1 AND p.status=''reversed'''
        '   AND p.hop_seq=('
        '       SELECT MAX(last_hop.hop_seq)'
        '       FROM dealer_resale_hop_profit_ledger last_hop'
        '       WHERE last_hop.root_order_id=$1'
        '   )'
        ')'
       INTO terminal USING order_id_value;
    RETURN terminal;
END $$;

-- One reusable relation defines the complete cash-job proof contract.  Every
-- migration phase below (preflight, duplicate detection, backfill and final
-- coverage) consumes this same relation so their predicates cannot drift.
CREATE OR REPLACE VIEW pg_temp.migration_trusted_completed_cash_jobs AS
SELECT j.cash_job_id,
       j.case_id,
       j.source_order_id,
       CASE LOWER(BTRIM(j.provider_evidence_jsonb->>'provider'))
           WHEN 'wechat_pay' THEN 'wechat'
           WHEN 'wechat_jsapi' THEN 'wechat'
           WHEN 'wechat_native' THEN 'wechat'
           WHEN 'xunhupay_manual' THEN 'xunhupay'
           WHEN 'xunhupay_callback' THEN 'xunhupay'
           ELSE LOWER(BTRIM(j.provider_evidence_jsonb->>'provider'))
       END AS provider,
       BTRIM(j.provider_refund_id) AS external_refund_id,
       j.amount_cents,
       j.provider_evidence_jsonb AS evidence_jsonb,
       COALESCE(j.completed_at,NOW()) AS claimed_at
  FROM service_refund_cash_jobs j
  JOIN consumer_refund_cases c ON c.case_id=j.case_id
  JOIN dealer_consumer_sales s ON s.order_id=j.source_order_id
  JOIN recharge_orders o ON o.id=j.source_order_id
 WHERE j.status='completed'
   AND j.source_order_id IS NOT DISTINCT FROM c.source_order_id
   AND j.consumer_user_id IS NOT DISTINCT FROM c.consumer_user_id
   AND j.consumer_user_id IS NOT DISTINCT FROM s.consumer_user_id
   AND j.consumer_user_id IS NOT DISTINCT FROM o.user_id
   AND j.responsible_service_user_id IS NOT DISTINCT FROM c.responsible_service_user_id
   AND j.responsible_service_user_id IS NOT DISTINCT FROM s.refund_responsible_user_id
   AND j.responsible_service_user_id IS NOT DISTINCT FROM s.seller_user_id
   AND j.responsible_service_user_id IS NOT DISTINCT FROM o.agent_user_id
   AND c.internal_settled_at IS NOT NULL
   AND c.status='completed'
   AND c.cash_status='completed'
   AND s.state='refunded'
   AND s.refunded_at IS NOT NULL
   AND j.completed_at IS NOT NULL
   AND j.attempt_count > 0
   AND j.last_error IS NULL
   AND j.amount_cents > 0
   AND j.amount_cents IS NOT DISTINCT FROM c.refund_amount_cents
   AND BTRIM(COALESCE(j.provider_refund_id,''))<>''
   AND BTRIM(j.provider_refund_id)
       IS NOT DISTINCT FROM BTRIM(o.settlement_snapshot_jsonb->>'provider_refund_id')
   AND BTRIM(j.provider_evidence_jsonb->>'provider_refund_id')
       IS NOT DISTINCT FROM BTRIM(j.provider_refund_id)
   AND j.provider_evidence_jsonb->>'source'
       IS NOT DISTINCT FROM 'recharge_orders.settlement_snapshot_jsonb'
   AND j.provider_evidence_jsonb->>'refund_evidence_kind'
       IS NOT DISTINCT FROM o.settlement_snapshot_jsonb->>'refund_evidence_kind'
   AND CASE
           WHEN COALESCE(j.provider_evidence_jsonb->>'refunded_amount_cents','')
                ~ '^[0-9]+$'
           THEN (j.provider_evidence_jsonb->>'refunded_amount_cents')::bigint
       END IS NOT DISTINCT FROM j.amount_cents
   AND CASE LOWER(BTRIM(j.provider_evidence_jsonb->>'provider'))
           WHEN 'wechat_pay' THEN 'wechat'
           WHEN 'wechat_jsapi' THEN 'wechat'
           WHEN 'wechat_native' THEN 'wechat'
           WHEN 'xunhupay_manual' THEN 'xunhupay'
           WHEN 'xunhupay_callback' THEN 'xunhupay'
           ELSE LOWER(BTRIM(j.provider_evidence_jsonb->>'provider'))
       END IS NOT DISTINCT FROM
       CASE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
           WHEN 'wechat_pay' THEN 'wechat'
           WHEN 'wechat_jsapi' THEN 'wechat'
           WHEN 'wechat_native' THEN 'wechat'
           WHEN 'xunhupay_manual' THEN 'xunhupay'
           WHEN 'xunhupay_callback' THEN 'xunhupay'
           ELSE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
       END
   AND LOWER(BTRIM(j.original_payment_route)) IS NOT DISTINCT FROM
       LOWER(BTRIM(CASE
           WHEN COALESCE(LOWER(BTRIM(o.actual_payment_channel)),'') NOT IN ('','unknown')
           THEN o.actual_payment_channel ELSE COALESCE(o.payment_method,'unknown')
       END))
   AND LOWER(BTRIM(j.provider_evidence_jsonb->>'original_payment_route'))
       IS NOT DISTINCT FROM LOWER(BTRIM(j.original_payment_route))
   AND pg_temp.migration_trusted_recharge_refund_proof(
       o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
       j.amount_cents,o.actual_payment_channel,o.payment_method,
       o.settlement_snapshot_jsonb
   ) IS TRUE;

-- A completed manual B2B row is only a trusted proof when its immutable audit
-- payload agrees with the payment terminal and the order's exact refundable
-- amount.  Refuse ambiguous historical rows; never bless an empty audit.
DO $$
DECLARE bad_order TEXT;
BEGIN
    SELECT r.order_id INTO bad_order
      FROM dealer_resale_refunds r
      JOIN dealer_resale_orders d ON d.order_id=r.order_id
      JOIN recharge_orders o ON o.id=r.order_id
     WHERE r.status='completed' AND r.external_channel IS NOT NULL
       AND r.external_refund_id IS NOT NULL
       AND (
           COALESCE(o.refund_status,'') NOT IN ('completed','pending_review')
           OR o.refund_completed_at IS NULL
           OR d.state IS DISTINCT FROM 'refunded'
           OR pg_temp.migration_b2b_profit_terminal(d.order_id) IS NOT TRUE
           OR COALESCE(o.refunded_amount_cents,0)
                <> (d.sale_amount_cents-COALESCE(r.processing_cost_cents,0))
           OR jsonb_typeof(r.audit_jsonb->'external_cash_refund') IS DISTINCT FROM 'object'
           OR CASE LOWER(BTRIM(r.audit_jsonb->'external_cash_refund'->>'provider'))
                  WHEN 'wechat_pay' THEN 'wechat'
                  WHEN 'wechat_jsapi' THEN 'wechat'
                  WHEN 'wechat_native' THEN 'wechat'
                  WHEN 'xunhupay_manual' THEN 'xunhupay'
                  WHEN 'xunhupay_callback' THEN 'xunhupay'
                  ELSE LOWER(BTRIM(r.audit_jsonb->'external_cash_refund'->>'provider'))
              END
              IS DISTINCT FROM
              CASE LOWER(BTRIM(r.external_channel))
                  WHEN 'wechat_pay' THEN 'wechat'
                  WHEN 'wechat_jsapi' THEN 'wechat'
                  WHEN 'wechat_native' THEN 'wechat'
                  WHEN 'xunhupay_manual' THEN 'xunhupay'
                  WHEN 'xunhupay_callback' THEN 'xunhupay'
                  ELSE LOWER(BTRIM(r.external_channel))
              END
           OR BTRIM(COALESCE(
                  r.audit_jsonb->'external_cash_refund'->>'external_refund_id',''
              )) <> BTRIM(r.external_refund_id)
           OR CASE
                  WHEN COALESCE(r.audit_jsonb->'external_cash_refund'->>'amount_cents','')
                       ~ '^[0-9]+$'
                  THEN (r.audit_jsonb->'external_cash_refund'->>'amount_cents')::bigint
              END IS DISTINCT FROM
              (d.sale_amount_cents-COALESCE(r.processing_cost_cents,0))
           OR BTRIM(COALESCE(
                  r.audit_jsonb->'external_cash_refund'->>'external_completed_at',''
              ))=''
           OR jsonb_typeof(r.audit_jsonb->'external_cash_refund'->'evidence')
                IS DISTINCT FROM 'object'
           OR r.audit_jsonb->'external_cash_refund'->'evidence'='{}'::jsonb
       )
     LIMIT 1;
    IF bad_order IS NOT NULL THEN
        RAISE EXCEPTION 'completed B2B refund lacks trusted immutable external proof: %', bad_order;
    END IF;
END $$;

-- A callback-derived proof must satisfy the exact runtime provider/kind/route/
-- amount contract and may only be registered after the B2B inventory and profit
-- projections reached their internal refund terminal.
DO $$
DECLARE bad_order TEXT;
BEGIN
    SELECT r.order_id INTO bad_order
      FROM dealer_resale_refunds r
      JOIN dealer_resale_orders d ON d.order_id=r.order_id
      JOIN recharge_orders o ON o.id=r.order_id
     WHERE r.status='completed'
       AND (r.external_channel IS NULL OR r.external_refund_id IS NULL)
       AND (
           d.state IS DISTINCT FROM 'refunded'
           OR pg_temp.migration_b2b_profit_terminal(d.order_id) IS NOT TRUE
           OR pg_temp.migration_trusted_recharge_refund_proof(
               o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
               d.sale_amount_cents-COALESCE(r.processing_cost_cents,0),
               o.actual_payment_channel,o.payment_method,o.settlement_snapshot_jsonb
           ) IS NOT TRUE
       )
     LIMIT 1;
    IF bad_order IS NOT NULL THEN
        RAISE EXCEPTION 'completed B2B callback refund lacks runtime-equivalent proof or internal terminal: %', bad_order;
    END IF;
END $$;

-- Consumer callback terminals use the same proof helper.  A zero-cash case has
-- no provider proof by design; every positive-cash refunded sale must be
-- runtime-verifiable before migration can claim its external refund id.
DO $$
DECLARE bad_case TEXT;
BEGIN
    SELECT c.case_id INTO bad_case
      FROM dealer_consumer_sales s
      JOIN recharge_orders o ON o.id=s.order_id
      JOIN consumer_refund_cases c ON c.source_order_id=s.order_id
     WHERE s.state='refunded'
       AND CASE WHEN c.internal_settled_at IS NOT NULL
                THEN c.refund_amount_cents ELSE s.sale_amount_cents END > 0
       AND pg_temp.migration_trusted_recharge_refund_proof(
           o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
           CASE WHEN c.internal_settled_at IS NOT NULL
                THEN c.refund_amount_cents ELSE s.sale_amount_cents END,
           o.actual_payment_channel,o.payment_method,o.settlement_snapshot_jsonb
       ) IS NOT TRUE
     LIMIT 1;
    IF bad_case IS NOT NULL THEN
        RAISE EXCEPTION 'completed consumer refund lacks runtime-equivalent provider proof: %', bad_case;
    END IF;
END $$;

-- Completed async cash jobs are a second projection of the same callback
-- proof.  Refuse route/provider/id/amount drift rather than registering the job
-- under a namespace the runtime settlement sink would reject.
DO $$
DECLARE bad_job TEXT;
BEGIN
    SELECT j.cash_job_id INTO bad_job
      FROM service_refund_cash_jobs j
      LEFT JOIN pg_temp.migration_trusted_completed_cash_jobs t
        ON t.cash_job_id=j.cash_job_id
     WHERE j.status='completed' AND t.cash_job_id IS NULL
     LIMIT 1;
    IF bad_job IS NOT NULL THEN
        RAISE EXCEPTION 'completed consumer cash job lacks runtime-equivalent provider proof: %', bad_job;
    END IF;
END $$;

DO $$
DECLARE duplicate_proof TEXT;
BEGIN
    WITH proofs AS (
        SELECT CASE LOWER(BTRIM(external_channel))
                   WHEN 'wechat_pay' THEN 'wechat'
                   WHEN 'wechat_jsapi' THEN 'wechat'
                   WHEN 'wechat_native' THEN 'wechat'
                   WHEN 'xunhupay_manual' THEN 'xunhupay'
                   WHEN 'xunhupay_callback' THEN 'xunhupay'
                   ELSE LOWER(BTRIM(external_channel))
               END AS provider,
               BTRIM(external_refund_id) AS external_refund_id,
               'dealer_b2b'::text AS scope, order_id AS local_ref
          FROM dealer_resale_refunds
         WHERE status='completed' AND external_channel IS NOT NULL
           AND external_refund_id IS NOT NULL
        UNION
        SELECT CASE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
                   WHEN 'wechat_pay' THEN 'wechat'
                   WHEN 'wechat_jsapi' THEN 'wechat'
                   WHEN 'wechat_native' THEN 'wechat'
                   WHEN 'xunhupay_manual' THEN 'xunhupay'
                   WHEN 'xunhupay_callback' THEN 'xunhupay'
                   ELSE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
               END,
               BTRIM(o.settlement_snapshot_jsonb->>'provider_refund_id'),
               'dealer_b2b',r.order_id
          FROM dealer_resale_refunds r
          JOIN dealer_resale_orders d ON d.order_id=r.order_id
          JOIN recharge_orders o ON o.id=r.order_id
         WHERE r.status='completed'
           AND (r.external_channel IS NULL OR r.external_refund_id IS NULL)
           AND d.state='refunded'
           AND pg_temp.migration_b2b_profit_terminal(d.order_id) IS TRUE
           AND pg_temp.migration_trusted_recharge_refund_proof(
               o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
               d.sale_amount_cents-COALESCE(r.processing_cost_cents,0),
               o.actual_payment_channel,o.payment_method,o.settlement_snapshot_jsonb
           )
        UNION
        SELECT CASE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
                   WHEN 'wechat_pay' THEN 'wechat'
                   WHEN 'wechat_jsapi' THEN 'wechat'
                   WHEN 'wechat_native' THEN 'wechat'
                   WHEN 'xunhupay_manual' THEN 'xunhupay'
                   WHEN 'xunhupay_callback' THEN 'xunhupay'
                   ELSE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
               END,
               BTRIM(o.settlement_snapshot_jsonb->>'provider_refund_id'),
               'consumer',c.case_id
          FROM dealer_consumer_sales s
          JOIN recharge_orders o ON o.id=s.order_id
          JOIN consumer_refund_cases c ON c.source_order_id=s.order_id
         WHERE s.state='refunded'
           AND CASE WHEN c.internal_settled_at IS NOT NULL
                    THEN c.refund_amount_cents ELSE s.sale_amount_cents END > 0
           AND pg_temp.migration_trusted_recharge_refund_proof(
               o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
               CASE WHEN c.internal_settled_at IS NOT NULL
                    THEN c.refund_amount_cents ELSE s.sale_amount_cents END,
               o.actual_payment_channel,o.payment_method,o.settlement_snapshot_jsonb
           )
        UNION
        SELECT t.provider,t.external_refund_id,'consumer',t.case_id
          FROM pg_temp.migration_trusted_completed_cash_jobs t
    )
    SELECT provider || ':' || external_refund_id INTO duplicate_proof
      FROM proofs WHERE provider<>'' AND external_refund_id<>''
     GROUP BY provider,external_refund_id HAVING COUNT(*)>1 LIMIT 1;
    IF duplicate_proof IS NOT NULL THEN
        RAISE EXCEPTION 'external refund proof already reused across refund legs: %', duplicate_proof;
    END IF;
END $$;

INSERT INTO external_refund_proof_registry(
    provider,external_refund_id,refund_scope,local_ref,amount_cents,evidence_jsonb,claimed_at
)
SELECT CASE LOWER(BTRIM(r.external_channel))
           WHEN 'wechat_pay' THEN 'wechat'
           WHEN 'wechat_jsapi' THEN 'wechat'
           WHEN 'wechat_native' THEN 'wechat'
           WHEN 'xunhupay_manual' THEN 'xunhupay'
           WHEN 'xunhupay_callback' THEN 'xunhupay'
           ELSE LOWER(BTRIM(r.external_channel))
       END,
       BTRIM(r.external_refund_id),'dealer_b2b',r.order_id,
       COALESCE(o.refunded_amount_cents,0),
       r.audit_jsonb->'external_cash_refund',COALESCE(r.completed_at,NOW())
  FROM dealer_resale_refunds r JOIN recharge_orders o ON o.id=r.order_id
 WHERE r.status='completed' AND r.external_channel IS NOT NULL
   AND r.external_refund_id IS NOT NULL AND COALESCE(o.refunded_amount_cents,0)>0
ON CONFLICT DO NOTHING;

INSERT INTO external_refund_proof_registry(
    provider,external_refund_id,refund_scope,local_ref,amount_cents,evidence_jsonb,claimed_at
)
SELECT CASE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
           WHEN 'wechat_pay' THEN 'wechat'
           WHEN 'wechat_jsapi' THEN 'wechat'
           WHEN 'wechat_native' THEN 'wechat'
           WHEN 'xunhupay_manual' THEN 'xunhupay'
           WHEN 'xunhupay_callback' THEN 'xunhupay'
           ELSE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
       END,
       BTRIM(o.settlement_snapshot_jsonb->>'provider_refund_id'),
       'dealer_b2b',r.order_id,o.refunded_amount_cents,
       o.settlement_snapshot_jsonb,o.refund_completed_at
  FROM dealer_resale_refunds r
  JOIN dealer_resale_orders d ON d.order_id=r.order_id
  JOIN recharge_orders o ON o.id=r.order_id
 WHERE r.status='completed' AND (r.external_channel IS NULL OR r.external_refund_id IS NULL)
   AND d.state='refunded'
   AND pg_temp.migration_b2b_profit_terminal(d.order_id) IS TRUE
   AND BTRIM(COALESCE(o.settlement_snapshot_jsonb->>'refund_provider',''))<>''
   AND BTRIM(COALESCE(o.settlement_snapshot_jsonb->>'provider_refund_id',''))<>''
   AND pg_temp.migration_trusted_recharge_refund_proof(
       o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
       d.sale_amount_cents-COALESCE(r.processing_cost_cents,0),
       o.actual_payment_channel,o.payment_method,o.settlement_snapshot_jsonb
   )
ON CONFLICT DO NOTHING;

INSERT INTO external_refund_proof_registry(
    provider,external_refund_id,refund_scope,local_ref,amount_cents,evidence_jsonb,claimed_at
)
SELECT CASE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
           WHEN 'wechat_pay' THEN 'wechat'
           WHEN 'wechat_jsapi' THEN 'wechat'
           WHEN 'wechat_native' THEN 'wechat'
           WHEN 'xunhupay_manual' THEN 'xunhupay'
           WHEN 'xunhupay_callback' THEN 'xunhupay'
           ELSE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
       END,
       BTRIM(o.settlement_snapshot_jsonb->>'provider_refund_id'),
       'consumer',c.case_id,
       CASE WHEN c.internal_settled_at IS NOT NULL
            THEN c.refund_amount_cents ELSE s.sale_amount_cents END,
       o.settlement_snapshot_jsonb,o.refund_completed_at
  FROM dealer_consumer_sales s
  JOIN recharge_orders o ON o.id=s.order_id
  JOIN consumer_refund_cases c ON c.source_order_id=s.order_id
 WHERE s.state='refunded'
   AND CASE WHEN c.internal_settled_at IS NOT NULL
            THEN c.refund_amount_cents ELSE s.sale_amount_cents END > 0
   AND BTRIM(COALESCE(o.settlement_snapshot_jsonb->>'refund_provider',''))<>''
   AND BTRIM(COALESCE(o.settlement_snapshot_jsonb->>'provider_refund_id',''))<>''
   AND pg_temp.migration_trusted_recharge_refund_proof(
       o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
       CASE WHEN c.internal_settled_at IS NOT NULL
            THEN c.refund_amount_cents ELSE s.sale_amount_cents END,
       o.actual_payment_channel,o.payment_method,o.settlement_snapshot_jsonb
   )
ON CONFLICT DO NOTHING;

INSERT INTO external_refund_proof_registry(
    provider,external_refund_id,refund_scope,local_ref,amount_cents,evidence_jsonb,claimed_at
)
SELECT t.provider,t.external_refund_id,'consumer',t.case_id,t.amount_cents,
       t.evidence_jsonb,t.claimed_at
  FROM pg_temp.migration_trusted_completed_cash_jobs t
ON CONFLICT DO NOTHING;

-- @index-guard idx_service_refund_funding_open ON service_refund_funding_work_orders plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_service_refund_funding_open' AND i.indrelid = to_regclass('public.service_refund_funding_work_orders')) THEN
        NULL;  -- 已在 public.service_refund_funding_work_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_service_refund_funding_open' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_service_refund_funding_open 已存在但不在 public.service_refund_funding_work_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_service_refund_funding_open' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_service_refund_funding_open ON public.service_refund_funding_work_orders (status, created_at) WHERE status <> 'closed';
    END IF;
END $idxguard$;

ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS reason_category TEXT NOT NULL DEFAULT 'voluntary_unused_inventory';
ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS processing_cost_cents BIGINT NOT NULL DEFAULT 0;
ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS processing_cost_evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS mandatory_evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS refund_amount_cents BIGINT NOT NULL DEFAULT 0;
ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE dealer_resale_refunds
    ADD COLUMN IF NOT EXISTS execution_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_refund_processing_cost') THEN
        ALTER TABLE dealer_resale_refunds ADD CONSTRAINT chk_dealer_refund_processing_cost
            CHECK (processing_cost_cents >= 0 AND jsonb_typeof(processing_cost_evidence_jsonb)='object');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_refund_mandatory_evidence') THEN
        ALTER TABLE dealer_resale_refunds ADD CONSTRAINT chk_dealer_refund_mandatory_evidence
            CHECK (jsonb_typeof(mandatory_evidence_jsonb)='object');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_dealer_refund_execution_money') THEN
        ALTER TABLE dealer_resale_refunds ADD CONSTRAINT chk_dealer_refund_execution_money
            CHECK (refund_amount_cents >= 0 AND attempt_count >= 0);
    END IF;
END $$;

ALTER TABLE agreement_signatures
    ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE agreement_signatures
    ADD COLUMN IF NOT EXISTS evidence_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE agent_factory_agreements
    ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE agent_factory_agreements
    ADD COLUMN IF NOT EXISTS signed_ip TEXT;
ALTER TABLE agent_factory_agreements
    ADD COLUMN IF NOT EXISTS signed_ua TEXT;
ALTER TABLE agent_factory_agreements
    ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ;
ALTER TABLE agent_factory_agreements
    ADD COLUMN IF NOT EXISTS rejected_reason TEXT;

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS actual_payment_channel TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_recharge_actual_payment_channel') THEN
        ALTER TABLE recharge_orders ADD CONSTRAINT chk_recharge_actual_payment_channel
            CHECK (actual_payment_channel IS NULL OR actual_payment_channel IN (
                'wechat_jsapi','wechat_native','xunhupay','manual_bank','unknown'
            ));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS purchase_agreement_acceptances (
    acceptance_id               TEXT PRIMARY KEY,
    user_id                     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    agreement_type              TEXT NOT NULL,
    agreement_version           TEXT NOT NULL,
    content_hash                TEXT NOT NULL,
    accepted_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_address                  TEXT,
    user_agent                  TEXT,
    surface                     TEXT NOT NULL,
    evidence_jsonb              JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT chk_purchase_acceptance_nonblank CHECK (
        BTRIM(acceptance_id)<>'' AND BTRIM(agreement_type)<>''
        AND BTRIM(agreement_version)<>'' AND BTRIM(content_hash)<>'' AND BTRIM(surface)<>''
    ),
    CONSTRAINT chk_purchase_acceptance_evidence CHECK (jsonb_typeof(evidence_jsonb)='object')
);

-- @index-guard idx_purchase_acceptance_user_time ON purchase_agreement_acceptances plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_purchase_acceptance_user_time' AND i.indrelid = to_regclass('public.purchase_agreement_acceptances')) THEN
        NULL;  -- 已在 public.purchase_agreement_acceptances 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_purchase_acceptance_user_time' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_purchase_acceptance_user_time 已存在但不在 public.purchase_agreement_acceptances 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_purchase_acceptance_user_time' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_purchase_acceptance_user_time ON public.purchase_agreement_acceptances (user_id, accepted_at DESC);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS purchase_agreement_acceptance_uses (
    acceptance_id               TEXT PRIMARY KEY REFERENCES purchase_agreement_acceptances(acceptance_id) ON DELETE RESTRICT,
    purchase_kind               TEXT NOT NULL,
    purchase_ref_id             TEXT NOT NULL,
    used_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_purchase_acceptance_global_once UNIQUE (acceptance_id),
    CONSTRAINT ux_purchase_acceptance_ref UNIQUE (purchase_kind, purchase_ref_id),
    CONSTRAINT chk_purchase_acceptance_kind CHECK (
        purchase_kind IN ('customer-recharge','subscription-checkout')
    )
);

CREATE OR REPLACE FUNCTION consume_purchase_acceptance_use()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_acceptance_id TEXT;
    v_kind TEXT;
    v_ref TEXT;
    v_surface TEXT;
    v_acceptance_user_id INTEGER;
    v_existing_kind TEXT;
    v_existing_ref TEXT;
BEGIN
    IF TG_TABLE_NAME = 'recharge_orders' THEN
        v_acceptance_id := NEW.pricing_snapshot_jsonb->>'terms_acceptance_id';
        v_kind := 'customer-recharge';
        v_ref := NEW.id::TEXT;
    ELSIF TG_TABLE_NAME = 'user_social_subscriptions' THEN
        v_acceptance_id := NEW.purchase_terms_acceptance_id;
        v_kind := 'subscription-checkout';
        v_ref := COALESCE(NEW.order_id::TEXT, 'SUB#' || NEW.id::TEXT);
    ELSE
        RAISE EXCEPTION 'unsupported acceptance consumer table %', TG_TABLE_NAME;
    END IF;
    IF v_acceptance_id IS NULL OR BTRIM(v_acceptance_id) = '' THEN
        RETURN NEW;
    END IF;
    SELECT surface,user_id INTO v_surface,v_acceptance_user_id
      FROM purchase_agreement_acceptances
     WHERE acceptance_id=v_acceptance_id;
    IF v_surface IS NULL OR v_surface <> v_kind THEN
        RAISE EXCEPTION 'purchase acceptance surface mismatch'
            USING ERRCODE='23514', CONSTRAINT='chk_purchase_acceptance_surface_binding';
    END IF;
    IF v_acceptance_user_id IS DISTINCT FROM NEW.user_id THEN
        RAISE EXCEPTION 'purchase acceptance owner mismatch'
            USING ERRCODE='23514', CONSTRAINT='chk_purchase_acceptance_owner_binding';
    END IF;
    INSERT INTO purchase_agreement_acceptance_uses(acceptance_id,purchase_kind,purchase_ref_id)
    VALUES (v_acceptance_id,v_kind,v_ref)
    ON CONFLICT(acceptance_id) DO NOTHING;
    IF NOT FOUND THEN
        SELECT purchase_kind,purchase_ref_id INTO v_existing_kind,v_existing_ref
          FROM purchase_agreement_acceptance_uses WHERE acceptance_id=v_acceptance_id;
        IF v_existing_kind IS DISTINCT FROM v_kind OR v_existing_ref IS DISTINCT FROM v_ref THEN
            RAISE EXCEPTION 'purchase acceptance already consumed'
                USING ERRCODE='23505', CONSTRAINT='ux_purchase_acceptance_global_once';
        END IF;
    END IF;
    RETURN NEW;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_recharge_purchase_acceptance_use') THEN
        CREATE TRIGGER trg_recharge_purchase_acceptance_use
        AFTER INSERT OR UPDATE OF pricing_snapshot_jsonb ON recharge_orders
        FOR EACH ROW EXECUTE FUNCTION consume_purchase_acceptance_use();
    END IF;
END $$;
-- @index-guard ux_recharge_terms_acceptance_once ON recharge_orders unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'ux_recharge_terms_acceptance_once' AND i.indrelid = to_regclass('public.recharge_orders')) THEN
        NULL;  -- 已在 public.recharge_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'ux_recharge_terms_acceptance_once' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] ux_recharge_terms_acceptance_once 已存在但不在 public.recharge_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'ux_recharge_terms_acceptance_once' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX ux_recharge_terms_acceptance_once ON public.recharge_orders ((pricing_snapshot_jsonb->>'terms_acceptance_id')) WHERE pricing_snapshot_jsonb ? 'terms_acceptance_id';
    END IF;
END $idxguard$;

DO $$
BEGIN
    IF to_regclass('public.user_social_subscriptions') IS NOT NULL THEN
        ALTER TABLE user_social_subscriptions
            ADD COLUMN IF NOT EXISTS purchase_terms_acceptance_id TEXT;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='fk_subscription_purchase_terms_acceptance'
        ) THEN
            ALTER TABLE user_social_subscriptions
                ADD CONSTRAINT fk_subscription_purchase_terms_acceptance
                FOREIGN KEY (purchase_terms_acceptance_id)
                REFERENCES purchase_agreement_acceptances(acceptance_id) ON DELETE RESTRICT;
        END IF;
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('public.user_social_subscriptions') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_subscription_purchase_acceptance_use') THEN
        CREATE TRIGGER trg_subscription_purchase_acceptance_use
        AFTER INSERT OR UPDATE OF purchase_terms_acceptance_id ON user_social_subscriptions
        FOR EACH ROW EXECUTE FUNCTION consume_purchase_acceptance_use();
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('public.user_social_subscriptions') IS NOT NULL THEN
        -- @index-guard ux_subscription_terms_acceptance_once ON user_social_subscriptions unique
        IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                    WHERE c.relname = 'ux_subscription_terms_acceptance_once' AND i.indrelid = to_regclass('public.user_social_subscriptions')) THEN
            NULL;  -- 已在 public.user_social_subscriptions 上 → 幂等跳过
        ELSIF EXISTS (SELECT 1 FROM pg_class c
                       WHERE c.relname = 'ux_subscription_terms_acceptance_once' AND c.relnamespace = 'public'::regnamespace) THEN
            RAISE EXCEPTION '[index-guard] ux_subscription_terms_acceptance_once 已存在但不在 public.user_social_subscriptions 上(实际宿主:%)—— 拒绝静默跳过',
                (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
                   LEFT JOIN pg_index i ON i.indexrelid = c.oid
                   LEFT JOIN pg_class t ON t.oid = i.indrelid
                  WHERE c.relname = 'ux_subscription_terms_acceptance_once' AND c.relnamespace = 'public'::regnamespace)
                USING ERRCODE = 'duplicate_object';
        ELSE
            CREATE UNIQUE INDEX ux_subscription_terms_acceptance_once ON public.user_social_subscriptions (purchase_terms_acceptance_id) WHERE purchase_terms_acceptance_id IS NOT NULL;
        END IF;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS subscription_refund_cases (
    case_id                     TEXT PRIMARY KEY,
    subscription_id            BIGINT NOT NULL UNIQUE,
    source_order_id             TEXT,
    consumer_user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reason_category             TEXT NOT NULL,
    decision_kind               TEXT NOT NULL,
    mandatory_evidence_status   TEXT NOT NULL DEFAULT 'not_applicable',
    status                      TEXT NOT NULL DEFAULT 'requested',
    refundable_amount_yuan      NUMERIC(10,2),
    usage_snapshot_jsonb        JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_evidence_jsonb      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_subscription_refund_decision CHECK (decision_kind IN ('mandatory','negotiated')),
    CONSTRAINT chk_subscription_refund_mandatory_evidence CHECK (
        mandatory_evidence_status IN ('not_applicable','claimed','verified','rejected')
    ),
    CONSTRAINT chk_subscription_refund_status CHECK (
        status IN ('requested','platform_review','approved','cash_pending','completed','rejected','manual_review')
    ),
    CONSTRAINT chk_subscription_refund_json CHECK (
        jsonb_typeof(usage_snapshot_jsonb)='object' AND jsonb_typeof(request_evidence_jsonb)='object'
    )
);

ALTER TABLE subscription_refund_cases
    ADD COLUMN IF NOT EXISTS mandatory_evidence_status TEXT NOT NULL DEFAULT 'not_applicable';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_subscription_refund_mandatory_evidence') THEN
        ALTER TABLE subscription_refund_cases
            ADD CONSTRAINT chk_subscription_refund_mandatory_evidence CHECK (
                mandatory_evidence_status IN ('not_applicable','claimed','verified','rejected')
            );
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('public.user_social_subscriptions') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_subscription_refund_subscription') THEN
        ALTER TABLE subscription_refund_cases
            ADD CONSTRAINT fk_subscription_refund_subscription
            FOREIGN KEY (subscription_id) REFERENCES user_social_subscriptions(id) ON DELETE RESTRICT;
    END IF;
END $$;

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
            'consumer_refund_cases','consumer_refund_lot_restorations',
            'service_refund_reserve_accounts','service_refund_liability_ledger',
            'service_refund_funding_work_orders','service_refund_cash_jobs',
            'external_refund_proof_registry',
            'purchase_agreement_acceptances','purchase_agreement_acceptance_uses',
            'subscription_refund_cases','recharge_orders','user_social_subscriptions'
          )
        LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'pre-existing NOT VALID constraint must be reviewed before prestart: %.%',
            r.rel, r.conname;
    END IF;
    -- Deliberately do not auto-VALIDATE pre-existing constraints.  A correctly
    -- named NOT VALID object is deployment drift and must block prestart.
    SELECT string_agg(v.t || '.' || v.c, ', ') INTO bad
      FROM (VALUES
        ('consumer_refund_cases','refund_amount_cents','bigint'),
        ('consumer_refund_cases','revoked_paid_points','bigint'),
        ('consumer_refund_cases','revoked_bonus_points','bigint'),
        ('consumer_refund_cases','restored_inventory_points','bigint'),
        ('consumer_refund_cases','revenue_reversed_cents','bigint'),
        ('consumer_refund_cases','row_version','bigint'),
        ('consumer_refund_cases','eligibility_jsonb','jsonb'),
        ('consumer_refund_cases','execution_jsonb','jsonb'),
        ('consumer_refund_lot_restorations','points','bigint'),
        ('consumer_refund_lot_restorations','cost_basis_cents','bigint'),
        ('dealer_consumer_lot_allocations','refunded_points','bigint'),
        ('dealer_consumer_lot_allocations','refunded_cost_cents','bigint'),
        ('dealer_resale_refunds','processing_cost_cents','bigint'),
        ('dealer_resale_refunds','processing_cost_evidence_jsonb','jsonb'),
        ('dealer_resale_refunds','mandatory_evidence_jsonb','jsonb'),
        ('service_refund_reserve_accounts','available_cents','bigint'),
        ('service_refund_reserve_accounts','row_version','bigint'),
        ('service_refund_liability_ledger','refund_amount_cents','bigint'),
        ('service_refund_liability_ledger','pending_settlement_offset_cents','bigint'),
        ('service_refund_liability_ledger','reserve_offset_cents','bigint'),
        ('service_refund_liability_ledger','negative_settlement_cents','bigint'),
        ('service_refund_funding_work_orders','shortage_cents','bigint'),
        ('service_refund_funding_work_orders','resolution_jsonb','jsonb'),
        ('service_refund_cash_jobs','amount_cents','bigint'),
        ('service_refund_cash_jobs','provider_evidence_jsonb','jsonb'),
        ('service_refund_cash_jobs','status','text'),
        ('service_refund_cash_jobs','attempt_count','integer'),
        ('purchase_agreement_acceptances','evidence_jsonb','jsonb'),
        ('external_refund_proof_registry','amount_cents','bigint'),
        ('external_refund_proof_registry','evidence_jsonb','jsonb'),
        ('external_refund_proof_registry','provider','text'),
        ('external_refund_proof_registry','external_refund_id','text'),
        ('external_refund_proof_registry','refund_scope','text'),
        ('external_refund_proof_registry','local_ref','text')
      ) AS v(t,c,typ)
      LEFT JOIN information_schema.columns x
        ON x.table_schema=current_schema() AND x.table_name=v.t
       AND x.column_name=v.c AND x.data_type=v.typ AND x.is_nullable='NO'
     WHERE x.column_name IS NULL;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'direct refund/agreement schema column shape mismatch: %', bad;
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
        'chk_consumer_refund_amounts','chk_consumer_refund_cash_status','chk_consumer_refund_decision_kind','chk_consumer_refund_eligibility_object',
        'chk_consumer_refund_execution','chk_consumer_refund_lot_values','chk_consumer_refund_mandatory_evidence','chk_consumer_refund_parties',
        'chk_consumer_refund_policy','chk_consumer_refund_status','chk_dealer_consumer_allocation_refunded','chk_dealer_consumer_allocation_status',
        'chk_dealer_consumer_allocation_values','chk_dealer_consumer_sale_allocations','chk_dealer_consumer_sale_collector','chk_dealer_consumer_sale_cost_positive',
        'chk_dealer_consumer_sale_markup','chk_dealer_consumer_sale_money','chk_dealer_consumer_sale_paid_fields','chk_dealer_consumer_sale_parties',
        'chk_dealer_consumer_sale_points','chk_dealer_consumer_sale_policy','chk_dealer_consumer_sale_refund_owner','chk_dealer_consumer_sale_state',
        'chk_dealer_consumer_sale_version','chk_dealer_consumer_transfer_cost','chk_dealer_consumer_transfer_direction','chk_dealer_consumer_transfer_sign',
        'chk_dealer_lot_acquisition_cost_positive','chk_dealer_lot_allocation_status','chk_dealer_lot_allocation_values','chk_dealer_lot_cost_conservation',
        'chk_dealer_lot_evidence_object','chk_dealer_lot_id_nonblank','chk_dealer_lot_points_conservation','chk_dealer_lot_points_positive',
        'chk_dealer_lot_pricing_version','chk_dealer_lot_source_kind','chk_dealer_lot_status','chk_dealer_manufacturer_issue_evidence',
        'chk_dealer_manufacturer_issue_values','chk_dealer_profit_cost_positive','chk_dealer_profit_money','chk_dealer_profit_parties',
        'chk_dealer_profit_status','chk_dealer_refund_audit_object','chk_dealer_refund_mandatory_evidence','chk_dealer_refund_processing_cost',
        'chk_dealer_refund_status','chk_dealer_resale_default_markup','chk_dealer_resale_global_singleton','chk_dealer_resale_order_allocations',
        'chk_dealer_resale_order_collector','chk_dealer_resale_order_cost_positive','chk_dealer_resale_order_markup','chk_dealer_resale_order_money',
        'chk_dealer_resale_order_paid_fields','chk_dealer_resale_order_parties','chk_dealer_resale_order_points','chk_dealer_resale_order_refund_owner',
        'chk_dealer_resale_order_source','chk_dealer_resale_order_source_allocations','chk_dealer_resale_order_state','chk_dealer_resale_order_versions',
        'chk_dealer_resale_policy_bounds','chk_dealer_resale_policy_markup','chk_dealer_resale_policy_row_version','chk_dealer_resale_policy_version',
        'chk_dealer_resale_settings_row_version','chk_dealer_resale_settings_version','chk_dealer_transfer_cost_positive','chk_dealer_transfer_delta_sign',
        'chk_dealer_transfer_direction','chk_dealer_transfer_entry_cost','chk_dealer_transfer_money','chk_dealer_transfer_parties',
        'chk_dealer_transfer_points','chk_dealer_transfer_state','chk_external_refund_proof_amount','chk_external_refund_proof_evidence',
        'chk_external_refund_proof_nonblank','chk_external_refund_proof_scope','chk_external_refund_proof_provider',
        'chk_purchase_acceptance_evidence','chk_purchase_acceptance_kind',
        'chk_purchase_acceptance_nonblank','chk_recharge_actual_payment_channel','chk_service_refund_cash_amount','chk_service_refund_cash_evidence',
        'chk_service_refund_cash_route','chk_service_refund_cash_status','chk_service_refund_funding_resolution','chk_service_refund_funding_shortage',
        'chk_service_refund_funding_status','chk_service_refund_liability_money','chk_service_refund_liability_status','chk_service_refund_reserve_values',
        'chk_subscription_refund_decision','chk_subscription_refund_json','chk_subscription_refund_mandatory_evidence','chk_subscription_refund_status',
        'consumer_refund_cases_source_order_id_fkey','fk_dealer_consumer_transfer_sale','fk_dealer_lot_source_transfer','fk_dealer_order_transfer',
        'fk_subscription_purchase_terms_acceptance','fk_subscription_refund_subscription','ux_consumer_refund_lot_restoration','ux_consumer_refund_source',
        'ux_dealer_consumer_allocation','ux_dealer_consumer_allocation_seq','ux_dealer_consumer_transfer_side','ux_dealer_lot_allocation',
        'ux_dealer_lot_allocation_seq','ux_dealer_resale_order_quote','ux_dealer_resale_order_transfer','ux_dealer_transfer_entry',
        'ux_external_refund_proof_key','ux_external_refund_proof_local','ux_purchase_acceptance_global_once','ux_purchase_acceptance_ref'
     ]);
    IF contract_count<>121 OR contract_digest<>'4c502a6142b15a06f201d65995c5898c' THEN
        RAISE EXCEPTION 'direct refund/agreement exact constraint contract mismatch';
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
        'ux_recharge_terms_acceptance_once','ux_service_refund_provider_ref',
        'ux_service_refund_provider_ref_global','ux_subscription_terms_acceptance_once'
     ]) AND x.indisunique AND x.indisvalid;
    IF index_contract_count<>4 OR index_contract_digest<>'44568d79f68d702df4ad4f4c15ba0678' THEN
        RAISE EXCEPTION 'direct refund/agreement exact index contract mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='purchase_agreement_acceptances'
          AND pc.conname='chk_purchase_acceptance_evidence' AND pc.contype='c'
          AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%jsonb_typeof(evidence_jsonb)=''object''::text%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='external_refund_proof_registry'
          AND pc.conname='chk_external_refund_proof_nonblank' AND pc.contype='c'
          AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%provider=lower(btrim(provider))%'
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%btrim(external_refund_id)<>%'
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%btrim(local_ref)<>%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='external_refund_proof_registry'
          AND pc.conname='chk_external_refund_proof_scope' AND pc.contype='c'
          AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%refund_scope=any(array[''consumer''::text,''dealer_b2b''::text])%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='external_refund_proof_registry'
          AND pc.conname='chk_external_refund_proof_provider' AND pc.contype='c'
          AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%provider=any(array[''wechat''::text,''xunhupay''::text])%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='external_refund_proof_registry'
          AND pc.conname='chk_external_refund_proof_amount' AND pc.contype='c'
          AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%amount_cents>0%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='external_refund_proof_registry'
          AND pc.conname='chk_external_refund_proof_evidence' AND pc.contype='c'
          AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%jsonb_typeof(evidence_jsonb)=''object''::text%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='external_refund_proof_registry'
          AND pc.conname='ux_external_refund_proof_key' AND pc.contype='u'
          AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%unique(provider,external_refund_id)%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint pc JOIN pg_class t ON t.oid=pc.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='external_refund_proof_registry'
          AND pc.conname='ux_external_refund_proof_local' AND pc.contype='u'
          AND pc.convalidated
          AND regexp_replace(lower(pg_get_constraintdef(pc.oid,true)),'\s','','g')
              LIKE '%unique(refund_scope,local_ref)%'
    ) THEN
        RAISE EXCEPTION 'direct refund/agreement critical constraint definition mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
        JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='recharge_orders'
          AND i.relname='ux_recharge_terms_acceptance_once'
          AND x.indisunique AND x.indisvalid
          AND x.indnkeyatts=1
          AND regexp_replace(lower(pg_get_indexdef(i.oid)),'\s','','g')
              LIKE '%pricing_snapshot_jsonb->>''terms_acceptance_id''::text%'
          AND regexp_replace(lower(pg_get_expr(x.indpred,x.indrelid)),'\s','','g')
              LIKE '%pricing_snapshot_jsonb?''terms_acceptance_id''::text%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
        JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname='user_social_subscriptions'
          AND i.relname='ux_subscription_terms_acceptance_once'
          AND x.indisunique AND x.indisvalid
          AND x.indnkeyatts=1
          AND regexp_replace(lower(pg_get_indexdef(i.oid)),'\s','','g')
              LIKE '%(purchase_terms_acceptance_id)%'
          AND regexp_replace(lower(pg_get_expr(x.indpred,x.indrelid)),'\s','','g')
              LIKE '%purchase_terms_acceptance_idisnotnull%'
    ) THEN
        RAISE EXCEPTION 'purchase acceptance unique index definition mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid=g.tgrelid
        JOIN pg_proc p ON p.oid=g.tgfoid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE n.nspname=current_schema() AND t.relname='recharge_orders'
          AND g.tgname='trg_recharge_purchase_acceptance_use'
          AND g.tgenabled IN ('O','A') AND g.tgtype=21
          AND p.proname='consume_purchase_acceptance_use' AND pn.nspname=current_schema()
          AND regexp_replace(lower(pg_get_triggerdef(g.oid,true)),'\s','','g')
              LIKE '%afterinsertorupdateofpricing_snapshot_jsonb%'
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid=g.tgrelid
        JOIN pg_proc p ON p.oid=g.tgfoid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE n.nspname=current_schema() AND t.relname='user_social_subscriptions'
          AND g.tgname='trg_subscription_purchase_acceptance_use'
          AND g.tgenabled IN ('O','A') AND g.tgtype=21
          AND p.proname='consume_purchase_acceptance_use' AND pn.nspname=current_schema()
          AND regexp_replace(lower(pg_get_triggerdef(g.oid,true)),'\s','','g')
              LIKE '%afterinsertorupdateofpurchase_terms_acceptance_id%'
    ) THEN
        RAISE EXCEPTION 'purchase acceptance trigger definition mismatch';
    END IF;
    -- Re-evaluate every completed job after registry writes.  This catches a
    -- same-transaction trigger or privileged maintenance hook that mutates the
    -- projection after the first preflight; external writers are fenced by the
    -- table locks held above.
    SELECT j.cash_job_id INTO bad
      FROM service_refund_cash_jobs j
      LEFT JOIN pg_temp.migration_trusted_completed_cash_jobs t
        ON t.cash_job_id=j.cash_job_id
     WHERE j.status='completed' AND t.cash_job_id IS NULL
     LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'completed consumer cash job changed during proof migration: %', bad;
    END IF;
    WITH completed_proofs AS (
        SELECT CASE LOWER(BTRIM(dr.external_channel))
                   WHEN 'wechat_pay' THEN 'wechat'
                   WHEN 'wechat_jsapi' THEN 'wechat'
                   WHEN 'wechat_native' THEN 'wechat'
                   WHEN 'xunhupay_manual' THEN 'xunhupay'
                   WHEN 'xunhupay_callback' THEN 'xunhupay'
                   ELSE LOWER(BTRIM(dr.external_channel))
               END AS provider,
               BTRIM(dr.external_refund_id) AS external_refund_id,
               'dealer_b2b'::text AS refund_scope,dr.order_id AS local_ref,
               COALESCE(o.refunded_amount_cents,0)::bigint AS amount_cents
          FROM dealer_resale_refunds dr JOIN recharge_orders o ON o.id=dr.order_id
         WHERE dr.status='completed' AND dr.external_channel IS NOT NULL
           AND dr.external_refund_id IS NOT NULL
        UNION
        SELECT CASE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
                   WHEN 'wechat_pay' THEN 'wechat'
                   WHEN 'wechat_jsapi' THEN 'wechat'
                   WHEN 'wechat_native' THEN 'wechat'
                   WHEN 'xunhupay_manual' THEN 'xunhupay'
                   WHEN 'xunhupay_callback' THEN 'xunhupay'
                   ELSE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
               END,
               BTRIM(o.settlement_snapshot_jsonb->>'provider_refund_id'),
               'dealer_b2b',dr.order_id,o.refunded_amount_cents::bigint
          FROM dealer_resale_refunds dr
          JOIN dealer_resale_orders d ON d.order_id=dr.order_id
          JOIN recharge_orders o ON o.id=dr.order_id
         WHERE dr.status='completed' AND (dr.external_channel IS NULL OR dr.external_refund_id IS NULL)
           AND d.state='refunded'
           AND pg_temp.migration_b2b_profit_terminal(d.order_id) IS TRUE
           AND pg_temp.migration_trusted_recharge_refund_proof(
               o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
               d.sale_amount_cents-COALESCE(dr.processing_cost_cents,0),
               o.actual_payment_channel,o.payment_method,o.settlement_snapshot_jsonb
           )
        UNION
        SELECT CASE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
                   WHEN 'wechat_pay' THEN 'wechat'
                   WHEN 'wechat_jsapi' THEN 'wechat'
                   WHEN 'wechat_native' THEN 'wechat'
                   WHEN 'xunhupay_manual' THEN 'xunhupay'
                   WHEN 'xunhupay_callback' THEN 'xunhupay'
                   ELSE LOWER(BTRIM(o.settlement_snapshot_jsonb->>'refund_provider'))
               END,
               BTRIM(o.settlement_snapshot_jsonb->>'provider_refund_id'),
               'consumer',c.case_id,
               CASE WHEN c.internal_settled_at IS NOT NULL
                    THEN c.refund_amount_cents ELSE s.sale_amount_cents END::bigint
          FROM dealer_consumer_sales s
          JOIN recharge_orders o ON o.id=s.order_id
          JOIN consumer_refund_cases c ON c.source_order_id=s.order_id
         WHERE s.state='refunded'
           AND CASE WHEN c.internal_settled_at IS NOT NULL
                    THEN c.refund_amount_cents ELSE s.sale_amount_cents END > 0
           AND pg_temp.migration_trusted_recharge_refund_proof(
               o.refund_status,o.refund_completed_at,o.refunded_amount_cents,
               CASE WHEN c.internal_settled_at IS NOT NULL
                    THEN c.refund_amount_cents ELSE s.sale_amount_cents END,
               o.actual_payment_channel,o.payment_method,o.settlement_snapshot_jsonb
           )
        UNION
        SELECT t.provider,t.external_refund_id,'consumer',t.case_id,t.amount_cents
          FROM pg_temp.migration_trusted_completed_cash_jobs t
    )
    SELECT string_agg(p.refund_scope || ':' || p.local_ref, ', ') INTO bad
      FROM completed_proofs p
      LEFT JOIN external_refund_proof_registry g
        ON g.provider=p.provider AND g.external_refund_id=p.external_refund_id
       AND g.refund_scope=p.refund_scope AND g.local_ref=p.local_ref
       AND g.amount_cents=p.amount_cents
     WHERE p.provider IS NULL OR p.provider='' OR p.external_refund_id=''
        OR p.amount_cents<=0 OR g.proof_id IS NULL;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'external refund proof registry coverage mismatch: %', bad;
    END IF;
END $$;
