-- Durable billing idempotency/refund lifecycle and per-charge debt-offset outbox.
-- Additive only.  Safe omissions/weak constraints are repaired; incompatible
-- types or dirty identity rows fail closed instead of accepting a fake schema.

BEGIN;

CREATE TABLE IF NOT EXISTS billing_deduction_idempotency (
    idempotency_key     TEXT PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    feature_code        TEXT NOT NULL,
    total_cost          BIGINT NOT NULL,
    extra_cost          BIGINT NOT NULL DEFAULT 0,
    brand_id            INTEGER,
    request_fingerprint CHAR(64) NOT NULL,
    status              TEXT NOT NULL DEFAULT 'in_progress',
    response_jsonb      JSONB,
    charge_tx_id        BIGINT,
    ledger_type         TEXT,
    deducted            BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    refund_pending_at   TIMESTAMPTZ,
    refunded_at         TIMESTAMPTZ
);

ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS feature_code TEXT;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS total_cost BIGINT;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS extra_cost BIGINT DEFAULT 0;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS request_fingerprint CHAR(64);
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'in_progress';
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS response_jsonb JSONB;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS charge_tx_id BIGINT;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS ledger_type TEXT;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS deducted BIGINT;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS refund_pending_at TIMESTAMPTZ;
ALTER TABLE billing_deduction_idempotency ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;

DO $$
DECLARE
    mismatch TEXT;
BEGIN
    SELECT string_agg(a.attname || '=' || pg_catalog.format_type(a.atttypid, a.atttypmod), ', ')
      INTO mismatch
      FROM pg_attribute a
      JOIN (VALUES
          ('idempotency_key','text'), ('user_id','integer'), ('feature_code','text'),
          ('total_cost','bigint'), ('extra_cost','bigint'), ('brand_id','integer'),
          ('request_fingerprint','character(64)'), ('status','text'),
          ('response_jsonb','jsonb'), ('charge_tx_id','bigint'), ('ledger_type','text'),
          ('deducted','bigint'), ('created_at','timestamp with time zone'),
          ('updated_at','timestamp with time zone'), ('completed_at','timestamp with time zone'),
          ('refund_pending_at','timestamp with time zone'), ('refunded_at','timestamp with time zone')
      ) expected(name, typ) ON expected.name = a.attname
     WHERE a.attrelid='billing_deduction_idempotency'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped
       AND pg_catalog.format_type(a.atttypid, a.atttypmod) <> expected.typ;
    IF mismatch IS NOT NULL THEN
        RAISE EXCEPTION 'billing_deduction_idempotency incompatible column types: %', mismatch;
    END IF;
END $$;

DO $$
DECLARE
    check_row RECORD;
BEGIN
    -- Old releases allowed only in_progress/completed.  Remove CHECKs before
    -- translating completed -> charged, then recreate/validate the exact
    -- current set below.
    FOR check_row IN
        SELECT conname FROM pg_constraint
         WHERE conrelid='billing_deduction_idempotency'::regclass AND contype='c'
    LOOP
        EXECUTE format('ALTER TABLE billing_deduction_idempotency DROP CONSTRAINT %I', check_row.conname);
    END LOOP;
END $$;

UPDATE billing_deduction_idempotency
   SET status='charged', updated_at=NOW()
 WHERE status='completed';

DO $$
DECLARE
    dirty BIGINT;
    pk RECORD;
    check_row RECORD;
BEGIN
    SELECT COUNT(*) INTO dirty
      FROM billing_deduction_idempotency
     WHERE idempotency_key IS NULL OR user_id IS NULL OR feature_code IS NULL
        OR total_cost IS NULL OR extra_cost IS NULL OR request_fingerprint IS NULL
        OR status IS NULL OR created_at IS NULL OR updated_at IS NULL;
    IF dirty > 0 THEN
        RAISE EXCEPTION 'billing_deduction_idempotency has % incomplete identity rows', dirty;
    END IF;
    SELECT COUNT(*) - COUNT(DISTINCT idempotency_key) INTO dirty
      FROM billing_deduction_idempotency;
    IF dirty > 0 THEN
        RAISE EXCEPTION 'billing_deduction_idempotency has % duplicate keys', dirty;
    END IF;

    FOR pk IN
        SELECT conname FROM pg_constraint
         WHERE conrelid='billing_deduction_idempotency'::regclass AND contype='p'
    LOOP
        EXECUTE format('ALTER TABLE billing_deduction_idempotency DROP CONSTRAINT %I', pk.conname);
    END LOOP;
    FOR check_row IN
        SELECT conname FROM pg_constraint
         WHERE conrelid='billing_deduction_idempotency'::regclass AND contype='c'
    LOOP
        EXECUTE format('ALTER TABLE billing_deduction_idempotency DROP CONSTRAINT %I', check_row.conname);
    END LOOP;
