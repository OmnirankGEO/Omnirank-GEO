-- ============================================================================
-- 客户信用钱包 → 单账本合并 · 【只读盘点】 · 2026-07-27
--
-- 工单：docs/AI-CONTEXT/WALLET_SINGLE_LEDGER_WORKORDER_2026-07-27.md (a80b4a4e) §3 阶段①.1
-- 用途：迁移前后都可重跑。迁移后重跑用于对账（§5 验收第 3 条）。
--
-- 🔴 本脚本【只有 SELECT】，不含任何写操作，可安全在生产直接执行。
--    psql -f scripts/wallet_credit_merge_inventory_2026_07_27.sql
-- ============================================================================

\echo ''
\echo '════════ ① 逐客户钱包对照（信用三池 vs 平台钱包 vs 迁移后应有） ════════'

SELECT
    w.customer_user_id                                       AS uid,
    COALESCE(u.display_name, u.username, '-')                AS name,
    CASE WHEN u.phone IS NULL THEN '-'
         ELSE overlay(u.phone placing '****' from 4 for 4) END AS phone,
    w.agent_user_id                                          AS agent,
    -- 信用钱包三池
    w.tool_credit_points                                     AS c_tool,
    w.publish_credit_points                                  AS c_pub,
    w.bonus_credit_points                                    AS c_bonus,
    -- 平台钱包现状
    COALESCE(uw.paid_points, 0)                              AS w_paid,
    COALESCE(uw.bonus_points, 0)                             AS w_bonus,
    COALESCE(uw.commission_points, 0)                        AS w_comm,
    COALESCE(uw.frozen_points, 0)                            AS w_frozen,
    -- 迁移映射（工单 §2）：tool + publish → paid_points；bonus_credit → bonus_points
    COALESCE(uw.paid_points, 0) + w.tool_credit_points + w.publish_credit_points
                                                             AS after_paid,
    COALESCE(uw.bonus_points, 0) + w.bonus_credit_points     AS after_bonus,
    -- 迁移是 UPDATE 还是需要先建钱包行
    CASE WHEN uw.user_id IS NULL THEN '🔴 缺 user_wallets 行' ELSE 'ok' END AS wallet_row
FROM customer_agent_credit_wallets w
LEFT JOIN users        u  ON u.id      = w.customer_user_id
LEFT JOIN user_wallets uw ON uw.user_id = w.customer_user_id
ORDER BY w.customer_user_id;

\echo ''
\echo '════════ ② 自洽性校验：三池合计 是否 == 累计购入 − 累计消费 ════════'
\echo '   MISMATCH 表示信用钱包内部账目本身就不平，必须先查清再迁，不许带病迁移。'

SELECT
    w.customer_user_id                                                    AS uid,
    (w.tool_credit_points + w.publish_credit_points + w.bonus_credit_points) AS pools_total,
    (w.total_purchased_points - w.total_consumed_points)                  AS purchased_minus_consumed,
    CASE WHEN (w.tool_credit_points + w.publish_credit_points + w.bonus_credit_points)
            = (w.total_purchased_points - w.total_consumed_points)
         THEN 'OK' ELSE '🔴 MISMATCH' END                                 AS self_consistency
FROM customer_agent_credit_wallets w
ORDER BY w.customer_user_id;

\echo ''
\echo '════════ ③ 信用钱包全量流水（迁移后此表停写，历史保留只读） ════════'

SELECT
    customer_user_id                        AS uid,
    to_char(created_at, 'MM-DD HH24:MI')    AS ts,
    type,
    pool,
    points,
    COALESCE(feature_code, '-')             AS feature,
    source,
    COALESCE(substr(description, 1, 40), '-') AS descr
FROM customer_credit_transactions
ORDER BY customer_user_id, created_at, id;

\echo ''
\echo '════════ ④ 幂等预检：是否已经迁过 ════════'
\echo '   首次迁移应为 0 行。非 0 表示迁移脚本已跑过，重跑会被幂等键拦下（不会重复入账）。'

SELECT
    COALESCE(order_id, '(null)')  AS merge_key,
    point_type,
    amount,
    to_char(created_at, 'MM-DD HH24:MI') AS ts
FROM point_transactions
WHERE type = 'credit_ledger_merge'
ORDER BY order_id, point_type;

\echo ''
\echo '════════ ⑤ 切换前置检查：在途冻结必须为 0 ════════'
\echo '   任何一行非 0 → 停下，不要执行迁移（工单 §4.2）。'

SELECT 'customer_credit_freezes(status=frozen)' AS check_item,
       count(*)::text AS value
  FROM customer_credit_freezes WHERE status = 'frozen'
UNION ALL SELECT 'diagnosis_runs(freeze_backend=v35)',
       count(*)::text FROM diagnosis_runs WHERE freeze_backend = 'v35'
UNION ALL SELECT 'diagnosis_refund_records(freeze_backend=v35)',
       count(*)::text FROM diagnosis_refund_records WHERE freeze_backend = 'v35'
UNION ALL SELECT 'geo_plan_tasks(freeze_table=v35)',
       count(*)::text FROM geo_plan_tasks WHERE freeze_table = 'v35'
UNION ALL SELECT 'geo_research_selfserve_queue(freeze_table=v35)',
       count(*)::text FROM geo_research_selfserve_queue WHERE freeze_table = 'v35'
UNION ALL SELECT 'monitoring_keyword_settlements(freeze_table=v35)',
       count(*)::text FROM monitoring_keyword_settlements WHERE freeze_table = 'v35';

\echo ''
\echo '════════ ⑥ 总量（迁移前后对账用） ════════'

SELECT
    count(*)                                                  AS wallets,
    SUM(tool_credit_points)                                   AS sum_tool,
    SUM(publish_credit_points)                                AS sum_publish,
    SUM(bonus_credit_points)                                  AS sum_bonus,
    SUM(tool_credit_points + publish_credit_points + bonus_credit_points) AS sum_all
FROM customer_agent_credit_wallets;

\echo ''
\echo '（盘点结束 · 本脚本零写操作）'
