-- 2026-04-30 · 调研数据月份权重
-- 引入按月加权聚合机制：
--   - 全局衰减系数与最小题数门槛存到 geo_aggregation_config
--   - 管理员可对 (行业, 月份) 单独覆盖权重，存到 geo_month_weights
-- 幂等：CREATE TABLE IF NOT EXISTS + INSERT ON CONFLICT DO NOTHING

CREATE TABLE IF NOT EXISTS geo_month_weights (
    id            SERIAL PRIMARY KEY,
    industry      TEXT NOT NULL,
    year_month    TEXT NOT NULL,                                   -- 'YYYY-MM'
    weight        REAL NOT NULL CHECK (weight >= 0 AND weight <= 10),
    note          TEXT,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by    TEXT,
    UNIQUE(industry, year_month)
);
CREATE INDEX IF NOT EXISTS idx_month_weights_industry ON geo_month_weights(industry);

CREATE TABLE IF NOT EXISTS geo_aggregation_config (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    description   TEXT,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by    TEXT
);

INSERT INTO geo_aggregation_config (key, value, description) VALUES
  ('decay_factor',          '0.3', '月份指数衰减系数：当月权重 1.0、上月 decay、上上月 decay²；推荐 0.3'),
  ('min_queries_per_month', '10',  '某月题数低于此值则跳过该月聚合，避免单月样本不足导致波动')
ON CONFLICT (key) DO NOTHING;
