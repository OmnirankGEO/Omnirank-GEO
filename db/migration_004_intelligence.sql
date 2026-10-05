-- migration_004_intelligence.sql
-- Phase 6: 智能建议与进化系统

CREATE TABLE IF NOT EXISTS ai_suggestions (
    id SERIAL PRIMARY KEY,
    target_type VARCHAR(20) NOT NULL,       -- 'personal' | 'team' | 'global'
    target_id VARCHAR(50) NOT NULL,         -- profile_id 或 team_id 或 'global'
    suggestion_type VARCHAR(50) NOT NULL,   -- 'daily_tip' | 'topic_recommend' | 'style_advice' | 'team_insight'
    trigger_type VARCHAR(20),               -- 'scheduled' | 'milestone' | 'alert' | 'event'
    title VARCHAR(300),
    content JSONB NOT NULL,
    data_sources JSONB,
    is_read BOOLEAN DEFAULT FALSE,
    is_actionable BOOLEAN DEFAULT TRUE,
    action_taken BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS achievements (
    id SERIAL PRIMARY KEY,
    profile_id VARCHAR(50) NOT NULL,
    team_id INT REFERENCES teams(id),
    achievement_type VARCHAR(50) NOT NULL,   -- 'streak' | 'milestone' | 'top_performer' | 'first_time'
    achievement_name VARCHAR(100),
    achievement_detail JSONB,
    achieved_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS capability_snapshots (
    id SERIAL PRIMARY KEY,
    profile_id VARCHAR(50) NOT NULL,
    team_id INT REFERENCES teams(id),
    period VARCHAR(10),                      -- '2026-03' (月份)
    topic_selection FLOAT,
    opening_quality FLOAT,
    script_structure FLOAT,
    engagement_driving FLOAT,
    publishing_rhythm FLOAT,
    data_analysis FLOAT,
    overall_score FLOAT,
    llm_assessment TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_suggestions_target ON ai_suggestions(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_type ON ai_suggestions(suggestion_type);
CREATE INDEX IF NOT EXISTS idx_suggestions_created ON ai_suggestions(created_at);
CREATE INDEX IF NOT EXISTS idx_achievements_profile ON achievements(profile_id);
CREATE INDEX IF NOT EXISTS idx_capability_profile ON capability_snapshots(profile_id, period);
