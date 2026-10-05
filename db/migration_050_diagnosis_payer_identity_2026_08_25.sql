-- ============================================================================
-- 050 · diagnosis_runs 落「这一单的钱到底从谁的钱包冻的」(WO_CODEX_P0_DIAG_FUNDING A-1)
-- ============================================================================
--
-- 为什么要这一列(Codex 终审 P0-1 的第二层)
-- ----------------------------------------------------------------------------
-- 防御型 GEO 的**平台承担腿**(admin_platform_ledger / sponsor_platform_ledger)
-- 在 confirm 里走的是 `freeze_points(platform_uid, ...)` —— 冻结行真真切切长在
-- **平台直营服务账号**的钱包上,`point_freezes.user_id = platform_uid`。
--
-- 而结算侧 `services/diagnosis_runs._do_settlement` 一直拿
-- `run["owner_user_id"]`(= 发起人 / 租户)去调 `commit_freeze / release_freeze`。
-- billing 的这两个原语在给了 freeze_id + user_id 时**按 id+user_id 定位**,
-- user 对不上就找不到那一行 —— 于是:
--   · commit 永远 success=false → 退避重试 → 5 次后 settlement_manual;
--   · `_frozen_total_for_run` / `_reserved_split_for_run` 的
--     `WHERE id=%s AND user_id=%s AND task_ref=%s` 直接查空 → 降级交付
--     一律 `frozen_amount_unreadable` / `reserved_split_freeze_missing` 转人工。
-- 也就是说:**即使把 exempt 旁路拆掉,平台腿仍然一分钱都结不掉**。
-- Codex 原文:「即使取消 exempt 旁路,现有结算仍用 tenant owner_user_id 查找冻结,
-- 而冻结行属于 platform UID,第二层仍无法结算。」
--
-- 这一列就是那个第二层:run 行持久化**真实 payer**,结算侧所有冻结定位统一读它。
--
-- 语义
-- ----------------------------------------------------------------------------
-- 🔴 `payer_user_id IS NULL` **= payer 就是 owner_user_id**(存量与个人钱包腿的常态)。
--    读取侧单点谓词 `services.diagnosis_runs.settlement_payer_user_id(run)`
--    做这个回落,**不在别处再写第二遍**(同一谓词写两处必有一处没人验)。
-- 🔴 只有 payer ≠ owner 的单(现役唯一来源 = defgeo 平台承担腿)才会写非 NULL 值。
--
-- 顺序 / 依赖
-- ----------------------------------------------------------------------------
-- 🔴 无依赖:只碰 diagnosis_runs 一张既有表,不加 FK、不建索引
--    (不按此列查询 —— 结算永远按 run_token 主键取行,再从行里读 payer)。
-- 🔴 **不动任何既有 CHECK**。特别是 `chk_diag_runs_billing_mode`(paid|exempt)
--    与 `chk_freeze_handle`:平台腿被修成走 `billing_mode='paid'`,
--    正是为了不去加第三个枚举值 —— 加值等于把
--    「假设只有 paid|exempt」的十几处读取(两条 sweeper 查询在内)全部点燃,
--    而漏掉的那一处不会让任何判据变红,只会让平台的钱静静挂着。
--    详见交付文 §A-1「为什么不新增 billing_mode 枚举值」。
--
-- 重放安全
-- ----------------------------------------------------------------------------
-- 🔴 prestart 每次部署**无条件重放全部迁移**(无追踪表)。本文件:
--    · 只有一条 `ADD COLUMN IF NOT EXISTS` → 天然幂等;
--    · **零 DML** —— 不回填、不 UPDATE 任何一行。存量行保持 NULL 是**有意**的:
--      NULL 的语义就是「payer = owner」,与存量事实完全一致,不需要也不许回填。
--
-- 漏跑后果(**刻意做成响亮**)
-- ----------------------------------------------------------------------------
-- 🔴 与 048 相反,这一列漏跑必须当场炸:
--    `services/startup_schema_guards.verify_diagnosis_schema_fail_closed` 把
--    `payer_user_id` 列进关键列集合,web/cron 起不来。
--    理由是漏跑的静默后果太贵:列不在 → `run.get("payer_user_id")` 取 None →
--    回落 owner → 平台腿又变回「拿租户 id 去 commit 平台的冻结」,
--    也就是本迁移要修的那个 bug **原样复活**,而且没有任何判据会红。
--    宁可 crash-loop 逼 prestart 先跑。
--
-- 回滚
-- ----------------------------------------------------------------------------
-- 🔴 回滚 = `ALTER TABLE diagnosis_runs DROP COLUMN payer_user_id;`
--    但回滚**必须与代码一起回**:代码还在的情况下删列 = fleet 起不来(见上)。
--    且回滚后所有 payer ≠ owner 的在途单会失去 payer 事实,只能人工结算。
--    因此**不放进部署清单自动做**。
-- ============================================================================

