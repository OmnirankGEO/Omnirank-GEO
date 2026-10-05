-- ============================================================================
-- 回滚 · 板块 C 白标作用域纠正（2026-07-22）
-- 仅移除本批新增对象；不触碰任何既有列/数据/快照。
--
-- ⚠️⚠️ 执行前建议备份审计表（回滚不删表，但归档改名前仍需保底快照）：
--   docker exec omnirank-db pg_dump -U geo_admin geo_agentscope -t whitelabel_audit > backup_whitelabel_audit_$(date +%Y%m%d).sql
--
-- 审计历史保护（集中严审 R1 · P2-3 + R4 · P1-4 fresh 守卫）：
--   whitelabel_audit 不 DROP —— DROP 会让 append-only 审计历史随回滚丢失。
--   改为 RENAME 归档保留（whitelabel_audit_archived_20260722），
--   dba 核验备份无误后可人工清理归档表。
--
-- fail-closed（R4 · P1-4）：
--   1. fresh 空库（whitelabel_settings / whitelabel_audit 均不存在）→ 全部安全跳过，
--      可连续执行两次；
--   2. audit 表存在但归档名 whitelabel_audit_archived_20260722 已被占用 → 真实冲突，
--      以稳定错误码整体中止（WHITELABEL_BACKOFFICE_ROLLBACK_BLOCKED_ARCHIVE_CONFLICT），
--      绝不覆盖/丢弃既有审计归档；
--   3. 函数/触发器不存在 → 安全跳过；
--   4. 永不 DROP 审计历史；归档只 RENAME，不覆盖。
--
-- [统一 R3 §七 · 2026-07-23] 锁纪律与 forward 对齐：
--   同一事务级咨询锁（key=2026072201）+ 有界 lock_timeout（5s）；forward×rollback
--   并发被序列化，等锁超 5s 以 55P03 lock_not_available 整体中止（无半态，可安全重跑）。
--   合同函数 public.whitelabel_backoffice_schema_blockers(boolean) 为本批新增对象，
--   随回滚一并 DROP（幂等 IF EXISTS）。
-- ============================================================================

BEGIN;
SET LOCAL search_path = pg_catalog, public;
-- [统一 R3 §七] 有界锁等待 + 与 forward 同一咨询锁（首部获取，覆盖后续全部 DO 块；
-- 下方 DO 块内重入为 no-op，保留以维持 R7 竞态收口的局部可读性）。
SET LOCAL lock_timeout = '5000';
SELECT pg_catalog.pg_advisory_xact_lock(2026072201);

-- append-only 触发器清理（先于表 RENAME 归档，否则归档表仍挂触发器、函数残留 drift）。
-- 表不存在时 DROP TRIGGER 会因 relation missing 直接报错，故按 to_regclass 守卫。
-- [R7 · 复审 P2] check-then-rename 竞态收口：事务级咨询锁（key=2026072201）在本脚本
-- 首部获取，序列化并发回滚执行；锁为事务域，覆盖后续全部 DO 块。
DO $$
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(2026072201);
    IF pg_catalog.to_regclass('public.whitelabel_audit') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_whitelabel_audit_append_only ON public.whitelabel_audit;';
    END IF;
END $$;
DROP FUNCTION IF EXISTS public.trg_whitelabel_audit_append_only();
-- [统一 R3 §七] 单一 readiness 合同函数同属本批新增对象，随回滚移除（幂等）。
DROP FUNCTION IF EXISTS public.whitelabel_backoffice_schema_blockers(boolean);

-- 审计表归档保留。注意：PostgreSQL RENAME 表时显式命名的索引【不】跟随改名
-- （R5 复审真实 PG16 实测），旧索引继续占用 canonical 名会让下次 forward 的
-- CREATE INDEX IF NOT EXISTS 静默跳过、新 audit 表丢 (user_id, created_at DESC)
-- 索引；故 RENAME 后必须同步显式重命名索引释放 canonical 名。
-- [R7 · 复审 P2] 竞态收口：对象一律按 OID 操作（咨询锁后固定，避免名字解析 TOCTOU）；
-- 重命名后再次核验归档对象 OID 与锁定对象一致；索引归属按 OID（pg_index 行级）核验。
-- 归档表保持完整可查，dba 核验后可人工 DROP。归档名已被占用时 fail-closed，
-- 不覆盖既有归档（二次回滚须先由 dba 处理归档表）。
DO $$
DECLARE
    audit_oid OID;
    idx_oid OID;
BEGIN
    -- 咨询锁（上方已获取，同事务内重入为 no-op）后再固定 OID，关闭检查-改名窗口。
    PERFORM pg_catalog.pg_advisory_xact_lock(2026072201);
    audit_oid := pg_catalog.to_regclass('public.whitelabel_audit');
    idx_oid := pg_catalog.to_regclass('public.idx_whitelabel_audit_user_created');

    IF audit_oid IS NULL THEN
        RAISE NOTICE 'whitelabel_audit 不存在（fresh 或已回滚）：跳过审计表归档';
        RETURN;
    END IF;
    IF pg_catalog.to_regclass('public.whitelabel_audit_archived_20260722') IS NOT NULL THEN
        RAISE EXCEPTION 'WHITELABEL_BACKOFFICE_ROLLBACK_BLOCKED_ARCHIVE_CONFLICT: whitelabel_audit_archived_20260722 已存在，拒绝覆盖既有审计归档';
    END IF;

    ALTER TABLE public.whitelabel_audit RENAME TO whitelabel_audit_archived_20260722;

    -- 重命名后再次核验：归档对象必须就是锁定时固定的那个 OID（否则整体回滚）。
    IF pg_catalog.to_regclass('public.whitelabel_audit_archived_20260722') IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'WHITELABEL_BACKOFFICE_ROLLBACK_BLOCKED_RENAME_IDENTITY_MISMATCH: 归档对象 OID 与锁定对象不一致';
    END IF;

    -- 索引归属按 OID 核验（而非名字）：仅当 canonical 索引确属归档表（OID）才重命名；
    -- 同名异物（其他表占用 canonical 名）属真实冲突 → fail-closed，不误改。
    IF idx_oid IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
              FROM pg_catalog.pg_index i
             WHERE i.indexrelid = idx_oid
               AND i.indrelid = audit_oid
        ) THEN
            ALTER INDEX public.idx_whitelabel_audit_user_created
                RENAME TO idx_whitelabel_audit_archived_20260722_user_created;
        ELSE
            RAISE EXCEPTION 'WHITELABEL_BACKOFFICE_ROLLBACK_BLOCKED_INDEX_OWNERSHIP_CONFLICT: idx_whitelabel_audit_user_created 不属于归档 audit 表，拒绝误改同名异物索引';
        END IF;
    END IF;
END $$;

-- settings 新列：public 精确 schema；fresh 空库表不存在时 ALTER TABLE IF EXISTS 安全跳过。
ALTER TABLE IF EXISTS public.whitelabel_settings DROP COLUMN IF EXISTS backoffice_brand_unlocked;
ALTER TABLE IF EXISTS public.whitelabel_settings DROP COLUMN IF EXISTS backoffice_brand_granted_by;
ALTER TABLE IF EXISTS public.whitelabel_settings DROP COLUMN IF EXISTS backoffice_brand_granted_at;
ALTER TABLE IF EXISTS public.whitelabel_settings DROP COLUMN IF EXISTS brand_version;

COMMIT;
