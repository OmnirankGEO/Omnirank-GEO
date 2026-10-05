-- ============================================================================
-- 诊断资金状态机 diagnosis_runs + 结果可见性列(WORKERS=4 · SPEC §3.2/§3.3a/§3.6/§3.6a)
-- 新装:CREATE TABLE 全列/约束内联;已有表:独立幂等补块(ADD COLUMN/CREATE INDEX/DO 预检)。
-- 2× 复跑零报错。正确性:终态只 billing 确认后写(R1);chk_freeze_handle schema 级强制
-- FreezeHandle 不变量(paid 进 running/commit_pending/release_pending 前必持 freeze_id+backend)。
-- 本批仅建表/加列(additive · 零 billing 触碰);状态机/R0/HC1 入口/sweeper 由 server.py 接线。
-- ============================================================================

-- ── diagnosis_runs:诊断 run 生命周期 + 资金结算态(§3.2)──────────────────────
CREATE TABLE IF NOT EXISTS diagnosis_runs (
    run_token             TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    owner_user_id         INTEGER NOT NULL,                 -- ★ 结算路由必需 · 与 freeze 同 user_id
    brand_id              INTEGER,
    client_request_id     TEXT NOT NULL,                    -- §3.3a HC1 请求级幂等(新装内联 NOT NULL)
    billing_mode          TEXT NOT NULL DEFAULT 'paid'
                          CONSTRAINT chk_diag_runs_billing_mode CHECK (billing_mode IN ('paid','exempt')),
    freeze_task_ref       TEXT NOT NULL,                    -- 'diag_'||run_token
    freeze_id             BIGINT,                           -- R0 付费成功后回填
    freeze_backend        TEXT
                          CONSTRAINT chk_diag_runs_freeze_backend CHECK (freeze_backend IS NULL OR freeze_backend IN ('legacy','v35')),  -- exempt/未回填=NULL
    run_status            TEXT NOT NULL DEFAULT 'pending_freeze'
                          CONSTRAINT chk_diag_runs_status CHECK (run_status IN (
                              'pending_freeze','running','commit_pending','committed',
                              'release_pending','released','cancelled','cancelled_no_freeze',
                              'completed_exempt','failed_exempt','settlement_manual',
                              'delivery_repair_pending',  -- [返工2 P0-2] 钱 committed 但产物 0 行(被并发删)→ 非成功终态
                              'manual_resolving')),       -- [返工3 P0] 双表人工处置 claim 已持久意图(退款前原子占用 · 非终态)
    reaped_reason         TEXT,
    settlement_attempts   INTEGER NOT NULL DEFAULT 0,
    last_settlement_error TEXT,
    status_changed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- 每次状态迁移必更(重试窗口依据)
    next_settlement_at    TIMESTAMPTZ,                         -- 退避 2→5→15min
    settled_at            TIMESTAMPTZ,
    verify_empty_count    INTEGER NOT NULL DEFAULT 0,          -- §3.0 未知冻结延迟确认:连续查空次数
    last_verify_at        TIMESTAMPTZ,                         -- 上次查空时刻(两次≥30s 才计连续)
    final_snapshot_jsonb  JSONB,                               -- §3.6a 终态 canonical 快照(reconciler 回补源)
    manual_resolution     TEXT,                                -- [返工3 P0] 双表人工处置 claim 持久意图 JSON(manual_resolving 态占用 · 崩溃续跑源)
    manual_resolution_token TEXT,                              -- [返工4 P0] 处置租约 token(只有持此 token 者能动钱/回退/推进)
    manual_lease_until    TIMESTAMPTZ,                         -- [返工4 P0] 租约到期时刻(有效租约内不并发 · 过期 sweeper 接管)
    heartbeat_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at           TIMESTAMPTZ,
    CONSTRAINT chk_freeze_handle CHECK (
        NOT (billing_mode='paid' AND run_status IN ('running','commit_pending','release_pending'))
        OR (freeze_id IS NOT NULL AND freeze_backend IS NOT NULL)
    )
);
-- 注:idx_diag_runs_status_hb / idx_diag_runs_next_settle 引用 run_status/heartbeat_at/next_settlement_at,
--   已下移到"全部 ADD COLUMN 之后"建(FF13:部分表这些列尚未补齐时建索引会 `column does not exist` 失败)。

-- ── 已有表升级双通道(新装上均 no-op)────────────────────────────────────────
-- 新列 ADD COLUMN IF NOT EXISTS(顺序无关 · 已内联于 CREATE 则 no-op)
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS verify_empty_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS last_verify_at        TIMESTAMPTZ;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS final_snapshot_jsonb  JSONB;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS next_settlement_at    TIMESTAMPTZ;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS settlement_attempts   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS last_settlement_error TEXT;

-- run_token = PRIMARY KEY 核心列(FF14 双通道):空部分表补为 PK;列在但无 PK 约束(残表)→ 补 PK;
--   含数据缺列 → fail-closed(无法自动补 PK)。防"有 run_token 列但无 PRIMARY KEY 的残表"蒙混过反查。
DO $$
DECLARE row_cnt bigint;
BEGIN
    IF to_regclass('diagnosis_runs') IS NULL THEN RETURN; END IF;
    SELECT count(*) INTO row_cnt FROM diagnosis_runs;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='diagnosis_runs' AND column_name='run_token') THEN
        IF row_cnt = 0 THEN
            ALTER TABLE diagnosis_runs ADD COLUMN run_token TEXT PRIMARY KEY;
            RAISE NOTICE '[diagnosis_runs 迁移] 空部分表已补 run_token PRIMARY KEY';
        ELSE
            RAISE EXCEPTION '[迁移 fail-closed] diagnosis_runs 有 % 行但缺 PK 列 run_token · 无法自动补 · '
                '备份后人工 ADD COLUMN run_token TEXT → 回填唯一非空值 → ADD PRIMARY KEY(run_token) → 重跑', row_cnt;
        END IF;
    ELSIF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='diagnosis_runs'::regclass AND contype='p') THEN
        -- run_token 列在但无 PRIMARY KEY(残表)→ 补 PK(值须唯一非空;否则 ADD 报错=fail-closed 暴露)
        ALTER TABLE diagnosis_runs ADD CONSTRAINT diagnosis_runs_pkey PRIMARY KEY (run_token);
        RAISE NOTICE '[diagnosis_runs 迁移] 已为 run_token 补 PRIMARY KEY 约束';
    END IF;
