-- ============================================================================
-- [v5 req1 · Deploy-CTO 2026-07-13] geo_plan_tasks 耐久结算态 + 补偿队列列
--   running→settling→done(commit 确认后才 done)· settlement_pending / refund_pending 补偿队列。
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v5_geo_plan_settlement_2026_07_13.sql
-- 验证:
--   docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d+ geo_plan_tasks"
-- 回滚(仅在无 settling/*_pending 存量时安全):
--   见文件末 ROLLBACK 注释(先把 settling→failed / settlement_pending→done / refund_pending→failed 收口再回退约束)。
-- ============================================================================

BEGIN;

-- 1. 加宽 status 列:'settlement_pending' = 18 字符 > VARCHAR(16) → 必须先加宽,否则 CHECK/写入失败
ALTER TABLE geo_plan_tasks ALTER COLUMN status TYPE VARCHAR(24);

-- 2. 替换 status CHECK 约束(旧 6 态 → 新 9 态)· 匿名内联约束 PG 自动名 geo_plan_tasks_status_check
ALTER TABLE geo_plan_tasks DROP CONSTRAINT IF EXISTS geo_plan_tasks_status_check;
ALTER TABLE geo_plan_tasks ADD CONSTRAINT geo_plan_tasks_status_check
  CHECK (status IN ('queued','running','settling','settlement_pending','refund_pending','settle_conflict',
                    'done','failed','cancelled','timeout'));

-- 3. 补偿队列辅助列
ALTER TABLE geo_plan_tasks ADD COLUMN IF NOT EXISTS pending_terminal    VARCHAR(16);   -- refund_pending 退成功后应落的终态
ALTER TABLE geo_plan_tasks ADD COLUMN IF NOT EXISTS settle_retry_count  SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE geo_plan_tasks ADD COLUMN IF NOT EXISTS settle_last_error   TEXT;

-- 4. 索引:去重/并发口径扩到全非终态(brand-status 部分索引重建)+ 补偿队列扫描索引
-- @drop-index-guard idx_geoplan_brand_status ON geo_plan_tasks if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geoplan_brand_status' AND i.indrelid = to_regclass('geo_plan_tasks')) THEN
        DROP INDEX IF EXISTS idx_geoplan_brand_status;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geoplan_brand_status' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] idx_geoplan_brand_status 不在 geo_plan_tasks 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geoplan_brand_status' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;
-- @index-guard idx_geoplan_brand_status ON geo_plan_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geoplan_brand_status' AND i.indrelid = to_regclass('public.geo_plan_tasks')) THEN
        NULL;  -- 已在 public.geo_plan_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geoplan_brand_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geoplan_brand_status 已存在但不在 public.geo_plan_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geoplan_brand_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geoplan_brand_status ON public.geo_plan_tasks (brand_id, status) WHERE status IN ('queued','running','settling','settlement_pending','refund_pending');
    END IF;
END $idxguard$;

-- @index-guard idx_geoplan_settle_pending ON geo_plan_tasks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_geoplan_settle_pending' AND i.indrelid = to_regclass('public.geo_plan_tasks')) THEN
        NULL;  -- 已在 public.geo_plan_tasks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_geoplan_settle_pending' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_geoplan_settle_pending 已存在但不在 public.geo_plan_tasks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_geoplan_settle_pending' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_geoplan_settle_pending ON public.geo_plan_tasks (id) WHERE status IN ('settlement_pending','refund_pending') AND archived_at IS NULL;
    END IF;
END $idxguard$;

INSERT INTO _migrations (name, applied_at)
VALUES ('v5_geo_plan_settlement_2026_07_13', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ============================================================================
-- ROLLBACK(仅在【无 settling/settlement_pending/refund_pending 存量行】时执行 · 否则先跑 reconcile 收口):
--   BEGIN;
--   -- 收口存量(若有):settling→failed / settlement_pending→done / refund_pending→failed 后再回退
--   ALTER TABLE geo_plan_tasks DROP CONSTRAINT IF EXISTS geo_plan_tasks_status_check;
--   ALTER TABLE geo_plan_tasks ADD CONSTRAINT geo_plan_tasks_status_check
--     CHECK (status IN ('queued','running','done','failed','cancelled','timeout'));
--   ALTER TABLE geo_plan_tasks ALTER COLUMN status TYPE VARCHAR(16);
--   DROP INDEX IF EXISTS idx_geoplan_settle_pending;
--   DELETE FROM _migrations WHERE name='v5_geo_plan_settlement_2026_07_13';
--   COMMIT;
--   (pending_terminal / settle_retry_count / settle_last_error 列可保留 · 无害)
-- ============================================================================
