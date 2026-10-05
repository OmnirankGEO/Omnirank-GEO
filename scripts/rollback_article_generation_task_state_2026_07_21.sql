-- Roll back the additive article-generation task projection only while it has
-- no retained reset/audit facts.  Intended for isolated rollback-forward
-- verification and for a confirmed pre-traffic application rollback.
BEGIN;

DO $guard$
DECLARE
    retained_events BIGINT := 0;
    retained_topic_state BIGINT := 0;
BEGIN
    IF pg_catalog.to_regclass('public.article_generation_revision_events') IS NOT NULL THEN
        LOCK TABLE public.article_generation_revision_events IN ACCESS EXCLUSIVE MODE;
        EXECUTE 'SELECT COUNT(*) FROM public.article_generation_revision_events'
           INTO retained_events;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema='public' AND table_name='topics'
           AND column_name='generation_request_id'
    ) THEN
        LOCK TABLE public.topics IN ACCESS EXCLUSIVE MODE;
        SELECT COUNT(*)
          INTO retained_topic_state
          FROM public.topics
         WHERE generation_request_id IS NOT NULL
            OR generation_revision <> 0
            OR generation_operation IS NOT NULL
            OR generation_error_code IS NOT NULL
            OR generation_error_message IS NOT NULL
            OR generation_retryable IS NOT NULL
            OR generation_failure_phase IS NOT NULL
            OR generation_refund_status IS NOT NULL;
    END IF;
    IF retained_events <> 0 OR retained_topic_state <> 0 THEN
        RAISE EXCEPTION
            'article generation rollback blocked: events=% topic_state=%',
            retained_events, retained_topic_state;
    END IF;
END
$guard$;

DROP TABLE IF EXISTS public.article_generation_revision_events;
DROP FUNCTION IF EXISTS public.article_generation_revision_events_immutable_guard();
DROP INDEX IF EXISTS public.idx_topics_generation_request;
ALTER TABLE public.topics
    DROP CONSTRAINT IF EXISTS topics_generation_revision_nonnegative_ck,
    DROP COLUMN IF EXISTS generation_refund_status,
    DROP COLUMN IF EXISTS generation_failure_phase,
    DROP COLUMN IF EXISTS generation_retryable,
    DROP COLUMN IF EXISTS generation_error_message,
    DROP COLUMN IF EXISTS generation_error_code,
    DROP COLUMN IF EXISTS generation_operation,
    DROP COLUMN IF EXISTS generation_revision,
    DROP COLUMN IF EXISTS generation_request_id;

COMMIT;
