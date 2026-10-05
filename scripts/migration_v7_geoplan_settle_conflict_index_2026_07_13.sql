-- ============================================================================
-- [v7 finding4 · Deploy-CTO NO-GO 返工 2026-07-13] settle_conflict 纳入品牌去重索引
--   additive · 幂等 · 在 v5 geo_plan_settlement 迁移之后执行。
-- 修:idx_geoplan_brand_status 的 WHERE 补 'settle_conflict' —— 与 ACTIVE_STATUSES(去重/并发口径)一致,
--    否则 settle_conflict 任务在途时同品牌去重/并发查询走不到部分索引(且口径漂移)。
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v7_geoplan_settle_conflict_index_2026_07_13.sql
-- 验证:
--   docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d geo_plan_tasks" | grep idx_geoplan_brand_status
-- ============================================================================

BEGIN;

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
        CREATE INDEX idx_geoplan_brand_status ON public.geo_plan_tasks (brand_id, status) WHERE status IN ('queued','running','settling','settlement_pending','refund_pending','settle_conflict');
    END IF;
END $idxguard$;

INSERT INTO _migrations (name, applied_at)
VALUES ('v7_geoplan_settle_conflict_index_2026_07_13', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ROLLBACK(还原 v5 口径 · 去 settle_conflict):
--   DROP INDEX IF EXISTS idx_geoplan_brand_status;
--   CREATE INDEX idx_geoplan_brand_status ON geo_plan_tasks(brand_id, status)
--     WHERE status IN ('queued','running','settling','settlement_pending','refund_pending');
--   DELETE FROM _migrations WHERE name='v7_geoplan_settle_conflict_index_2026_07_13';
