-- rollback_research_monitor_v4.sql

BEGIN;

ALTER TABLE geo_research_articles DROP COLUMN IF EXISTS score_attempts;

DELETE FROM _migration_markers WHERE marker = 'research_monitor_v4';

COMMIT;
