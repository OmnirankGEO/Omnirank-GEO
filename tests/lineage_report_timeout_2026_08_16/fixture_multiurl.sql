-- [P0 血缘三报 2026-08-16] **构造夹具**:让「两级 MAX 折叠成一级」这个改写
-- 真的被判据打到。
--
-- 为什么必须构造:生产快照里 17,149 篇文章中有 1,688 篇引用了多个 cite_url,
-- 但**没有任何一篇**的信号值在这些 URL 之间不同(实测
-- adopted_differs_across_urls=0 / weight_differs_across_urls=0)。
-- ⇒ 真数据上 MIN ≡ MAX ≡ SUM(单值),旧写法和任何错误折叠都返回同样结果,
--   「48/48 逐行同结果」全绿但**零判别力** —— 与「反向对照本身为零」同型。
--
-- 本夹具构造 3 篇文章,每篇跨 URL 的信号值**故意互不相同**,使得
-- MAX / MIN / SUM 三种折叠必然给出不同答案。
-- 全程在事务内,调用方 ROLLBACK,不留痕。

-- round(citations 的 FK)
INSERT INTO geo_research_round (round_id, batch_id, triggered_by, status)
VALUES ('P0LINEAGE-ROUND', 'P0LINEAGE-BATCH', 'manual', 'completed');

-- ── 文章 1:新口径 · 跨 2 个 URL 的 tier/weight/engine 全不同 ──
INSERT INTO geo_research_articles
    (id, url, url_hash, domain, title, primary_industry, corpus_grade,
     canonical_body_hash, content_cluster_id, label_provenance_version,
     inline_cleaned_content, cleaned_char_count, content_type, intent_type,
     expired, review_status, clean_status)
VALUES
    (9900001, 'https://p0fixture.test/a1', repeat('a', 40), 'p0fixture.test',
     'P0 夹具 · 多 URL 差异(新口径)', '装修建材', 'JC5',
     repeat('h', 64), 'cluster-p0-1', 'research-source-lineage-v1.0',
     repeat('正', 900), 900, 'guide', 'comparison',
     FALSE, 'approved', 'cleaned'),
    (9900002, 'https://p0fixture.test/a2', repeat('b', 40), 'p0fixture.test',
     'P0 夹具 · 多 URL 差异(旧口径 legacy)', '装修建材', 'JC5',
     repeat('i', 64), 'cluster-p0-2', 'research-source-lineage-v1.0',
     repeat('文', 800), 800, 'guide', 'comparison',
     FALSE, 'approved', 'cleaned'),
    (9900003, 'https://p0fixture.test/a3', repeat('c', 40), 'p0fixture.test',
     'P0 夹具 · 新旧口径混合', '装修建材', 'JC5',
     repeat('j', 64), 'cluster-p0-3', 'research-source-lineage-v1.0',
     repeat('混', 700), 700, 'guide', 'comparison',
     FALSE, 'approved', 'cleaned');

-- raw:每篇文章挂 2 条不同 cite_url
INSERT INTO geo_research_raw (id, industry, query, engine, cited_platform, cite_url)
VALUES
    (9900101, '装修建材', 'p0 fixture q1', 'doubao',   'doubao',   'https://p0src.test/high'),
    (9900102, '装修建材', 'p0 fixture q2', 'deepseek', 'deepseek', 'https://p0src.test/low'),
    (9900103, '装修建材', 'p0 fixture q3', 'kimi',     'kimi',     'https://p0src.test/legacy-high'),
    (9900104, '装修建材', 'p0 fixture q4', 'qwen',     'qwen',     'https://p0src.test/legacy-low'),
    (9900105, '装修建材', 'p0 fixture q5', 'doubao',   'doubao',   'https://p0src.test/mix-new'),
    (9900106, '装修建材', 'p0 fixture q6', 'kimi',     'kimi',     'https://p0src.test/mix-legacy');

INSERT INTO geo_research_article_citations (article_id, round_id, platform, raw_id, prompt_id)
VALUES
    (9900001, 'P0LINEAGE-ROUND', 'doubao',   9900101, NULL),
    (9900001, 'P0LINEAGE-ROUND', 'deepseek', 9900102, NULL),
    (9900002, 'P0LINEAGE-ROUND', 'kimi',     9900103, NULL),
    (9900002, 'P0LINEAGE-ROUND', 'qwen',     9900104, NULL),
    (9900003, 'P0LINEAGE-ROUND', 'doubao',   9900105, NULL),
    (9900003, 'P0LINEAGE-ROUND', 'kimi',     9900106, NULL);

-- ── 信号 ──
-- /high : answer_adopted · weight 9.5 · 2 个不同引擎 → engine_count=2
-- /low  : search_result_only · weight 0.25 · 1 个引擎 → engine_count=1
--   ⇒ 文章 1 正确(MAX)= adopted 1 / search_only 1 / weight 9.5 / engine_count 2
--     误用 MIN = adopted 0 / weight 0.25 / engine_count 1   ← 必然被判据看见
--     误用 SUM(weight) = 9.75                               ← 必然被判据看见
INSERT INTO geo_research_source_signals
    (source_url, industry_key, engine, signal_tier, balanced_weight,
     lineage_status, label_provenance_type, prompt_id, round_id)
VALUES
    ('https://p0src.test/high', '装修建材', 'doubao',   'answer_adopted',     9.500000,
     'complete', 'direct_observation', 'p0-1', 'P0LINEAGE-ROUND'),
    ('https://p0src.test/high', '装修建材', '豆包',     'answer_adopted',     8.000000,
     'complete', 'direct_observation', 'p0-2', 'P0LINEAGE-ROUND'),
    ('https://p0src.test/high', '装修建材', 'kimi',     'answer_adopted',     7.000000,
     'complete', 'direct_observation', 'p0-3', 'P0LINEAGE-ROUND'),
    ('https://p0src.test/low',  '装修建材', 'deepseek', 'search_result_only', 0.250000,
     'complete', 'direct_observation', 'p0-4', 'P0LINEAGE-ROUND'),

    -- 文章 2:两条都是 legacy(lineage_status 非 complete)→ 只进 *_legacy 列
    ('https://p0src.test/legacy-high', '装修建材', 'kimi', 'answer_adopted',     6.000000,
     'legacy_unknown', 'legacy_unknown', 'p0-5', 'P0LINEAGE-ROUND'),
    ('https://p0src.test/legacy-low',  '装修建材', 'qwen', 'search_result_only', 0.100000,
     'legacy_unknown', 'legacy_unknown', 'p0-6', 'P0LINEAGE-ROUND'),

    -- 文章 3:一条新口径 cited_source + 一条 legacy answer_adopted(混合)
    ('https://p0src.test/mix-new',    '装修建材', 'doubao', 'cited_source',   3.750000,
     'complete', 'direct_observation', 'p0-7', 'P0LINEAGE-ROUND'),
    ('https://p0src.test/mix-legacy', '装修建材', 'kimi',   'answer_adopted', 5.500000,
     'legacy_unknown', 'legacy_unknown', 'p0-8', 'P0LINEAGE-ROUND');
