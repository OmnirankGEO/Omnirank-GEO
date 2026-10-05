-- ============================================================================
-- 040 · 防御型 GEO · 问题计划(question plan)不可变 revision 链
--
-- 授权:Review-CTO 2026-08-21 裁定 RFC ①②③ 全批
--       (docs/AI-CONTEXT/DEFGEO_WP0_RFC_NEW_TABLES_2026-08-21.md)
-- 规格:§15.2 QuestionPlanPreview / §16.1-16.3 / REV-01,02,03,10,11,12,13
--
-- 🔴 additive-only:只 CREATE 新表 + 新索引,**不 ALTER 任何既有表**。
-- 🔴 体内零 DML:本文件无 INSERT/UPDATE/DELETE。
--    prestart 每次部署**无条件重放全部迁移**(无追踪表),迁移里的 DML 是常驻地雷。
--    需要 backfill 就落一次性脚本 + 幂等判据,不写在这里。
-- 🔴 重放安全:全部 IF NOT EXISTS / 走 pg_constraint、pg_trigger 存在性判断。
--    2× 复跑第二遍全 no-op。
-- 🔴 漏跑后果**响亮**(刻意):新端点显式 INSERT 本表 → UndefinedTable 当场抛、端点受控失败。
--    静默返空会让用户看到「已为你生成题单」而其实什么都没存 —— 假结论比"暂时不可用"坏得多。
-- 🔴 顺序无依赖:刻意**不加 FK** 到 brands/users ——
--    归属校验必须**每请求现做**,不能靠 FK 假装做过;
--    而 FK 在软删/租户迁移时是部署期地雷。归属列只作过滤与审计。
--
-- ⚠️ 命名去混淆(§0.5.1-7 · Review-CTO 批准):
--    本表用 `question_identity_key`,**不是** `question_key`。
--    现役 geo_article_target_question_snapshots.question_key 是
--    CHARACTER(64) **内容哈希** + 全局 UNIQUE;改题文它就变。
--    本表的 identity key 语义**相反**:改题文它**不变**(REV-02),
--    变的是 question_revision。两者同名会让"改一个字"变成"换了一道题"。
-- ============================================================================

CREATE TABLE IF NOT EXISTS defgeo_question_plans (
    id                    BIGSERIAL PRIMARY KEY,

    -- 计划身份:同一 plan_id 的多个 revision 构成不可变链。
    plan_id               UUID        NOT NULL,
    plan_revision         INTEGER     NOT NULL,

    -- 归属(每请求现做校验的过滤依据 · 不是权限本身)
    tenant_owner_user_id  INTEGER     NOT NULL,
    brand_id              INTEGER     NOT NULL,
    profile_revision_id   TEXT        NOT NULL,

    mode                  VARCHAR(16) NOT NULL,
    question_set_version  VARCHAR(80) NOT NULL,

    -- canonical hash 覆盖 brand/profile revision + 全部题目(含 ordinal/mode side/family)
    canonical_hash        CHARACTER(64) NOT NULL,

    -- 冻结题单全文。整块不可变(见下方 trigger)。
    frozen_payload        JSONB       NOT NULL,

    -- 计数从 payload 投影出来,单独落列只为查询;判据核对二者一致。
    defensive_count       INTEGER     NOT NULL DEFAULT 0,
    offensive_count       INTEGER     NOT NULL DEFAULT 0,
    total_count           INTEGER     NOT NULL DEFAULT 0,

    -- 幂等:同 clientRequestId + 同**请求内容**重放返回同一 revision。
    -- 🔴 这里挂的是 request_content_hash 而**不是** canonical_hash:
    --    canonical_hash 覆盖 question_identity_key,而身份键由每次新生成的 plan_id 派生,
    --    同一份题单两次提交会得到两个不同的 canonical_hash ⇒ 唯一约束永不命中 ⇒
    --    安静地建出第二份题单。(真 HTTP 判据当场抓到过这个,纯函数层看不出来。)
    client_request_id     TEXT        NOT NULL,
    request_content_hash  CHARACTER(64) NOT NULL,

    expires_at            TIMESTAMPTZ NOT NULL,
    superseded_by_revision INTEGER,
    created_by_user_id    INTEGER     NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 约束(全部走 pg_constraint 存在性判断 · 重放安全)──────────────────────
DO $$
BEGIN
    IF to_regclass('defgeo_question_plans') IS NULL THEN RETURN; END IF;

    -- (plan_id, plan_revision) 唯一 —— revision 链的骨架
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='uq_defgeo_qplan_revision'
                      AND conrelid='defgeo_question_plans'::regclass) THEN
        ALTER TABLE defgeo_question_plans
            ADD CONSTRAINT uq_defgeo_qplan_revision UNIQUE (plan_id, plan_revision);
    END IF;

    -- mode 闭集
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_qplan_mode'
                      AND conrelid='defgeo_question_plans'::regclass) THEN
        ALTER TABLE defgeo_question_plans
            ADD CONSTRAINT chk_defgeo_qplan_mode
            CHECK (mode IN ('defensive','offensive','hybrid'));
    END IF;

    -- revision 从 1 起递增
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_qplan_revision_positive'
                      AND conrelid='defgeo_question_plans'::regclass) THEN
        ALTER TABLE defgeo_question_plans
            ADD CONSTRAINT chk_defgeo_qplan_revision_positive CHECK (plan_revision >= 1);
    END IF;

    -- 计数守恒:total = defensive + offensive,且均非负。
    -- 逐项对不代表整体对 —— 这条挡的正是"两侧计数各自看着合理但加起来不等于总数"。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='chk_defgeo_qplan_counts'
                      AND conrelid='defgeo_question_plans'::regclass) THEN
        ALTER TABLE defgeo_question_plans
            ADD CONSTRAINT chk_defgeo_qplan_counts
            CHECK (defensive_count >= 0 AND offensive_count >= 0
                   AND total_count = defensive_count + offensive_count);
    END IF;

    -- 幂等根:同一 (tenant, client_request_id, canonical_hash) 只可能有一行。
    -- 换 HTTP key 但同 clientRequestId/canonical payload 仍返回同一 plan/revision(§15.2)。
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname='uq_defgeo_qplan_idempotency'
                      AND conrelid='defgeo_question_plans'::regclass) THEN
        ALTER TABLE defgeo_question_plans
            ADD CONSTRAINT uq_defgeo_qplan_idempotency
            UNIQUE (tenant_owner_user_id, client_request_id, request_content_hash);
    END IF;
