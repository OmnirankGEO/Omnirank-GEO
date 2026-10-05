-- rollback_research_monitor_v3.sql
-- 反向 migration v3 (A.6 v3)

BEGIN;

DROP INDEX IF EXISTS idx_geo_research_articles_pending_top;
ALTER TABLE geo_research_articles DROP COLUMN IF EXISTS total_citation_count;

DELETE FROM _migration_markers WHERE marker = 'research_monitor_v3';

COMMIT;
