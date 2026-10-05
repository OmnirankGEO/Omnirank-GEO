-- ============================================================================
-- 046 · 防御型 GEO WP7+WP8 · 监测 lineage / 防御投影 / 报告快照 / 续费 / 小榜冻结理由
--
-- 窄 RFC:docs/AI-CONTEXT/DEFGEO_W4_RFC_NEW_TABLES_2026-08-22.md(🟡 待 Owner 签)
-- 规格:§6.1 / §6.3 / §13.1 / §13.2 / §13.3 / §13.4 / §9.6
--
-- 🔴 五张**全新**表,零 ALTER 既有表、**体内零 DML**
--    (prestart 每次部署无条件重放全部迁移 —— 迁移里的 DML 是常驻地雷)。
--
-- 🔴 为什么必须另建 attempt 账本(逐字证据,不是"为了好看"):
--    现役 db/monitoring_db.py:4254-4264 的重试是
--        UPDATE monitoring_run_cells SET ... error_code=NULL, error_message=NULL ...
--    第一次尝试为什么失败,这一行跑完就没了。MON-03「重试建 child attempt,
--    **不覆盖原始失败**」与 MON-10「保留 attempt error」在一格一行的结构上
--    不可能成立。§13.1 又明令监测老链一行不改语义 ⇒ 只能另存。
--
-- 🔴 三条承重约束(判据各有一发变异,摘掉必红):
--    ① uq_defgeo_attempt_cell_ordinal      —— 同一格第 N 次尝试只能有一行
--    ② trg_defgeo_attempt_terminal_immutable —— 终态行不可改写(结构性,不靠应用层)
--    ③ uq_defgeo_attempt_single_inflight   —— 同一格同时至多一个在飞 attempt
-- ============================================================================

-- ────────────────────────────────────────────────────────────────────────────
-- 1. attempt 账本(追加式)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS defgeo_monitoring_attempts (
    attempt_id            CHARACTER(64) PRIMARY KEY,
    plan_cell_id          CHARACTER(64) NOT NULL,
    attempt_ordinal       INTEGER       NOT NULL,
    -- retry/fallback 的父 attempt。NULL = 本格第一次尝试。
    parent_attempt_id     CHARACTER(64),

    run_authority_id      TEXT          NOT NULL,
    tenant_owner_user_id  INTEGER       NOT NULL,
    brand_id              INTEGER       NOT NULL,
    -- 与现役一格一行的桥。可空:policy_skipped 从未派发,没有现役 cell。
    monitoring_cell_id    BIGINT,

    actual_provider       TEXT          NOT NULL,
    actual_model          TEXT          NOT NULL,
    -- [工单 E3-4 · P1-9b · 2026-08-26] 上面那一列**从哪来**。
    --
    -- 缺省 ``planned_fallback`` 是有意的:open 那一刻按定义只能是计划值
    -- (派发**之前**登记,平台还没回答)。只有收口时拿到真回显才会被改成
    -- ``provider_echo``(attempt_ledger.close_attempt 里那条 CASE)。
    --
    -- 🔴 为什么不能只靠列名叫 actual 就当它是实际值:两种来源在正常路径上
    --    相等,但**相等是巧合不是约束**(供应商灰度/别名路由、我们改了
    --    QWEN_ENGINE["model"] 而请求侧发了旧值)。而这一列同时是保真度对账
    --    与计价的取数口 —— 混在一起等于让"实际模型"证明不了它自己。
    actual_model_source   TEXT          NOT NULL DEFAULT 'planned_fallback',
    -- 平台确实不给版本时为 NULL(现役 monitoring_results.model_revision_unknown_reason
    -- 就是为这个存在的)。NULL 与空串不同义,所以这里允许 NULL、禁空串。
    actual_model_revision TEXT,
    actual_surface        TEXT          NOT NULL,
    actual_search_mode    TEXT          NOT NULL,
    request_hash          TEXT          NOT NULL,

    -- MON-11:policy_skipped 无 provider 调用 ⇒ 计 terminal 不计 attempted。
    provider_called       BOOLEAN       NOT NULL DEFAULT TRUE,

    -- NULL = 在飞。终态四值见 CHECK。
    terminal_state        VARCHAR(24),
    error_code            VARCHAR(64),
    error_message         TEXT,
    monitoring_result_id  INTEGER,
    terminal_at           TIMESTAMPTZ,

    ledger_version        VARCHAR(48)   NOT NULL,
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- 🔴 ``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表一列都不会补。
--    任何跑过旧版 046 的库(判据一次性库、以及万一已经上过线的环境)
--    都要靠下面这条 ALTER 才拿得到新列。additive + IF NOT EXISTS ⇒ 重放安全。
ALTER TABLE public.defgeo_monitoring_attempts
    ADD COLUMN IF NOT EXISTS actual_model_source TEXT NOT NULL DEFAULT 'planned_fallback';

DO $$
BEGIN
    -- ① 同一 plan cell 的第 N 次尝试只能有一行。
    --    这条唯一约束是「retry 不覆盖原始失败」的**结构**承重点:
    --    第二次尝试拿不到 ordinal=1,只能是 2,于是第 1 行原样留着。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_defgeo_attempt_cell_ordinal'
                      AND conrelid = 'public.defgeo_monitoring_attempts'::regclass) THEN
        ALTER TABLE defgeo_monitoring_attempts
            ADD CONSTRAINT uq_defgeo_attempt_cell_ordinal
            UNIQUE (plan_cell_id, attempt_ordinal);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_attempt_ordinal_positive'
                      AND conrelid = 'public.defgeo_monitoring_attempts'::regclass) THEN
        ALTER TABLE defgeo_monitoring_attempts
            ADD CONSTRAINT chk_defgeo_attempt_ordinal_positive
            CHECK (attempt_ordinal >= 1);
    END IF;

    -- 终态四值。与 services/defensive_geo/monitoring/attempt_ledger.TERMINAL_STATES
    -- 逐值同源;两处不一致时判据 test_terminal_states_match_db_check 转红。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_attempt_terminal_state'
                      AND conrelid = 'public.defgeo_monitoring_attempts'::regclass) THEN
        ALTER TABLE defgeo_monitoring_attempts
            ADD CONSTRAINT chk_defgeo_attempt_terminal_state
            CHECK (terminal_state IS NULL OR terminal_state IN
                   ('answered','entity_ambiguous','engine_error','policy_skipped'));
    END IF;

    -- 终态与终态时间必须同时有或同时无 —— 半套状态在恢复时无法判断"到底收了没"。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_attempt_terminal_pair'
                      AND conrelid = 'public.defgeo_monitoring_attempts'::regclass) THEN
        ALTER TABLE defgeo_monitoring_attempts
            ADD CONSTRAINT chk_defgeo_attempt_terminal_pair
            CHECK ((terminal_state IS NULL) = (terminal_at IS NULL));
    END IF;

    -- MON-02:engine_error 必须带 code(没有 code 的错误格给不出 reason/action)。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_attempt_engine_error_has_code'
                      AND conrelid = 'public.defgeo_monitoring_attempts'::regclass) THEN
        ALTER TABLE defgeo_monitoring_attempts
            ADD CONSTRAINT chk_defgeo_attempt_engine_error_has_code
            CHECK (terminal_state <> 'engine_error' OR error_code IS NOT NULL);
    END IF;

    -- MON-11:policy_skipped 恒为零 provider 调用。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_attempt_skipped_no_provider'
                      AND conrelid = 'public.defgeo_monitoring_attempts'::regclass) THEN
        ALTER TABLE defgeo_monitoring_attempts
            ADD CONSTRAINT chk_defgeo_attempt_skipped_no_provider
            CHECK (terminal_state <> 'policy_skipped' OR provider_called = FALSE);
    END IF;

    -- NULL 与空串不同义(§6.1「显式编码为 null」)。空串会让两个不同身份撞成一个。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_attempt_no_empty_revision'
                      AND conrelid = 'public.defgeo_monitoring_attempts'::regclass) THEN
        ALTER TABLE defgeo_monitoring_attempts
            ADD CONSTRAINT chk_defgeo_attempt_no_empty_revision
            CHECK (actual_model_revision IS NULL OR actual_model_revision <> '');
    END IF;

    -- [工单 E3-1 · 2026-08-26 · Codex 二审 P1-F6] 租户归属**禁 0**。
    --
    -- 本列 NOT NULL,所以"取不到租户"在类型层没有出口 —— 六个写点全都
    -- 用 `... or 0` 把这个出口堵成了一个**编出来的租户**:0 号用户不存在,
    -- 于是整条运行在账本里挂在一个不存在的人名下,而任何判据都不会红。
    --
    -- 应用层的正确行为是**不写这一行 + 告警**(见
    -- run_ledger_bridge._require_tenant_owner)。这条 CHECK 是它的结构承重:
    -- 应用层可以被下一个人改回去,CHECK 不会。
    --
    -- 🔴 存量行若已有 0,本约束会让本迁移**当场失败**(prestart 非零退出)。
    --    这是有意的:0 行必须先被 census + 处置,不许静默带着假租户上线。
    --    census 与处置命令见 scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql。
    -- 🔴 定义等价核验**不在这里手写**:本文件末尾的 @readiness-begin 046 块
    --    由 scripts/defgeo_readiness_gen.py 机械生成,期望集 = 本文件声明的
    --    全部 ADD CONSTRAINT(现在 28 条),期望定义从真 PG16 现读。
    --    在这里再手写一段 = 同一谓词写两处,必有一处没人验。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_attempt_tenant_owner_positive'
                      AND conrelid = 'public.defgeo_monitoring_attempts'::regclass) THEN
        ALTER TABLE defgeo_monitoring_attempts
            ADD CONSTRAINT chk_defgeo_attempt_tenant_owner_positive
            CHECK (tenant_owner_user_id > 0);
    END IF;
