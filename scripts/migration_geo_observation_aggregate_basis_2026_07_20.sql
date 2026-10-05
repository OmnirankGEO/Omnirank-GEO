-- Stable GEO observation aggregate policy basis + durable refresh manifest.
-- Additive only. Existing aggregate rows remain NULL-basis and are never
-- eligible for product reads or activation.

ALTER TABLE public.geo_observation_policy
    ADD COLUMN IF NOT EXISTS active_aggregate_policy_basis TEXT;

ALTER TABLE public.geo_observation_aggregates
    ADD COLUMN IF NOT EXISTS policy_basis_hash TEXT;
ALTER TABLE public.geo_observation_aggregates
    ADD COLUMN IF NOT EXISTS eligibility_epoch BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.geo_observation_aggregates
    ADD COLUMN IF NOT EXISTS promotion_sequence_watermark BIGINT NOT NULL DEFAULT 0;

CREATE SEQUENCE IF NOT EXISTS public.geo_observation_promotion_seq
    AS BIGINT INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807
    START WITH 1 CACHE 1 NO CYCLE;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_sequences
         WHERE schemaname='public' AND sequencename='geo_observation_promotion_seq'
           AND data_type::text='bigint' AND start_value=1 AND min_value=1
           AND max_value=9223372036854775807 AND increment_by=1
           AND cycle=FALSE AND cache_size=1
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_class cls JOIN pg_namespace n ON n.oid=cls.relnamespace
         WHERE n.nspname='public' AND cls.relname='geo_observation_promotion_seq'
           AND cls.relkind='S' AND cls.relpersistence='p'
    ) THEN
        RAISE EXCEPTION 'public.geo_observation_promotion_seq 定义/持久性漂移';
    END IF;
END $$;
ALTER TABLE public.geo_observation_events
    ADD COLUMN IF NOT EXISTS promotion_seq BIGINT;
UPDATE public.geo_observation_events
   SET promotion_seq=nextval('public.geo_observation_promotion_seq'::regclass)
 WHERE processing_state='promoted' AND promotion_seq IS NULL;
SELECT setval(
    'public.geo_observation_promotion_seq',
    GREATEST(
        1,
        COALESCE((SELECT MAX(promotion_seq) FROM public.geo_observation_events),0),
        (SELECT last_value FROM public.geo_observation_promotion_seq)
    ),
    TRUE
);

CREATE TABLE IF NOT EXISTS public.geo_observation_eligibility_epoch (
    scope_type TEXT PRIMARY KEY,
    epoch BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_geo_obs_eligibility_epoch_scope
        CHECK (scope_type IN ('private_brand','public_industry')),
    CONSTRAINT chk_geo_obs_eligibility_epoch_nonnegative
        CHECK (epoch >= 0)
);
INSERT INTO public.geo_observation_eligibility_epoch(scope_type)
VALUES ('private_brand'),('public_industry')
ON CONFLICT (scope_type) DO NOTHING;

-- Eligibility invalidation is bucket-scoped.  The legacy two-row global epoch
-- remains for backward-compatible audit/readiness only; product aggregates do
-- not use it.  A withdrawal dirties exactly the day/week/month buckets that
-- contained the event, so unrelated history remains readable.
CREATE TABLE IF NOT EXISTS public.geo_observation_aggregate_bucket_revision (
    scope_type TEXT NOT NULL,
    bucket_granularity TEXT NOT NULL,
    bucket_start DATE NOT NULL,
    epoch BIGINT NOT NULL DEFAULT 0,
    dirty BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT geo_observation_aggregate_bucket_revision_pkey
        PRIMARY KEY (scope_type,bucket_granularity,bucket_start),
    CONSTRAINT chk_geo_obs_bucket_revision_scope
        CHECK (scope_type IN ('private_brand','public_industry')),
    CONSTRAINT chk_geo_obs_bucket_revision_granularity
        CHECK (bucket_granularity IN ('day','week','month')),
    CONSTRAINT chk_geo_obs_bucket_revision_epoch CHECK (epoch >= 0)
);

ALTER TABLE public.geo_observation_aggregate_bucket_revision
    ADD COLUMN IF NOT EXISTS published_receipt JSONB;

-- @index-guard idx_geo_obs_bucket_revision_dirty ON geo_observation_aggregate_bucket_revision plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_bucket_revision_dirty' AND i.indrelid = to_regclass('public.geo_observation_aggregate_bucket_revision')) THEN
        NULL;  -- 已在 public.geo_observation_aggregate_bucket_revision 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_bucket_revision_dirty' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_bucket_revision_dirty 已存在但不在 public.geo_observation_aggregate_bucket_revision 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_bucket_revision_dirty' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_bucket_revision_dirty ON public.geo_observation_aggregate_bucket_revision (dirty,bucket_start,scope_type,bucket_granularity) WHERE dirty=TRUE;
    END IF;
END $idxguard$;
-- @index-guard idx_geo_obs_bucket_revision_receipt_gin ON geo_observation_aggregate_bucket_revision plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_bucket_revision_receipt_gin' AND i.indrelid = to_regclass('public.geo_observation_aggregate_bucket_revision')) THEN
        NULL;  -- 已在 public.geo_observation_aggregate_bucket_revision 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_bucket_revision_receipt_gin' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_bucket_revision_receipt_gin 已存在但不在 public.geo_observation_aggregate_bucket_revision 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_bucket_revision_receipt_gin' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_bucket_revision_receipt_gin ON public.geo_observation_aggregate_bucket_revision USING gin (published_receipt jsonb_path_ops) WHERE published_receipt IS NOT NULL;
    END IF;
END $idxguard$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='chk_geo_obs_policy_active_aggregate_basis'
           AND conrelid='public.geo_observation_policy'::regclass
    ) THEN
        ALTER TABLE public.geo_observation_policy
            ADD CONSTRAINT chk_geo_obs_policy_active_aggregate_basis
            CHECK (active_aggregate_policy_basis IS NULL OR
                   active_aggregate_policy_basis ~ '^[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='chk_geo_obs_agg_policy_basis'
           AND conrelid='public.geo_observation_aggregates'::regclass
    ) THEN
        ALTER TABLE public.geo_observation_aggregates
            ADD CONSTRAINT chk_geo_obs_agg_policy_basis
            CHECK (policy_basis_hash IS NULL OR policy_basis_hash ~ '^[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='chk_geo_obs_agg_eligibility_epoch'
           AND conrelid='public.geo_observation_aggregates'::regclass
    ) THEN
        ALTER TABLE public.geo_observation_aggregates
            ADD CONSTRAINT chk_geo_obs_agg_eligibility_epoch
            CHECK (eligibility_epoch >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='chk_geo_obs_event_promotion_seq'
           AND conrelid='public.geo_observation_events'::regclass
    ) THEN
        ALTER TABLE public.geo_observation_events
            ADD CONSTRAINT chk_geo_obs_event_promotion_seq
            CHECK ((processing_state = 'promoted' AND promotion_seq IS NOT NULL)
                   OR (processing_state = 'withdrawn')
                   OR (processing_state NOT IN ('promoted','withdrawn') AND promotion_seq IS NULL));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='chk_geo_obs_agg_promotion_sequence_watermark'
           AND conrelid='public.geo_observation_aggregates'::regclass
    ) THEN
        ALTER TABLE public.geo_observation_aggregates
            ADD CONSTRAINT chk_geo_obs_agg_promotion_sequence_watermark
            CHECK (promotion_sequence_watermark >= 0);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.geo_obs_assign_promotion_seq()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' AND NEW.promotion_seq IS NOT NULL THEN
        RAISE EXCEPTION 'promotion_seq is database assigned';
    END IF;
    IF TG_OP='UPDATE' AND NEW.promotion_seq IS DISTINCT FROM OLD.promotion_seq THEN
        RAISE EXCEPTION 'promotion_seq is immutable';
    END IF;
    IF TG_OP='UPDATE' AND OLD.processing_state='withdrawn'
       AND NEW.processing_state <> 'withdrawn' THEN
        RAISE EXCEPTION 'withdrawn observation is terminal; append a revision';
    END IF;
    IF TG_OP='UPDATE' AND OLD.processing_state='promoted'
       AND NEW.processing_state NOT IN ('promoted','withdrawn') THEN
        RAISE EXCEPTION 'promoted observation may only remain promoted or become withdrawn';
    END IF;
    IF TG_OP='UPDATE' AND OLD.processing_state <> 'promoted'
       AND NEW.processing_state='promoted' AND OLD.promotion_seq IS NOT NULL THEN
        RAISE EXCEPTION 'first promotion requires an unassigned sequence';
    END IF;
    IF NEW.processing_state='promoted' AND NEW.promotion_seq IS NULL THEN
        NEW.promotion_seq := nextval('public.geo_observation_promotion_seq'::regclass);
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_geo_obs_assign_promotion_seq ON public.geo_observation_events;
CREATE TRIGGER trg_geo_obs_assign_promotion_seq
BEFORE INSERT OR UPDATE OF processing_state,promotion_seq ON public.geo_observation_events
FOR EACH ROW EXECUTE FUNCTION public.geo_obs_assign_promotion_seq();