END $$;

ALTER TABLE billing_deduction_idempotency
    ALTER COLUMN idempotency_key DROP DEFAULT,
    ALTER COLUMN idempotency_key SET NOT NULL,
    ALTER COLUMN user_id DROP DEFAULT,
    ALTER COLUMN user_id SET NOT NULL,
    ALTER COLUMN feature_code DROP DEFAULT,
    ALTER COLUMN feature_code SET NOT NULL,
    ALTER COLUMN total_cost DROP DEFAULT,
    ALTER COLUMN total_cost SET NOT NULL,
    ALTER COLUMN extra_cost SET DEFAULT 0,
    ALTER COLUMN extra_cost SET NOT NULL,
    ALTER COLUMN brand_id DROP DEFAULT,
    ALTER COLUMN brand_id DROP NOT NULL,
    ALTER COLUMN request_fingerprint DROP DEFAULT,
    ALTER COLUMN request_fingerprint SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'in_progress',
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN response_jsonb DROP DEFAULT,
    ALTER COLUMN response_jsonb DROP NOT NULL,
    ALTER COLUMN charge_tx_id DROP DEFAULT,
    ALTER COLUMN charge_tx_id DROP NOT NULL,
    ALTER COLUMN ledger_type DROP DEFAULT,
    ALTER COLUMN ledger_type DROP NOT NULL,
    ALTER COLUMN deducted DROP DEFAULT,
    ALTER COLUMN deducted DROP NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET NOT NULL,
    ALTER COLUMN completed_at DROP DEFAULT,
    ALTER COLUMN completed_at DROP NOT NULL,
    ALTER COLUMN refund_pending_at DROP DEFAULT,
    ALTER COLUMN refund_pending_at DROP NOT NULL,
    ALTER COLUMN refunded_at DROP DEFAULT,
    ALTER COLUMN refunded_at DROP NOT NULL;

ALTER TABLE billing_deduction_idempotency
    ADD CONSTRAINT billing_deduction_idempotency_pkey PRIMARY KEY (idempotency_key),
    ADD CONSTRAINT billing_deduction_idempotency_total_cost_check
        CHECK (total_cost >= 0) NOT VALID,
    ADD CONSTRAINT billing_deduction_idempotency_fingerprint_check
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$') NOT VALID,
    ADD CONSTRAINT billing_deduction_idempotency_status_check
        CHECK (status IN ('in_progress','charged','refund_pending','refunded')) NOT VALID,
    ADD CONSTRAINT billing_deduction_idempotency_ledger_check
        CHECK (ledger_type IS NULL OR ledger_type IN ('legacy','v35')) NOT VALID,
    ADD CONSTRAINT billing_deduction_idempotency_deducted_check
        CHECK (deducted IS NULL OR deducted >= 0) NOT VALID,
    ADD CONSTRAINT billing_deduction_idempotency_terminal_identity_check
        CHECK (
            status='in_progress' OR
            (response_jsonb IS NOT NULL AND charge_tx_id IS NOT NULL
             AND ledger_type IN ('legacy','v35') AND deducted > 0
             AND completed_at IS NOT NULL)
        ) NOT VALID,
    ADD CONSTRAINT billing_deduction_idempotency_refund_timestamp_check
        CHECK (
            (status <> 'refund_pending' OR refund_pending_at IS NOT NULL)
            AND (status <> 'refunded' OR refunded_at IS NOT NULL)
        ) NOT VALID;

