-- migration_research_monitor_review_v2.sql
-- A.6 审核状态机给 reference_articles 加 2 字段
-- 注意: reference_articles 是写作大厅子系统的表, 可能未先建
-- 本 migration 用 DO $$ 条件块包裹 ALTER, 表不存在时跳过 + warning 日志, 不挂整个 BEGIN/COMMIT
-- 跑法: docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope < scripts/migration_research_monitor_review_v2.sql
-- 验证: docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d reference_articles" | grep -E "archive_reason|source_article_id"

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'reference_articles'
    ) THEN
        -- 字段 1: archive_reason 标记软删原因(便于审计)
        ALTER TABLE reference_articles
            ADD COLUMN IF NOT EXISTS archive_reason VARCHAR(50);
        COMMENT ON COLUMN reference_articles.archive_reason IS '软删原因 (research_monitor_undo / research_monitor_reject / admin_manual / ...)';

        -- 字段 2: source_article_id 反向引用 geo_research_articles.id (复用旧 ref 行用)
        ALTER TABLE reference_articles
            ADD COLUMN IF NOT EXISTS source_article_id BIGINT;
        COMMENT ON COLUMN reference_articles.source_article_id IS '若来自 geo_research_articles approve, 指向该 article id (重新 approve 时复用旧 ref 行)';

        -- 索引: 按 source_article_id 反查 (重新 approve 复用)
        CREATE INDEX IF NOT EXISTS idx_reference_articles_source_article
            ON reference_articles(source_article_id) WHERE source_article_id IS NOT NULL;

        RAISE NOTICE 'reference_articles 表已扩展 archive_reason + source_article_id 字段';
    ELSE
        RAISE NOTICE 'reference_articles 表不存在(写作大厅子系统未部署), 跳过本 migration · marker 仍写入避免重跑';
    END IF;
END$$;

-- migration marker (不论是否真 ALTER 都写入, 让本 migration 在两种环境下都"已执行")
INSERT INTO _migration_markers (marker, applied_at, note)
VALUES ('research_monitor_review_v2', NOW(), 'A.6 reference_articles 加 archive_reason + source_article_id 字段(若表存在)')
ON CONFLICT (marker) DO NOTHING;

COMMIT;
