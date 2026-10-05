# -*- coding: utf-8 -*-
"""WO_285b 判据用的静态调用图(读工作树文件,不 import 被测代码)。

节点 = (模块, 顶层函数名)。边 = 函数体里对可解析目标的调用,或把函数当实参传(asyncio.to_thread(f, ...))。
  · f(...)     → 同模块顶层 f;或 `from M import f [as g]`(函数内的局部 import 也算,本仓惯例)
  · a.f(...)   → a 是模块别名(`import M as a` / `from P import M as a`)
根 = 路由处理函数:装饰器是 X.get/post/put/patch/delete/websocket/api_route(...)。
guard = 函数体(跳过 docstring)以 `global F` 开头、紧跟一个测试里含 F 的 `if` 且其中有 return
       ⇒ 每进程只跑一次;遍历到它就不再往下展开。
不解析:getattr / 字符串派发 / 类方法 ⇒ 结果是**下界**。
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict, deque
from pathlib import Path

ALTER = re.compile(r"\bALTER\s+TABLE\b", re.I)
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def has_alter(sql: str) -> bool:
    """去掉 SQL 注释后仍含 ALTER TABLE(注释里复述的 ALTER 不算:_BRANDS_CREATE_TABLE_SQL 的注释就提到过)。"""
    return bool(ALTER.search(_SQL_COMMENT.sub("", sql)))


ROUTE_ATTRS = {"get", "post", "put", "patch", "delete", "websocket", "api_route"}
SKIP_DIRS = ("tests/", "scripts/", "frontend/", "docs/", "node_modules/", ".venv/", "venv/")


def production_files(root: Path) -> dict:
    mods = {}
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(SKIP_DIRS) or "/tests/" in rel or "/node_modules/" in rel or rel.startswith("."):
            continue
        mods[rel[:-3].replace("/", ".").removesuffix(".__init__")] = p
    return mods


def _resolve_from(mod: str, level: int, module: str | None) -> str:
    if level == 0:
        return module or ""
    parts = mod.split(".")
    base = parts[:-level] if level <= len(parts) else []
    return ".".join(base + ([module] if module else []))


def _is_guarded(fn) -> bool:
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(getattr(fn.body[0], "value", None), ast.Constant)) else fn.body
    if len(body) < 2 or not isinstance(body[0], ast.Global) or not isinstance(body[1], ast.If):
        return False
    names = {n.id for n in ast.walk(body[1].test) if isinstance(n, ast.Name)}
    return bool(names & set(body[0].names)) and any(isinstance(x, ast.Return) for x in body[1].body)


_CATALOG_CALL = re.compile(r"exist|constraint_def|column_type|regclass|fetchone|fetchall|missing", re.I)
TRUSTED_MODULES = {"db.schema_guard"}   # 它自己的条件由真 PG 格(持锁时立即返回)验证


def _is_catalog_test(test) -> bool:
    """`if` 的条件是不是在「问目录」:调了 *exist* / constraint_def / fetchone 之类,或拿值去比对一个集合(`"c" not in cols`)。
    🔴 只认这两种:第一版把任何 `if` 都当守卫,于是 `if sql.startswith("CREATE UNIQUE…"):` 这种**按位置**的 if
       也放行了 —— 注毒(去掉 brands 的约束查询)全绿,就是这么漏的。"""
    for n in ast.walk(test):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            if _CATALOG_CALL.search(name or ""):
                return True
        if isinstance(n, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops) \
                and any(isinstance(c, (ast.Name, ast.Call, ast.Attribute)) for c in n.comparators):
            return True
    return False


def _conditional(sql: str, node, parents, fn, mod: str = "") -> bool:
    """这条 ALTER 是否「只在对象缺失 / 不一致时才执行」。"""
    if mod in TRUSTED_MODULES:
        return True
    text = " ".join(sql.split()).upper()
    if text.startswith("DO ") and " IF " in f" {text} ":
        return True                      # DO $$ … IF NOT EXISTS (SELECT … pg_constraint …) THEN ALTER … $$
    cur, top = node, None
    while cur in parents and cur is not fn:
        if parents[cur] is fn:
            top = cur                    # 字面量所在的函数顶层语句
        cur = parents[cur]
        if isinstance(cur, ast.If) and _is_catalog_test(cur.test):
            return True                  # Python 侧先查目录再 ALTER(嵌在问目录的 if 里)
    # 提前返回式:同一函数里、这条语句之前,有「if 问目录: … return」(如 if column_exists(...): return)
    if top is not None and top in fn.body:
        for stmt in fn.body[:fn.body.index(top)]:
            if isinstance(stmt, ast.If) and _is_catalog_test(stmt.test) \
                    and any(isinstance(x, ast.Return) for x in stmt.body):
                return True
    return False


def build(root: Path):
    mods = production_files(root)
    defs, imports, roots, trees = {}, {}, set(), {}
    for mod, path in mods.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        trees[mod] = tree
        imp = {}
        # [ONESHOT 提速 4] 原先两遍全树 walk(先收 import、再找路由装饰器)合成一遍;
        #   模块级函数的 `defs[...] = n` 仍在装饰器 setdefault 之后执行 ⇒ defs 最终取值与原先逐项相同
        #   (等价由 .deploy_runtime/speed4_ddl_equiv.py 在多棵真树上比过)。
        decorated = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    imp[a.asname or a.name.split(".")[0]] = ("MOD", a.name if a.asname else a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                base = _resolve_from(mod, n.level, n.module)
                for a in n.names:
                    full = f"{base}.{a.name}" if base else a.name
                    imp[a.asname or a.name] = ("MOD", full) if full in mods else (base, a.name)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    f = d.func if isinstance(d, ast.Call) else d
                    if isinstance(f, ast.Attribute) and f.attr in ROUTE_ATTRS:
                        decorated.append(n)
                        break
        imports[mod] = imp
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs[(mod, n.name)] = n
        for n in decorated:
            roots.add((mod, n.name))
            defs.setdefault((mod, n.name), n)

    def target(mod, node):
        imp = imports.get(mod, {})
        if isinstance(node, ast.Name):
            if (mod, node.id) in defs:
                return (mod, node.id)
            t = imp.get(node.id)
            if t and t[0] != "MOD":
                return t
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            t = imp.get(node.value.id)
            if t and t[0] == "MOD":
                return (t[1], node.attr)
        return None

    edges = defaultdict(set)
    for (mod, name), fn in defs.items():
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                for cand in [n.func] + list(n.args):
                    t = target(mod, cand)
                    if t and t in defs and t != (mod, name):
                        edges[(mod, name)].add(t)
    guarded = {k for k, fn in defs.items() if _is_guarded(fn)}

    seen, via, q = set(roots), {}, deque(roots)
    while q:
        u = q.popleft()
        if u in guarded:
            continue
        for v in edges[u]:
            if v not in seen:
                seen.add(v)
                via[v] = u
                q.append(v)

    # 模块级常量里的 ALTER(`X = "ALTER …"` 或元组 / 列表里有一条):函数体引用 X 就等于在那里执行它。
    # 🔴 第一版漏了这种写法:db/brands_schema._BRANDS_INDEX_STATEMENTS 里的 DROP CONSTRAINT 被
    #    ensure_brands_schema 在循环里执行,字面量却长在模块级 ⇒ 图看不见;真 PG 计数(init_db 每调一次 1 条 ALTER)才揪出来。
    module_consts = {}
    for mod, tree in trees.items():
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                sqls = [c.value for c in ast.walk(n.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                        and has_alter(c.value)]
                if sqls:
                    module_consts[(mod, n.targets[0].id)] = (n.lineno, sqls)

    # 每个「路由经无 guard 路径可达、自身也无 guard」的函数里的 ALTER 字面量(含引用到的模块级常量)
    hot = []
    for (mod, name) in seen:
        if (mod, name) in guarded or (mod, name) not in defs:
            continue
        fn = defs[(mod, name)]
        # [ONESHOT 提速 4] 先一遍 walk 挑候选(ALTER 字面量 / 引用了模块级 ALTER 常量的名字);没有候选就跳过
        #   父节点表和第二遍 walk —— 绝大多数可达函数两样都没有。候选保持原 walk 顺序,下面逐个套原判据,产出逐项不变。
        cands = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and has_alter(n.value):
                cands.append(n)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                t = imports.get(mod, {}).get(n.id)
                if (mod, n.id) in module_consts or (t and t in module_consts):
                    cands.append(n)
        if not cands:
            continue
        parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
        chain, cur = [(mod, name)], (mod, name)
        while cur in via:
            cur = via[cur]
            chain.append(cur)
        route = f"{chain[-1][0]}.{chain[-1][1]}"
        for n in cands:
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and has_alter(n.value) \
                    and not isinstance(parents.get(n), ast.Expr):
                hot.append({"key": f"{mod}.{name}", "line": n.lineno,
                            "sql": " ".join(n.value.split())[:90],
                            "conditional": _conditional(n.value, n, parents, fn, mod), "route": route})
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                ref = (mod, n.id) if (mod, n.id) in module_consts else None
                if ref is None:
                    t = imports.get(mod, {}).get(n.id)
                    ref = t if t and t in module_consts else None
                if ref is not None:
                    line, sqls = module_consts[ref]
                    for sql in sqls:
                        hot.append({"key": f"{mod}.{name}", "line": n.lineno,
                                    "sql": f"[常量 {ref[0]}.{ref[1]}:{line}] " + " ".join(sql.split())[:70],
                                    "conditional": _conditional(sql, n, parents, fn, mod), "route": route})
    return {"routes": len(roots), "edges": sum(len(v) for v in edges.values()), "hot": hot,
            "reached": seen, "guarded": guarded, "defs": defs}
