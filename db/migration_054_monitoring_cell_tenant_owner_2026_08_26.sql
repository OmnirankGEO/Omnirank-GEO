-- ============================================================================
-- 054 · 监测格冻结租户归属(工单 E3-1 · Codex 二审 P1-F6)
-- ============================================================================
-- 号段登记:053 归工单D(审查员挂龄提醒)· **054 归本单** · 下一空 = 055。
--
-- 缺陷(已亲手证实,不是转述 —— 且比二审报告更毒):
--   attempt 账本的 `tenant_owner_user_id` 有**六**个写点,没有一个读得到真租户:
--     ① run_ledger_bridge.open_for_claim         `cell.get("billing_user_id") or 0`
--     ② 同上,重试臂(reserve_monitoring_cell_retry 传 dict(claimed))
--     ③ run_ledger_bridge.record_skip_for_plan   同一句
--     ④ legacy_bridge.capture_before_retry_overwrite  同一句
--     ⑤ run_ledger_bridge.close_unattempted_for_cells `COALESCE(b.owner_user_id, 0)`
--     ⑥ run_ledger_bridge.close_for_human_resolution  写的是**操作者** actor_user_id
--
--   ①②③④ 读的键 `billing_user_id` 在 `monitoring_run_cells` 上**根本不存在**
--   (生产 pg_dump 32 列逐列核对,零命中;那一列属于
--    `monitoring_keyword_settlements`)。四个写点因此恒落 **0** ——
--   0 不是"未知",0 是一个**编出来的租户**。
--
--   ⑤ 现读 `brands.owner_user_id`:品牌在跑完与收口之间被转移,
--   同一次运行的历史归属会跟着改。
--
--   ⑥ 把"谁点的确认"写成"这是谁的运行":管理员替客户确认身份时,
--   那一行 attempt 归到管理员名下。
--
-- 🔴 二审报告说「Python 侧列注册表声称 monitoring_run_cells 有 billing_user_id」——
--    **这一句不成立**。`db/monitoring_db.assert_monitoring_cell_retry_ready` 的
--    expected 集合里 monitoring_run_cells 那一格没有 billing_user_id
--    (它出现在同函数 monitoring_keyword_settlements 那一格,是对的)。
--    撒谎的不是注册表,是**判据夹具**:
--    `tests/defensive_geo_pkgf_2026_08_23/conftest._LIVE_SCHEMA` 手写的
--    monitoring_run_cells 多造了一列 `billing_user_id`,而那份夹具正是
--    `test_open_for_claim_only_reads_keys_the_live_cell_row_really_has`
--    的**分母**。于是一条专门为这个 bug 写的锁,拿被污染的分母去判,恒绿。
--    (本仓第三次「夹具供了生产不会供的东西」;新增的是第三形态:
--     夹具同时是那条锁的分母。)
--
-- 本迁移只做一件事:把租户归属**冻在格上**。
--
-- 🔴 体内零 DML(与 043/046/051/052 同规矩)——
--    prestart 每次部署无条件重放全部迁移(无追踪表),迁移里的 DML 是常驻地雷。
--    存量行 tenant_owner_user_id 保持 NULL;回填与 census 由 Deploy 在发车前
--    单跑 `scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql`,
--    命令与判读写在交付文里。
--    🔴 刻意**不**把回填放进迁移体内:回填读的是**当前** brands.owner_user_id,
--       放进迁移就意味着**每次部署**都会拿当天的 owner 去补新出现的 NULL 行 ——
--       那正好把本迁移要消灭的"归属随品牌转移漂移"以另一种形态请回来。
--       一次性脚本跑完即止,漂移窗口只有一次且可审计。
--
-- 🔴 可空是**有意**的:NULL = "这一格的租户未知"。
--    未知必须能表达,否则代码只剩两条路 —— 编一个(就是 0,本迁移要杀的)
--    或者让现役监测跑不起来(违反 §13.1「监测老链一行不改语义」)。
--    账本侧禁 0 的那把锁装在 046 的 `defgeo_monitoring_attempts` 上
--    (`chk_defgeo_attempt_tenant_owner_positive`):未知 ⇒ 不写账本 + 告警,
--    绝不落一个编出来的数。
--
-- 🔴 顺序依赖:依赖 `scripts/migration_monitoring_cell_retry_2026_07_21.sql`
--    建的 monitoring_run_cells(manifest 第 110 行,远在本文件之前)。编号序即执行序。
--
-- 🔴 漏跑后果**响亮**(刻意):`create_monitoring_run_cells` 的 INSERT 显式写
--    这一列 → 列不在即 UndefinedColumn 当场抛 ⇒ 建格整批失败,
--    绝不会退化成"格建出来了但没冻租户"。
-- ============================================================================

