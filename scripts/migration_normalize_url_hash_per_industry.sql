-- P14.2 C2 (2026-05-29) · 文章去重口径"行业内 URL"迁移
--
-- 背景:
--   旧 schema: geo_research_articles UNIQUE (url_hash) · UNIQUE (url) · 同 URL 全局去重
--   后果: 同一 URL 被多行业引用时 · article.primary_industry 不变 · 文章库按 primary 筛
--         "测试" 看不到 5 篇 primary='geo服务' 的文章 (admin 实测 round_20260528_232257 暴露)
--
-- 新 schema: UNIQUE (url_hash, primary_industry) · UNIQUE (url, primary_industry)
--   每个行业拥有自己的 article 行 · 跨行业复用内容字段 (省 OSS/Jina) · 但 article_id 独立
--   - is_duplicate=TRUE · primary_article_id 指向首篇 (跨行业 dup 行)
--   - citation.article_id 指向"对应行业"的 article 行
--
-- 部署 CTO 注意:
--   ⚠️ 跑此 migration 前 · 必须确认:
--     1. 后端 8000 已停 (或全部 round 处于完成态 · 无 running/pending)
--     2. DB 备份就绪 (虽 backfill 是 INSERT 不删改老行 · 但 schema 改动需要 rollback 准备)
--     3. Code 部署:本 SQL 跑完后再部署 P14.2 C1 代码也可 ·
--        或代码先部署(老 schema 下 stage 3 跨行业行为退化 reused_existing · 不报错)
--   测试: 本地 dev 已跑 conftest.py 等价 schema · 集成测试 283/283 PASS · backfill 测试见
--         tests/research_monitor/test_migration_normalize_url_hash_per_industry.py
--
-- Idempotent: 重复跑无副作用 · 所有 DDL 用 IF EXISTS / IF NOT EXISTS · backfill 用 NOT EXISTS

BEGIN;

-- ========================================
-- 1. 兜底 primary_industry NOT NULL/空 (必须先有值才能加复合 UNIQUE)
-- ========================================
UPDATE geo_research_articles
   SET primary_industry = 'unknown'
 WHERE primary_industry IS NULL OR primary_industry = '';

-- ========================================
-- 2. DROP 旧全局 UNIQUE (idempotent)
-- ========================================
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'geo_research_articles_url_hash_key') THEN
        ALTER TABLE geo_research_articles
            DROP CONSTRAINT geo_research_articles_url_hash_key;
        RAISE NOTICE '[P14.2] DROP 旧 url_hash UNIQUE';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'geo_research_articles_url_key') THEN
        ALTER TABLE geo_research_articles
            DROP CONSTRAINT geo_research_articles_url_key;
        RAISE NOTICE '[P14.2] DROP 旧 url UNIQUE';
    END IF;
END $$;

-- ========================================
-- 3. 复合 UNIQUE 兜底加 (跨行业行能并存)
-- ========================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'geo_research_articles_url_hash_industry_key') THEN
        ALTER TABLE geo_research_articles
            ADD CONSTRAINT geo_research_articles_url_hash_industry_key
            UNIQUE (url_hash, primary_industry);
        RAISE NOTICE '[P14.2] ADD UNIQUE (url_hash, primary_industry)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'geo_research_articles_url_industry_key') THEN
        ALTER TABLE geo_research_articles
            ADD CONSTRAINT geo_research_articles_url_industry_key
            UNIQUE (url, primary_industry);
        RAISE NOTICE '[P14.2] ADD UNIQUE (url, primary_industry)';
    END IF;
END $$;

-- ========================================
-- 4. Backfill: 为每个 cross-industry citation 创建对应行业 dup article
--    (老板规格 B: 不是按 5 条写死 · 通用 SQL · 可重复跑)
--    规则:
--    - 找所有 citation.industry_name <> article.primary_industry 的 citation
--    - 对 (url_hash, citation_industry_name) 缺失 article 行的组合 · 创建 dup
--    - 复制 14 内容字段 · 重写 7 身份字段 · 不复制 5 权限字段 (跟 C1 代码规格对齐)
-- ========================================
INSERT INTO geo_research_articles
    (url, url_hash, domain, title,
     oss_key_raw, oss_key_cleaned,
     raw_char_count, cleaned_char_count, content_hash,
     domain_tier, content_type, inline_cleaned_content,
     clean_status, review_status,
     primary_industry,
     is_duplicate, primary_article_id,
     total_citation_count,
     first_seen_round_id,
     last_seen_at, fetched_at)
