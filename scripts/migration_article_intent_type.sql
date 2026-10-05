-- migration_article_intent_type.sql
-- P15 (2026-06-01) · geo_research_articles 加文章意图分类 5 列 + check + index
--
-- 业务背景:
--   stage 4 清洗完成后 · stage 4.5 调 LLM (DeepSeek) 分类成 8 种意图:
--   榜单推荐 / 指南教程 / 资讯长文 / 对比评测 / 数据报告 / 政策权威 / 定义百科 / FAQ
--   占比给老板决策 AI 写作时输出种类的参考 (一期不反哺 · 只展现)
--
-- 跑法: docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope < scripts/migration_article_intent_type.sql
-- 幂等: ADD COLUMN IF NOT EXISTS + ADD CONSTRAINT 用 DO $$ 包 IF NOT EXISTS · 重跑不挂

ALTER TABLE geo_research_articles
    ADD COLUMN IF NOT EXISTS intent_type VARCHAR(20),
    ADD COLUMN IF NOT EXISTS intent_confidence NUMERIC(4,3),
    ADD COLUMN IF NOT EXISTS intent_reason TEXT,
    ADD COLUMN IF NOT EXISTS intent_model VARCHAR(50),
    ADD COLUMN IF NOT EXISTS intent_classified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_geo_research_articles_intent_type
    ON geo_research_articles(intent_type)
    WHERE intent_type IS NOT NULL;


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'geo_research_articles_intent_type_check'
    ) THEN
        ALTER TABLE geo_research_articles
            ADD CONSTRAINT geo_research_articles_intent_type_check
            CHECK (
                intent_type IS NULL OR intent_type IN (
                    'ranking', 'tutorial', 'long_form', 'comparison',
                    'data_report', 'policy', 'definition', 'faq'
                )
            );
    END IF;
END $$;

COMMENT ON COLUMN geo_research_articles.intent_type IS
    'P15 · 文章意图分类 8 类:ranking/tutorial/long_form/comparison/data_report/policy/definition/faq';
COMMENT ON COLUMN geo_research_articles.intent_confidence IS
    'P15 · LLM 给的分类置信度 0-1 · NULL = 未分类或失败';
COMMENT ON COLUMN geo_research_articles.intent_reason IS
    'P15 · LLM 给的分类理由 (debug 用)';
COMMENT ON COLUMN geo_research_articles.intent_model IS
    'P15 · 分类用的模型 (默认 deepseek-v4-flash)';
COMMENT ON COLUMN geo_research_articles.intent_classified_at IS
    'P15 · 分类时间 · NULL = 未分类';

INSERT INTO _migration_markers (marker, applied_at, note)
VALUES (
    'article_intent_type_2026_06_01',
    NOW(),
    'P15: 加 intent_type 5 列 + check + index'
)
ON CONFLICT (marker) DO NOTHING;

-- 校验:
--   SELECT column_name, data_type, character_maximum_length
--     FROM information_schema.columns
--    WHERE table_name = 'geo_research_articles' AND column_name LIKE 'intent_%';
--   -- 期望 5 行
