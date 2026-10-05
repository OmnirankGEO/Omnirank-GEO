-- V35 W3 rollback
-- [boss r12 P0 修正] W3 只删 W3 自己拥有的资产
--   ✓ W3 自己建:agent_factory_agreements 表 + 2 索引 + idx_recharge_sku 索引
--   ❌ 严禁删 W1 已建的 recharge_orders.sku_template_id 列
--     (W1 主 migration line 46 已 ADD COLUMN · 是 W1 资产 · W3 仅 index 它)
--     若 W3 rollback DROP 该列 → W1/W2 的 wallet_api/create_recharge_order/Orchestrator 链路全断
DROP INDEX IF EXISTS idx_recharge_sku;

DROP INDEX IF EXISTS idx_agent_factory_agreements_version;
DROP INDEX IF EXISTS idx_agent_factory_agreements_agent;
DROP TABLE IF EXISTS agent_factory_agreements;
