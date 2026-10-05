-- P0.5 空壳 quote 止血 · migration(CTO-15.7 2026-04-24)
--
-- 触发:quotes 表 157 份 · 真付款 1 份 ¥75.88 · 98% 空壳 draft
-- 元凶:workflows/diagnosis_workflow.py:771-787 + 1795-1819 自动 save_quote
-- (已在本 commit 删除)
--
-- 老板批:
--   · 不加 5 JSONB 字段(挪 P1.14 主题包交易真相设计)
--   · 只加最小 soft delete 字段
--   · 存量 ~154 空壳软删 · 列表过滤 WHERE deleted_at IS NULL
--
-- 部署前必做:
--   docker exec omnirank-db pg_dump -U geo_admin geo_agentscope -t quotes > backup_quotes_$(date +%Y%m%d_%H%M).sql

BEGIN;

-- 加软删 + 清理原因字段
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP DEFAULT NULL;
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS cleanup_reason TEXT DEFAULT NULL;

-- 部分索引加速"过滤空壳"类查询
CREATE INDEX IF NOT EXISTS idx_quotes_deleted_at
  ON quotes(deleted_at) WHERE deleted_at IS NULL;

-- 软删存量空壳 quote(total_articles=0 AND monthly_price=0 AND status='draft' AND markdown 空)
UPDATE quotes
SET deleted_at = NOW(),
    cleanup_reason = 'empty_auto_quote_from_diagnosis_p0.5'
WHERE total_articles = 0
  AND monthly_price = 0
  AND status = 'draft'
  AND (markdown IS NULL OR markdown = '')
  AND deleted_at IS NULL;

COMMIT;

-- ========== 验收查询 ==========
-- 1. 新增字段存在
-- \d quotes  -- 看到 deleted_at / cleanup_reason

-- 2. 存量空壳软删数
-- SELECT COUNT(*) FROM quotes
-- WHERE deleted_at IS NOT NULL AND cleanup_reason = 'empty_auto_quote_from_diagnosis_p0.5';
-- 期望:~150+

-- 3. 剩余真实 quote
-- SELECT COUNT(*), COALESCE(SUM(monthly_price), 0) AS total_price
-- FROM quotes WHERE deleted_at IS NULL;

-- 4. 应用层过滤验证:代理报价中心 / 客户 portal 不应看到 deleted_at 非空的 quote
-- (后端 get_quotes_list 等接口需加 WHERE deleted_at IS NULL · 下一 commit 处理)

-- ========== 回滚(仅紧急情况)==========
-- UPDATE quotes SET deleted_at = NULL, cleanup_reason = NULL
--   WHERE cleanup_reason = 'empty_auto_quote_from_diagnosis_p0.5';
-- ALTER TABLE quotes DROP COLUMN IF EXISTS cleanup_reason;
-- ALTER TABLE quotes DROP COLUMN IF EXISTS deleted_at;
