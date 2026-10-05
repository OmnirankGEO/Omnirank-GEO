"""从仓库 DDL 里解析出每张表【被声明过】的列集。

为什么要解析而不是把列表抄一份进测试:抄一份就是把同一个 typo 抄两遍,
锁不住任何东西。解析出来的集合是独立信源 —— 白名单里写错一个字母,
它就不在声明集里,断言当场红。

覆盖三种加列写法(本仓三种都在用):
  1. CREATE TABLE IF NOT EXISTS <t> ( ... )
  2. ALTER TABLE <t|{var}> ADD COLUMN IF NOT EXISTS <col> ...
     —— {var} 形式回溯最近的 `for <var> in (...)` 元组取表名
  3. _safe_add_column(cursor, "<t>", "<col>", ...)
"""
import os
import re

_SQL_TABLE_KEYWORDS = {
    "PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK", "EXCLUDE", "LIKE",
}

_SCAN_DIRS = ("db", "scripts", "services", "api")
_SCAN_EXTS = (".py", ".sql")


def _iter_sources(root):
    for d in _SCAN_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if fn.endswith(_SCAN_EXTS):
                    path = os.path.join(dirpath, fn)
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        yield path, fh.read()


def _create_table_columns(src, table):
    cols = set()
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + re.escape(table) + r"\s*\(",
        re.IGNORECASE,
    )
    for m in pattern.finditer(src):
        body = src[m.end():]
        for raw in body.split("\n"):
            line = raw.strip()
            if line.startswith(")"):
                break
            if not line or line.startswith("--"):
                continue
            token = re.split(r"[\s(]", line, 1)[0].strip('",')
            if not token or token.upper() in _SQL_TABLE_KEYWORDS:
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
                cols.add(token)
    return cols


def _resolve_templated_table(src, pos, var_expr):
    """`ALTER TABLE {_tbl} ...` → 回溯最近的 `for _tbl in ("a", "b")` 取表名。"""
    var = var_expr.strip("{}").strip()
    head = src[:pos]
    loops = list(re.finditer(
        r"for\s+" + re.escape(var) + r"\s+in\s+\(([^)]*)\)", head))
    if not loops:
        return set()
    tuple_body = loops[-1].group(1)
    return {a or b for a, b in re.findall(r'"([\w]+)"|\'([\w]+)\'', tuple_body)}


def _alter_add_columns(src, table):
    cols = set()
    for m in re.finditer(
        r"ALTER\s+TABLE\s+(\{?[\w_]+\}?)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([\w_]+)",
        src, re.IGNORECASE,
    ):
        tbl_expr, col = m.group(1), m.group(2)
        if tbl_expr.startswith("{"):
            targets = _resolve_templated_table(src, m.start(), tbl_expr)
        else:
            targets = {tbl_expr}
        if table in targets:
            cols.add(col)
    return cols


def _safe_add_columns(src, table):
    return {
        m.group(2)
        for m in re.finditer(
            r'_safe_add_column\(\s*\w+\s*,\s*"([\w_]+)"\s*,\s*"([\w_]+)"', src)
        if m.group(1) == table
    }


def declared_columns(root, table):
    """仓库里为 `table` 声明过的全部列名。"""
    cols = set()
    for _path, src in _iter_sources(root):
        cols |= _create_table_columns(src, table)
        cols |= _alter_add_columns(src, table)
        cols |= _safe_add_columns(src, table)
    return cols
