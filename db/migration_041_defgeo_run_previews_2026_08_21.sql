-- ============================================================================
-- 041 · 防御型 GEO · 正式诊断运行预览(run preview)· 不可变字节 + live sidecar
--
-- 授权:Review-CTO 2026-08-21 裁定 RFC ①②③ 全批
-- 规格:§15.3 DiagnosisRunPreview / §3.5 / DIA-FIN-02,09,10,11,13
--
-- 🔴 additive-only · 体内零 DML · 重放安全 · 刻意不加 FK(归属每请求现做)
--    —— 逐条同 040,理由不再重复。
--
-- 🔴🔴 **禁止第二套 settlement enum**(§3.5 原文,已自诺,此处写成 schema 事实):
--    本表的 `lifecycle` **只有** open|expired|consumed 三值,描述的是
--    「这份 preview 还能不能用」,**不是**钱走到哪、也不是 run 跑到哪。
--    confirm 之后唯一可写的 settlement authority 仍是现役
--    `diagnosis_runs.run_status`;runState/fundingState 由
--    services/defensive_geo/run_status_projection.py 只读投影出来,不落库。
--    本表**没有**、也不许有任何 status/state/funding_state/run_state 列。
--    判据 test_no_second_settlement_enum 逐列扫描本表并对着
--    chk_diag_runs_status 的值表求交,命中即红。
--
-- 🔴 preview 字节不可变(§15.3):`frozen_payload` / `canonical_hash` 一经写入永不变。
--    余额、审批、expiry 都**不**写回本表 —— 它们是逐次授权重算的 live sidecar,
--    由端点即时计算,不持久化。持久化它们就等于让 frozen bytes 随环境漂移。
--    唯一允许的原地变更:lifecycle open → consumed/expired(+ 相应 consumed_* 回填)。
-- ============================================================================

CREATE TABLE IF NOT EXISTS defgeo_diagnosis_run_previews (
    preview_id            UUID        PRIMARY KEY,

    -- 归属(过滤与审计 · 不是权限本身)
    tenant_owner_user_id  INTEGER     NOT NULL,
    brand_id              INTEGER     NOT NULL,
    created_by_user_id    INTEGER     NOT NULL,

    -- 引用的题单(逻辑引用 · 无 FK,理由同上)
    question_plan_id      UUID        NOT NULL,
    question_plan_revision INTEGER    NOT NULL,
    question_plan_hash    CHARACTER(64) NOT NULL,
    profile_revision_id   TEXT        NOT NULL,

    campaign_mode         VARCHAR(16) NOT NULL,

    -- 🔴 冻结字节:整份 DiagnosisRunPreview。永不变。
    frozen_payload        JSONB       NOT NULL,
    canonical_hash        CHARACTER(64) NOT NULL,

    -- 冻结价(§3.5:confirm 后 worker 只读 frozen exact points,禁 current reprice)
    feature_code          VARCHAR(64) NOT NULL,
    pricing_catalog_version VARCHAR(80) NOT NULL,
    base_points           INTEGER     NOT NULL,
    extra_points          INTEGER     NOT NULL,
    exact_total_points    INTEGER     NOT NULL,

    -- 冻结付款方(live 侧只重算 approvalState/余额,payer 本身不许漂)
    funding_policy        VARCHAR(32) NOT NULL,
    principal_kind        VARCHAR(32) NOT NULL,
    sponsor_policy_ref    TEXT,
    approval_requirement  VARCHAR(16) NOT NULL,

    planned_cells         INTEGER     NOT NULL,

    -- 🔴 三值闭集。**不是** settlement 状态。
    lifecycle             VARCHAR(16) NOT NULL DEFAULT 'open',

    -- consumed 时必须指得回原 command(§15.3:consumed 只重放原 command)
    consumed_command_id   TEXT,
    consumed_at           TIMESTAMPTZ,

    -- 传输级幂等:同 key + 同 canonical request 重放同 preview;异请求 409
    idempotency_key       TEXT        NOT NULL,
    canonical_request_hash CHARACTER(64) NOT NULL,

    expires_at            TIMESTAMPTZ NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF to_regclass('defgeo_diagnosis_run_previews') IS NULL THEN RETURN; END IF;

    -- 🔴 lifecycle 三值 CHECK(Review-CTO 附带条件)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_preview_lifecycle'
                      AND conrelid='defgeo_diagnosis_run_previews'::regclass) THEN
        ALTER TABLE defgeo_diagnosis_run_previews
            ADD CONSTRAINT chk_defgeo_preview_lifecycle
            CHECK (lifecycle IN ('open','expired','consumed'));
    END IF;

    -- consumed ⟺ 必须有 command 与时间;非 consumed ⟺ 两者必须为空。
    -- 双向都锁:只锁一半会让 "consumed 但指不回 command" 或
    -- "open 却挂着 command" 两种半状态各自溜过去。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_preview_consumed_pair'
                      AND conrelid='defgeo_diagnosis_run_previews'::regclass) THEN
        ALTER TABLE defgeo_diagnosis_run_previews
            ADD CONSTRAINT chk_defgeo_preview_consumed_pair
            CHECK (
                (lifecycle = 'consumed'
                 AND consumed_command_id IS NOT NULL AND consumed_at IS NOT NULL)
                OR
                (lifecycle <> 'consumed'
                 AND consumed_command_id IS NULL AND consumed_at IS NULL)
            );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_preview_mode'
                      AND conrelid='defgeo_diagnosis_run_previews'::regclass) THEN
        ALTER TABLE defgeo_diagnosis_run_previews
            ADD CONSTRAINT chk_defgeo_preview_mode
            CHECK (campaign_mode IN ('defensive','offensive','hybrid'));
    END IF;

    -- 资金四格:policy 闭集 + 算术守恒 + 非负。
    -- 与 services/defensive_geo/funding_projection.py 的矩阵各写一次是**有意**的:
    -- 两处同时改错的概率远低于一处;判据核对二者一致。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_preview_funding_policy'
                      AND conrelid='defgeo_diagnosis_run_previews'::regclass) THEN
        ALTER TABLE defgeo_diagnosis_run_previews
            ADD CONSTRAINT chk_defgeo_preview_funding_policy
            CHECK (funding_policy IN ('personal_wallet','organization_budget',
                                      'admin_platform_ledger','sponsor_platform_ledger'));
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_preview_points'
                      AND conrelid='defgeo_diagnosis_run_previews'::regclass) THEN
        ALTER TABLE defgeo_diagnosis_run_previews
            ADD CONSTRAINT chk_defgeo_preview_points
            CHECK (base_points >= 0 AND extra_points >= 0
                   AND exact_total_points = base_points + extra_points);
    END IF;

    -- 零计划格不得启动(§15.4:progressPct 分母)
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_preview_planned_cells'
                      AND conrelid='defgeo_diagnosis_run_previews'::regclass) THEN
        ALTER TABLE defgeo_diagnosis_run_previews
            ADD CONSTRAINT chk_defgeo_preview_planned_cells CHECK (planned_cells >= 1);
    END IF;

    -- 传输幂等根:同 (tenant, key, canonical request hash) 唯一 → DIA-FIN-02 的 20 并发恰一
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='uq_defgeo_preview_idempotency'
                      AND conrelid='defgeo_diagnosis_run_previews'::regclass) THEN
        ALTER TABLE defgeo_diagnosis_run_previews
            ADD CONSTRAINT uq_defgeo_preview_idempotency
            UNIQUE (tenant_owner_user_id, idempotency_key, canonical_request_hash);
    END IF;
