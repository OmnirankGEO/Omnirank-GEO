-- ============================================================================
-- [v6 req3 · Deploy-CTO 2026-07-13] fund_recovery_orders 正式 migration
--   系统内部资金操作【耐久补偿工单】· 正式建表 + 处理器所需列(claim/backoff/终态)+ 含 kind 的唯一键。
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v6_fund_recovery_2026_07_13.sql
-- 验证:
--   docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d+ fund_recovery_orders"
-- 回滚:见文件末(表可保留 · 无害;仅在确认无 pending/processing 时可 DROP)。
-- 说明:代码侧 db/fund_recovery_db.init_fund_recovery_tables 亦幂等自建/自升级(随 init_wallet_tables 启动)。
--       本 migration 是【正式部署脚本】· 与代码自建同构 · 供 Deploy 显式先行执行。
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS fund_recovery_orders (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,
    ref_key       TEXT,
    user_id       INTEGER,
    feature_code  TEXT,
    charge_tx_id  BIGINT,
    amount_points INTEGER,
    kind          TEXT NOT NULL DEFAULT 'refund'
                  CHECK (kind IN ('refund','release','commit','state_fix')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','processing','resolved','manual','failed')),
    retry_count   SMALLINT NOT NULL DEFAULT 0,
    reason        TEXT,
    last_error    TEXT,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at    TIMESTAMPTZ,
    worker_id     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ
);

-- 旧表(v5 已建的 3 态版)幂等升级:补列 + 放宽 status CHECK
ALTER TABLE fund_recovery_orders ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE fund_recovery_orders ADD COLUMN IF NOT EXISTS claimed_at    TIMESTAMPTZ;
ALTER TABLE fund_recovery_orders ADD COLUMN IF NOT EXISTS worker_id     TEXT;
ALTER TABLE fund_recovery_orders DROP CONSTRAINT IF EXISTS fund_recovery_orders_status_check;
ALTER TABLE fund_recovery_orders ADD CONSTRAINT fund_recovery_orders_status_check
  CHECK (status IN ('pending','processing','resolved','manual','failed'));

-- 认领扫描索引(pending 且到重试点)
-- @drop-index-guard idx_fund_recovery_status ON fund_recovery_orders if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_fund_recovery_status' AND i.indrelid = to_regclass('fund_recovery_orders')) THEN
        DROP INDEX IF EXISTS idx_fund_recovery_status;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_fund_recovery_status' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] idx_fund_recovery_status 不在 fund_recovery_orders 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_fund_recovery_status' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
-- @index-guard idx_fund_recovery_claim ON fund_recovery_orders plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_fund_recovery_claim' AND i.indrelid = to_regclass('public.fund_recovery_orders')) THEN
        NULL;  -- 已在 public.fund_recovery_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_fund_recovery_claim' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_fund_recovery_claim 已存在但不在 public.fund_recovery_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_fund_recovery_claim' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_fund_recovery_claim ON public.fund_recovery_orders (next_retry_at) WHERE status = 'pending';
    END IF;
END $idxguard$;

-- 唯一键【含 kind】· 仅 charge_tx_id 非空时去重(同 charge 的 refund 与 state_fix 是不同工单,绝不互吞)
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
        CREATE UNIQUE INDEX uniq_fund_recovery_open ON public.fund_recovery_orders (source, COALESCE(ref_key,''), kind, charge_tx_id) WHERE status = 'pending' AND charge_tx_id IS NOT NULL;
    END IF;
END $idxguard$;

INSERT INTO _migrations (name, applied_at)
VALUES ('v6_fund_recovery_2026_07_13', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ROLLBACK(仅在确认无 pending/processing 工单时):
--   DROP TABLE IF EXISTS fund_recovery_orders;
--   DELETE FROM _migrations WHERE name='v6_fund_recovery_2026_07_13';
