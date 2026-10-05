-- migration_004_material_cards.sql
-- 素材卡：给销售提供口播可直接使用的素材

CREATE TABLE IF NOT EXISTS material_cards (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER,                        -- 关联品牌
    card_type VARCHAR(30) NOT NULL,          -- 'search_demo' | 'case_study' | 'industry_insight' | 'script_template'
    title VARCHAR(200) NOT NULL,             -- 素材卡标题
    content TEXT NOT NULL,                   -- 主内容（话术/脚本/数据点）
    subtitle VARCHAR(200),                   -- 副标题/分类标签
    extra_data JSONB,                        -- 扩展数据（搜索词、数据源等）
    usage_count INTEGER DEFAULT 0,           -- 被复制/使用次数
    is_pinned BOOLEAN DEFAULT FALSE,         -- 是否置顶
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_material_cards_brand ON material_cards(brand_id);
CREATE INDEX IF NOT EXISTS idx_material_cards_type ON material_cards(card_type);
CREATE INDEX IF NOT EXISTS idx_material_cards_pinned ON material_cards(is_pinned DESC, updated_at DESC);
