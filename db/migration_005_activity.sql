-- migration_005_activity.sql
-- P2: 成员活动追踪
-- activity_log 表：记录用户在团队内的关键操作

CREATE TABLE IF NOT EXISTS activity_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    action_type VARCHAR(30) NOT NULL,   -- 'corpus_upload' | 'script_create' | 'review' | 'topic_confirm' | 'login'
    metadata JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activity_log_user ON activity_log(user_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_team ON activity_log(team_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_action ON activity_log(action_type);
CREATE INDEX IF NOT EXISTS idx_activity_log_created ON activity_log(created_at);
CREATE INDEX IF NOT EXISTS idx_activity_log_team_created ON activity_log(team_id, created_at);
