# -*- coding: utf-8 -*-
"""#94 v2 · 只取**语法位置确定**的列引用。

v1 用自由 tokenizer,6397 FAIL 基本全假阳:字符串值 'running'、函数名 EXTRACT/ABS、
别名 `AS count`、占位符 %s→s 全被当成列名。
v2 规则(精度优先,宁漏不误):
  · 先剥字符串字面量与占位符 —— 它们是**值**不是列
  · WHERE:只取**比较运算符左侧**的裸标识符
  · SELECT 列表:仅当整个列表是「纯裸标识符逗号分隔」才取;含函数/别名/*/表达式 ⇒ 整段跳过
  · UPDATE SET:取 `col =` 左侧;INSERT:取显式列清单
  · 一律排除 表名.列名 形式里的表名,和 SQL 保留字
"""
from __future__ import annotations

import ast, io, json, os, pathlib, re, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(__file__).resolve().parents[1]
DUMP = pathlib.Path(os.environ.get("PROD_SCHEMA_DUMP",
    r"C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql"))
SCAN = ["api", "db", "services", "workflows", "tools", "agents", "advisors", "middleware", "auth"]

#: 约束行的关键词 —— 只有这几个词**整词**出现在行首才是约束,不是列。
_CONSTRAINT_HEAD = re.compile(r"(?i)(CONSTRAINT|PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY|EXCLUDE)\b")


def parse_schema(dump_text: str) -> dict[str, set[str]]:
    """从 `pg_dump --schema-only` 文本解析出 表 → 列集合。

    🔴 约束行必须按**词边界**判,不能用 `startswith`。
       `"check_date date NOT NULL".upper()` = `"CHECK_DATE …"`,**以 CHECK 开头** ——
       `startswith("CHECK")` 会把这一整列当成 CHECK 约束丢掉。
       凡列名以 check / unique / constraint / primary / foreign / exclude 开头的,
       都会从 schema 里消失,于是所有引用它们的 SQL 全被判 FAIL。

       实测代价:2026-09-05 的残留 FAIL 16 条里 **13 条**是这一个前缀 bug 造的假阳,
       而我已经在给 Deploy 准备加性迁移工单 —— 而生产 dump 里明明写着
       `check_date date NOT NULL` 且带 `UNIQUE (keyword_id, keyword_source, check_date)`。
       **给已存在的列再 ADD COLUMN 一次**,是一次本可以发生的生产事故。

       戳穿它的不是判据,是一处**内部矛盾**:约束引用了一个我认为不存在的列。
       第一反应「这约束是陈旧的吧」是错的 —— 那是给两条互斥读数补一个和解前提,
       零区分力还会让人停止追查。去看 dump 原文才对。

    抽成模块级函数是为了让判据能**驱动这段真代码**,而不是在判据里复制一份解析逻辑。
    """
    schema: dict[str, set[str]] = {}
    for m in re.finditer(r"CREATE TABLE (?:public\.)?\"?([A-Za-z_]\w*)\"?\s*\((.*?)\n\);", dump_text, re.S):
        cols: set[str] = set()
        for line in m.group(2).split("\n"):
            t = line.strip().rstrip(",")
            if not t or _CONSTRAINT_HEAD.match(t):
                continue
            nm = t.split()[0].strip('"')
            if re.fullmatch(r"[A-Za-z_]\w*", nm):
                cols.add(nm.lower())
        if cols:
            schema[m.group(1).lower()] = cols
    return schema