ALTER TABLE billing_deduction_idempotency VALIDATE CONSTRAINT billing_deduction_idempotency_total_cost_check;
ALTER TABLE billing_deduction_idempotency VALIDATE CONSTRAINT billing_deduction_idempotency_fingerprint_check;
ALTER TABLE billing_deduction_idempotency VALIDATE CONSTRAINT billing_deduction_idempotency_status_check;
ALTER TABLE billing_deduction_idempotency VALIDATE CONSTRAINT billing_deduction_idempotency_ledger_check;
ALTER TABLE billing_deduction_idempotency VALIDATE CONSTRAINT billing_deduction_idempotency_deducted_check;
ALTER TABLE billing_deduction_idempotency VALIDATE CONSTRAINT billing_deduction_idempotency_terminal_identity_check;
ALTER TABLE billing_deduction_idempotency VALIDATE CONSTRAINT billing_deduction_idempotency_refund_timestamp_check;

ALTER TABLE billing_deduction_idempotency
    DROP CONSTRAINT IF EXISTS idx_billing_deduction_idempotency_created;
-- @drop-index-guard idx_billing_deduction_idempotency_created ON billing_deduction_idempotency if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_billing_deduction_idempotency_created' AND i.indrelid = to_regclass('billing_deduction_idempotency')) THEN
        DROP INDEX IF EXISTS idx_billing_deduction_idempotency_created;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_billing_deduction_idempotency_created' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] idx_billing_deduction_idempotency_created 不在 billing_deduction_idempotency 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_billing_deduction_idempotency_created' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
CREATE INDEX idx_billing_deduction_idempotency_created
    ON billing_deduction_idempotency(created_at DESC);
ALTER TABLE billing_deduction_idempotency
    DROP CONSTRAINT IF EXISTS idx_billing_deduction_charge_identity;
-- @drop-index-guard idx_billing_deduction_charge_identity ON billing_deduction_idempotency if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_billing_deduction_charge_identity' AND i.indrelid = to_regclass('billing_deduction_idempotency')) THEN
        DROP INDEX IF EXISTS idx_billing_deduction_charge_identity;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_billing_deduction_charge_identity' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] idx_billing_deduction_charge_identity 不在 billing_deduction_idempotency 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_billing_deduction_charge_identity' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
CREATE UNIQUE INDEX idx_billing_deduction_charge_identity
    ON billing_deduction_idempotency(user_id, feature_code, ledger_type, charge_tx_id)
    WHERE charge_tx_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS billing_debt_offset_outbox (
    event_key          TEXT PRIMARY KEY,
    user_id            INTEGER NOT NULL,
    feature_code       TEXT NOT NULL,
    charge_tx_id       BIGINT NOT NULL,
    ledger_type        TEXT NOT NULL,
    consumed_points    BIGINT NOT NULL,
    idempotency_key    TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
    retry_count        SMALLINT NOT NULL DEFAULT 0,
    next_retry_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error         TEXT,
    result_jsonb       JSONB,
    refunded_points    BIGINT NOT NULL DEFAULT 0,
    reversed_points    BIGINT NOT NULL DEFAULT 0,
    reversal_jsonb     JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at       TIMESTAMPTZ,
    cancelled_at       TIMESTAMPTZ,
    reversed_at        TIMESTAMPTZ
);

ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS event_key TEXT;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS feature_code TEXT;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS charge_tx_id BIGINT;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS ledger_type TEXT;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS consumed_points BIGINT;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending';
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS retry_count SMALLINT DEFAULT 0;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS result_jsonb JSONB;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS refunded_points BIGINT DEFAULT 0;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS reversed_points BIGINT DEFAULT 0;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS reversal_jsonb JSONB;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE billing_debt_offset_outbox ADD COLUMN IF NOT EXISTS reversed_at TIMESTAMPTZ;

-- These counters did not exist before this migration.  NULL on a legacy
-- pending/completed/manual row has one safe interpretation: no recorded
-- refund/reversal yet.  Terminal cancelled/reversed rows are never guessed.
UPDATE billing_debt_offset_outbox
   SET refunded_points=COALESCE(refunded_points,0),
       reversed_points=COALESCE(reversed_points,0)
 WHERE status IN ('pending','completed','manual')
   AND (refunded_points IS NULL OR reversed_points IS NULL);

