-- migration_research_monitor_v3.sql
-- A.6 v3 · 给 geo_research_articles 加 total_citation_count 字段
-- 用于"按 AI 累计引用次数排序"推 admin top N (老板 2026-05-08 拍板 N=30)
--
-- 跑法: docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope < scripts/migration_research_monitor_v3.sql
-- 验证: docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d geo_research_articles" | grep total_citation_count
-- 回滚: scripts/rollback_research_monitor_v3.sql

BEGIN;

-- 字段 1: total_citation_count 累计被引用次数 (跨多轮多 AI)
-- 数值越大 = 越多 AI 多次引用 = 真权威
ALTER TABLE geo_research_articles
    ADD COLUMN IF NOT EXISTS total_citation_count INTEGER NOT NULL DEFAULT 0;
COMMENT ON COLUMN geo_research_articles.total_citation_count IS '累计被引用次数(跨多轮多 AI),由 round_runner stage 7 之前重算 · 用于审核界面 top N 排序';

-- 索引: 按 total_citation_count 排序的主查询路径(WHERE review_status='pending_review' ORDER BY ...)
CREATE INDEX IF NOT EXISTS idx_geo_research_articles_pending_top
    ON geo_research_articles(review_status, total_citation_count DESC, cleaned_char_count DESC)
 WHERE review_status = 'pending_review';
COMMENT ON INDEX idx_geo_research_articles_pending_top IS 'pending_review top N 排序加速 (total_citation DESC, char_count DESC)';

-- 数据回填: 用现有 geo_research_article_citations 表算每篇 article 的累计引用数
UPDATE geo_research_articles a
   SET total_citation_count = COALESCE(sub.cnt, 0)
  FROM (
      SELECT article_id, COUNT(*) AS cnt
        FROM geo_research_article_citations
       GROUP BY article_id
  ) sub
 WHERE a.id = sub.article_id;

-- migration marker
INSERT INTO _migration_markers (marker, applied_at, note)
VALUES ('research_monitor_v3', NOW(), 'A.6 v3 加 total_citation_count 字段 + 索引 + 数据回填')
ON CONFLICT (marker) DO NOTHING;

COMMIT;
