-- scripts/migration_commission_backfill.sql
-- One-time migration: move historical commission earnings from paid_points to commission_points
-- Background: Before this feature, T+3 commission settlement went to paid_points.
-- Now commissions go to commission_points (for withdrawal). This script migrates historical data.

BEGIN;

-- 1. Sum all historical commission settlement transactions per user
-- These are transactions where type='commission_settlement' AND point_type='paid'
WITH legacy_commission AS (
  SELECT user_id, SUM(amount) as total
  FROM point_transactions
  WHERE type = 'commission_settlement' AND point_type = 'paid'
    AND created_at < '2026-04-21 00:00:00'
  GROUP BY user_id
)
UPDATE user_wallets w SET
  paid_points = GREATEST(0, paid_points - lc.total),
  commission_points = commission_points + LEAST(lc.total, w.paid_points),
  updated_at = NOW()
FROM legacy_commission lc
WHERE w.user_id = lc.user_id;

-- 2. Write audit trail for the migration
INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, description)
SELECT lc.user_id, 'legacy_commission_migration', 'commission',
       LEAST(lc.total, w.paid_points),
       w.commission_points,
       '历史佣金迁移：paid_points → commission_points'
FROM (
  SELECT user_id, SUM(amount) as total
  FROM point_transactions
  WHERE type = 'commission_settlement' AND point_type = 'paid'
    AND created_at < '2026-04-21 00:00:00'
  GROUP BY user_id
) lc
JOIN user_wallets w ON w.user_id = lc.user_id;

-- 3. Migrate existing agents to agent_friendly deduction preference
-- So they preserve commission_points for withdrawal instead of spending them
UPDATE user_wallets SET deduction_preference = 'agent_friendly'
WHERE agent_level >= 1
  AND (deduction_preference IS NULL OR deduction_preference = 'default')
  AND commission_points > 0;

COMMIT;
