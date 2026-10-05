-- Destructive rollback is allowed only before this migration has accumulated
-- provider/charging recovery facts. Losing any such row could authorize a
-- second paid POST or strand an immutable provider outcome.
DO $$
DECLARE
  target_oid OID := to_regclass('public.marketing_material_generation_attempts');
  recovery_column_count INTEGER;
  retained_facts BIGINT;
BEGIN
  IF target_oid IS NULL THEN
    RAISE EXCEPTION 'geo provider attempt rollback target public.marketing_material_generation_attempts is missing';
  END IF;
  SELECT COUNT(*) INTO recovery_column_count
  FROM pg_catalog.pg_attribute
  WHERE attrelid=target_oid AND NOT attisdropped
    AND attname IN (
      'component_id','submit_state','poll_state','submit_guard_token','last_heartbeat_at',
      'provider_result_jsonb','materialization_state','resolution_state','resolved_at'
    );
  IF recovery_column_count NOT IN (0,9) THEN
    RAISE EXCEPTION 'geo provider attempt rollback found a partial recovery schema';
  END IF;
  IF recovery_column_count=9 THEN
    SELECT COUNT(*) INTO retained_facts
    FROM public.marketing_material_generation_attempts
    WHERE status<>'pending'
       OR component_id<>'' OR submit_state<>'not_started' OR poll_state<>'not_started'
       OR submit_guard_token<>'' OR last_heartbeat_at IS NOT NULL
       OR provider_task_id<>'' OR provider_result_jsonb<>'{}'::jsonb
       OR materialization_state<>'not_started' OR resolution_state<>'automatic'
       OR resolved_at IS NOT NULL;
    IF retained_facts<>0 THEN
      RAISE EXCEPTION
        'geo provider attempt rollback refused: % row(s) retain provider/idempotency recovery facts',
        retained_facts;
    END IF;
  END IF;
END $$;

DROP INDEX IF EXISTS public.idx_marketing_attempt_recovery;
DROP INDEX IF EXISTS public.uq_marketing_attempt_component_no;
ALTER TABLE public.marketing_material_generation_attempts
  DROP CONSTRAINT IF EXISTS marketing_attempt_resolution_state_ck,
  DROP CONSTRAINT IF EXISTS marketing_attempt_materialization_state_ck,
  DROP CONSTRAINT IF EXISTS marketing_attempt_poll_state_ck,
  DROP CONSTRAINT IF EXISTS marketing_attempt_submit_state_ck,
  DROP COLUMN IF EXISTS resolved_at,
  DROP COLUMN IF EXISTS resolution_state,
  DROP COLUMN IF EXISTS materialization_state,
  DROP COLUMN IF EXISTS provider_result_jsonb,
  DROP COLUMN IF EXISTS last_heartbeat_at,
  DROP COLUMN IF EXISTS submit_guard_token,
  DROP COLUMN IF EXISTS poll_state,
  DROP COLUMN IF EXISTS submit_state,
  DROP COLUMN IF EXISTS component_id;
