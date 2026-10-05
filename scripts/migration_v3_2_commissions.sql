-- ============================================
-- v3.2 分销体系改造 — 数据库迁移
-- 日期: 2026-04-16
-- 作者: CTO-9.0
--
-- 变更:
--   1. pending_commissions 新表（状态机 + T+3 观察期）
--   2. recharge_orders 加退款字段（3 天窗口 + refund_status）
--   3. referral_links 加反作弊字段（设备指纹 + IP + 可疑标记）
--   4. users 加注册时设备指纹 / IP（幂等，已有则跳过）
--   5. commission_version 字段区分 v3.1 legacy vs v3.2 新规则
-- ============================================

-- ============================================
-- 1. pending_commissions 表（核心新表）
-- ============================================

CREATE TABLE IF NOT EXISTS pending_commissions (
    id SERIAL PRIMARY KEY,

    -- 谁拿到佣金（代理）
    user_id INTEGER NOT NULL REFERENCES users(id),

    -- 产生佣金的源头（充值用户）
    source_user_id INTEGER NOT NULL REFERENCES users(id),

    -- 关联的充值订单（recharge_orders.id 是 TEXT）
    order_id TEXT NOT NULL,

    -- 佣金金额（元）
    amount_yuan NUMERIC(10, 2) NOT NULL,

    -- 1 = 直推 18%, 2 = 间推 3%
    level INTEGER NOT NULL CHECK (level IN (1, 2)),

    -- 记录当时的佣金率（未来调整不影响历史记录）
    commission_rate NUMERIC(5, 4) NOT NULL,

    -- 状态机:
    --   pending              待结算（观察期内）
    --   settled              已结算（T+3 后，可使用/提现）
    --   refunded_cancelled   因充值退款自动取消
    --   frozen               因反作弊冻结，待人工审核
    --   refund_clawback      已结算后被追回（代理钱包扣回）
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'settled', 'refunded_cancelled', 'frozen', 'refund_clawback')),

    -- 时间线
    created_at TIMESTAMP DEFAULT NOW(),
    available_at TIMESTAMP NOT NULL,       -- created_at + 3 自然日
    settled_at TIMESTAMP,

    -- 冻结原因（status = 'frozen' 时填写）
    frozen_reason VARCHAR(200),

    -- 审核信息
    reviewed_by VARCHAR(50),
    reviewed_at TIMESTAMP
);

-- 索引
-- @index-guard idx_pending_commissions_user ON pending_commissions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pending_commissions_user' AND i.indrelid = to_regclass('public.pending_commissions')) THEN
        NULL;  -- 已在 public.pending_commissions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pending_commissions_user' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pending_commissions_user 已存在但不在 public.pending_commissions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pending_commissions_user' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pending_commissions_user ON public.pending_commissions (user_id, status);
    END IF;
END $idxguard$;

-- @index-guard idx_pending_commissions_available ON pending_commissions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pending_commissions_available' AND i.indrelid = to_regclass('public.pending_commissions')) THEN
        NULL;  -- 已在 public.pending_commissions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pending_commissions_available' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pending_commissions_available 已存在但不在 public.pending_commissions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pending_commissions_available' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pending_commissions_available ON public.pending_commissions (available_at) WHERE status = 'pending';
    END IF;
END $idxguard$;

-- @index-guard idx_pending_commissions_order ON pending_commissions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pending_commissions_order' AND i.indrelid = to_regclass('public.pending_commissions')) THEN
        NULL;  -- 已在 public.pending_commissions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pending_commissions_order' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pending_commissions_order 已存在但不在 public.pending_commissions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pending_commissions_order' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pending_commissions_order ON public.pending_commissions (order_id);
    END IF;
END $idxguard$;

-- @index-guard idx_pending_commissions_source ON pending_commissions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pending_commissions_source' AND i.indrelid = to_regclass('public.pending_commissions')) THEN
        NULL;  -- 已在 public.pending_commissions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pending_commissions_source' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pending_commissions_source 已存在但不在 public.pending_commissions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pending_commissions_source' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pending_commissions_source ON public.pending_commissions (source_user_id);
    END IF;
END $idxguard$;

-- @index-guard idx_pending_commissions_frozen ON pending_commissions plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_pending_commissions_frozen' AND i.indrelid = to_regclass('public.pending_commissions')) THEN
        NULL;  -- 已在 public.pending_commissions 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_pending_commissions_frozen' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_pending_commissions_frozen 已存在但不在 public.pending_commissions 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_pending_commissions_frozen' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_pending_commissions_frozen ON public.pending_commissions (status, created_at DESC) WHERE status = 'frozen';
    END IF;
END $idxguard$;

