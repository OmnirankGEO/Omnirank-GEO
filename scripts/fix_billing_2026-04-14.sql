-- ============================================================
-- 计费修复脚本 2026-04-14
-- 问题：GEO诊断定价 930→应为 650，用户被多扣且失败未退费
--
-- 使用方法（先备份再执行）：
--   docker exec omnirank-db pg_dump -U geo_admin geo_agentscope > backup_before_billing_fix.sql
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/fix_billing_2026-04-14.sql
-- ============================================================

BEGIN;

-- ============ 第一步：修正功能定价 ============

-- GEO专项诊断 930→650
UPDATE feature_pricing SET cost_points = 650 WHERE feature_code = 'geo_diagnosis' AND cost_points = 930;

-- 社媒专项诊断 930→650
UPDATE feature_pricing SET cost_points = 650 WHERE feature_code = 'social_diagnosis' AND cost_points = 930;

-- 全面诊断 1560→1040
UPDATE feature_pricing SET cost_points = 1040 WHERE feature_code = 'full_diagnosis' AND cost_points = 1560;

-- 验证修正结果
SELECT feature_code, cost_points
FROM feature_pricing
WHERE feature_code IN ('geo_diagnosis', 'social_diagnosis', 'full_diagnosis');


-- ============ 第二步：查看受影响的用户（先看不改） ============

-- 列出所有被多扣的 GEO 诊断消费记录（按930扣的）
-- 如果诊断成功：多扣了 930-650=280 积分
-- 如果诊断失败且未退费：多扣了整个 930 积分
SELECT
    pt.user_id,
    pt.feature_code,
    pt.amount,
    pt.created_at,
    pt.description,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM point_transactions ref
            WHERE ref.user_id = pt.user_id
              AND ref.type = 'refund'
              AND ref.order_id = CAST(pt.id AS TEXT)
        ) THEN '已退费'
        ELSE '未退费'
    END AS refund_status
FROM point_transactions pt
WHERE pt.feature_code = 'geo_diagnosis'
  AND pt.type = 'consume'
  AND ABS(pt.amount) = 930
ORDER BY pt.created_at DESC;


-- ============ 第三步：退还多扣积分（手动执行，看完第二步结果再决定） ============
-- ⚠️ 以下是模板，需要根据第二步结果填入实际 user_id

-- 方法A：对于成功完成的诊断，每笔退还差额 280 积分
-- （需要人工确认哪些是成功的，把 user_id 填进去）
--
-- INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code, description)
-- SELECT
--     user_id, 'refund', point_type, 280,
--     CASE WHEN point_type = 'bonus'
--         THEN (SELECT bonus_points FROM user_wallets WHERE user_wallets.user_id = pt.user_id)
--         ELSE (SELECT paid_points FROM user_wallets WHERE user_wallets.user_id = pt.user_id)
--     END + 280,
--     'geo_diagnosis',
--     '定价修正退还差额(930→650)'
-- FROM point_transactions pt
-- WHERE pt.feature_code = 'geo_diagnosis' AND pt.type = 'consume' AND ABS(pt.amount) = 930
--   AND pt.user_id IN (你要退的用户ID列表);

-- 方法B：对于失败且未退费的诊断，退还全部 930 积分
-- （截图中的用户，需要确认 user_id）
--
-- 先查出该用户的未退费消费记录数量：
-- SELECT user_id, COUNT(*) as charge_count, SUM(ABS(amount)) as total_charged
-- FROM point_transactions
-- WHERE feature_code = 'geo_diagnosis' AND type = 'consume'
--   AND id NOT IN (SELECT CAST(order_id AS BIGINT) FROM point_transactions WHERE type = 'refund' AND order_id IS NOT NULL)
-- GROUP BY user_id
-- HAVING COUNT(*) > 1;
--
-- 确认后批量退还（用 UPDATE user_wallets 加回积分 + INSERT 退费记录）

COMMIT;

-- ============ 验证 ============
-- 执行后检查定价是否正确：
-- SELECT feature_code, cost_points FROM feature_pricing WHERE feature_code LIKE '%diagnosis%';
