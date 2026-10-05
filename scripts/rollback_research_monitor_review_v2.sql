-- rollback_research_monitor_review_v2.sql
-- 回滚 migration_research_monitor_review_v2.sql 的所有变更
-- 用 DO $$ 条件块包裹 DROP, 表不存在时跳过 + warning 日志, 不挂整个 BEGIN/COMMIT

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'reference_articles'
    ) THEN
        DROP INDEX IF EXISTS idx_reference_articles_source_article;
        ALTER TABLE reference_articles DROP COLUMN IF EXISTS source_article_id;
        ALTER TABLE reference_articles DROP COLUMN IF EXISTS archive_reason;
        RAISE NOTICE 'reference_articles 字段已回滚';
    ELSE
        RAISE NOTICE 'reference_articles 表不存在, 跳过 rollback · marker 仍删除';
    END IF;
END$$;

DELETE FROM _migration_markers WHERE marker = 'research_monitor_review_v2';

COMMIT;
