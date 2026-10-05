-- ============================================================================
-- [v10 item5 · Deploy-CTO 2026-07-13 · v11 F6 返工] channel_revenue 补偿工单 exactly-once 唯一键
--   additive · 幂等 · 在 v9 fund_recovery kind 放宽迁移之后执行(kind 'record'/'reverse' 已放宽)。
-- 目的:
--   渠道收益 record/reverse 耐久补偿工单靠 insert_recovery_order_cursor(ON CONFLICT DO NOTHING)幂等,
--   但需一条【部分唯一索引】兜底 exactly-once:防并发/蓝绿双 scheduler 为【同一订单同一 kind】建重复未终结工单。
--   唯一键 = (source, ref_key, kind) WHERE source='channel_revenue' AND ref_key IS NOT NULL
--            AND status IN ('pending','processing','manual')。
--   ref_key=order_id;record 与 reverse 是不同 kind(同订单可各一条),故键含 kind。
-- 🔴 [v11 F6] 本迁移【绝不静默把未终结工单改成 resolved】:
--   建唯一键前【预检重复】· 有重复 → 立即 RAISE 中止(不动任何工单状态 → 本迁移完全可逆)。
--   若确需折叠既有重复,改跑 migration_v10b_dedup_channel_revenue_with_backup_2026_07_13.sql
--   (完整备份 + 可执行可验证的逐行恢复),去重后再回来执行本迁移。
--   说明:channel_revenue 补偿工单是 v9/v10 新增能力,生产从未部署 → 正常应为【零重复】,本迁移干净通过。
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v10_channel_revenue_exactly_once_2026_07_13.sql
-- 验证:
--   docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d fund_recovery_orders" | grep uniq_fund_recovery_channel_open
-- 说明:代码侧 db/fund_recovery_db.init_fund_recovery_tables 亦【预检·不静默折叠】自建(随启动)。
-- ============================================================================

BEGIN;

-- 锁/语句超时:避免长时间持锁阻塞在线业务(拿不到锁/超时即整事务回滚,可安全重试)
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 1. [v11 F6] 重复预检:存在重复未终结 channel_revenue 工单 → RAISE 中止(禁止静默折叠 · 不动工单状态)。
DO $$
DECLARE _dup INT;
BEGIN
  SELECT COUNT(*) INTO _dup FROM (
    SELECT 1 FROM fund_recovery_orders
     WHERE source='channel_revenue' AND ref_key IS NOT NULL
       AND status IN ('pending','processing','manual')
     GROUP BY source, ref_key, kind
    HAVING COUNT(*) > 1
  ) d;
  IF _dup > 0 THEN
    RAISE EXCEPTION
      'ABORT[v10 exactly-once]: 存在 % 组重复未终结 channel_revenue 工单 · 本迁移禁止静默折叠 · 请先跑 migration_v10b_dedup_channel_revenue_with_backup_2026_07_13.sql(备份+去重·含逐行恢复)后再执行本迁移',
      _dup;
  END IF;
END $$;

-- 2. 部分唯一索引(预检已保证无重复 → 直接可建 · IF NOT EXISTS 幂等 · 先 DROP 保证为当前版定义)
-- @drop-index-guard uniq_fund_recovery_channel_open ON fund_recovery_orders if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_fund_recovery_channel_open' AND i.indrelid = to_regclass('fund_recovery_orders')) THEN
        DROP INDEX IF EXISTS uniq_fund_recovery_channel_open;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_fund_recovery_channel_open' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] uniq_fund_recovery_channel_open 不在 fund_recovery_orders 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_fund_recovery_channel_open' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
-- @index-guard uniq_fund_recovery_channel_open ON fund_recovery_orders unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uniq_fund_recovery_channel_open' AND i.indrelid = to_regclass('public.fund_recovery_orders')) THEN
        NULL;  -- 已在 public.fund_recovery_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uniq_fund_recovery_channel_open' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uniq_fund_recovery_channel_open 已存在但不在 public.fund_recovery_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uniq_fund_recovery_channel_open' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uniq_fund_recovery_channel_open ON public.fund_recovery_orders (source, ref_key, kind) WHERE source = 'channel_revenue' AND ref_key IS NOT NULL AND status IN ('pending','processing','manual');
    END IF;
END $idxguard$;

INSERT INTO _migrations (name, applied_at)
VALUES ('v10_channel_revenue_exactly_once_2026_07_13', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ROLLBACK(还原 · 本迁移不改任何工单状态 → 完全可逆):
--   DROP INDEX IF EXISTS uniq_fund_recovery_channel_open;
--   DELETE FROM _migrations WHERE name='v10_channel_revenue_exactly_once_2026_07_13';
