-- ============================================================================
-- [v11 F6 · Deploy-CTO 2026-07-13] channel_revenue 重复未终结工单 —— 备份 + 折叠 + 可执行逐行恢复
--   仅当 migration_v10_channel_revenue_exactly_once 预检 RAISE(存在重复)时才需要跑本脚本。
--   本脚本【不静默】:折叠前把每一条【将被改动的行】完整快照进备份表,提供【可执行、可验证的逐行恢复】。
--   跑完本脚本(去重)后,再回去执行 migration_v10(此时预检为零 → 干净建唯一键)。
-- 执行:
--   docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v10b_dedup_channel_revenue_with_backup_2026_07_13.sql
-- ============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- 1. 备份表(幂等)· 快照【将被折叠】的完整行(含原 status/resolved_at/updated_at/last_error 供逐行恢复)
CREATE TABLE IF NOT EXISTS fund_recovery_orders_v10_dedup_backup (
  backup_id        BIGSERIAL PRIMARY KEY,
  order_id         BIGINT NOT NULL,
  orig_status      TEXT,
  orig_resolved_at TIMESTAMPTZ,
  orig_updated_at  TIMESTAMPTZ,
  orig_last_error  TEXT,
  backed_up_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  batch_tag        TEXT NOT NULL DEFAULT 'v10b_2026_07_13'
);
-- [v12 item5] 备份表唯一键 (order_id, batch_tag):防【重复执行 forward】撞唯一键 + 不覆盖首次完整备份
--   (首次备份的 orig_* 是恢复基准 · 二次执行必须 no-op 不改它)。
-- @index-guard uq_v10b_dedup_backup_order ON fund_recovery_orders_v10_dedup_backup unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_v10b_dedup_backup_order' AND i.indrelid = to_regclass('public.fund_recovery_orders_v10_dedup_backup')) THEN
        NULL;  -- 已在 public.fund_recovery_orders_v10_dedup_backup 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_v10b_dedup_backup_order' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_v10b_dedup_backup_order 已存在但不在 public.fund_recovery_orders_v10_dedup_backup 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_v10b_dedup_backup_order' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_v10b_dedup_backup_order ON public.fund_recovery_orders_v10_dedup_backup (order_id, batch_tag);
    END IF;
END $idxguard$;

-- 2. 快照要折叠的行:每 (source,ref_key,kind) 保最新 id,其余(rn>1)全量入备份。
--   [v12 item5] ON CONFLICT DO NOTHING → 二次 forward 即便重新命中同 order_id 也不覆盖首次备份(保恢复基准)。
INSERT INTO fund_recovery_orders_v10_dedup_backup
       (order_id, orig_status, orig_resolved_at, orig_updated_at, orig_last_error)
SELECT f.id, f.status, f.resolved_at, f.updated_at, f.last_error
  FROM fund_recovery_orders f
  JOIN (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY source, ref_key, kind ORDER BY id DESC) AS rn
      FROM fund_recovery_orders
     WHERE source='channel_revenue' AND ref_key IS NOT NULL
       AND status IN ('pending','processing','manual')
  ) r ON r.id = f.id AND r.rn > 1
ON CONFLICT (order_id, batch_tag) DO NOTHING;

-- 3. 折叠 —— 只改【已入备份】的那些行(其余留最新一条)
UPDATE fund_recovery_orders f
   SET status='resolved', resolved_at=NOW(), updated_at=NOW(),
       last_error = COALESCE(last_error,'') || ' · [v10b dedup·已备份] 折叠重复 channel_revenue 工单(保最新一条)'
  FROM fund_recovery_orders_v10_dedup_backup b
 WHERE f.id = b.order_id AND b.batch_tag='v10b_2026_07_13'
   -- 二次 forward 必须是真 no-op：只折叠仍未终结的备份行，禁止刷新
   -- resolved_at 或重复追加 last_error。
   AND f.status IN ('pending','processing','manual');

COMMIT;

-- 验证(折叠数 == 备份数 · 折叠后无重复):
--   SELECT (SELECT COUNT(*) FROM fund_recovery_orders_v10_dedup_backup WHERE batch_tag='v10b_2026_07_13') AS backed_up;
--   SELECT COUNT(*) AS remaining_dups FROM (
--     SELECT 1 FROM fund_recovery_orders
--      WHERE source='channel_revenue' AND ref_key IS NOT NULL AND status IN ('pending','processing','manual')
--      GROUP BY source, ref_key, kind HAVING COUNT(*)>1) d;   -- 应为 0

-- ROLLBACK(可执行 · 逐行精确恢复原状态 · 可验证):
--   🔴 [v11 集中审核修] 若 migration_v10(部分唯一索引 uniq_fund_recovery_channel_open)【已应用】,必须先
--      DROP 该索引再恢复 —— 否则把折叠行 status 还原回 pending/processing/manual 会与"保留的最新一条"重新
--      构成 2+ 未终结行,触发唯一键冲突使整个 rollback 事务 abort。恢复后如需重新收口再单飞 v10(会预检)。
--   BEGIN;
--   DROP INDEX IF EXISTS uniq_fund_recovery_channel_open;   -- 若 v10 已建键:先撤键,恢复才不撞唯一约束
--   UPDATE fund_recovery_orders f
--      SET status=b.orig_status, resolved_at=b.orig_resolved_at,
--          updated_at=b.orig_updated_at, last_error=b.orig_last_error
--      FROM fund_recovery_orders_v10_dedup_backup b
--     WHERE f.id=b.order_id AND b.batch_tag='v10b_2026_07_13';
--   -- 验证逐行一致(应为 0):
--   SELECT COUNT(*) AS mismatched FROM fund_recovery_orders f
--     JOIN fund_recovery_orders_v10_dedup_backup b ON b.order_id=f.id AND b.batch_tag='v10b_2026_07_13'
--    WHERE f.status IS DISTINCT FROM b.orig_status
--       OR f.resolved_at IS DISTINCT FROM b.orig_resolved_at
--       OR f.last_error  IS DISTINCT FROM b.orig_last_error;
--   COMMIT;
