-- ============================================================================
-- [v11 集中审核修 · Deploy-CTO 2026-07-13] agent_sku_overrides 加 deleted_at 显式删除标记
--   additive · 幂等。目的:区分【显式软删除】(代理主动删掉·deleted_at 非空)与【临时下架】(is_active=FALSE·deleted_at 空)。
--   一键应用系数(apply_markup_for_agent loop B)只对"临时下架/从未上架"的可售默认规格 materialize active 包,
--   【不 resurrect 显式删除的包】—— 否则代理删掉的 SKU 会因调系数悄悄重新上架售卖(集中审核 P3)。
-- 执行:docker exec -i omnirank-db psql -U geo_admin geo_agentscope < scripts/migration_v11_agent_sku_override_deleted_at_2026_07_13.sql
-- 验证:docker exec omnirank-db psql -U geo_admin geo_agentscope -c "\d agent_sku_overrides" | grep deleted_at
-- ============================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE agent_sku_overrides ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- 回填:历史上被软删除的行(is_active=FALSE 且被 recharge_orders 引用 = 曾因订单引用而软删)标记 deleted_at。
--   保守回填:仅回填【确被订单引用】的 inactive 行(这些正是 delete_agent_sku_override 的软删产物);
--   未被引用的 inactive 行视为"临时下架"(可被一键定价重新 materialize),不回填。
UPDATE agent_sku_overrides o
   SET deleted_at = COALESCE(o.deleted_at, o.updated_at, NOW())
 WHERE o.is_active = FALSE AND o.deleted_at IS NULL
   AND EXISTS (SELECT 1 FROM recharge_orders r WHERE r.override_id = o.id);

INSERT INTO _migrations (name, applied_at)
VALUES ('v11_agent_sku_override_deleted_at_2026_07_13', NOW())
ON CONFLICT (name) DO NOTHING;

COMMIT;

-- ROLLBACK(还原):
--   ALTER TABLE agent_sku_overrides DROP COLUMN IF EXISTS deleted_at;
--   DELETE FROM _migrations WHERE name='v11_agent_sku_override_deleted_at_2026_07_13';
--   (回填仅写了新列 · DROP COLUMN 即完全撤销 · 不动 is_active/其它业务列)
