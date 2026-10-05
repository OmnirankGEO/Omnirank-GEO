-- ── 049 · 结算 AI 审查员裁定记录(WO_SETTLEMENT_AI_ADJUDICATOR_2026-08-25 · 迁移号 Review 2026-08-25 登记)──
--
-- 🔴 为什么不复用既有 `diagnosis_settlement_audit`(Review 已采纳此理由):
--    不是"没有留痕表",是**够不着**——
--    ① 写入侧 `_write_settlement_audit` 把 detail 硬截 2000 字符,证据包(冻结拆分快照 +
--       provider 计数 + 产物在库结论 + 钱包冻结行 + charge link 状态)装不下;
--    ② 没有结构化列 ⇒ 升级触发三之「同一失败原因连续 ≥N 单」只能 LIKE 匹配自由文本,
--       是会静默失效的那种判据;这里把 failure_cause 提成**独立列 + 索引**;
--    ③ 没有 rule_version / frozen_points ⇒「退款争议以本表为准」时答不出
--       "这单是哪版规则判的、当时冻了多少"。
--    既有 audit 表**不退役**:审查员每落一行本表,同事务照写一行 audit
--    (两轨要么都在要么都不在 · Review 2026-08-25 硬要求),admin 既有审计端点照旧看得见。
--
-- 🔴 append-only:只 INSERT,不 UPDATE / 不 DELETE。一单可有多行(见 phase),
--    因为"裁定"与"执行结果"发生在两个不同事务里,把结果 UPDATE 回同一行就不叫不可变记录了。
--
-- 🔴 顺序无依赖:不建 FK(run_token 上不挂外键 —— diagnosis_runs 行被清理时审计轨必须留存),
--    不碰任何既有表,**零 DML**。
--
-- 🔴 漏跑后果**响亮**(刻意):审查员每条路径都先写本表再动状态机,表不在 → UndefinedTable 当场抛,
--    tick 记异常并跳过该单,**不会**降级成"没留痕就把钱结了"。
--
-- 🔴 重放安全:prestart 每次部署无条件重放全部迁移(无追踪表)。
--    CREATE TABLE / CREATE INDEX 全部 IF NOT EXISTS;CHECK 走 pg_constraint 存在性判断。
--    索引名带表名前缀,避免 `CREATE INDEX IF NOT EXISTS` 只按 schema 关系名判存(不绑表)导致撞名。

CREATE TABLE IF NOT EXISTS diagnosis_settlement_adjudications (
    id               BIGSERIAL PRIMARY KEY,
    run_token        TEXT NOT NULL,
    -- decided=已裁定待执行 · executed=执行成功 · execution_failed=执行未成(钱没动)
    -- · escalated=转人工升级(不碰状态机 · 零资金变动)
    phase            TEXT NOT NULL,
    -- commit=交付(冻结转消费 · 比例由 sweeper 的 _partial_commit_points 算)
    -- · release=全额退 · escalate=不动钱
    decision         TEXT NOT NULL,
    -- 仅 decision='escalate' 时非空:over_limit | evidence_incomplete | same_cause_streak
    escalation_code  TEXT,
    -- 机械归一化的失败原因。**独立列**就是为了让「同因连续 ≥N 单」是一条可索引查询,
    -- 而不是对自由文本做 LIKE。
    failure_cause    TEXT,
    -- 裁定当时的冻结额(限额门控依据 · commit/release 两向都按它门控)。
    frozen_points    BIGINT,
    -- 规则版本:退款争议要能答"这单是哪版规则判的"。
    rule_version     TEXT NOT NULL,
    -- 证据包全文(冻结拆分快照 / provider 计数 / 产物在库 / 钱包冻结行 / charge link 状态)。
    evidence_jsonb   JSONB NOT NULL,
    -- 执行结果原文(phase='executed'/'execution_failed' 行才有)。
    outcome          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 闭集 CHECK 用 DO 块补挂(重放安全:已存在则跳过)。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_diag_adjudication_phase'
           AND conrelid = 'public.diagnosis_settlement_adjudications'::regclass
    ) THEN
        ALTER TABLE diagnosis_settlement_adjudications
            ADD CONSTRAINT ck_diag_adjudication_phase
            CHECK (phase IN ('decided', 'executed', 'execution_failed', 'escalated'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_diag_adjudication_decision'
           AND conrelid = 'public.diagnosis_settlement_adjudications'::regclass
    ) THEN
        ALTER TABLE diagnosis_settlement_adjudications
            ADD CONSTRAINT ck_diag_adjudication_decision
            CHECK (decision IN ('commit', 'release', 'escalate'));
    END IF;

    -- escalate 必须带 escalation_code;非 escalate 必须不带 —— 防"升级了但说不出为什么"
    -- 与"自动处置却挂了个升级码"两个方向。
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_diag_adjudication_escalation_code'
           AND conrelid = 'public.diagnosis_settlement_adjudications'::regclass
    ) THEN
        ALTER TABLE diagnosis_settlement_adjudications
            ADD CONSTRAINT ck_diag_adjudication_escalation_code
            CHECK (
                (decision = 'escalate' AND escalation_code IS NOT NULL)
                OR (decision <> 'escalate' AND escalation_code IS NULL)
            );
    END IF;
END $$;

-- @index-guard idx_diag_adjudication_run ON diagnosis_settlement_adjudications plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_diag_adjudication_run' AND i.indrelid = to_regclass('public.diagnosis_settlement_adjudications')) THEN
        NULL;  -- 已在 public.diagnosis_settlement_adjudications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_diag_adjudication_run' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_diag_adjudication_run 已存在但不在 public.diagnosis_settlement_adjudications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_diag_adjudication_run' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_diag_adjudication_run ON public.diagnosis_settlement_adjudications (run_token, created_at DESC);
    END IF;
END $idxguard$;

-- 「同一失败原因连续 ≥N 单」的支撑索引。
-- @index-guard idx_diag_adjudication_cause ON diagnosis_settlement_adjudications plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_diag_adjudication_cause' AND i.indrelid = to_regclass('public.diagnosis_settlement_adjudications')) THEN
        NULL;  -- 已在 public.diagnosis_settlement_adjudications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_diag_adjudication_cause' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_diag_adjudication_cause 已存在但不在 public.diagnosis_settlement_adjudications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_diag_adjudication_cause' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_diag_adjudication_cause ON public.diagnosis_settlement_adjudications (failure_cause, created_at DESC) WHERE failure_cause IS NOT NULL;
    END IF;
END $idxguard$;

-- 用户侧闭环:「自动处置完成 → 发一条『核实完成,结果已更新』」需要按 run 找已执行行,
-- 且**只找一次**(是否已发由 notification_outbox 的唯一 event_key 兜底,不在本表加可变列)。
-- @index-guard idx_diag_adjudication_executed ON diagnosis_settlement_adjudications plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_diag_adjudication_executed' AND i.indrelid = to_regclass('public.diagnosis_settlement_adjudications')) THEN
        NULL;  -- 已在 public.diagnosis_settlement_adjudications 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_diag_adjudication_executed' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_diag_adjudication_executed 已存在但不在 public.diagnosis_settlement_adjudications 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_diag_adjudication_executed' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_diag_adjudication_executed ON public.diagnosis_settlement_adjudications (created_at DESC) WHERE phase = 'executed';
    END IF;
END $idxguard$;