END $$;


-- ③ 同一格同时至多一个在飞 attempt。partial unique —— 终态行不受约束,
--    所以一格可以有任意多条**历史**尝试,但只能有一条**正在跑**的。
-- @index-guard uq_defgeo_attempt_single_inflight ON defgeo_monitoring_attempts unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_defgeo_attempt_single_inflight' AND i.indrelid = to_regclass('public.defgeo_monitoring_attempts')) THEN
        NULL;  -- 已在 public.defgeo_monitoring_attempts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_defgeo_attempt_single_inflight' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_defgeo_attempt_single_inflight 已存在但不在 public.defgeo_monitoring_attempts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_defgeo_attempt_single_inflight' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_defgeo_attempt_single_inflight ON public.defgeo_monitoring_attempts (plan_cell_id) WHERE terminal_state IS NULL;
    END IF;
END $idxguard$;

-- @index-guard idx_defgeo_attempt_cell ON defgeo_monitoring_attempts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_attempt_cell' AND i.indrelid = to_regclass('public.defgeo_monitoring_attempts')) THEN
        NULL;  -- 已在 public.defgeo_monitoring_attempts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_attempt_cell' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_attempt_cell 已存在但不在 public.defgeo_monitoring_attempts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_attempt_cell' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_attempt_cell ON public.defgeo_monitoring_attempts (plan_cell_id, attempt_ordinal);
    END IF;
END $idxguard$;
-- @index-guard idx_defgeo_attempt_tenant_brand ON defgeo_monitoring_attempts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_attempt_tenant_brand' AND i.indrelid = to_regclass('public.defgeo_monitoring_attempts')) THEN
        NULL;  -- 已在 public.defgeo_monitoring_attempts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_attempt_tenant_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_attempt_tenant_brand 已存在但不在 public.defgeo_monitoring_attempts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_attempt_tenant_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_attempt_tenant_brand ON public.defgeo_monitoring_attempts (tenant_owner_user_id, brand_id);
    END IF;
END $idxguard$;
-- @index-guard idx_defgeo_attempt_run_authority ON defgeo_monitoring_attempts plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_attempt_run_authority' AND i.indrelid = to_regclass('public.defgeo_monitoring_attempts')) THEN
        NULL;  -- 已在 public.defgeo_monitoring_attempts 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_attempt_run_authority' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_attempt_run_authority 已存在但不在 public.defgeo_monitoring_attempts 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_attempt_run_authority' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_attempt_run_authority ON public.defgeo_monitoring_attempts (run_authority_id);
    END IF;
END $idxguard$;

-- ② 终态行不可改写。
--
-- 🔴 为什么要触发器而不是"应用层记得加 WHERE terminal_state IS NULL":
--    应用层那条 CAS 是**纵深**的第一层,它防的是本模块自己写错;
--    触发器防的是**将来某个人**在别处写一条 UPDATE。MON-03 是 H0-DATA,
--    它的承重点不能是"我们记得"。判据对两层各拆一次。
CREATE OR REPLACE FUNCTION defgeo_attempt_terminal_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.terminal_state IS NOT NULL THEN
        RAISE EXCEPTION
            'defgeo_monitoring_attempts %: 终态 attempt 不可改写(MON-03「重试建 child attempt,不覆盖原始失败」)。要记录新的一次尝试请插入新行(ordinal+1)。',
            OLD.attempt_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_defgeo_attempt_terminal_immutable
    ON defgeo_monitoring_attempts;
