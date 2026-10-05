-- ============================================================================
-- 051 · 防御型 GEO 发布链结算守卫(工单B · Codex 终审 P0-4 / P1-5)
-- ============================================================================
-- 号段登记:051 归 WO_CODEX_P0_PUBLISH_FUNDING_2026-08-25(工单B);
--          050 归工单A;下一空 = 052。
--
-- 本迁移做两件事,都长在 defgeo_publish_commands 上:
--
-- ① [P0-4] 结算重试账两列 —— settlement_attempts / last_settlement_error
--    顺序反转之后,「物理结算没成功」不再写业务终态,而是进 retry/manual。
--    retry 必须有个**次数**,否则 commit 一直失败就变成另一种「无限延期挂钱」
--    (那正是 P1-2 在 outbox 上那条的同形)。诊断链的
--    `diagnosis_runs.settlement_attempts` / `last_settlement_error` 是同一件事,
--    这里逐字照抄它的语义,不发明第二套。
--    写点唯一:`store.bump_settlement_attempt`(不进 bump_status 的 allowed)。
--
-- ② [P1-5] provider_order_ref 上的**部分唯一索引**(NULL 豁免)
--    047 只加了列,没有唯一性。MHZ 那条上游反查按「标题 + 媒体」匹配、
--    第一条命中即返回 —— 同标题同媒体重发时,**旧订单号可能被绑到新 command**。
--    两条 command 拿到同一个 order_sn 之后,`publish_worker.poll_pending_outcomes`
--    会用同一行 `mhz_synced_orders` 的终态去推**两笔**冻结的结算。
--    这条索引是承重的那一半:应用层的预检会被并发绕过,唯一索引不会(TOCTOU)。
--    NULL 豁免是必须的 —— NULL 的意思是「还没外调过 / 上游没给单号」,
--    那不是身份冲突,把它算进唯一性等于只允许一条命令处于「还没外调」。
--
-- 🔴 additive-only:两列 IF NOT EXISTS + 可空/带默认;索引 IF NOT EXISTS 走
--    本仓 index-guard 形态(名字撞到别的宿主时 RAISE,不静默跳过)。
-- 🔴 体内零 DML:prestart 每次部署无条件重放全部迁移(无追踪表)。
-- 🔴 不碰 funding_state / command_state / canonical_publication_state 三个 CHECK。
-- ============================================================================

-- ── ① 结算重试账 ──────────────────────────────────────────────────────────
ALTER TABLE public.defgeo_publish_commands
    ADD COLUMN IF NOT EXISTS settlement_attempts INTEGER NOT NULL DEFAULT 0;

ALTER TABLE public.defgeo_publish_commands
    ADD COLUMN IF NOT EXISTS last_settlement_error TEXT;

-- ── ② provider_order_ref 部分唯一(NULL 豁免)───────────────────────────────
-- 🔴 `CREATE UNIQUE INDEX IF NOT EXISTS` 按 schema 里的**关系名**判存,既不绑表
--    也不限 relkind —— 名字被别的表(甚至别的 relkind)占着时它会静默跳过,
--    于是这条唯一性从上线起就不存在。本仓 index-guard 形态挡的正是这一格。
-- @index-guard uq_defgeo_pcmd_provider_order_ref ON defgeo_publish_commands unique
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'uq_defgeo_pcmd_provider_order_ref'
                  AND i.indrelid = to_regclass('public.defgeo_publish_commands')) THEN
        NULL;  -- 已在 public.defgeo_publish_commands 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'uq_defgeo_pcmd_provider_order_ref'
                     AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] uq_defgeo_pcmd_provider_order_ref 已存在但不在 public.defgeo_publish_commands 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'uq_defgeo_pcmd_provider_order_ref'
                AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE UNIQUE INDEX uq_defgeo_pcmd_provider_order_ref ON public.defgeo_publish_commands (provider_order_ref) WHERE provider_order_ref IS NOT NULL;
    END IF;
END $idxguard$;