ALTER TABLE diagnosis_runs
    ADD COLUMN IF NOT EXISTS payer_user_id INTEGER;

COMMENT ON COLUMN diagnosis_runs.payer_user_id IS
    '这一单的物理冻结实际长在谁的钱包上(point_freezes.user_id)。'
    'NULL = payer 就是 owner_user_id(存量 + 个人钱包腿常态);'
    '非 NULL 的现役唯一来源 = 防御型 GEO 平台承担腿(平台直营服务账号 UID)。'
    '读取单点 = services.diagnosis_runs.settlement_payer_user_id(run)。'
    'WO_CODEX_P0_DIAG_FUNDING_2026-08-25 A-1(迁移号 050:040-049 已被防御 GEO 班列占用)。';

-- ============================================================================
-- [E2-4 · Codex 二审 · 2026-08-26] 迁移自证:这一列必须是 integer 且**可空**
-- ============================================================================
-- 为什么加这一段
-- ----------------------------------------------------------------------------
-- `ADD COLUMN IF NOT EXISTS` 是按**列名**判存的:库上已经有一个同名列时它直接
-- no-op —— 不管那个列是什么类型、可不可空。于是一个 `payer_user_id TEXT`
-- (手工建的 / 别的分支建的 / 回滚残留)会:
--   · 让本迁移每次重放都"成功",prestart 一路绿;
--   · 让启动守卫的旧版(只核列名)也放行;
--   · 而结算侧 `int(run.get("payer_user_id"))` 拿到 '123' 这种字符串照样能转 ——
--     真正炸的地方在 `commit_freeze(user_id=...)`,那时钱已经在动了。
-- 这就是「同名错类型不被 IF NOT EXISTS 纠正」那一类,和
-- 「CREATE INDEX IF NOT EXISTS 按名判存不绑表」是同一族。
--
-- 🔴 为什么核的是「**可空**」而不是「非空」
-- ----------------------------------------------------------------------------
-- 本列的语义是 `NULL = payer 就是 owner_user_id`(存量与个人钱包腿的常态)。
-- 有人把它建成 NOT NULL,存量每一行都得有值 —— 而它们本来就该是 NULL。
-- 所以这一列的正确性方向和大多数列相反:必须**可空**。
--
-- 重放安全:纯 SELECT + RAISE,零 DML,幂等。
-- ============================================================================
DO $$
DECLARE
    v_type text;
    v_nullable text;
BEGIN
    SELECT data_type, is_nullable INTO v_type, v_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'diagnosis_runs'
      AND column_name = 'payer_user_id';

    IF v_type IS NULL THEN
        RAISE EXCEPTION
            '[050 自证] diagnosis_runs.payer_user_id 建完之后查不到 —— ADD COLUMN 没生效?';
    END IF;
    IF v_type <> 'integer' THEN
        RAISE EXCEPTION
            '[050 自证] diagnosis_runs.payer_user_id 类型是 % ,要求 integer · '
            '同名错类型不会被 ADD COLUMN IF NOT EXISTS 纠正,必须人工 ALTER 或 DROP 后重跑',
            v_type;
    END IF;
    IF v_nullable <> 'YES' THEN
        RAISE EXCEPTION
            '[050 自证] diagnosis_runs.payer_user_id 被建成了 NOT NULL · '
            '本列语义是「NULL = payer 就是 owner」,非空会让存量每一行都需要一个不存在的值';
    END IF;
END
$$;

-- @readiness-begin 050
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT 0 条 + @index-guard 0 个 + CREATE TRIGGER 0 个 + ADD COLUMN 1 列),
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

DO $readiness050$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('payer_user_id', 'public.diagnosis_runs', 'typ=23|mod=-1|notnull=false|deflits=|defops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[050] 列 %.% 的目标表不存在', r.tname, r.cname;
        END IF;
        SELECT public.defgeo_ident_column(r.tname, r.cname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[050] 列 %.% 不存在 —— ADD COLUMN 没生效?', r.tname, r.cname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[050] 列 %.% 的合同不符:期望「%」实得「%」 —— '
                'ADD COLUMN IF NOT EXISTS 按**列名**判存,库上已有同名列时直接 no-op,'
                '错类型/错长度/错 default 一律不纠正;必须人工 ALTER 或 DROP 后重跑',
                r.tname, r.cname, r.cdef, actual;
        END IF;
    END LOOP;
    RAISE NOTICE '[050] exact schema readiness 通过(1 列 + 0 约束 + 0 索引 + 0 触发器逐字对上)';
END $readiness050$;
-- @readiness-end 050
