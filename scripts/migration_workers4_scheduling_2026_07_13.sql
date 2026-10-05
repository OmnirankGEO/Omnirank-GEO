-- ============================================================================
-- WORKERS=4 · B2 调度"至多一次"三表(SPEC §2.2 / §7)
-- 全部新表 · CREATE IF NOT EXISTS 幂等 · 2× 复跑零报错 · 无破坏性操作
-- 正确性在 DB(UNIQUE 键 + 终态 CAS),Redis leader/epoch 只是优化(§2.2)
-- 部署:db-before-code · 由 ROLE=prestart 单飞跑(D4);WORKERS=1 兼容期也可由启动自检跑
-- ============================================================================

-- ── sched_job_runs:DB 写类 job 的"恰一次 claim"(SPEC §2.2)──────────────────
-- 同一 (job_name, scheduled_at) 双实例争抢 → UNIQUE 只允许一个 claim 成功
-- 短事务 claim(INSERT ON CONFLICT DO NOTHING → status='claimed')→ 提交;
-- 执行体在事务外跑,每 30s 续 claim_heartbeat_at;完成短事务标 done。
-- 心跳死(90s)才可被回收重试;重复执行由各 job 自己的业务幂等键兜底。
CREATE TABLE IF NOT EXISTS sched_job_runs (
    id                 BIGSERIAL PRIMARY KEY,
    job_name           TEXT        NOT NULL,
    scheduled_at       TIMESTAMPTZ NOT NULL,          -- cron 触发时刻(按 job 粒度分桶)· 与 job_name 组成 dedup 键
    epoch              BIGINT,                         -- 抢到 claim 的 leader 任期(sched:epoch)
    claim_token        TEXT,                           -- [FF3] claim 持有者身份 · heartbeat/finish CAS 只认自己 · stale reclaim 换 token
    status             TEXT        NOT NULL DEFAULT 'claimed'
                       CHECK (status IN ('claimed','done','failed')),
    claim_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result             JSONB,
    last_error         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at        TIMESTAMPTZ,
    CONSTRAINT uq_sched_job_runs UNIQUE (job_name, scheduled_at)
);
-- 已有表升级(新装 no-op):[FF3] claim_token 列
ALTER TABLE sched_job_runs ADD COLUMN IF NOT EXISTS claim_token TEXT;
-- @index-guard idx_sched_job_runs_hb ON sched_job_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_sched_job_runs_hb' AND i.indrelid = to_regclass('public.sched_job_runs')) THEN
        NULL;  -- 已在 public.sched_job_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_sched_job_runs_hb' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_sched_job_runs_hb 已存在但不在 public.sched_job_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_sched_job_runs_hb' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_sched_job_runs_hb ON public.sched_job_runs (status, claim_heartbeat_at);
    END IF;
END $idxguard$;

