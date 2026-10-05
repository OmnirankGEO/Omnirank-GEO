SET LOCAL search_path = pg_catalog, public;

DO $guard$
DECLARE
    evidence_count BIGINT := 0;
BEGIN
    IF pg_catalog.to_regclass('public.monitoring_identity_decision_events') IS NOT NULL THEN
        EXECUTE 'SELECT COUNT(*) FROM public.monitoring_identity_decision_events'
           INTO evidence_count;
        IF evidence_count > 0 THEN
            RAISE EXCEPTION
                'monitoring identity rollback blocked: decision events exist';
        END IF;
    END IF;

    IF pg_catalog.to_regclass('public.monitoring_identity_name_decisions') IS NOT NULL THEN
        EXECUTE 'SELECT COUNT(*) FROM public.monitoring_identity_name_decisions'
           INTO evidence_count;
        IF evidence_count > 0 THEN
            RAISE EXCEPTION
                'monitoring identity rollback blocked: name decisions exist';
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'monitoring_results'
           AND column_name = 'identity_review_state'
    ) THEN
        EXECUTE $sql$
            SELECT COUNT(*)
              FROM public.monitoring_results
             WHERE identity_review_state <> 'not_required'
                OR response_status = 'brand_identity_unresolved'
                OR mention_type = 'pending_identity'
        $sql$ INTO evidence_count;
        IF evidence_count > 0 THEN
            RAISE EXCEPTION
                'monitoring identity rollback blocked: unresolved or resolved evidence exists';
        END IF;
    END IF;
END
$guard$;

DROP TRIGGER IF EXISTS trg_monitoring_identity_events_append_only
    ON public.monitoring_identity_decision_events;
DROP FUNCTION IF EXISTS public.reject_monitoring_identity_event_mutation();
DROP TABLE IF EXISTS public.monitoring_identity_decision_events;
DROP TABLE IF EXISTS public.monitoring_identity_name_decisions;
DROP INDEX IF EXISTS public.idx_monitoring_results_identity_pending;
ALTER TABLE public.monitoring_results
    DROP CONSTRAINT IF EXISTS fk_monitoring_results_identity_brand,
    DROP CONSTRAINT IF EXISTS chk_monitoring_results_identity_version,
    DROP CONSTRAINT IF EXISTS chk_monitoring_results_identity_markers,
    DROP CONSTRAINT IF EXISTS chk_monitoring_results_identity_pending,
    DROP CONSTRAINT IF EXISTS chk_monitoring_results_identity_candidates,
    DROP CONSTRAINT IF EXISTS chk_monitoring_results_identity_review_state,
    DROP COLUMN IF EXISTS identity_resolved_by,
    DROP COLUMN IF EXISTS identity_resolved_at,
    DROP COLUMN IF EXISTS identity_decision_version,
    DROP COLUMN IF EXISTS identity_evidence_hash,
    DROP COLUMN IF EXISTS identity_evidence_snippet,
    DROP COLUMN IF EXISTS identity_candidates,
    DROP COLUMN IF EXISTS identity_brand_id,
    DROP COLUMN IF EXISTS identity_review_state;
