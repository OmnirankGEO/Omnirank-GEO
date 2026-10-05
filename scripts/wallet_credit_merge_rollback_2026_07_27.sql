-- ============================================================================
-- 客户信用钱包 → 单账本合并 · 【回滚】 · 2026-07-27
--
-- 配套：wallet_credit_merge_migrate_2026_07_27.sql
-- 依赖：wallet_credit_merge_snapshot_20260727（迁移脚本写入的快照）
--
-- 🔴 仅在迁移出问题需要撤回时使用。用法与迁移一致，先 dry-run：
--     BEGIN;
--     \i scripts/wallet_credit_merge_rollback_2026_07_27.sql
--     ROLLBACK;   ← 确认输出无误后才改 COMMIT
--
-- 🔴 使用前必须确认：迁移之后【没有发生过新的扣费/充值】。
--    若已有新交易，机械回滚会把新交易一并抹掉 —— 这种情况请人工逐笔处理，不要跑本脚本。
--    脚本会做一次保护性检查并在检测到时 RAISE EXCEPTION 中止。
--
-- 恢复内容：
--   · user_wallets 扣回本次迁入的 paid / bonus
--   · customer_agent_credit_wallets 三池恢复为迁移前值
--   · 删除本次写入的 point_transactions(credit_ledger_merge) 与
--     customer_credit_transactions(related_order_id='credit_merge:<uid>') —— 这两批是本次
--     迁移【自己产生】的记录，不是原始流水；原有 24 行历史流水一行不动。
-- ============================================================================

-- 🔴 必须放在最前:psql 默认 autocommit,每条语句独立事务 —— 前面的 DO block RAISE EXCEPTION
--    【拦不住】后面的语句继续执行(本地验证实测:预检 ABORT 后回滚照跑,保护形同虚设)。
\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 阶段 0 · 全量预检（必须在任何写操作之前跑完）
--
-- 🔴 为什么单独一遍：本地验证时发现，把保护检查放在回滚循环内部，执行方会先看到
--    几行「[ROLLBACK] uid=... 扣回 ...」再看到 ABORT，很容易误判成"数据被改坏了一半"。
--    事实上 PL/pgSQL 的 EXCEPTION 会把整个 DO block 原子回滚，一行都没落库 ——
--    但在资金操作现场，让人误以为改坏一半是**危险的**（可能慌乱之下做出错误补救）。
--    所以：先全量预检，全部通过才进入回滚循环，输出不再有误导。
-- ---------------------------------------------------------------------------
DO $precheck$
DECLARE
    s       RECORD;
    v_newer INT;
    v_bad   INT := 0;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '════════ 阶段 0 · 回滚前置预检 ════════';
    FOR s IN
        SELECT * FROM wallet_credit_merge_snapshot_20260727 ORDER BY customer_user_id
    LOOP
        SELECT count(*) INTO v_newer
          FROM point_transactions
         WHERE user_id = s.customer_user_id
           AND created_at > s.migrated_at::timestamp   -- 类型对齐,防时区偏移误判
           AND type <> 'credit_ledger_merge';
        IF v_newer > 0 THEN
            RAISE WARNING '[BLOCK] uid=% 迁移后已产生 % 笔新流水', s.customer_user_id, v_newer;
            v_bad := v_bad + 1;
        END IF;
    END LOOP;

    IF v_bad > 0 THEN
        RAISE EXCEPTION
            '[ABORT] % 个账号在迁移后已有新流水，机械回滚会抹掉它们。本脚本未做任何修改。请人工逐笔处理。',
            v_bad;
    END IF;
    RAISE NOTICE '预检通过：无账号在迁移后产生新流水，可以安全回滚。';
END
$precheck$;

DO $rollback$
DECLARE
    s            RECORD;
    v_paid_back  BIGINT;
    v_bonus_back BIGINT;
    v_done       INT := 0;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '════════ 单账本合并 · 回滚 ════════';

    FOR s IN
        SELECT * FROM wallet_credit_merge_snapshot_20260727 ORDER BY customer_user_id
    LOOP
        -- 幂等：迁移流水已被删（说明回滚跑过）则跳过
        PERFORM 1 FROM point_transactions
         WHERE type = 'credit_ledger_merge' AND order_id = s.merge_key LIMIT 1;
        IF NOT FOUND THEN
            RAISE NOTICE '[SKIP] uid=% 已回滚过', s.customer_user_id;
            CONTINUE;
        END IF;

        v_paid_back  := s.before_tool + s.before_publish;
        v_bonus_back := s.before_bonus_credit;

        PERFORM 1 FROM user_wallets WHERE user_id = s.customer_user_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION '[ABORT] uid=% 缺 user_wallets 行，无法回滚', s.customer_user_id;
        END IF;

        -- ③ 平台钱包扣回
        UPDATE user_wallets
           SET paid_points  = paid_points  - v_paid_back,
               bonus_points = bonus_points - v_bonus_back,
               updated_at   = NOW()
         WHERE user_id = s.customer_user_id;

        -- ④ 信用钱包三池恢复
        UPDATE customer_agent_credit_wallets
           SET tool_credit_points    = s.before_tool,
               publish_credit_points = s.before_publish,
               bonus_credit_points   = s.before_bonus_credit,
               updated_at            = NOW()
         WHERE customer_user_id = s.customer_user_id;

        -- ⑤ 删除本次迁移【自己写入】的两批记录（不碰历史流水）
        DELETE FROM point_transactions
         WHERE type = 'credit_ledger_merge' AND order_id = s.merge_key;
        DELETE FROM customer_credit_transactions
         WHERE related_order_id = s.merge_key AND type = 'revoke' AND source = 'admin_adjust';

        v_done := v_done + 1;
        RAISE NOTICE '[ROLLBACK] uid=% | 扣回 paid -% bonus -% | 三池恢复 tool=% pub=% bonus=%',
            s.customer_user_id, v_paid_back, v_bonus_back,
            s.before_tool, s.before_publish, s.before_bonus_credit;
    END LOOP;

    RAISE NOTICE '';
    RAISE NOTICE '════════ 回滚完成 % 户 ════════', v_done;
    RAISE NOTICE '';
END
$rollback$;

\echo ''
\echo '════════ 回滚后核对：应与迁移前盘点完全一致 ════════'
SELECT w.customer_user_id AS uid,
       w.tool_credit_points, w.publish_credit_points, w.bonus_credit_points,
       uw.paid_points, uw.bonus_points
  FROM customer_agent_credit_wallets w
  LEFT JOIN user_wallets uw ON uw.user_id = w.customer_user_id
 ORDER BY w.customer_user_id;

\echo ''
\echo '（快照表 wallet_credit_merge_snapshot_20260727 保留不删，供审计）'
