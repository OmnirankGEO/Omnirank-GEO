-- ============================================================================
-- 客户信用钱包 → 单账本合并 · 【迁移】 · 2026-07-27
--
-- 工单：docs/AI-CONTEXT/WALLET_SINGLE_LEDGER_WORKORDER_2026-07-27.md (a80b4a4e) §3 阶段①.2-4
-- 依据：Owner 2026-07-27「不能有两本账，用户只有充值算力和赠送算力」
-- 映射（工单 §2）：tool_credit + publish_credit → paid_points；bonus_credit → bonus_points
--
-- ┌──────────────────────────────────────────────────────────────────────────┐
-- │ 🔴 dry-run 用法（工单要求先跑这个，把 NOTICE 输出贴回给 Owner/Review）      │
-- │                                                                          │
-- │   BEGIN;                                                                 │
-- │   \i scripts/wallet_credit_merge_migrate_2026_07_27.sql                  │
-- │   ROLLBACK;          ← 必须 ROLLBACK，本步不得留下任何写入                  │
-- │                                                                          │
-- │ 正式执行：把上面的 ROLLBACK 换成 COMMIT（且需 Owner 已定 §USER149 开关）     │
-- └──────────────────────────────────────────────────────────────────────────┘
--
-- 幂等：以 point_transactions.order_id = 'credit_merge:<uid>' 为键。
--       重跑会逐个 SKIP，不重复入账（工单 §5 验收第 5 条要求实跑第二次证明）。
--
-- 红线遵循（工单 §4.3）：
--   · 不 DROP / 不 TRUNCATE / 无 WHERE 的 UPDATE 一律没有
--   · 不删任何原始流水：customer_credit_transactions 24 行原样保留
--   · 迁移前快照写入 wallet_credit_merge_snapshot_20260727（回滚脚本依赖它）
--   · 找不到钱包行 → RAISE EXCEPTION 显式失败，绝不静默当 0
-- ============================================================================

-- 🔴 必须放在最前:psql 默认 autocommit,每条语句独立事务 —— 前面的 DO block RAISE EXCEPTION
--    【拦不住】后面的语句继续执行(本地验证实测:预检 ABORT 后回滚照跑,保护形同虚设)。
\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 快照表（回滚依赖）。只新增表，不动任何现有表结构。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wallet_credit_merge_snapshot_20260727 (
    id                    BIGSERIAL PRIMARY KEY,
    customer_user_id      INTEGER     NOT NULL,
    -- 迁移前 · 信用钱包三池
    before_tool           BIGINT      NOT NULL,
    before_publish        BIGINT      NOT NULL,
    before_bonus_credit   BIGINT      NOT NULL,
    -- 迁移前 · 平台钱包
    before_paid           BIGINT      NOT NULL,
    before_bonus          BIGINT      NOT NULL,
    -- 迁移后 · 平台钱包
    after_paid            BIGINT      NOT NULL,
    after_bonus           BIGINT      NOT NULL,
    merge_key             TEXT        NOT NULL,
    -- 🔴 用 TIMESTAMP(无时区)而非 TIMESTAMPTZ:point_transactions.created_at 与
    --    customer_credit_transactions.created_at 都是 timestamp without time zone。
    --    跨类型比较会按 session timezone 隐式转换,带来最多 ±小时级偏移 ——
    --    本地验证时正是这个偏移让「迁移后新增流水」检查误报 BLOCK。
    migrated_at           TIMESTAMP   NOT NULL DEFAULT LOCALTIMESTAMP,
    UNIQUE (customer_user_id, merge_key)
);

