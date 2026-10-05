-- migration_003_inspirations.sql
-- Phase 3: 灵感看板（品牌维度）

CREATE TABLE IF NOT EXISTS daily_inspirations (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER,                   -- 关联品牌，NULL 表示通用灵感
    category VARCHAR(30),               -- 'geo_trend' | 'ai_search' | 'social_media' | 'industry'
    title VARCHAR(300),
    summary TEXT,
    source_url VARCHAR(500),
    source_name VARCHAR(100),           -- '秘塔搜索' | '抖音' | '小红书' | '新闻'
    relevance_score FLOAT,
    tags JSONB,
    cover_image VARCHAR(500),
    published_at TIMESTAMP,
    fetched_at TIMESTAMP DEFAULT NOW(),
    is_featured BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_inspirations_date ON daily_inspirations(fetched_at);
CREATE INDEX IF NOT EXISTS idx_inspirations_category ON daily_inspirations(category);
CREATE INDEX IF NOT EXISTS idx_inspirations_brand ON daily_inspirations(brand_id);

-- 安全添加列（已存在的表）
ALTER TABLE daily_inspirations ADD COLUMN IF NOT EXISTS brand_id INTEGER;
ALTER TABLE daily_inspirations ADD COLUMN IF NOT EXISTS engagement_data JSONB;
ALTER TABLE daily_inspirations ADD COLUMN IF NOT EXISTS llm_breakdown TEXT;