CREATE OR REPLACE FUNCTION public.geo_obs_reject_published_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' AND OLD.processing_state IN ('promoted','withdrawn') THEN
        RAISE EXCEPTION 'published observation cannot be deleted; withdraw and append a revision';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;

    IF OLD.processing_state='promoted' THEN
        IF NEW.processing_state='promoted' THEN
            IF (to_jsonb(NEW)-ARRAY['updated_at','legal_hold','retention_until']) IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['updated_at','legal_hold','retention_until']) THEN
                RAISE EXCEPTION 'promoted observation business fields are immutable';
            END IF;
        ELSIF NEW.processing_state='withdrawn' THEN
            IF (to_jsonb(NEW)-ARRAY['processing_state','withdrawn_at','lease_token','lease_until',
                                    'rejection_codes','updated_at','legal_hold']) IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['processing_state','withdrawn_at','lease_token','lease_until',
                                    'rejection_codes','updated_at','legal_hold']) THEN
                RAISE EXCEPTION 'withdrawal cannot rewrite promoted observation inputs';
            END IF;
        ELSE
            RAISE EXCEPTION 'promoted observation may only remain promoted or become withdrawn';
        END IF;
    ELSIF OLD.processing_state='withdrawn' THEN
        IF NEW.processing_state <> 'withdrawn' THEN
            RAISE EXCEPTION 'withdrawn observation is terminal; append a revision';
        END IF;
        IF (to_jsonb(NEW)-ARRAY['owner_user_id','brand_id','answer_hash','prompt_fingerprint',
                                'updated_at','legal_hold']) IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['owner_user_id','brand_id','answer_hash','prompt_fingerprint',
                                'updated_at','legal_hold']) THEN
            RAISE EXCEPTION 'withdrawn observation only permits retention anonymization';
        END IF;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_geo_obs_event_published_immutable ON public.geo_observation_events;
CREATE TRIGGER trg_geo_obs_event_published_immutable
BEFORE UPDATE OR DELETE ON public.geo_observation_events
FOR EACH ROW EXECUTE FUNCTION public.geo_obs_reject_published_event_mutation();

-- @index-guard uq_geo_obs_event_promotion_seq ON geo_observation_events unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_event_promotion_seq' AND i.indrelid = to_regclass('public.geo_observation_events')) THEN
        NULL;  -- 已在 public.geo_observation_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_event_promotion_seq' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_event_promotion_seq 已存在但不在 public.geo_observation_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_event_promotion_seq' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_event_promotion_seq ON public.geo_observation_events (promotion_seq) WHERE promotion_seq IS NOT NULL;
    END IF;
END $idxguard$;

CREATE OR REPLACE FUNCTION public.geo_obs_reject_promoted_input_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    event_state TEXT;
BEGIN
    SELECT processing_state INTO event_state
      FROM public.geo_observation_events WHERE id=OLD.event_id;
    IF event_state='promoted' THEN
        RAISE EXCEPTION 'promoted observation input is immutable; withdraw event and append a revision';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_geo_obs_signal_promoted_immutable
    ON public.geo_observation_signals;
CREATE TRIGGER trg_geo_obs_signal_promoted_immutable
BEFORE UPDATE OR DELETE ON public.geo_observation_signals
FOR EACH ROW EXECUTE FUNCTION public.geo_obs_reject_promoted_input_mutation();

DROP TRIGGER IF EXISTS trg_geo_obs_bucket_promoted_immutable
    ON public.geo_observation_contributor_buckets;
CREATE TRIGGER trg_geo_obs_bucket_promoted_immutable
BEFORE UPDATE OR DELETE ON public.geo_observation_contributor_buckets
FOR EACH ROW EXECUTE FUNCTION public.geo_obs_reject_promoted_input_mutation();