DO $migrate$
DECLARE
    r                RECORD;
    v_merge_paid     BIGINT;
    v_merge_bonus    BIGINT;
    v_new_paid       BIGINT;
    v_new_bonus      BIGINT;
    v_key            TEXT;
    v_done           INT := 0;
    v_skipped        INT := 0;
    v_total_paid     BIGINT := 0;
    v_total_bonus    BIGINT := 0;
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '════════ 客户信用钱包 → 单账本合并 ════════';

    FOR r IN
        SELECT w.customer_user_id,
               w.tool_credit_points,
               w.publish_credit_points,
               w.bonus_credit_points
          FROM customer_agent_credit_wallets w
         ORDER BY w.customer_user_id
           FOR UPDATE
    LOOP
        v_key := 'credit_merge:' || r.customer_user_id;

        -- ① 幂等：已迁过就跳过（重跑安全）
        PERFORM 1 FROM point_transactions
         WHERE type = 'credit_ledger_merge' AND order_id = v_key
         LIMIT 1;
        IF FOUND THEN
            RAISE NOTICE '[SKIP] uid=% 已迁过（幂等键 %）', r.customer_user_id, v_key;
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;

        v_merge_paid  := r.tool_credit_points + r.publish_credit_points;
        v_merge_bonus := r.bonus_credit_points;

        IF v_merge_paid = 0 AND v_merge_bonus = 0 THEN
            RAISE NOTICE '[SKIP] uid=% 三池均为 0，无需迁移', r.customer_user_id;
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;

        -- ② 锁平台钱包行（与信用钱包行同事务持锁，防并发扣费穿插）
        PERFORM 1 FROM user_wallets WHERE user_id = r.customer_user_id FOR UPDATE;
        IF NOT FOUND THEN
            -- 🔴 显式失败，不静默建行也不当 0（工单 §4.3：禁止静默回落）
            RAISE EXCEPTION
                '[ABORT] uid=% 没有 user_wallets 行，拒绝静默处理。请先人工确认该账号状态。',
                r.customer_user_id;
        END IF;

        -- ③ 迁入平台钱包
        UPDATE user_wallets
           SET paid_points  = paid_points  + v_merge_paid,
               bonus_points = bonus_points + v_merge_bonus,
               updated_at   = NOW()
         WHERE user_id = r.customer_user_id
        RETURNING paid_points, bonus_points INTO v_new_paid, v_new_bonus;

        -- ④ 快照（回滚脚本依赖）
        INSERT INTO wallet_credit_merge_snapshot_20260727
            (customer_user_id, before_tool, before_publish, before_bonus_credit,
             before_paid, before_bonus, after_paid, after_bonus, merge_key)
        VALUES
            (r.customer_user_id, r.tool_credit_points, r.publish_credit_points,
             r.bonus_credit_points, v_new_paid - v_merge_paid, v_new_bonus - v_merge_bonus,
             v_new_paid, v_new_bonus, v_key);

        -- ⑤ 平台侧流水（order_id 作幂等键；不写 source 列 —— 该列在部分环境不存在）
        IF v_merge_paid > 0 THEN
            INSERT INTO point_transactions
                (user_id, type, point_type, amount, balance_after, description, order_id)
            VALUES
                (r.customer_user_id, 'credit_ledger_merge', 'paid', v_merge_paid, v_new_paid,
                 '账户合并 · 原服务商额度 ' || v_merge_paid || ' 算力并入充值算力（可用于全部功能）',
                 v_key);
        END IF;
        IF v_merge_bonus > 0 THEN
            INSERT INTO point_transactions
                (user_id, type, point_type, amount, balance_after, description, order_id)
            VALUES
                (r.customer_user_id, 'credit_ledger_merge', 'bonus', v_merge_bonus, v_new_bonus,
                 '账户合并 · 原服务商赠送额度 ' || v_merge_bonus || ' 算力并入赠送算力',
                 v_key);
        END IF;

        -- ⑥ 信用侧留痕后清零。
        --    type='revoke' + source='admin_adjust' 均在既有 CHECK 白名单内，无需改 schema。
        --    不清零会导致展示层双算、库存守恒式重复计入（见影响判定表 A1/E 档）。
        IF r.tool_credit_points > 0 THEN
            INSERT INTO customer_credit_transactions
                (customer_user_id, agent_user_id, type, pool, points,
                 balance_tool_after, balance_publish_after, balance_bonus_after,
                 related_order_id, source, description)
            SELECT r.customer_user_id, w.agent_user_id, 'revoke', 'tool', -r.tool_credit_points,
                   0, 0, 0, v_key, 'admin_adjust',
                   '账户合并 · 额度迁往平台钱包（单账本收敛）'
              FROM customer_agent_credit_wallets w
             WHERE w.customer_user_id = r.customer_user_id;
        END IF;
        IF r.publish_credit_points > 0 THEN
            INSERT INTO customer_credit_transactions
                (customer_user_id, agent_user_id, type, pool, points,
                 balance_tool_after, balance_publish_after, balance_bonus_after,
                 related_order_id, source, description)
            SELECT r.customer_user_id, w.agent_user_id, 'revoke', 'publish', -r.publish_credit_points,
                   0, 0, 0, v_key, 'admin_adjust',
                   '账户合并 · 额度迁往平台钱包（单账本收敛）'
              FROM customer_agent_credit_wallets w
             WHERE w.customer_user_id = r.customer_user_id;
        END IF;
        IF r.bonus_credit_points > 0 THEN
            INSERT INTO customer_credit_transactions
                (customer_user_id, agent_user_id, type, pool, points,
                 balance_tool_after, balance_publish_after, balance_bonus_after,
                 related_order_id, source, description)
            SELECT r.customer_user_id, w.agent_user_id, 'revoke', 'bonus', -r.bonus_credit_points,
                   0, 0, 0, v_key, 'admin_adjust',
                   '账户合并 · 赠送额度迁往平台钱包（单账本收敛）'
              FROM customer_agent_credit_wallets w
             WHERE w.customer_user_id = r.customer_user_id;
        END IF;

        UPDATE customer_agent_credit_wallets
           SET tool_credit_points    = 0,
               publish_credit_points = 0,
               bonus_credit_points   = 0,
               updated_at            = NOW()
         WHERE customer_user_id = r.customer_user_id;

        v_done        := v_done + 1;
        v_total_paid  := v_total_paid  + v_merge_paid;
        v_total_bonus := v_total_bonus + v_merge_bonus;

        RAISE NOTICE '[MERGE] uid=% | 信用(tool % + pub % + bonus %) → 平台(paid % / bonus %) | 迁入 paid +% bonus +%',
            r.customer_user_id, r.tool_credit_points, r.publish_credit_points, r.bonus_credit_points,
            v_new_paid, v_new_bonus, v_merge_paid, v_merge_bonus;
    END LOOP;

    RAISE NOTICE '';
    RAISE NOTICE '════════ 合计：迁移 % 户 / 跳过 % 户 | paid +% · bonus +% ════════',
        v_done, v_skipped, v_total_paid, v_total_bonus;
    RAISE NOTICE '';
