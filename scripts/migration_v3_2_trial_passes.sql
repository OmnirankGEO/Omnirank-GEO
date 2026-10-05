-- ============================================
-- v3.2 Phase 3: 代理试用通行证 + 协议签署
-- 日期: 2026-04-16
-- 作者: CTO-9.0
-- ============================================

-- ============================================
-- 1. trial_passes 表
-- ============================================

CREATE TABLE IF NOT EXISTS trial_passes (
    id SERIAL PRIMARY KEY,
    recipient_user_id INTEGER NOT NULL REFERENCES users(id),
    issuer_user_id INTEGER NOT NULL REFERENCES users(id),
    cost_points INTEGER NOT NULL DEFAULT 260,
    issuer_reward_points INTEGER DEFAULT 100,
    platform_keep_points INTEGER DEFAULT 160,
    points_held BOOLEAN DEFAULT FALSE,
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'active', 'expired', 'cancelled')),
    applied_at TIMESTAMP DEFAULT NOW(),
    reviewed_at TIMESTAMP,
    activated_at TIMESTAMP,
    expires_at TIMESTAMP,
    reject_reason TEXT
);

-- @index-guard idx_trial_passes_recipient ON trial_passes plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_trial_passes_recipient' AND i.indrelid = to_regclass('public.trial_passes')) THEN
        NULL;  -- 已在 public.trial_passes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_trial_passes_recipient' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_trial_passes_recipient 已存在但不在 public.trial_passes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_trial_passes_recipient' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_trial_passes_recipient ON public.trial_passes (recipient_user_id, status);
    END IF;
END $idxguard$;

-- @index-guard idx_trial_passes_issuer_pending ON trial_passes plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_trial_passes_issuer_pending' AND i.indrelid = to_regclass('public.trial_passes')) THEN
        NULL;  -- 已在 public.trial_passes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_trial_passes_issuer_pending' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_trial_passes_issuer_pending 已存在但不在 public.trial_passes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_trial_passes_issuer_pending' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_trial_passes_issuer_pending ON public.trial_passes (issuer_user_id, status) WHERE status = 'pending';
    END IF;
END $idxguard$;

-- @index-guard idx_trial_passes_expires ON trial_passes plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_trial_passes_expires' AND i.indrelid = to_regclass('public.trial_passes')) THEN
        NULL;  -- 已在 public.trial_passes 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_trial_passes_expires' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_trial_passes_expires 已存在但不在 public.trial_passes 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_trial_passes_expires' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_trial_passes_expires ON public.trial_passes (expires_at) WHERE status = 'active';
    END IF;
END $idxguard$;

-- ============================================
-- 2. agreement_signatures 表（协议签署记录）
-- ============================================

CREATE TABLE IF NOT EXISTS agreement_signatures (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    agreement_type VARCHAR(50) NOT NULL,
        -- 'user_terms' / 'privacy' / 'agent_contract'
    agreement_version VARCHAR(20) NOT NULL,
    signed_at TIMESTAMP DEFAULT NOW(),
    ip_address VARCHAR(45),
    user_agent TEXT
);

-- @index-guard idx_agreement_signatures_user ON agreement_signatures plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_agreement_signatures_user' AND i.indrelid = to_regclass('public.agreement_signatures')) THEN
        NULL;  -- 已在 public.agreement_signatures 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_agreement_signatures_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_agreement_signatures_user 已存在但不在 public.agreement_signatures 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_agreement_signatures_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_agreement_signatures_user ON public.agreement_signatures (user_id, agreement_type);
    END IF;
END $idxguard$;

-- @index-guard idx_agreement_signatures_unique ON agreement_signatures unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_agreement_signatures_unique' AND i.indrelid = to_regclass('public.agreement_signatures')) THEN
        NULL;  -- 已在 public.agreement_signatures 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_agreement_signatures_unique' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_agreement_signatures_unique 已存在但不在 public.agreement_signatures 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_agreement_signatures_unique' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX idx_agreement_signatures_unique ON public.agreement_signatures (user_id, agreement_type, agreement_version);
    END IF;
END $idxguard$;

-- ============================================
-- 3. feature_pricing 增加 geo_plan_unlock 和 trial_pass_apply
-- ============================================

INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, is_active)
VALUES
    ('geo_plan_unlock', '解锁完整 GEO 方案', 260, 2.0, TRUE),
    ('trial_pass_apply', '申请代理工作台 1 天试用', 260, 2.0, TRUE)
ON CONFLICT (feature_code) DO NOTHING;

SELECT 'v3.2 Phase 3 schema 迁移完成' AS status, NOW() AS applied_at;