DO $$
DECLARE
    mismatch TEXT;
    dirty BIGINT;
    c RECORD;
BEGIN
    SELECT string_agg(a.attname || '=' || pg_catalog.format_type(a.atttypid, a.atttypmod), ', ')
      INTO mismatch
      FROM pg_attribute a
      JOIN (VALUES
          ('event_key','text'), ('user_id','integer'), ('feature_code','text'),
          ('charge_tx_id','bigint'), ('ledger_type','text'), ('consumed_points','bigint'),
          ('idempotency_key','text'), ('status','text'), ('retry_count','smallint'),
           ('next_retry_at','timestamp with time zone'), ('last_error','text'),
           ('result_jsonb','jsonb'), ('refunded_points','bigint'),
           ('reversed_points','bigint'), ('reversal_jsonb','jsonb'),
           ('created_at','timestamp with time zone'),
           ('updated_at','timestamp with time zone'), ('completed_at','timestamp with time zone'),
           ('cancelled_at','timestamp with time zone'), ('reversed_at','timestamp with time zone')
      ) expected(name, typ) ON expected.name = a.attname
     WHERE a.attrelid='billing_debt_offset_outbox'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped
       AND pg_catalog.format_type(a.atttypid, a.atttypmod) <> expected.typ;
    IF mismatch IS NOT NULL THEN
        RAISE EXCEPTION 'billing_debt_offset_outbox incompatible column types: %', mismatch;
    END IF;
    SELECT COUNT(*) INTO dirty FROM billing_debt_offset_outbox
     WHERE event_key IS NULL OR user_id IS NULL OR feature_code IS NULL
        OR charge_tx_id IS NULL OR ledger_type IS NULL OR consumed_points IS NULL
        OR status IS NULL OR retry_count IS NULL OR next_retry_at IS NULL
        OR refunded_points IS NULL OR reversed_points IS NULL
        OR created_at IS NULL OR updated_at IS NULL;
    IF dirty > 0 THEN
        RAISE EXCEPTION 'billing_debt_offset_outbox has % incomplete rows', dirty;
    END IF;
    SELECT COUNT(*) - COUNT(DISTINCT event_key) INTO dirty FROM billing_debt_offset_outbox;
    IF dirty > 0 THEN
        RAISE EXCEPTION 'billing_debt_offset_outbox has % duplicate event keys', dirty;
    END IF;
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid='billing_debt_offset_outbox'::regclass AND contype IN ('p','c')
    LOOP
        EXECUTE format('ALTER TABLE billing_debt_offset_outbox DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;

ALTER TABLE billing_debt_offset_outbox
    ALTER COLUMN event_key DROP DEFAULT,
    ALTER COLUMN event_key SET NOT NULL,
    ALTER COLUMN user_id DROP DEFAULT,
    ALTER COLUMN user_id SET NOT NULL,
    ALTER COLUMN feature_code DROP DEFAULT,
    ALTER COLUMN feature_code SET NOT NULL,
    ALTER COLUMN charge_tx_id DROP DEFAULT,
    ALTER COLUMN charge_tx_id SET NOT NULL,
    ALTER COLUMN ledger_type DROP DEFAULT,
    ALTER COLUMN ledger_type SET NOT NULL,
    ALTER COLUMN consumed_points DROP DEFAULT,
    ALTER COLUMN consumed_points SET NOT NULL,
    ALTER COLUMN idempotency_key DROP DEFAULT,
    ALTER COLUMN idempotency_key DROP NOT NULL,
    ALTER COLUMN status SET DEFAULT 'pending',
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN retry_count SET DEFAULT 0,
    ALTER COLUMN retry_count SET NOT NULL,
    ALTER COLUMN next_retry_at SET DEFAULT NOW(),
    ALTER COLUMN next_retry_at SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET NOT NULL,
    ALTER COLUMN last_error DROP DEFAULT,
    ALTER COLUMN last_error DROP NOT NULL,
    ALTER COLUMN result_jsonb DROP DEFAULT,
    ALTER COLUMN result_jsonb DROP NOT NULL,
    ALTER COLUMN refunded_points SET DEFAULT 0,
    ALTER COLUMN refunded_points SET NOT NULL,
    ALTER COLUMN reversed_points SET DEFAULT 0,
    ALTER COLUMN reversed_points SET NOT NULL,
    ALTER COLUMN reversal_jsonb DROP DEFAULT,
    ALTER COLUMN reversal_jsonb DROP NOT NULL,
    ALTER COLUMN completed_at DROP DEFAULT,
    ALTER COLUMN completed_at DROP NOT NULL,
    ALTER COLUMN cancelled_at DROP DEFAULT,
    ALTER COLUMN cancelled_at DROP NOT NULL,
    ALTER COLUMN reversed_at DROP DEFAULT,
    ALTER COLUMN reversed_at DROP NOT NULL;

ALTER TABLE billing_debt_offset_outbox
    ADD CONSTRAINT billing_debt_offset_outbox_pkey PRIMARY KEY (event_key),
    ADD CONSTRAINT billing_debt_offset_outbox_status_check
        CHECK (status IN ('pending','completed','manual','cancelled','reversed')) NOT VALID,
    ADD CONSTRAINT billing_debt_offset_outbox_ledger_check
        CHECK (ledger_type IN ('legacy','v35')) NOT VALID,
    ADD CONSTRAINT billing_debt_offset_outbox_points_check
        CHECK (consumed_points > 0) NOT VALID,
    ADD CONSTRAINT billing_debt_offset_outbox_retry_check
        CHECK (retry_count >= 0) NOT VALID,
    ADD CONSTRAINT billing_debt_offset_outbox_refund_points_check
        CHECK (
            refunded_points >= 0 AND refunded_points <= consumed_points
            AND reversed_points >= 0 AND reversed_points <= consumed_points
        ) NOT VALID,
    ADD CONSTRAINT billing_debt_offset_outbox_event_identity_check
        CHECK (
            event_key = 'billing_debt_offset:' || ledger_type || ':' || charge_tx_id::text
        ) NOT VALID,
    ADD CONSTRAINT billing_debt_offset_outbox_terminal_check
        CHECK (
            (status <> 'completed' OR (result_jsonb IS NOT NULL AND completed_at IS NOT NULL))
            AND (status <> 'manual' OR last_error IS NOT NULL)
            AND (status <> 'cancelled' OR (
                cancelled_at IS NOT NULL AND refunded_points=consumed_points
            ))
            AND (status <> 'reversed' OR (
                result_jsonb IS NOT NULL AND completed_at IS NOT NULL
                AND reversal_jsonb IS NOT NULL AND reversed_at IS NOT NULL
                AND refunded_points=consumed_points
                AND reversed_points=COALESCE((result_jsonb->>'offset_points')::bigint,0)
            ))
        ) NOT VALID;
ALTER TABLE billing_debt_offset_outbox VALIDATE CONSTRAINT billing_debt_offset_outbox_status_check;
ALTER TABLE billing_debt_offset_outbox VALIDATE CONSTRAINT billing_debt_offset_outbox_ledger_check;
ALTER TABLE billing_debt_offset_outbox VALIDATE CONSTRAINT billing_debt_offset_outbox_points_check;
ALTER TABLE billing_debt_offset_outbox VALIDATE CONSTRAINT billing_debt_offset_outbox_retry_check;
ALTER TABLE billing_debt_offset_outbox VALIDATE CONSTRAINT billing_debt_offset_outbox_refund_points_check;
ALTER TABLE billing_debt_offset_outbox VALIDATE CONSTRAINT billing_debt_offset_outbox_event_identity_check;
ALTER TABLE billing_debt_offset_outbox VALIDATE CONSTRAINT billing_debt_offset_outbox_terminal_check;

-- A previously deployed idempotency row has no trustworthy proof that the old
-- post-commit debt hook ran.  Never auto-advance it (which could double offset);
-- preserve it as an explicit manual receipt for controlled reconciliation.
INSERT INTO billing_debt_offset_outbox
    (event_key,user_id,feature_code,charge_tx_id,ledger_type,consumed_points,
     idempotency_key,status,last_error)
SELECT 'billing_debt_offset:' || ledger_type || ':' || charge_tx_id::text,
       user_id,feature_code,charge_tx_id,ledger_type,deducted,
       idempotency_key,'manual','pre_migration_charge_requires_debt_audit'
  FROM billing_deduction_idempotency
 WHERE status IN ('charged','refund_pending','refunded')
   AND deducted > 0 AND charge_tx_id IS NOT NULL
   AND ledger_type IN ('legacy','v35')
ON CONFLICT (event_key) DO NOTHING;

DO $$
DECLARE
    mismatch BIGINT;
BEGIN
    SELECT COUNT(*) INTO mismatch
      FROM billing_deduction_idempotency i
      LEFT JOIN billing_debt_offset_outbox o ON o.idempotency_key=i.idempotency_key
     WHERE i.status IN ('charged','refund_pending','refunded')
       AND i.deducted > 0
       AND (
           o.event_key IS NULL
           OR o.user_id IS DISTINCT FROM i.user_id
           OR o.feature_code IS DISTINCT FROM i.feature_code
           OR o.charge_tx_id IS DISTINCT FROM i.charge_tx_id
           OR o.ledger_type IS DISTINCT FROM i.ledger_type
           OR o.consumed_points IS DISTINCT FROM i.deducted
       );
    IF mismatch > 0 THEN
        RAISE EXCEPTION 'billing debt receipts conflict with % existing charge rows', mismatch;
    END IF;
    SELECT COUNT(*) INTO mismatch
      FROM billing_debt_offset_outbox o
      LEFT JOIN billing_deduction_idempotency i ON i.idempotency_key=o.idempotency_key
     WHERE o.idempotency_key IS NOT NULL
       AND (
           i.idempotency_key IS NULL
           OR i.status NOT IN ('charged','refund_pending','refunded')
           OR o.user_id IS DISTINCT FROM i.user_id
           OR o.feature_code IS DISTINCT FROM i.feature_code
           OR o.charge_tx_id IS DISTINCT FROM i.charge_tx_id
           OR o.ledger_type IS DISTINCT FROM i.ledger_type
           OR o.consumed_points IS DISTINCT FROM i.deducted
       );
    IF mismatch > 0 THEN
        RAISE EXCEPTION 'billing debt outbox has % orphan/conflicting idempotency references', mismatch;
    END IF;
END $$;

ALTER TABLE billing_debt_offset_outbox
    DROP CONSTRAINT IF EXISTS idx_billing_debt_offset_pending;
-- @drop-index-guard idx_billing_debt_offset_pending ON billing_debt_offset_outbox if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_billing_debt_offset_pending' AND i.indrelid = to_regclass('billing_debt_offset_outbox')) THEN
        DROP INDEX IF EXISTS idx_billing_debt_offset_pending;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_billing_debt_offset_pending' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] idx_billing_debt_offset_pending 不在 billing_debt_offset_outbox 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_billing_debt_offset_pending' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
CREATE INDEX idx_billing_debt_offset_pending
    ON billing_debt_offset_outbox(next_retry_at, created_at)
    WHERE status='pending';
ALTER TABLE billing_debt_offset_outbox
    DROP CONSTRAINT IF EXISTS idx_billing_debt_offset_charge;
-- @drop-index-guard idx_billing_debt_offset_charge ON billing_debt_offset_outbox if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_billing_debt_offset_charge' AND i.indrelid = to_regclass('billing_debt_offset_outbox')) THEN
        DROP INDEX IF EXISTS idx_billing_debt_offset_charge;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_billing_debt_offset_charge' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] idx_billing_debt_offset_charge 不在 billing_debt_offset_outbox 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_billing_debt_offset_charge' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
CREATE UNIQUE INDEX idx_billing_debt_offset_charge
    ON billing_debt_offset_outbox(ledger_type, charge_tx_id);

DO $$
DECLARE
    invalid_constraints INTEGER;
    created_index TEXT;
    charge_index TEXT;
    pending_index TEXT;
    debt_charge_index TEXT;
    created_valid BOOLEAN;
    charge_valid BOOLEAN;
    pending_valid BOOLEAN;
    debt_charge_valid BOOLEAN;
BEGIN
    SELECT COUNT(*) INTO invalid_constraints
      FROM pg_constraint
     WHERE conrelid IN (
        'billing_deduction_idempotency'::regclass,
        'billing_debt_offset_outbox'::regclass
     ) AND contype IN ('p','c') AND NOT convalidated;
    IF invalid_constraints <> 0 THEN
        RAISE EXCEPTION 'billing migration left % unvalidated constraints', invalid_constraints;
    END IF;
    SELECT p.indexdef, i.indisvalid AND i.indisready
      INTO created_index, created_valid
      FROM pg_indexes p
      JOIN pg_class c
        ON c.oid=to_regclass(format('%I.%I',p.schemaname,p.indexname))
      JOIN pg_index i ON i.indexrelid=c.oid
     WHERE p.schemaname=current_schema() AND p.indexname='idx_billing_deduction_idempotency_created'
       AND p.tablename='billing_deduction_idempotency';  -- 绑表:索引名只在 schema 内唯一,不绑表会读到别的表上的同名索引
    SELECT p.indexdef, i.indisvalid AND i.indisready AND i.indisunique
      INTO charge_index, charge_valid
      FROM pg_indexes p
      JOIN pg_class c
        ON c.oid=to_regclass(format('%I.%I',p.schemaname,p.indexname))
      JOIN pg_index i ON i.indexrelid=c.oid
     WHERE p.schemaname=current_schema() AND p.indexname='idx_billing_deduction_charge_identity'
       AND p.tablename='billing_deduction_idempotency';  -- 绑表:索引名只在 schema 内唯一,不绑表会读到别的表上的同名索引
    SELECT p.indexdef, i.indisvalid AND i.indisready
      INTO pending_index, pending_valid
      FROM pg_indexes p
      JOIN pg_class c
        ON c.oid=to_regclass(format('%I.%I',p.schemaname,p.indexname))
      JOIN pg_index i ON i.indexrelid=c.oid
     WHERE p.schemaname=current_schema() AND p.indexname='idx_billing_debt_offset_pending'
       AND p.tablename='billing_debt_offset_outbox';  -- 绑表:索引名只在 schema 内唯一,不绑表会读到别的表上的同名索引
    SELECT p.indexdef, i.indisvalid AND i.indisready AND i.indisunique
      INTO debt_charge_index, debt_charge_valid
      FROM pg_indexes p
      JOIN pg_class c
        ON c.oid=to_regclass(format('%I.%I',p.schemaname,p.indexname))
      JOIN pg_index i ON i.indexrelid=c.oid
     WHERE p.schemaname=current_schema() AND p.indexname='idx_billing_debt_offset_charge'
       AND p.tablename='billing_debt_offset_outbox';  -- 绑表:索引名只在 schema 内唯一,不绑表会读到别的表上的同名索引
    IF created_index IS NULL OR created_valid IS NOT TRUE
       OR position('(created_at DESC)' IN created_index) = 0 THEN
        RAISE EXCEPTION 'billing idempotency created_at index definition invalid: %', created_index;
    END IF;
    IF charge_index IS NULL OR charge_valid IS NOT TRUE
       OR position('UNIQUE INDEX' IN charge_index) = 0
       OR position('(user_id, feature_code, ledger_type, charge_tx_id)' IN charge_index) = 0
       OR position('(charge_tx_id IS NOT NULL)' IN charge_index) = 0 THEN
        RAISE EXCEPTION 'billing charge identity index definition invalid: %', charge_index;
    END IF;
    IF pending_index IS NULL OR pending_valid IS NOT TRUE
       OR position('(next_retry_at, created_at)' IN pending_index) = 0
       OR position('(status = ''pending''::text)' IN pending_index) = 0 THEN
        RAISE EXCEPTION 'billing debt pending index definition invalid: %', pending_index;
    END IF;
    IF debt_charge_index IS NULL OR debt_charge_valid IS NOT TRUE
       OR position('(ledger_type, charge_tx_id)' IN debt_charge_index) = 0 THEN
        RAISE EXCEPTION 'billing debt charge index definition invalid: %', debt_charge_index;
    END IF;
END $$;

COMMIT;

-- Rollback is deployment-gated and intentionally manual: these rows are
-- financial replay/recovery evidence and must never be dropped automatically.
