-- 052 · 激活 outbox 冻结 payer 身份(工单 C-1 · Codex 终审 P1-6)
--
-- 缺陷(已亲手证实,不是转述):
--   `api/selection_api._confirm_quote_row` 调 `enqueue_activation` 时**只传**
--   accepted_snapshot_id / quote_id / brand_id / accepted_snapshot_hash ——
--   043 建表时就留好的 `tenant_owner_id` / `actor_user_id` 一个都没传。
--   于是 `services/defensive_geo/activation_materializer._tenant_of` 的
--   「显式优先」分支恒不命中,每次物化都**现读** `brands.owner_user_id`:
--
--       explicit = row.get("tenant_owner_id")     # 生产恒 NULL
--       if explicit: ...                          # 恒 False
--       SELECT owner_user_id FROM brands WHERE id = %s   # ← 现读可变列
--
--   品牌在「客户确认报价」与「物化」之间被转移(改 owner_user_id),
--   同一个 accepted snapshot 会解析出**另一个** tenant,继而
--   `payer_classification.classify_user_id(cur, tenant)` 判出**另一个付款方**,
--   `execution_budget_policy.derive(..., payer_user_id=tenant)` 把预算签在别人头上。
--   commercial basis 成立那一刻的付款人,不该由之后的品牌转移改写。
--
-- 本迁移只做一件事:给 outbox 行加**冻结的付款人身份**三列。
--
-- 🔴 体内零 DML(与 043 同规矩)。存量 pending 行三列保持 NULL,
--    由 materializer 的「冻结值缺失 ⇒ 转人工」路径处置 ——
--    **不回落现读**(回落等于把这个 bug 留在存量行上,且没有任何判据会红)。
--
-- 🔴 与 043 头部「零资金列」的关系:043 禁的是 points / 金额 / wallet 列
--    (免得有人把"预扣执行算力"混进 activation 事务)。本迁移加的是
--    **身份**列,不是金额列 —— 三列都不含任何数额语义,
--    `tests/defensive_geo_w2_2026_08_21::test_migration_043_has_no_funding_column`
--    的禁词(points/wallet/amount/balance/freeze)一个都不出现。
--
-- 🔴 号段登记:050 归工单A(诊断资金链)· 051 归工单B(发布结算守卫)·
--    **052 归本单**· 下一空 = 053。
--
-- 🔴 顺序依赖:必须置于 043 之后(它建的表)。编号序即执行序。
--
-- 🔴 漏跑后果**响亮**:`enqueue_activation` 的 INSERT 显式写这三列,
--    列不在 → UndefinedColumn 当场抛 → `ActivationEnqueueError` →
--    fail-closed 让整笔客户确认回滚。绝不会退化成"确认成功但没冻结付款人"。

ALTER TABLE public.defgeo_activation_outbox
    ADD COLUMN IF NOT EXISTS payer_user_id       INTEGER;

ALTER TABLE public.defgeo_activation_outbox
    ADD COLUMN IF NOT EXISTS payer_funding_policy  VARCHAR(64);

ALTER TABLE public.defgeo_activation_outbox
    ADD COLUMN IF NOT EXISTS payer_principal_kind  VARCHAR(64);

-- 三列**同生同死**。半冻结的付款人身份不可表达:
-- 只冻了 payer_user_id 而没冻 funding_policy 时,materializer 仍然得现算政策,
-- 那正是本迁移要消灭的那一格。
--
-- 🔴 只约束这三个**新**列 —— 存量行三列全 NULL,天然满足「全空」那一支,
--    所以 ADD CONSTRAINT 不会因存量数据失败。
--    刻意**不**把 043 既有的 tenant_owner_id 拉进这条 CHECK:那一列
--    建表起就可空且入队处可以不传,把它并进来会让本迁移在任何
--    「tenant 非空而 payer 空」的存量行上直接失败 ⇒ prestart 非零退出 ⇒ 部署 halt。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'defgeo_activation_outbox_payer_group'
           AND conrelid = 'public.defgeo_activation_outbox'::regclass
    ) THEN
        ALTER TABLE public.defgeo_activation_outbox
            ADD CONSTRAINT defgeo_activation_outbox_payer_group CHECK (
                (payer_user_id IS NULL
                 AND payer_funding_policy IS NULL
                 AND payer_principal_kind IS NULL)
                OR
                (payer_user_id IS NOT NULL
                 AND payer_funding_policy IS NOT NULL
                 AND payer_principal_kind IS NOT NULL)
            );
    END IF;
