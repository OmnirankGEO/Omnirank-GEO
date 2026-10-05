-- 代理私有报价系数 · 幂等 migration
-- 默认 2.0 倍，对应系统成本基准约 60 元/篇 -> 默认客户价约 120 元/篇。

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS quote_markup_ratio NUMERIC(4,2) DEFAULT 2.0;

UPDATE users
SET quote_markup_ratio = 2.0
WHERE quote_markup_ratio IS NULL;
