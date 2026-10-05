# -*- coding: utf-8 -*-
"""检测「拿系统目录的**渲染文本**做全等比较」这种写法。

## 为什么要有这把锁

`pg_get_constraintdef()` / `pg_get_indexdef()` / `pg_get_triggerdef()` /
`pg_get_expr()` 返回的是 PG **当场渲染**出来的文本。同一条逻辑约束,
在「刚 ADD 出来」与「经过 dump→restore(文本被重新解析)」之后渲染**不同**:

    建库:  CHECK (((mode)::text = ANY ((ARRAY['a'::character varying, …])::text[])))
    还原:  CHECK (((mode)::text = ANY (ARRAY[('a'::character varying)::text, …])))

⇒ 拿它跟写死的字符串比,**首次部署过、还原副本上重放必炸**。
2026-09-01 实测:869 条 CHECK 里 29 条会漂。

`db/migration_035`(2026-08-18)已经诊断过这件事并改用语义比对,注释写得很清楚;
而 040–054(08-21~26)是在它之后写的,又把病带了回来。
**注释传不出去,门才传得出去** —— 所以有了这把锁。

## 合法例外

把渲染输出**喂给身份函数**(`defgeo_ident_*`)再比,是对的做法:
比较的是语义身份,不是渲染。所以判非法看的是**比较运算符两侧的最外层调用**,
而不是「这行里有没有出现 pg_get_*」。
"""
from __future__ import annotations

import re

RENDER_FUNCS = ("pg_get_constraintdef", "pg_get_indexdef",
                "pg_get_triggerdef", "pg_get_expr")
IDENT_PREFIX = "defgeo_ident_"
_CMP = ("<>", "!=", "=")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _strip_comments(sql: str) -> str:
    """去掉 `--` 行注释与 /* */ 块注释 —— 注释里提到函数名不算用它。"""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"(?m)--.*$", "", sql)


def _call_span(sql: str, start: int) -> int:
    """从函数名起点找到它调用的右括号位置(括号配平);找不到返回 -1。"""
    i = sql.find("(", start)
    if i < 0:
        return -1
    depth = 0
    while i < len(sql):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _outermost_call_before(sql: str, pos: int) -> str | None:
    """比较运算符**左侧**紧邻的那个调用,取它的函数名(最外层)。

    做法:从 pos 往左跳过空白;若是 ')' 就往左配平到对应 '(',再读它前面的标识符。
    否则直接读左边的标识符(变量名)。
    """
    i = pos - 1
    while i >= 0 and sql[i].isspace():
        i -= 1
    if i < 0:
        return None
    if sql[i] == ")":
        depth = 0
        while i >= 0:
            if sql[i] == ")":
                depth += 1
            elif sql[i] == "(":
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        i -= 1
        while i >= 0 and sql[i].isspace():
            i -= 1
    j = i
    while j >= 0 and (sql[j].isalnum() or sql[j] in "_."):
        j -= 1
    name = sql[j + 1:i + 1]
    return name or None


def _outermost_call_after(sql: str, pos: int) -> str | None:
    """比较运算符**右侧**紧邻的那个标识符/调用名。"""
    m = _WORD.search(sql, pos)
    if not m or not sql[pos:m.start()].strip() == "":
        # 右边不是标识符开头(比如字符串字面量),不是我们要抓的形态
        return None
    return m.group(0)


def _expr_is_render(expr: str) -> bool:
    """这段**被赋值的表达式**里,渲染函数的值有没有流进结果。

    判「里面出现过」是安全的,**前提是** expr 已经被正确限定为
    `SELECT` 与 `INTO` 之间的那一段(选择列表),不含 `WHERE` ——
    `SELECT count(*) INTO cnt FROM … WHERE pg_get_constraintdef(oid) <> '…'`
    的选择列表只有 `count(*)`,渲染函数在 WHERE 里,不会误伤。

    🔴 为什么不能只判「最外层是渲染函数」:索引那半的真实写法是
       `regexp_replace(lower(pg_get_indexdef(i.oid)),'\\s','','g')`
       —— **最外层是包装函数**,污染穿不过去,于是整族漏掉
       (2026-09-01 实测漏掉 2 个存量文件的索引守卫)。
       这是「接线锁用包含判定」那一族的镜像:该用包含的地方用了同一性。

    唯一的合法包装是 `defgeo_ident_*(…)`:它比的是**语义身份**,不是渲染。
    """
    e = expr.strip().rstrip(";").strip()
    e = re.sub(r"::[A-Za-z_][A-Za-z0-9_ ]*(\[\])?$", "", e).strip()   # 去尾部 cast
    m = _WORD.match(e)
    if m and m.group(0).lower().startswith(IDENT_PREFIX):
        end = _call_span(e, 0)
        if end == len(e) - 1:
            return False                   # 合法:整段就是一个身份函数调用
    return any(re.search(r"\b" + re.escape(fn) + r"\s*\(", e) for fn in RENDER_FUNCS)