END $$;

-- @index-guard idx_defgeo_preview_plan ON defgeo_diagnosis_run_previews plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_preview_plan' AND i.indrelid = to_regclass('public.defgeo_diagnosis_run_previews')) THEN
        NULL;  -- 已在 public.defgeo_diagnosis_run_previews 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_preview_plan' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_preview_plan 已存在但不在 public.defgeo_diagnosis_run_previews 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_preview_plan' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_preview_plan ON public.defgeo_diagnosis_run_previews (question_plan_id, question_plan_revision);
    END IF;
END $idxguard$;

-- ── 🔴 frozen bytes 不可变强制 ────────────────────────────────────────────
--
-- 允许的原地变更**只有一种**:lifecycle 从 open 前进到 expired/consumed,
-- 并同时回填 consumed_*。其余任何列改一个字节都拒。
-- 特别地:frozen_payload / canonical_hash / 四格资金 / 冻结价 全部锁死 ——
-- 「preview 随余额或审批变化」正是 §19 变异 134 点名的形态。
CREATE OR REPLACE FUNCTION defgeo_preview_forbid_mutation() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'defgeo_diagnosis_run_previews 不可删除:preview 是资金审计链的一环'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- 冻结面逐列比对(不用 to_jsonb 整行比 —— 那样 lifecycle 一变就整行不同,锁不住细节)
    IF ROW(NEW.preview_id, NEW.tenant_owner_user_id, NEW.brand_id, NEW.created_by_user_id,
           NEW.question_plan_id, NEW.question_plan_revision, NEW.question_plan_hash,
           NEW.profile_revision_id, NEW.campaign_mode, NEW.frozen_payload, NEW.canonical_hash,
           NEW.feature_code, NEW.pricing_catalog_version, NEW.base_points, NEW.extra_points,
           NEW.exact_total_points, NEW.funding_policy, NEW.principal_kind,
           NEW.sponsor_policy_ref, NEW.approval_requirement, NEW.planned_cells,
           NEW.idempotency_key, NEW.canonical_request_hash, NEW.expires_at, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.preview_id, OLD.tenant_owner_user_id, OLD.brand_id, OLD.created_by_user_id,
           OLD.question_plan_id, OLD.question_plan_revision, OLD.question_plan_hash,
           OLD.profile_revision_id, OLD.campaign_mode, OLD.frozen_payload, OLD.canonical_hash,
           OLD.feature_code, OLD.pricing_catalog_version, OLD.base_points, OLD.extra_points,
           OLD.exact_total_points, OLD.funding_policy, OLD.principal_kind,
           OLD.sponsor_policy_ref, OLD.approval_requirement, OLD.planned_cells,
           OLD.idempotency_key, OLD.canonical_request_hash, OLD.expires_at, OLD.created_at)
    THEN
        RAISE EXCEPTION
            'defgeo_diagnosis_run_previews 冻结字节不可变(§15.3):preview_id=% —— '
            'preview 永不随余额/审批/expiry 改变;要改只能新建 preview',
            OLD.preview_id
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- lifecycle 只许从 open 单向前进,终态不可回退、不可互转。
    -- 「expired/consumed 再开放」是 §19 变异 96/134 点名的形态。
    IF OLD.lifecycle <> NEW.lifecycle AND OLD.lifecycle <> 'open' THEN
        RAISE EXCEPTION
            'defgeo preview lifecycle 终态不可变更:% → %(preview_id=%)',
            OLD.lifecycle, NEW.lifecycle, OLD.preview_id
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF to_regclass('defgeo_diagnosis_run_previews') IS NULL THEN RETURN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname='trg_defgeo_preview_immutable'
                      AND tgrelid='defgeo_diagnosis_run_previews'::regclass) THEN
        CREATE TRIGGER trg_defgeo_preview_immutable
            BEFORE UPDATE OR DELETE ON defgeo_diagnosis_run_previews
            FOR EACH ROW EXECUTE FUNCTION defgeo_preview_forbid_mutation();
    END IF;
