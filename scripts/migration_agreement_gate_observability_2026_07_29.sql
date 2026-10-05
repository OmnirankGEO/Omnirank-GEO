-- 协议门禁触发的最小可观测(工单 2026-07-29 §4.1)
--
-- 为什么需要:2026-07-26 起,缺协议账号登录被 428 拦下后前端识别失败,用户永久锁死在
-- 登录页。这个状态持续了 3 天,**全站零信号** —— 428 只落在 nginx access log 里,
-- 没有任何人会去翻。既有告警体系里也没有"我正在把人挡在门外"这一类。
--
-- 本表只记"门禁触发了"这一个事实,不记凭据、不记密码、不记 IP 明文之外的任何东西。
-- 纯 additive + 幂等;漏跑的表现是"计数为空"而不是登录失败(记录器全程 best-effort)。

CREATE TABLE IF NOT EXISTS registration_agreement_gate_events (
    id           BIGSERIAL PRIMARY KEY,
    user_id      INTEGER     NOT NULL,
    auth_method  TEXT        NOT NULL,
    occurred_at  TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- @index-guard idx_agreement_gate_events_occurred ON registration_agreement_gate_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_agreement_gate_events_occurred' AND i.indrelid = to_regclass('registration_agreement_gate_events')) THEN
        NULL;  -- 已在 registration_agreement_gate_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_agreement_gate_events_occurred' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('registration_agreement_gate_events'))) THEN
        RAISE EXCEPTION '[index-guard] idx_agreement_gate_events_occurred 已存在但不在 registration_agreement_gate_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_agreement_gate_events_occurred' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('registration_agreement_gate_events')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_agreement_gate_events_occurred ON registration_agreement_gate_events (occurred_at DESC);
    END IF;
END $idxguard$;

-- @index-guard idx_agreement_gate_events_user ON registration_agreement_gate_events plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_agreement_gate_events_user' AND i.indrelid = to_regclass('registration_agreement_gate_events')) THEN
        NULL;  -- 已在 registration_agreement_gate_events 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_agreement_gate_events_user' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('registration_agreement_gate_events'))) THEN
        RAISE EXCEPTION '[index-guard] idx_agreement_gate_events_user 已存在但不在 registration_agreement_gate_events 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_agreement_gate_events_user' AND c.relnamespace = (SELECT relnamespace FROM pg_class WHERE oid = to_regclass('registration_agreement_gate_events')))
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_agreement_gate_events_user ON registration_agreement_gate_events (user_id, occurred_at DESC);
    END IF;
END $idxguard$;

-- 形态断言:只核列与类型,**不核数据行** ——
-- 断言数据会让"这天恰好没人触发门禁"变成部署失败(C-6 教训)。
DO $$
DECLARE
    missing TEXT;
BEGIN
    SELECT string_agg(expected.column_name, ', ')
      INTO missing
      FROM (VALUES
              ('user_id',     'integer'),
              ('auth_method', 'text'),
              ('occurred_at', 'timestamp without time zone')
           ) AS expected(column_name, data_type)
      LEFT JOIN information_schema.columns c
             ON c.table_schema = current_schema()
            AND c.table_name   = 'registration_agreement_gate_events'
            AND c.column_name  = expected.column_name
            AND c.data_type    = expected.data_type
     WHERE c.column_name IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'registration_agreement_gate_events 形态不符,缺列或类型不符: %', missing;
    END IF;
END $$;