END $$;

-- session_id / owner_user_id / freeze_task_ref = NOT NULL 无默认核心列(FF13 双通道):
--   · 已有**空**部分表 → 直接 ADD NOT NULL(0 行不违反),自愈升级;
--   · 已有**含数据**部分表且缺这些列 → 无默认且无法从历史推断值 → **fail-closed 输出精确人工订正清单**并中止。
DO $$
DECLARE
    row_cnt bigint;
    missing_nn text := '';
    c text;
    nn_cols text[] := ARRAY['session_id','owner_user_id','freeze_task_ref'];
BEGIN
    IF to_regclass('diagnosis_runs') IS NULL THEN
        RETURN;  -- 表还没建(CREATE 在前已建 · 防御)
    END IF;
    SELECT count(*) INTO row_cnt FROM diagnosis_runs;
    FOREACH c IN ARRAY nn_cols LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='diagnosis_runs' AND column_name=c) THEN
            missing_nn := missing_nn || ' ' || c;
        END IF;
    END LOOP;
    IF missing_nn = '' THEN
        NULL;  -- 全在(新装/已升级)
    ELSIF row_cnt = 0 THEN
        -- 空部分表:逐列 ADD NOT NULL(0 行安全)· 类型与 CREATE 内联一致
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='diagnosis_runs' AND column_name='session_id') THEN
            ALTER TABLE diagnosis_runs ADD COLUMN session_id TEXT NOT NULL;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='diagnosis_runs' AND column_name='owner_user_id') THEN
            ALTER TABLE diagnosis_runs ADD COLUMN owner_user_id INTEGER NOT NULL;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='diagnosis_runs' AND column_name='freeze_task_ref') THEN
            ALTER TABLE diagnosis_runs ADD COLUMN freeze_task_ref TEXT NOT NULL;
        END IF;
        RAISE NOTICE '[diagnosis_runs 迁移] 空部分表已补 NOT NULL 核心列:%', missing_nn;
    ELSE
        RAISE EXCEPTION '[迁移 fail-closed] diagnosis_runs 有 % 行数据但缺 NOT NULL 核心列:% · 无默认且无法从历史推断'
            '(session_id/owner_user_id=结算路由必需 · freeze_task_ref=diag_||run_token 资金句柄)· 禁止自动猜测。'
            '人工订正:1)备份 pg_dump -t diagnosis_runs;2)对每列 ADD COLUMN <col> <type> → UPDATE 回填正确值 → '
            'ALTER COLUMN <col> SET NOT NULL;3)重跑本迁移', row_cnt, missing_nn;
    END IF;
END $$;

-- 其余核心列(可空 / 有默认)的已有表升级通道(新装 no-op)
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS brand_id        INTEGER;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS billing_mode    TEXT NOT NULL DEFAULT 'paid';
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS freeze_id       BIGINT;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS freeze_backend  TEXT;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS run_status      TEXT NOT NULL DEFAULT 'pending_freeze';
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS reaped_reason   TEXT;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS settled_at      TIMESTAMPTZ;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS heartbeat_at    TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS finished_at     TIMESTAMPTZ;
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS manual_resolution TEXT;   -- [返工3 P0] 双表人工处置 claim 持久意图 JSON
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS manual_resolution_token TEXT;      -- [返工4 P0] 处置租约 token
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS manual_lease_until TIMESTAMPTZ;    -- [返工4 P0] 租约到期时刻

-- 非唯一索引(引用列均已在上方补齐 · FF13 下移至此,部分表自愈后建才不 column-does-not-exist)
-- @index-guard idx_diag_runs_status_hb ON diagnosis_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_diag_runs_status_hb' AND i.indrelid = to_regclass('public.diagnosis_runs')) THEN
        NULL;  -- 已在 public.diagnosis_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_diag_runs_status_hb' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_diag_runs_status_hb 已存在但不在 public.diagnosis_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_diag_runs_status_hb' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_diag_runs_status_hb ON public.diagnosis_runs (run_status, heartbeat_at);
    END IF;
END $idxguard$;
-- @index-guard idx_diag_runs_next_settle ON diagnosis_runs plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_diag_runs_next_settle' AND i.indrelid = to_regclass('public.diagnosis_runs')) THEN
        NULL;  -- 已在 public.diagnosis_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_diag_runs_next_settle' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_diag_runs_next_settle 已存在但不在 public.diagnosis_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_diag_runs_next_settle' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_diag_runs_next_settle ON public.diagnosis_runs (next_settlement_at) WHERE run_status IN ('commit_pending','release_pending');
    END IF;
