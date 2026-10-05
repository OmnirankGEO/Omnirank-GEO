-- migration_research_monitor_v4.sql
-- A.6 v4 · 给 geo_research_articles 加 score_attempts 字段 (C-3 backlog 修复)
-- 防"评分失败的文章永远卡 pending_review + score=NULL"问题
--
-- 跑法: docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope < scripts/migration_research_monitor_v4.sql
-- 验证: docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d geo_research_articles" | grep score_attempts
-- 回滚: scripts/rollback_research_monitor_v4.sql

BEGIN;

-- 评分失败重试计数器 · 累计失败 N 次 (默认 3) 后软着 review_status='auto_skipped'
ALTER TABLE geo_research_articles
    ADD COLUMN IF NOT EXISTS score_attempts SMALLINT NOT NULL DEFAULT 0;
COMMENT ON COLUMN geo_research_articles.score_attempts IS '累计 LLM 评分失败次数 · 满 SCORE_ATTEMPTS_MAX (默认 3) 后 review_status 软着 auto_skipped 防卡死';

-- migration marker
INSERT INTO _migration_markers (marker, applied_at, note)
VALUES ('research_monitor_v4', NOW(), 'A.6 v4 加 score_attempts 字段 (C-3 防评分失败卡死)')
ON CONFLICT (marker) DO NOTHING;

COMMIT;
