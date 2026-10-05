-- migration_geo_engine_stats_normalize_2026_07_03.sql
-- 归属:GEO 数据飞轮融入 · 批次 B1(B1-1 引擎名归一 + B1-2 空名平台哨兵清理)
-- 性质:一次性数据迁移 · 幂等 · 无破坏性(只重写 geo_engine_stats 自身,事务内 DELETE+INSERT 原子)
-- 由 Deploy-CTO 手动 psql 应用(项目无 .sql 自动 runner)。建议应用前先
--   docker exec omnirank-db pg_dump -U geo_admin -t geo_engine_stats geo_agentscope > backup_ges_YYYYMMDD.sql
--
-- 背景:
--   B1-1 round_runner 写 geo_research_raw.engine 用小写别名(doubao/kimi/deepseek/qwen),
--        聚合层历史漏归一 → geo_engine_stats 同一引擎裂成多行(小写 + 中文并存),
--        且 get_industry_engine_scores 的 engine_weights.get(eng,0.1) 永远 fallback 0.1(权重失效)。
--   B1-2 零引用哨兵行 cited_platform='' 漏过滤 → geo_engine_stats 出现 platform='' 空名平台行,
--        带非零 citation_rate,挤占推荐。
--   代码侧已在 services/placement_service.aggregate_research_stats 修复(归一 + 过滤);
--   本迁移清理历史已落库的裂行/空名行(未即时重跑聚合的行业也能立刻纠正)。
--
-- SQL 4 维核验:
--   列名/类型对 db/diagnosis_db.py:982-994 建表语句(industry TEXT, engine TEXT, platform TEXT,
--     citation_count INTEGER, total_queries INTEGER, citation_rate REAL, avg_position REAL,
--     sample_queries INTEGER, last_updated TIMESTAMP);UNIQUE(industry, engine, platform)。
--   归属:geo_engine_stats 自身;不动 geo_research_raw(哨兵行语义保留在 raw)。
--   幂等:再跑时 engine 已 canonical、无空平台行 → 归一 CASE 走 ELSE、GROUP BY 不再合并 → 结果一致
--         (last_updated 刷新为 NOW(),可接受)。
--   合并口径:citation_rate 按 total_queries 加权、avg_position 按 citation_count 加权(合理近似;
--         下一次自然聚合会用 raw 精确重算)。

BEGIN;

CREATE TEMP TABLE _ges_merged ON COMMIT DROP AS
SELECT
    industry,
    CASE
        WHEN LOWER(engine) IN ('doubao', '豆包') THEN '豆包'
        WHEN LOWER(engine) IN ('kimi') THEN 'Kimi'
        WHEN LOWER(engine) IN ('deepseek') THEN 'DeepSeek'
        WHEN LOWER(engine) IN ('qwen', '千问') THEN '千问'
        ELSE engine
    END AS engine,
    platform,
    SUM(citation_count)::INTEGER AS citation_count,
    SUM(total_queries)::INTEGER  AS total_queries,
    CASE WHEN SUM(total_queries) > 0
         THEN (SUM(citation_rate::double precision * total_queries) / SUM(total_queries))::REAL
         ELSE AVG(citation_rate)::REAL END AS citation_rate,
    CASE WHEN SUM(citation_count) > 0
         THEN (SUM(avg_position::double precision * citation_count) / SUM(citation_count))::REAL
         ELSE AVG(avg_position)::REAL END AS avg_position,
    MAX(sample_queries)::INTEGER AS sample_queries
FROM geo_engine_stats
WHERE platform IS NOT NULL AND platform <> ''   -- [B1-2] 丢弃空名平台哨兵历史行
GROUP BY 1, 2, 3;                                -- 1=industry, 2=归一engine, 3=platform → 保证 UNIQUE 唯一

DELETE FROM geo_engine_stats;

INSERT INTO geo_engine_stats
    (industry, engine, platform, citation_count, total_queries,
     citation_rate, avg_position, sample_queries, last_updated)
SELECT industry, engine, platform, citation_count, total_queries,
       citation_rate, avg_position, sample_queries, NOW()
FROM _ges_merged;

COMMIT;

-- 验收(应用后手动核对):
--   SELECT DISTINCT engine FROM geo_engine_stats;             -- 只应出现 豆包/Kimi/DeepSeek/千问(及未知别名原样)
--   SELECT count(*) FROM geo_engine_stats WHERE platform='';  -- 应为 0
