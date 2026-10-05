-- [答案实体 shadow 层] 手动迁移(文档级 · 不进自动迁移链)
-- 与 db/research_answer_entity_db.init_research_answer_entity_tables() 逐字对应(幂等兜底)。
-- 纪律:CREATE IF NOT EXISTS · 不 DROP/TRUNCATE/RENAME · shadow 表松耦合(raw_id 逻辑外键,不加 DB 级 FK constraint,
--       与 geo_research_source_signals 同惯例:避免建表顺序耦合 + 重处理时的引用完整性阻断)。
-- dry-run:BEGIN; <本文件>; -- 核对无 column does not exist / 幂等重跑 0 变化; ROLLBACK;

-- 🔴 粒度(出口严审 P1-1):fact = answer 级(geo_research_raw 一行一 citation·共享 answer_text)。
-- 去重键 UNIQUE(engine, batch_id, answer_hash)· 与 api/research_monitor_citations_api 的
-- GROUP BY engine_norm, batch_id, MD5(answer_text) 同款范式。batch_id 用 COALESCE 存(NULL→'')避免 NULL≠NULL 逃去重。
CREATE TABLE IF NOT EXISTS geo_research_answer_facts (
    id BIGSERIAL PRIMARY KEY,
    raw_id INTEGER NOT NULL,                 -- 锚点 raw id(该答案的代表行·MIN(id))
    industry TEXT NOT NULL DEFAULT '',
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',
    query TEXT NOT NULL DEFAULT '',
    engine VARCHAR(40) NOT NULL DEFAULT '',  -- 归一后引擎
    batch_id VARCHAR(120) NOT NULL DEFAULT '',   -- COALESCE(batch_id,'') 存 · 参与去重键
    answer_hash CHAR(32) NOT NULL DEFAULT '',    -- MD5(answer_text) · answer 级去重键
    raw_ids JSONB NOT NULL DEFAULT '[]'::jsonb,  -- 该答案覆盖的全部 citation raw 行
    citation_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
    answer_excerpt TEXT,                     -- 短摘要,不存完整 answer_text
    entity_count INTEGER NOT NULL DEFAULT 0,
    quality_flag VARCHAR(20) NOT NULL DEFAULT '',   -- auto/degraded/skipped
    extractor_version VARCHAR(40) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (engine, batch_id, answer_hash)
);
CREATE INDEX IF NOT EXISTS idx_answer_facts_industry ON geo_research_answer_facts(industry_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_answer_facts_engine ON geo_research_answer_facts(engine);
CREATE INDEX IF NOT EXISTS idx_answer_facts_batch ON geo_research_answer_facts(batch_id);

CREATE TABLE IF NOT EXISTS geo_research_answer_entities (
    id BIGSERIAL PRIMARY KEY,
    answer_fact_id BIGINT NOT NULL,          -- 逻辑 FK → geo_research_answer_facts.id
    raw_id INTEGER NOT NULL DEFAULT 0,
    industry_key VARCHAR(100) NOT NULL DEFAULT 'general',   -- 反规范化(读侧 GROUP BY 免 join)
    engine VARCHAR(40) NOT NULL DEFAULT '',                 -- 反规范化(读侧 GROUP BY 免 join)
    entity_name TEXT NOT NULL,               -- 原文名
    entity_key VARCHAR(80) NOT NULL,         -- 🔴 归一键(build_entity_key + brand-alias 折叠)
    entity_type VARCHAR(20) NOT NULL DEFAULT 'brand',       -- brand/company/product
    recommendation_rank INTEGER,
    mention_rank INTEGER,
    recommendation_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_phrases JSONB NOT NULL DEFAULT '[]'::jsonb,    -- 短语,非原文段
    source_urls JSONB NOT NULL DEFAULT '[]'::jsonb,         -- 支撑来源 URL · 可 join 回 geo_research_source_signals(§11 消费)
    confidence NUMERIC(5,3) NOT NULL DEFAULT 0,
    llm_model VARCHAR(80) NOT NULL DEFAULT '',
    extractor_version VARCHAR(40) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (answer_fact_id, entity_key)
);
CREATE INDEX IF NOT EXISTS idx_answer_entities_industry ON geo_research_answer_entities(industry_key, entity_key);
CREATE INDEX IF NOT EXISTS idx_answer_entities_key ON geo_research_answer_entities(entity_key);
CREATE INDEX IF NOT EXISTS idx_answer_entities_fact ON geo_research_answer_entities(answer_fact_id);
CREATE INDEX IF NOT EXISTS idx_answer_entities_engine ON geo_research_answer_entities(engine);