END $$;

-- @index-guard idx_defgeo_qplan_brand_latest ON defgeo_question_plans plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_defgeo_qplan_brand_latest' AND i.indrelid = to_regclass('public.defgeo_question_plans')) THEN
        NULL;  -- 已在 public.defgeo_question_plans 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_defgeo_qplan_brand_latest' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_defgeo_qplan_brand_latest 已存在但不在 public.defgeo_question_plans 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_defgeo_qplan_brand_latest' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_defgeo_qplan_brand_latest ON public.defgeo_question_plans (brand_id, plan_id, plan_revision DESC);
    END IF;
END $idxguard$;

-- ── 🔴 不可变强制:已签发的 revision 永不原地 UPDATE(REV-01)──────────────
--
-- 为什么用 trigger 而不是只在应用层守:
--   应用层守卫挡得住我们自己的代码路径,挡不住 psql 手改、别的包顺手写、
--   以及以后某个 ORM 的 upsert。REV-01 是 H0-DATA,只有 DB 级才是真的挡住。
--   应用层守卫仍然要有(见 services/defensive_geo/question_plan_store.py),
--   两层各自配判据 —— 但**这一层是承重的**。
--
-- 只放行 superseded_by_revision 从 NULL 变成非 NULL 这一种迁移
-- (标记"已被新 revision 取代"不改变任何已签发事实,且单向不可逆)。
CREATE OR REPLACE FUNCTION defgeo_qplan_forbid_mutation() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'defgeo_question_plans 不可删除(REV-01):已签发的 revision 是审计事实,'
            '取代请新建 revision 并回填 superseded_by_revision'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- 唯一允许的原地变更:NULL → 非 NULL 的 superseded 标记
    IF OLD.superseded_by_revision IS NULL
       AND NEW.superseded_by_revision IS NOT NULL
       AND ROW(NEW.plan_id, NEW.plan_revision, NEW.tenant_owner_user_id, NEW.brand_id,
               NEW.profile_revision_id, NEW.mode, NEW.question_set_version,
               NEW.canonical_hash, NEW.frozen_payload, NEW.defensive_count,
               NEW.offensive_count, NEW.total_count, NEW.client_request_id,
               NEW.request_content_hash,
               NEW.expires_at, NEW.created_by_user_id, NEW.created_at)
         IS NOT DISTINCT FROM
           ROW(OLD.plan_id, OLD.plan_revision, OLD.tenant_owner_user_id, OLD.brand_id,
               OLD.profile_revision_id, OLD.mode, OLD.question_set_version,
               OLD.canonical_hash, OLD.frozen_payload, OLD.defensive_count,
               OLD.offensive_count, OLD.total_count, OLD.client_request_id,
               OLD.request_content_hash,
               OLD.expires_at, OLD.created_by_user_id, OLD.created_at)
    THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'defgeo_question_plans 已签发 revision 不可原地 UPDATE(REV-01):'
        'plan_id=% revision=% —— 改题只能生成 superseding revision',
        OLD.plan_id, OLD.plan_revision
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF to_regclass('defgeo_question_plans') IS NULL THEN RETURN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname='trg_defgeo_qplan_immutable'
                      AND tgrelid='defgeo_question_plans'::regclass) THEN
        CREATE TRIGGER trg_defgeo_qplan_immutable
            BEFORE UPDATE OR DELETE ON defgeo_question_plans
            FOR EACH ROW EXECUTE FUNCTION defgeo_qplan_forbid_mutation();
    END IF;
END $$;