END $$;

-- ── 反查自证 ─────────────────────────────────────────────────────────────
DO $$
DECLARE missing TEXT := '';
        forbidden TEXT := '';
BEGIN
    IF to_regclass('defgeo_diagnosis_run_previews') IS NULL THEN
        RAISE EXCEPTION '[041] defgeo_diagnosis_run_previews 未建成';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_defgeo_preview_lifecycle'
                        AND conrelid = 'public.defgeo_diagnosis_run_previews'::regclass) THEN
        missing := missing || ' chk_defgeo_preview_lifecycle'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_defgeo_preview_consumed_pair'
                        AND conrelid = 'public.defgeo_diagnosis_run_previews'::regclass) THEN
        missing := missing || ' chk_defgeo_preview_consumed_pair'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_defgeo_preview_points'
                        AND conrelid = 'public.defgeo_diagnosis_run_previews'::regclass) THEN
        missing := missing || ' chk_defgeo_preview_points'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_defgeo_preview_idempotency'
                        AND conrelid = 'public.defgeo_diagnosis_run_previews'::regclass) THEN
        missing := missing || ' uq_defgeo_preview_idempotency'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_defgeo_preview_immutable') THEN
        missing := missing || ' trg_defgeo_preview_immutable'; END IF;
    IF missing <> '' THEN
        RAISE EXCEPTION '[041] 缺少约束/触发器:%', missing;
    END IF;

    -- 🔴 禁第二套 settlement enum:本表不许出现任何 settlement 语义列。
    -- 🔴 [2026-08-27] ``table_schema = 'public'`` 不是装饰:information_schema
    --    是**跨 schema** 的,不带它等于问「**任何** schema 里有没有这几列」。
    --    别的 schema 里一张同名表就能替真表回答这个问题 —— 两个方向都错:
    --    它那边有 run_status ⇒ 这里误 RAISE(真表干净却发不了车);
    --    真表脏而它那边干净时,string_agg 仍会把脏列带出来,但归属已经说不清。
    --    (同族:DROP INDEX 不绑表 / CREATE INDEX IF NOT EXISTS 按名判存。)
    SELECT string_agg(column_name, ' ') INTO forbidden
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'defgeo_diagnosis_run_previews'
       AND column_name IN ('run_status','funding_state','run_state','settlement_state','billing_mode');
    IF forbidden IS NOT NULL THEN
        RAISE EXCEPTION
            '[041] 本表出现 settlement 语义列(%) —— §3.5 禁第二套 settlement enum,'
            'confirm 后唯一 settlement authority 是 diagnosis_runs.run_status', forbidden;
    END IF;

    RAISE NOTICE '[041] defgeo_diagnosis_run_previews 反查通过(表/6 约束/不可变 trigger/无第二套 enum)';
