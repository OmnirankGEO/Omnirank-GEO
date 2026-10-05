-- ============================================================================
-- 回滚 · 板块 A 诊断品牌身份确认 events 表泛化（2026-07-22）
-- 对应迁移：scripts/migration_diagnosis_identity_review_2026_07_22.sql
--
-- fail-closed（R4 · P1-2；R5 · 复审 P1 修复）：
--   1. 表不存在（fresh 环境）→ 安全跳过；列不存在（未 forward / 已回滚）→ 安全跳过；
--   2. 只要存在 source_kind='diagnosis' 的耐久事件，立即以稳定错误码整体中止：
--        GEO_DIAGNOSIS_IDENTITY_ROLLBACK_BLOCKED_DURABLE_EVENTS
--      诊断人工确认事件是不可逆审计血缘，逆 additive 清理（DROP COLUMN）会把
--      血缘随列永久删除，绝不以"人工提前备份"充当代码守卫；
--   3. 仅当确认零 diagnosis 耐久事件，才执行逆 additive 清理（约束先于列）；
--   4. 恢复 result_id NOT NULL：回滚后生产 prestart 按 manifest 顺序会先重放
--      旧监测迁移，其 result_id 契约核验要求非空；保留放宽态会让下一次部署
--      直接 abort（R5 复审真实 PG16 复现）。恢复前先防御性核验无 NULL 行：
--        GEO_DIAGNOSIS_IDENTITY_ROLLBACK_BLOCKED_NULL_RESULT_ID
--      （chk_monitoring_identity_event_source 已保证 monitoring 行非空、
--      diagnosis 事件已被 §2 拦截，此处为兜底核验，命中即整体回滚）；
--   5. 本脚本不 DELETE / 不 TRUNCATE 任何事件行；失败时事件行、列、约束完整保留。
--
-- 幂等：全部 IF EXISTS / to_regclass 守卫，可连跑两次。
-- ============================================================================

BEGIN;
SET LOCAL search_path = pg_catalog, public;

DO $$
DECLARE
    durable_count BIGINT := 0;
    null_count BIGINT := 0;
BEGIN
    -- 1. 表不存在（fresh）：列/约束随表一起缺失，无需任何清理。
    IF pg_catalog.to_regclass('public.monitoring_identity_decision_events') IS NULL THEN
        RAISE NOTICE 'monitoring_identity_decision_events 不存在：跳过诊断泛化回滚';
        RETURN;
    END IF;

    -- 2. source_kind 列不存在（从未 forward 或已完成回滚）：安全跳过。
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_attribute
         WHERE attrelid = 'public.monitoring_identity_decision_events'::pg_catalog.regclass
           AND attname = 'source_kind'
           AND attnum > 0
           AND NOT attisdropped
    ) THEN
        RAISE NOTICE 'source_kind 列不存在（未 forward 或已回滚）：跳过诊断泛化回滚';
        RETURN;
    END IF;

    -- 3. 存在 diagnosis 耐久事件 → fail-closed，整体回滚，稳定错误码。
    EXECUTE 'SELECT COUNT(*) FROM public.monitoring_identity_decision_events WHERE source_kind = ''diagnosis'''
       INTO durable_count;
    IF durable_count > 0 THEN
        RAISE EXCEPTION 'GEO_DIAGNOSIS_IDENTITY_ROLLBACK_BLOCKED_DURABLE_EVENTS: % durable diagnosis identity decision event(s) exist; refusing irreversible audit-lineage destruction', durable_count;
    END IF;

    -- 4. result_id NULL 行防御性核验：恢复 NOT NULL 前置条件；命中即整体回滚。
    EXECUTE 'SELECT COUNT(*) FROM public.monitoring_identity_decision_events WHERE result_id IS NULL'
       INTO null_count;
    IF null_count > 0 THEN
        RAISE EXCEPTION 'GEO_DIAGNOSIS_IDENTITY_ROLLBACK_BLOCKED_NULL_RESULT_ID: % row(s) carry NULL result_id; refusing to restore NOT NULL over them', null_count;
    END IF;
END $$;

-- 5. 仅当确认零 diagnosis 耐久事件：逆 additive 清理（约束先于列，否则 DROP COLUMN
--    会因依赖报错）。ALTER TABLE IF EXISTS 使 fresh/已回滚库同样安全。
ALTER TABLE IF EXISTS public.monitoring_identity_decision_events
    DROP CONSTRAINT IF EXISTS chk_monitoring_identity_event_source,
    DROP CONSTRAINT IF EXISTS chk_monitoring_identity_event_source_kind;

ALTER TABLE IF EXISTS public.monitoring_identity_decision_events
    DROP COLUMN IF EXISTS source_kind,
    DROP COLUMN IF EXISTS source_result_id,
    DROP COLUMN IF EXISTS tenant_id,
    DROP COLUMN IF EXISTS ip,
    DROP COLUMN IF EXISTS reason;

-- 6. 恢复 result_id NOT NULL（回到 D6 前形状）：prestart 全量重放时旧监测迁移
--    的 result_id 契约核验依赖非空；NULL 前置核验已在 §4 完成，此处必定成功。
ALTER TABLE IF EXISTS public.monitoring_identity_decision_events
    ALTER COLUMN result_id SET NOT NULL;

COMMIT;