def run(verbose: bool = False) -> dict:
    """跑一次普查,返回 {pass, stale, fail, unres, kinds}。"""
    schema = parse_schema(io.open(DUMP, encoding="utf-8", errors="replace").read())
    dump = io.open(DUMP, encoding="utf-8", errors="replace").read()
    VIEWS = {m.group(1).lower() for m in re.finditer(r"CREATE VIEW (?:public\.)?\"?([A-Za-z_]\w*)\"?", dump)}

    added: dict[str, set[str]] = {}
    for p in list(ROOT.glob("db/**/*.py")) + list(ROOT.glob("scripts/**/*.sql")):
        try: s = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError: continue
        for m in re.finditer(r"ALTER TABLE\s+(?:IF EXISTS\s+)?(?:public\.)?\"?(\w+)\"?\s+ADD COLUMN\s+(?:IF NOT EXISTS\s+)?\"?(\w+)\"?", s, re.I):
            added.setdefault(m.group(1).lower(), set()).add(m.group(2).lower())
        for m in re.finditer(r"_safe_add_column\(\s*\w+\s*,\s*[\"'](\w+)[\"']\s*,\s*[\"'](\w+)[\"']", s):
            added.setdefault(m.group(1).lower(), set()).add(m.group(2).lower())

    RESERVED = set("""select from where and or not in is null order by group having limit offset as on join
    left right inner outer full cross union all distinct case when then else end asc desc set values into
    returning with exists between like ilike true false interval cast using conflict do nothing update
    insert delete default current_date current_timestamp now array any filter over partition for share
    nowait desc asc offset fetch only""".split())

    def strip_values(sql: str) -> str:
        # 🔴 SQL 串内部的 `--` 行注释 / `/* */` 块注释 —— 第三种「把『提到』当『在用』」的形态
        #    (前两种:Python docstring、自由 tokenize)。实测 db/monitoring_db.py 的查询里有
        #        -- [Deploy-CTO 2026-05-30] allow_confirmed=True 时也放行 confirmed …
        #    而 `allow_confirmed` 其实是 **Python 形参**,不是列;注释里带 `=` 就被当成了 WHERE 比较。
        sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
        sql = re.sub(r"--[^\n]*", " ", sql)
        sql = re.sub(r"'(?:[^']|'')*'", " ?VAL? ", sql)          # 字符串字面量
        sql = re.sub(r"%\((\w+)\)s|%s|\?", " ?PH? ", sql)         # 占位符
        sql = re.sub(r"::\w+", " ", sql)                           # 类型转换
        return sql

    # 🔴 `=(?!>)`:PostgreSQL 具名参数 `make_interval(secs => %s)` 里的 `secs =>` **不是**列比较。
    #    v2 第一版没排它 ⇒ secs / days / hours / mins 全被当成列名(21 处假阳)。
    CMP = r"(?:=(?!>)|!=|<>|<=|>=|<|>|\bIS\b|\bIN\b|\bLIKE\b|\bILIKE\b|\bBETWEEN\b)"
    def where_cols(seg: str) -> set[str]:
        out = set()
        for m in re.finditer(r"(?<![\w.])([a-z_]\w*)\s*" + CMP, strip_values(seg), re.I):
            c = m.group(1).lower()
            if c not in RESERVED: out.add(c)
        return out

    BARE_LIST = re.compile(r"^[\s\w,\"]+$")
    def select_cols(sel: str) -> set[str] | None:
        """只在「纯裸标识符逗号分隔」时返回列集;否则 None(整段跳过)。"""
        s = sel.strip()
        if "*" in s or "(" in s or re.search(r"\bAS\b", s, re.I) or not BARE_LIST.match(s):
            return None
        out = set()
        for part in s.split(","):
            c = part.strip().strip('"').lower()
            if not c or "." in c: return None
            if not re.fullmatch(r"[a-z_]\w*", c) or c in RESERVED: return None
            out.add(c)
        return out or None

    SQLISH = re.compile(r"\b(SELECT|UPDATE|INSERT\s+INTO|DELETE\s+FROM)\b", re.I)
    UNRES = re.compile(r"\bJOIN\b|\bUNION\b|\bWITH\b|\(\s*SELECT\b", re.I)
    res = {"pass":0,"stale":[],"fail":[],"unres":0,"kinds":{}}
    def unres(k): res["unres"]+=1; res["kinds"][k]=res["kinds"].get(k,0)+1

    for d in SCAN:
        base = ROOT/d
        if not base.exists(): continue
        for p in base.rglob("*.py"):
            try: tree = ast.parse(io.open(p, encoding="utf-8", errors="replace").read())
            except SyntaxError: continue
            rel = p.relative_to(ROOT).as_posix()
            # 🔴 排除 docstring:模块/类/函数的首个字符串表达式。
            #    docstring 里引用 SQL 是**说明**不是执行(#107 同病:把「提到」当「在写」)。
            #    实测两例:tools/media_cost_ssot.py:1 的散文 `markup = mhz_config.markup_ratio`
            #    被当成 WHERE 里的列比较。
            docs = set()
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and fn.body:
                    b0 = fn.body[0]
                    if isinstance(b0, ast.Expr) and isinstance(b0.value, ast.Constant) and isinstance(b0.value.value, str):
                        docs.add(id(b0.value))
            for n in ast.walk(tree):
                if id(n) in docs:
                    continue
                if isinstance(n, ast.JoinedStr):
                    if SQLISH.search(ast.unparse(n)): unres("f-string")
                    continue
                if not (isinstance(n, ast.Constant) and isinstance(n.value, str)): continue
                sql = n.value
                if not SQLISH.search(sql): continue
                # 注释里的 SQL 关键词不算代码(同上,主循环这一层也要剥)
                sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
                sql = re.sub(r"--.*", " ", sql)
                if UNRES.search(sql): unres("JOIN/子查询/CTE"); continue
                pairs = []
                m = re.search(r"\bSELECT\s+(.+?)\s+FROM\s+(?:public\.)?\"?(\w+)\"?(?:\s|$|;)", sql, re.I|re.S)
                if m:
                    tbl = m.group(2).lower()
                    sc = select_cols(m.group(1))
                    if sc is None: unres("SELECT 列表含函数/别名/*")
                    w = re.search(r"\bWHERE\s+(.+?)(?:\bORDER\b|\bGROUP\b|\bLIMIT\b|\bFOR\b|$)", sql, re.I|re.S)
                    cols = (sc or set()) | (where_cols(w.group(1)) if w else set())
                    if cols: pairs.append((tbl, cols))
                m = re.search(r"\bUPDATE\s+(?:public\.)?\"?(\w+)\"?\s+SET\s+(.+?)(?:\bWHERE\b|\bRETURNING\b|$)", sql, re.I|re.S)
                if m:
                    tbl = m.group(1).lower()
                    setc = {c.strip().strip('"').lower() for c in re.findall(r"(?<![\w.])([a-z_]\w*)\s*=(?!>)", strip_values(m.group(2)), re.I)} - RESERVED
                    w = re.search(r"\bWHERE\s+(.+?)(?:\bRETURNING\b|$)", sql, re.I|re.S)
                    cols = setc | (where_cols(w.group(1)) if w else set())
                    if cols: pairs.append((tbl, cols))
                m = re.search(r"\bINSERT\s+INTO\s+(?:public\.)?\"?(\w+)\"?\s*\(([^)]*)\)", sql, re.I|re.S)
                if m:
                    cols = {c.strip().strip('"').lower() for c in m.group(2).split(",") if c.strip()}
                    cols = {c for c in cols if re.fullmatch(r"[a-z_]\w*", c)} - RESERVED
                    if cols: pairs.append((m.group(1).lower(), cols))
                m = re.search(r"\bDELETE\s+FROM\s+(?:public\.)?\"?(\w+)\"?(.*)$", sql, re.I|re.S)
                if m:
                    w = re.search(r"\bWHERE\s+(.+)$", m.group(2), re.I|re.S)
                    if w:
                        c = where_cols(w.group(1))
                        if c: pairs.append((m.group(1).lower(), c))
                if not pairs: unres("形态不支持"); continue
                for tbl, cols in pairs:
                    if tbl not in schema:
                        unres("表不在 dump"); continue
                    if tbl in VIEWS and not schema.get(tbl): unres("视图无列"); continue
                    for c in sorted(cols):
                        if c in schema[tbl]: res["pass"]+=1
                        elif c in added.get(tbl,set()): res["stale"].append({"f":rel,"l":n.lineno,"t":tbl,"c":c})
                        else: res["fail"].append({"f":rel,"l":n.lineno,"t":tbl,"c":c,"sql":re.sub(r"\s+"," ",sql)[:110]})


    if verbose:
        print(f"  分母 {len(schema)} 表 · 树内 ADD COLUMN {len(added)} 表")
        print(f"  PASS {res['pass']} · STALE {len(res['stale'])} · **FAIL {len(res['fail'])}** · UNRESOLVED {res['unres']}")
        for k,v in sorted(res["kinds"].items(), key=lambda x:-x[1])[:6]: print(f"      {v:5d}  {k}")
        print(f"  🔴 FAIL 逐条:")
        for r in res["fail"]: print(f"      {r['f']}:{r['l']}  {r['t']}.{r['c']}\n          {r['sql']}")
        print(f"  STALE 逐条:")
        for r in res["stale"]: print(f"      {r['f']}:{r['l']}  {r['t']}.{r['c']}")
    return res


if __name__ == "__main__":
    r = run(verbose=True)
    if len(sys.argv) > 1:
        io.open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(r, ensure_ascii=False, indent=2))
