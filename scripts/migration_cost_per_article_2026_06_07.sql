-- ============================================================
-- M2 · 服务商自设单篇内容成本 cost_per_article
-- 报价防亏资金批 · GEO CTO · 2026-06-07
-- ============================================================
-- 幂等 · 可回滚 · 与 db/auth_db.py init 同口径(init 也会 ADD COLUMN IF NOT EXISTS·二者任一先跑均可)
--
-- 语义:
--   NULL      = 走系统动态成本(竞品来源权威度加权 35-350 · 默认兜底 ¥60)  ← 存量用户默认·零影响
--   非 NULL   = 服务商自设单篇成本 · A 完全覆盖系统动态(投央媒服务商可设真实 ¥350)
-- 与 M1 成本地板联动:cost_floor = ceil(篇数 × cost_per_article × markup) → 报价永不亏于真实成本。
--
-- ⚠️ DEFAULT NULL:不回溯/不批量 UPDATE 存量·老用户继续走系统动态(向后兼容)。
-- ============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS cost_per_article NUMERIC(6,2) DEFAULT NULL;

-- 验证(应返回 1 行 · data_type=numeric · is_nullable=YES · column_default 为空):
-- SELECT column_name, data_type, numeric_precision, numeric_scale, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_name='users' AND column_name='cost_per_article';
