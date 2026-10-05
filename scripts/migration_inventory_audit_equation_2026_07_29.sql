-- ============================================================
-- 库存对账等式返修 · 2026-07-29
-- 工单:合并「cron NameError 静默死亡」+「1.3 亿枚举式漏算」
--
-- 本迁移只动 inventory_audit_runs 这张【审计快照表】:
--   * 加失败态列 status / error_message  —— 让"对账程序自己死了"落得下来
--   * 加 agent_total_frozen              —— 新等式把冻结池计入(漏了立刻漂移)
--   * 加 wallet_total / ledger_total     —— 账实相符式的两个量,行自解释
--   * INTEGER → BIGINT                   —— 钱包/流水早已 bigint,快照列还是
--                                           int4;当前 1.3 亿离 2.147e9 只差
--                                           一个量级,溢出会让 INSERT 抛错 =
--                                           对账再次死掉
--
-- 🔴 additive-only:不改任何既有行的值,不动扣费/退费/退款口径,
--    不触碰 point_transactions / user_wallets / agent_inventory_* 本体。
-- 幂等:重复执行为 no-op(IF NOT EXISTS / 目标类型判断 / 约束存在判断)。
-- ============================================================

-- 1) 失败态与新等式列(全部 additive)
ALTER TABLE inventory_audit_runs ADD COLUMN IF NOT EXISTS status        TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE inventory_audit_runs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE inventory_audit_runs ADD COLUMN IF NOT EXISTS agent_total_frozen BIGINT NOT NULL DEFAULT 0;
ALTER TABLE inventory_audit_runs ADD COLUMN IF NOT EXISTS wallet_total  BIGINT;
ALTER TABLE inventory_audit_runs ADD COLUMN IF NOT EXISTS ledger_total  BIGINT;

-- 2) status 白名单(fail-closed:只认 ok/failed)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.inventory_audit_runs'::regclass
          AND conname = 'inventory_audit_runs_status_check'
    ) THEN
        ALTER TABLE inventory_audit_runs
            ADD CONSTRAINT inventory_audit_runs_status_check
            CHECK (status IN ('ok', 'failed'));
    END IF;
END $$;

-- 3) 快照数值列 int4 → int8(表只有几十行 · rewrite 代价可忽略)
DO $$
DECLARE c TEXT;
BEGIN
    FOREACH c IN ARRAY ARRAY[
        'agent_total_paid','agent_total_bonus',
        'customer_total_tool','customer_total_publish','customer_total_bonus',
        'platform_consumed','historical_purchased','historical_admin_adjust',
        'refunded_or_revoked','diff_paid','diff_bonus','diff_publish'
    ] LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='public' AND table_name='inventory_audit_runs'
              AND column_name=c AND data_type='integer'
        ) THEN
            EXECUTE format('ALTER TABLE inventory_audit_runs ALTER COLUMN %I TYPE BIGINT', c);
        END IF;
    END LOOP;
END $$;

-- 4) 失败态查询索引(admin 历史页 drift_only 会同时收 failed 行)
-- @index-guard idx_audit_runs_status ON inventory_audit_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_audit_runs_status' AND i.indrelid = to_regclass('public.inventory_audit_runs')) THEN
        NULL;  -- 已在 public.inventory_audit_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_audit_runs_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_audit_runs_status 已存在但不在 public.inventory_audit_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_audit_runs_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_audit_runs_status ON public.inventory_audit_runs (status, run_at DESC);
    END IF;
END $idxguard$;

-- 5) fail-closed 自验:列/约束/类型没到位就 RAISE,让 prestart 非零退出
DO $$
DECLARE v_cols INTEGER; v_chk INTEGER; v_int4 INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_cols FROM information_schema.columns
     WHERE table_schema='public' AND table_name='inventory_audit_runs'
       AND column_name IN ('status','error_message','agent_total_frozen',
                           'wallet_total','ledger_total');
    IF v_cols <> 5 THEN
        RAISE EXCEPTION 'inventory_audit_runs 失败态/等式列未建全 · 实际 %', v_cols;
    END IF;

    SELECT COUNT(*) INTO v_chk FROM pg_constraint
     WHERE conrelid='public.inventory_audit_runs'::regclass
       AND conname='inventory_audit_runs_status_check';
    IF v_chk <> 1 THEN
        RAISE EXCEPTION 'inventory_audit_runs.status CHECK 约束缺失';
    END IF;

    SELECT COUNT(*) INTO v_int4 FROM information_schema.columns
     WHERE table_schema='public' AND table_name='inventory_audit_runs'
       AND data_type='integer'
       AND column_name IN ('agent_total_paid','agent_total_bonus',
                           'customer_total_tool','customer_total_publish','customer_total_bonus',
                           'platform_consumed','historical_purchased','historical_admin_adjust',
                           'refunded_or_revoked','diff_paid','diff_bonus','diff_publish');
    IF v_int4 <> 0 THEN
        RAISE EXCEPTION 'inventory_audit_runs 仍有 % 个 int4 快照列未加宽', v_int4;
    END IF;

    RAISE NOTICE 'inventory_audit_runs 等式返修 migration verified OK';
END $$;
