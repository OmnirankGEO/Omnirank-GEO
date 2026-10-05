-- ============================================================
-- 回滚 · M2 服务商自设单篇内容成本 cost_per_article
-- 报价防亏资金批 · GEO CTO · 2026-06-07
-- ============================================================
-- 幂等 · 删列即回滚(列 DEFAULT NULL·删除不影响任何存量算价:无值时本就走系统动态成本)。
-- ⚠️ 删列会丢失服务商已自设的成本值;回滚前如需保留可先:
--    SELECT id, username, cost_per_article FROM users WHERE cost_per_article IS NOT NULL;
-- ============================================================

ALTER TABLE users DROP COLUMN IF EXISTS cost_per_article;
