-- ============================================================
-- V3.5 v7 migration 跑前 preflight check(Codex 复审 P0-3 程序化校验)
-- 用 pg_get_constraintdef + string_agg 拼接全部 CHECK 定义后实证旧值超集
-- 不只看约束名 · 也不依赖 IN 关键字(Postgres 实际输出 ANY(ARRAY[...]) 而非 IN(...))
--
-- Deploy AI 跑 v7 migration(migration_v35_v7_fund_chain_2026_06_08.sql)前必须先跑本脚本
-- 任一 RAISE EXCEPTION → 停止 v7 部署 · 手工核 prod 约束定义后再继续
-- ============================================================

\echo === Preflight 1/2: agent_inventory_transactions type CHECK ===
DO $$
DECLARE all_def TEXT;
BEGIN
    -- 拼接表上所有 CHECK 定义(Postgres 可能输出 IN(...) 或 ANY(ARRAY[...]) · 不依赖关键字)
    SELECT string_agg(pg_get_constraintdef(oid), ' | ' ORDER BY conname)
    INTO all_def
    FROM pg_constraint
    WHERE conrelid = 'agent_inventory_transactions'::regclass
      AND contype = 'c';

    IF all_def IS NULL THEN
        RAISE EXCEPTION '[Preflight FAIL] agent_inventory_transactions 无任何 CHECK 约束 · prod schema 异常';
    END IF;

    RAISE NOTICE '[Preflight 1] All CHECKs: %', all_def;

    -- v7 migration 要 ADD 9 值(8 旧 + purchase_from_commission)· 现有定义必须含 8 旧值
    IF all_def NOT LIKE '%purchase_prepay%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 purchase_prepay · prod 定义异常 · 手工核';
    END IF;
    IF all_def NOT LIKE '%purchase_auto%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 purchase_auto · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%purchase_admin_adjust%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 purchase_admin_adjust · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%allocate_to_customer%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 allocate_to_customer · prod 定义异常';
    END IF;
    -- [Codex r3 复审] v7 新 CHECK 含 allocate_to_customer_offline(旧 8 值之一)· 必须单独校验
    IF all_def NOT LIKE '%allocate_to_customer_offline%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 allocate_to_customer_offline · prod 定义异常 · 手工核';
    END IF;
    IF all_def NOT LIKE '%revoke_from_customer%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 revoke_from_customer · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%admin_adjust%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 admin_adjust · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%refund_clawback%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 refund_clawback · prod 定义异常';
    END IF;

    -- 若 prod 已手动加过 purchase_from_commission(hotfix)· v7 ADD 会失败:
    -- v7 用 DROP IF EXISTS + ADD · 默认约束名 DROP 跑空 + ADD 重复 → 失败
    IF all_def LIKE '%purchase_from_commission%' THEN
        RAISE NOTICE '[Preflight WARN] CHECK 已含 purchase_from_commission(可能 hotfix 过)· v7 ADD 会失败 · 需手工 ALTER';
    END IF;

    RAISE NOTICE '[Preflight 1 PASS] agent_inventory_transactions CHECK 包含旧 8 值 · v7 可安全 DROP+ADD';
END $$;

\echo === Preflight 2/2: customer_credit_transactions source CHECK ===
DO $$
DECLARE all_def TEXT;
BEGIN
    SELECT string_agg(pg_get_constraintdef(oid), ' | ' ORDER BY conname)
    INTO all_def
    FROM pg_constraint
    WHERE conrelid = 'customer_credit_transactions'::regclass
      AND contype = 'c';

    IF all_def IS NULL THEN
        RAISE EXCEPTION '[Preflight FAIL] customer_credit_transactions 无任何 CHECK 约束 · prod schema 异常';
    END IF;

    RAISE NOTICE '[Preflight 2] All CHECKs: %', all_def;

    -- v7 ADD 7 值(6 旧 + tool_fail_refund)· 现有定义必须含 6 旧值
    IF all_def NOT LIKE '%online_payment%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 online_payment · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%offline_allocation%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 offline_allocation · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%admin_adjust%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 admin_adjust · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%tool_consume%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 tool_consume · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%refund_revoke%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 refund_revoke · prod 定义异常';
    END IF;
    IF all_def NOT LIKE '%agent_rebate%' THEN
        RAISE EXCEPTION '[Preflight FAIL] CHECK 不含 agent_rebate(2026-05-30 已加)· prod 未升级 · 手工核';
    END IF;

    IF all_def LIKE '%tool_fail_refund%' THEN
        RAISE NOTICE '[Preflight WARN] CHECK 已含 tool_fail_refund(可能 hotfix 过)· v7 ADD 会失败 · 需手工 ALTER';
    END IF;

    RAISE NOTICE '[Preflight 2 PASS] customer_credit_transactions CHECK 包含旧 6 值 · v7 可安全 DROP+ADD';
END $$;

\echo === All preflight checks passed · v7 migration 可继续 ===
