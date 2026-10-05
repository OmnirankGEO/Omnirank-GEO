# -*- coding: utf-8 -*-
"""扫「代码会往 `public.topics.status` 写进哪些值」。

🔴 这个扫描器是 WO_225-c1 §8.4b 结构锁的仪器。仪器坏了要**出声**,不许静默少数:
   每一处写入点的状态值都必须能被解析出来;解析不出来的进 `unresolved`,
   判据拿它跟一份**冻结的、逐条写明理由**的清单比对 —— 多一条就红。
   (本仓的老病:扫描器悄悄少算,读数看起来很干净,而它其实什么都没扫到。)

🔴 只算 `public.topics`,不算 `social_topics` / `geo_douyin_topics` / `topic_embeddings`
   —— 它们是**不同的表**,各有各的状态词表。第一版枚举正是因为跨表捞,
   把 archived/making/done/used/skipped 也算了进来。
"""
from __future__ import annotations

import ast
import io
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]

#: 目标表。`(?<![_\w.])` 挡住 social_topics / geo_douyin_topics / t.topics 之类。
_STMT = re.compile(r"(?<![_\w.])(INSERT\s+INTO|UPDATE)\s+topics\b", re.IGNORECASE)

#: 状态字面量的形状:小写 + 下划线。`'标题生成中...'` 这种中文占位不是状态。
_STATUS_WORD = re.compile(r"^[a-z][a-z0-9_]*$")


def production_files() -> List[str]:
    """生产 .py。排除 tests/ 与 scripts/(后者是一次性校验脚本,不是生产写入点)。"""
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out if not f.startswith(("tests/", "scripts/"))]


def _sql_strings(tree: ast.AST) -> List[Tuple[str, ast.Call]]:
    """所有 `X.execute(<字符串常量>, ...)` 调用 -> (sql, call)。"""
    found: List[Tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "execute"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((first.value, node))
    return found


def _split_top_level(text: str) -> List[str]:
    """按逗号切分,忽略括号内与引号内的逗号。"""
    parts, depth, cur, quote = [], 0, [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _param_literals(call: ast.Call) -> List[object]:
    """execute 的第二个参数(元组/列表)里每一项:字面量给值,其余给 None。"""
    if len(call.args) < 2:
        return []
    arg = call.args[1]
    if not isinstance(arg, (ast.Tuple, ast.List)):
        return []
    out: List[object] = []
    for el in arg.elts:
        if isinstance(el, ast.Constant):
            out.append(el.value)
        else:
            out.append(None)
    return out


def _from_update(sql: str, params: List[object],
                 found: Set[str], unresolved: List[str], where: str) -> None:
    """`UPDATE topics SET ... status = <x>`。"""
    # 🔴 `(?<![_\w])` 不可省:没有它,`billing_status = 'not_charged'` 会被当成
    #    `status = 'not_charged'` —— 实测过一次,扫出两个根本不是 topics.status 的值。
    #    「一个串被无关行满足」在本仓是累犯形态。
    for m in re.finditer(r"(?<![_\w])status\s*=\s*('([^']*)'|%s)", sql, re.IGNORECASE):
        if m.group(2) is not None:
            if _STATUS_WORD.match(m.group(2)):
                found.add(m.group(2))
            continue
        # `%s` —— 数它前面有几个占位符,去参数里取
        idx = sql[:m.start()].count("%s")
        if idx < len(params) and isinstance(params[idx], str):
            if _STATUS_WORD.match(params[idx]):
                found.add(params[idx])
                continue
        unresolved.append("%s · UPDATE status=%%s 取不到字面量(第 %d 个占位)" % (where, idx))


def _from_insert(sql: str, params: List[object],
                 found: Set[str], unresolved: List[str], where: str) -> None:
    """`INSERT INTO topics (cols) VALUES (vals)`。"""
    m = re.search(r"INSERT\s+INTO\s+topics\s*\(([^)]*)\)\s*VALUES\s*\((.*?)\)\s*(RETURNING|$)",
                  sql, re.IGNORECASE | re.DOTALL)
    if not m:
        unresolved.append("%s · INSERT 语句解析不出 列/值 两段" % where)
        return
    cols = [c.strip().strip('"') for c in _split_top_level(m.group(1))]
    vals = _split_top_level(m.group(2))
    if "status" not in cols:
        return            # 这条 INSERT 不写 status,用列默认值(DDL 是 'draft')
    if len(cols) != len(vals):
        unresolved.append("%s · INSERT 列数 %d 与值数 %d 对不上" % (where, len(cols), len(vals)))
        return
    v = vals[cols.index("status")].strip()
    lit = re.fullmatch(r"'([^']*)'", v)
    if lit:
        if _STATUS_WORD.match(lit.group(1)):
            found.add(lit.group(1))
        return
    if v == "%s":
        idx = "".join(vals[:cols.index("status")]).count("%s")
        if idx < len(params) and isinstance(params[idx], str) and _STATUS_WORD.match(params[idx]):
            found.add(params[idx])
            return
    unresolved.append("%s · INSERT status 取不到字面量(值=%r)" % (where, v))


def scan() -> Dict[str, object]:
    """返回 {statuses, unresolved, sites}。"""
    found: Set[str] = set()
    unresolved: List[str] = []
    sites = 0
    for f in production_files():
        src = io.open(ROOT / f, encoding="utf-8", errors="replace").read()
        if not _STMT.search(src):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:                       # pragma: no cover
            unresolved.append("%s · 语法解析失败" % f)
            continue
        for sql, call in _sql_strings(tree):
            if not _STMT.search(sql):
                continue
            sites += 1
            where = "%s:%s" % (f, getattr(call, "lineno", "?"))
            params = _param_literals(call)
            if re.search(r"(?<![_\w.])INSERT\s+INTO\s+topics\b", sql, re.IGNORECASE):
                _from_insert(sql, params, found, unresolved, where)
            else:
                _from_update(sql, params, found, unresolved, where)
    return {"statuses": found, "unresolved": unresolved, "sites": sites}