CREATE TRIGGER trg_defgeo_attempt_terminal_immutable
    BEFORE UPDATE ON defgeo_monitoring_attempts
    FOR EACH ROW EXECUTE FUNCTION defgeo_attempt_terminal_immutable();


-- ────────────────────────────────────────────────────────────────────────────
-- 2. 防御事实投影(§13.1)—— **额外**投影,不改现役 target_outcome
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS defgeo_monitoring_defensive_projections (
    projection_id               CHARACTER(64) PRIMARY KEY,
    monitoring_result_id        INTEGER       NOT NULL,
    tenant_owner_user_id        INTEGER       NOT NULL,
    brand_id                    INTEGER       NOT NULL,
    plan_item_key               TEXT          NOT NULL,
    question_revision           INTEGER       NOT NULL,
    projection_version          VARCHAR(48)   NOT NULL,

    identity_state              VARCHAR(24)   NOT NULL,
    completeness                VARCHAR(24)   NOT NULL,
    -- 🔴 宽度 48 是**刻意**的:合法四值最长 corroborated(12),
    --    但被点名禁止的合并态 unsupported_or_conflicting 有 28 字符。
    --    列宽定成 24 时,那个值会被 StringDataRightTruncation 挡掉 ——
    --    看起来也是「插不进去」,可**真正拦住它的是列宽不是 CHECK**,
    --    于是把 CHECK 整条删掉判据照样绿(实测过,当场发现)。
    --    留出宽度,让 chk_defgeo_proj_support_level 真的承重。
    support_level               VARCHAR(48)   NOT NULL,
    has_conflict                BOOLEAN       NOT NULL,

    matched_facts               TEXT[]        NOT NULL DEFAULT '{}',
    missing_facts               TEXT[]        NOT NULL DEFAULT '{}',
    conflicting_facts           TEXT[]        NOT NULL DEFAULT '{}',

    evidence_manifest_revision  TEXT          NOT NULL,
    confidence                  DOUBLE PRECISION,

    -- R-1(§0.5.2):内容/判断类低置信冲突默认交 AI 复判,人工只留资金/身份类。
    adjudication_state          VARCHAR(24)   NOT NULL DEFAULT 'not_required',
    adjudicated_by              TEXT,

    created_at                  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_proj_identity_state'
                      AND conrelid = 'public.defgeo_monitoring_defensive_projections'::regclass) THEN
        ALTER TABLE defgeo_monitoring_defensive_projections
            ADD CONSTRAINT chk_defgeo_proj_identity_state
            CHECK (identity_state IN
                   ('confirmed','ambiguous','misidentified','not_evaluated'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_proj_completeness'
                      AND conrelid = 'public.defgeo_monitoring_defensive_projections'::regclass) THEN
        ALTER TABLE defgeo_monitoring_defensive_projections
            ADD CONSTRAINT chk_defgeo_proj_completeness
            CHECK (completeness IN ('complete','partial','not_evaluable'));
    END IF;

    -- 🔴 §13.1 逐字「禁止复活 unsupported_or_conflicting 合并态」。
    --    四值枚举把那个合并态在库层就挡掉 —— 它连存都存不进来。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_proj_support_level'
                      AND conrelid = 'public.defgeo_monitoring_defensive_projections'::regclass) THEN
        ALTER TABLE defgeo_monitoring_defensive_projections
            ADD CONSTRAINT chk_defgeo_proj_support_level
            CHECK (support_level IN
                   ('corroborated','inferred','unsupported','unknown'));
    END IF;

    -- MET-23:hasConflict=true 必有冲突源、false 必为空。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_proj_conflict_pair'
                      AND conrelid = 'public.defgeo_monitoring_defensive_projections'::regclass) THEN
        ALTER TABLE defgeo_monitoring_defensive_projections
            ADD CONSTRAINT chk_defgeo_proj_conflict_pair
            CHECK (has_conflict = (array_length(conflicting_facts, 1) IS NOT NULL));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_proj_adjudication'
                      AND conrelid = 'public.defgeo_monitoring_defensive_projections'::regclass) THEN
        ALTER TABLE defgeo_monitoring_defensive_projections
            ADD CONSTRAINT chk_defgeo_proj_adjudication
            CHECK (adjudication_state IN
                   ('not_required','pending_review','ai_adjudicated','human_adjudicated'));
    END IF;

    -- R-1 留痕:AI/人工两档必须带可复现的裁决方;另两档必须为空。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_proj_adjudicator_trace'
                      AND conrelid = 'public.defgeo_monitoring_defensive_projections'::regclass) THEN
        ALTER TABLE defgeo_monitoring_defensive_projections
            ADD CONSTRAINT chk_defgeo_proj_adjudicator_trace
            CHECK (
                (adjudication_state IN ('not_required','pending_review')
                 AND adjudicated_by IS NULL)
             OR (adjudication_state = 'ai_adjudicated'
                 AND adjudicated_by LIKE 'ai:%@%')
             OR (adjudication_state = 'human_adjudicated'
                 AND adjudicated_by LIKE 'human:%')
            );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_proj_confidence_range'
                      AND conrelid = 'public.defgeo_monitoring_defensive_projections'::regclass) THEN
        ALTER TABLE defgeo_monitoring_defensive_projections
            ADD CONSTRAINT chk_defgeo_proj_confidence_range
            CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0));
    END IF;
END $$;

-- @index-guard idx_defgeo_proj_result ON defgeo_monitoring_defensive_projections plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_proj_result' AND i.indrelid = to_regclass('public.defgeo_monitoring_defensive_projections')) THEN
        NULL;  -- 已在 public.defgeo_monitoring_defensive_projections 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_proj_result' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_proj_result 已存在但不在 public.defgeo_monitoring_defensive_projections 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_proj_result' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_proj_result ON public.defgeo_monitoring_defensive_projections (monitoring_result_id);
    END IF;
END $idxguard$;
-- @index-guard idx_defgeo_proj_tenant_brand ON defgeo_monitoring_defensive_projections plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_proj_tenant_brand' AND i.indrelid = to_regclass('public.defgeo_monitoring_defensive_projections')) THEN
        NULL;  -- 已在 public.defgeo_monitoring_defensive_projections 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_proj_tenant_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_proj_tenant_brand 已存在但不在 public.defgeo_monitoring_defensive_projections 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_proj_tenant_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_proj_tenant_brand ON public.defgeo_monitoring_defensive_projections (tenant_owner_user_id, brand_id);
    END IF;