ALTER TABLE public.monitoring_run_cells
    ADD COLUMN IF NOT EXISTS tenant_owner_user_id INTEGER;

-- 🔴 顺序有意:列形状核验在 ADD CONSTRAINT **之前**。
--    同名但类型错的列(比如有人把它建成 TEXT)会让 `... > 0` 直接抛
--    `operator does not exist: text > integer` —— 同样是响亮失败,
--    但运维从那句话看不出根因是「这一列被建成了别的类型」。
--    先核形状,报出来的才是人话。
-- ── 反查自证(不是注释,是会 RAISE 的断言)────────────────────────────────
-- 🔴 `ADD COLUMN IF NOT EXISTS` 之后,"幂等跳过"与"根本没装上"长得一模一样;
--    所以这里逐项核**列的形状**(类型 / 可空)。
-- 🔴 约束的**定义等价**不在这里手写:本文件末尾的 @readiness-begin 054 块
--    由 scripts/defgeo_readiness_gen.py 机械生成,期望定义从真 PG16 现读。
--    在这里再手写一段 = 同一谓词写两处,必有一处没人验
--    (而 051/052 被 Codex 打穿的正是「手写那份不够狠」)。
DO $$
DECLARE
    col_type    text;
    col_null    text;
BEGIN
    SELECT data_type, is_nullable INTO col_type, col_null
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'monitoring_run_cells'
       AND column_name = 'tenant_owner_user_id';
    IF col_type IS NULL THEN
        RAISE EXCEPTION '[054] monitoring_run_cells.tenant_owner_user_id 没装上 —— 拒绝静默通过';
    END IF;
    IF col_type <> 'integer' THEN
        RAISE EXCEPTION '[054] tenant_owner_user_id 类型是 % 而不是 integer —— 同名错类型列会让全链静默走形', col_type;
    END IF;
    IF col_null <> 'YES' THEN
        RAISE EXCEPTION '[054] tenant_owner_user_id 被加了 NOT NULL —— "租户未知"就无法表达,代码只剩编一个或让监测跑不起来';
    END IF;

END $$;

-- 0 是被本单消灭的那个**编造值**,必须在结构上不可表达。
-- NULL(未知)放行 —— 见上面"可空是有意的"。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_monitoring_run_cells_tenant_owner_positive'
           AND conrelid = 'public.monitoring_run_cells'::regclass
    ) THEN
        ALTER TABLE public.monitoring_run_cells
            ADD CONSTRAINT chk_monitoring_run_cells_tenant_owner_positive
            CHECK (tenant_owner_user_id IS NULL OR tenant_owner_user_id > 0);
    END IF;
END $$;

-- @readiness-begin 054
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT 1 条 + @index-guard 0 个 + CREATE TRIGGER 0 个 + ADD COLUMN 1 列),
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

DO $readiness054$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('tenant_owner_user_id', 'public.monitoring_run_cells', 'typ=23|mod=-1|notnull=false|deflits=|defops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[054] 列 %.% 的目标表不存在', r.tname, r.cname;
        END IF;
        SELECT public.defgeo_ident_column(r.tname, r.cname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[054] 列 %.% 不存在 —— ADD COLUMN 没生效?', r.tname, r.cname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[054] 列 %.% 的合同不符:期望「%」实得「%」 —— '
                'ADD COLUMN IF NOT EXISTS 按**列名**判存,库上已有同名列时直接 no-op,'
                '错类型/错长度/错 default 一律不纠正;必须人工 ALTER 或 DROP 后重跑',
                r.tname, r.cname, r.cdef, actual;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('chk_monitoring_run_cells_tenant_owner_positive', 'public.monitoring_run_cells', 'c|cols=tenant_owner_user_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=0|ops=>:1,is:1,null:1,or:1')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[054] 约束的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_constraint(r.cname, r.tname), c.convalidated
          INTO actual, ok
          FROM pg_constraint c
         WHERE c.conname = r.cname AND c.conrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION '[054] 约束 % 不在 % 上(可能被同名约束挡在了别的表)',
                r.cname, r.tname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[054] 约束 % 语义身份漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[054] 约束 % 是 NOT VALID —— 存量行从没被验过,守卫只守未来', r.cname;
        END IF;
    END LOOP;
    RAISE NOTICE '[054] exact schema readiness 通过(1 列 + 1 约束 + 0 索引 + 0 触发器逐字对上)';
END $readiness054$;
-- @readiness-end 054
