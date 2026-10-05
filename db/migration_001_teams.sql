-- migration_001_teams.sql
-- Phase 0: 团队隔离体系
-- 创建 teams, team_members, notifications 表
-- user_id: INTEGER (匹配 users.id SERIAL)
-- profile_id: VARCHAR(20) (匹配实际8字符短UUID)
-- brand_id: INTEGER (匹配 brands.id)

CREATE TABLE IF NOT EXISTS teams (
    id SERIAL PRIMARY KEY,
    team_name VARCHAR(100) NOT NULL,
    team_code VARCHAR(10) UNIQUE NOT NULL,   -- 6位随机码，如 "HD-A3X9"
    region VARCHAR(50),
    description TEXT,
    leader_id INTEGER REFERENCES users(id),
    brand_id INTEGER REFERENCES brands(id),
    max_members INT DEFAULT 50,
    status VARCHAR(20) DEFAULT 'active',     -- 'active' | 'frozen' | 'dissolved'
    created_by INTEGER REFERENCES users(id),
    dissolved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS team_members (
    id SERIAL PRIMARY KEY,
    team_id INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    profile_id VARCHAR(20),
    role VARCHAR(20) DEFAULT 'member',       -- 'leader' | 'member'
    joined_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'active',     -- 'active' | 'removed'
    UNIQUE(team_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_teams_brand ON teams(brand_id);
CREATE INDEX IF NOT EXISTS idx_teams_code ON teams(team_code);
CREATE INDEX IF NOT EXISTS idx_teams_status ON teams(status);
CREATE INDEX IF NOT EXISTS idx_team_members_team ON team_members(team_id);
CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);

-- 用户通知表（区别于现有 brand 级别的 notifications 表）
-- 现有 notifications 表以 brand_id/client_id 为维度，面向监测报告
-- 新表以 user_id 为维度，面向团队/社媒/成就等个人通知
CREATE TABLE IF NOT EXISTS user_notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    type VARCHAR(30) NOT NULL,        -- 'team_join' | 'weekly_report' | 'achievement' | 'alert' | 'corpus_done' | 'system'
    title VARCHAR(200),
    content TEXT,
    link VARCHAR(300),                -- 点击跳转的页面路径
    is_read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_notifications_user ON user_notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_user_notifications_created ON user_notifications(created_at);
