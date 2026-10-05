"""迁移里「索引存在性守卫」的**单一扫描谓词**(census / readiness / 改写脚本共用)。

═══════════════════════════════════════════════════════════════════════════
为什么要有这个文件
═══════════════════════════════════════════════════════════════════════════
``CREATE [UNIQUE] INDEX IF NOT EXISTS idx_x ON t (...)`` 的判存是
**按 schema 内的索引名**,不绑表。同名索引只要长在**别的表**上,
这条语句就静默跳过 —— 目标表上的索引**永远建不出来**,而迁移返回成功。

  · 普通索引缺失 = 静默性能塌(查询还对,只是慢);
  · **UNIQUE 索引缺失 = 唯一性约束静默消失 = 数据完整性洞**。
    ``uq_defgeo_attempt_single_inflight`` 这种 partial unique 缺了,
    「一个 run 同时只能有一个 inflight attempt」这条不变式就没人执行了。

而 ``scripts/prestart.py`` **每次部署无条件重放全部 manifest 迁移**(无追踪表),
所以这不是"某次迁移可能出错",是每次部署都在重演。

═══════════════════════════════════════════════════════════════════════════
🔴 同一谓词只写这一处
═══════════════════════════════════════════════════════════════════════════
本仓 2026-08 的教训:同一谓词写两处 ⇒ 必有一处没人验。
census 负向锁、readiness 期望对象集导出、以及一次性改写脚本
**都从这里取**,任何一方改了口径,另外两方立刻跟着变(要红一起红)。

扫描器本身的判别力由 ``tests/.../test_migration_index_scope_pg.py`` 的
正负样本判据守着 —— 把这里的正则挖空,那些判据必须当场红。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "IndexStatement",
    "IndexPredicate",
    "manifest_sql_files",
    "strip_sql_noise",
    "scan_ifne_index_statements",
    "scan_bound_index_guards",
    "scan_declared_indexes",
    "bound_guard_defects",
    "scan_index_name_predicates",
    "scan_drop_index_sites",
    # 轴C(三单)
    "scan_bound_drop_guards",
    "bound_drop_guard_defects",
    "scan_dynamic_drop_sites",
    "scan_naked_drop_index_sites",
    "scan_dynamic_drop_any",
]


# ══════════════════════════════════════════════════════════════════════════
# 1. manifest 分母(不手写文件清单)
# ══════════════════════════════════════════════════════════════════════════
def manifest_sql_files(root: Path) -> list[Path]:
    """从 ``db/migration_manifest.py`` 机械取出全部迁移文件。

    手写一份文件名单,漏掉的那一份不会让任何判据变红 —— 本仓前科。
    这里直接读 SSOT 的 ``MIGRATIONS`` 列表。
    """
    return [root / rel for rel in manifest_relpaths(root)]


def manifest_relpaths(root: Path) -> list[str]:
    """``MIGRATIONS`` 的相对路径列表 —— **直接 import SSOT**,不再正则解析。

    🔴 第一版用正则从源码里抠 ``MIGRATIONS = [...]``。独立审计的 IM-13 指出:
       扫描器自己在返回处加一句 ``[:112]`` 就能把 042–046(防御 GEO 自己的五个迁移)
       抽走,而四条"地板"判据(files≥110 / bound≥400 / unique≥100 / preds≥20)
       一条都不会响 —— **计数式地板挡不住定向抽取**。
       直接 import 之后,分母就是 SSOT 本身;再要动它必须动
       ``db/migration_manifest.py``,而那是保护文件(零 diff 锁盯着)。
       判据侧另有一条"返回值必须逐项等于 MIGRATIONS"的集合锁。
    """
    import sys as _sys
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from db.migration_manifest import MIGRATIONS  # noqa: PLC0415

    rels = [r for r in MIGRATIONS if str(r).endswith(".sql")]
    if not rels:
        raise AssertionError("MIGRATIONS 里一个 .sql 都没有 —— 分母源塌了")
    return list(rels)


# ══════════════════════════════════════════════════════════════════════════
# 2. 词法:注释 / 单引号串 / dollar-quote 串一律屏蔽掉再匹配
# ══════════════════════════════════════════════════════════════════════════
#: 被屏蔽的字符替换成空格,**长度逐字保持** —— 这样偏移量仍可直接切原文。
_MASK = " "


def strip_sql_noise(sql: str) -> str:
    """把 ``--`` 行注释、``/* */`` 块注释、``'...'`` 串屏蔽成等长空白。

    dollar-quote(``$$``/``$tag$``)**不屏蔽内容**:040/044 这类文件里
    DO 块体本身就是承重 SQL,屏蔽掉就等于把一半守卫扫没了(零分母假绿)。
    只把 dollar 定界符本身记录下来,交由调用方判断"是否位于 DO 块内"。
    """
    out = list(sql)
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = _MASK
            i = j
            continue
        # 块注释(PostgreSQL 允许嵌套)
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql[j] == "/" and j + 1 < n and sql[j + 1] == "*":
                    depth += 1
                    j += 2
                    continue
                if sql[j] == "*" and j + 1 < n and sql[j + 1] == "/":
                    depth -= 1
                    j += 2
                    continue
                j += 1
            for k in range(i, min(j, n)):
                out[k] = _MASK
            i = j
            continue
        # dollar-quote:块体内的 SQL 是**承重**的(040/044 的守卫就住在 DO 块里),
        # 所以内容不整段屏蔽。但块体里的 ``--`` 行注释仍然是注释 ——
        # 🔴 独立审计 IM-12:第一版整段跳过 ⇒ DO 块里注掉的一行旧写法照样被算成
        #    真语句,轴A 无故变红(而顶层的负样本判据宣称"注释不算",在块里并不成立)。
        #    块体里的**单引号串不屏蔽**:``EXECUTE 'CREATE INDEX IF NOT EXISTS …'``
        #    是真的会执行的,必须仍然被扫到(fail-closed)。
        if ch == "$":
            m = re.match(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    i = n
                    continue
                _mask_comments_inside(sql, out, i + len(tag), end)
                i = end + len(tag)
                continue
        # 单引号串
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            for k in range(i, min(j + 1, n)):
                out[k] = _MASK
            i = j + 1
            continue
        i += 1
    return "".join(out)


def _mask_comments_inside(sql: str, out: list[str], start: int, end: int) -> None:
    """把 dollar-quote 块体 ``[start, end)`` 里的 ``--`` 行注释屏蔽成等长空白。

    只屏蔽**不在单引号串里**的 ``--``:块体里的 ``'a--b'`` 是字符串,不是注释。
    """
    i = start
    while i < end:
        ch = sql[i]
        if ch == "'":                       # 跳过整个串(内容不动)
            j = i + 1
            while j < end:
                if sql[j] == "'":
                    if j + 1 < end and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if ch == "-" and i + 1 < end and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = end if j == -1 or j > end else j
            for k in range(i, j):
                out[k] = _MASK
            i = j
            continue
        i += 1


def _dollar_regions(sql: str) -> list[tuple[int, int]]:
    """全部 dollar-quote 区间(含定界符)。用来判断某语句是否落在 DO/函数体内。"""
    regions: list[tuple[int, int]] = []
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] == "$":
            m = re.match(r"\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    break
                regions.append((i, end + len(tag)))
                i = end + len(tag)
                continue
        i += 1
    return regions


# ══════════════════════════════════════════════════════════════════════════
# 3. 语句模型
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class IndexStatement:
    """一条 ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` 的机械解析结果。"""

    path: str            # 仓库相对路径
    index: str           # 索引名
    table: str           # 目标表(不带 schema 前缀,原文里带就剥掉)
    unique: bool
    body: str            # ``(col, ...) [WHERE ...]`` 原样片段(不含结尾分号)
    start: int           # 在原文里的字符偏移(含 CREATE)
    end: int             # 结尾分号之后
    inside_dollar: bool  # 是否位于 DO 块 / 函数体内

    @property
    def key(self) -> tuple[str, str]:
        return (self.index, self.table)


#: 标识符:裸的或**双引号包起来的**。
#: 🔴 第一版只认裸标识符。独立审计 IM-01 用 ``CREATE UNIQUE INDEX IF NOT EXISTS
#:    "ux_xxx" ON …`` 把轴A 的负向锁整个绕过去,并在真 PG16 上把索引建了出来
#:    —— 加两个引号就不红了。同一个扫描器在轴C 里**是**认引号的,
#:    轴A 不认是遗漏,不是设计。
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z_0-9$]*)'
_IFNE_HEAD = re.compile(
    r"CREATE\s+(?P<uniq>UNIQUE\s+)?INDEX\s+(?P<conc>CONCURRENTLY\s+)?"
    rf"IF\s+NOT\s+EXISTS\s+(?P<idx>{_IDENT})\s+"
    rf"ON\s+(?:(?P<sch>{_IDENT})\s*\.\s*)?(?P<tbl>{_IDENT})"
    r"(?P<using>\s+USING\s+[A-Za-z_][A-Za-z_0-9]*)?",
    re.IGNORECASE,
)


def _statement_end(masked: str, start: int) -> int:
    """从 ``start`` 起找到分号(括号深度为 0 处)。noise 已屏蔽,不会被串里的 ``;`` 骗。"""
    depth = 0
    i = start
    n = len(masked)
    while i < n:
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return i + 1
        i += 1
    return n


def scan_ifne_index_statements(sql: str, rel: str) -> list[IndexStatement]:
    """扫出全部**旧形态**(``IF NOT EXISTS`` 名字判存)索引语句。

    census 负向锁盯的就是这个函数的返回长度。
    """
    masked = strip_sql_noise(sql)
    dollars = _dollar_regions(sql)
    found: list[IndexStatement] = []
    for m in _IFNE_HEAD.finditer(masked):
        end = _statement_end(masked, m.end())
        body = sql[m.end():end].strip()
        if body.endswith(";"):
            body = body[:-1].rstrip()
        found.append(
            IndexStatement(
                path=rel,
                index=m.group("idx").strip('"'),
                table=m.group("tbl").strip('"'),
                unique=bool(m.group("uniq")),
                body=(m.group("using") or "") + " " + body if m.group("using") else body,
                start=m.start(),
                end=end,
                inside_dollar=any(a < m.start() < b for a, b in dollars),
            )
        )
    return found


# ══════════════════════════════════════════════════════════════════════════
# 4. 新形态:绑定 indrelid 的守卫
# ══════════════════════════════════════════════════════════════════════════
#: 新守卫的结构指纹 —— 同表跳过 / 异表 RAISE / 不存在真建,三条腿缺一不可。
_BOUND_GUARD = re.compile(
    r"--\s*@index-guard\s+(?P<idx>[A-Za-z_][A-Za-z_0-9$]*)\s+ON\s+"
    r"(?P<tbl>[A-Za-z_][A-Za-z_0-9$]*)\s+(?P<kind>unique|plain)\b",
    re.IGNORECASE,
)


def scan_bound_index_guards(sql: str, rel: str) -> list[IndexStatement]:
    """扫出全部**新形态**(表绑定)索引守卫,按其自带的 ``-- @index-guard`` 锚。

    锚是注释,但它**必须**与同一块里的 ``indrelid`` / ``RAISE EXCEPTION`` /
    ``CREATE [UNIQUE] INDEX`` 同时成立(见 :func:`bound_guard_defects`),
    所以不能靠只写注释来骗过判据。
    """
    out: list[IndexStatement] = []
    for m in _BOUND_GUARD.finditer(sql):
        end = _guard_block_end(sql, m.end())
        out.append(
            IndexStatement(
                path=rel,
                index=m.group("idx"),
                table=m.group("tbl"),
                unique=m.group("kind").lower() == "unique",
                body=sql[m.end():end],
                start=m.start(),
                end=end,
                inside_dollar=False,
            )
        )
    return out


def _guard_block_end(sql: str, start: int) -> int:
    """守卫块从锚注释起,到**它自己的** ``END IF;`` 为止。

    🔴 第一版找的是 ``$idxguard$;``。对**内联**守卫(已在别人的 DO 块里,
       没有自己的 ``$idxguard$``)那会一路找到**下一条独立守卫**的收尾 ——
       body 里于是白捡了下一条守卫的 indrelid / RAISE / CREATE,
       缺腿检测**恒绿**。实测两处 body 长到 2503 / 2944 字符。
       撕锁 SELF-03 把它照出来了。
       两种形态的守卫都只有一层 ``IF … ELSIF … ELSE … END IF;``,
       所以"锚之后的第一个 END IF;"对两种形态都是自己的收尾。
    """
    tail = re.search(r"END\s+IF\s*;", sql[start:], re.IGNORECASE)
    if tail:
        return start + tail.end()
    fallback = sql.find("$idxguard$;", start)
    return len(sql) if fallback == -1 else fallback + len("$idxguard$;")


def bound_guard_defects(stmt: IndexStatement) -> list[str]:
    """一条新守卫必须同时具备的三条腿 —— 缺哪条报哪条。

    🔴 逐条**锚在这个守卫自己的索引名与表名上**,不是"块里出现过这个词":
       第一版只查 ``"INDRELID" in body``,而 RAISE 的诊断子查询里本来就有
       ``LEFT JOIN pg_class t ON t.oid = i.indrelid`` —— 于是把承重的
       ``IF`` 分支上的绑定拆掉,检测照样全绿(撕锁 SELF-03 实测)。
       现在比的是模板逐字片段,顺带把"绑到了别的表"也一起挡住。
    """
    seg = " ".join(stmt.body.split())
    idx, tbl = stmt.index, stmt.table
    kw = "CREATE UNIQUE INDEX" if stmt.unique else "CREATE INDEX"
    defects: list[str] = []
    # 🔴 分支关键字必须一起比。独立审计 IM-03:把承重分支的 ``IF EXISTS (``
    #    改一个词成 ``IF NOT EXISTS (``,三条腿的模板串一个不少 ⇒ 全绿,
    #    而守卫从此永远走"幂等跳过",索引一辈子建不出来
    #    (真 PG16 实测:``uq_publish_idem_command_id`` 静默消失、重放零报错)。
    if ("IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            f"WHERE c.relname = '{idx}' AND i.indrelid = to_regclass('public.{tbl}')) THEN "
            "NULL;") not in seg:
        defects.append(
            f"承重分支不是「IF EXISTS(…{idx} 绑到 public.{tbl}…) THEN NULL;」"
            "(绑定被拆 / 分支被取反 / 跳过腿被改)")
    if f"RAISE EXCEPTION '[index-guard] {idx} 已存在但不在 public.{tbl} 上" not in seg:
        defects.append(f"{idx} 异表同名没有 RAISE(会静默跳过)")
    if f"{kw} {idx} ON public.{tbl} " not in seg:
        defects.append(f"没有 `{kw} {idx} ON public.{tbl}` 分支(不存在时建不出来)")
    return defects


# ══════════════════════════════════════════════════════════════════════════
# 5. 声明对象集(readiness 期望对象的机械分母)
# ══════════════════════════════════════════════════════════════════════════
def scan_declared_indexes(sql: str, rel: str) -> list[IndexStatement]:
    """一个迁移文件**声明**的全部索引(新旧两种形态并集)。

    readiness 的"期望索引集"从这里导出,不手写 —— 手写清单漏掉的那一条
    不会让任何判据变红。
    """
    merged = scan_ifne_index_statements(sql, rel) + scan_bound_index_guards(sql, rel)
    seen: set[tuple[str, str]] = set()
    out: list[IndexStatement] = []
    for s in sorted(merged, key=lambda x: x.start):
        if s.key in seen:
            continue
        seen.add(s.key)
        out.append(s)
    return out


# ══════════════════════════════════════════════════════════════════════════
# 6. 第二根轴:**索引名判存谓词**(不是建索引,是"这个索引在不在")
# ══════════════════════════════════════════════════════════════════════════
# 同一个病有三种语法形态,只堵一种等于没堵(本仓 2026-08 记过三次):
#   ① CREATE [UNIQUE] INDEX IF NOT EXISTS      —— 建的时候被骗 → 索引不存在
#   ② SELECT … FROM pg_indexes  WHERE indexname='X'  —— 反查时被骗 → readiness 报假绿
#   ③ SELECT … FROM pg_class    WHERE relname  ='X'  —— 同上,还会驱动 DROP
# ① 由 :func:`scan_ifne_index_statements` 管,②③ 由这里管。
@dataclass(frozen=True)
class IndexPredicate:
    """一条"按索引名判存"的谓词。"""

    path: str
    line: int
    source: str          # pg_indexes | pg_class
    bound: bool          # 是否绑了表(tablename= / indrelid=)
    text: str            # 归一空白后的谓词原文


_PRED_INDEXNAME = re.compile(r"\bindexname\b", re.IGNORECASE)
_PRED_PGCLASS_IDX = re.compile(
    r"pg_get_indexdef|relkind\s*=\s*'i'|JOIN\s+pg_index\b|FROM\s+pg_index\b", re.IGNORECASE)
#: 🔴 两个方向都要认。独立审计 IM-10:把操作数写反成 ``'idx_ghost' = c2.relname``
#:    就能让整条不绑表的谓词**退出分母** —— preds 计数不变、unbound 仍为 0、全绿。
#:    「同一目标的语法形态要一次枚举全」在本仓已经记过三次。
_PRED_NAME_LITERAL = re.compile(
    r"\b(relname|indexname)\s*(=|IN)\s*"
    r"|'[^']+'\s*(=|IN)\s*(\w+\.)?(relname|indexname)\b", re.IGNORECASE)
#: ═══ "绑了表" 的三种真实成立形态 ═══
#: 仓里三种都存在,少认一种就是永久假红;认得太宽就是永久假绿。
#:   ① **紧作用域内直接绑** —— 同一条 SELECT 里 ``indrelid = …`` / ``tablename = …``
#:      / ``(tablename, indexname) IN (…)``
#:   ② **先按名取行、再单独比 indrelid**(``migration_quote_snapshots_and_archives``
#:      的 §index contract):比对与取行不在同一条语句里 ⇒ 只能放宽到块级,
#:      但**必须是把 indrelid 与一个 regclass 相比**,不是块里出现过 indrelid 这个词
#:   ③ **整串比 pg_get_indexdef**(``migration_geo_observation_aggregate_basis``):
#:      期望串里含 ``ON public.<表>``,长错表当场不等
#:
#: 🔴 为什么不能只在块级查 ``\bindrelid\b``(第一版就是,被撕锁 SELF-06/07/08 打穿):
#:    一个 DO 块里往往有好几条谓词,拆掉**其中一条**的绑定,块里别处还留着
#:    ``indrelid`` 这个词 ⇒ 锁照样全绿。这条锁存在的唯一理由就是抓这种回退,
#:    抓不到就等于没有锁。
_PRED_TIGHT_BOUND = re.compile(
    r"\bindrelid\s*(=|IS\s+DISTINCT\s+FROM|<>)"
    r"|\btablename\s*(=|IN\b)"
    r"|\(\s*tablename\s*,\s*indexname\s*\)\s*IN\b",
    re.IGNORECASE,
)
#: 逃生门②:紧跟其后把 indrelid **拿去比**(不是光提到这个词)。
#: 仓里两种真实写法都要认:
#:   · ``index_row.indrelid IS DISTINCT FROM format('public.%I',…)::regclass``(quote_snapshots)
#:   · ``idx_rec.indrelid <> audit_oid``(whitelabel §6 合同函数 —— 比的是先取好的 oid 变量,
#:     不是 regclass 字面量;只认 regclass 会把仓里**最好**的那份写法判成假红)
_PRED_BLOCK_RECHECK = re.compile(
    r"\bindrelid\s*(<>|=|!=|IS\s+DISTINCT\s+FROM)", re.IGNORECASE)
#: 逃生门③:块里整串比 ``pg_get_indexdef``,且期望串里真的出现 ``ON public.<表>``
#: (归一化去空白后写作 ``onpublic.<表>``)。
_PRED_BLOCK_DEFCMP = re.compile(r"pg_get_indexdef", re.IGNORECASE)
_PRED_DEF_HAS_TABLE = re.compile(r"on\s*public\s*\.\s*[A-Za-z_]", re.IGNORECASE)
#: 形态④:**join 到宿主表**再用它的 relname(``dealer_inventory_resale`` /
#: ``direct_service_refund_agreements`` 的 index contract 就是这么写的:
#: ``JOIN pg_class t ON t.oid=x.indrelid … AND t.relname='<表>'``,
#: 或把 ``t.relname`` 拌进 digest —— 长错表 digest 当场不等)。
#: 要求 join **与** 对该别名 relname 的引用同时成立,只有 join 不算。
_PRED_JOIN_HOST = re.compile(
    r"JOIN\s+pg_class\s+(?P<a>\w+)\s+ON\s+(?P=a)\.oid\s*=\s*\w+\.indrelid", re.IGNORECASE)


def _predicate_is_bound(tight: str, block: str) -> bool:
    if _PRED_TIGHT_BOUND.search(tight):
        return True
    m = _PRED_JOIN_HOST.search(tight)
    if m and re.search(rf"\b{m.group('a')}\.relname\b", tight, re.IGNORECASE):
        return True
    if _PRED_BLOCK_RECHECK.search(block):
        return True
    return bool(_PRED_BLOCK_DEFCMP.search(block) and _PRED_DEF_HAS_TABLE.search(block))


def _enclosing_query(masked: str, pos: int) -> tuple[int, int]:
    """**紧**作用域 = 该谓词所在的那一层括号 / 语句(到未配对括号或分号为止)。"""
    depth = 0
    i = pos
    while i > 0:
        ch = masked[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        i -= 1
    start = i + 1
    depth = 0
    j = pos
    n = len(masked)
    while j < n:
        ch = masked[j]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        j += 1
    return start, j


def _escape_window(sql: str, masked: str, tight_end: int, block_end: int) -> int:
    """逃生门②③ 能看多远 —— **紧跟其后、且本身不含索引名字面量**的几条语句。

    🔴 为什么不能放到整块(第一版就是,自证脚本当场打穿):
       一个 DO 块里往往有好几条谓词。把**其中一条**的绑定拆掉,块里别的谓词
       还带着 ``indrelid``/``pg_get_indexdef`` ⇒ 逃生门替它顶了绿,
       而它自己已经能被诱饵骗过。逃生门只能借"同一件事的下半句",
       不能借"隔壁另一个索引的绑定"。
    """
    i = tight_end
    for _ in range(4):
        if i >= block_end or i - tight_end > 1500:
            break
        j = masked.find(";", i)
        if j == -1 or j >= block_end:
            j = min(block_end, i + 1500)
        else:
            j += 1
        if _PRED_NAME_LITERAL.search(sql[i:j]):
            break                     # 下一条语句是**另一条**索引名谓词 —— 到此为止
        i = j
    return i


def _enclosing_block(sql: str, masked: str, pos: int) -> tuple[int, int]:
    """块作用域 = 所在 DO 块 / 函数体;不在块里就是所在顶层语句。"""
    for a, b in _dollar_regions(sql):
        if a < pos < b:
            return a, b
    i = masked.rfind(";", 0, pos)
    j = masked.find(";", pos)
    return i + 1, (len(masked) if j == -1 else j + 1)


def scan_index_name_predicates(sql: str, rel: str) -> list[IndexPredicate]:
    """轴B:除 ``@index-guard`` 块**之外**的、按索引名判存的谓词。

    🔴 为什么把 ``@index-guard`` 块整个排除:守卫里的 RAISE 诊断子查询
       (``WHERE c.relname = '<idx>' AND c.relnamespace = 'public'::regnamespace``)
       **故意**是命名空间级的 —— 它要回答的正是"那这个名字到底长在哪张表上"。
       把它算成"不绑表"就是 459 条永久假红。守卫块自己的正确性由轴A 的
       :func:`bound_guard_defects` 逐字模板锁守,不归轴B。
    """
    masked = strip_sql_noise(sql)
    # 轴A 的建索引守卫 + 轴C 的 DROP 守卫,块内一律不归轴B ——
    # 两种守卫的 RAISE 诊断子查询都**故意**是命名空间级的(它要回答"名字长在哪张表上"),
    # 算成"不绑表"就是几百条永久假红。守卫自身由各自的逐字模板锁守。
    guard_spans = ([(g.start, g.end) for g in scan_bound_index_guards(sql, rel)]
                   + [(g.start, g.end) for g in scan_bound_drop_guards(sql, rel)])
    seen: set[tuple[int, int]] = set()
    out: list[IndexPredicate] = []
    for rx, src in ((_PRED_INDEXNAME, "pg_indexes"), (_PRED_PGCLASS_IDX, "pg_class")):
        for m in rx.finditer(masked):
            if any(a <= m.start() < b for a, b in guard_spans):
                continue
            span = _enclosing_query(masked, m.start())
            if span in seen:
                continue
            tight = sql[span[0]:span[1]]
            if not _PRED_NAME_LITERAL.search(tight):
                continue
            seen.add(span)
            _ba, bb = _enclosing_block(sql, masked, m.start())
            win_end = _escape_window(sql, masked, span[1], bb)
            out.append(IndexPredicate(
                path=rel,
                line=sql[:span[0]].count("\n") + 1,
                source=src,
                bound=_predicate_is_bound(tight, sql[span[0]:win_end]),
                text=" ".join(tight.split()),
            ))
    return sorted(out, key=lambda p: p.line)


#: ``DROP INDEX [IF EXISTS] <name>`` 在 PostgreSQL 语法上**无法绑表** ——
#: 名字撞了就删错表。这一根轴不在本单的改动范围里(改它是破坏性语义变更,
#: 归 Owner/Review 裁),但**必须被数住**:冻结当前站点集合,
#: 新增一处就红,免得它悄悄长大。
def _drop_sites_raw(sql: str) -> list[tuple[int, str, int]]:
    """``(行号, 索引名, **真实字符偏移**)``。

    🔴 ``DROP INDEX a, b, c;`` 是合法 PG 语法。第一版只取**第一个**名字,
       独立审计 IM-08 就用逗号把第二个名字藏进去。逗号列表必须整串枚举。
    🔴 独立审计 FG-1/FG-3/FG-4:偏移必须是**真实字符偏移**,不能按
       ``splitlines()`` 逐行折算 —— 折算会让 (a) 写在守卫 ``END IF;`` 同一行行尾的
       裸 DROP 被算进守卫区间、(b) 蹭动态站点行号的裸 DROP 被一并豁免、
       (c) CRLF 下整体错位。
    """
    masked = strip_sql_noise(sql)
    out: list[tuple[int, str, int]] = []
    head = re.compile(r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?", re.IGNORECASE)
    name = re.compile(r"\s*(?:public\s*\.\s*)?(%I|[A-Za-z_\"][A-Za-z_0-9$\"]*)", re.IGNORECASE)
    for m in head.finditer(masked):
        line = sql[:m.start()].count("\n") + 1
        pos = m.end()
        while True:
            nm = name.match(masked, pos)
            if not nm:
                break
            out.append((line, nm.group(1).strip('"'), m.start()))
            pos = nm.end()
            comma = re.match(r"\s*,", masked[pos:pos + 40])
            if not comma:
                break
            pos += comma.end()
    return out


def scan_drop_index_sites(sql: str, rel: str) -> list[tuple[str, int, str]]:
    """``(文件, 行号, 索引名)`` —— 全部 ``DROP INDEX`` 站点(含逗号列表的每个名字)。"""
    return [(rel, line, nm) for line, nm, _off in _drop_sites_raw(sql)]


# ══════════════════════════════════════════════════════════════════════════
# 7. 轴C:``DROP INDEX`` 的表绑定守卫(三单)
# ══════════════════════════════════════════════════════════════════════════
# 老形态 ``DROP INDEX [IF EXISTS] <name>`` 语法上**不带表名**。PG16 实测(轴C §一):
#   · 同名索引长在别的表上 → **静默把那张无辜表的索引删掉**,零报错;
#   · 名字不存在 → ``IF EXISTS`` 打 NOTICE 跳过;不带 ``IF EXISTS`` 则 ERROR;
#   · 同名但不是索引(relkind r/v/m/S)→ ERROR ``"X" is not an index``,
#     **且 ``IF EXISTS`` 压不住**(实测:``DROP INDEX IF EXISTS <表名>`` 照样 ERROR)。
# 新守卫按同一口径:同表真删 / 异表或异类 RAISE / 不存在按原语句是否带 IF EXISTS 决定。
_BOUND_DROP_GUARD = re.compile(
    r"--\s*@drop-index-guard\s+(?P<idx>[A-Za-z_][A-Za-z_0-9$]*)\s+ON\s+"
    r"(?P<tbl>[A-Za-z_][A-Za-z_0-9$]*)\s+(?P<mode>if-exists|strict)\b",
    re.IGNORECASE,
)


def _drop_guard_block_end(sql: str, start: int) -> int:
    """DROP 守卫的**整块**边界。

    🔴 独立审计 FG-1/FG-7:第一版收在"锚之后第一个 ``END IF;``",于是
       ``END IF;`` 与 ``END $dropguard$;`` 之间的一切都在 body **之外** ——
       在那里补一条裸 DROP、或补一句 ``EXCEPTION WHEN OTHERS THEN NULL;``,
       逐字模板锁与逐字等价锁**同时**看不见(实测两者都 identical)。
       独立守卫收在 ``END $dropguard$;``;内联守卫没有自己的 dollar 尾,
       仍收在 ``END IF;``(它本来就长在别人的块里)。
    """
    tail = sql.find("END $dropguard$;", start)
    if tail != -1:
        nxt = sql.find("-- @drop-index-guard", start)
        if nxt == -1 or tail < nxt:
            return tail + len("END $dropguard$;")
    m = re.search(r"END\s+IF\s*;", sql[start:], re.IGNORECASE)
    return start + m.end() if m else len(sql)


def scan_bound_drop_guards(sql: str, rel: str) -> list[IndexStatement]:
    """扫出全部**新形态**的 DROP 守卫(复用 :class:`IndexStatement`;
    ``unique`` 字段在这里表示"原语句带 IF EXISTS")。"""
    out: list[IndexStatement] = []
    for m in _BOUND_DROP_GUARD.finditer(sql):
        end = _drop_guard_block_end(sql, m.end())
        out.append(IndexStatement(
            path=rel, index=m.group("idx"), table=m.group("tbl"),
            unique=m.group("mode").lower() == "if-exists",
            body=sql[m.end():end], start=m.start(), end=end, inside_dollar=False))
    return out


def bound_drop_guard_defects(stmt: IndexStatement) -> list[str]:
    """DROP 守卫的三条腿,逐字锚在它自己的索引名+表名上(与轴A 同纪律)。"""
    seg = " ".join(stmt.body.split())
    idx, tbl = stmt.index, stmt.table
    defects: list[str] = []
    drop_ifex = "IF EXISTS " if stmt.unique else ""
    if ("IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            f"WHERE c.relname = '{idx}' AND i.indrelid = to_regclass('{tbl}')) THEN "
            f"DROP INDEX {drop_ifex}{idx};") not in seg:
        defects.append(f"承重分支不是「同表命中 → DROP INDEX {drop_ifex}{idx}」"
                       "(绑定被拆 / 分支被取反 / 真删腿被摘 / TOCTOU 的 IF EXISTS 被拿掉)")
    if f"RAISE EXCEPTION '[drop-index-guard] {idx} 不在 {tbl} 上" not in seg:
        defects.append(f"{idx} 异表/异类同名没有 RAISE(会静默删掉别的表的索引)")
    tail = ("NULL;  -- 不存在 → 幂等跳过" if stmt.unique
            else f"""RAISE EXCEPTION 'index "{idx}" does not exist'""")
    if " ".join(tail.split()) not in seg:
        defects.append("不存在分支与原语句的 IF EXISTS 口径不一致"
                       f"({'应幂等跳过' if stmt.unique else '应 undefined_object 报错'})")
    # 🔴 守卫体内**恰好一条** DROP,而且就是它自己那一条(独立审计 FG-1:
    #    在块里补第二条裸 DROP,逐字模板锁只查"该有的串在不在",不查"有没有多的")。
    drops = [nm for _ln, nm, _off in _drop_sites_raw(stmt.body)]
    if drops != [idx]:
        defects.append(f"守卫体内的 DROP 不是恰好一条自己的({drops})")
    # 🔴 FG-7:``EXCEPTION WHEN OTHERS THEN NULL;`` 会把 RAISE 腿整个吞掉。
    if re.search(r"\bEXCEPTION\s+WHEN\b", seg, re.IGNORECASE):
        defects.append("守卫块里有 EXCEPTION 处理段 —— 它会把异表同名的 RAISE 吞掉")
    return defects


#: 动态形态:``EXECUTE`` 一条**以 DROP INDEX 开头的串**。名字运行期算,静态守卫加不上。
#: 🔴 独立审计 FG-2:第一版只认 ``EXECUTE format('DROP INDEX``,于是
#:    ``EXECUTE 'DROP INDEX IF EXISTS ' || quote_ident(x);`` 与
#:    ``EXECUTE format('DROP %s public.%I','INDEX',…)`` 两种拼串形态
#:    让**裸锁 / 动态锁 / 站点普查三个分母同时为 0** —— 真 PG16 实测每次部署
#:    静默删掉 ``bank_cards`` 上一条运行期建的 UNIQUE 索引,99 条判据全绿。
#:    「同一目标的语法形态要一次枚举全」在本仓已经记过四次。
_DYNAMIC_DROP = re.compile(
    r"EXECUTE\b[^;]{0,160}?'\s*DROP\s+(?:%s\s+)?INDEX\b", re.IGNORECASE | re.DOTALL)


#: 🔴 FG-2b:``EXECUTE format('DROP %s public.%I', 'INDEX', …)`` 里 ``INDEX`` 这个词
#:    **一个字都不出现**,任何"认 INDEX 关键字"的正则都抓不到。
#:    所以再加一根**更粗但可冻结**的轴:``EXECUTE`` 里任何以 ``DROP`` 开头的串。
#:    manifest 里今天只有 5 处(4 处是 ``DROP TABLE pg_temp.*`` 的临时表清理),
#:    冻结即可 —— 新增任何一处动态 DROP 都会红,逼人来看它是不是在删索引。
_DYNAMIC_DROP_ANY = re.compile(
    r"EXECUTE\b[^;]{0,160}?'\s*DROP\s", re.IGNORECASE | re.DOTALL)


def scan_dynamic_drop_any(sql: str, rel: str) -> list[tuple[str, int, str]]:
    """``(文件, 行号, 归一化片段)`` —— manifest 里全部动态 ``EXECUTE … 'DROP …`` 站点。"""
    out = []
    for m in _DYNAMIC_DROP_ANY.finditer(sql):
        out.append((rel, sql[:m.start()].count("\n") + 1,
                    " ".join(sql[m.start():m.start() + 70].split())))
    return out


def scan_dynamic_drop_sites(sql: str, rel: str) -> list[tuple[str, int, int, int]]:
    """``(文件, 行号, 起, 止)`` —— 止 = 该 ``EXECUTE`` 语句的分号(按**字符区间**豁免,不按行号)。

    口径取 :data:`_DYNAMIC_DROP_ANY`(更宽):藏在动态串里的索引 DROP 不算"裸"
    (静态守卫加不上),但会被 :func:`scan_dynamic_drop_any` 的冻结集逮住。
    """
    out = []
    for m in _DYNAMIC_DROP_ANY.finditer(sql):
        semi = sql.find(";", m.end())
        out.append((rel, sql[:m.start()].count("\n") + 1, m.start(),
                    len(sql) if semi == -1 else semi + 1))
    return out


def scan_naked_drop_index_sites(sql: str, rel: str) -> list[tuple[str, int, str]]:
    """轴C 负向锁的分母:**不在** ``@drop-index-guard`` 块里、也不是动态形态的裸 DROP。

    改写完之后这个集合必须是**空的**;新迁移带着裸 DROP 进来当场红。
    """
    guards = scan_bound_drop_guards(sql, rel)
    dyn = [(a, b) for _r, _ln, a, b in scan_dynamic_drop_sites(sql, rel)]
    out = []
    for line, name, off in _drop_sites_raw(sql):
        rel_ = rel
        owner = next((g for g in guards if g.start <= off < g.end), None)
        if owner is not None:
            # 🔴 守卫体内的真删语句**只豁免它自己那一个名字**。
            #    独立审计 IM-08 的同型手法在这里又活了一次(撕锁 AXISC-04 / EXT-06 实测):
            #    ``DROP INDEX public.<自己>, public.<别人>;`` —— 第二个名字整条藏在守卫
            #    span 里,"落在守卫里就跳过"会把它一并放行,而它**根本没被任何 indrelid 绑过**。
            if name == owner.index:
                continue
            out.append((rel_, line, name))
            continue
        # 🔴 动态豁免按**字符区间**,不按行号(FG-3:蹭同一行的裸 DROP 会被一并豁免)
        if any(a <= off < b for a, b in dyn) or name == "%I":
            continue                      # 动态形态,另册冻结
        out.append((rel_, line, name))
    return out
