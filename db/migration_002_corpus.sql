-- migration_002_corpus.sql
-- Phase 2: 个人语料 + LLM特征提取
-- profile_corpus: 个人语料记录
-- profile_style: 个人风格画像（每人一条）
-- team_corpus: 团队共享语料

CREATE TABLE IF NOT EXISTS profile_corpus (
    id SERIAL PRIMARY KEY,
    profile_id VARCHAR(20) NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    team_id INT REFERENCES teams(id) ON DELETE SET NULL,
    source_type VARCHAR(20),                          -- 'audio' | 'text' | 'recording'
    source_name VARCHAR(200),
    raw_text TEXT,
    extracted_features JSONB,
    personal_quotes JSONB,
    style_markers JSONB,
    business_insights JSONB,
    feature_density FLOAT,
    word_count INT,
    audio_duration INT,                               -- 音频时长(秒)
    asr_status VARCHAR(20) DEFAULT 'completed',       -- 'completed'|'failed'|'pending'|'manual'
    shared_to_team BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS profile_style (
    id SERIAL PRIMARY KEY,
    profile_id VARCHAR(20) UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    style_profile JSONB,                              -- 完整风格画像JSON
    corpus_count INT DEFAULT 0,
    total_duration INT DEFAULT 0,                     -- 累计音频时长(秒)
    top_quotes JSONB,
    business_knowledge JSONB,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS team_corpus (
    id SERIAL PRIMARY KEY,
    team_id INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    contributed_by INTEGER NOT NULL REFERENCES users(id),
    source_corpus_id INT REFERENCES profile_corpus(id) ON DELETE SET NULL,
    title VARCHAR(200),
    shared_quotes JSONB,
    shared_insights JSONB,
    shared_talking_points JSONB,
    tags JSONB,
    is_pinned BOOLEAN DEFAULT FALSE,
    upvotes INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'active',              -- 'active'|'archived'
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_corpus_profile ON profile_corpus(profile_id);
CREATE INDEX IF NOT EXISTS idx_corpus_user ON profile_corpus(user_id);
CREATE INDEX IF NOT EXISTS idx_corpus_team ON profile_corpus(team_id);
CREATE INDEX IF NOT EXISTS idx_corpus_asr ON profile_corpus(asr_status);
CREATE INDEX IF NOT EXISTS idx_style_profile ON profile_style(profile_id);
CREATE INDEX IF NOT EXISTS idx_team_corpus_team ON team_corpus(team_id);
CREATE INDEX IF NOT EXISTS idx_team_corpus_status ON team_corpus(status);
