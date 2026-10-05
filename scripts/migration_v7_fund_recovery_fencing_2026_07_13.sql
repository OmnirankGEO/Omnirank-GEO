-- ============================================================================
-- [v7 finding3 · Deploy-CTO NO-GO 返工 2026-07-13] fund_recovery_orders worker fencing
--   additive · 幂等 · 在 v6 fund_recovery 迁移之后执行(或独立执行 · 表由 v6 建)。
-- 修:
--   1. 加 claim_token 列(每次认领生成新 token · resolve/requeue 必须持同 token · 防迟到 worker 翻 resolved→pending)
--   2. 唯一键 WHERE 覆盖【所有未终结状态】(pending/processing/manual)· 防一单 processing 时再建重复工单致双退
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v7_fund_recovery_fencing_2026_07_13.sql
-- 验证:
--   docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d+ fund_recovery_orders"
-- 说明:代码侧 db/fund_recovery_db.init_fund_recovery_tables 亦幂等自建/自升级(随 init_wallet_tables 启动)。
-- ============================================================================

BEGIN;

-- 1. worker fencing token
ALTER TABLE fund_recovery_orders ADD COLUMN IF NOT EXISTS claim_token TEXT;

-- 1b. [v8 P1-1] ledger_type('legacy'|'v35')· 退款证据只查对应账本(两表 id 空间独立 · 防跨表同 ID 误判致漏退)
ALTER TABLE fund_recovery_orders ADD COLUMN IF NOT EXISTS ledger_type TEXT;

-- 2. 唯一键覆盖所有未终结状态(先 DROP · CREATE INDEX IF NOT EXISTS 对同名旧定义是 no-op)
-- @drop-index-guard uniq_fund_recovery_open ON fund_recovery_orders if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_fund_recovery_open' AND i.indrelid = to_regclass('fund_recovery_orders')) THEN
        DROP INDEX IF EXISTS uniq_fund_recovery_open;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_fund_recovery_open' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] uniq_fund_recovery_open 不在 fund_recovery_orders 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_fund_recovery_open' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
-- @index-guard uniq_fund_recovery_open ON fund_recovery_orders unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_fund_recovery_open' AND i.indrelid = to_regclass('public.fund_recovery_orders')) THEN
        NULL;  -- 已在 public.fund_recovery_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_fund_recovery_open' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uniq_fund_recovery_open 已存在但不在 public.fund_recovery_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_fund_recovery_open' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uniq_fund_recovery_open ON public.fund_recovery_orders (source, COALESCE(ref_key,''), kind, charge_tx_id) WHERE status IN ('pending','processing','manual') AND charge_tx_id IS NOT NULL;
    END IF;
END $idxguard$;

-- 2b. NULL charge_tx_id 工单【不去重】(保 v6 行为)· 若历史误建过 null-charge 唯一键则清除(见二轮对抗审:
--     ref_key=quote_id 非 per-charge 判别键 · 同 quote 两笔不同扣费会被误并致漏退)。
-- @drop-index-guard uniq_fund_recovery_open_nullcharge ON fund_recovery_orders if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_fund_recovery_open_nullcharge' AND i.indrelid = to_regclass('fund_recovery_orders')) THEN
        DROP INDEX IF EXISTS uniq_fund_recovery_open_nullcharge;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_fund_recovery_open_nullcharge' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] uniq_fund_recovery_open_nullcharge 不在 fund_recovery_orders 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_fund_recovery_open_nullcharge' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;

-- 2c. [v8 对抗审 P2] geo_plan_settle 冲突工单 exactly-once(charge_tx_id 恒 NULL · ref_key=geoplan_{tid} 是任务唯一身份):
--     防 inline vs 巡检 / 蓝绿双 scheduler TOCTOU 建重复未终结工单致另一条永久 un-closeable。
-- 🔴 [v8 二轮对抗审 P1] 建唯一键【前必须 pre-dedup 既有重复】· 否则 prod 已有 TOCTOU 重复行时 CREATE UNIQUE INDEX 报
--     duplicate key 中断迁移。折叠每 (source,ref_key) 只留最新一条,其余 resolved:
UPDATE fund_recovery_orders f
   SET status='resolved', resolved_at=NOW(), updated_at=NOW(),
       last_error = COALESCE(last_error,'') || ' · [v8 pre-dedup] 折叠重复 geo_plan_settle 工单(保最新一条)'
  FROM (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY source, ref_key ORDER BY id DESC) AS rn
      FROM fund_recovery_orders
     WHERE source='geo_plan_settle' AND ref_key IS NOT NULL AND status IN ('pending','processing','manual')
  ) r
 WHERE f.id = r.id AND r.rn > 1;

-- @drop-index-guard uniq_fund_recovery_geoplan_open ON fund_recovery_orders if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_fund_recovery_geoplan_open' AND i.indrelid = to_regclass('fund_recovery_orders')) THEN
        DROP INDEX IF EXISTS uniq_fund_recovery_geoplan_open;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_fund_recovery_geoplan_open' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] uniq_fund_recovery_geoplan_open 不在 fund_recovery_orders 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_fund_recovery_geoplan_open' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
-- @index-guard uniq_fund_recovery_geoplan_open ON fund_recovery_orders unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_fund_recovery_geoplan_open' AND i.indrelid = to_regclass('public.fund_recovery_orders')) THEN
        NULL;  -- 已在 public.fund_recovery_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_fund_recovery_geoplan_open' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uniq_fund_recovery_geoplan_open 已存在但不在 public.fund_recovery_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_fund_recovery_geoplan_open' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uniq_fund_recovery_geoplan_open ON public.fund_recovery_orders (source, ref_key) WHERE source = 'geo_plan_settle' AND ref_key IS NOT NULL AND status IN ('pending','processing','manual');
    END IF;
END $idxguard$;

INSERT INTO _migrations (name, applied_at)
VALUES ('v7_fund_recovery_fencing_2026_07_13', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ROLLBACK(还原 v6 口径):
--   DROP INDEX IF EXISTS uniq_fund_recovery_open;
--   CREATE UNIQUE INDEX uniq_fund_recovery_open
--     ON fund_recovery_orders(source, COALESCE(ref_key,''), kind, charge_tx_id)
--     WHERE status = 'pending' AND charge_tx_id IS NOT NULL;
--   ALTER TABLE fund_recovery_orders DROP COLUMN IF EXISTS claim_token;
--   DELETE FROM _migrations WHERE name='v7_fund_recovery_fencing_2026_07_13';