SELECT DISTINCT ON (a.url_hash, i.name)
       a.url, a.url_hash, a.domain, a.title,
       a.oss_key_raw, a.oss_key_cleaned,
       a.raw_char_count, a.cleaned_char_count, a.content_hash,
       a.domain_tier, a.content_type, a.inline_cleaned_content,
       a.clean_status, a.review_status,
       i.name AS target_industry,  -- 跨行业名变身
       TRUE,                       -- is_duplicate
       a.id,                       -- primary_article_id 指向首篇
       0,                          -- total_citation_count 后面统一重算
       c.round_id,                 -- 老板复核 fix: 用本行业首次 citation 的 round_id
                                   -- (而非原 article.first_seen_round_id · 否则文章库按
                                   --  first_seen_round_id+primary_industry 筛 · 历史 backfill
                                   --  dup 不会出现在"本行业 这一轮"视图)
       NOW(), NOW()
  FROM geo_research_article_citations c
  JOIN geo_research_articles a ON a.id = c.article_id
  JOIN geo_research_industries i ON i.id = c.industry_id
 WHERE c.industry_id IS NOT NULL
   AND i.name <> a.primary_industry
   AND NOT EXISTS (
       SELECT 1 FROM geo_research_articles a2
        WHERE a2.url_hash = a.url_hash
          AND a2.primary_industry = i.name
   )
 ORDER BY a.url_hash, i.name,
          c.cited_at ASC NULLS LAST, c.id ASC  -- 多 citation 同 (url_hash, industry) 时挑最早
ON CONFLICT DO NOTHING;

-- ========================================
-- 5. 迁移 citation.article_id 指向新创建的对应行业 article
--    (idempotent: WHERE 严格匹配 industry 不同 + 目标新 article is_duplicate=true)
-- ========================================
UPDATE geo_research_article_citations c
   SET article_id = new_a.id
  FROM geo_research_articles a,
       geo_research_industries i,
       geo_research_articles new_a
 WHERE c.article_id = a.id
   AND c.industry_id = i.id
   AND i.name <> a.primary_industry
   AND new_a.url_hash = a.url_hash
   AND new_a.primary_industry = i.name
   AND new_a.id <> a.id
   AND new_a.is_duplicate = TRUE;

-- ========================================
-- 6. 重算 total_citation_count (按当前 citation 真实分布)
--    (backfill 后老 article 的 cite 数减少 · 新 dup article 的 cite 数增加)
-- ========================================
UPDATE geo_research_articles a
   SET total_citation_count = COALESCE((
       SELECT COUNT(*)
         FROM geo_research_article_citations c
        WHERE c.article_id = a.id
   ), 0);

-- ========================================
-- 7. 验证报告 (输出 backfill 摘要 · 不阻塞 commit)
-- ========================================
DO $$
DECLARE
    cnt_dup INTEGER;
    cnt_orphan_citation INTEGER;
BEGIN
    SELECT COUNT(*) INTO cnt_dup
      FROM geo_research_articles WHERE is_duplicate = TRUE;
    SELECT COUNT(*) INTO cnt_orphan_citation
      FROM geo_research_article_citations c
      JOIN geo_research_articles a ON a.id = c.article_id
      JOIN geo_research_industries i ON i.id = c.industry_id
     WHERE c.industry_id IS NOT NULL
       AND i.name <> a.primary_industry;
    RAISE NOTICE '[P14.2 backfill 报告] dup article 总数=%, 残留跨行业 citation 数=% (应=0)',
        cnt_dup, cnt_orphan_citation;
END $$;

COMMIT;

-- 跑完手动验证 (部署 CTO 跑):
--   1. SELECT conname FROM pg_constraint WHERE conrelid='geo_research_articles'::regclass AND contype='u';
--      预期: 含 *_url_hash_industry_key + *_url_industry_key · 不含 *_url_hash_key + *_url_key
--   2. SELECT COUNT(*) FROM geo_research_article_citations c
--          JOIN geo_research_articles a ON a.id=c.article_id
--          JOIN geo_research_industries i ON i.id=c.industry_id
--          WHERE i.name <> a.primary_industry AND c.industry_id IS NOT NULL;
--      预期: 0 (无残留跨行业 citation)
--   3. 重跑本 SQL · 应 NOTICE "重复跑" 但 dup article 总数 + citation 分布不变
