-- Refund work order center migration · 2026-06-09
-- Safe to run repeatedly. No destructive SQL.

CREATE TABLE IF NOT EXISTS refund_work_orders (
    id BIGSERIAL PRIMARY KEY,
    source_order_id TEXT NOT NULL,
    customer_user_id INTEGER,
    agent_user_id INTEGER,
    refund_reason_category TEXT NOT NULL DEFAULT 'other',
    refund_reason_detail TEXT DEFAULT '',
    refund_method TEXT NOT NULL DEFAULT 'manual_wechat',
    requested_refund_cents INTEGER NOT NULL DEFAULT 0,
    estimated_refund_cents INTEGER NOT NULL DEFAULT 0,
    refundable_power BIGINT NOT NULL DEFAULT 0,
    customer_requested_at TIMESTAMP,
    agent_confirmed_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'draft',
    impact_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by INTEGER,
    reviewed_by INTEGER,
    payout_operator_id INTEGER,
    payout_proof_url TEXT,
    rejected_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    submitted_at TIMESTAMP,
    reviewed_at TIMESTAMP,
    executed_at TIMESTAMP,
    completed_at TIMESTAMP,
    last_action_error TEXT,
    execution_result JSONB,
    CONSTRAINT refund_work_orders_status_check CHECK (
        status IN ('draft','submitted','approved','payout_pending','completed','rejected','cancelled')
    )
);

CREATE TABLE IF NOT EXISTS refund_work_order_attachments (
    id BIGSERIAL PRIMARY KEY,
    work_order_id BIGINT NOT NULL REFERENCES refund_work_orders(id) ON DELETE CASCADE,
    file_url TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    evidence_type TEXT NOT NULL DEFAULT 'other',
    mime_type TEXT,
    file_size_bytes BIGINT NOT NULL DEFAULT 0,
    uploaded_by INTEGER,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS refund_work_order_events (
    id BIGSERIAL PRIMARY KEY,
    work_order_id BIGINT NOT NULL REFERENCES refund_work_orders(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor_user_id INTEGER,
    note TEXT DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_refund_work_orders_status_time
ON refund_work_orders(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_refund_work_orders_order
ON refund_work_orders(source_order_id);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_refund_work_orders_open_order
ON refund_work_orders(source_order_id)
WHERE status IN ('draft','submitted','approved','payout_pending');

CREATE INDEX IF NOT EXISTS idx_refund_work_order_attachments_work_order
ON refund_work_order_attachments(work_order_id, uploaded_at DESC);

CREATE INDEX IF NOT EXISTS idx_refund_work_order_events_work_order
ON refund_work_order_events(work_order_id, created_at ASC);