END $idxguard$;


-- ────────────────────────────────────────────────────────────────────────────
-- 3. 报告快照(§13.3 冻结面 / §6.1 末段 11 项)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS defgeo_report_snapshots (
    id                          BIGSERIAL PRIMARY KEY,
    report_snapshot_id          TEXT          NOT NULL,
    revision                    INTEGER       NOT NULL,
    tenant_owner_user_id        INTEGER       NOT NULL,
    brand_id                    INTEGER       NOT NULL,
    diagnosis_id                INTEGER,

    state                       VARCHAR(24)   NOT NULL,
    content_hash                CHARACTER(64) NOT NULL,

    -- §6.1 末段逐字 11 项冻结。**全部 NOT NULL** —— 少一项就无法从前五边界
    -- 重建第六边界(MON-06)。
    plan_snapshot_id            TEXT          NOT NULL,
    plan_snapshot_hash          CHARACTER(64) NOT NULL,
    sampling_window_start       TIMESTAMPTZ   NOT NULL,
    sampling_window_end         TIMESTAMPTZ   NOT NULL,
    cutoff_at                   TIMESTAMPTZ   NOT NULL,
    input_watermark             TEXT          NOT NULL,
    raw_result_ids              INTEGER[]     NOT NULL,
    entity_resolver_version     VARCHAR(64)   NOT NULL,
    outcome_classifier_version  VARCHAR(64)   NOT NULL,
    evidence_extractor_version  VARCHAR(64)   NOT NULL,
    metric_definition_version   VARCHAR(64)   NOT NULL,

    snapshot_version            VARCHAR(48)   NOT NULL,
    created_at                  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_defgeo_snapshot_revision'
                      AND conrelid = 'public.defgeo_report_snapshots'::regclass) THEN
        ALTER TABLE defgeo_report_snapshots
            ADD CONSTRAINT uq_defgeo_snapshot_revision
            UNIQUE (report_snapshot_id, revision);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_snapshot_state'
                      AND conrelid = 'public.defgeo_report_snapshots'::regclass) THEN
        ALTER TABLE defgeo_report_snapshots
            ADD CONSTRAINT chk_defgeo_snapshot_state
            CHECK (state IN ('processing','ready','partial','no_conclusion','failed'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_snapshot_revision_positive'
                      AND conrelid = 'public.defgeo_report_snapshots'::regclass) THEN
        ALTER TABLE defgeo_report_snapshots
            ADD CONSTRAINT chk_defgeo_snapshot_revision_positive
            CHECK (revision >= 1);
    END IF;

    -- 采样窗口方向。倒着的窗口会让 D0/D30 的 as_of 递增断言恒真。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_snapshot_window_order'
                      AND conrelid = 'public.defgeo_report_snapshots'::regclass) THEN
        ALTER TABLE defgeo_report_snapshots
            ADD CONSTRAINT chk_defgeo_snapshot_window_order
            CHECK (sampling_window_end >= sampling_window_start);
    END IF;
END $$;

-- POR-15:「同 reportSnapshotId/hash **永不**从 processing/failed 原地变 ready」。
-- 与 attempt 账本同一思路:结构承重,不靠应用层记得。
CREATE OR REPLACE FUNCTION defgeo_snapshot_no_inplace_state_change()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.content_hash = NEW.content_hash AND OLD.state IS DISTINCT FROM NEW.state THEN
        RAISE EXCEPTION
            'defgeo_report_snapshots %(rev %): 同一 content hash 上原地改状态 % -> %(POR-15)。状态推进必须写**新 revision** —— 原地改会让已经发出去的链接指向一份内容没变但结论变了的报告。',
            OLD.report_snapshot_id, OLD.revision, OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_defgeo_snapshot_no_inplace_state_change
    ON defgeo_report_snapshots;
CREATE TRIGGER trg_defgeo_snapshot_no_inplace_state_change
    BEFORE UPDATE ON defgeo_report_snapshots
    FOR EACH ROW EXECUTE FUNCTION defgeo_snapshot_no_inplace_state_change();

-- @index-guard idx_defgeo_snapshot_tenant_brand ON defgeo_report_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_snapshot_tenant_brand' AND i.indrelid = to_regclass('public.defgeo_report_snapshots')) THEN
        NULL;  -- 已在 public.defgeo_report_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_snapshot_tenant_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_snapshot_tenant_brand 已存在但不在 public.defgeo_report_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_snapshot_tenant_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_snapshot_tenant_brand ON public.defgeo_report_snapshots (tenant_owner_user_id, brand_id);
    END IF;
END $idxguard$;
-- @index-guard idx_defgeo_snapshot_diagnosis ON defgeo_report_snapshots plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_snapshot_diagnosis' AND i.indrelid = to_regclass('public.defgeo_report_snapshots')) THEN
        NULL;  -- 已在 public.defgeo_report_snapshots 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_snapshot_diagnosis' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_snapshot_diagnosis 已存在但不在 public.defgeo_report_snapshots 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_snapshot_diagnosis' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_snapshot_diagnosis ON public.defgeo_report_snapshots (diagnosis_id);
    END IF;
END $idxguard$;


-- ────────────────────────────────────────────────────────────────────────────
-- 4. 续费建议(§13.4)—— 🔴 **零资金列**
-- ────────────────────────────────────────────────────────────────────────────
-- 表上没有任何 amount / points / price / freeze 列。
-- 「不自动扣费」这件事在 schema 层就没有落脚点 —— 想自动扣也无处记账。
CREATE TABLE IF NOT EXISTS defgeo_renewal_recommendations (
    id                        BIGSERIAL PRIMARY KEY,
    recommendation_id         TEXT        NOT NULL,
    tenant_owner_user_id      INTEGER     NOT NULL,
    brand_id                  INTEGER     NOT NULL,
    basis_report_snapshot_id  TEXT        NOT NULL,

    level                     VARCHAR(32) NOT NULL,
    comparability_level       VARCHAR(16) NOT NULL,
    -- 六维读数逐条存,不存一句结论 —— 结论可以重算,读数不能。
    signals                   JSONB       NOT NULL,

    recommendation_version    VARCHAR(48) NOT NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_defgeo_renewal_id'
                      AND conrelid = 'public.defgeo_renewal_recommendations'::regclass) THEN
        ALTER TABLE defgeo_renewal_recommendations
            ADD CONSTRAINT uq_defgeo_renewal_id UNIQUE (recommendation_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_renewal_level'
                      AND conrelid = 'public.defgeo_renewal_recommendations'::regclass) THEN
        ALTER TABLE defgeo_renewal_recommendations
            ADD CONSTRAINT chk_defgeo_renewal_level
            CHECK (level IN
                   ('renew_recommended','renew_with_adjustment','insufficient_basis'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_renewal_comparability'
                      AND conrelid = 'public.defgeo_renewal_recommendations'::regclass) THEN
        ALTER TABLE defgeo_renewal_recommendations
            ADD CONSTRAINT chk_defgeo_renewal_comparability
            CHECK (comparability_level IN ('none','partial','full'));
    END IF;

    -- §13.2「不可比时不算涨跌」的下游:不可比就只能是 insufficient_basis。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_renewal_none_is_insufficient'
                      AND conrelid = 'public.defgeo_renewal_recommendations'::regclass) THEN
        ALTER TABLE defgeo_renewal_recommendations
            ADD CONSTRAINT chk_defgeo_renewal_none_is_insufficient
            CHECK (comparability_level <> 'none' OR level = 'insufficient_basis');
    END IF;

    -- 六维必须齐(§13.4 逐字六维,少一维就是挑着说)。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_renewal_six_signals'
                      AND conrelid = 'public.defgeo_renewal_recommendations'::regclass) THEN
        ALTER TABLE defgeo_renewal_recommendations
            ADD CONSTRAINT chk_defgeo_renewal_six_signals
            CHECK (jsonb_typeof(signals) = 'array' AND jsonb_array_length(signals) = 6);
    END IF;
