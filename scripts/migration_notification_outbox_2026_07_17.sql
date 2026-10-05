-- 全站关键业务通知 outbox（additive / prestart-only / idempotent）
-- 业务终态与本表同事务写入；cron leader 异步 exactly-once 派发到 user_notifications。

ALTER TABLE user_notifications
    ADD COLUMN IF NOT EXISTS level VARCHAR(20) NOT NULL DEFAULT 'light';
ALTER TABLE user_notifications
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE user_notifications
    ADD COLUMN IF NOT EXISTS event_key TEXT NULL;

-- @index-guard uq_user_notifications_user_event ON user_notifications unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_user_notifications_user_event' AND i.indrelid = to_regclass('user_notifications')) THEN
        NULL;  -- 已在 user_notifications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_user_notifications_user_event' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('user_notifications'))) THEN
        RAISE EXCEPTION '[index-guard] uq_user_notifications_user_event 已存在但不在 user_notifications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_user_notifications_user_event' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('user_notifications')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_user_notifications_user_event ON user_notifications (user_id, event_key) WHERE event_key IS NOT NULL;
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS notification_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    business_id TEXT NOT NULL,
    terminal_state TEXT NOT NULL,
    -- 不设 users 外键：历史任务可能指向已注销账号。此时业务终态仍须提交，
    -- 派发失败留在 outbox 重试/告警，不能由通知外键反向回滚真实业务。
    recipient_user_id INTEGER NOT NULL,
    recipient_kind TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('important','gentle','light','silent')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    route TEXT NOT NULL,
    privacy_policy TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','delivered','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    claimed_at TIMESTAMPTZ,
    claim_token TEXT,
    last_error TEXT,
    delivered_notification_id BIGINT,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- 本迁移尚未部署，但测试/候选环境可能已运行过旧草案；重复执行时收敛到
-- fail-durable 约束，避免历史孤儿接收者阻断业务终态。
ALTER TABLE notification_outbox
    DROP CONSTRAINT IF EXISTS notification_outbox_recipient_user_id_fkey;

-- @index-guard idx_notification_outbox_dispatch ON notification_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_notification_outbox_dispatch' AND i.indrelid = to_regclass('notification_outbox')) THEN
        NULL;  -- 已在 notification_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_notification_outbox_dispatch' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('notification_outbox'))) THEN
        RAISE EXCEPTION '[index-guard] idx_notification_outbox_dispatch 已存在但不在 notification_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_notification_outbox_dispatch' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('notification_outbox')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_notification_outbox_dispatch ON notification_outbox (status, available_at, id) WHERE status IN ('pending','processing');
    END IF;
END $idxguard$;
-- @index-guard idx_notification_outbox_recipient ON notification_outbox plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_notification_outbox_recipient' AND i.indrelid = to_regclass('notification_outbox')) THEN
        NULL;  -- 已在 notification_outbox 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_notification_outbox_recipient' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('notification_outbox'))) THEN
        RAISE EXCEPTION '[index-guard] idx_notification_outbox_recipient 已存在但不在 notification_outbox 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_notification_outbox_recipient' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('notification_outbox')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_notification_outbox_recipient ON notification_outbox (recipient_user_id, created_at DESC);
    END IF;
END $idxguard$;
