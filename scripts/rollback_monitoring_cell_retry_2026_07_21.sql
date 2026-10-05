-- Rollback for the additive monitoring cell retry ledger.
-- Run only after proving there are no retained run-cell/retry facts to preserve.
BEGIN;
SET LOCAL search_path = pg_catalog, public;

DO $$
DECLARE
    has_review_facts BOOLEAN := FALSE;
    has_retry_facts BOOLEAN := FALSE;
    has_cell_facts BOOLEAN := FALSE;
    has_settlement_facts BOOLEAN := FALSE;
BEGIN
    LOCK TABLE public.monitoring_tasks, public.monitoring_results, public.quotes
        IN ACCESS EXCLUSIVE MODE;
    IF pg_catalog.to_regclass('public.monitoring_provider_review_events') IS NOT NULL THEN
        LOCK TABLE public.monitoring_provider_review_events IN ACCESS EXCLUSIVE MODE;
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.monitoring_provider_review_events LIMIT 1)'
            INTO has_review_facts;
    END IF;
    IF pg_catalog.to_regclass('public.monitoring_cell_retry_requests') IS NOT NULL THEN
        LOCK TABLE public.monitoring_cell_retry_requests IN ACCESS EXCLUSIVE MODE;
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.monitoring_cell_retry_requests LIMIT 1)'
            INTO has_retry_facts;
    END IF;
    IF pg_catalog.to_regclass('public.monitoring_keyword_settlements') IS NOT NULL THEN
        LOCK TABLE public.monitoring_keyword_settlements IN ACCESS EXCLUSIVE MODE;
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.monitoring_keyword_settlements LIMIT 1)'
            INTO has_settlement_facts;
    END IF;
    IF pg_catalog.to_regclass('public.monitoring_run_cells') IS NOT NULL THEN
        LOCK TABLE public.monitoring_run_cells IN ACCESS EXCLUSIVE MODE;
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.monitoring_run_cells LIMIT 1)'
            INTO has_cell_facts;
    END IF;
    IF has_review_facts OR has_retry_facts OR has_cell_facts OR has_settlement_facts THEN
        RAISE EXCEPTION 'monitoring cell retry rollback blocked: retained facts exist';
    END IF;
END $$;

DROP TABLE IF EXISTS public.monitoring_provider_review_events;
DROP FUNCTION IF EXISTS public.reject_monitoring_provider_review_event_mutation();
DROP TABLE IF EXISTS public.monitoring_cell_retry_requests;
DROP TABLE IF EXISTS public.monitoring_keyword_settlements;
DO $$
BEGIN
    IF pg_catalog.to_regclass('public.monitoring_run_cells') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_monitoring_run_cell_terminal_overwrite ON public.monitoring_run_cells';
    END IF;
END $$;
DROP FUNCTION IF EXISTS public.reject_monitoring_run_cell_terminal_overwrite();
DROP TABLE IF EXISTS public.monitoring_run_cells;
ALTER TABLE public.monitoring_tasks
    DROP CONSTRAINT IF EXISTS uq_monitoring_tasks_id_brand;
ALTER TABLE public.monitoring_results
    DROP CONSTRAINT IF EXISTS uq_monitoring_results_id_task;
ALTER TABLE public.quotes
    DROP CONSTRAINT IF EXISTS uq_quotes_id_brand;
COMMIT;
