-- =====================================================================
-- 释放历史 8 笔 zombie freezes(2026-04-22 至 2026-05-22)
-- 来源: Deploy-CTO 2026-05-22 D0 实证(老板 D0 报告 §D0-5)
-- 影响: 4 用户 5460 积分被永久冻结 · 需手动 release 退回
--
-- ✅ 2026-05-22 老板已授权 COMMIT(see commit message)
--    末尾从 ROLLBACK; 改成 COMMIT; · Deploy-CTO 直跑即可
--    回滚预案:若验证不对 · 用 pre_zombie_release_*.sql restore 整张表
--
-- ⚠️ 由 Deploy-CTO 在 prod omnirank-db 执行 · 我(GEO 主 CTO)不 SSH prod
-- ⚠️ 必须 pg_dump 备份 user_wallets + point_freezes + point_transactions 后再跑
--
-- 跑法(Deploy-CTO):
--   1. docker exec omnirank-db pg_dump -U geo_admin -d geo_agentscope \
--      -t user_wallets -t point_freezes -t point_transactions \
--      > /tmp/pre_zombie_release_$(date +%Y%m%d_%H%M).sql
--   2. docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope \
--      < scripts/sql/release_zombie_freezes_2026-05-22.sql
--   3. 实证:
--      SELECT user_id, frozen_points FROM user_wallets WHERE user_id IN (25,28,40,46);
--      SELECT status, COUNT(*) FROM point_freezes GROUP BY status;
--
-- 风险:
--   - release_freeze 公开接口幂等 · 即便重跑也安全(已 released 的 no-op)
--   - 但 SQL 直改 status 跳过 application 层 · 没走 release_freeze 流水 INSERT
--   - 选择 A(纯 SQL · 快 · 0 流水)还是 B(调 release_freeze API · 慢 · 有流水)?
--     默认走 A · 流水通过本 SQL 内 INSERT 补齐
--     若想走 B · 可通过 admin POST /api/admin/release-freeze/{freeze_id} 逐个调
--
-- 2026-05-22 · CTO-15.23 GEO 主业 · 上线前最后一轮检测
-- =====================================================================

BEGIN;

-- 1. 备份:打印当前 8 笔 zombie 状态(放 transaction 内 · ROLLBACK 时也能看到)
\echo '=== zombie 8 笔状态(release 前)==='
SELECT id, user_id, feature_code, amount_total, amount_bonus, amount_commission, amount_paid,
       status, created_at, task_ref
FROM point_freezes
WHERE status = 'frozen'
  AND created_at < CURRENT_TIMESTAMP - INTERVAL '12 hours'
ORDER BY id;

\echo ''
\echo '=== 受影响用户 frozen_points 当前值 ==='
SELECT uw.user_id, uw.paid_points, uw.bonus_points, uw.commission_points, uw.frozen_points
FROM user_wallets uw
WHERE uw.user_id IN (
    SELECT DISTINCT user_id FROM point_freezes
    WHERE status = 'frozen' AND created_at < CURRENT_TIMESTAMP - INTERVAL '12 hours'
);

-- 2. 给每笔 zombie 退回 bonus/commission/paid 三池(按 freeze 时拆分比例)
WITH zombies AS (
    SELECT id, user_id, feature_code, amount_total, amount_bonus, amount_commission, amount_paid,
           brand_id
    FROM point_freezes
    WHERE status = 'frozen'
      AND created_at < CURRENT_TIMESTAMP - INTERVAL '12 hours'
)
UPDATE user_wallets uw
SET bonus_points = uw.bonus_points + COALESCE(z.amount_bonus, 0),
    commission_points = uw.commission_points + COALESCE(z.amount_commission, 0),
    paid_points = uw.paid_points + COALESCE(z.amount_paid, 0),
    frozen_points = uw.frozen_points - z.amount_total,
    updated_at = CURRENT_TIMESTAMP
FROM zombies z
WHERE uw.user_id = z.user_id;

-- 3. 标 point_freezes status='released'
UPDATE point_freezes
SET status = 'released',
    released_at = CURRENT_TIMESTAMP,
    reason = '[ManualSweep 2026-05-22] D0 实证 12h+ zombie 手动 release · 上线前清理'
