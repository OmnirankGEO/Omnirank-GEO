-- 通知中心 · 两类通道的已读水位(工单 2026-07-29 T3 §3.3.3)
--
-- 为什么需要:
--   工单要求通知中心分「系统消息」与「扣费通知」两类,各自有未读数,且已读状态
--   刷新后不回退。系统消息落在 user_notifications 有 is_read 列;**扣费通知没有**
--   —— 它是 point_transactions(资金流水)的只读投影,而工单 §3.1 明令
--   「扣费通知只做展示与跳转,一行扣费逻辑都不许碰」。
--
--   所以已读状态不能写进流水表(那是资金表),只能另立一张**纯 UI 状态表**:
--   记 user × 通道的"我已经读到哪一条了"水位。未读数 = 水位之后的流水条数。
--
-- 边界:
--   · 本表与资金无关 —— 不参与任何金额计算、不进对账等式、不被扣费/退费读写;
--   · 纯 additive + 幂等,可反复连跑;
--   · 漏跑的表现是「扣费通知一直显示未读」,不是报错、更不是扣费异常
--     (读取方 best-effort:查不到水位就按"全部未读"降级,查询本身包 try)。

CREATE TABLE IF NOT EXISTS public.user_notification_read_marks (
    user_id       INTEGER   NOT NULL,
    channel       TEXT      NOT NULL,
    last_read_ref BIGINT    NOT NULL DEFAULT 0,
    updated_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, channel)
);

-- 形态断言:只核列与类型,不核数据行(新表本来就该是空的)。
DO $$
DECLARE
    missing TEXT;
BEGIN
    SELECT string_agg(expected.column_name, ', ')
      INTO missing
      FROM (VALUES
              ('user_id',       'integer'),
              ('channel',       'text'),
              ('last_read_ref', 'bigint'),
              ('updated_at',    'timestamp without time zone')
           ) AS expected(column_name, data_type)
      LEFT JOIN information_schema.columns c
             ON c.table_schema = 'public'
            AND c.table_name   = 'user_notification_read_marks'
            AND c.column_name  = expected.column_name
            AND c.data_type    = expected.data_type
     WHERE c.column_name IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'user_notification_read_marks 形态不符,缺列或类型不符: %', missing;
    END IF;
END $$;

-- 历史通知按业务单号取终态时要按 event_key 前缀扫;既有索引只有 (user_id, event_key)
-- 唯一约束(见 migration_notification_outbox_2026_07_17.sql)。这里补一条按用户+时间倒序
-- 的复合索引,让历史页翻页与终态去重的自连接都走索引而不是全表。
-- @index-guard idx_user_notifications_user_created ON user_notifications plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_user_notifications_user_created' AND i.indrelid = to_regclass('public.user_notifications')) THEN
        NULL;  -- 已在 public.user_notifications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_user_notifications_user_created' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_user_notifications_user_created 已存在但不在 public.user_notifications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_user_notifications_user_created' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_user_notifications_user_created ON public.user_notifications (user_id, created_at DESC, id DESC);
    END IF;
END $idxguard$;
