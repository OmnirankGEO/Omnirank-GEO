-- Social Studio Agent Loop · Step 9 billing support
-- Safe to run repeatedly.

ALTER TABLE social_agent_tool_audit
    ADD COLUMN IF NOT EXISTS charged_points INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refunded_points INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS billing_status VARCHAR(30),
    ADD COLUMN IF NOT EXISTS billing_note TEXT;

CREATE INDEX IF NOT EXISTS idx_social_agent_tool_audit_billing_status
ON social_agent_tool_audit(billing_status, created_at DESC);

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
VALUES ('agent_tool_call', '社媒 Agent 工具调用', 0, 0.0, FALSE)
ON CONFLICT (feature_code) DO UPDATE
SET feature_name = EXCLUDED.feature_name,
    cost_points = 0,
    cost_compute = 0.0,
    requires_paid_points = FALSE,
    is_active = TRUE;
