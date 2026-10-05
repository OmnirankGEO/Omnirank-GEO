-- [BUG-P2] 退款工单上级服务商签字硬字段
-- 根因:refund_work_order_api execute 硬编码 service_provider_signoff=True 传给 request_refund,
-- 静默绕过铁律(下级退款须上级服务商先沟通签字)。改为工单显式记录签字 + execute 传实际值 + 未签字 fail-closed。
-- prod 实证(2026-06-10 SSH):refund_work_orders 0 行 · 无 agent_signoff 列 → ADD COLUMN NOT NULL DEFAULT 安全。
-- 与代码 db/refund_work_order_db.py:init_refund_work_order_tables 的自愈 ALTER 等价(幂等)。
BEGIN;

ALTER TABLE refund_work_orders
    ADD COLUMN IF NOT EXISTS agent_signoff_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE refund_work_orders
    ADD COLUMN IF NOT EXISTS agent_signoff_note TEXT DEFAULT '';
ALTER TABLE refund_work_orders
    ADD COLUMN IF NOT EXISTS agent_signoff_by INTEGER;
ALTER TABLE refund_work_orders
    ADD COLUMN IF NOT EXISTS agent_signoff_at TIMESTAMP;

COMMIT;

-- 核验:
-- SELECT column_name FROM information_schema.columns
--  WHERE table_name='refund_work_orders' AND column_name LIKE 'agent_signoff%';
