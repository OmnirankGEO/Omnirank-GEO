-- Social Studio Agent Loop · Step 5 plan state
-- Safe to run repeatedly.

CREATE TABLE IF NOT EXISTS social_agent_plans (
    plan_id VARCHAR(36) PRIMARY KEY,
    profile_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    current_step INTEGER DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    user_intent_summary TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CHECK (status IN ('active', 'stale', 'completed', 'cancelled'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_social_agent_plans_one_active
ON social_agent_plans(user_id, profile_id)
WHERE status='active';

CREATE INDEX IF NOT EXISTS idx_social_agent_plans_profile_status
ON social_agent_plans(profile_id, status, updated_at DESC);
