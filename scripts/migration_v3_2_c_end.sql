-- ============================================
-- C 端重构 v3.2 — 数据库迁移
-- 日期: 2026-04-16
-- 作者: CTO-9.0
--
-- 幂等设计：所有 CREATE / ALTER 都带 IF NOT EXISTS
-- 可重复执行不会报错
-- ============================================

-- ============================================
-- 1. user_settings 表（如不存在则建）+ preferred_mode 字段
-- ============================================

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    preferred_mode VARCHAR(10) DEFAULT NULL,
        -- NULL = 自动按 agent_level 判定
        -- 'c'  = 明确选 C 端
        -- 'agent' = 明确选代理端
    notification_enabled BOOLEAN DEFAULT TRUE,
    personalization_enabled BOOLEAN DEFAULT TRUE,
    language VARCHAR(10) DEFAULT 'zh-CN',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 已存在则补字段
ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS preferred_mode VARCHAR(10) DEFAULT NULL;

ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS notification_enabled BOOLEAN DEFAULT TRUE;

ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS personalization_enabled BOOLEAN DEFAULT TRUE;

ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS language VARCHAR(10) DEFAULT 'zh-CN';

-- @index-guard idx_user_settings_mode ON user_settings plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_user_settings_mode' AND i.indrelid = to_regclass('public.user_settings')) THEN
        NULL;  -- 已在 public.user_settings 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_user_settings_mode' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_user_settings_mode 已存在但不在 public.user_settings 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_user_settings_mode' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_user_settings_mode ON public.user_settings (preferred_mode);
    END IF;
END $idxguard$;

-- ============================================
-- 2. user_action_logs 表（合规日志）
-- ============================================

CREATE TABLE IF NOT EXISTS user_action_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    action_type VARCHAR(50) NOT NULL,
        -- 'user_confirmed_publish'  用户确认发布（免责证据）
        -- 'ai_generate'             AI 生成内容
        -- 'user_confirmed_payment'  用户确认支付
        -- 'user_agreed_terms'       同意协议
        -- 'mode_switch'             模式切换
        -- 'visit_dashboard'         访问代理端仪表盘
    action_detail JSONB,
    ai_response TEXT,
    user_confirmed_at TIMESTAMP,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- @index-guard idx_user_action_logs_user ON user_action_logs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_user_action_logs_user' AND i.indrelid = to_regclass('public.user_action_logs')) THEN
        NULL;  -- 已在 public.user_action_logs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_user_action_logs_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_user_action_logs_user 已存在但不在 public.user_action_logs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_user_action_logs_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_user_action_logs_user ON public.user_action_logs (user_id, created_at DESC);
    END IF;
END $idxguard$;

-- @index-guard idx_user_action_logs_type ON user_action_logs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_user_action_logs_type' AND i.indrelid = to_regclass('public.user_action_logs')) THEN
        NULL;  -- 已在 public.user_action_logs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_user_action_logs_type' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_user_action_logs_type 已存在但不在 public.user_action_logs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_user_action_logs_type' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_user_action_logs_type ON public.user_action_logs (action_type, created_at DESC);
    END IF;
END $idxguard$;

-- @index-guard idx_user_action_logs_dashboard_30d ON user_action_logs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_user_action_logs_dashboard_30d' AND i.indrelid = to_regclass('public.user_action_logs')) THEN
        NULL;  -- 已在 public.user_action_logs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_user_action_logs_dashboard_30d' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_user_action_logs_dashboard_30d 已存在但不在 public.user_action_logs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_user_action_logs_dashboard_30d' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_user_action_logs_dashboard_30d ON public.user_action_logs (user_id, created_at DESC) WHERE action_type = 'visit_dashboard';
    END IF;
END $idxguard$;

-- ============================================
-- 3. feature_flags 表（灰度开关）
-- ============================================

CREATE TABLE IF NOT EXISTS feature_flags (
    key VARCHAR(100) PRIMARY KEY,
    value_json JSONB NOT NULL,
    description TEXT,
    updated_at TIMESTAMP DEFAULT NOW(),
    updated_by VARCHAR(100)
);

-- 插入 v3.2 默认灰度配置（不存在则插入）
INSERT INTO feature_flags (key, value_json, description)
VALUES
    ('v3_2_enabled_user_ids',
     '{"user_ids": []}'::jsonb,
     'v3.2 功能白名单用户 ID 列表（灰度初期使用）'),

    ('v3_2_enabled_percent',
     '{"percent": 0}'::jsonb,
     'v3.2 灰度百分比 0-100（哈希 user_id 决定）'),

    ('v3_2_new_users_only',
     '{"enabled": true}'::jsonb,
     '仅新注册用户走 v3.2 系统（老用户走 legacy）'),

    ('v3_2_force_legacy_users',
     '{"user_ids": []}'::jsonb,
     '强制走 legacy 系统的用户 ID（对老代理特殊保护）'),

    ('c_end_download_hidden',
     '{"enabled": true}'::jsonb,
     'C 端隐藏下载按钮（仅保留复制）'),

    ('trial_pass_enabled',
     '{"enabled": true, "cost_points": 260, "reward_points": 100, "duration_hours": 24}'::jsonb,
     '代理试用通行证配置')
ON CONFLICT (key) DO NOTHING;

-- ============================================
-- 4. 为现有用户初始化 user_settings 记录
-- ============================================

-- 确保每个 active 用户都有 user_settings 行（幂等）
INSERT INTO user_settings (user_id, preferred_mode)
SELECT u.id, NULL
FROM users u
LEFT JOIN user_settings us ON u.id = us.user_id
WHERE us.user_id IS NULL
  AND (u.is_active = 1 OR u.is_active IS NULL);

-- ============================================
-- 5. 验证查询（手动跑，检查迁移结果）
-- ============================================

-- 查询 1: user_settings 分布
-- SELECT preferred_mode, COUNT(*) FROM user_settings GROUP BY preferred_mode;

-- 查询 2: feature_flags 是否就位
-- SELECT key, value_json FROM feature_flags WHERE key LIKE 'v3_2%' OR key LIKE 'c_end%';

-- 查询 3: user_action_logs 表结构
-- \d user_action_logs

SELECT 'v3.2 C端重构 schema 迁移完成' AS status, NOW() AS applied_at;
