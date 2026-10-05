-- 行业知识公共库表
CREATE TABLE IF NOT EXISTS industry_knowledge (
    id SERIAL PRIMARY KEY,
    level VARCHAR(10) NOT NULL,
    industry VARCHAR(100) NOT NULL,
    category VARCHAR(100),
    knowledge JSONB NOT NULL,
    source VARCHAR(50) DEFAULT 'auto',
    version INTEGER DEFAULT 1,
    generated_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP,
    search_count INTEGER DEFAULT 0,
    correction_count INTEGER DEFAULT 0,
    UNIQUE(level, industry, category)
);

CREATE INDEX IF NOT EXISTS idx_ik_lookup ON industry_knowledge(level, industry, category);
CREATE INDEX IF NOT EXISTS idx_ik_expires ON industry_knowledge(expires_at);

-- client_profiles 加品类和底稿字段
ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS category VARCHAR(100);
ALTER TABLE client_profiles ADD COLUMN IF NOT EXISTS industry_brief JSONB;

SELECT 'industry_knowledge table ready' AS status;