-- ── 反查自证(不是 DML · 只 RAISE)────────────────────────────────────────
DO $$
DECLARE missing TEXT := '';
BEGIN
    IF to_regclass('defgeo_question_plans') IS NULL THEN
        RAISE EXCEPTION '[040] defgeo_question_plans 未建成';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_defgeo_qplan_revision'
                        AND conrelid = 'public.defgeo_question_plans'::regclass) THEN
        missing := missing || ' uq_defgeo_qplan_revision'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_defgeo_qplan_mode'
                        AND conrelid = 'public.defgeo_question_plans'::regclass) THEN
        missing := missing || ' chk_defgeo_qplan_mode'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_defgeo_qplan_counts'
                        AND conrelid = 'public.defgeo_question_plans'::regclass) THEN
        missing := missing || ' chk_defgeo_qplan_counts'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_defgeo_qplan_idempotency'
                        AND conrelid = 'public.defgeo_question_plans'::regclass) THEN
        missing := missing || ' uq_defgeo_qplan_idempotency'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_defgeo_qplan_immutable') THEN
        missing := missing || ' trg_defgeo_qplan_immutable'; END IF;
    IF missing <> '' THEN
        RAISE EXCEPTION '[040] 缺少约束/触发器:%', missing;
    END IF;
    RAISE NOTICE '[040] defgeo_question_plans 反查通过(表/4 约束/不可变 trigger 全在)';
END $$;

-- @readiness-begin 040
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT 5 条 + @index-guard 1 个 + CREATE TRIGGER 1 个 + ADD COLUMN 0 列),
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

DO $readiness040$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('chk_defgeo_qplan_counts', 'public.defgeo_question_plans', 'c|cols=defensive_count,offensive_count,total_count|fk=-|fkcols=-|upd= |del= |valid=true|lits=0|ops==:1,>=:2,and:2'),
        ('chk_defgeo_qplan_mode', 'public.defgeo_question_plans', 'c|cols=mode|fk=-|fkcols=-|upd= |del= |valid=true|lits=defensive,hybrid,offensive|ops==:1,any:1'),
        ('chk_defgeo_qplan_revision_positive', 'public.defgeo_question_plans', 'c|cols=plan_revision|fk=-|fkcols=-|upd= |del= |valid=true|lits=1|ops=>=:1'),
        ('uq_defgeo_qplan_idempotency', 'public.defgeo_question_plans', 'u|cols=client_request_id,request_content_hash,tenant_owner_user_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops='),
        ('uq_defgeo_qplan_revision', 'public.defgeo_question_plans', 'u|cols=plan_id,plan_revision|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[040] 约束的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_constraint(r.cname, r.tname), c.convalidated
          INTO actual, ok
          FROM pg_constraint c
         WHERE c.conname = r.cname AND c.conrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION '[040] 约束 % 不在 % 上(可能被同名约束挡在了别的表)',
                r.cname, r.tname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[040] 约束 % 语义身份漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[040] 约束 % 是 NOT VALID —— 存量行从没被验过,守卫只守未来', r.cname;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('idx_defgeo_qplan_brand_latest', 'public.defgeo_question_plans', 'live=true|unique=false|cols=brand_id,plan_id,plan_revision|am=btree|predlits=|predops=|exprlits=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[040] 索引的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_index(r.cname, r.tname),
               (i.indisvalid AND i.indisready) INTO actual, ok
          FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
         WHERE c.relname = r.cname AND i.indrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[040] 索引 % 不在 % 上 —— 可能被**别的表上的同名索引**挡掉了'
                '(CREATE INDEX IF NOT EXISTS 按名判存不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT t2.relname FROM pg_class c2
                            JOIN pg_index i2 ON i2.indexrelid = c2.oid
                            JOIN pg_class t2 ON t2.oid = i2.indrelid
                           WHERE c2.relname = r.cname
                             AND c2.relnamespace = 'public'::regnamespace), '(没有同名索引)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[040] 索引 % 定义漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[040] 索引 % 的 indisvalid/indisready 不成立 —— '
                'CREATE INDEX CONCURRENTLY 失败留下的壳子文本与正品一模一样,'
                '但它不保证唯一性', r.cname;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('trg_defgeo_qplan_immutable', 'public.defgeo_question_plans', 'enabled=O|type=27|fn=68ff950d289ed9a274183bff73649e20')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[040] 触发器的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_trigger(r.cname, r.tname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[040] 触发器 % 不在 % 上 —— 「按名判存」会被**别的表上的同名触发器**'
                '骗过(pg_trigger.tgname 不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT c2.relname FROM pg_trigger g2
                            JOIN pg_class c2 ON c2.oid = g2.tgrelid
                           WHERE g2.tgname = r.cname AND NOT g2.tgisinternal
                           LIMIT 1), '(没有同名触发器)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[040] 触发器 % 的定义/启用状态/函数体与预期不符 —— '
                '同名放行触发器、DISABLE 掉的触发器、被换成 RETURN NEW 的函数体,'
                '三种都长成「它在」的样子;期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
    END LOOP;
    RAISE NOTICE '[040] exact schema readiness 通过(0 列 + 5 约束 + 1 索引 + 1 触发器逐字对上)';
END $readiness040$;
-- @readiness-end 040