-- ============================================
-- 2. recharge_orders 加退款字段
-- ============================================

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS refund_deadline TIMESTAMP;       -- created_at + 3 自然日

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS refundable BOOLEAN DEFAULT TRUE;

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS refund_status VARCHAR(20) DEFAULT NULL;
    -- NULL / pending / approved / completed / rejected / failed

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS refunded_amount_cents INTEGER DEFAULT 0;

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS refund_requested_at TIMESTAMP;

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS refund_completed_at TIMESTAMP;

ALTER TABLE recharge_orders
    ADD COLUMN IF NOT EXISTS commission_version VARCHAR(10) DEFAULT 'v3.2';
    -- v3.2 新订单走新逻辑 / legacy 老订单保留历史佣金

-- 给历史订单回填 deadline（保守：已完成的订单不可退款）
UPDATE recharge_orders
SET refundable = FALSE,
    commission_version = 'legacy'
WHERE commission_version IS NULL
  AND payment_status = 'paid';

-- @index-guard idx_recharge_orders_refund_status ON recharge_orders plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_recharge_orders_refund_status' AND i.indrelid = to_regclass('public.recharge_orders')) THEN
        NULL;  -- 已在 public.recharge_orders 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_recharge_orders_refund_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_recharge_orders_refund_status 已存在但不在 public.recharge_orders 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_recharge_orders_refund_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_recharge_orders_refund_status ON public.recharge_orders (refund_status) WHERE refund_status IS NOT NULL;
    END IF;
END $idxguard$;

-- ============================================
-- 3. referral_links 加反作弊字段
-- ============================================

ALTER TABLE referral_links
    ADD COLUMN IF NOT EXISTS bound_device_fingerprint VARCHAR(128);

ALTER TABLE referral_links
    ADD COLUMN IF NOT EXISTS bound_ip VARCHAR(45);

ALTER TABLE referral_links
    ADD COLUMN IF NOT EXISTS is_suspicious BOOLEAN DEFAULT FALSE;

ALTER TABLE referral_links
    ADD COLUMN IF NOT EXISTS suspicious_reason VARCHAR(200);

ALTER TABLE referral_links
    ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(50);

ALTER TABLE referral_links
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP;

-- @index-guard idx_referral_links_suspicious ON referral_links plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_referral_links_suspicious' AND i.indrelid = to_regclass('public.referral_links')) THEN
        NULL;  -- 已在 public.referral_links 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_referral_links_suspicious' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_referral_links_suspicious 已存在但不在 public.referral_links 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_referral_links_suspicious' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_referral_links_suspicious ON public.referral_links (is_suspicious) WHERE is_suspicious = TRUE;
    END IF;
END $idxguard$;

-- ============================================
-- 4. users 加注册设备指纹 / IP（幂等）
-- ============================================

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS register_device_fingerprint VARCHAR(128);

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS register_ip VARCHAR(45);

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS last_login_ip VARCHAR(45);

-- @index-guard idx_users_register_fingerprint ON users plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_users_register_fingerprint' AND i.indrelid = to_regclass('public.users')) THEN
        NULL;  -- 已在 public.users 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_users_register_fingerprint' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_users_register_fingerprint 已存在但不在 public.users 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_users_register_fingerprint' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_users_register_fingerprint ON public.users (register_device_fingerprint) WHERE register_device_fingerprint IS NOT NULL;
    END IF;
END $idxguard$;

-- @index-guard idx_users_register_ip ON users plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_users_register_ip' AND i.indrelid = to_regclass('public.users')) THEN
        NULL;  -- 已在 public.users 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_users_register_ip' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_users_register_ip 已存在但不在 public.users 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_users_register_ip' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_users_register_ip ON public.users (register_ip) WHERE register_ip IS NOT NULL;
    END IF;
END $idxguard$;

-- ============================================
-- 5. 新佣金率常量（注释，实际在代码中硬编码）
-- ============================================

-- v3.1 legacy: L1=12%, L2=3%
-- v3.2 new:    L1=28%, L2=5%
-- 在 api/referral_api.py 中通过 feature_flags.v3_2_enabled_user_ids
-- 白名单控制哪些用户走新费率

-- ============================================
-- 6. 验证
-- ============================================

-- 验证新表
-- \d pending_commissions

-- 验证 recharge_orders 新字段
-- SELECT column_name FROM information_schema.columns
-- WHERE table_name = 'recharge_orders' AND column_name LIKE 'refund%';

-- 验证 commission_version 分布
-- SELECT commission_version, COUNT(*) FROM recharge_orders GROUP BY commission_version;

SELECT 'v3.2 分销体系 schema 迁移完成' AS status, NOW() AS applied_at;
