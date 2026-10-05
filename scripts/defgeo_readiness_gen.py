"""从迁移文件**机械导出** exact-readiness 的期望对象集,并就地生成自验块。

═══════════════════════════════════════════════════════════════════════════
为什么不能手写这份清单
═══════════════════════════════════════════════════════════════════════════
fa8aecc50 那轮的 readiness 块里,期望对象是**手抄**的三/五条"承重约束"。
手写清单的问题不是"抄错"(抄错会红),是**漏抄**:漏掉的那一条不会让任何
判据变红。040 手抄 3 条,文件其实声明了 5 条约束 + 1 个索引;
046 手抄 4 条,文件其实声明了 27 条约束 + 11 个索引。
被漏掉的 22 条约束和 11 个索引,readiness **一条都没在验**。

而索引这一类恰恰是本单要修的那种病的受害者:
``uq_defgeo_attempt_single_inflight`` 被别的表上的同名索引挡掉时,
旧 readiness(只看 pg_constraint)**结构上就够不到它** —— 报绿是必然的。

═══════════════════════════════════════════════════════════════════════════
分母怎么来
═══════════════════════════════════════════════════════════════════════════
  · 约束 —— 文件里的 ``ALTER TABLE <t> ADD CONSTRAINT <n>``(显式命名的那些;
            CREATE TABLE 内联的 PK 由 PostgreSQL 自动命名,不在本轴)
  · 索引 —— 文件里的 ``-- @index-guard <idx> ON <t> <kind>`` 锚
  · 期望定义 —— 从**真 PG16 现读**(``pg_get_constraintdef`` / ``pg_get_indexdef``),
            不手抄 DDL 片段(手抄一个括号就是一条恒红或恒绿的假判据)

生成块由 ``-- @readiness-begin/<file-tag>`` 与 ``-- @readiness-end/<file-tag>``
夹住,可反复重跑(幂等)。双向一致性由
``tests/defensive_geo_w3_2026_08_21/test_migration_index_scope_pg.py`` 守。

用法::

    python scripts/defgeo_readiness_gen.py --dsn postgresql://.../xxx_test [--apply]
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.defgeo_index_guard_scan import (  # noqa: E402
    scan_bound_index_guards,
    strip_sql_noise,
)

ROOT = Path(__file__).resolve().parent.parent

#: 本轴的作用域。
#:
#: 🔴 [工单 E3-3 · 2026-08-26 · Codex 二审 P1-F4/F5] 这份清单曾经只有四个文件
#:    (fa8aecc50 那轮引入 exact-readiness 的那四个)。Codex 二审打穿的 051/052
#:    **恰好都在这个轴外面** —— 轴本身建得好好的,只是没指着它们:
#:      · 051 的终检只做两次 ``position(... in idxdef)`` 子串探针 ⇒
#:        一条 ``UNIQUE (id) WHERE provider_order_ref IS NOT NULL`` 的同名索引
#:        三个探针全过,而它对 provider_order_ref 零约束;
#:      · 052 全文没有 ``pg_get_constraintdef`` ⇒ 同名两列弱 CHECK 直接放行,
#:        而这一形态 2026-08-26 **真的**在本仓的判据库上发生过(工单C 的
#:        c1_35 就是为它补的)。
#:    修的方式不是再手写两段守卫(手写的那份漏掉谁都不会红),
#:    而是把它们**接进这条已经存在的机械轴**。
#:
#: 🔴 本轴要求文件至少声明一侧对象(约束或索引),不再要求两侧都有 ——
#:    051 声明 0 约束 + 1 索引,052/054 声明 1 约束 + 0 索引。
#:    旧生成器对空的一侧会产出语法错误的 ``(VALUES\n\n)``,
#:    ``block_is_canonical`` 也直接判 False —— 那正是这三个文件进不来的技术原因。
READINESS_FILES = {
    "040": "db/migration_040_defgeo_question_plans_2026_08_21.sql",
    "041": "db/migration_041_defgeo_run_previews_2026_08_21.sql",
    "044": "db/migration_044_defgeo_publish_decision_2026_08_21.sql",
    "046": "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql",
    "050": "db/migration_050_diagnosis_payer_identity_2026_08_25.sql",
    "051": "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql",
    "052": "db/migration_052_defgeo_activation_frozen_payer_2026_08_25.sql",
    "054": "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql",
}

BEGIN = "-- @readiness-begin {tag}"
END = "-- @readiness-end {tag}"

_ADD_CONSTRAINT = re.compile(
    r"ALTER\s+TABLE\s+(?:public\.)?(?P<tbl>[A-Za-z_][A-Za-z_0-9]*)\s+"
    r"ADD\s+CONSTRAINT\s+(?P<name>[A-Za-z_][A-Za-z_0-9]*)",
    re.IGNORECASE,
)


def declared_constraints(sql: str) -> list[tuple[str, str]]:
    """(约束名, 表名) —— 去重保序。"""
    seen, out = set(), []
    for m in _ADD_CONSTRAINT.finditer(strip_sql_noise(sql)):
        key = (m.group("name"), m.group("tbl"))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return sorted(out)


def declared_indexes(sql: str, rel: str) -> list[tuple[str, str]]:
    return sorted({(s.index, s.table) for s in scan_bound_index_guards(sql, rel)})


#: 🔴 [工单 V3-A · Codex 三审 P1-2] 触发器轴。
#:
#: 加这一轴的理由是一份真 PG16 反例:040 的反查块写的是
#: ``IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_defgeo_qplan_immutable')``
#: —— **连表都没绑**(没有 tgrelid 谓词),更不看它执行的是哪个函数。
#: 于是把同名触发器换成一个 ``RETURN NEW`` 的放行版本,040 二跑照样报通过,
#: 而不可变字段可以随便 UPDATE。「按名判存」在触发器上比在索引上更贵:
#: 索引骗过去只是少了个约束,触发器骗过去是**审计不可变性整个消失**。
#:
#: 同族第三例:``CREATE INDEX IF NOT EXISTS`` 按名判存不绑表 /
#: ``DROP INDEX`` 语法上不绑表 / 现在是 ``pg_trigger`` 按名判存不绑表。
_CREATE_TRIGGER = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+"
    r"(?P<name>[A-Za-z_][A-Za-z_0-9]*)\s+.*?\s+ON\s+(?:public\.)?"
    r"(?P<tbl>[A-Za-z_][A-Za-z_0-9]*)",
    re.IGNORECASE | re.DOTALL,
)


#: 🔴 [工单 V3-A · Codex 三审 P1-3 + P2-4] 列合同轴。
#:
#: Codex 亲验的三个 poison 都是同一个形状 —— ``ADD COLUMN IF NOT EXISTS``
#: **按列名判存**,库上已有同名列就直接 no-op,类型/长度/默认值一律不纠正:
#:   · 050 接受 ``payer_user_id INTEGER DEFAULT 777`` ⇒ 省略该列的旧 INSERT 静默得到 payer 777;
#:   · 051 接受 ``settlement_attempts BIGINT`` / ``last_settlement_error INTEGER``
#:     ⇒ 真实 writer 写错误文本时 InvalidTextRepresentation;
#:   · 052 接受 ``payer_user_id BIGINT DEFAULT 777`` 及身份文本默认值
#:     ⇒ 旧形状 INSERT 把租户 123 解析成 payer 777。
#: 050 原来核了 type/nullable **两轴**,default 轴**零核验** —— 那不是疏忽的极限,
#: 是「手写守卫只守作者当时想到的那几维」的必然结果。所以这一轴同样机械化:
#: 分母 = 文件里声明的每一条 ADD COLUMN,期望值 = 真 PG16 现读的
#: ``format_type(atttypid, atttypmod)`` + ``attnotnull`` + ``pg_get_expr(adbin)``。
_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:public\.)?"
    r"(?P<tbl>[A-Za-z_][A-Za-z_0-9]*)\s+"
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<col>[A-Za-z_][A-Za-z_0-9]*)",
    re.IGNORECASE | re.DOTALL,
)


def declared_columns(sql: str) -> list[tuple[str, str]]:
    """(列名, 表名) —— 文件里每一条 ``ADD COLUMN``,去重保序。

    与约束/触发器同样走 ``strip_sql_noise``:注释里大量在讲
    ``ADD COLUMN IF NOT EXISTS`` 这件事,算进分母会当场红一片。
    """
    seen, out = set(), []
    for m in _ADD_COLUMN.finditer(strip_sql_noise(sql)):
        key = (m.group("col"), m.group("tbl"))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return sorted(out)


def declared_triggers(sql: str) -> list[tuple[str, str]]:
    """(触发器名, 表名) —— 从文件里机械扫,去重保序。

    与约束同样走 ``strip_sql_noise``:注释掉的示例不进分母
    (进了的话这条轴一上来就红几十条,红多了就没人看了)。
    """
    seen, out = set(), []
    for m in _CREATE_TRIGGER.finditer(strip_sql_noise(sql)):
        key = (m.group("name"), m.group("tbl"))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return sorted(out)


IDENT_FUNCS_SQL = r"""-- ══════════════════════════════════════════════════════════════════════
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
"""


def _q(s: str) -> str:
    """SQL 单引号字面量(内部单引号翻倍)。"""
    return "'" + s.replace("'", "''") + "'"


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def _con_section(tag: str, cons: list[tuple[str, str, str]]) -> str:
    """约束那一段。``cons`` 为空时整段不生成(空 VALUES 表是语法错误)。

    🔴 [P0 2026-09-02] 期望值与实得值**都**走 ``defgeo_ident_constraint`` ——
       不再比 ``pg_get_constraintdef()`` 的渲染文本(理由见 IDENT_FUNCS_SQL 抬头)。
    """
    if not cons:
        return ""
    con_rows = ",\n".join(
        f"        ({_q(n)}, {_q('public.' + t)}, {_q(d)})" for n, t, d in cons)
    return f"""
    FOR r IN SELECT * FROM (VALUES
{con_rows}
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[{tag}] 约束的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_constraint(r.cname, r.tname), c.convalidated
          INTO actual, ok
          FROM pg_constraint c
         WHERE c.conname = r.cname AND c.conrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION '[{tag}] 约束 % 不在 % 上(可能被同名约束挡在了别的表)',
                r.cname, r.tname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[{tag}] 约束 % 语义身份漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[{tag}] 约束 % 是 NOT VALID —— 存量行从没被验过,守卫只守未来', r.cname;
        END IF;
    END LOOP;
"""


def _idx_section(tag: str, idxs: list[tuple[str, str, str]]) -> str:
    """索引那一段。``idxs`` 为空时整段不生成。"""
    if not idxs:
        return ""
    idx_rows = ",\n".join(
        f"        ({_q(n)}, {_q('public.' + t)}, {_q(d)})" for n, t, d in idxs)
    return f"""
    FOR r IN SELECT * FROM (VALUES
{idx_rows}
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[{tag}] 索引的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_index(r.cname, r.tname),
               (i.indisvalid AND i.indisready) INTO actual, ok
          FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
         WHERE c.relname = r.cname AND i.indrelid = r.tname::regclass;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[{tag}] 索引 % 不在 % 上 —— 可能被**别的表上的同名索引**挡掉了'
                '(CREATE INDEX IF NOT EXISTS 按名判存不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT t2.relname FROM pg_class c2
                            JOIN pg_index i2 ON i2.indexrelid = c2.oid
                            JOIN pg_class t2 ON t2.oid = i2.indrelid
                           WHERE c2.relname = r.cname
                             AND c2.relnamespace = 'public'::regnamespace), '(没有同名索引)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION '[{tag}] 索引 % 定义漂移:期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
        IF NOT ok THEN
            RAISE EXCEPTION
                '[{tag}] 索引 % 的 indisvalid/indisready 不成立 —— '
                'CREATE INDEX CONCURRENTLY 失败留下的壳子文本与正品一模一样,'
                '但它不保证唯一性', r.cname;
        END IF;
    END LOOP;
"""


def _trg_section(tag: str, trgs: list[tuple[str, str, str]]) -> str:
    """触发器那一段。``trgs`` 为空时整段不生成。

    第三项(期望定义)是一个**三合一**串:
    ``norm(pg_get_triggerdef) | enabled=<tgenabled> | fn=<md5(norm(pg_get_functiondef))>``

      · ``pg_get_triggerdef`` 里已经含了宿主表、BEFORE/AFTER、事件集、FOR EACH ROW
        和 ``EXECUTE FUNCTION <fn>`` —— 即 tgrelid / tgtype / tgfoid 三样;
      · ``tgenabled`` 单独拼进来:一个 ``ALTER TABLE … DISABLE TRIGGER`` 的触发器
        目录里定义**一字不差**,却一次都不会执行;
      · 函数体取 md5:同名触发器指向同名函数、但函数体被换成 ``RETURN NEW``
        的那一手,只有比函数定义才看得见。

    打包成一个串是为了让三段共用同一张 ``(cname, tname, cdef)`` VALUES 表和
    同一套往返解析 —— 换成四元组会把 ``parse_block_rows`` / ``block_is_canonical``
    的往返一致性整套推倒重写,而那套往返本身是被判据守着的。
    """
    if not trgs:
        return ""
    trg_rows = ",\n".join(
        f"        ({_q(n)}, {_q('public.' + t)}, {_q(d)})" for n, t, d in trgs)
    return f"""
    FOR r IN SELECT * FROM (VALUES
{trg_rows}
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[{tag}] 触发器的目标表 % 不存在', r.tname;
        END IF;
        SELECT public.defgeo_ident_trigger(r.cname, r.tname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[{tag}] 触发器 % 不在 % 上 —— 「按名判存」会被**别的表上的同名触发器**'
                '骗过(pg_trigger.tgname 不绑表);实际宿主:%',
                r.cname, r.tname,
                COALESCE((SELECT c2.relname FROM pg_trigger g2
                            JOIN pg_class c2 ON c2.oid = g2.tgrelid
                           WHERE g2.tgname = r.cname AND NOT g2.tgisinternal
                           LIMIT 1), '(没有同名触发器)');
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[{tag}] 触发器 % 的定义/启用状态/函数体与预期不符 —— '
                '同名放行触发器、DISABLE 掉的触发器、被换成 RETURN NEW 的函数体,'
                '三种都长成「它在」的样子;期望「%」实得「%」',
                r.cname, r.cdef, actual;
        END IF;
    END LOOP;
"""


def _col_section(tag: str, cols: list[tuple[str, str, str]]) -> str:
    """列合同那一段。``cols`` 为空时整段不生成。

    第三项是三合一串:``format_type(atttypid,atttypmod)|notnull=<t/f>|default=<expr|->``

      · ``format_type`` 带 **typmod** —— ``character varying(64)`` 与
        ``character varying(255)`` 在 ``information_schema.data_type`` 里
        **都是** ``character varying``:只核 data_type 的守卫对"错长度"零判别;
      · ``attnotnull`` —— 可空性;
      · ``pg_get_expr(adbin)`` —— **default 轴**。050 原来这一轴零核验,
        而 Codex 的三个 poison 有两个走的正是它(DEFAULT 777)。
        没有 default 时期望值写字面量 ``-``,不是 NULL:NULL 参与 ``<>`` 比较
        结果是 NULL 而不是 true,整条 IF 会静默不成立 —— 那是一个**恒绿**的守卫。
    """
    if not cols:
        return ""
    col_rows = ",\n".join(
        f"        ({_q(n)}, {_q('public.' + t)}, {_q(d)})" for n, t, d in cols)
    return f"""
    FOR r IN SELECT * FROM (VALUES
{col_rows}
    ) AS t(cname, tname, cdef) LOOP
        IF to_regclass(r.tname) IS NULL THEN
            RAISE EXCEPTION '[{tag}] 列 %.% 的目标表不存在', r.tname, r.cname;
        END IF;
        SELECT public.defgeo_ident_column(r.tname, r.cname) INTO actual;
        IF actual IS NULL THEN
            RAISE EXCEPTION
                '[{tag}] 列 %.% 不存在 —— ADD COLUMN 没生效?', r.tname, r.cname;
        END IF;
        IF actual <> r.cdef THEN
            RAISE EXCEPTION
                '[{tag}] 列 %.% 的合同不符:期望「%」实得「%」 —— '
                'ADD COLUMN IF NOT EXISTS 按**列名**判存,库上已有同名列时直接 no-op,'
                '错类型/错长度/错 default 一律不纠正;必须人工 ALTER 或 DROP 后重跑',
                r.tname, r.cname, r.cdef, actual;
        END IF;
    END LOOP;
"""


def build_block(tag: str, cons: list[tuple[str, str, str]],
                idxs: list[tuple[str, str, str]],
                trgs: list[tuple[str, str, str]] | None = None,
                cols: list[tuple[str, str, str]] | None = None) -> str:
    """cons/idxs 里的第三项是从真 PG16 读到的定义。

    🔴 [工单 E3-3] 允许**单侧**文件:051 只有索引、052/054 只有约束。
       旧版无条件生成两张 VALUES 表,空的那一侧会产出
       ``FOR r IN SELECT * FROM (VALUES\n\n    ) AS t(...)`` —— PG 语法错误。
       这就是这三个文件此前进不了本轴的技术原因(不是"不该进")。
       两侧都空仍然拒绝:那样这块 readiness 是在对空气说话。
    """
    trgs = list(trgs or [])
    cols = list(cols or [])
    if not cons and not idxs and not trgs and not cols:
        raise SystemExit(f"[{tag}] 约束/索引/触发器/列都为空 —— readiness 块会对空气说话,拒绝生成")
    con_section = _con_section(tag, cons)
    idx_section = _idx_section(tag, idxs)
    trg_section = _trg_section(tag, trgs)
    col_section = _col_section(tag, cols)
    ident_funcs = IDENT_FUNCS_SQL
    return f"""{BEGIN.format(tag=tag)}
-- 🔴 本块由 scripts/defgeo_readiness_gen.py **机械生成**,不要手改。
--    期望对象集 = 本文件声明的全部对象(ADD CONSTRAINT {len(cons)} 条 + @index-guard {len(idxs)} 个 + CREATE TRIGGER {len(trgs)} 个 + ADD COLUMN {len(cols)} 列),
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
{ident_funcs}
DO $readiness{tag}$
DECLARE r RECORD; actual TEXT; ok BOOLEAN;
BEGIN{col_section}{con_section}{idx_section}{trg_section}    RAISE NOTICE '[{tag}] exact schema readiness 通过({len(cols)} 列 + {len(cons)} 约束 + {len(idxs)} 索引 + {len(trgs)} 触发器逐字对上)';
END $readiness{tag}$;
{END.format(tag=tag)}"""


_ROW = re.compile(r"^\s*\('((?:[^']|'')+)',\s*'public\.((?:[^']|'')+)',\s*'((?:[^']|'')*)'\)",
                  re.MULTILINE)


def _unq(s: str) -> str:
    return s.replace("''", "'")


def parse_block_rows(block: str) -> tuple[list, list]:
    """把生成块里的 VALUES 表读回 (名, 表, 定义)。

    🔴 [工单 E3-3] 段数是 1 或 2,不再固定 2 —— 单侧文件(051 只有索引 /
       052、054 只有约束)只生成一段。段的归属**按段体里的取定义函数判**
       (``defgeo_ident_constraint`` vs ``defgeo_ident_index``),不按出现顺序猜:
       按顺序猜会在单侧文件上把索引表读成约束表,而两边都是三元组、
       解析不会报错 —— 那是一个静默错位,比报错贵得多。
    """
    parts = block.split("FOR r IN SELECT * FROM (VALUES")
    if not 2 <= len(parts) <= 5:
        raise AssertionError(
            f"readiness 块里的 VALUES 表数不是 1~4(实得 {len(parts) - 1})")
    buckets: dict[str, list] = {"con": [], "idx": [], "trg": [], "col": []}
    for seg in parts[1:]:
        rows = [(_unq(a), _unq(b), _unq(c)) for a, b, c in _ROW.findall(seg)]
        head = seg.split("END LOOP;")[0]
        # 🔴 归属按段体里的**取定义函数**判,不按出现顺序猜:按顺序猜会在单侧文件上
        #    把索引表读成约束表,而三边都是三元组、解析不会报错 —— 静默错位比报错贵。
        # 🔴 [P0 2026-09-02] 归属锚跟着取值口一起换:段体里已经没有
        #    pg_get_*def 了,再按老名字判会**四段全不命中**并当场 AssertionError。
        #    改了取值口却忘了改这里的分类锚,是同一笔里最容易漏的一处。
        hit = [k for k, fn in (("con", "defgeo_ident_constraint"),
                               ("idx", "defgeo_ident_index"),
                               ("trg", "defgeo_ident_trigger"),
                               ("col", "defgeo_ident_column")) if fn in head]
        if len(hit) != 1:
            raise AssertionError(
                f"readiness 段归属不唯一(命中 {hit})—— 无法归属")
        buckets[hit[0]].extend(rows)
    return buckets["con"], buckets["idx"], buckets["trg"], buckets["col"]


def block_is_canonical(sql: str, tag: str) -> tuple[bool, str]:
    """生成块必须**逐字**等于把它自己的 VALUES 行重新喂给生成器的结果。

    🔴 为什么不能用 substring 断言:独立审计 IM-05/IM-06 分别把
       ``i.indrelid = r.tname::regclass`` 改成 ``(… OR TRUE)``、
       把 ``IF actual IS NULL THEN`` 改成 ``IF FALSE THEN`` ——
       两发都让 readiness 退回按名判存 / 让"索引缺失"分支变成死代码,
       而 ``assert "…" in block`` 全绿。逐字比对把整块骨架一次钉死。
    """
    b, e = BEGIN.format(tag=tag), END.format(tag=tag)
    if b not in sql or e not in sql:
        return False, f"[{tag}] 没有机械生成的 readiness 块(标记缺失)"
    block = sql[sql.index(b): sql.index(e) + len(e)]
    cons, idxs, trgs, cols = parse_block_rows(block)
    # 🔴 [工单 E3-3] 单侧文件合法(051 只有索引 / 052、054 只有约束),
    #    **全空**仍然非法 —— 那块 readiness 是在对空气说话。
    if not cons and not idxs and not trgs and not cols:
        return False, (f"[{tag}] VALUES 表解析出空集"
                       f"(约束 {len(cons)} / 索引 {len(idxs)} / 触发器 {len(trgs)}"
                       f" / 列 {len(cols)})")
    rebuilt = build_block(tag, cons, idxs, trgs, cols)
    if rebuilt != block:
        for i, (x, y) in enumerate(zip(block.splitlines(), rebuilt.splitlines())):
            if x != y:
                return False, (f"[{tag}] readiness 块第 {i + 1} 行与生成器输出不符\n"
                               f"  文件里:{x}\n  生成器:{y}")
        return False, (f"[{tag}] readiness 块行数与生成器输出不符"
                       f"(文件 {len(block.splitlines())} / 生成器 {len(rebuilt.splitlines())})")
    return True, ""


def fetch_defs(dsn: str, cons: list[tuple[str, str]],
               idxs: list[tuple[str, str]],
               trgs: list[tuple[str, str]] | None = None,
               cols: list[tuple[str, str]] | None = None) -> tuple[list, list, list, list]:
    conn = psycopg2.connect(dsn)
    # 🔴 期望值与运行期校验**用同一批函数**算 —— 两边各写一份 SQL 正是
    #    「同一谓词写两处」,而漂掉的那一份没有任何判据在守。
    with conn.cursor() as _c0:
        _c0.execute(IDENT_FUNCS_SQL)
    conn.commit()
    out_c, out_i, out_t, out_col = [], [], [], []
    try:
        with conn.cursor() as cur:
            for name, tbl in cons:
                cur.execute(
                    "SELECT public.defgeo_ident_constraint(%s, %s)",
                    (name, f"public.{tbl}"))
                row = cur.fetchone()
                if not row:
                    raise SystemExit(f"真 PG16 里找不到约束 {name} on {tbl} —— 期望值取不到,拒绝生成")
                out_c.append((name, tbl, row[0]))
            for name, tbl in idxs:
                cur.execute(
                    "SELECT public.defgeo_ident_index(%s, %s)",
                    (name, f"public.{tbl}"))
                row = cur.fetchone()
                if not row:
                    raise SystemExit(f"真 PG16 里找不到索引 {name} on {tbl} —— 期望值取不到,拒绝生成")
                out_i.append((name, tbl, row[0]))
            for name, tbl in (trgs or []):
                cur.execute("SELECT public.defgeo_ident_trigger(%s, %s)", (name, f"public.{tbl}"))
                row = cur.fetchone()
                if not row:
                    raise SystemExit(
                        f"真 PG16 里找不到触发器 {name} on {tbl} —— 期望值取不到,拒绝生成")
                out_t.append((name, tbl, row[0]))
            for name, tbl in (cols or []):
                # 🔴 参数序是 (表, 列) —— 与另外三个 (名, 表) 相反。
                #    上一版照抄了 (name, table),函数查不到行返 NULL,
                #    然后在 _q(None) 上炸成 AttributeError,离真因两层远。
                cur.execute("SELECT public.defgeo_ident_column(%s, %s)",
                            (f"public.{tbl}", name))
                row = cur.fetchone()
                if not row:
                    raise SystemExit(
                        f"真 PG16 里找不到列 {tbl}.{name} —— 期望值取不到,拒绝生成")
                out_col.append((name, tbl, row[0]))
        conn.rollback()
    finally:
        conn.close()
    return out_c, out_i, out_t, out_col


#: 老块的起点标记(fa8aecc50 那轮写的),整块连同其上方注释一起被替换。
_OLD_HEAD = "-- ── exact schema readiness"


def splice(sql: str, tag: str, block: str) -> str:
    b, e = BEGIN.format(tag=tag), END.format(tag=tag)
    if b in sql:
        i = sql.index(b)
        j = sql.index(e) + len(e)
        return sql[:i] + block + sql[j:]
    if _OLD_HEAD in sql:
        i = sql.index(_OLD_HEAD)
        # 老块以它自己的 `END $$;` 收尾
        j = sql.index("END $$;", i) + len("END $$;")
        return sql[:i] + block + sql[j:]
    return sql.rstrip("\n") + "\n\n" + block + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if "test" not in a.dsn.rsplit("/", 1)[-1].lower():
        raise SystemExit("安全栓:期望值只能从一次性测试库现读")

    for tag, rel in READINESS_FILES.items():
        path = ROOT / rel
        with io.open(path, encoding="utf-8", newline="") as fh:
            sql = fh.read()
        cons = declared_constraints(sql)
        idxs = declared_indexes(sql, rel)
        trgs = declared_triggers(sql)
        cols = declared_columns(sql)
        cdefs, idefs, tdefs, coldefs = fetch_defs(a.dsn, cons, idxs, trgs, cols)
        block = build_block(tag, cdefs, idefs, tdefs, coldefs)
        new = splice(sql, tag, block)
        print(f"{tag} {rel}: 约束 {len(cons)} · 索引 {len(idxs)} · 触发器 {len(trgs)}"
              f" · 列 {len(cols)} · {'写入' if a.apply else '预览'}")
        if a.apply:
            with io.open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