END $idxguard$;
-- billing_mode / run_status / freeze_backend 的 CHECK(已有表补块 · 新装内联故 IF NOT EXISTS 跳过)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_diag_runs_billing_mode' AND conrelid='diagnosis_runs'::regclass) THEN
        ALTER TABLE diagnosis_runs ADD CONSTRAINT chk_diag_runs_billing_mode CHECK (billing_mode IN ('paid','exempt'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_diag_runs_freeze_backend' AND conrelid='diagnosis_runs'::regclass) THEN
        ALTER TABLE diagnosis_runs ADD CONSTRAINT chk_diag_runs_freeze_backend CHECK (freeze_backend IS NULL OR freeze_backend IN ('legacy','v35'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_diag_runs_status' AND conrelid='diagnosis_runs'::regclass) THEN
        ALTER TABLE diagnosis_runs ADD CONSTRAINT chk_diag_runs_status CHECK (run_status IN (
            'pending_freeze','running','commit_pending','committed','release_pending','released',
            'cancelled','cancelled_no_freeze','completed_exempt','failed_exempt','settlement_manual',
            'delivery_repair_pending','manual_resolving'));
    END IF;
END $$;

-- [返工2 P0-2 / 返工3 P0] chk_diag_runs_status 已存在但缺新枚举值(delivery_repair_pending / manual_resolving)→ DROP+重建拓宽。
--   幂等:两值都含则 strpos>0 均跳过;2× 复跑第二遍已含故 no-op。CHECK 加枚举值必须 DROP+ADD(无法原地拓宽)。
DO $$
DECLARE ckdef text;
BEGIN
    IF to_regclass('diagnosis_runs') IS NULL THEN RETURN; END IF;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname='chk_diag_runs_status' AND conrelid='diagnosis_runs'::regclass);
    IF ckdef IS NOT NULL AND (strpos(ckdef, quote_literal('delivery_repair_pending')) = 0
                              OR strpos(ckdef, quote_literal('manual_resolving')) = 0) THEN
        ALTER TABLE diagnosis_runs DROP CONSTRAINT chk_diag_runs_status;
        ALTER TABLE diagnosis_runs ADD CONSTRAINT chk_diag_runs_status CHECK (run_status IN (
            'pending_freeze','running','commit_pending','committed','release_pending','released',
            'cancelled','cancelled_no_freeze','completed_exempt','failed_exempt','settlement_manual',
            'delivery_repair_pending','manual_resolving'));
        RAISE NOTICE '[diagnosis_runs 迁移] chk_diag_runs_status 已拓宽含 delivery_repair_pending + manual_resolving';
    END IF;
END $$;

-- client_request_id HC2 五步(已有表数据安全 · 新装已内联 NOT NULL 故各步 no-op)：
-- ① 可空补列
ALTER TABLE diagnosis_runs ADD COLUMN IF NOT EXISTS client_request_id TEXT;
-- ② 存量回填(仅 NULL 行)
UPDATE diagnosis_runs SET client_request_id = 'legacy:' || run_token WHERE client_request_id IS NULL;
-- ③ 验证无 NULL(fail-closed:有 NULL 则 RAISE 中止,人工订正)+ ④ SET NOT NULL(PG 幂等:已 NOT NULL 时 no-op)
DO $$
DECLARE null_cnt integer;
BEGIN
    SELECT count(*) INTO null_cnt FROM diagnosis_runs WHERE client_request_id IS NULL;
    IF null_cnt > 0 THEN
        RAISE EXCEPTION 'client_request_id 仍有 % 行 NULL · 回填异常 · 禁止 SET NOT NULL,人工核验后重试', null_cnt;
    END IF;
    ALTER TABLE diagnosis_runs ALTER COLUMN client_request_id SET NOT NULL;  -- 不吞异常:真出错=非零退出
END $$;

-- ⑤ HC1 两个唯一约束(§3.3a):请求级幂等 + 同品牌活跃(partial)
-- @index-guard uq_diag_request_idem ON diagnosis_runs unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_diag_request_idem' AND i.indrelid = to_regclass('public.diagnosis_runs')) THEN
        NULL;  -- 已在 public.diagnosis_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_diag_request_idem' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_diag_request_idem 已存在但不在 public.diagnosis_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_diag_request_idem' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_diag_request_idem ON public.diagnosis_runs (owner_user_id, client_request_id);
    END IF;
END $idxguard$;
-- @index-guard uq_diag_active_per_brand ON diagnosis_runs unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_diag_active_per_brand' AND i.indrelid = to_regclass('public.diagnosis_runs')) THEN
        NULL;  -- 已在 public.diagnosis_runs 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_diag_active_per_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_diag_active_per_brand 已存在但不在 public.diagnosis_runs 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_diag_active_per_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_diag_active_per_brand ON public.diagnosis_runs (owner_user_id, brand_id) WHERE run_status IN ('pending_freeze','running','commit_pending','release_pending');
    END IF;
END $idxguard$;

-- chk_freeze_handle 已有表补块(fail-closed 预检 · 新装已内联故 IF NOT EXISTS 跳过)
DO $$
DECLARE bad_count integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_freeze_handle' AND conrelid = 'diagnosis_runs'::regclass
    ) THEN
        SELECT count(*) INTO bad_count FROM diagnosis_runs
         WHERE billing_mode='paid' AND run_status IN ('running','commit_pending','release_pending')
           AND (freeze_id IS NULL OR freeze_backend IS NULL);
        IF bad_count > 0 THEN
            RAISE EXCEPTION 'chk_freeze_handle 预检失败: % 行违反 · 禁止自动猜测资金 backend · 跑清单人工订正后重试', bad_count;
        END IF;
        ALTER TABLE diagnosis_runs ADD CONSTRAINT chk_freeze_handle CHECK (
            NOT (billing_mode='paid' AND run_status IN ('running','commit_pending','release_pending'))
            OR (freeze_id IS NOT NULL AND freeze_backend IS NOT NULL));
    END IF;
END $$;

-- ── 结果可见性列(§3.6 · 产物表 additive · 旧数据 NULL 视为 published 向后兼容)──────
-- 本批先加主产物表 diagnosis_records;其余写入点(报告/衍生物)由接线期盘点补(交付清单)。
-- to_regclass 守卫:表不存在则整段 no-op(不报错)。
DO $$
BEGIN
    IF to_regclass('diagnosis_records') IS NOT NULL THEN
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS run_token TEXT;
        ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS result_visibility TEXT;  -- NULL=published(兼容)
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_diag_records_visibility' AND conrelid = 'diagnosis_records'::regclass
        ) THEN
            ALTER TABLE diagnosis_records ADD CONSTRAINT chk_diag_records_visibility CHECK (
                result_visibility IS NULL OR result_visibility IN ('pending','published','withheld'));
        END IF;
        -- @index-guard idx_diag_records_run_token ON diagnosis_records plain
        IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                    WHERE c.relname = 'idx_diag_records_run_token' AND i.indrelid = to_regclass('public.diagnosis_records')) THEN
            NULL;  -- 已在 public.diagnosis_records 上 → 幂等跳过
        ELSIF EXISTS (SELECT 1 FROM pg_class c
                       WHERE c.relname = 'idx_diag_records_run_token' AND c.relnamespace = 'public'::regnamespace) THEN
            RAISE EXCEPTION '[index-guard] idx_diag_records_run_token 已存在但不在 public.diagnosis_records 上(实际宿主:%)—— 拒绝静默跳过',
                (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
                   LEFT JOIN pg_index i ON i.indexrelid = c.oid
                   LEFT JOIN pg_class t ON t.oid = i.indrelid
                  WHERE c.relname = 'idx_diag_records_run_token' AND c.relnamespace = 'public'::regnamespace)
                USING ERRCODE = 'duplicate_object';
        ELSE
            CREATE INDEX idx_diag_records_run_token ON public.diagnosis_records (run_token);
        END IF;
    END IF;