def _tainted_vars(sql: str) -> set[str]:
    """找出「值**直接**来自渲染函数」的变量。

    两种写法都收(且都只认最外层):
      · `SELECT pg_get_constraintdef(oid) INTO actual FROM …`
      · `actual := pg_get_constraintdef(oid);`
    🔴 外面套了 `defgeo_ident_*` 的**不算污染** —— 那正是正确的做法。
    """
    out: set[str] = set()
    # 🔴 先找 INTO 再**向前**找同一条语句内最近的 SELECT。
    #    第一版写成 `SELECT(.*?)INTO` 惰性前向匹配,结果从文件里更早的那个
    #    `SELECT * FROM (VALUES …)` 起头、跨过整条语句 —— 正样本当场报 0。
    #    语句边界用 `;`(PL/pgSQL 里 DO 块的 `$tag$` 不含裸分号问题,够用)。
    for m in re.finditer(r"(?is)\bINTO\b\s+([A-Za-z_][A-Za-z0-9_,\s]*?)\s*(?:\bFROM\b|;)", sql):
        head = sql[:m.start()]
        semi = head.rfind(";")
        sel = head.rfind("SELECT", semi + 1 if semi >= 0 else 0)
        if sel < 0:
            sel = head.lower().rfind("select", semi + 1 if semi >= 0 else 0)
        if sel < 0:
            continue
        # 🔴 `SELECT a, b INTO x, y` 要**按位配对**:只认「整段就是一个调用」的话,
        #    `SELECT pg_get_constraintdef(oid), convalidated INTO actual, ok` 会漏。
        items = _split_top_commas(sql[sel + 6:m.start()])
        names = [n.strip().lower() for n in m.group(1).split(",") if n.strip()]
        for expr, name in zip(items, names):
            if _WORD.fullmatch(name) and _expr_is_render(expr):
                out.add(name)
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*:=\s*([^;]+);", sql):
        if _expr_is_render(m.group(2)):
            out.add(m.group(1).lower())
    return out


def _split_top_commas(s: str) -> list[str]:
    """按**顶层**逗号切分(括号内的逗号不算)。"""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def find_render_text_comparisons(sql: str) -> list[str]:
    """返回违规点描述列表;空列表 = 干净。"""
    sql = _strip_comments(sql)
    tainted = _tainted_vars(sql)
    hits: list[str] = []
    for op in _CMP:
        for m in re.finditer(re.escape(op), sql):
            # `<=` `>=` `!=` 里的 `=` 不重复计
            if op == "=" and (sql[m.start() - 1] in "<>!:" or sql[m.start() + 1] == "="):
                continue
            left = _outermost_call_before(sql, m.start())
            right = _outermost_call_after(sql, m.end())
            for side, name in (("左", left), ("右", right)):
                if not name:
                    continue
                low = name.lower()
                if low.startswith(IDENT_PREFIX):
                    continue                      # 合法:比的是语义身份
                if low in RENDER_FUNCS:
                    line = sql.count(chr(10), 0, m.start()) + 1
                    hits.append(f"第~{line} 行:{op} 的{side}侧直接是 {name}() 的渲染输出")
                elif low in tainted:
                    line = sql.count(chr(10), 0, m.start()) + 1
                    hits.append(f"第~{line} 行:{op} 的{side}侧是变量 {name},"
                                f"它的值直接来自渲染函数")
    # 同一处可能被左右两侧各记一次,去重保序
    seen, uniq = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq
