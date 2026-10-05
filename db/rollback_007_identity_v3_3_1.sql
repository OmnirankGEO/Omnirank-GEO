-- =================================================
-- 身份模型 V3.3.1 · Rollback 007
-- 5 分钟可执行 · 老板 + Deploy-CTO 双签后才能跑
-- =================================================
-- ⚠️ WARNING:
-- 1. 此脚本会清空 V3.3.1 新表数据
-- 2. system_settings V3.3.1 flags 全部删除
-- 3. wallet_transactions / recharge_orders / subscription_orders 的 source 字段保留
--    (避免破坏其他已存数据 · 字段保留不影响业务)
-- 4. V3.2.1 老表数据不动
-- =================================================

BEGIN;

-- 1. 删 V3.3.1 新表
DROP TABLE IF EXISTS failed_service_fee_jobs;
DROP TABLE IF EXISTS invite_codes;
DROP TABLE IF EXISTS service_fee_clawback_pending;
DROP TABLE IF EXISTS service_fee_conversion_quota;
DROP TABLE IF EXISTS service_fee_conversion_orders;
DROP TABLE IF EXISTS service_fee_settlements;
DROP TABLE IF EXISTS service_fee_records;

-- 2. 删 V3.3.1 feature flags(保留其他 system_settings)
DELETE FROM system_settings WHERE key LIKE 'V3_3_1_%';
DELETE FROM system_settings WHERE key IN (
    'service_fee_conversion_bonus_rate',
    'service_fee_conversion_quota_default',
    'service_fee_conversion_quota_premium',
    'service_fee_conversion_min_age_days',
    'service_fee_withdrawal_min_amount',
    'service_fee_withdrawal_max_per_week',
    'service_fee_withdrawal_invoice_threshold',
    'service_fee_withdrawal_dual_sign_threshold',
    'service_fee_rate',
    'referral_bonus_rate',
    'referral_bonus_settle_days',
    'service_fee_settle_days',
    'l1_monthly_invite_quota'
);

-- 3. 删 finance_reviewer role(V3.3.1 新增)
DELETE FROM roles WHERE name='finance_reviewer';

-- 4. 删 rbac_route_audit 表
DROP TABLE IF EXISTS rbac_route_audit;

-- 5. user_wallets 字段保留(避免破坏数据 · 业务不依赖)
--    is_kyc_passed / bank_account_verified / agent_tier 字段保留
--    如必须清:
--      ALTER TABLE user_wallets DROP COLUMN IF EXISTS is_kyc_passed;
--      ALTER TABLE user_wallets DROP COLUMN IF EXISTS bank_account_verified;
--      ALTER TABLE user_wallets DROP COLUMN IF EXISTS agent_tier;

-- 6. pending_bonus_records 的 V3.3.1 新字段保留(不影响业务)
--    net_cash_revenue_yuan / source 保留

COMMIT;

-- =================================================
-- 回滚后验证
-- =================================================
-- 1. 5 张新表已删:
--   SELECT COUNT(*) FROM information_schema.tables
--    WHERE table_name IN ('service_fee_records','service_fee_settlements',
--      'service_fee_conversion_orders','service_fee_clawback_pending',
--      'service_fee_conversion_quota');
--   应返回 0
--
-- 2. V3.3.1 flags 已删:
--   SELECT COUNT(*) FROM system_settings WHERE key LIKE 'V3_3_1_%';
--   应返回 0
--
-- 3. 老 V3.1/V3.2 数据完整:
--   SELECT COUNT(*) FROM pending_commissions;  -- 不应 0(若 V3.1 已有数据)
--   SELECT COUNT(*) FROM referral_bonus_records;  -- 不应 0