CREATE OR REPLACE FUNCTION public.geo_obs_mark_aggregate_buckets_dirty(
    p_scope_type TEXT,
    p_observed_at TIMESTAMPTZ
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    day_start DATE := (p_observed_at AT TIME ZONE 'UTC')::date;
BEGIN
    IF p_scope_type NOT IN ('private_brand','public_industry') OR p_observed_at IS NULL THEN
        RAISE EXCEPTION 'invalid aggregate bucket invalidation';
    END IF;
    INSERT INTO public.geo_observation_aggregate_bucket_revision AS revision(
        scope_type,bucket_granularity,bucket_start,epoch,dirty,updated_at
    ) VALUES
        (p_scope_type,'day',day_start,1,TRUE,NOW()),
        (p_scope_type,'week',date_trunc('week',p_observed_at AT TIME ZONE 'UTC')::date,1,TRUE,NOW()),
        (p_scope_type,'month',date_trunc('month',p_observed_at AT TIME ZONE 'UTC')::date,1,TRUE,NOW())
    ON CONFLICT (scope_type,bucket_granularity,bucket_start) DO UPDATE
       SET epoch=revision.epoch+1,
           dirty=TRUE,updated_at=NOW();
END $$;

CREATE OR REPLACE FUNCTION public.geo_obs_bump_eligibility_epoch_on_event()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    invalidates BOOLEAN := FALSE;
    affects_private BOOLEAN := FALSE;
    affects_public BOOLEAN := FALSE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        invalidates := OLD.processing_state = 'promoted';
    ELSIF TG_OP = 'UPDATE' AND OLD.processing_state = 'promoted' THEN
        invalidates := NEW.processing_state <> 'promoted'
            OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
            OR NEW.brand_id IS DISTINCT FROM OLD.brand_id
            OR NEW.industry_key IS DISTINCT FROM OLD.industry_key
            OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
            OR NEW.withdrawn_at IS DISTINCT FROM OLD.withdrawn_at;
    END IF;
    IF invalidates THEN
        affects_private := OLD.owner_user_id IS NOT NULL AND OLD.brand_id IS NOT NULL;
        SELECT (OLD.source_type='research_round') OR EXISTS(
            SELECT 1 FROM public.geo_observation_contributor_buckets b WHERE b.event_id=OLD.id
        ) INTO affects_public;
        IF affects_private THEN
            PERFORM public.geo_obs_mark_aggregate_buckets_dirty('private_brand',OLD.observed_at);
        END IF;
        IF affects_public THEN
            PERFORM public.geo_obs_mark_aggregate_buckets_dirty('public_industry',OLD.observed_at);
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_geo_obs_event_eligibility_epoch ON public.geo_observation_events;
CREATE TRIGGER trg_geo_obs_event_eligibility_epoch
AFTER UPDATE OR DELETE ON public.geo_observation_events
FOR EACH ROW EXECUTE FUNCTION public.geo_obs_bump_eligibility_epoch_on_event();

CREATE OR REPLACE FUNCTION public.geo_obs_bump_public_epoch_on_bucket()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    was_promoted BOOLEAN := FALSE;
    event_observed_at TIMESTAMPTZ;
BEGIN
    SELECT processing_state='promoted',observed_at INTO was_promoted,event_observed_at
      FROM public.geo_observation_events WHERE id=OLD.event_id;
    IF COALESCE(was_promoted,FALSE) THEN
        PERFORM public.geo_obs_mark_aggregate_buckets_dirty('public_industry',event_observed_at);
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_geo_obs_bucket_eligibility_epoch
    ON public.geo_observation_contributor_buckets;
CREATE TRIGGER trg_geo_obs_bucket_eligibility_epoch
AFTER UPDATE OR DELETE ON public.geo_observation_contributor_buckets
FOR EACH ROW EXECUTE FUNCTION public.geo_obs_bump_public_epoch_on_bucket();

-- @index-guard idx_geo_obs_agg_policy_basis_scope_bucket ON geo_observation_aggregates plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_agg_policy_basis_scope_bucket' AND i.indrelid = to_regclass('public.geo_observation_aggregates')) THEN
        NULL;  -- 已在 public.geo_observation_aggregates 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_agg_policy_basis_scope_bucket' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_agg_policy_basis_scope_bucket 已存在但不在 public.geo_observation_aggregates 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_agg_policy_basis_scope_bucket' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_agg_policy_basis_scope_bucket ON public.geo_observation_aggregates (policy_basis_hash, scope_type, bucket_granularity, bucket_start);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS public.geo_observation_aggregate_refresh_manifest (
    manifest_key TEXT PRIMARY KEY,
    policy_basis_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    aggregation_version TEXT NOT NULL,
    metric_version TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    bucket_granularity TEXT NOT NULL,
    bucket_start DATE NOT NULL,
    bucket_end DATE NOT NULL,
    input_watermark TIMESTAMPTZ,
    eligibility_epoch BIGINT NOT NULL,
    promotion_sequence_watermark BIGINT NOT NULL,
    expected_scope_cell_count BIGINT NOT NULL,
    expected_scope_cell_fingerprint TEXT NOT NULL,
    aggregate_key_fingerprint TEXT NOT NULL,
    eligible_observation_count BIGINT NOT NULL,
    overall_cell_count BIGINT NOT NULL,
    aggregate_row_count BIGINT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_geo_obs_agg_manifest_basis
        CHECK (policy_basis_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_geo_obs_agg_manifest_scope
        CHECK (scope_type IN ('private_brand','public_industry')),
    CONSTRAINT chk_geo_obs_agg_manifest_granularity
        CHECK (bucket_granularity IN ('day','week','month')),
    CONSTRAINT chk_geo_obs_agg_manifest_bounds
        CHECK (bucket_end >= bucket_start),
    CONSTRAINT chk_geo_obs_agg_manifest_counts
        CHECK (eligibility_epoch >= 0 AND promotion_sequence_watermark >= 0
               AND eligible_observation_count >= 0 AND expected_scope_cell_count >= 0
               AND overall_cell_count >= 0 AND aggregate_row_count >= 0),
    CONSTRAINT chk_geo_obs_agg_manifest_fingerprints
        CHECK (expected_scope_cell_fingerprint ~ '^[0-9a-f]{64}$'
               AND aggregate_key_fingerprint ~ '^[0-9a-f]{64}$')
);

ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    ADD COLUMN IF NOT EXISTS promotion_sequence_watermark BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    ADD COLUMN IF NOT EXISTS expected_scope_cell_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    ADD COLUMN IF NOT EXISTS expected_scope_cell_fingerprint TEXT NOT NULL
        DEFAULT '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945';
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    ADD COLUMN IF NOT EXISTS aggregate_key_fingerprint TEXT NOT NULL
        DEFAULT '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945';
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    ALTER COLUMN promotion_sequence_watermark DROP DEFAULT,
    ALTER COLUMN expected_scope_cell_count DROP DEFAULT,
    ALTER COLUMN expected_scope_cell_fingerprint DROP DEFAULT,
    ALTER COLUMN aggregate_key_fingerprint DROP DEFAULT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname='chk_geo_obs_agg_manifest_fingerprints'
           AND conrelid='public.geo_observation_aggregate_refresh_manifest'::regclass
    ) THEN
        ALTER TABLE public.geo_observation_aggregate_refresh_manifest
            ADD CONSTRAINT chk_geo_obs_agg_manifest_fingerprints
            CHECK (expected_scope_cell_fingerprint ~ '^[0-9a-f]{64}$'
                   AND aggregate_key_fingerprint ~ '^[0-9a-f]{64}$');
    END IF;
END $$;

-- @index-guard uq_geo_obs_agg_manifest_cell ON geo_observation_aggregate_refresh_manifest unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_geo_obs_agg_manifest_cell' AND i.indrelid = to_regclass('public.geo_observation_aggregate_refresh_manifest')) THEN
        NULL;  -- 已在 public.geo_observation_aggregate_refresh_manifest 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_geo_obs_agg_manifest_cell' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_geo_obs_agg_manifest_cell 已存在但不在 public.geo_observation_aggregate_refresh_manifest 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_geo_obs_agg_manifest_cell' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_geo_obs_agg_manifest_cell ON public.geo_observation_aggregate_refresh_manifest (policy_basis_hash, contract_version, aggregation_version, metric_version, scope_type, bucket_granularity, bucket_start);
    END IF;
END $idxguard$;

-- @index-guard idx_geo_obs_agg_manifest_basis_scope ON geo_observation_aggregate_refresh_manifest plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_agg_manifest_basis_scope' AND i.indrelid = to_regclass('public.geo_observation_aggregate_refresh_manifest')) THEN
        NULL;  -- 已在 public.geo_observation_aggregate_refresh_manifest 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_agg_manifest_basis_scope' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_agg_manifest_basis_scope 已存在但不在 public.geo_observation_aggregate_refresh_manifest 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_agg_manifest_basis_scope' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_agg_manifest_basis_scope ON public.geo_observation_aggregate_refresh_manifest (policy_basis_hash, scope_type, bucket_granularity, bucket_start);
    END IF;
END $idxguard$;

-- Semantic insights are paid outputs and must be tied to one immutable
-- aggregate snapshot. Legacy rows stay nullable and are deliberately
-- unreadable through the new API until regenerated from a current snapshot.
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS snapshot_id CHAR(64);
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS policy_basis_hash TEXT;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS scope_type TEXT;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS bucket_granularity TEXT;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS bucket_start DATE;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS bucket_epoch BIGINT;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS promotion_sequence_watermark BIGINT;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS aggregate_input_watermark TIMESTAMPTZ;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS aggregate_contract_version TEXT;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS aggregate_aggregation_version TEXT;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS aggregate_metric_version TEXT;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS paid_call_started_at TIMESTAMPTZ;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS paid_call_unknown_at TIMESTAMPTZ;
ALTER TABLE public.geo_observation_insight_jobs
    ADD COLUMN IF NOT EXISTS budget_reserved_at TIMESTAMPTZ;

UPDATE public.geo_observation_insight_jobs
   SET budget_reserved_at=paid_call_started_at
 WHERE paid_call_started_at IS NOT NULL
   AND budget_reserved_at IS NULL;

-- Upgrade a698-era snapshot jobs before installing the stronger lineage CHECK.
-- A legacy snapshot is preserved only when exactly one published manifest can
-- prove its missing watermark/version lineage.  Missing or ambiguous evidence
-- is never guessed: the cached answer becomes unavailable and all snapshot
-- fields are cleared so it cannot be served or reused for another paid call.
WITH candidates AS (
    SELECT j.job_id,
           MIN(m.input_watermark) AS input_watermark,
           MIN(m.contract_version) AS contract_version,
           MIN(m.aggregation_version) AS aggregation_version,
           MIN(m.metric_version) AS metric_version
      FROM public.geo_observation_insight_jobs j
      JOIN public.geo_observation_aggregate_refresh_manifest m
        ON m.policy_basis_hash=j.policy_basis_hash
       AND m.scope_type=j.scope_type
       AND m.bucket_granularity=j.bucket_granularity
       AND m.bucket_start=j.bucket_start
       AND m.eligibility_epoch=j.bucket_epoch
       AND m.promotion_sequence_watermark=j.promotion_sequence_watermark
     WHERE j.snapshot_id IS NOT NULL
       AND (j.aggregate_input_watermark IS NULL
            OR j.aggregate_contract_version IS NULL
            OR j.aggregate_aggregation_version IS NULL
            OR j.aggregate_metric_version IS NULL)
       AND m.input_watermark IS NOT NULL
       AND length(m.contract_version)>0
       AND length(m.aggregation_version)>0
       AND length(m.metric_version)>0
       AND (j.aggregate_input_watermark IS NULL
            OR j.aggregate_input_watermark=m.input_watermark)
       AND (j.aggregate_contract_version IS NULL
            OR j.aggregate_contract_version=m.contract_version)
       AND (j.aggregate_aggregation_version IS NULL
            OR j.aggregate_aggregation_version=m.aggregation_version)
       AND (j.aggregate_metric_version IS NULL
            OR j.aggregate_metric_version=m.metric_version)
     GROUP BY j.job_id
    HAVING COUNT(DISTINCT ROW(
               m.input_watermark,m.contract_version,
               m.aggregation_version,m.metric_version
           ))=1
), resolved AS (
    SELECT j.job_id,c.input_watermark,c.contract_version,
           c.aggregation_version,c.metric_version,
           encode(sha256(convert_to(concat_ws(chr(31),
               j.policy_basis_hash,j.scope_type,j.bucket_granularity,
               to_char(j.bucket_start,'YYYY-MM-DD'),j.bucket_epoch::text,
               j.promotion_sequence_watermark::text,
               to_char(c.input_watermark AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               c.contract_version,c.aggregation_version,c.metric_version
           ),'UTF8')),'hex') AS canonical_snapshot_id
      FROM public.geo_observation_insight_jobs j
      JOIN candidates c ON c.job_id=j.job_id
)
UPDATE public.geo_observation_insight_jobs j
   SET aggregate_input_watermark=r.input_watermark,
       aggregate_contract_version=r.contract_version,
       aggregate_aggregation_version=r.aggregation_version,
       aggregate_metric_version=r.metric_version,
       updated_at=NOW()
  FROM resolved r
 WHERE j.job_id=r.job_id;

UPDATE public.geo_observation_insight_jobs
   SET state='result_unknown',result_state=NULL,summary=NULL,
       evidence_refs='[]'::jsonb,allowed_actions='[]'::jsonb,
       error_code='SEMANTIC_INSIGHT_UNAVAILABLE',
       error_detail='legacy snapshot lineage cannot be uniquely proven',
       snapshot_id=NULL,policy_basis_hash=NULL,scope_type=NULL,
       bucket_granularity=NULL,bucket_start=NULL,bucket_epoch=NULL,
       promotion_sequence_watermark=NULL,aggregate_input_watermark=NULL,
       aggregate_contract_version=NULL,aggregate_aggregation_version=NULL,
       aggregate_metric_version=NULL,lease_token=NULL,lease_until=NULL,
       updated_at=NOW()
 WHERE snapshot_id IS NOT NULL
   AND (aggregate_input_watermark IS NULL
        OR aggregate_contract_version IS NULL
        OR aggregate_aggregation_version IS NULL
        OR aggregate_metric_version IS NULL);

ALTER TABLE public.geo_observation_insight_jobs
    DROP CONSTRAINT IF EXISTS geo_obs_insight_state_chk;
ALTER TABLE public.geo_observation_insight_jobs
    ADD CONSTRAINT geo_obs_insight_state_chk
    CHECK (state IN ('pending','running','paid_call_started','completed','failed','result_unknown'));
UPDATE public.geo_observation_insight_jobs
   SET state='result_unknown',error_code='SEMANTIC_INSIGHT_UNAVAILABLE',
       error_detail='legacy insight has no aggregate snapshot lineage',updated_at=NOW()
 WHERE snapshot_id IS NULL
    AND (
        state IS DISTINCT FROM 'result_unknown'
        OR error_code IS DISTINCT FROM 'SEMANTIC_INSIGHT_UNAVAILABLE'
    );

-- Canonicalize already-populated pre-fix snapshots too.  The earlier Python
-- identity used ``str(timestamptz)`` and could hash the same instant differently
-- under another session timezone.  If two historical rows collapse to one
-- canonical identity, do not guess which paid answer is authoritative: make
-- every duplicate unavailable and require regeneration from the current
-- snapshot.  UNIQUE permits multiple NULLs, so this remains deterministic.
WITH normalized AS (
    SELECT j.job_id,j.owner_user_id,j.brand_id,j.input_hash,
           encode(sha256(convert_to(concat_ws(chr(31),
               j.policy_basis_hash,j.scope_type,j.bucket_granularity,
               to_char(j.bucket_start,'YYYY-MM-DD'),j.bucket_epoch::text,
               j.promotion_sequence_watermark::text,
               to_char(j.aggregate_input_watermark AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               j.aggregate_contract_version,j.aggregate_aggregation_version,
               j.aggregate_metric_version
           ),'UTF8')),'hex') AS canonical_snapshot_id
      FROM public.geo_observation_insight_jobs j
     WHERE j.snapshot_id IS NOT NULL
       AND j.policy_basis_hash IS NOT NULL AND j.scope_type IS NOT NULL
       AND j.bucket_granularity IS NOT NULL AND j.bucket_start IS NOT NULL
       AND j.bucket_epoch IS NOT NULL AND j.promotion_sequence_watermark IS NOT NULL
       AND j.aggregate_input_watermark IS NOT NULL
       AND j.aggregate_contract_version IS NOT NULL
       AND j.aggregate_aggregation_version IS NOT NULL
       AND j.aggregate_metric_version IS NOT NULL
), duplicates AS (
    SELECT owner_user_id,brand_id,input_hash,canonical_snapshot_id
      FROM normalized
     GROUP BY owner_user_id,brand_id,input_hash,canonical_snapshot_id
    HAVING COUNT(*)>1
)
UPDATE public.geo_observation_insight_jobs j
   SET state='result_unknown',result_state=NULL,summary=NULL,
       evidence_refs='[]'::jsonb,allowed_actions='[]'::jsonb,
       error_code='SEMANTIC_INSIGHT_UNAVAILABLE',
       error_detail='duplicate legacy snapshot identity is ambiguous',
       snapshot_id=NULL,policy_basis_hash=NULL,scope_type=NULL,
       bucket_granularity=NULL,bucket_start=NULL,bucket_epoch=NULL,
       promotion_sequence_watermark=NULL,aggregate_input_watermark=NULL,
       aggregate_contract_version=NULL,aggregate_aggregation_version=NULL,
       aggregate_metric_version=NULL,lease_token=NULL,lease_until=NULL,
       updated_at=NOW()
  FROM normalized n
  JOIN duplicates d
    ON d.owner_user_id=n.owner_user_id AND d.brand_id=n.brand_id
   AND d.input_hash=n.input_hash
   AND d.canonical_snapshot_id=n.canonical_snapshot_id
 WHERE j.job_id=n.job_id;

WITH normalized AS (
    SELECT j.job_id,
           encode(sha256(convert_to(concat_ws(chr(31),
               j.policy_basis_hash,j.scope_type,j.bucket_granularity,
               to_char(j.bucket_start,'YYYY-MM-DD'),j.bucket_epoch::text,
               j.promotion_sequence_watermark::text,
               to_char(j.aggregate_input_watermark AT TIME ZONE 'UTC',
                       'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               j.aggregate_contract_version,j.aggregate_aggregation_version,
               j.aggregate_metric_version
           ),'UTF8')),'hex') AS canonical_snapshot_id
      FROM public.geo_observation_insight_jobs j
     WHERE j.snapshot_id IS NOT NULL
       AND j.policy_basis_hash IS NOT NULL AND j.scope_type IS NOT NULL
       AND j.bucket_granularity IS NOT NULL AND j.bucket_start IS NOT NULL
       AND j.bucket_epoch IS NOT NULL AND j.promotion_sequence_watermark IS NOT NULL
       AND j.aggregate_input_watermark IS NOT NULL
       AND j.aggregate_contract_version IS NOT NULL
       AND j.aggregate_aggregation_version IS NOT NULL
       AND j.aggregate_metric_version IS NOT NULL
)
UPDATE public.geo_observation_insight_jobs j
   SET snapshot_id=n.canonical_snapshot_id,updated_at=NOW()
  FROM normalized n
 WHERE j.job_id=n.job_id
   AND j.snapshot_id::text IS DISTINCT FROM n.canonical_snapshot_id;

DO $$
DECLARE
    item RECORD;
    canonical_unique_exists BOOLEAN := FALSE;
BEGIN
    -- Normalize every UNIQUE constraint by its exact ordered column set, not
    -- by a conventional name.  The old three-column gate prevents legitimate
    -- cross-snapshot jobs and may exist under an arbitrary constraint name.
    FOR item IN
        SELECT c.conname,
               array_agg(a.attname ORDER BY key.ordinality) AS columns,
               array_agg(a.attname ORDER BY a.attname) AS column_set
          FROM pg_constraint c
          JOIN LATERAL unnest(c.conkey) WITH ORDINALITY key(attnum,ordinality)
            ON TRUE
          JOIN pg_attribute a
            ON a.attrelid=c.conrelid AND a.attnum=key.attnum
         WHERE c.conrelid='public.geo_observation_insight_jobs'::regclass
           AND c.contype='u'
         GROUP BY c.conname
    LOOP
        IF item.column_set = ARRAY['brand_id','input_hash','owner_user_id']::name[] THEN
            EXECUTE format(
                'ALTER TABLE public.geo_observation_insight_jobs DROP CONSTRAINT %I',
                item.conname
            );
        ELSIF item.column_set = ARRAY[
            'brand_id','input_hash','owner_user_id','snapshot_id'
        ]::name[] THEN
            IF item.conname='geo_obs_insight_scope_snapshot_uk'
               AND item.columns=ARRAY[
                   'owner_user_id','brand_id','input_hash','snapshot_id'
               ]::name[] THEN
                canonical_unique_exists := TRUE;
            ELSE
                EXECUTE format(
                    'ALTER TABLE public.geo_observation_insight_jobs DROP CONSTRAINT %I',
                    item.conname
                );
            END IF;
        ELSE
            RAISE EXCEPTION 'insight jobs 存在未知额外 UNIQUE 约束: % %',
                item.conname,item.columns;
        END IF;
    END LOOP;

    -- Raw UNIQUE indexes not owned by a constraint obey the same contract.
    FOR item IN
        SELECT idx.relname AS index_name,
               array_agg(att.attname ORDER BY key.ordinality) AS columns,
               array_agg(att.attname ORDER BY att.attname) AS column_set
          FROM pg_index i
          JOIN pg_class idx ON idx.oid=i.indexrelid
          JOIN LATERAL unnest(i.indkey::smallint[]) WITH ORDINALITY key(attnum,ordinality)
            ON key.ordinality <= i.indnkeyatts
          LEFT JOIN pg_attribute att
            ON att.attrelid=i.indrelid AND att.attnum=key.attnum
          LEFT JOIN pg_constraint c ON c.conindid=i.indexrelid
         WHERE i.indrelid='public.geo_observation_insight_jobs'::regclass
           AND i.indisunique AND NOT i.indisprimary AND c.oid IS NULL
         GROUP BY idx.relname
    LOOP
        IF item.column_set = ARRAY['brand_id','input_hash','owner_user_id']::name[]
           OR item.column_set = ARRAY[
               'brand_id','input_hash','owner_user_id','snapshot_id'
           ]::name[] THEN
            EXECUTE format('DROP INDEX public.%I',item.index_name);
        ELSE
            RAISE EXCEPTION 'insight jobs 存在未知额外 UNIQUE 索引: % %',
                item.index_name,item.columns;
        END IF;
    END LOOP;

    IF NOT canonical_unique_exists THEN
        ALTER TABLE public.geo_observation_insight_jobs
            ADD CONSTRAINT geo_obs_insight_scope_snapshot_uk
            UNIQUE (owner_user_id,brand_id,input_hash,snapshot_id);
    END IF;
END $$;

ALTER TABLE public.geo_observation_insight_jobs
    DROP CONSTRAINT IF EXISTS chk_geo_obs_insight_snapshot;
ALTER TABLE public.geo_observation_insight_jobs
    ADD CONSTRAINT chk_geo_obs_insight_snapshot CHECK (
                (snapshot_id IS NULL AND state='result_unknown'
                 AND policy_basis_hash IS NULL AND scope_type IS NULL
                 AND bucket_granularity IS NULL AND bucket_start IS NULL
                 AND bucket_epoch IS NULL AND promotion_sequence_watermark IS NULL
                 AND aggregate_input_watermark IS NULL
                 AND aggregate_contract_version IS NULL
                 AND aggregate_aggregation_version IS NULL
                 AND aggregate_metric_version IS NULL)
                OR
                (snapshot_id IS NOT NULL AND snapshot_id ~ '^[0-9a-f]{64}$'
                 AND policy_basis_hash IS NOT NULL
                 AND policy_basis_hash ~ '^[0-9a-f]{64}$'
                 AND scope_type IS NOT NULL AND scope_type='private_brand'
                 AND bucket_granularity IS NOT NULL
                 AND bucket_granularity IN ('day','week','month')
                 AND bucket_start IS NOT NULL
                 AND bucket_epoch IS NOT NULL AND bucket_epoch >= 0
                 AND promotion_sequence_watermark IS NOT NULL
                 AND promotion_sequence_watermark >= 0
                 AND aggregate_input_watermark IS NOT NULL
                 AND aggregate_contract_version IS NOT NULL
                 AND length(aggregate_contract_version) > 0
                 AND aggregate_aggregation_version IS NOT NULL
                 AND length(aggregate_aggregation_version) > 0
                 AND aggregate_metric_version IS NOT NULL
                 AND length(aggregate_metric_version) > 0)
    );
ALTER TABLE public.geo_observation_insight_jobs
    DROP CONSTRAINT IF EXISTS chk_geo_obs_insight_budget_reservation;
ALTER TABLE public.geo_observation_insight_jobs
    ADD CONSTRAINT chk_geo_obs_insight_budget_reservation CHECK (
        (budget_reserved_at IS NULL AND paid_call_started_at IS NULL)
        OR
        (budget_reserved_at IS NOT NULL AND paid_call_started_at IS NOT NULL
         AND budget_reserved_at=paid_call_started_at)
    );
-- @index-guard idx_geo_obs_insight_paid_recovery ON geo_observation_insight_jobs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geo_obs_insight_paid_recovery' AND i.indrelid = to_regclass('public.geo_observation_insight_jobs')) THEN
        NULL;  -- 已在 public.geo_observation_insight_jobs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geo_obs_insight_paid_recovery' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geo_obs_insight_paid_recovery 已存在但不在 public.geo_observation_insight_jobs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geo_obs_insight_paid_recovery' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geo_obs_insight_paid_recovery ON public.geo_observation_insight_jobs (state,lease_until) WHERE state IN ('pending','running','paid_call_started','result_unknown');
    END IF;
END $idxguard$;

ALTER TABLE public.geo_observation_policy
    VALIDATE CONSTRAINT chk_geo_obs_policy_active_aggregate_basis;
ALTER TABLE public.geo_observation_aggregates
    VALIDATE CONSTRAINT chk_geo_obs_agg_policy_basis;
ALTER TABLE public.geo_observation_aggregates
    VALIDATE CONSTRAINT chk_geo_obs_agg_eligibility_epoch;
ALTER TABLE public.geo_observation_aggregates
    VALIDATE CONSTRAINT chk_geo_obs_agg_promotion_sequence_watermark;
ALTER TABLE public.geo_observation_events
    VALIDATE CONSTRAINT chk_geo_obs_event_promotion_seq;
ALTER TABLE public.geo_observation_eligibility_epoch
    VALIDATE CONSTRAINT chk_geo_obs_eligibility_epoch_scope;
ALTER TABLE public.geo_observation_eligibility_epoch
    VALIDATE CONSTRAINT chk_geo_obs_eligibility_epoch_nonnegative;
ALTER TABLE public.geo_observation_aggregate_bucket_revision
    VALIDATE CONSTRAINT chk_geo_obs_bucket_revision_scope;
ALTER TABLE public.geo_observation_aggregate_bucket_revision
    VALIDATE CONSTRAINT chk_geo_obs_bucket_revision_granularity;
ALTER TABLE public.geo_observation_aggregate_bucket_revision
    VALIDATE CONSTRAINT chk_geo_obs_bucket_revision_epoch;
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    VALIDATE CONSTRAINT chk_geo_obs_agg_manifest_basis;
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    VALIDATE CONSTRAINT chk_geo_obs_agg_manifest_scope;
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    VALIDATE CONSTRAINT chk_geo_obs_agg_manifest_granularity;
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    VALIDATE CONSTRAINT chk_geo_obs_agg_manifest_bounds;
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    VALIDATE CONSTRAINT chk_geo_obs_agg_manifest_counts;
ALTER TABLE public.geo_observation_aggregate_refresh_manifest
    VALIDATE CONSTRAINT chk_geo_obs_agg_manifest_fingerprints;
ALTER TABLE public.geo_observation_insight_jobs
    VALIDATE CONSTRAINT geo_obs_insight_state_chk;
ALTER TABLE public.geo_observation_insight_jobs
    VALIDATE CONSTRAINT chk_geo_obs_insight_snapshot;
ALTER TABLE public.geo_observation_insight_jobs
    VALIDATE CONSTRAINT chk_geo_obs_insight_budget_reservation;

DO $$
DECLARE
    c RECORD;
    con RECORD;
    rel RECORD;
    idx TEXT;
    actual TEXT;
BEGIN
    FOR rel IN
        SELECT * FROM (VALUES
            ('geo_observation_events'),
            ('geo_observation_signals'),
            ('geo_observation_contributor_buckets'),
            ('geo_observation_audit'),
            ('geo_observation_policy'),
            ('geo_observation_gold_eval'),
            ('geo_observation_aggregates'),
            ('geo_observation_aggregate_refresh_manifest'),
            ('geo_observation_eligibility_epoch'),
            ('geo_observation_aggregate_bucket_revision'),
            ('geo_observation_insight_jobs')
        ) AS expected(table_name)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_class cls
            JOIN pg_namespace n ON n.oid=cls.relnamespace
            WHERE n.nspname='public' AND cls.relname=rel.table_name
              AND cls.relkind='r' AND cls.relpersistence='p'
        ) THEN
            RAISE EXCEPTION '观测关键真相表必须为 public permanent table: %',
                rel.table_name;
        END IF;
    END LOOP;

    FOR c IN
        SELECT * FROM (VALUES
            ('geo_observation_policy','active_aggregate_policy_basis','text','YES'),
            ('geo_observation_aggregates','policy_basis_hash','text','YES'),
            ('geo_observation_aggregates','eligibility_epoch','bigint','NO'),
            ('geo_observation_aggregates','promotion_sequence_watermark','bigint','NO'),
            ('geo_observation_events','promotion_seq','bigint','YES'),
            ('geo_observation_eligibility_epoch','scope_type','text','NO'),
            ('geo_observation_eligibility_epoch','epoch','bigint','NO'),
            ('geo_observation_eligibility_epoch','updated_at','timestamp with time zone','NO'),
            ('geo_observation_aggregate_bucket_revision','scope_type','text','NO'),
            ('geo_observation_aggregate_bucket_revision','bucket_granularity','text','NO'),
            ('geo_observation_aggregate_bucket_revision','bucket_start','date','NO'),
            ('geo_observation_aggregate_bucket_revision','epoch','bigint','NO'),
            ('geo_observation_aggregate_bucket_revision','dirty','boolean','NO'),
            ('geo_observation_aggregate_bucket_revision','updated_at','timestamp with time zone','NO'),
            ('geo_observation_aggregate_bucket_revision','published_receipt','jsonb','YES'),
            ('geo_observation_aggregate_refresh_manifest','manifest_key','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','policy_basis_hash','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','policy_version','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','contract_version','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','aggregation_version','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','metric_version','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','scope_type','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','bucket_granularity','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','bucket_start','date','NO'),
            ('geo_observation_aggregate_refresh_manifest','bucket_end','date','NO'),
            ('geo_observation_aggregate_refresh_manifest','input_watermark','timestamp with time zone','YES'),
            ('geo_observation_aggregate_refresh_manifest','eligibility_epoch','bigint','NO'),
            ('geo_observation_aggregate_refresh_manifest','promotion_sequence_watermark','bigint','NO'),
            ('geo_observation_aggregate_refresh_manifest','expected_scope_cell_count','bigint','NO'),
            ('geo_observation_aggregate_refresh_manifest','expected_scope_cell_fingerprint','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','aggregate_key_fingerprint','text','NO'),
            ('geo_observation_aggregate_refresh_manifest','eligible_observation_count','bigint','NO'),
            ('geo_observation_aggregate_refresh_manifest','overall_cell_count','bigint','NO'),
            ('geo_observation_aggregate_refresh_manifest','aggregate_row_count','bigint','NO'),
            ('geo_observation_aggregate_refresh_manifest','completed_at','timestamp with time zone','NO'),
            ('geo_observation_aggregate_refresh_manifest','created_at','timestamp with time zone','NO'),
            ('geo_observation_aggregate_refresh_manifest','updated_at','timestamp with time zone','NO')
            ,('geo_observation_insight_jobs','snapshot_id','character','YES')
            ,('geo_observation_insight_jobs','policy_basis_hash','text','YES')
            ,('geo_observation_insight_jobs','scope_type','text','YES')
            ,('geo_observation_insight_jobs','bucket_granularity','text','YES')
            ,('geo_observation_insight_jobs','bucket_start','date','YES')
            ,('geo_observation_insight_jobs','bucket_epoch','bigint','YES')
            ,('geo_observation_insight_jobs','promotion_sequence_watermark','bigint','YES')
            ,('geo_observation_insight_jobs','aggregate_input_watermark','timestamp with time zone','YES')
            ,('geo_observation_insight_jobs','aggregate_contract_version','text','YES')
            ,('geo_observation_insight_jobs','aggregate_aggregation_version','text','YES')
            ,('geo_observation_insight_jobs','aggregate_metric_version','text','YES')
            ,('geo_observation_insight_jobs','paid_call_started_at','timestamp with time zone','YES')
            ,('geo_observation_insight_jobs','paid_call_unknown_at','timestamp with time zone','YES')
            ,('geo_observation_insight_jobs','budget_reserved_at','timestamp with time zone','YES')
        ) AS expected(table_name,column_name,data_type,is_nullable)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns x
             WHERE x.table_schema='public' AND x.table_name=c.table_name
               AND x.column_name=c.column_name AND x.data_type=c.data_type
               AND x.is_nullable=c.is_nullable
        ) THEN
            RAISE EXCEPTION 'aggregate basis schema 列定义漂移: %.%', c.table_name, c.column_name;
        END IF;
    END LOOP;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='geo_observation_aggregate_refresh_manifest'
           AND column_name='created_at' AND lower(column_default)='now()'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='geo_observation_aggregate_refresh_manifest'
           AND column_name='updated_at' AND lower(column_default)='now()'
    ) THEN
        RAISE EXCEPTION 'aggregate manifest 时间列默认值漂移';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='geo_observation_aggregate_refresh_manifest'
           AND column_name IN ('promotion_sequence_watermark','expected_scope_cell_count',
                               'expected_scope_cell_fingerprint','aggregate_key_fingerprint')
           AND column_default IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'aggregate manifest snapshot 列默认值漂移';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='geo_observation_aggregates'
           AND column_name='eligibility_epoch' AND lower(column_default)='0'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='geo_observation_aggregates'
           AND column_name='promotion_sequence_watermark' AND lower(column_default)='0'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='geo_observation_eligibility_epoch'
           AND column_name='epoch' AND lower(column_default)='0'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='geo_observation_eligibility_epoch'
           AND column_name='updated_at' AND lower(column_default)='now()'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='geo_observation_aggregate_bucket_revision'
           AND column_name='epoch' AND lower(column_default)='0'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='geo_observation_aggregate_bucket_revision'
           AND column_name='dirty' AND lower(column_default)='false'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public' AND table_name='geo_observation_aggregate_bucket_revision'
           AND column_name='updated_at' AND lower(column_default)='now()'
    ) THEN
        RAISE EXCEPTION 'eligibility epoch 默认值漂移';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_sequences
         WHERE schemaname='public' AND sequencename='geo_observation_promotion_seq'
           AND data_type::text='bigint' AND start_value=1 AND min_value=1
           AND max_value=9223372036854775807 AND increment_by=1
           AND cycle=FALSE AND cache_size=1
    ) THEN
        RAISE EXCEPTION 'public.geo_observation_promotion_seq 定义漂移';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_class cls
        JOIN pg_namespace n ON n.oid=cls.relnamespace
        WHERE n.nspname='public' AND cls.relname='geo_observation_promotion_seq'
          AND cls.relkind='S' AND cls.relpersistence='p'
    ) THEN
        RAISE EXCEPTION 'public.geo_observation_promotion_seq 持久性漂移';
    END IF;
    IF NOT (SELECT is_called FROM public.geo_observation_promotion_seq)
       OR (SELECT last_value FROM public.geo_observation_promotion_seq) <
          COALESCE((SELECT MAX(promotion_seq) FROM public.geo_observation_events),0)
       OR (SELECT last_value FROM public.geo_observation_promotion_seq) >= 9223372036854775807 THEN
        RAISE EXCEPTION 'promotion sequence 下一值不能严格大于现有事件最大序列';
    END IF;

    FOR con IN
        SELECT * FROM (VALUES
            ('geo_observation_policy','chk_geo_obs_policy_active_aggregate_basis'),
            ('geo_observation_aggregates','chk_geo_obs_agg_policy_basis'),
            ('geo_observation_aggregates','chk_geo_obs_agg_eligibility_epoch'),
            ('geo_observation_aggregates','chk_geo_obs_agg_promotion_sequence_watermark'),
            ('geo_observation_events','chk_geo_obs_event_promotion_seq'),
            ('geo_observation_eligibility_epoch','geo_observation_eligibility_epoch_pkey'),
            ('geo_observation_eligibility_epoch','chk_geo_obs_eligibility_epoch_scope'),
            ('geo_observation_eligibility_epoch','chk_geo_obs_eligibility_epoch_nonnegative'),
            ('geo_observation_aggregate_bucket_revision','geo_observation_aggregate_bucket_revision_pkey'),
            ('geo_observation_aggregate_bucket_revision','chk_geo_obs_bucket_revision_scope'),
            ('geo_observation_aggregate_bucket_revision','chk_geo_obs_bucket_revision_granularity'),
            ('geo_observation_aggregate_bucket_revision','chk_geo_obs_bucket_revision_epoch'),
            ('geo_observation_aggregate_refresh_manifest','geo_observation_aggregate_refresh_manifest_pkey'),
            ('geo_observation_aggregate_refresh_manifest','chk_geo_obs_agg_manifest_basis'),
            ('geo_observation_aggregate_refresh_manifest','chk_geo_obs_agg_manifest_scope'),
            ('geo_observation_aggregate_refresh_manifest','chk_geo_obs_agg_manifest_granularity'),
            ('geo_observation_aggregate_refresh_manifest','chk_geo_obs_agg_manifest_bounds'),
            ('geo_observation_aggregate_refresh_manifest','chk_geo_obs_agg_manifest_counts'),
            ('geo_observation_aggregate_refresh_manifest','chk_geo_obs_agg_manifest_fingerprints')
            ,('geo_observation_insight_jobs','geo_obs_insight_state_chk')
            ,('geo_observation_insight_jobs','chk_geo_obs_insight_snapshot')
            ,('geo_observation_insight_jobs','chk_geo_obs_insight_budget_reservation')
            ,('geo_observation_insight_jobs','geo_obs_insight_scope_snapshot_uk')
        ) AS expected(table_name,constraint_name)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint p
             WHERE p.conname=con.constraint_name
               AND p.conrelid=('public.'||con.table_name)::regclass AND p.convalidated
        ) THEN
            RAISE EXCEPTION 'aggregate basis schema 约束缺失/未验证: %.%', con.table_name, con.constraint_name;
        END IF;
    END LOOP;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_policy_active_aggregate_basis'
       AND conrelid='public.geo_observation_policy'::regclass;
    IF actual IS DISTINCT FROM
       'check(((active_aggregate_policy_basisisnull)or(active_aggregate_policy_basis~''^[0-9a-f]{64}$''::text)))' THEN
        RAISE EXCEPTION 'chk_geo_obs_policy_active_aggregate_basis 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_policy_basis'
       AND conrelid='public.geo_observation_aggregates'::regclass;
    IF actual IS DISTINCT FROM
       'check(((policy_basis_hashisnull)or(policy_basis_hash~''^[0-9a-f]{64}$''::text)))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_policy_basis 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_eligibility_epoch'
       AND conrelid='public.geo_observation_aggregates'::regclass;
    IF actual IS DISTINCT FROM 'check((eligibility_epoch>=0))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_eligibility_epoch 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_promotion_sequence_watermark'
       AND conrelid='public.geo_observation_aggregates'::regclass;
    IF actual IS DISTINCT FROM 'check((promotion_sequence_watermark>=0))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_promotion_sequence_watermark 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_event_promotion_seq'
       AND conrelid='public.geo_observation_events'::regclass;
    IF actual IS DISTINCT FROM
       'check((((processing_state=''promoted''::text)and(promotion_seqisnotnull))or(processing_state=''withdrawn''::text)or((processing_state<>all(array[''promoted''::text,''withdrawn''::text]))and(promotion_seqisnull))))' THEN
        RAISE EXCEPTION 'chk_geo_obs_event_promotion_seq 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_eligibility_epoch_scope'
       AND conrelid='public.geo_observation_eligibility_epoch'::regclass;
    IF actual IS DISTINCT FROM
       'check((scope_type=any(array[''private_brand''::text,''public_industry''::text])))' THEN
        RAISE EXCEPTION 'chk_geo_obs_eligibility_epoch_scope 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_eligibility_epoch_nonnegative'
       AND conrelid='public.geo_observation_eligibility_epoch'::regclass;
    IF actual IS DISTINCT FROM 'check((epoch>=0))' THEN
        RAISE EXCEPTION 'chk_geo_obs_eligibility_epoch_nonnegative 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='geo_observation_aggregate_bucket_revision_pkey'
       AND conrelid='public.geo_observation_aggregate_bucket_revision'::regclass;
    IF actual IS DISTINCT FROM 'primarykey(scope_type,bucket_granularity,bucket_start)' THEN
        RAISE EXCEPTION 'bucket revision PK 定义漂移: %', actual;
    END IF;
    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_bucket_revision_scope'
       AND conrelid='public.geo_observation_aggregate_bucket_revision'::regclass;
    IF actual IS DISTINCT FROM
       'check((scope_type=any(array[''private_brand''::text,''public_industry''::text])))' THEN
        RAISE EXCEPTION 'bucket revision scope CHECK 定义漂移: %', actual;
    END IF;
    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_bucket_revision_granularity'
       AND conrelid='public.geo_observation_aggregate_bucket_revision'::regclass;
    IF actual IS DISTINCT FROM
       'check((bucket_granularity=any(array[''day''::text,''week''::text,''month''::text])))' THEN
        RAISE EXCEPTION 'bucket revision granularity CHECK 定义漂移: %', actual;
    END IF;
    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_bucket_revision_epoch'
       AND conrelid='public.geo_observation_aggregate_bucket_revision'::regclass;
    IF actual IS DISTINCT FROM 'check((epoch>=0))' THEN
        RAISE EXCEPTION 'bucket revision epoch CHECK 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='geo_observation_aggregate_refresh_manifest_pkey'
       AND conrelid='public.geo_observation_aggregate_refresh_manifest'::regclass;
    IF actual IS DISTINCT FROM 'primarykey(manifest_key)' THEN
        RAISE EXCEPTION 'manifest PK 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_manifest_basis'
       AND conrelid='public.geo_observation_aggregate_refresh_manifest'::regclass;
    IF actual IS DISTINCT FROM
       'check((policy_basis_hash~''^[0-9a-f]{64}$''::text))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_manifest_basis 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_manifest_scope'
       AND conrelid='public.geo_observation_aggregate_refresh_manifest'::regclass;
    IF actual IS DISTINCT FROM
       'check((scope_type=any(array[''private_brand''::text,''public_industry''::text])))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_manifest_scope 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_manifest_granularity'
       AND conrelid='public.geo_observation_aggregate_refresh_manifest'::regclass;
    IF actual IS DISTINCT FROM
       'check((bucket_granularity=any(array[''day''::text,''week''::text,''month''::text])))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_manifest_granularity 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_manifest_bounds'
       AND conrelid='public.geo_observation_aggregate_refresh_manifest'::regclass;
    IF actual IS DISTINCT FROM 'check((bucket_end>=bucket_start))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_manifest_bounds 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_manifest_counts'
       AND conrelid='public.geo_observation_aggregate_refresh_manifest'::regclass;
    IF actual IS DISTINCT FROM
       'check(((eligibility_epoch>=0)and(promotion_sequence_watermark>=0)and(eligible_observation_count>=0)and(expected_scope_cell_count>=0)and(overall_cell_count>=0)and(aggregate_row_count>=0)))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_manifest_counts 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_agg_manifest_fingerprints'
       AND conrelid='public.geo_observation_aggregate_refresh_manifest'::regclass;
    IF actual IS DISTINCT FROM
       'check(((expected_scope_cell_fingerprint~''^[0-9a-f]{64}$''::text)and(aggregate_key_fingerprint~''^[0-9a-f]{64}$''::text)))' THEN
        RAISE EXCEPTION 'chk_geo_obs_agg_manifest_fingerprints 定义漂移: %', actual;
    END IF;

    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='geo_obs_insight_state_chk'
       AND conrelid='public.geo_observation_insight_jobs'::regclass;
    IF actual IS DISTINCT FROM
       'check((state=any(array[''pending''::text,''running''::text,''paid_call_started''::text,''completed''::text,''failed''::text,''result_unknown''::text])))' THEN
        RAISE EXCEPTION 'geo_obs_insight_state_chk 定义漂移: %', actual;
    END IF;
    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='geo_obs_insight_scope_snapshot_uk'
       AND conrelid='public.geo_observation_insight_jobs'::regclass;
    IF actual IS DISTINCT FROM 'unique(owner_user_id,brand_id,input_hash,snapshot_id)' THEN
        RAISE EXCEPTION 'insight snapshot unique 定义漂移: %', actual;
    END IF;
    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_insight_snapshot'
       AND conrelid='public.geo_observation_insight_jobs'::regclass;
    IF actual IS DISTINCT FROM
       'check((((snapshot_idisnull)and(state=''result_unknown''::text)and(policy_basis_hashisnull)and(scope_typeisnull)and(bucket_granularityisnull)and(bucket_startisnull)and(bucket_epochisnull)and(promotion_sequence_watermarkisnull)and(aggregate_input_watermarkisnull)and(aggregate_contract_versionisnull)and(aggregate_aggregation_versionisnull)and(aggregate_metric_versionisnull))or((snapshot_idisnotnull)and(snapshot_id~''^[0-9a-f]{64}$''::text)and(policy_basis_hashisnotnull)and(policy_basis_hash~''^[0-9a-f]{64}$''::text)and(scope_typeisnotnull)and(scope_type=''private_brand''::text)and(bucket_granularityisnotnull)and(bucket_granularity=any(array[''day''::text,''week''::text,''month''::text]))and(bucket_startisnotnull)and(bucket_epochisnotnull)and(bucket_epoch>=0)and(promotion_sequence_watermarkisnotnull)and(promotion_sequence_watermark>=0)and(aggregate_input_watermarkisnotnull)and(aggregate_contract_versionisnotnull)and(length(aggregate_contract_version)>0)and(aggregate_aggregation_versionisnotnull)and(length(aggregate_aggregation_version)>0)and(aggregate_metric_versionisnotnull)and(length(aggregate_metric_version)>0))))' THEN
        RAISE EXCEPTION 'chk_geo_obs_insight_snapshot 定义漂移: %', actual;
    END IF;
    SELECT regexp_replace(lower(pg_get_constraintdef(oid)), '\s+', '', 'g')
      INTO actual FROM pg_constraint
     WHERE conname='chk_geo_obs_insight_budget_reservation'
       AND conrelid='public.geo_observation_insight_jobs'::regclass;
    IF actual IS DISTINCT FROM
       'check((((budget_reserved_atisnull)and(paid_call_started_atisnull))or((budget_reserved_atisnotnull)and(paid_call_started_atisnotnull)and(budget_reserved_at=paid_call_started_at))))' THEN
        RAISE EXCEPTION 'chk_geo_obs_insight_budget_reservation 定义漂移: %', actual;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname='trg_geo_obs_event_eligibility_epoch'
           AND tgrelid='public.geo_observation_events'::regclass AND NOT tgisinternal
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname='trg_geo_obs_bucket_eligibility_epoch'
           AND tgrelid='public.geo_observation_contributor_buckets'::regclass AND NOT tgisinternal
    ) THEN
        RAISE EXCEPTION 'eligibility epoch trigger 缺失';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname='trg_geo_obs_assign_promotion_seq'
           AND tgrelid='public.geo_observation_events'::regclass AND NOT tgisinternal
           AND tgenabled IN ('O','A')
    ) THEN
        RAISE EXCEPTION 'promotion sequence trigger 缺失/未启用';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname='trg_geo_obs_event_published_immutable'
           AND tgrelid='public.geo_observation_events'::regclass AND NOT tgisinternal
           AND tgenabled IN ('O','A')
    ) THEN
        RAISE EXCEPTION 'published event immutable trigger 缺失/未启用';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname='trg_geo_obs_signal_promoted_immutable'
           AND tgrelid='public.geo_observation_signals'::regclass AND NOT tgisinternal
           AND tgenabled IN ('O','A')
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname='trg_geo_obs_bucket_promoted_immutable'
           AND tgrelid='public.geo_observation_contributor_buckets'::regclass AND NOT tgisinternal
           AND tgenabled IN ('O','A')
    ) THEN
        RAISE EXCEPTION 'promoted input immutable trigger 缺失/未启用';
    END IF;

    SELECT regexp_replace(lower(pg_get_functiondef(oid)), '\s+', '', 'g') INTO actual
      FROM pg_proc WHERE proname='geo_obs_assign_promotion_seq'
       AND pronamespace='public'::regnamespace;
    IF actual IS DISTINCT FROM
       'createorreplacefunctionpublic.geo_obs_assign_promotion_seq()returnstriggerlanguageplpgsqlas$function$beginiftg_op=''insert''andnew.promotion_seqisnotnullthenraiseexception''promotion_seqisdatabaseassigned'';endif;iftg_op=''update''andnew.promotion_seqisdistinctfromold.promotion_seqthenraiseexception''promotion_seqisimmutable'';endif;iftg_op=''update''andold.processing_state=''withdrawn''andnew.processing_state<>''withdrawn''thenraiseexception''withdrawnobservationisterminal;appendarevision'';endif;iftg_op=''update''andold.processing_state=''promoted''andnew.processing_statenotin(''promoted'',''withdrawn'')thenraiseexception''promotedobservationmayonlyremainpromotedorbecomewithdrawn'';endif;iftg_op=''update''andold.processing_state<>''promoted''andnew.processing_state=''promoted''andold.promotion_seqisnotnullthenraiseexception''firstpromotionrequiresanunassignedsequence'';endif;ifnew.processing_state=''promoted''andnew.promotion_seqisnullthennew.promotion_seq:=nextval(''public.geo_observation_promotion_seq''::regclass);endif;returnnew;end$function$' THEN
        RAISE EXCEPTION 'geo_obs_assign_promotion_seq 函数体漂移';
    END IF;
    SELECT regexp_replace(lower(pg_get_functiondef(oid)), '\s+', '', 'g') INTO actual
      FROM pg_proc WHERE proname='geo_obs_reject_promoted_input_mutation'
       AND pronamespace='public'::regnamespace;
    IF actual IS DISTINCT FROM
       'createorreplacefunctionpublic.geo_obs_reject_promoted_input_mutation()returnstriggerlanguageplpgsqlas$function$declareevent_statetext;beginselectprocessing_stateintoevent_statefrompublic.geo_observation_eventswhereid=old.event_id;ifevent_state=''promoted''thenraiseexception''promotedobservationinputisimmutable;withdraweventandappendarevision'';endif;iftg_op=''delete''thenreturnold;endif;returnnew;end$function$' THEN
        RAISE EXCEPTION 'geo_obs_reject_promoted_input_mutation 函数体漂移';
    END IF;
    SELECT regexp_replace(lower(pg_get_functiondef(oid)), '\s+', '', 'g') INTO actual
      FROM pg_proc WHERE proname='geo_obs_reject_published_event_mutation'
       AND pronamespace='public'::regnamespace;
    IF actual IS DISTINCT FROM
       'createorreplacefunctionpublic.geo_obs_reject_published_event_mutation()returnstriggerlanguageplpgsqlas$function$beginiftg_op=''delete''andold.processing_statein(''promoted'',''withdrawn'')thenraiseexception''publishedobservationcannotbedeleted;withdrawandappendarevision'';endif;iftg_op=''delete''thenreturnold;endif;ifold.processing_state=''promoted''thenifnew.processing_state=''promoted''thenif(to_jsonb(new)-array[''updated_at'',''legal_hold'',''retention_until''])isdistinctfrom(to_jsonb(old)-array[''updated_at'',''legal_hold'',''retention_until''])thenraiseexception''promotedobservationbusinessfieldsareimmutable'';endif;elsifnew.processing_state=''withdrawn''thenif(to_jsonb(new)-array[''processing_state'',''withdrawn_at'',''lease_token'',''lease_until'',''rejection_codes'',''updated_at'',''legal_hold''])isdistinctfrom(to_jsonb(old)-array[''processing_state'',''withdrawn_at'',''lease_token'',''lease_until'',''rejection_codes'',''updated_at'',''legal_hold''])thenraiseexception''withdrawalcannotrewritepromotedobservationinputs'';endif;elseraiseexception''promotedobservationmayonlyremainpromotedorbecomewithdrawn'';endif;elsifold.processing_state=''withdrawn''thenifnew.processing_state<>''withdrawn''thenraiseexception''withdrawnobservationisterminal;appendarevision'';endif;if(to_jsonb(new)-array[''owner_user_id'',''brand_id'',''answer_hash'',''prompt_fingerprint'',''updated_at'',''legal_hold''])isdistinctfrom(to_jsonb(old)-array[''owner_user_id'',''brand_id'',''answer_hash'',''prompt_fingerprint'',''updated_at'',''legal_hold''])thenraiseexception''withdrawnobservationonlypermitsretentionanonymization'';endif;endif;returnnew;end$function$' THEN
        RAISE EXCEPTION 'geo_obs_reject_published_event_mutation 函数体漂移';
    END IF;
    SELECT regexp_replace(lower(pg_get_triggerdef(oid)), '\s+', '', 'g') INTO actual
      FROM pg_trigger WHERE tgname='trg_geo_obs_assign_promotion_seq'
       AND tgrelid='public.geo_observation_events'::regclass AND NOT tgisinternal;
    IF actual IS DISTINCT FROM
       'createtriggertrg_geo_obs_assign_promotion_seqbeforeinsertorupdateofprocessing_state,promotion_seqonpublic.geo_observation_eventsforeachrowexecutefunctiongeo_obs_assign_promotion_seq()' THEN
        RAISE EXCEPTION 'trg_geo_obs_assign_promotion_seq 定义漂移';
    END IF;
    SELECT regexp_replace(lower(pg_get_triggerdef(oid)), '\s+', '', 'g') INTO actual
      FROM pg_trigger WHERE tgname='trg_geo_obs_signal_promoted_immutable'
       AND tgrelid='public.geo_observation_signals'::regclass AND NOT tgisinternal;
    IF actual IS DISTINCT FROM
       'createtriggertrg_geo_obs_signal_promoted_immutablebeforedeleteorupdateonpublic.geo_observation_signalsforeachrowexecutefunctiongeo_obs_reject_promoted_input_mutation()' THEN
        RAISE EXCEPTION 'trg_geo_obs_signal_promoted_immutable 定义漂移';
    END IF;
    SELECT regexp_replace(lower(pg_get_triggerdef(oid)), '\s+', '', 'g') INTO actual
      FROM pg_trigger WHERE tgname='trg_geo_obs_bucket_promoted_immutable'
       AND tgrelid='public.geo_observation_contributor_buckets'::regclass AND NOT tgisinternal;
    IF actual IS DISTINCT FROM
       'createtriggertrg_geo_obs_bucket_promoted_immutablebeforedeleteorupdateonpublic.geo_observation_contributor_bucketsforeachrowexecutefunctiongeo_obs_reject_promoted_input_mutation()' THEN
        RAISE EXCEPTION 'trg_geo_obs_bucket_promoted_immutable 定义漂移';
    END IF;
    SELECT regexp_replace(lower(pg_get_triggerdef(oid)), '\s+', '', 'g') INTO actual
      FROM pg_trigger WHERE tgname='trg_geo_obs_event_published_immutable'
       AND tgrelid='public.geo_observation_events'::regclass AND NOT tgisinternal;
    IF actual IS DISTINCT FROM
       'createtriggertrg_geo_obs_event_published_immutablebeforedeleteorupdateonpublic.geo_observation_eventsforeachrowexecutefunctiongeo_obs_reject_published_event_mutation()' THEN
        RAISE EXCEPTION 'trg_geo_obs_event_published_immutable 定义漂移';
    END IF;

    SELECT pg_get_indexdef(to_regclass('public.uq_geo_obs_agg_manifest_cell')) INTO idx;
    IF regexp_replace(lower(COALESCE(idx,'')), '\s+', '', 'g') IS DISTINCT FROM
       'createuniqueindexuq_geo_obs_agg_manifest_cellonpublic.geo_observation_aggregate_refresh_manifestusingbtree(policy_basis_hash,contract_version,aggregation_version,metric_version,scope_type,bucket_granularity,bucket_start)' THEN
        RAISE EXCEPTION 'uq_geo_obs_agg_manifest_cell 定义漂移: %', idx;
    END IF;
    SELECT pg_get_indexdef(to_regclass('public.idx_geo_obs_agg_policy_basis_scope_bucket')) INTO idx;
    IF regexp_replace(lower(COALESCE(idx,'')), '\s+', '', 'g') IS DISTINCT FROM
       'createindexidx_geo_obs_agg_policy_basis_scope_bucketonpublic.geo_observation_aggregatesusingbtree(policy_basis_hash,scope_type,bucket_granularity,bucket_start)' THEN
        RAISE EXCEPTION 'idx_geo_obs_agg_policy_basis_scope_bucket 定义漂移: %', idx;
    END IF;
    SELECT pg_get_indexdef(to_regclass('public.idx_geo_obs_agg_manifest_basis_scope')) INTO idx;
    IF regexp_replace(lower(COALESCE(idx,'')), '\s+', '', 'g') IS DISTINCT FROM
       'createindexidx_geo_obs_agg_manifest_basis_scopeonpublic.geo_observation_aggregate_refresh_manifestusingbtree(policy_basis_hash,scope_type,bucket_granularity,bucket_start)' THEN
        RAISE EXCEPTION 'idx_geo_obs_agg_manifest_basis_scope 定义漂移: %', idx;
    END IF;
    SELECT pg_get_indexdef(to_regclass('public.uq_geo_obs_event_promotion_seq')) INTO idx;
    IF regexp_replace(lower(COALESCE(idx,'')), '\s+', '', 'g') IS DISTINCT FROM
       'createuniqueindexuq_geo_obs_event_promotion_seqonpublic.geo_observation_eventsusingbtree(promotion_seq)where(promotion_seqisnotnull)' THEN
        RAISE EXCEPTION 'uq_geo_obs_event_promotion_seq 定义漂移: %', idx;
    END IF;
    SELECT pg_get_indexdef(to_regclass('public.idx_geo_obs_bucket_revision_dirty')) INTO idx;
    IF regexp_replace(lower(COALESCE(idx,'')), '\s+', '', 'g') IS DISTINCT FROM
       'createindexidx_geo_obs_bucket_revision_dirtyonpublic.geo_observation_aggregate_bucket_revisionusingbtree(dirty,bucket_start,scope_type,bucket_granularity)where(dirty=true)' THEN
        RAISE EXCEPTION 'idx_geo_obs_bucket_revision_dirty 定义漂移: %', idx;
    END IF;    SELECT pg_get_indexdef(to_regclass('public.idx_geo_obs_bucket_revision_receipt_gin')) INTO idx;
    IF regexp_replace(lower(COALESCE(idx,'')), '\s+', '', 'g') IS DISTINCT FROM
       'createindexidx_geo_obs_bucket_revision_receipt_ginonpublic.geo_observation_aggregate_bucket_revisionusinggin(published_receiptjsonb_path_ops)where(published_receiptisnotnull)' THEN
        RAISE EXCEPTION 'idx_geo_obs_bucket_revision_receipt_gin 定义漂移: %', idx;
    END IF;
    SELECT pg_get_indexdef(to_regclass('public.idx_geo_obs_insight_paid_recovery')) INTO idx;
    IF regexp_replace(lower(COALESCE(idx,'')), '\s+', '', 'g') IS DISTINCT FROM
       'createindexidx_geo_obs_insight_paid_recoveryonpublic.geo_observation_insight_jobsusingbtree(state,lease_until)where(state=any(array[''pending''::text,''running''::text,''paid_call_started''::text,''result_unknown''::text]))' THEN
        RAISE EXCEPTION 'idx_geo_obs_insight_paid_recovery 定义漂移: %', idx;
    END IF;
END $$;
