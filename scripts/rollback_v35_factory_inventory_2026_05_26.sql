-- ============================================================
-- V3.5 工厂模式 · 回滚 SQL
-- ============================================================
-- ⚠️ 危险:回滚会删除所有 V3.5 新表 · 数据丢失
-- 仅在 migration 失败 + DB 完全空白的情况下使用
-- prod 永远不要直接跑此回滚 · 必须先备份
--
-- [Codex r4 P0-2] 不写 BEGIN/COMMIT · 由 Python wrapper 管事务
-- ============================================================


-- 1. 删 system_settings 配置
DELETE FROM system_settings WHERE key IN (
    'platform_fee_config',
    'LEGACY_REFERRAL_V32_ENABLED',
    'V35_FACTORY_INVENTORY_ENABLED',
    'agent_inventory_alert_config'
);

-- 2. 删 11 张新表(顺序:先 child 后 parent · 防 FK)
DROP TABLE IF EXISTS agent_settlement_request_items CASCADE;
DROP TABLE IF EXISTS agent_settlement_requests CASCADE;
DROP TABLE IF EXISTS agent_revenue_ledger CASCADE;
DROP TABLE IF EXISTS customer_agent_bindings CASCADE;
DROP TABLE IF EXISTS customer_credit_transactions CASCADE;
DROP TABLE IF EXISTS customer_agent_credit_wallets CASCADE;
DROP TABLE IF EXISTS agent_inventory_transactions CASCADE;
DROP TABLE IF EXISTS agent_inventory_wallets CASCADE;
DROP TABLE IF EXISTS agent_sku_overrides CASCADE;
DROP TABLE IF EXISTS sku_templates CASCADE;
DROP TABLE IF EXISTS agent_tax_profiles CASCADE;

-- 3. 回滚 recharge_orders 扩列
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS agent_user_id;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS sku_template_id;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS factory_cents;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS agent_revenue_cents;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS agent_margin_before_tax_cents;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS gateway_fee_bps;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS gateway_fee_cents;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS settlement_service_fee_bps;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS settlement_service_fee_cents;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS tax_rate_bps;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS tax_withholding_cents;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS pricing_snapshot_jsonb;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS settlement_mode;
DROP INDEX IF EXISTS idx_recharge_agent;
DROP INDEX IF EXISTS idx_recharge_settlement_mode;

-- 4. 回滚 users 扩列
ALTER TABLE users DROP COLUMN IF EXISTS referred_by_agent_id;
ALTER TABLE users DROP COLUMN IF EXISTS agent_bound_at;
DROP INDEX IF EXISTS idx_users_referred_agent;

-- 5. 回滚 feature_pricing 扩列
ALTER TABLE feature_pricing DROP COLUMN IF EXISTS wholesale_cents;
ALTER TABLE feature_pricing DROP COLUMN IF EXISTS wholesale_points;
ALTER TABLE feature_pricing DROP COLUMN IF EXISTS platform_cost_cents;

-- 6. 回滚 mhz_media 扩列
ALTER TABLE mhz_media DROP COLUMN IF EXISTS wholesale_cents;
ALTER TABLE mhz_media DROP COLUMN IF EXISTS wholesale_points;
ALTER TABLE mhz_media DROP COLUMN IF EXISTS platform_cost_cents;

-- [Codex r4 P0-2] 无 COMMIT · Python wrapper 管
