-- ============================================================
-- D3 · 代理 SKU 额度包全局加价系数(2026-05-30 · 工厂模式)
-- ============================================================
-- 代理设一个全局系数,一键把所有额度包售价定为 出厂价 × 系数。
-- ⚠️ 独立于 users.quote_markup_ratio(那是 GEO 关键词报价的 markup,别混)。
-- 幂等:IF NOT EXISTS · 既有 DB / fresh DB 都安全。

ALTER TABLE users ADD COLUMN IF NOT EXISTS agent_sku_markup_ratio NUMERIC(4,2);

COMMENT ON COLUMN users.agent_sku_markup_ratio IS
  'V3.5 代理 SKU 额度包全局加价系数(零售价 = 出厂价 × 系数)· 1.0~3.0 · 区别于 quote_markup_ratio(GEO 关键词报价)';

-- 验证:
-- SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name='users' AND column_name='agent_sku_markup_ratio';