END $$;

-- 反向自证:三列真的装上了、CHECK 真的在。
-- 🔴 迁移体内自证是本仓 043/047/049 的既有形态 —— 幂等跳过与"没装上"
--    在 ADD COLUMN IF NOT EXISTS 之后长得一模一样,不查一遍分不出来。
DO $$
DECLARE
    got INTEGER;
BEGIN
    SELECT COUNT(*) INTO got
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name   = 'defgeo_activation_outbox'
       AND column_name IN ('payer_user_id', 'payer_funding_policy', 'payer_principal_kind');
    IF got <> 3 THEN
        RAISE EXCEPTION '[052] 冻结 payer 三列没装齐(实得 %/3)—— 拒绝静默通过', got;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'defgeo_activation_outbox_payer_group'
           AND conrelid = 'public.defgeo_activation_outbox'::regclass
    ) THEN
        RAISE EXCEPTION '[052] defgeo_activation_outbox_payer_group 约束不在 —— 半冻结身份会变得可表达';
    END IF;
END $$;

-- @readiness-begin 052
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT 1 条 + @index-guard 0 个 + CREATE TRIGGER 0 个 + ADD COLUMN 3 列),
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

DO $readiness052$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('payer_funding_policy', 'public.defgeo_activation_outbox', 'typ=1043|mod=68|notnull=false|deflits=|defops='),
        ('payer_principal_kind', 'public.defgeo_activation_outbox', 'typ=1043|mod=68|notnull=false|deflits=|defops='),
        ('payer_user_id', 'public.defgeo_activation_outbox', 'typ=23|mod=-1|notnull=false|deflits=|defops=')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[052] 列 %.% 的目标表不存在', r.tname, r.cname;
        END IF;
        SELECT public.defgeo_ident_column(r.tname, r.cname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[052] 列 %.% 不存在 —— ADD COLUMN 没生效?', r.tname, r.cname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[052] 列 %.% 的合同不符:期望「%」实得「%」 —— '
                'ADD COLUMN IF NOT EXISTS 按**列名**判存,库上已有同名列时直接 no-op,'
                '错类型/错长度/错 default 一律不纠正;必须人工 ALTER 或 DROP 后重跑',
                r.tname, r.cname, r.cdef, actual;
        END IF;
    END LOOP;

    FOR r IN SELECT * FROM (VALUES
        ('defgeo_activation_outbox_payer_group', 'public.defgeo_activation_outbox', 'c|cols=payer_funding_policy,payer_principal_kind,payer_user_id|fk=-|fkcols=-|upd= |del= |valid=true|lits=|ops=and:4,is:6,not:3,null:6,or:1')
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[052] 约束的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_constraint(r.cname, r.tname), c.convalidated
          INTO actual, ok
          FROM pg_constraint c
         WHERE c.conname = r.cname AND c.conrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION '[052] 约束 % 不在 % 上(可能被同名约束挡在了别的表)',
                r.cname, r.tname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[052] 约束 % 语义身份漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[052] 约束 % 是 NOT VALID —— 存量行从没被验过,守卫只守未来', r.cname;
        END IF;
    END LOOP;
    RAISE NOTICE '[052] exact schema readiness 通过(3 列 + 1 约束 + 0 索引 + 0 触发器逐字对上)';
END $readiness052$;
-- @readiness-end 052
