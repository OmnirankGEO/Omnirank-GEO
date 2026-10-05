-- P0.4 发布事实源收束 · migration(CTO-15.7 2026-04-24)
--
-- 老板 §10 批:articles 加 first_published_at 1 denormalized 查询字段
-- 不加 publish_url / publish_source / published_by 等其他字段(信息在 media_publications)
--
-- media_publications 表 schema 已有 article_id / screenshot_path 字段(T0 docker exec 确认)
-- 本次 migration 只加 articles.first_published_at + partial idx · 不改 media_publications
--
-- 部署前必做:
--   docker exec omnirank-db pg_dump -U geo_admin geo_agentscope -t articles > backup_articles_$(date +%Y%m%d_%H%M).sql

BEGIN;

-- denormalized 查询加速字段(事实源仍是 media_publications)
ALTER TABLE articles ADD COLUMN IF NOT EXISTS first_published_at TIMESTAMP DEFAULT NULL;

-- partial index · 仅索引非 NULL 行 · 空间高效
CREATE INDEX IF NOT EXISTS idx_articles_first_pub
  ON articles(first_published_at) WHERE first_published_at IS NOT NULL;

-- 字段说明
COMMENT ON COLUMN articles.first_published_at IS
  'P0.4 denormalized · 首次发布时间(任一平台) · 事实源 media_publications · 仅查询加速用';

COMMIT;

-- ========== 验收查询 ==========
-- 1. 字段存在
-- \d articles  -- 看到 first_published_at TIMESTAMP DEFAULT NULL

-- 2. 索引存在
-- \di idx_articles_first_pub  -- 看到 partial btree

-- 3. 存量 538 篇(CLAUDE.md 提及)的 first_published_at 默认 NULL
-- SELECT COUNT(*) AS total,
--        COUNT(first_published_at) AS already_pub
-- FROM articles;
-- 期望:already_pub = 0(从新数据开始累积 · 老板批不回填存量)

-- 4. 发布进度 dashboard 查询(services/publication_facts.get_brand_publish_progress 使用)
-- SELECT COUNT(*) AS total,
--        COUNT(*) FILTER (WHERE first_published_at IS NOT NULL) AS published
-- FROM articles a JOIN topics t ON t.id = a.topic_id
-- WHERE t.brand_id = <某 brand_id>;

-- ========== 回滚(仅紧急情况)==========
-- DROP INDEX IF EXISTS idx_articles_first_pub;
-- ALTER TABLE articles DROP COLUMN IF EXISTS first_published_at;
