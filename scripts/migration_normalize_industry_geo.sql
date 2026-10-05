-- P14-v5 (2026-05-27) · 行业名归一: 'GEO' → 'geo服务' · 全表覆盖 + 外键安全 + 拼接字段防御
--
-- 背景:
--   历史 'GEO' 行业字段分散在多张表 · 必须一次性归一到 'geo服务'
--   否则发布参谋 summary 显示 'geo服务' 但矩阵查 stats 表里仍叫 'GEO' · 返 0 个平台断链
--
-- v5 修复 (review 反馈生产边界):
--   1. industries 表合并: 若 prod 同时存在 'GEO' 和 'geo服务' 两行 · 直接 DELETE 'GEO'
--      会因为 geo_research_prompts.industry_id / geo_research_article_citations.industry_id
--      的外键约束失败. 改成: 先迁外键到 new_id · 再 DELETE 老行
--   2. batches.industry 是逗号拼接字段 (如 'GEO,房地产'). 之前 exact match 漏掉.
--      改用 regex 替换 · 处理首/中/尾 + 单独情形 · idempotent (geo服务 不会再被匹配 GEO)
--
-- 本脚本覆盖所有 industry 字段表:
--   1. geo_research_raw.industry
--   2. geo_engine_stats.industry              (发布参谋矩阵 · 必须改否则断链)
--   3. geo_research_articles.primary_industry
--   4. geo_month_weights.industry
--   5. geo_research_batches.industry          (逗号拼接 · regex 安全)
--   6. geo_research_industries.name           (合并 · 安全迁外键)
--
-- idempotent: 跑完后没行可改 · 重复跑不会出错
--
-- 跑后必做:
--   python -c "from services.placement_service import get_placement_service; \
--              get_placement_service().aggregate_research_stats(['geo服务'])"
--
-- 跑法:
--   psql -h <host> -U <user> -d <db> -f migration_normalize_industry_geo.sql

BEGIN;

-- 1. raw 表 (主表)
UPDATE geo_research_raw
   SET industry = 'geo服务'
 WHERE industry = 'GEO';

-- 2. engine_stats 表 (发布参谋矩阵数据源)
UPDATE geo_engine_stats
   SET industry = 'geo服务'
 WHERE industry = 'GEO';

-- 3. articles 表
UPDATE geo_research_articles
   SET primary_industry = 'geo服务'
 WHERE primary_industry = 'GEO';

-- 4. month_weights 表
UPDATE geo_month_weights
   SET industry = 'geo服务'
 WHERE industry = 'GEO';

-- 5. batches 表 · industry 字段可能是逗号拼接 (如 'GEO,房地产')
--    用 regex 替换 · 锚定单词边界 (开头/逗号 + GEO + 结尾/逗号)
--    g flag · 处理一行内多个匹配 (虽然不应该有 'GEO,GEO' 但防御性)
UPDATE geo_research_batches
   SET industry = regexp_replace(industry, '(^|,)GEO(,|$)', '\1geo服务\2', 'g')
 WHERE industry ~ '(^|,)GEO(,|$)';

-- 6. industries 表 · 必须先迁外键再删 · 防止 prod 双行业行外键约束失败
DO $$
DECLARE
    v_old_id INT;
    v_new_id INT;
BEGIN
    SELECT id INTO v_old_id FROM geo_research_industries WHERE name = 'GEO';
    SELECT id INTO v_new_id FROM geo_research_industries WHERE name = 'geo服务';

    -- case 1: 'GEO' 不存在 → noop (含 v5 之前已经清过 / 全新部署)
    IF v_old_id IS NULL THEN
        RAISE NOTICE 'industries: 无 GEO 行 · 跳过';

    -- case 2: 只有 'GEO' · 没 'geo服务' → 简单 rename
    ELSIF v_new_id IS NULL THEN
        UPDATE geo_research_industries SET name = 'geo服务' WHERE id = v_old_id;
        RAISE NOTICE 'industries: rename GEO(id=%) → geo服务', v_old_id;

    -- case 3: 两个都有 · 必须先迁外键再删老行
    ELSE
        -- 迁 prompts 的 industry_id
        UPDATE geo_research_prompts
           SET industry_id = v_new_id
         WHERE industry_id = v_old_id;

        -- 迁 article_citations 的 industry_id
        UPDATE geo_research_article_citations
           SET industry_id = v_new_id
         WHERE industry_id = v_old_id;

        -- 老行外键无引用 · 安全删
        DELETE FROM geo_research_industries WHERE id = v_old_id;
        RAISE NOTICE 'industries: 合并 GEO(id=%) → geo服务(id=%) · 迁外键 + 删老行', v_old_id, v_new_id;
    END IF;
END $$;

-- 7. (P14-v6, slug 改 v7) 孤儿行业 backfill · raw/stats/articles 有但 industries 表缺失的自动补行
--    根治 GEO/通用 这种"绕过 industries 表直接灌 raw"的历史脏数据
--    新行 active=true · sort_order=999 (排到现有人工配置末尾)
--    slug 用 ind_<md5(name) 前 12 位> deterministic (跟应用层 slug_for_name 等价)
--      旧 v6 用 ind_<epoch_ms> 同毫秒批量补会撞 slug UNIQUE 导致 migration 炸 · v7 已修
--      md5 同 name 永远同 slug · 幂等 · 重复跑 ON CONFLICT (name) DO NOTHING 安全
--    后续管理员可在 "行业 & Prompts" tab 给它们加 prompts 才能跑批
DO $$
DECLARE
    v_name TEXT;
    v_slug TEXT;
BEGIN
    FOR v_name IN
        SELECT DISTINCT industry FROM (
            SELECT industry FROM geo_research_raw
             WHERE industry IS NOT NULL AND industry <> ''
            UNION
            SELECT industry FROM geo_engine_stats
             WHERE industry IS NOT NULL AND industry <> ''
            UNION
            SELECT primary_industry AS industry FROM geo_research_articles
             WHERE primary_industry IS NOT NULL AND primary_industry <> ''
        ) u
        WHERE NOT EXISTS (
            SELECT 1 FROM geo_research_industries i WHERE i.name = u.industry
        )
    LOOP
        -- 确定性 slug: ind_<md5(name) 前 12 位> · 跟 services/research_monitor/industry_registry.slug_for_name 等价
        v_slug := 'ind_' || substr(md5(v_name), 1, 12);
        INSERT INTO geo_research_industries (name, slug, sort_order, active)
        VALUES (v_name, v_slug, 999, TRUE)
        ON CONFLICT (name) DO NOTHING;
        RAISE NOTICE 'industries: backfill orphan name=% slug=%', v_name, v_slug;
    END LOOP;
END $$;

COMMIT;

-- 验证 (跑后跑一次):
--   SELECT 'raw',COUNT(*) FROM geo_research_raw WHERE industry='GEO'
--   UNION ALL SELECT 'stats',COUNT(*) FROM geo_engine_stats WHERE industry='GEO'
--   UNION ALL SELECT 'articles',COUNT(*) FROM geo_research_articles WHERE primary_industry='GEO'
--   UNION ALL SELECT 'batches_exact',COUNT(*) FROM geo_research_batches WHERE industry='GEO'
--   UNION ALL SELECT 'batches_concat',COUNT(*) FROM geo_research_batches WHERE industry ~ '(^|,)GEO(,|$)'
--   UNION ALL SELECT 'mw',COUNT(*) FROM geo_month_weights WHERE industry='GEO'
--   UNION ALL SELECT 'industries',COUNT(*) FROM geo_research_industries WHERE name='GEO';
--   全部应为 0