END
$migrate$;

-- ---------------------------------------------------------------------------
-- 迁移后即时对账（dry-run 时这几行也会跑，看到的是事务内的值）
-- ---------------------------------------------------------------------------
\echo ''
\echo '════════ 对账 A：信用钱包应全部归零 ════════'
SELECT customer_user_id AS uid, tool_credit_points, publish_credit_points, bonus_credit_points
  FROM customer_agent_credit_wallets
 ORDER BY customer_user_id;

\echo ''
\echo '════════ 对账 B：逐客户 迁移前信用+平台 == 迁移后平台 ════════'
SELECT s.customer_user_id                                   AS uid,
       (s.before_tool + s.before_publish + s.before_paid)    AS expect_paid,
       s.after_paid,
       (s.before_bonus_credit + s.before_bonus)              AS expect_bonus,
       s.after_bonus,
       CASE WHEN (s.before_tool + s.before_publish + s.before_paid) = s.after_paid
             AND (s.before_bonus_credit + s.before_bonus)    = s.after_bonus
            THEN 'OK' ELSE '🔴 DIFF' END                     AS verdict
  FROM wallet_credit_merge_snapshot_20260727 s
 ORDER BY s.customer_user_id;

\echo ''
\echo '════════ 对账 C：本次写入的平台流水 ════════'
SELECT user_id AS uid, point_type, amount, balance_after, order_id
  FROM point_transactions
 WHERE type = 'credit_ledger_merge'
 ORDER BY user_id, point_type;

\echo ''
\echo '════════ 对账 D：余额构成分解（O-1=B 硬条件：商誉补偿必须与合并笔可区分） ════════'
\echo '   Owner 2026-07-27 决定 O-1=B：user 149 的 14,091 商誉补偿【不回收】，'
\echo '   但必须能和本次合并笔分开，否则日后对账看到账差却查不到原因。'
\echo '   本查询按 type 拆开每个被迁客户的全部流水构成 —— 任何时候重跑都能看到分解。'
\echo '   · credit_ledger_merge = 本次账本合并迁入（本脚本写的）'
\echo '   · admin_adjust        = 管理员手工调整（含那笔 14,091 商誉补偿，非本次产生）'
\echo '   · 其余                = 正常业务流水'

SELECT pt.user_id                        AS uid,
       pt.type,
       count(*)                          AS n,
       SUM(pt.amount)                    AS points,
       CASE pt.type
            WHEN 'credit_ledger_merge' THEN '← 本次合并迁入'
            WHEN 'admin_adjust'        THEN '← 管理员调整（商誉补偿在此，不属本次合并）'
            ELSE '' END                  AS note
  FROM point_transactions pt
 WHERE pt.user_id IN (SELECT customer_user_id FROM wallet_credit_merge_snapshot_20260727)
 GROUP BY pt.user_id, pt.type
 ORDER BY pt.user_id, pt.type;

\echo ''
\echo '🔴 提请 Owner 注意（我没有权限也不擅自改历史流水）：'
\echo '   那笔 14,091（point_transactions id=2155, 2026-07-27 12:10）的 description 现为'
\echo '   「最高管理员算力校正：核对支付流水后修正历史到账差异」。'
\echo '   这与 O-1 的真实口径【商誉补偿·不回收】不一致 —— 它读起来像"补一笔漏到账的钱"，'
\echo '   而实际是安抚性补偿。半年后对账的人正是会被这句话误导。'
\echo '   建议由 admin 走正规工单补一条更正说明（本脚本不动历史流水）。'

\echo ''
\echo '⚠️ dry-run 请确认以上四段全部 OK 后再 ROLLBACK；正式执行才 COMMIT。'
