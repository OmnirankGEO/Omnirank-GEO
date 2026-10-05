-- ============================================================================
-- 单账本收敛 · 【切换当日复查】 · 2026-07-27
--
-- 用途：阶段②（拆 billing.py 分支）与阶段③（停写）上线【当天】跑这一份。
--       判定表 §0 结论 2 的数字是时点数据，不是结构性保证 —— 必须当日复查。
--
-- 🔴 本脚本【只有 SELECT】，可安全在生产随时执行。
--    psql -f scripts/wallet_credit_merge_switchday_check_2026_07_27.sql
--
-- 判读规则：下面每一项的 verdict 都必须是 PASS。
--          任何一项 BLOCK → 停下，不要继续切换。
-- ============================================================================

\echo ''
\echo '════════ 切换当日复查（全部必须 PASS） ════════'
\echo ''

-- ① 在途冻结必须为 0
--    真因说明（实证 2026-07-27）：customer_credit_freezes 历史仅 1 行
--    （uid=103 · geo_diagnosis · 650 · 06-18 11:25 冻结 → 11:28 committed），
--    早已正常结算完毕。冻结链是【实现完整的活代码】，不是没实现、也不是被禁用，
--    所以随时可能产生新的在途冻结 —— 这一项当日必查。
SELECT '① 在途 v35 冻结' AS check_item,
       count(*)::text     AS actual,
       '0'                AS expect,
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE '🔴 BLOCK' END AS verdict
  FROM customer_credit_freezes WHERE status = 'frozen'

-- ② 五张持久化表的 v35 标记必须为 0（有则说明有任务仍指向信用冻结表）
UNION ALL
SELECT '② diagnosis_runs.freeze_backend=v35', count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE '🔴 BLOCK' END
  FROM diagnosis_runs WHERE freeze_backend = 'v35'
UNION ALL
SELECT '② diagnosis_refund_records.freeze_backend=v35', count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE '🔴 BLOCK' END
  FROM diagnosis_refund_records WHERE freeze_backend = 'v35'
UNION ALL
SELECT '② geo_plan_tasks.freeze_table=v35', count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE '🔴 BLOCK' END
  FROM geo_plan_tasks WHERE freeze_table = 'v35'
UNION ALL
SELECT '② geo_research_selfserve_queue.freeze_table=v35', count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE '🔴 BLOCK' END
  FROM geo_research_selfserve_queue WHERE freeze_table = 'v35'
UNION ALL
SELECT '② monitoring_keyword_settlements.freeze_table=v35', count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE '🔴 BLOCK' END
  FROM monitoring_keyword_settlements WHERE freeze_table = 'v35'

-- ③ 迁移已完成：信用钱包三池应全部归零
UNION ALL
SELECT '③ 信用钱包三池残余',
       COALESCE(SUM(tool_credit_points + publish_credit_points + bonus_credit_points), 0)::text,
       '0',
       CASE WHEN COALESCE(SUM(tool_credit_points + publish_credit_points + bonus_credit_points), 0) = 0
            THEN 'PASS' ELSE '🔴 BLOCK（迁移未完成或又有新入账）' END
  FROM customer_agent_credit_wallets

-- ④ 钱包数没变多：变多说明 B 档入账源还没停干净
UNION ALL
SELECT '④ 信用钱包户数（迁移时为 5）', count(*)::text, '<= 5',
       CASE WHEN count(*) <= 5 THEN 'PASS' ELSE '🔴 BLOCK（有新客户被建了信用钱包）' END
  FROM customer_agent_credit_wallets

-- ⑤ 停写验证：阶段③上线后不应再有【新】的非迁移流水
UNION ALL
SELECT '⑤ 迁移后新增的非迁移信用流水',
       count(*)::text, '0',
       CASE WHEN count(*) = 0 THEN 'PASS' ELSE '🔴 BLOCK（B 档还有入账源在写）' END
  FROM customer_credit_transactions cct
 WHERE cct.source <> 'admin_adjust'
   -- 两侧都用无时区 timestamp 比较,避免隐式时区转换造成误报(见迁移脚本快照表注释)
   AND cct.created_at > COALESCE(
        (SELECT MIN(migrated_at)::timestamp FROM wallet_credit_merge_snapshot_20260727),
        TIMESTAMP '2099-01-01')

-- ⑥ 迁移幂等键完整性：每个被迁客户都应恰好有迁移流水
UNION ALL
SELECT '⑥ 迁移流水户数 == 快照户数',
       (SELECT count(DISTINCT user_id)::text FROM point_transactions WHERE type = 'credit_ledger_merge'),
       (SELECT count(*)::text FROM wallet_credit_merge_snapshot_20260727),
       CASE WHEN (SELECT count(DISTINCT user_id) FROM point_transactions WHERE type = 'credit_ledger_merge')
               = (SELECT count(*) FROM wallet_credit_merge_snapshot_20260727)
            THEN 'PASS' ELSE '🔴 BLOCK' END
;

\echo ''
\echo '════════ 参考信息（不作判定，供人工确认） ════════'

\echo ''
\echo '· 逐户当前余额：'
SELECT w.customer_user_id AS uid,
       w.tool_credit_points + w.publish_credit_points + w.bonus_credit_points AS credit_left,
       uw.paid_points, uw.bonus_points
  FROM customer_agent_credit_wallets w
  LEFT JOIN user_wallets uw ON uw.user_id = w.customer_user_id
 ORDER BY w.customer_user_id;

\echo ''
\echo '· 库存对账 cron 最近 5 次（拆除后若未同步改守恒等式，diff 会持续 >> 阈值 1）：'
SELECT to_char(run_at, 'MM-DD HH24:MI') AS run_at,
       diff_paid, diff_bonus, diff_publish, has_drift
  FROM inventory_audit_runs
 ORDER BY run_at DESC
 LIMIT 5;

\echo ''
\echo '（复查结束 · 本脚本零写操作）'
