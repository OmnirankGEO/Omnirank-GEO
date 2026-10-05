-- Migration 005: 关键词达标倒计时功能
-- quotes 表增加服务期限字段 + keyword_compliance_log 每日达标记录表

-- 1. quotes 表加字段
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS service_days INT DEFAULT 365;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS service_start_date DATE;

-- 回填 service_start_date：优先 paid_at，其次 confirmed_at
UPDATE quotes
SET service_start_date = COALESCE(
    DATE(paid_at),
    DATE(confirmed_at),
    DATE(created_at)
)
WHERE service_start_date IS NULL;

-- 2. keyword_compliance_log 表
CREATE TABLE IF NOT EXISTS keyword_compliance_log (
    id SERIAL PRIMARY KEY,
    keyword_id INT NOT NULL,
    keyword_source VARCHAR(20) NOT NULL DEFAULT 'confirmed',
    quote_id INT NOT NULL,
    check_date DATE NOT NULL,
    detection_rate NUMERIC(5,1),
    target_rate INT NOT NULL,
    is_compliant BOOLEAN NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(keyword_id, keyword_source, check_date)
);

CREATE INDEX IF NOT EXISTS idx_kcl_keyword ON keyword_compliance_log(keyword_id, keyword_source);
CREATE INDEX IF NOT EXISTS idx_kcl_quote ON keyword_compliance_log(quote_id, check_date);