WHERE status = 'frozen'
  AND created_at < CURRENT_TIMESTAMP - INTERVAL '12 hours';

-- 4. 补流水(point_transactions · 跟 release_freeze 函数同口径 source='balance_deduction')
-- 注:这里用单条汇总流水 · 每笔 zombie 写 1 条 release 流水
INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code,
                                description, order_id, brand_id, source, created_at)
SELECT pf.user_id,
       'release',
       'bonus',
       pf.amount_bonus,
       (SELECT bonus_points FROM user_wallets WHERE user_id = pf.user_id),
       pf.feature_code,
       '[手动 sweep]释放 12h+ zombie freeze id=' || pf.id::text,
       'MSWEEP-' || pf.id::text,
       pf.brand_id,
       'balance_deduction',
       CURRENT_TIMESTAMP
FROM point_freezes pf
WHERE pf.status = 'released'
  AND pf.released_at >= CURRENT_TIMESTAMP - INTERVAL '5 minutes'
  AND pf.amount_bonus > 0
  AND pf.reason LIKE '[ManualSweep 2026-05-22]%';

INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code,
                                description, order_id, brand_id, source, created_at)
SELECT pf.user_id,
       'release',
       'commission',
       pf.amount_commission,
       (SELECT commission_points FROM user_wallets WHERE user_id = pf.user_id),
       pf.feature_code,
       '[手动 sweep]释放 12h+ zombie freeze id=' || pf.id::text,
       'MSWEEP-' || pf.id::text,
       pf.brand_id,
       'balance_deduction',
       CURRENT_TIMESTAMP
FROM point_freezes pf
WHERE pf.status = 'released'
  AND pf.released_at >= CURRENT_TIMESTAMP - INTERVAL '5 minutes'
  AND pf.amount_commission > 0
  AND pf.reason LIKE '[ManualSweep 2026-05-22]%';

INSERT INTO point_transactions (user_id, type, point_type, amount, balance_after, feature_code,
                                description, order_id, brand_id, source, created_at)
SELECT pf.user_id,
       'release',
       'paid',
       pf.amount_paid,
       (SELECT paid_points FROM user_wallets WHERE user_id = pf.user_id),
       pf.feature_code,
       '[手动 sweep]释放 12h+ zombie freeze id=' || pf.id::text,
       'MSWEEP-' || pf.id::text,
       pf.brand_id,
       'balance_deduction',
       CURRENT_TIMESTAMP
FROM point_freezes pf
WHERE pf.status = 'released'
  AND pf.released_at >= CURRENT_TIMESTAMP - INTERVAL '5 minutes'
  AND pf.amount_paid > 0
  AND pf.reason LIKE '[ManualSweep 2026-05-22]%';

-- 5. 验证
\echo ''
\echo '=== release 后状态 ==='
SELECT id, user_id, feature_code, amount_total, status, released_at
FROM point_freezes
WHERE reason LIKE '[ManualSweep 2026-05-22]%'
ORDER BY id;

\echo ''
\echo '=== 受影响用户 release 后 frozen_points ==='
SELECT uw.user_id, uw.paid_points, uw.bonus_points, uw.commission_points, uw.frozen_points
FROM user_wallets uw
WHERE uw.user_id IN (25, 28, 40, 46);

\echo ''
\echo '=== CHECK 约束实证(必须全 t)==='
SELECT user_id,
       frozen_points >= 0 AS frozen_ok,
       paid_points >= 0 AS paid_ok,
       bonus_points >= 0 AS bonus_ok,
       commission_points >= 0 AS commission_ok
FROM user_wallets
WHERE user_id IN (25, 28, 40, 46);

-- ✅ 2026-05-22 老板已授权 COMMIT · Deploy-CTO 直跑(默认提交)
-- 回滚预案保留:若验证不对 · 用 pg_dump 出来的 pre_zombie_release_*.sql restore
-- 改回 dry-run: 把 COMMIT; 注释 · 解开 -- ROLLBACK;
COMMIT;
-- ROLLBACK;