-- ── sched_commands:admin 手动触发命令队列(SPEC §7 · 可恢复)────────────────
-- web 端点 INSERT ON CONFLICT (command_id) DO NOTHING;leader cron 原子 claim
-- (FOR UPDATE SKIP LOCKED);执行完写 done/failed + result;web 轮询回读。
-- 回收:执行中每 30s 续 claim_heartbeat_at;status='claimed' 且心跳死(90s)才回收
-- (合法长任务不误抢)+ 各 job 可配执行超时。
CREATE TABLE IF NOT EXISTS sched_commands (
    id                 BIGSERIAL PRIMARY KEY,
    command_id         TEXT        NOT NULL UNIQUE,      -- = 前端稳定 request_id · 入队幂等键(双击去重)
    job_name           TEXT        NOT NULL,
    args               JSONB,
    status             TEXT        NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','claimed','done','failed')),
    requested_by       INTEGER,
    claim_token        TEXT,                              -- [FF4] 领取者身份 · finish CAS 只认自己 · stale reclaim 换 token
    claimed_at         TIMESTAMPTZ,
    claimed_epoch      BIGINT,
    claim_heartbeat_at TIMESTAMPTZ,
    result             JSONB,
    last_error         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- 已有表升级(新装 no-op):[FF4] claim_token 列 · [FF14 P1-5] 受控恢复审计列(operator/理由证据)
ALTER TABLE sched_commands ADD COLUMN IF NOT EXISTS claim_token TEXT;
ALTER TABLE sched_commands ADD COLUMN IF NOT EXISTS resolved_by     TEXT;   -- 受控恢复操作人
ALTER TABLE sched_commands ADD COLUMN IF NOT EXISTS resolution_note TEXT;   -- 核对的业务结果/理由/证据
ALTER TABLE sched_commands ADD COLUMN IF NOT EXISTS resolved_at     TIMESTAMPTZ;
-- @index-guard idx_sched_commands_status ON sched_commands plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_sched_commands_status' AND i.indrelid = to_regclass('public.sched_commands')) THEN
        NULL;  -- 已在 public.sched_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_sched_commands_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_sched_commands_status 已存在但不在 public.sched_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_sched_commands_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_sched_commands_status ON public.sched_commands (status, created_at);
    END IF;
END $idxguard$;
-- @index-guard idx_sched_commands_reclaim ON sched_commands plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_sched_commands_reclaim' AND i.indrelid = to_regclass('public.sched_commands')) THEN
        NULL;  -- 已在 public.sched_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_sched_commands_reclaim' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_sched_commands_reclaim 已存在但不在 public.sched_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_sched_commands_reclaim' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_sched_commands_reclaim ON public.sched_commands (status, claim_heartbeat_at);
    END IF;
END $idxguard$;

-- ── [FF10/FF14/FF15 P0-1/P1-5/P1-2] status CHECK = 'outcome_unknown'(死 claim/命令 = **业务结果未知**态)────────
-- 语义(FF14 改正):**不是**"retry_pending 可随手重跑"——旧执行体可能只是丢心跳仍在产生副作用,盲目
-- 重跑=人工双跑。故为 `outcome_unknown`:先核对业务结果,再由受控操作(resolve_unknown_command)决定。
-- [FF15 P1-2 修真升级路径]:**旧版曾发过 FF13 的 `retry_pending` 具名 CHECK + 存量行**。原逻辑排除已有
-- 具名约束 → FF13→FF14 升级时旧 CHECK 不被替换、存量 retry_pending 不迁移 → 首次写 outcome_unknown CheckViolation。
-- 修:①先删**任何缺 outcome_unknown 的 status CHECK**(含 FF13 具名 retry_pending + 更旧 auto-named);
--    ②无约束挡时迁存量数据 retry_pending→outcome_unknown;③再建正确具名 CHECK。顺序:删→迁数据→建。
-- 2× 复跑安全:第二遍正确 CHECK 含 outcome_unknown 不被删、无 retry_pending 数据、ADD 跳过。
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
              WHERE conrelid='sched_job_runs'::regclass AND contype='c'
                AND pg_get_constraintdef(oid) ILIKE '%status%'
                AND pg_get_constraintdef(oid) NOT ILIKE '%outcome_unknown%'   -- 缺目标值的旧 CHECK 全删(含具名 retry_pending)
    LOOP
        EXECUTE format('ALTER TABLE sched_job_runs DROP CONSTRAINT %I', r.conname);
    END LOOP;
    UPDATE sched_job_runs SET status='outcome_unknown' WHERE status='retry_pending';   -- 迁存量(约束已删,不会挡)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_sched_job_runs_status' AND conrelid='sched_job_runs'::regclass) THEN
        ALTER TABLE sched_job_runs ADD CONSTRAINT chk_sched_job_runs_status
            CHECK (status IN ('claimed','done','failed','outcome_unknown'));
    END IF;
END $$;

DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT conname FROM pg_constraint
              WHERE conrelid='sched_commands'::regclass AND contype='c'
                AND pg_get_constraintdef(oid) ILIKE '%status%'
                AND pg_get_constraintdef(oid) NOT ILIKE '%outcome_unknown%'
    LOOP
        EXECUTE format('ALTER TABLE sched_commands DROP CONSTRAINT %I', r.conname);
    END LOOP;
    UPDATE sched_commands SET status='outcome_unknown' WHERE status='retry_pending';
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_sched_commands_status' AND conrelid='sched_commands'::regclass) THEN
        ALTER TABLE sched_commands ADD CONSTRAINT chk_sched_commands_status
            CHECK (status IN ('pending','claimed','done','failed','outcome_unknown'));
    END IF;
END $$;

-- ── outbox_messages:外部消息 transactional outbox(SPEC §2.2)────────────────
-- 业务事务内建消息(message_key UNIQUE 防重复"创建");leader 消费发送。
-- 语义如实(§2.2 v1.3.1):outbox 只防"重复创建消息";"外部发送成功→标 sent 前崩溃"
-- 必然重发 → 仅供应商支持幂等键才可宣称至多一次;短信/邮件等无幂等键供应商归 at-least-once。
CREATE TABLE IF NOT EXISTS outbox_messages (
    id              BIGSERIAL PRIMARY KEY,
    message_key     TEXT        NOT NULL UNIQUE,
    kind            TEXT        NOT NULL,              -- sms / email / wechat / webhook
    payload         JSONB       NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','sent','failed')),
    attempts        INTEGER     NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_attempt_at TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ
);
-- @index-guard idx_outbox_pending ON outbox_messages plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_outbox_pending' AND i.indrelid = to_regclass('public.outbox_messages')) THEN
        NULL;  -- 已在 public.outbox_messages 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_outbox_pending' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_outbox_pending 已存在但不在 public.outbox_messages 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_outbox_pending' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_outbox_pending ON public.outbox_messages (status, next_attempt_at) WHERE status = 'pending';
    END IF;
END $idxguard$;

-- ── 反查断言(启动自检 / 手动 dry-run 后核实三表建成)──────────────────────
-- SELECT COUNT(*) FROM information_schema.tables
--  WHERE table_name IN ('sched_job_runs','sched_commands','outbox_messages');   -- 期望 3
