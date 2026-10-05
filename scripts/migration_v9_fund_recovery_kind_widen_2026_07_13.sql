-- ============================================================================
-- [v9 · Deploy-CTO NO-GO P1-2 返工 2026-07-13] fund_recovery_orders.kind CHECK 放宽
--   additive · 幂等 · 在 v7 fund_recovery fencing 迁移之后执行(或独立执行 · 表由 v6 建)。
-- 背景:
--   P1-2 修复引入渠道收益记账/冲销的 exactly-once 耐久补偿工单(source='channel_revenue',
--   kind='record' / 'reverse')。这两个 kind 值不在 v6 老 CHECK ('refund','release','commit','state_fix') 内,
--   若不放宽 CHECK,insert_recovery_order_cursor(kind='record'/'reverse') 会违反 CHECK 抛错 →
--   主充值/退款事务回滚 → 无限 fail-closed 重试(brick)。
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v9_fund_recovery_kind_widen_2026_07_13.sql
-- 验证:
--   docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d+ fund_recovery_orders" | grep kind
-- 说明:代码侧 db/fund_recovery_db.init_fund_recovery_tables 亦幂等自建/自升级(随 init_wallet_tables 启动)。
-- ============================================================================

BEGIN;

-- 放宽 kind CHECK(DROP+ADD 幂等 · 与 status CHECK 同法)· 现有行全在旧集合内,加宽不会违反已有数据。
ALTER TABLE fund_recovery_orders DROP CONSTRAINT IF EXISTS fund_recovery_orders_kind_check;
ALTER TABLE fund_recovery_orders ADD CONSTRAINT fund_recovery_orders_kind_check
  CHECK (kind IN ('refund','release','commit','state_fix','record','reverse'));

INSERT INTO _migrations (name, applied_at)
VALUES ('v9_fund_recovery_kind_widen_2026_07_13', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ROLLBACK(还原 v7 口径 · 仅在无 channel_revenue 工单残留时安全):
--   DELETE FROM fund_recovery_orders WHERE kind IN ('record','reverse');  -- 或先人工核对
--   ALTER TABLE fund_recovery_orders DROP CONSTRAINT IF EXISTS fund_recovery_orders_kind_check;
--   ALTER TABLE fund_recovery_orders ADD CONSTRAINT fund_recovery_orders_kind_check
--     CHECK (kind IN ('refund','release','commit','state_fix'));
--   DELETE FROM _migrations WHERE name='v9_fund_recovery_kind_widen_2026_07_13';
