-- Social Studio Agent Loop · Step 12 metrics support
-- Safe to run repeatedly.

CREATE TABLE IF NOT EXISTS social_agent_metrics (
    id BIGSERIAL PRIMARY KEY,
    turn_id TEXT,
    user_id INTEGER,
    profile_id TEXT,
    model TEXT,
    used_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    tool_error_count INTEGER NOT NULL DEFAULT 0,
    cost_points INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    plan_completed BOOLEAN NOT NULL DEFAULT FALSE,
    confirmation_requested BOOLEAN NOT NULL DEFAULT FALSE,
    confirmation_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_social_agent_metrics_created
ON social_agent_metrics(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_social_agent_metrics_profile_created
ON social_agent_metrics(profile_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_social_agent_metrics_turn
ON social_agent_metrics(turn_id);