END $$;

-- @index-guard idx_defgeo_renewal_tenant_brand ON defgeo_renewal_recommendations plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_renewal_tenant_brand' AND i.indrelid = to_regclass('public.defgeo_renewal_recommendations')) THEN
        NULL;  -- 已在 public.defgeo_renewal_recommendations 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_renewal_tenant_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_renewal_tenant_brand 已存在但不在 public.defgeo_renewal_recommendations 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_renewal_tenant_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_renewal_tenant_brand ON public.defgeo_renewal_recommendations (tenant_owner_user_id, brand_id);
    END IF;
END $idxguard$;


-- ────────────────────────────────────────────────────────────────────────────
-- 5. 小榜冻结公开理由(§9.6)—— 只读,不现场生成
-- ────────────────────────────────────────────────────────────────────────────
-- §9.6 逐字:「小榜只能读取**公开、冻结**的推荐原因和下一步,
--            **不现场生成**媒体理由或商业数字」。
-- 「冻结」的可执行含义 = 签发时写下来、之后只读。现算的理由每次可能不同,
-- 客户问两次会得到两套说法,而两套都会被当成我们的承诺。
CREATE TABLE IF NOT EXISTS defgeo_xiaobang_frozen_reasons (
    id                    BIGSERIAL PRIMARY KEY,
    reason_ref            TEXT        NOT NULL,
    tenant_owner_user_id  INTEGER     NOT NULL,
    brand_id              INTEGER     NOT NULL,
    -- 理由挂在哪个冻结对象上(报告快照 / 媒体决策快照 / 报价快照)。
    subject_kind          VARCHAR(32) NOT NULL,
    subject_ref           TEXT        NOT NULL,

    reason_code           VARCHAR(64) NOT NULL,
    -- 🔴 人话,零工程词、零商业数字。运行时另有
    --    services/defensive_geo/xiaobang/frozen_reasons.assert_no_business_numbers。
    public_text           TEXT        NOT NULL,
    next_action_kind      VARCHAR(48) NOT NULL,

    frozen_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at            TIMESTAMPTZ,
    reasons_version       VARCHAR(48) NOT NULL
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_defgeo_frozen_reason_ref'
                      AND conrelid = 'public.defgeo_xiaobang_frozen_reasons'::regclass) THEN
        ALTER TABLE defgeo_xiaobang_frozen_reasons
            ADD CONSTRAINT uq_defgeo_frozen_reason_ref UNIQUE (reason_ref);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_frozen_reason_subject'
                      AND conrelid = 'public.defgeo_xiaobang_frozen_reasons'::regclass) THEN
        ALTER TABLE defgeo_xiaobang_frozen_reasons
            ADD CONSTRAINT chk_defgeo_frozen_reason_subject
            CHECK (subject_kind IN
                   ('report_snapshot','media_decision_snapshot','quote_snapshot'));
    END IF;

    -- 🔴 零商业数字的**库层**下限:理由文本里不许出现"N 算力 / N 元 / ¥N"形态。
    --    运行时门更严(还查百分比与内部枚举),但库层这一道保证
    --    "绕过服务端直接写库"也进不来。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_frozen_reason_no_money'
                      AND conrelid = 'public.defgeo_xiaobang_frozen_reasons'::regclass) THEN
        ALTER TABLE defgeo_xiaobang_frozen_reasons
            ADD CONSTRAINT chk_defgeo_frozen_reason_no_money
            CHECK (public_text !~ '[0-9]\s*(算力|元|块钱)' AND public_text !~ '[¥$]\s*[0-9]');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_defgeo_frozen_reason_text_nonempty'
                      AND conrelid = 'public.defgeo_xiaobang_frozen_reasons'::regclass) THEN
        ALTER TABLE defgeo_xiaobang_frozen_reasons
            ADD CONSTRAINT chk_defgeo_frozen_reason_text_nonempty
            CHECK (length(btrim(public_text)) > 0);
    END IF;
END $$;

-- @index-guard idx_defgeo_frozen_reason_subject ON defgeo_xiaobang_frozen_reasons plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_frozen_reason_subject' AND i.indrelid = to_regclass('public.defgeo_xiaobang_frozen_reasons')) THEN
        NULL;  -- 已在 public.defgeo_xiaobang_frozen_reasons 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_frozen_reason_subject' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_frozen_reason_subject 已存在但不在 public.defgeo_xiaobang_frozen_reasons 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_frozen_reason_subject' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_frozen_reason_subject ON public.defgeo_xiaobang_frozen_reasons (subject_kind, subject_ref);
    END IF;
END $idxguard$;
-- @index-guard idx_defgeo_frozen_reason_tenant_brand ON defgeo_xiaobang_frozen_reasons plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_frozen_reason_tenant_brand' AND i.indrelid = to_regclass('public.defgeo_xiaobang_frozen_reasons')) THEN
        NULL;  -- 已在 public.defgeo_xiaobang_frozen_reasons 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_frozen_reason_tenant_brand' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_frozen_reason_tenant_brand 已存在但不在 public.defgeo_xiaobang_frozen_reasons 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_frozen_reason_tenant_brand' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_frozen_reason_tenant_brand ON public.defgeo_xiaobang_frozen_reasons (tenant_owner_user_id, brand_id);
    END IF;