END $$;

-- @readiness-begin 041
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT 7 条 + @index-guard 1 个 + CREATE TRIGGER 1 个 + ADD COLUMN 0 列),
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

DO $readiness041$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('chk_defgeo_preview_consumed_pair', 'public.defgeo_diagnosis_run_previews', 'c|cols=consumed_at,consumed_command_id,lifecycle|fk=-|fkcols=-|upd= |del= |valid=true|lits=consumed|ops=<>:1,=:1,and:4,is:4,not:2,null:4,or:1'),
        ('chk_defgeo_preview_funding_policy', 'public.defgeo_diagnosis_run_previews', 'c|cols=funding_policy|fk=-|fkcols=-|upd= |del= |valid=true|lits=admin_platform_ledger,organization_budget,personal_wallet,sponsor_platform_ledger|ops==:1,any:1'),
        ('chk_defgeo_preview_lifecycle', 'public.defgeo_diagnosis_run_previews', 'c|cols=lifecycle|fk=-|fkcols=-|upd= |del= |valid=true|lits=consumed,expired,open|ops==:1,any:1'),
        ('chk_defgeo_preview_mode', 'public.defgeo_diagnosis_run_previews', 'c|cols=campaign_mode|fk=-|fkcols=-|upd= |del= |valid=true|lits=defensive,hybrid,offensive|ops==:1,any:1'),
        ('chk_defgeo_preview_planned_cells', 'public.defgeo_diagnosis_run_previews', 'c|cols=planned_cells|fk=-|fkcols=-|upd= |del= |valid=true|lits=1|ops=>=:1'),
        ('chk_defgeo_preview_points', 'public.defgeo_diagnosis_run_previews', 'c|cols=base_points,exact_total_points,extra_points|fk=-|fkcols=-|upd= |del= |valid=true|lits=0|ops==:1,>=:2,and:2'),
        ('uq_defgeo_preview_idempotency', 'public.defgeo_diagnosis_run_previews', 'u|cols=canonical_request_hash,idempotency_key,tenant_owner_user_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[041] 约束的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_constraint(r.cname, r.tname), c.convalidated
          INTO actual, ok
          FROM pg_constraint c
         WHERE c.conname = r.cname AND c.conrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION '[041] 约束 % 不在 % 上(可能被同名约束挡在了别的表)',
                r.cname, r.tname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[041] 约束 % 语义身份漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[041] 约束 % 是 NOT VALID —— 存量行从没被验过,守卫只守未来', r.cname;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('idx_defgeo_preview_plan', 'public.defgeo_diagnosis_run_previews', 'live=true|unique=false|cols=question_plan_id,question_plan_revision|am=btree|predlits=|predops=|exprlits=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[041] 索引的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_index(r.cname, r.tname),
               (i.indisvalid AND i.indisready) INTO actual, ok
          FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
         WHERE c.relname = r.cname AND i.indrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[041] 索引 % 不在 % 上 —— 可能被**别的表上的同名索引**挡掉了'
                '(CREATE INDEX IF NOT EXISTS 按名判存不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT t2.relname FROM pg_class c2
                            JOIN pg_index i2 ON i2.indexrelid = c2.oid
                            JOIN pg_class t2 ON t2.oid = i2.indrelid
                           WHERE c2.relname = r.cname
                             AND c2.relnamespace = 'public'::regnamespace), '(没有同名索引)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[041] 索引 % 定义漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[041] 索引 % 的 indisvalid/indisready 不成立 —— '
                'CREATE INDEX CONCURRENTLY 失败留下的壳子文本与正品一模一样,'
                '但它不保证唯一性', r.cname;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('trg_defgeo_preview_immutable', 'public.defgeo_diagnosis_run_previews', 'enabled=O|type=27|fn=d109524bdb22e26f1d7be306b122b003')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[041] 触发器的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_trigger(r.cname, r.tname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[041] 触发器 % 不在 % 上 —— 「按名判存」会被**别的表上的同名触发器**'
                '骗过(pg_trigger.tgname 不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT c2.relname FROM pg_trigger g2
                            JOIN pg_class c2 ON c2.oid = g2.tgrelid
                           WHERE g2.tgname = r.cname AND NOT g2.tgisinternal
                           LIMIT 1), '(没有同名触发器)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[041] 触发器 % 的定义/启用状态/函数体与预期不符 —— '
                '同名放行触发器、DISABLE 掉的触发器、被换成 RETURN NEW 的函数体,'
                '三种都长成「它在」的样子;期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
    END LOOP;
    RAISE NOTICE '[041] exact schema readiness 通过(0 列 + 7 约束 + 1 索引 + 1 触发器逐字对上)';
END $readiness041$;
-- @readiness-end 041
