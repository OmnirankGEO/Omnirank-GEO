-- Social Studio Agent Loop · Step 8 tool audit log
-- Safe to run repeatedly.

CREATE TABLE IF NOT EXISTS social_agent_tool_audit (
    id BIGSERIAL PRIMARY KEY,
    turn_id TEXT,
    user_id INTEGER,
    profile_id TEXT,
    tool_name TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    result_chars INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL,
    error TEXT,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost_points INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CHECK (status IN ('ok', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_social_agent_tool_audit_turn
ON social_agent_tool_audit(turn_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_social_agent_tool_audit_profile
ON social_agent_tool_audit(profile_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_social_agent_tool_audit_tool_status
ON social_agent_tool_audit(tool_name, status, created_at DESC);