END $idxguard$;

-- @readiness-begin 046
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT 28 条 + @index-guard 11 个 + CREATE TRIGGER 2 个 + ADD COLUMN 1 列),
--    不是手挑的"承重"子集 —— 手挑清单漏掉的那一条不会让任何判据变红。
--    期望值 = 真 PG16 上 defgeo_ident_* 六个函数算出的**语义身份**(不是 pg_get_*def 的渲染文本)。
--    🔴 渲染文本会被 pg_dump→pg_restore / ALTER VALIDATE / 跨版本重写(语义等价、文本不等),
--       守全等就会在"第二次部署"必炸;换期望串只是把洞挪到全新库那一侧。
-- 🔴 索引**必须按 (表, 索引名) 判存**:索引名只在 schema 内唯一、不绑表,
--    别的表上有同名索引时"按名判存"会报绿,而目标表上其实一个都没有。
-- 🔴 触发器同理,而且更贵:`pg_trigger.tgname` 同样不绑表,且"它在"证明不了
--    "它拦得住" —— 所以这里核的是 (宿主表, 定义, tgenabled, 函数体 md5) 四样。
-- 🔴 列合同核 (format_type 带 typmod, attnotnull, pg_get_expr(adbin)) 三样:
--    只核 information_schema.data_type 的守卫对「错长度」零判别(varchar(64) 与
--    varchar(255) 的 data_type 都是 character varying),对「错 default」更是零核验。
-- 🔴 仍然零 DML:只 SELECT + RAISE(连 SAVEPOINT+ROLLBACK 的插入探针也不用)。
-- 🔴 下面这批身份函数由生成器**同一份常量**发射到每个 readiness 块里,
--    并且生成期算期望值用的**就是它们** —— 期望侧与运行期侧只有一份实现。
--    CREATE OR REPLACE 是 DDL、幂等,块单独重放也自带函数。
-- ══════════════════════════════════════════════════════════════════════
-- 结构化身份函数 —— readiness 块与生成器**共用同一份实现**
-- ══════════════════════════════════════════════════════════════════════
-- 🔴 [P0 · 2026-09-02] 为什么不再比 `pg_get_*def()` 的字符串:
--    `pg_dump -Fc` → `pg_restore --schema-only` 会把
--      CHECK (col IN (...))  在 varchar 列上从
--      `ANY ((ARRAY[...])::text[])` 重渲染成 `ANY (ARRAY[(...)::text, ...])`
--    —— 语义等价、文本不等。首次部署走 ADD 分支不比对所以过,**第二次起必炸**。
--    把期望串换成还原后那一形是陷阱:全新库(灾备重建/新环境)上又炸,只是把洞挪个位置。
--    去空白/小写也救不了:差异是**结构性**的(括号与 cast 层级)。
--    而且 dump/restore 只是触发重渲染的**路径之一** —— `ALTER … VALIDATE`、
--    跨大版本升级都可能再改渲染形。所以:**不比渲染,比语义身份**。
--
-- 🔴 全程只读系统表,零 DML(迁移体内禁 DML 是本仓铁律;
--    连 SAVEPOINT+ROLLBACK 的插入探针也算 DML)。
--
-- 🔴 五维,缺一漏一类:
--    ① contype/属性  ② conkey 列序集合  ③ convalidated
--    ④ **字面量集合**(排序去重;含数字)—— 抓 IN 闭集被改
--    ⑤ **运算符多重集**(token 计数)—— 抓 `> 0` 被改成 `< 0` 这种
--       字面量集合与 conkey 都不变的漂移。④ 单独用会漏它。
--    ⑤ 取自渲染文本,但只取 **token 多重集**,对括号/cast 层级改写稳定,
--    也不重造表达式树(重造 = 另一套 SQL 解析器,自己会漂)。

