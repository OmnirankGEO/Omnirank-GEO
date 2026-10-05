-- Forward-compatible application rollback.
-- Older application code ignores these additive columns and tables. Clearing
-- active pointers would break every already-issued public quote and would not
-- be recoverable deterministically, so rollback intentionally performs no DML.
BEGIN;
SELECT 'quote snapshot schema retained; no data rollback required' AS rollback_status;
COMMIT;
