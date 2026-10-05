-- ============================================================
-- V3.5 工厂模式 W2 · 后台 + API + DTO 隔离
-- 2026-05-26 · 无 BEGIN/COMMIT(Python wrapper 管事务 · idempotent · staging 用 --dry-run 验证)
--
-- 改动:
-- 1. recharge_orders 加 order_type / source_token / binding_source 三列
--    - order_type ∈ NULL/'customer_recharge'/'agent_inventory_purchase'
--    - source_token: 扫码 ID / 邀请码值(代理推广追踪)
--    - binding_source: 'qrcode' / 'ref_link' / 'invite_code' / 'admin_manual'(对齐 customer_agent_bindings 枚举)
-- 2. agent_settlement_requests 加 wire_transfer_no 列(银行流水号 / 微信交易号)
-- 3. agent_revenue_ledger 加索引(代理可提 items 查询性能)
-- ============================================================

-- 1. recharge_orders 扩列
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS order_type TEXT;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS source_token TEXT;
ALTER TABLE recharge_orders ADD COLUMN IF NOT EXISTS binding_source TEXT;

-- 把 NULL 视为 'customer_recharge'(向后兼容)
CREATE INDEX IF NOT EXISTS idx_recharge_order_type ON recharge_orders(order_type);
CREATE INDEX IF NOT EXISTS idx_recharge_source_token ON recharge_orders(source_token);

-- 2. agent_settlement_requests 扩 5 列(审批 audit trail + 凭证)
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS wire_transfer_no TEXT;
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS approved_by_user_id INTEGER;
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP;
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS rejected_by_user_id INTEGER;
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMP;
ALTER TABLE agent_settlement_requests ADD COLUMN IF NOT EXISTS reject_reason TEXT;

-- 3. agent_revenue_ledger 加索引:代理可提 items 查询(settled + 未锁定到任何 request)
CREATE INDEX IF NOT EXISTS idx_agent_revenue_available
    ON agent_revenue_ledger(agent_user_id, status, settlement_request_id)
    WHERE status = 'settled' AND settlement_request_id IS NULL;

-- ============================================================
-- 验证(失败立即 RAISE)
-- ============================================================
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM information_schema.columns
    WHERE table_name = 'recharge_orders'
      AND column_name IN ('order_type', 'source_token', 'binding_source');
    IF v_count <> 3 THEN
        RAISE EXCEPTION 'W2 migration FAILED: recharge_orders 3 列未全部加上 · 实际 %', v_count;
    END IF;

    SELECT COUNT(*) INTO v_count
    FROM information_schema.columns
    WHERE table_name = 'agent_settlement_requests'
      AND column_name IN ('wire_transfer_no','approved_by_user_id','approved_at',
                          'rejected_by_user_id','rejected_at','reject_reason');
    IF v_count <> 6 THEN
        RAISE EXCEPTION 'W2 migration FAILED: agent_settlement_requests 6 列未全加 · 实际 %', v_count;
    END IF;

    RAISE NOTICE 'V35 W2 migration verified OK';
END $$;