-- ── 反查自证(不是注释,是会 RAISE 的断言)────────────────────────────────
DO $$
DECLARE missing text;
BEGIN
    -- ① 两列都在
    SELECT string_agg(c, ', ') INTO missing
      FROM unnest(ARRAY['settlement_attempts', 'last_settlement_error']) AS c
     WHERE NOT EXISTS (
         SELECT 1 FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name = 'defgeo_publish_commands'
            AND column_name = c
     );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION '[反查] 051 结算重试账列缺失: %', missing;
    END IF;

    -- settlement_attempts 必须 NOT NULL + DEFAULT 0(存量行不能是 NULL,
    -- 否则 `settlement_attempts + 1` 恒 NULL,上限判断永不成立)。
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'defgeo_publish_commands'
           AND column_name = 'settlement_attempts'
           AND is_nullable = 'NO' AND column_default LIKE '0%'
    ) THEN
        RAISE EXCEPTION '[反查] 051 settlement_attempts 不是 NOT NULL DEFAULT 0 —— NULL+1 恒 NULL,上限判断会永不成立';
    END IF;

    -- last_settlement_error 必须可空:NULL = "还没失败过"。
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'defgeo_publish_commands'
           AND column_name = 'last_settlement_error' AND is_nullable = 'NO'
    ) THEN
        RAISE EXCEPTION '[反查] 051 last_settlement_error 被加了 NOT NULL —— "还没失败过"就无法表达了';
    END IF;

    -- ② 唯一索引的**定义等价核验**不在这里手写。
    --
    -- 🔴 这一段原来是三次子串探针(`position('UNIQUE INDEX' in idxdef)` /
    --    `position('provider_order_ref IS NOT NULL' in idxdef)`)。Codex 二审
    --    P1-F4 打穿的正是它:一条
    --        CREATE UNIQUE INDEX uq_defgeo_pcmd_provider_order_ref
    --            ON public.defgeo_publish_commands (id)
    --         WHERE provider_order_ref IS NOT NULL
    --    三个探针**全过**,而它对 provider_order_ref 零约束 ——
    --    同一个上游单号照样能结算两笔冻结。子串探针够不到键列、键序、
    --    精确谓词、indisvalid/indisready。
    --
    -- 🔴 修法不是把探针写得更细(手写那份漏掉哪一位都不会红),而是接进
    --    本仓**已经存在**的机械轴:文件末尾的 @readiness-begin 051 块由
    --    scripts/defgeo_readiness_gen.py 生成,期望对象集 = 本文件
    --    `-- @index-guard` 锚机械扫出来的全集,期望定义 = 真 PG16 的
    --    `pg_get_indexdef` 归一化文本逐字比对。
    --    (轴一直在,只是 READINESS_FILES 这份**手写**名单没把 051/052 列进去
    --     —— 漏登记的那一个不会让任何判据变红,同一条老教训的又一形态。)
END $$;

-- @readiness-begin 051
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT 0 条 + @index-guard 1 个 + CREATE TRIGGER 0 个 + ADD COLUMN 2 列),
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

DO $readiness051$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('last_settlement_error', 'public.defgeo_publish_commands', 'typ=25|mod=-1|notnull=false|deflits=|defops='),
        ('settlement_attempts', 'public.defgeo_publish_commands', 'typ=23|mod=-1|notnull=true|deflits=0|defops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[051] 列 %.% 的目标表不存在', r.tname, r.cname;
        END IF;
        SELECT public.defgeo_ident_column(r.tname, r.cname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[051] 列 %.% 不存在 —— ADD COLUMN 没生效?', r.tname, r.cname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[051] 列 %.% 的合同不符:期望「%」实得「%」 —— '
                'ADD COLUMN IF NOT EXISTS 按**列名**判存,库上已有同名列时直接 no-op,'
                '错类型/错长度/错 default 一律不纠正;必须人工 ALTER 或 DROP 后重跑',
                r.tname, r.cname, r.cdef, actual;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('uq_defgeo_pcmd_provider_order_ref', 'public.defgeo_publish_commands', 'live=true|unique=true|cols=provider_order_ref|am=btree|predlits=|predops=is:1,not:1,null:1|exprlits=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[051] 索引的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_index(r.cname, r.tname),
               (i.indisvalid AND i.indisready) INTO actual, ok
          FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
         WHERE c.relname = r.cname AND i.indrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[051] 索引 % 不在 % 上 —— 可能被**别的表上的同名索引**挡掉了'
                '(CREATE INDEX IF NOT EXISTS 按名判存不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT t2.relname FROM pg_class c2
                            JOIN pg_index i2 ON i2.indexrelid = c2.oid
                            JOIN pg_class t2 ON t2.oid = i2.indrelid
                           WHERE c2.relname = r.cname
                             AND c2.relnamespace = 'public'::regnamespace), '(没有同名索引)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[051] 索引 % 定义漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[051] 索引 % 的 indisvalid/indisready 不成立 —— '
                'CREATE INDEX CONCURRENTLY 失败留下的壳子文本与正品一模一样,'
                '但它不保证唯一性', r.cname;
        END IF;
    END LOOP;
    RAISE NOTICE '[051] exact schema readiness 通过(2 列 + 0 约束 + 1 索引 + 0 触发器逐字对上)';
END $readiness051$;
-- @readiness-end 051
