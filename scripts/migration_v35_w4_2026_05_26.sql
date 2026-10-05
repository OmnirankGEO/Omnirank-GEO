-- ============================================================
-- V3.5 W4 · 治理 + 对账日报 + dispute audit · schema
-- 2026-05-26 · idempotent · 无 BEGIN/COMMIT
-- ============================================================

-- 1. 绑定争议历史表(W2 customer_agent_bindings.dispute_status 是当前状态 · 本表是历史 audit)
CREATE TABLE IF NOT EXISTS customer_agent_binding_disputes (
    id BIGSERIAL PRIMARY KEY,
    customer_user_id INTEGER NOT NULL,
    old_agent_user_id INTEGER NOT NULL,
    new_agent_user_id INTEGER NOT NULL,
    binding_source TEXT,
    source_token TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending','resolved_keep_old','resolved_reassign','rejected')),
    admin_decision TEXT,
    admin_user_id INTEGER,
    note TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_binding_disputes_customer ON customer_agent_binding_disputes(customer_user_id, status);
CREATE INDEX IF NOT EXISTS idx_binding_disputes_status ON customer_agent_binding_disputes(status, created_at DESC);

-- 2. 线下划拨明细(给分批 revoke 用 · W2 仅总余额 cap · W4 升级)
CREATE TABLE IF NOT EXISTS agent_inventory_allocations (
    id BIGSERIAL PRIMARY KEY,
    agent_user_id INTEGER NOT NULL,
    customer_user_id INTEGER NOT NULL,
    allocation_ref TEXT UNIQUE NOT NULL,       -- offline_xxx 或 order_id
    source TEXT NOT NULL CHECK (source IN ('online_payment','offline_allocation','admin_adjust')),
    tool_points INTEGER NOT NULL DEFAULT 0,
    publish_points INTEGER NOT NULL DEFAULT 0,
    bonus_points INTEGER NOT NULL DEFAULT 0,
    revoked_tool INTEGER NOT NULL DEFAULT 0,
    revoked_publish INTEGER NOT NULL DEFAULT 0,
    revoked_bonus INTEGER NOT NULL DEFAULT 0,
    consumed_estimate INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_inv_alloc_agent ON agent_inventory_allocations(agent_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_inv_alloc_customer ON agent_inventory_allocations(customer_user_id, source);

-- 3. 对账快照表
CREATE TABLE IF NOT EXISTS inventory_audit_runs (
    id BIGSERIAL PRIMARY KEY,
    run_at TIMESTAMP DEFAULT NOW(),
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ('cron','admin_manual')),
    triggered_by_user_id INTEGER,
    agent_total_paid INTEGER NOT NULL,
    agent_total_bonus INTEGER NOT NULL,
    customer_total_tool INTEGER NOT NULL,
    customer_total_publish INTEGER NOT NULL,
    customer_total_bonus INTEGER NOT NULL,
    platform_consumed INTEGER NOT NULL,
    historical_purchased INTEGER NOT NULL,
    historical_admin_adjust INTEGER NOT NULL,
    refunded_or_revoked INTEGER NOT NULL DEFAULT 0,
    diff_paid INTEGER NOT NULL,
    diff_bonus INTEGER NOT NULL,
    diff_publish INTEGER NOT NULL,
    has_drift BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_runs_at ON inventory_audit_runs(run_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_runs_drift ON inventory_audit_runs(has_drift, run_at DESC);

CREATE TABLE IF NOT EXISTS inventory_audit_diffs (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES inventory_audit_runs(id) ON DELETE CASCADE,
    track TEXT NOT NULL CHECK (track IN ('paid','bonus','publish')),
    diff_points INTEGER NOT NULL,
    detail JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_diffs_run ON inventory_audit_diffs(run_id, track);

-- 4. 验证
DO $$
DECLARE v_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_count FROM information_schema.tables
     WHERE table_name IN (
        'customer_agent_binding_disputes',
        'agent_inventory_allocations',
        'inventory_audit_runs',
        'inventory_audit_diffs'
     );
    IF v_count <> 4 THEN
        RAISE EXCEPTION 'W4 migration FAILED: 4 张表未全建 · 实际 %', v_count;
    END IF;
    RAISE NOTICE 'V35 W4 migration verified OK';
END $$;