END $$;

-- ── [返工2 item8] settlement_manual 持久审计表(append-only · 人工/自动资金处置全程留痕)──────
--   Redis 告警列表是易失的(ltrim 1000 · 重启丢);人工逐笔处置(尤其双表)+ 自动转人工的原因必须持久留痕
--   供事后审计/对账。additive · 只 INSERT 不 UPDATE/DELETE。
CREATE TABLE IF NOT EXISTS diagnosis_settlement_audit (
    id            BIGSERIAL PRIMARY KEY,
    run_token     TEXT NOT NULL,
    operator      TEXT NOT NULL,               -- 人工=用户名(uid);自动='system'
    action        TEXT NOT NULL,               -- manual_resolve_single|manual_resolve_double|auto_to_manual|dup_release|delivery_repair 等
    detail        TEXT,                        -- 审计正文(backend/freeze_id/decision/reason/note)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- @index-guard idx_diag_settle_audit_run ON diagnosis_settlement_audit plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_diag_settle_audit_run' AND i.indrelid = to_regclass('public.diagnosis_settlement_audit')) THEN
        NULL;  -- 已在 public.diagnosis_settlement_audit 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_diag_settle_audit_run' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_diag_settle_audit_run 已存在但不在 public.diagnosis_settlement_audit 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_diag_settle_audit_run' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_diag_settle_audit_run ON public.diagnosis_settlement_audit (run_token, created_at DESC);
    END IF;
END $idxguard$;

-- ── [返工5 复审二 P1] 诊断退款记录(run-bound · 引用**真实钱包退款流水** · 只读核验非手填凭证)──────
--   老板复审二:confirm 只读原冻结金额 + 接受手填任意 external_ref = 系统自造凭证再自证退款(假闭环)。且只核总额不核资金池拆分。
--   正解:confirm 只能**引用并锁定已有 refund_tx_ref**(finance 退款路径产生的不可变 type='refund' 流水 id),
--     只读核验 run_token/task_ref/freeze_id/backend/owner/**逐资金池拆分**/金额/单次使用;无真实退款流水 → 保持 refund_pending。
--   FK(run_token)+ 全 NOT NULL + backend CHECK + points>0 CHECK + points BIGINT(匹配 legacy amount_total)。
--   UNIQUE(run_token)=一 run 一退款;UNIQUE(backend,refund_tx_ref)=一退款流水只核销一个 run。additive · 只留痕不改余额。
CREATE TABLE IF NOT EXISTS diagnosis_refund_records (
    id              BIGSERIAL PRIMARY KEY,
    run_token       TEXT NOT NULL,               -- 被退的诊断 run(唯一)
    freeze_task_ref TEXT NOT NULL,               -- 该 run 冻结 task_ref
    freeze_id       BIGINT NOT NULL,             -- 被退冻结 id(keeper)
    freeze_backend  TEXT NOT NULL,               -- legacy|v35
    owner_user_id   INTEGER NOT NULL,            -- 被扣费方
    points          BIGINT NOT NULL,             -- 已核验退款总额 = 冻结 amount_total(BIGINT 匹配 legacy)
    pool_split      JSONB NOT NULL DEFAULT '{}'::jsonb,   -- 逐资金池拆分留痕(核验通过的实际退款拆分)
    refund_tx_ref   TEXT NOT NULL,               -- 引用的**真实钱包退款流水** id(type=refund · 只读核验 · 一证一 run)
    operator        TEXT NOT NULL,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_diag_refund_backend CHECK (freeze_backend IN ('legacy','v35')),
    CONSTRAINT chk_diag_refund_points_pos CHECK (points > 0),
    CONSTRAINT fk_diag_refund_run FOREIGN KEY (run_token) REFERENCES diagnosis_runs (run_token)
);
-- ── [P1-3 老板复审] 补块:已有旧版表〔f5b48142:external_ref / points INTEGER / freeze_id 可空 / 无 FK〕→ 升级 · **真 fail-closed** · 幂等 ──
--   老板复审 P1-3 三宗罪修复:
--     ① 删除所有 EXCEPTION WHEN others THEN NULL(旧版会**静默跳过 SET NOT NULL** → 空句柄残行蒙混过关宣布"全通过")。
--     ② 升级前**逐项统计不可证明的旧行 → RAISE EXCEPTION 输出待人工核对 run_token**(禁 external_ref 自动冒充 refund_tx_ref · 禁猜测回填)。
--     ③ FK/CHECK 加完后**显式 VALIDATE CONSTRAINT**(原 NOT VALID 无 VALIDATE → 旧残行带空句柄可绕过 · convalidated 恒 false)。
DO $$
DECLARE
    bad_cnt bigint;
    bad_ids text;
BEGIN
    IF to_regclass('diagnosis_refund_records') IS NULL THEN RETURN; END IF;
    -- 可空补列(升级旧表 · 新装 no-op)· **不给 external_ref 自动赋值 refund_tx_ref**(不可证明的凭证禁冒充真实退款流水)
    ALTER TABLE diagnosis_refund_records ADD COLUMN IF NOT EXISTS refund_tx_ref TEXT;
    ALTER TABLE diagnosis_refund_records ADD COLUMN IF NOT EXISTS pool_split JSONB NOT NULL DEFAULT '{}'::jsonb;
    -- 升级前 fail-closed 预检 1:任一"结构不完整 / 不可证明"的既有行 → RAISE 输出人工订正清单
    SELECT count(*),
           COALESCE(string_agg(DISTINCT COALESCE(run_token,'<null>'), ',' ORDER BY COALESCE(run_token,'<null>')), '')
      INTO bad_cnt, bad_ids
      FROM diagnosis_refund_records r
     WHERE r.run_token IS NULL OR r.freeze_task_ref IS NULL OR r.freeze_id IS NULL
        OR r.freeze_backend IS NULL OR r.freeze_backend NOT IN ('legacy','v35')
        OR r.owner_user_id IS NULL OR r.points IS NULL OR r.points <= 0 OR r.refund_tx_ref IS NULL
        OR NOT EXISTS (SELECT 1 FROM diagnosis_runs dr WHERE dr.run_token = r.run_token);
    IF bad_cnt > 0 THEN
        RAISE EXCEPTION '[迁移 fail-closed] diagnosis_refund_records 有 % 行不可证明(NULL 句柄 / 非法 backend / points≤0 / 无 refund_tx_ref / 孤儿 run_token)· '
            '禁自动回填(尤其禁 external_ref 冒充 refund_tx_ref)· 待人工核对 run_token: [%] · 逐行核实真实退款流水后回填 refund_tx_ref 再重跑', bad_cnt, bad_ids;
    END IF;
    -- 预检 2:重复 run_token(一 run 一退款)
    SELECT count(*) INTO bad_cnt FROM (
        SELECT run_token FROM diagnosis_refund_records GROUP BY run_token HAVING count(*) > 1) t;
    IF bad_cnt > 0 THEN
        RAISE EXCEPTION '[迁移 fail-closed] diagnosis_refund_records 有 % 个重复 run_token(违反一 run 一退款)· 人工核对后重跑', bad_cnt;
    END IF;
    -- 预检 3:重复 (freeze_backend, refund_tx_ref)(一退款流水只核销一个 run)
    SELECT count(*) INTO bad_cnt FROM (
        SELECT freeze_backend, refund_tx_ref FROM diagnosis_refund_records
         WHERE refund_tx_ref IS NOT NULL GROUP BY freeze_backend, refund_tx_ref HAVING count(*) > 1) t;
    IF bad_cnt > 0 THEN
        RAISE EXCEPTION '[迁移 fail-closed] diagnosis_refund_records 有 % 组重复 (backend, refund_tx_ref)(违反一证一 run)· 人工核对后重跑', bad_cnt;
    END IF;
    -- 干净(预检全过 / 空表):points→BIGINT + 必填列 SET NOT NULL(**不吞异常** · 真出错=RAISE 非零退出)
    ALTER TABLE diagnosis_refund_records ALTER COLUMN points TYPE BIGINT;
    ALTER TABLE diagnosis_refund_records ALTER COLUMN run_token SET NOT NULL;
    ALTER TABLE diagnosis_refund_records ALTER COLUMN freeze_task_ref SET NOT NULL;
    ALTER TABLE diagnosis_refund_records ALTER COLUMN freeze_id SET NOT NULL;
    ALTER TABLE diagnosis_refund_records ALTER COLUMN freeze_backend SET NOT NULL;
    ALTER TABLE diagnosis_refund_records ALTER COLUMN owner_user_id SET NOT NULL;
    ALTER TABLE diagnosis_refund_records ALTER COLUMN points SET NOT NULL;
    ALTER TABLE diagnosis_refund_records ALTER COLUMN refund_tx_ref SET NOT NULL;
    -- ADD FK/CHECK(NOT VALID · 幂等 · 新装已内联 VALID 则 IF NOT EXISTS 跳过)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_diag_refund_backend' AND conrelid='diagnosis_refund_records'::regclass) THEN
        ALTER TABLE diagnosis_refund_records ADD CONSTRAINT chk_diag_refund_backend CHECK (freeze_backend IN ('legacy','v35')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_diag_refund_points_pos' AND conrelid='diagnosis_refund_records'::regclass) THEN
        ALTER TABLE diagnosis_refund_records ADD CONSTRAINT chk_diag_refund_points_pos CHECK (points > 0) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_diag_refund_run' AND conrelid='diagnosis_refund_records'::regclass) THEN
        ALTER TABLE diagnosis_refund_records ADD CONSTRAINT fk_diag_refund_run FOREIGN KEY (run_token) REFERENCES diagnosis_runs (run_token) NOT VALID;
    END IF;
    -- **显式 VALIDATE**(已 VALID 时 no-op · 幂等)· 预检已保证无违反行 → 必过 · 使 convalidated=TRUE(反查有齿)
    ALTER TABLE diagnosis_refund_records VALIDATE CONSTRAINT chk_diag_refund_backend;
    ALTER TABLE diagnosis_refund_records VALIDATE CONSTRAINT chk_diag_refund_points_pos;
    ALTER TABLE diagnosis_refund_records VALIDATE CONSTRAINT fk_diag_refund_run;
END $$;
-- @drop-index-guard uq_diag_refund_extref ON diagnosis_refund_records if-exists
DO $dropguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_diag_refund_extref' AND i.indrelid = to_regclass('diagnosis_refund_records')) THEN
        DROP INDEX IF EXISTS uq_diag_refund_extref;
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_diag_refund_extref' AND c.relnamespace = current_schema()::regnamespace) THEN
        RAISE EXCEPTION '[drop-index-guard] uq_diag_refund_extref 不在 diagnosis_refund_records 上(实际宿主:%)—— 拒绝删掉别的表的索引',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_diag_refund_extref' AND c.relnamespace = current_schema()::regnamespace)
            USING ERRCODE = 'wrong_object_type';
    ELSE
        NULL;  -- 不存在 → 幂等跳过(与老形态 IF EXISTS 同口径)
    END IF;
END $dropguard$;   -- 旧唯一(external_ref)废弃 → 换 refund_tx_ref
-- @index-guard uq_diag_refund_run ON diagnosis_refund_records unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_diag_refund_run' AND i.indrelid = to_regclass('public.diagnosis_refund_records')) THEN
        NULL;  -- 已在 public.diagnosis_refund_records 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_diag_refund_run' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_diag_refund_run 已存在但不在 public.diagnosis_refund_records 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_diag_refund_run' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_diag_refund_run ON public.diagnosis_refund_records (run_token);
    END IF;
END $idxguard$;
-- @index-guard uq_diag_refund_txref ON diagnosis_refund_records unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_diag_refund_txref' AND i.indrelid = to_regclass('public.diagnosis_refund_records')) THEN
        NULL;  -- 已在 public.diagnosis_refund_records 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_diag_refund_txref' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_diag_refund_txref 已存在但不在 public.diagnosis_refund_records 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_diag_refund_txref' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_diag_refund_txref ON public.diagnosis_refund_records (freeze_backend, refund_tx_ref) WHERE refund_tx_ref IS NOT NULL;
    END IF;
END $idxguard$;

-- ── [P1-2] customer_credit_transactions.source 白名单加 'diagnosis_delivery_refund'(v35 交付缺失精确退款 source)──
--   铁律(agent_rebate 2026-05-30 / tool_fail_refund 2026-06-08 同款根因):**代码写新 source 必须同批更新 CHECK 白名单**,
--   否则每笔 v35 交付退款 INSERT 被 DB 拒回滚 → refund_and_confirm_v35_delivery 事务回滚 → 退款永远做不成。
--   幂等:DROP IF EXISTS + ADD 含全部 9 值(7 现有 canonical + diagnosis_delivery_refund + direct_service_refund)。
--   ⚠️ 假设 prod 该 CHECK = canonical 默认名 customer_credit_transactions_source_check(7 值)· Deploy 跑前 \d customer_credit_transactions
--      核约束名/取值(prod 若有非默认名手动约束先按名 DROP)· 资金/schema 批单独验单独放行。
DO $$
BEGIN
    IF to_regclass('customer_credit_transactions') IS NOT NULL THEN
        ALTER TABLE customer_credit_transactions DROP CONSTRAINT IF EXISTS customer_credit_transactions_source_check;
        ALTER TABLE customer_credit_transactions ADD CONSTRAINT customer_credit_transactions_source_check CHECK (source IN (
            'online_payment', 'offline_allocation', 'admin_adjust', 'tool_consume', 'refund_revoke',
            'agent_rebate', 'tool_fail_refund', 'diagnosis_delivery_refund', 'direct_service_refund'));
        RAISE NOTICE '[diagnosis_runs 迁移] customer_credit_transactions.source CHECK 保留 diagnosis_delivery_refund + direct_service_refund(canonical 9 值)';
    END IF;
END $$;

-- ── [V3.5 退款最终根治] 客户额度 consume/refund 流水符号 SSOT ───────────────
-- 代码语义核验:services/customer_credit.py 所有 consume 写负数、refund 写正数；历史生产只读证据亦为
-- consume 5 条全负且当前无 refund。约束只收紧这两个 type，不影响 allocate/revoke。
-- 升级铁律:先统计并列出违规行；有任一脏行直接 RAISE，禁止自动改历史数据。NOT VALID + 显式 VALIDATE
-- 兼容旧表并保证 convalidated=true；重复执行时 no-op + 再验证。
DO $$
DECLARE
    bad_count BIGINT := 0;
    bad_examples TEXT := '';
BEGIN
    IF to_regclass('customer_credit_transactions') IS NOT NULL THEN
        SELECT COUNT(*) INTO bad_count
        FROM customer_credit_transactions
        WHERE (type = 'consume' AND points >= 0)
           OR (type = 'refund' AND points <= 0);

        SELECT COALESCE(string_agg(format('id=%s type=%s points=%s', id, type, points), '; '), '')
          INTO bad_examples
        FROM (
            SELECT id, type, points
            FROM customer_credit_transactions
            WHERE (type = 'consume' AND points >= 0)
               OR (type = 'refund' AND points <= 0)
            ORDER BY id
            LIMIT 20
        ) bad;

        RAISE NOTICE '[customer_credit_transactions 符号预检] violations=% examples=%', bad_count, bad_examples;
        IF bad_count > 0 THEN
            RAISE EXCEPTION '[customer_credit_transactions 符号迁移 fail-closed] 发现 % 条违规流水(前20条:%)；禁止自动改历史数据，请人工核对订正后重跑',
                bad_count, bad_examples;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'customer_credit_transactions_consume_refund_points_sign_check'
              AND conrelid = 'customer_credit_transactions'::regclass
        ) THEN
            ALTER TABLE customer_credit_transactions
                ADD CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check
                CHECK (
                    (type <> 'consume' OR points < 0)
                    AND (type <> 'refund' OR points > 0)
                ) NOT VALID;
        END IF;
        ALTER TABLE customer_credit_transactions
            VALIDATE CONSTRAINT customer_credit_transactions_consume_refund_points_sign_check;
    END IF;
END $$;

-- ── 可执行反查断言(FF9+FF13 · 失败即 RAISE → 迁移/prestart 非零退出 · 不放不完整 schema 出去)──────
-- 核验(全维度 · 不只查名):全部核心列存在 + **每列类型精确** + **全部 NOT NULL 列约束** +
--   **四 CHECK 的定义含期望取值** + **两唯一索引的列 + partial predicate 真实定义** + diagnosis_records 两列+CHECK。
-- 任一不满足 RAISE EXCEPTION(硬门 · 非注释)。
DO $$
DECLARE
    col text;
    typ text;
    missing text := '';
    ckdef text;
    idxdef text;
    i int;
    -- (col, information_schema.data_type)· TIMESTAMPTZ→'timestamp with time zone'
    typemap text[] := ARRAY[
        'run_token','text', 'session_id','text', 'owner_user_id','integer', 'brand_id','integer',
        'client_request_id','text', 'billing_mode','text', 'freeze_task_ref','text', 'freeze_id','bigint',
        'freeze_backend','text', 'run_status','text', 'reaped_reason','text', 'settlement_attempts','integer',
        'last_settlement_error','text', 'status_changed_at','timestamp with time zone',
        'next_settlement_at','timestamp with time zone', 'settled_at','timestamp with time zone',
        'verify_empty_count','integer', 'last_verify_at','timestamp with time zone',
        'final_snapshot_jsonb','jsonb', 'manual_resolution','text',
        'manual_resolution_token','text', 'manual_lease_until','timestamp with time zone',
        'heartbeat_at','timestamp with time zone',
        'created_at','timestamp with time zone', 'finished_at','timestamp with time zone'];
    notnull_cols text[] := ARRAY['run_token','session_id','owner_user_id','client_request_id','billing_mode',
        'freeze_task_ref','run_status','settlement_attempts','status_changed_at','verify_empty_count',
        'heartbeat_at','created_at'];
    v text;
    -- 全枚举集(FF14:核全部值 · 不抽查)
    status_vals text[] := ARRAY['pending_freeze','running','commit_pending','committed','release_pending',
        'released','cancelled','cancelled_no_freeze','completed_exempt','failed_exempt','settlement_manual',
        'delivery_repair_pending','manual_resolving'];
    billing_vals text[] := ARRAY['paid','exempt'];
    backend_vals text[] := ARRAY['legacy','v35'];
    visibility_vals text[] := ARRAY['pending','published','withheld'];
BEGIN
    IF to_regclass('diagnosis_runs') IS NULL THEN
        RAISE EXCEPTION '[反查] diagnosis_runs 表不存在';
    END IF;
    -- 0) run_token 必须是 PRIMARY KEY(不只是列存在 · 防有列无 PK 的残表蒙混)
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.conrelid='diagnosis_runs'::regclass AND c.contype='p' AND a.attname='run_token'
    ) THEN
        RAISE EXCEPTION '[反查] run_token 不是 PRIMARY KEY';
    END IF;
    -- 1) 全部核心列存在 + 类型精确
    i := 1;
    WHILE i <= array_length(typemap, 1) LOOP
        col := typemap[i]; typ := typemap[i + 1];
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='diagnosis_runs' AND column_name=col) THEN
            missing := missing || ' ' || col;
        ELSIF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='diagnosis_runs' AND column_name=col AND data_type=typ) THEN
            RAISE EXCEPTION '[反查] 列 % 类型错(期望 %)', col, typ;
        END IF;
        i := i + 2;
    END LOOP;
    IF missing <> '' THEN
        RAISE EXCEPTION '[反查] diagnosis_runs 缺列:%', missing;
    END IF;
    -- 2) NOT NULL 列约束(全部)
    FOREACH col IN ARRAY notnull_cols LOOP
        IF EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name='diagnosis_runs' AND column_name=col AND is_nullable='YES') THEN
            RAISE EXCEPTION '[反查] 列 % 应为 NOT NULL 但可空', col;
        END IF;
    END LOOP;
    -- 3) 四 CHECK 存在 + 定义含**全部**期望取值(FF14:strpos 精确子串核每个枚举值 · 不抽查 · 不用 ILIKE 免下划线通配)
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname='chk_diag_runs_status' AND conrelid='diagnosis_runs'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_diag_runs_status 缺失'; END IF;
    FOREACH v IN ARRAY status_vals LOOP
        IF strpos(ckdef, quote_literal(v)) = 0 THEN
            RAISE EXCEPTION '[反查] chk_diag_runs_status 缺枚举值 %:%', v, ckdef;
        END IF;
    END LOOP;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname='chk_diag_runs_billing_mode' AND conrelid='diagnosis_runs'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_diag_runs_billing_mode 缺失'; END IF;
    FOREACH v IN ARRAY billing_vals LOOP
        IF strpos(ckdef, quote_literal(v)) = 0 THEN RAISE EXCEPTION '[反查] chk_diag_runs_billing_mode 缺枚举值 %:%', v, ckdef; END IF;
    END LOOP;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname='chk_diag_runs_freeze_backend' AND conrelid='diagnosis_runs'::regclass);
    IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_diag_runs_freeze_backend 缺失'; END IF;
    FOREACH v IN ARRAY backend_vals LOOP
        IF strpos(ckdef, quote_literal(v)) = 0 THEN RAISE EXCEPTION '[反查] chk_diag_runs_freeze_backend 缺枚举值 %:%', v, ckdef; END IF;
    END LOOP;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname='chk_freeze_handle' AND conrelid='diagnosis_runs'::regclass);
    -- chk_freeze_handle 非枚举(结构不变量):须含 freeze_id/freeze_backend 且覆盖三活跃态
    IF ckdef IS NULL OR strpos(ckdef,'freeze_id')=0 OR strpos(ckdef,'freeze_backend')=0
       OR strpos(ckdef,'running')=0 OR strpos(ckdef,'commit_pending')=0 OR strpos(ckdef,'release_pending')=0 THEN
        RAISE EXCEPTION '[反查] chk_freeze_handle 缺失或不变量不完整:%', ckdef;
    END IF;
    -- 4) 两唯一索引:UNIQUE + 列 + partial predicate 真实定义(pg_get_indexdef 全定义核)
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass('uq_diag_request_idem'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%owner_user_id%'
       OR idxdef NOT ILIKE '%client_request_id%' THEN
        RAISE EXCEPTION '[反查] uq_diag_request_idem 缺失/非唯一/列不符:%', idxdef;
    END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass('uq_diag_active_per_brand'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%owner_user_id%'
       OR idxdef NOT ILIKE '%brand_id%' OR idxdef NOT ILIKE '%WHERE%'
       OR idxdef NOT ILIKE '%pending_freeze%' OR idxdef NOT ILIKE '%running%'
       OR idxdef NOT ILIKE '%commit_pending%' OR idxdef NOT ILIKE '%release_pending%' THEN
        RAISE EXCEPTION '[反查] uq_diag_active_per_brand 缺失/列或 partial predicate 不符:%', idxdef;
    END IF;
    -- 5) diagnosis_records 可见性两列 + CHECK(仅当表存在)
    IF to_regclass('diagnosis_records') IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='diagnosis_records' AND column_name='run_token') THEN
            RAISE EXCEPTION '[反查] diagnosis_records.run_token 缺失';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='diagnosis_records' AND column_name='result_visibility') THEN
            RAISE EXCEPTION '[反查] diagnosis_records.result_visibility 缺失';
        END IF;
        ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conname='chk_diag_records_visibility' AND conrelid='diagnosis_records'::regclass);
        IF ckdef IS NULL THEN RAISE EXCEPTION '[反查] chk_diag_records_visibility 缺失'; END IF;
        FOREACH v IN ARRAY visibility_vals LOOP
            IF strpos(ckdef, quote_literal(v)) = 0 THEN RAISE EXCEPTION '[反查] chk_diag_records_visibility 缺枚举值 %:%', v, ckdef; END IF;
        END LOOP;
    END IF;
    -- 6) [返工2 item8] settlement 持久审计表 + 核心列
    IF to_regclass('diagnosis_settlement_audit') IS NULL THEN
        RAISE EXCEPTION '[反查] diagnosis_settlement_audit 表不存在';
    END IF;
    FOREACH col IN ARRAY ARRAY['run_token','operator','action','created_at'] LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='diagnosis_settlement_audit' AND column_name=col) THEN
            RAISE EXCEPTION '[反查] diagnosis_settlement_audit 缺列 %', col;
        END IF;
    END LOOP;
    -- 7) [返工5 复审二 P1] 诊断退款记录表 + 核心列 + FK/CHECK + points BIGINT + 两唯一索引(run_token 单退 · refund_tx_ref 单核销)
    IF to_regclass('diagnosis_refund_records') IS NULL THEN
        RAISE EXCEPTION '[反查] diagnosis_refund_records 表不存在';
    END IF;
    FOREACH col IN ARRAY ARRAY['run_token','freeze_task_ref','freeze_id','freeze_backend','owner_user_id','points','pool_split','refund_tx_ref','operator','created_at'] LOOP
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='diagnosis_refund_records' AND column_name=col) THEN
            RAISE EXCEPTION '[反查] diagnosis_refund_records 缺列 %', col;
        END IF;
    END LOOP;
    -- points 必须 BIGINT(匹配 legacy amount_total · 防溢出)
    IF (SELECT data_type FROM information_schema.columns WHERE table_name='diagnosis_refund_records' AND column_name='points') <> 'bigint' THEN
        RAISE EXCEPTION '[反查] diagnosis_refund_records.points 非 bigint';
    END IF;
    -- [P1-3] 必填列 NOT NULL(is_nullable='NO' · 不只查列存在 · 防旧残表带空句柄蒙混)
    FOREACH col IN ARRAY ARRAY['run_token','freeze_task_ref','freeze_id','freeze_backend','owner_user_id','points','refund_tx_ref'] LOOP
        IF EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name='diagnosis_refund_records' AND column_name=col AND is_nullable='YES') THEN
            RAISE EXCEPTION '[反查] diagnosis_refund_records.% 应为 NOT NULL 但可空(旧残表未收紧 · fail-closed)', col;
        END IF;
    END LOOP;
    -- [P1-3] FK 存在 + **引用 diagnosis_runs(run_token)** + **convalidated=TRUE**(NOT VALID 未 VALIDATE 也算失败)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_diag_refund_run' AND conrelid='diagnosis_refund_records'::regclass
                   AND contype='f' AND confrelid='diagnosis_runs'::regclass AND convalidated) THEN
        RAISE EXCEPTION '[反查] fk_diag_refund_run 缺失/未引用 diagnosis_runs/未 VALIDATE(convalidated=false)';
    END IF;
    -- [P1-3] 两 CHECK 存在 + **完整定义**(backend 含 legacy/v35 · points_pos 含 points>0)+ **convalidated=TRUE**
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname='chk_diag_refund_backend' AND conrelid='diagnosis_refund_records'::regclass AND convalidated);
    IF ckdef IS NULL OR strpos(ckdef, quote_literal('legacy'))=0 OR strpos(ckdef, quote_literal('v35'))=0 THEN
        RAISE EXCEPTION '[反查] chk_diag_refund_backend 缺失/未 VALIDATE/定义不含 legacy+v35:%', ckdef;
    END IF;
    ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname='chk_diag_refund_points_pos' AND conrelid='diagnosis_refund_records'::regclass AND convalidated);
    IF ckdef IS NULL OR strpos(ckdef, 'points')=0 OR strpos(ckdef, '> 0')=0 THEN
        RAISE EXCEPTION '[反查] chk_diag_refund_points_pos 缺失/未 VALIDATE/定义不含 points > 0:%', ckdef;
    END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass('uq_diag_refund_run'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%run_token%' THEN
        RAISE EXCEPTION '[反查] uq_diag_refund_run 缺失/非唯一/列不符:%', idxdef;
    END IF;
    idxdef := (SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid = to_regclass('uq_diag_refund_txref'));
    IF idxdef IS NULL OR idxdef NOT ILIKE '%UNIQUE%' OR idxdef NOT ILIKE '%refund_tx_ref%' OR idxdef NOT ILIKE '%freeze_backend%'
       OR idxdef NOT ILIKE '%WHERE%' THEN
        RAISE EXCEPTION '[反查] uq_diag_refund_txref 缺失/非唯一/列或 partial predicate 不符:%', idxdef;
    END IF;
    -- 8) [P1-2] customer_credit_transactions.source CHECK 含 diagnosis_delivery_refund(仅当表存在 · 防 v35 退款 INSERT 被拒)
    IF to_regclass('customer_credit_transactions') IS NOT NULL THEN
        ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conname='customer_credit_transactions_source_check' AND conrelid='customer_credit_transactions'::regclass);
        IF ckdef IS NULL OR strpos(ckdef, quote_literal('diagnosis_delivery_refund'))=0 THEN
            RAISE EXCEPTION '[反查] customer_credit_transactions.source CHECK 缺失或不含 diagnosis_delivery_refund(v35 退款会被 DB 拒):%', ckdef;
        END IF;
        ckdef := (SELECT pg_get_constraintdef(oid) FROM pg_constraint
                   WHERE conname='customer_credit_transactions_consume_refund_points_sign_check'
                     AND conrelid='customer_credit_transactions'::regclass AND convalidated);
        IF ckdef IS NULL
           OR strpos(ckdef, quote_literal('consume'))=0 OR strpos(ckdef, 'points < 0')=0
           OR strpos(ckdef, quote_literal('refund'))=0 OR strpos(ckdef, 'points > 0')=0 THEN
            RAISE EXCEPTION '[反查] customer_credit_transactions consume/refund 符号 CHECK 缺失/未 VALIDATE/定义不完整:%', ckdef;
        END IF;
    END IF;
    RAISE NOTICE '[diagnosis_runs 迁移] 反查断言全部通过(列/类型/NOT NULL/run_token PK/CHECK 全枚举+定义+convalidated/FK 引用+convalidated/唯一索引+partial predicate/审计表/退款记录表/source CHECK+流水符号 CHECK)';
END $$;
