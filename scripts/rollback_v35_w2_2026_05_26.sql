-- V35 W2 rollback · 无 BEGIN/COMMIT(Python wrapper 管)
DROP INDEX IF EXISTS idx_agent_revenue_available;
DROP INDEX IF EXISTS idx_recharge_source_token;
DROP INDEX IF EXISTS idx_recharge_order_type;

ALTER TABLE agent_settlement_requests DROP COLUMN IF EXISTS reject_reason;
ALTER TABLE agent_settlement_requests DROP COLUMN IF EXISTS rejected_at;
ALTER TABLE agent_settlement_requests DROP COLUMN IF EXISTS rejected_by_user_id;
ALTER TABLE agent_settlement_requests DROP COLUMN IF EXISTS approved_at;
ALTER TABLE agent_settlement_requests DROP COLUMN IF EXISTS approved_by_user_id;
ALTER TABLE agent_settlement_requests DROP COLUMN IF EXISTS wire_transfer_no;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS binding_source;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS source_token;
ALTER TABLE recharge_orders DROP COLUMN IF EXISTS order_type;