CREATE OR REPLACE FUNCTION public.defgeo_ident_literals(p_expr text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT COALESCE(string_agg(v, ',' ORDER BY v), '')
      FROM (
        SELECT DISTINCT m[1] AS v
          FROM regexp_matches(lower(COALESCE(p_expr, '')), '''([^'']*)''', 'g') AS m
        UNION
        SELECT DISTINCT m[1]
          FROM regexp_matches(lower(COALESCE(p_expr, '')),
                              '(?<![a-z_0-9.''])(-?[0-9]+(?:\.[0-9]+)?)', 'g') AS m
      ) s
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_ops(p_expr text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT COALESCE(string_agg(op || ':' || n::text, ',' ORDER BY op), '')
      FROM (
        SELECT m[1] AS op, count(*) AS n
          FROM regexp_matches(lower(COALESCE(p_expr, '')),
               '(<=|>=|<>|!=|=|<|>|\yany\y|\yall\y|\yin\y|\yand\y|\yor\y|\ynot\y|\yis\y|\ynull\y|\ylike\y|\ysimilar\y|\ybetween\y)',
               'g') AS m
         GROUP BY m[1]
      ) s
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_constraint(p_name text, p_table text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT c.contype::text
        || '|cols=' || COALESCE(
               (SELECT string_agg(a.attname, ',' ORDER BY a.attname)
                  FROM unnest(c.conkey) k
                  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k), '-')
        || '|fk=' || COALESCE(c.confrelid::regclass::text, '-')
        || '|fkcols=' || COALESCE(
               (SELECT string_agg(a.attname, ',' ORDER BY a.attname)
                  FROM unnest(c.confkey) k
                  JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k), '-')
        || '|upd=' || COALESCE(NULLIF(c.confupdtype::text, ''), '-')
        || '|del=' || COALESCE(NULLIF(c.confdeltype::text, ''), '-')
        || '|valid=' || c.convalidated::text
        || '|lits=' || public.defgeo_ident_literals(pg_get_expr(c.conbin, c.conrelid))
        || '|ops=' || public.defgeo_ident_ops(pg_get_expr(c.conbin, c.conrelid))
      FROM pg_constraint c
     WHERE c.conname = p_name AND c.conrelid = to_regclass(p_table)
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_index(p_name text, p_table text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT 'live=' || (i.indisvalid AND i.indisready)::text
        || '|unique=' || i.indisunique::text
        || '|cols=' || COALESCE(
               (SELECT string_agg(a.attname, ',' ORDER BY a.attname)
                  FROM unnest(i.indkey::int2[]) k
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k), '-')
        || '|am=' || am.amname
        || '|predlits=' || public.defgeo_ident_literals(pg_get_expr(i.indpred, i.indrelid))
        || '|predops=' || public.defgeo_ident_ops(pg_get_expr(i.indpred, i.indrelid))
        || '|exprlits=' || public.defgeo_ident_literals(pg_get_expr(i.indexprs, i.indrelid))
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
      JOIN pg_am am ON am.oid = c.relam
     WHERE c.relname = p_name AND i.indrelid = to_regclass(p_table)
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_column(p_table text, p_col text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT 'typ=' || a.atttypid::text
        || '|mod=' || a.atttypmod::text
        || '|notnull=' || a.attnotnull::text
        || '|deflits=' || public.defgeo_ident_literals(pg_get_expr(d.adbin, d.adrelid))
        || '|defops=' || public.defgeo_ident_ops(pg_get_expr(d.adbin, d.adrelid))
      FROM pg_attribute a
      LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE a.attrelid = to_regclass(p_table) AND a.attname = p_col
       AND a.attnum > 0 AND NOT a.attisdropped
$fn$;

CREATE OR REPLACE FUNCTION public.defgeo_ident_trigger(p_name text, p_table text)
RETURNS text LANGUAGE sql STABLE AS $fn$
    SELECT 'enabled=' || g.tgenabled::text
        || '|type=' || g.tgtype::text
        || '|fn=' || md5(regexp_replace(lower(pg_get_functiondef(g.tgfoid)), '\s+', '', 'g'))
      FROM pg_trigger g
     WHERE g.tgname = p_name AND g.tgrelid = to_regclass(p_table)
       AND NOT g.tgisinternal
$fn$;

DO $readiness046$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('actual_model_source', 'public.defgeo_monitoring_attempts', 'typ=25|mod=-1|notnull=true|deflits=planned_fallback|defops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[046] 列 %.% 的目标表不存在', r.tname, r.cname;
        END IF;
        SELECT public.defgeo_ident_column(r.tname, r.cname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[046] 列 %.% 不存在 —— ADD COLUMN 没生效?', r.tname, r.cname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[046] 列 %.% 的合同不符:期望「%」实得「%」 —— '
                'ADD COLUMN IF NOT EXISTS 按**列名**判存,库上已有同名列时直接 no-op,'
                '错类型/错长度/错 default 一律不纠正;必须人工 ALTER 或 DROP 后重跑',
                r.tname, r.cname, r.cdef, actual;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('chk_defgeo_attempt_engine_error_has_code', 'public.defgeo_monitoring_attempts', 'c|cols=error_code,terminal_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=engine_error|ops=<>:1,is:1,not:1,null:1,or:1'),
        ('chk_defgeo_attempt_no_empty_revision', 'public.defgeo_monitoring_attempts', 'c|cols=actual_model_revision|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops=<>:1,is:1,null:1,or:1'),
        ('chk_defgeo_attempt_ordinal_positive', 'public.defgeo_monitoring_attempts', 'c|cols=attempt_ordinal|fk=-|fkcols=-|upd= |del= |valid=true|lits=1|ops=>=:1'),
        ('chk_defgeo_attempt_skipped_no_provider', 'public.defgeo_monitoring_attempts', 'c|cols=provider_called,terminal_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=policy_skipped|ops=<>:1,=:1,or:1'),
        ('chk_defgeo_attempt_tenant_owner_positive', 'public.defgeo_monitoring_attempts', 'c|cols=tenant_owner_user_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=0|ops=>:1'),
        ('chk_defgeo_attempt_terminal_pair', 'public.defgeo_monitoring_attempts', 'c|cols=terminal_at,terminal_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops==:1,is:2,null:2'),
        ('chk_defgeo_attempt_terminal_state', 'public.defgeo_monitoring_attempts', 'c|cols=terminal_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=answered,engine_error,entity_ambiguous,policy_skipped|ops==:1,any:1,is:1,null:1,or:1'),
        ('chk_defgeo_frozen_reason_no_money', 'public.defgeo_xiaobang_frozen_reasons', 'c|cols=public_text|fk=-|fkcols=-|upd= |del= |valid=true|lits=0,9,[0-9]\s*(算力|元|块钱),[¥$]\s*[0-9]|ops=and:1'),
        ('chk_defgeo_frozen_reason_subject', 'public.defgeo_xiaobang_frozen_reasons', 'c|cols=subject_kind|fk=-|fkcols=-|upd= |del= |valid=true|lits=media_decision_snapshot,quote_snapshot,report_snapshot|ops==:1,any:1'),
        ('chk_defgeo_frozen_reason_text_nonempty', 'public.defgeo_xiaobang_frozen_reasons', 'c|cols=public_text|fk=-|fkcols=-|upd= |del= |valid=true|lits=0|ops=>:1'),
        ('chk_defgeo_proj_adjudication', 'public.defgeo_monitoring_defensive_projections', 'c|cols=adjudication_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=ai_adjudicated,human_adjudicated,not_required,pending_review|ops==:1,any:1'),
        ('chk_defgeo_proj_adjudicator_trace', 'public.defgeo_monitoring_defensive_projections', 'c|cols=adjudicated_by,adjudication_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=ai:%@%,ai_adjudicated,human:%,human_adjudicated,not_required,pending_review|ops==:3,and:3,any:1,is:1,null:1,or:2'),
        ('chk_defgeo_proj_completeness', 'public.defgeo_monitoring_defensive_projections', 'c|cols=completeness|fk=-|fkcols=-|upd= |del= |valid=true|lits=complete,not_evaluable,partial|ops==:1,any:1'),
        ('chk_defgeo_proj_confidence_range', 'public.defgeo_monitoring_defensive_projections', 'c|cols=confidence|fk=-|fkcols=-|upd= |del= |valid=true|lits=0.0,1.0|ops=<=:1,>=:1,and:1,is:1,null:1,or:1'),
        ('chk_defgeo_proj_conflict_pair', 'public.defgeo_monitoring_defensive_projections', 'c|cols=conflicting_facts,has_conflict|fk=-|fkcols=-|upd= |del= |valid=true|lits=1|ops==:1,is:1,not:1,null:1'),
        ('chk_defgeo_proj_identity_state', 'public.defgeo_monitoring_defensive_projections', 'c|cols=identity_state|fk=-|fkcols=-|upd= |del= |valid=true|lits=ambiguous,confirmed,misidentified,not_evaluated|ops==:1,any:1'),
        ('chk_defgeo_proj_support_level', 'public.defgeo_monitoring_defensive_projections', 'c|cols=support_level|fk=-|fkcols=-|upd= |del= |valid=true|lits=corroborated,inferred,unknown,unsupported|ops==:1,any:1'),
        ('chk_defgeo_renewal_comparability', 'public.defgeo_renewal_recommendations', 'c|cols=comparability_level|fk=-|fkcols=-|upd= |del= |valid=true|lits=full,none,partial|ops==:1,any:1'),
        ('chk_defgeo_renewal_level', 'public.defgeo_renewal_recommendations', 'c|cols=level|fk=-|fkcols=-|upd= |del= |valid=true|lits=insufficient_basis,renew_recommended,renew_with_adjustment|ops==:1,any:1'),
        ('chk_defgeo_renewal_none_is_insufficient', 'public.defgeo_renewal_recommendations', 'c|cols=comparability_level,level|fk=-|fkcols=-|upd= |del= |valid=true|lits=insufficient_basis,none|ops=<>:1,=:1,or:1'),
        ('chk_defgeo_renewal_six_signals', 'public.defgeo_renewal_recommendations', 'c|cols=signals|fk=-|fkcols=-|upd= |del= |valid=true|lits=6,array|ops==:2,and:1'),
        ('chk_defgeo_snapshot_revision_positive', 'public.defgeo_report_snapshots', 'c|cols=revision|fk=-|fkcols=-|upd= |del= |valid=true|lits=1|ops=>=:1'),
        ('chk_defgeo_snapshot_state', 'public.defgeo_report_snapshots', 'c|cols=state|fk=-|fkcols=-|upd= |del= |valid=true|lits=failed,no_conclusion,partial,processing,ready|ops==:1,any:1'),
        ('chk_defgeo_snapshot_window_order', 'public.defgeo_report_snapshots', 'c|cols=sampling_window_end,sampling_window_start|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops=>=:1'),
        ('uq_defgeo_attempt_cell_ordinal', 'public.defgeo_monitoring_attempts', 'u|cols=attempt_ordinal,plan_cell_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops='),
        ('uq_defgeo_frozen_reason_ref', 'public.defgeo_xiaobang_frozen_reasons', 'u|cols=reason_ref|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops='),
        ('uq_defgeo_renewal_id', 'public.defgeo_renewal_recommendations', 'u|cols=recommendation_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops='),
        ('uq_defgeo_snapshot_revision', 'public.defgeo_report_snapshots', 'u|cols=report_snapshot_id,revision|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[046] 约束的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_constraint(r.cname, r.tname), c.convalidated
          INTO actual, ok
          FROM pg_constraint c
         WHERE c.conname = r.cname AND c.conrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION '[046] 约束 % 不在 % 上(可能被同名约束挡在了别的表)',
                r.cname, r.tname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[046] 约束 % 语义身份漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[046] 约束 % 是 NOT VALID —— 存量行从没被验过,守卫只守未来', r.cname;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('idx_defgeo_attempt_cell', 'public.defgeo_monitoring_attempts', 'live=true|unique=false|cols=attempt_ordinal,plan_cell_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_attempt_run_authority', 'public.defgeo_monitoring_attempts', 'live=true|unique=false|cols=run_authority_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_attempt_tenant_brand', 'public.defgeo_monitoring_attempts', 'live=true|unique=false|cols=brand_id,tenant_owner_user_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_frozen_reason_subject', 'public.defgeo_xiaobang_frozen_reasons', 'live=true|unique=false|cols=subject_kind,subject_ref|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_frozen_reason_tenant_brand', 'public.defgeo_xiaobang_frozen_reasons', 'live=true|unique=false|cols=brand_id,tenant_owner_user_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_proj_result', 'public.defgeo_monitoring_defensive_projections', 'live=true|unique=false|cols=monitoring_result_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_proj_tenant_brand', 'public.defgeo_monitoring_defensive_projections', 'live=true|unique=false|cols=brand_id,tenant_owner_user_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_renewal_tenant_brand', 'public.defgeo_renewal_recommendations', 'live=true|unique=false|cols=brand_id,tenant_owner_user_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_snapshot_diagnosis', 'public.defgeo_report_snapshots', 'live=true|unique=false|cols=diagnosis_id|am=btree|predlits=|predops=|exprlits='),
        ('idx_defgeo_snapshot_tenant_brand', 'public.defgeo_report_snapshots', 'live=true|unique=false|cols=brand_id,tenant_owner_user_id|am=btree|predlits=|predops=|exprlits='),
        ('uq_defgeo_attempt_single_inflight', 'public.defgeo_monitoring_attempts', 'live=true|unique=true|cols=plan_cell_id|am=btree|predlits=|predops=is:1,null:1|exprlits=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[046] 索引的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_index(r.cname, r.tname),
               (i.indisvalid AND i.indisready) INTO actual, ok
          FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
         WHERE c.relname = r.cname AND i.indrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[046] 索引 % 不在 % 上 —— 可能被**别的表上的同名索引**挡掉了'
                '(CREATE INDEX IF NOT EXISTS 按名判存不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT t2.relname FROM pg_class c2
                            JOIN pg_index i2 ON i2.indexrelid = c2.oid
                            JOIN pg_class t2 ON t2.oid = i2.indrelid
                           WHERE c2.relname = r.cname
                             AND c2.relnamespace = 'public'::regnamespace), '(没有同名索引)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[046] 索引 % 定义漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[046] 索引 % 的 indisvalid/indisready 不成立 —— '
                'CREATE INDEX CONCURRENTLY 失败留下的壳子文本与正品一模一样,'
                '但它不保证唯一性', r.cname;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('trg_defgeo_attempt_terminal_immutable', 'public.defgeo_monitoring_attempts', 'enabled=O|type=19|fn=f50bb71c0dc01f48e180b250bff7318f'),
        ('trg_defgeo_snapshot_no_inplace_state_change', 'public.defgeo_report_snapshots', 'enabled=O|type=19|fn=5c9cbaaf65ddc8cab32a8e990935b253')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[046] 触发器的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_trigger(r.cname, r.tname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[046] 触发器 % 不在 % 上 —— 「按名判存」会被**别的表上的同名触发器**'
                '骗过(pg_trigger.tgname 不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT c2.relname FROM pg_trigger g2
                            JOIN pg_class c2 ON c2.oid = g2.tgrelid
                           WHERE g2.tgname = r.cname AND NOT g2.tgisinternal
                           LIMIT 1), '(没有同名触发器)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[046] 触发器 % 的定义/启用状态/函数体与预期不符 —— '
                '同名放行触发器、DISABLE 掉的触发器、被换成 RETURN NEW 的函数体,'
                '三种都长成「它在」的样子;期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
    END LOOP;
    RAISE NOTICE '[046] exact schema readiness 通过(1 列 + 28 约束 + 11 索引 + 2 触发器逐字对上)';
END $readiness046$;
-- @readiness-end 046
