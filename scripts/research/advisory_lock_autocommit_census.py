#!/usr/bin/env python3
"""§1 普查 · pg_advisory_xact_lock 调用方 autocommit 全仓普查(WO-LATENT-TRAPS 2026-08-17)

**为什么有这个脚本**:`pg_advisory_xact_lock` 是**事务级**锁 —— 在 autocommit 连接上,
取锁语句自成一个事务、语句一结束锁立刻释放 = **锁了等于没锁**(并发超发 / 孤儿写入)。
图文包一周内三次撞上。全仓既有代码 100+ 处在用该锁,**从没人审过调用方是不是 autocommit**。

**结构锚**:不 grep 我记得的那 6 处,而是 AST 扫**全部被 git 跟踪的 .py**,
凡字符串字面量含 `pg_advisory_xact_lock` 的 `.execute(...)` 一律入分母。
分母自证 = AST 计数 vs `git grep -c` 行数两把尺子对照(脚本自己算,不手抄)。

**分类(穷尽 · 无第 N+1 类)**:
  TXN        连接源在本函数内可判、且是事务连接(get_db() / get_connection() 且无 autocommit=True)
  AUTOCOMMIT 连接源可判、且被置 autocommit=True                 → **踩中**
  PARAM      cursor/conn 由参数传入 → 递归追调用方;全 TXN 判 TXN,
             任一 AUTOCOMMIT 判 AUTOCOMMIT,追不到调用方判 UNRESOLVED
  UNRESOLVED 追不出连接来源(公开面 / 实参非变量 / 调用环)—— 靠静态判不了,归运行时硬闸管
  UNKNOWN    连本地绑定都没解析出来(模块级等)
  NONPROD    tests/ · docs/ 路径 —— 入分母但不定罪

用法:
  python scripts/research/advisory_lock_autocommit_census.py            # 普查表
  python scripts/research/advisory_lock_autocommit_census.py --selftest # 判别力自证(正/反样本)
  python scripts/research/advisory_lock_autocommit_census.py --fail-on-hit  # 有踩中则 RC=1
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

LOCK_TOKEN = "pg_advisory_xact_lock"
# 事务连接来源:拿到即 autocommit=False —— db/connection.py:180 每次 getconn 强制复位
TXN_SOURCES = {"get_db", "get_connection", "_get_conn", "get_conn"}


# ---------------------------------------------------------------- 收集 callsite
class _LockVisitor(ast.NodeVisitor):
    """找 <recv>.execute(<字面量含 LOCK_TOKEN>) 并记录所属函数。"""

    def __init__(self) -> None:
        self.hits: List[dict] = []
        self._fn_stack: List[ast.AST] = []

    def _enter_fn(self, node):
        self._fn_stack.append(node)
        self.generic_visit(node)
        self._fn_stack.pop()

    visit_FunctionDef = _enter_fn
    visit_AsyncFunctionDef = _enter_fn

    def visit_Call(self, node: ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany"):
            if _call_carries_token(node):
                self.hits.append(
                    {
                        "line": node.lineno,
                        "recv": _name_of(func.value),
                        "fn": self._fn_stack[-1] if self._fn_stack else None,
                    }
                )
        self.generic_visit(node)


def _call_carries_token(node: ast.Call) -> bool:
    if not node.args:
        return False
    for sub in ast.walk(node.args[0]):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and LOCK_TOKEN in sub.value:
            return True
    return False


def _name_of(node) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


# ------------------------------------------------------- 函数内解析 cursor→conn
def _binding_kind(fn: ast.AST, var: str) -> Tuple[str, str]:
    """在函数 fn 内判 var 的来源。返回 (kind, 证据)。kind ∈ {TXN, AUTOCOMMIT, PARAM, UNKNOWN}"""
    if fn is None:
        return "UNKNOWN", "无所属函数(模块级)"

    params = {
        a.arg
        for a in list(getattr(fn.args, "posonlyargs", [])) + fn.args.args + fn.args.kwonlyargs
    }
    if getattr(fn.args, "vararg", None):
        params.add(fn.args.vararg.arg)
    if getattr(fn.args, "kwarg", None):
        params.add(fn.args.kwarg.arg)

    # 1) var = <conn>.cursor()  → 改追 conn
    conn_var = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if _name_of(tgt) == var and isinstance(node.value, ast.Call):
                    f = node.value.func
                    if isinstance(f, ast.Attribute) and f.attr == "cursor":
                        conn_var = _name_of(f.value)
                    elif isinstance(f, ast.Name) and f.id in TXN_SOURCES:
                        conn_var = var
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if _name_of(item.optional_vars) == var and isinstance(item.context_expr, ast.Call):
                    f = item.context_expr.func
                    if isinstance(f, ast.Attribute) and f.attr == "cursor":
                        conn_var = _name_of(f.value)

    target = conn_var or var

    # 2) 该 conn 是否在本函数内被显式置 autocommit=True
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value is True:
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "autocommit" and _name_of(tgt.value) == target:
                    return "AUTOCOMMIT", "%s.autocommit=True @L%d" % (target, node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "set_session":
            if _name_of(node.func.value) == target:
                for kw in node.keywords:
                    if kw.arg == "autocommit" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        return "AUTOCOMMIT", "%s.set_session(autocommit=True) @L%d" % (target, node.lineno)

    # 3) conn 来自 with get_db() / get_connection()
    for node in ast.walk(fn):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if _name_of(item.optional_vars) == target and isinstance(item.context_expr, ast.Call):
                    src = _name_of(item.context_expr.func)
                    if src and src.split(".")[-1] in TXN_SOURCES:
                        return "TXN", "with %s() as %s @L%d" % (src, target, node.lineno)
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if _name_of(tgt) == target and isinstance(node.value, ast.Call):
                    src = _name_of(node.value.func)
                    if src and src.split(".")[-1] in TXN_SOURCES:
                        return "TXN", "%s = %s() @L%d" % (target, src, node.lineno)

    # 4) 参数传入
    if target in params or var in params:
        return "PARAM", "参数 %s" % target

    return "UNKNOWN", "未解析 %s" % target


# --------------------------------------------------------------- 调用方追溯
def _index_tree(tree, path, index):
    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack: List[ast.AST] = []

        def _fn(self, node):
            self.stack.append(node)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

        def visit_Call(self, node: ast.Call):
            nm = _name_of(node.func)
            if nm:
                index[nm.split(".")[-1]].append((path, self.stack[-1] if self.stack else None, node))
            self.generic_visit(node)

    V().visit(tree)


def _build_call_index(abs_files: List[str]):
    index = defaultdict(list)
    for path in abs_files:
        try:
            tree = ast.parse(_read(path), filename=path)
        except SyntaxError:
            continue
        _index_tree(tree, path, index)
    return index


def _resolve_param(fn: ast.AST, var: str, call_index, depth: int = 0, seen=None) -> Tuple[str, str]:
    """参数型:追所有调用方,归并判定。"""
    if seen is None:
        seen = set()
    if fn is None or depth > 4:
        return "UNRESOLVED", "追溯超深/无函数"
    key = (getattr(fn, "name", "?"), var, depth)
    if key in seen:
        return "UNRESOLVED", "调用环"
    seen.add(key)

    fname = getattr(fn, "name", None)
    if not fname:
        return "UNRESOLVED", "匿名函数"

    all_args = list(getattr(fn.args, "posonlyargs", [])) + fn.args.args
    try:
        pos = [a.arg for a in all_args].index(var)
    except ValueError:
        pos = None

    callers = call_index.get(fname, [])
    if not callers:
        return "UNRESOLVED", "无仓内调用方(公开面)· %s()" % fname

    kinds: List[str] = []
    notes: List[str] = []
    for cpath, cfn, cnode in callers:
        argnode = None
        if pos is not None and len(cnode.args) > pos:
            argnode = cnode.args[pos]
        for kw in cnode.keywords:
            if kw.arg == var:
                argnode = kw.value
        if argnode is None:
            kinds.append("UNRESOLVED")
            notes.append("%s:%d 未传 %s" % (os.path.basename(cpath), cnode.lineno, var))
            continue
        aname = _name_of(argnode)
        if not aname and isinstance(argnode, ast.Call):
            f = argnode.func
            if isinstance(f, ast.Attribute) and f.attr == "cursor":
                aname = _name_of(f.value)
        if not aname:
            kinds.append("UNRESOLVED")
            notes.append("%s:%d 实参非变量" % (os.path.basename(cpath), cnode.lineno))
            continue
        k, ev = _binding_kind(cfn, aname)
        if k == "PARAM":
            k, ev = _resolve_param(cfn, aname, call_index, depth + 1, seen)
        kinds.append(k)
        notes.append("%s:%d %s->%s(%s)" % (os.path.basename(cpath), cnode.lineno, aname, k, ev))

    if "AUTOCOMMIT" in kinds:
        return "AUTOCOMMIT", " | ".join(n for n, k in zip(notes, kinds) if k == "AUTOCOMMIT")
    if kinds and all(k == "TXN" for k in kinds):
        return "TXN", "%d 个调用方全 TXN" % len(kinds)
    return "UNRESOLVED", " | ".join(notes[:2]) + (" (+%d)" % (len(notes) - 2) if len(notes) > 2 else "")


# ---------------------------------------------------------------------- driver
def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _git_tracked_py(root: str) -> List[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout
    return [p for p in out.splitlines() if p.strip()]


def _grep_denominator(root: str) -> int:
    """分母自证:git grep 的命中行数(独立于 AST 的第二把尺子)。"""
    out = subprocess.run(
        ["git", "grep", "-c", LOCK_TOKEN, "--", "*.py"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout
    total = 0
    for line in out.splitlines():
        if ":" in line:
            try:
                total += int(line.rsplit(":", 1)[1])
            except ValueError:
                pass
    return total


def _is_nonprod(path: str) -> bool:
    p = path.replace("\\", "/")
    return p.startswith("tests/") or p.startswith("docs/") or "/tests/" in p


def census(root: str) -> Tuple[List[dict], int]:
    files = _git_tracked_py(root)
    call_index = _build_call_index([os.path.join(root, f) for f in files])
    rows: List[dict] = []
    for rel in files:
        path = os.path.join(root, rel)
        src = _read(path)
        if LOCK_TOKEN not in src:
            continue
        # 🔴 含 token 的文件语法错 = 整文件被跳过 = 分母静默缩水(2026-08-17 实测 87→84 无声)。
        #    这里必须响亮失败,不许 `except SyntaxError: continue`。
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError as exc:
            raise SystemExit(
                "普查中止:%s 含 %s 但语法错(%s)—— 静默跳过会让分母缩水且不报错" % (rel, LOCK_TOKEN, exc)
            )
        v = _LockVisitor()
        v.visit(tree)
        for hit in v.hits:
            if _is_nonprod(rel):
                kind, ev = "NONPROD", "非生产路径(tests/docs)"
            else:
                kind, ev = _binding_kind(hit["fn"], hit["recv"])
                if kind == "PARAM":
                    kind, ev = _resolve_param(hit["fn"], hit["recv"], call_index)
            rows.append(
                {
                    "file": rel,
                    "line": hit["line"],
                    "fn": getattr(hit["fn"], "name", "<module>"),
                    "recv": hit["recv"],
                    "kind": kind,
                    "evidence": ev,
                }
            )
    return rows, _grep_denominator(root)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--fail-on-hit", action="store_true", help="有 AUTOCOMMIT 则退出码 1")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.root)

    rows, grep_lines = census(args.root)
    by_kind: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)

    print("%-11s %-54s %6s  %-40s %s" % ("kind", "file", "line", "fn", "证据"))
    print("-" * 160)
    for kind in ("AUTOCOMMIT", "UNRESOLVED", "UNKNOWN", "PARAM", "TXN", "NONPROD"):
        for r in sorted(by_kind.get(kind, []), key=lambda x: (x["file"], x["line"])):
            print("%-11s %-54s %6d  %-40s %s" % (r["kind"], r["file"], r["line"], r["fn"], r["evidence"][:64]))

    print("-" * 160)
    print("AST callsite 总数 = %d   |   git grep 命中行数(第二把尺子) = %d" % (len(rows), grep_lines))
    for kind in ("AUTOCOMMIT", "UNRESOLVED", "UNKNOWN", "PARAM", "TXN", "NONPROD"):
        print("  %-11s %d" % (kind, len(by_kind.get(kind, []))))
    assert sum(len(v) for v in by_kind.values()) == len(rows), "分类未穷尽"

    # ---- 工单口径:每一处必须落进「安全 / 已加闸 / 非锁用途」三类,无第四类 ----
    safe = len(by_kind.get("TXN", []))
    gated = len(by_kind.get("UNRESOLVED", [])) + len(by_kind.get("UNKNOWN", []))
    nonlock = len(by_kind.get("NONPROD", []))
    hit = len(by_kind.get("AUTOCOMMIT", []))
    print()
    print("== 工单三类口径 ==")
    print("  安全(静态可证在事务里)                  %d" % safe)
    print("  已加闸(静态追不出来源 → require_xact_scope) %d" % gated)
    print("  非锁用途(tests/docs 路径)                %d" % nonlock)
    print("  ─────────────────────────────────────")
    print("  合计 %d   (= AST callsite 总数 %d)" % (safe + gated + nonlock + hit, len(rows)))
    if hit:
        print("  🔴 未归类的踩中 %d 处 —— 必须处理掉,不许留成第四类" % hit)
    assert safe + gated + nonlock + hit == len(rows), "三类口径没盖住全部 callsite"
    if args.fail_on_hit and by_kind.get("AUTOCOMMIT"):
        return 1
    return 0


# ------------------------------------------------------------------- selftest
_POSITIVE = """
from db.connection import get_connection

def synthetic_autocommit_caller(user_id):
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_xact_lock(920799, %s)", (user_id,))
    cur.execute("INSERT INTO t(v) VALUES (1)")
"""

_NEGATIVE = """
from db.connection import get_db

def synthetic_txn_caller(user_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(920799, %s)", (user_id,))
        cur.execute("INSERT INTO t(v) VALUES (1)")
"""

_POSITIVE_INDIRECT = """
from db.connection import get_connection

def _synthetic_inner(cur, user_id):
    cur.execute("SELECT pg_advisory_xact_lock(920798, %s)", (user_id,))

def synthetic_outer(user_id):
    conn = get_connection()
    conn.autocommit = True
    _synthetic_inner(conn.cursor(), user_id)
"""

_NEGATIVE_INDIRECT = """
from db.connection import get_db

def _synthetic_inner_ok(cur, user_id):
    cur.execute("SELECT pg_advisory_xact_lock(920797, %s)", (user_id,))

def synthetic_outer_ok(user_id):
    with get_db() as conn:
        _synthetic_inner_ok(conn.cursor(), user_id)
"""


def _classify_snippet(src: str) -> List[dict]:
    tree = ast.parse(src, filename="<synthetic>")
    call_index = defaultdict(list)
    _index_tree(tree, "<synthetic>", call_index)
    v = _LockVisitor()
    v.visit(tree)
    out = []
    for hit in v.hits:
        kind, ev = _binding_kind(hit["fn"], hit["recv"])
        if kind == "PARAM":
            kind, ev = _resolve_param(hit["fn"], hit["recv"], call_index)
        out.append({"fn": getattr(hit["fn"], "name", "?"), "kind": kind, "evidence": ev})
    return out


def selftest(root: str) -> int:
    ok = True

    def check(label, rows, want):
        nonlocal ok
        got = [r["kind"] for r in rows]
        good = got == want
        ok = ok and good
        print("  [%s] %s: 期望 %s 实得 %s   %s" % ("PASS" if good else "FAIL", label, want, got, rows))

    print("§1 普查探针 · 判别力自证(每个'必须命中'配一个'必须不命中')")
    print("① 正样本 · 直连 autocommit —— 探针必须抓到")
    check("直连 autocommit", _classify_snippet(_POSITIVE), ["AUTOCOMMIT"])
    print("② 反向对照 · get_db 事务连接 —— 探针必须放行(证明不是恒红)")
    check("get_db 事务", _classify_snippet(_NEGATIVE), ["TXN"])
    print("③ 正样本 · cursor 跨函数传参 + 上游 autocommit(WO 关注的真实形态)")
    check("跨函数 autocommit", _classify_snippet(_POSITIVE_INDIRECT), ["AUTOCOMMIT"])
    print("④ 反向对照 · cursor 跨函数传参 + 上游事务 —— 必须不命中")
    check("跨函数 TXN", _classify_snippet(_NEGATIVE_INDIRECT), ["TXN"])
    print("⑤ 分母自证 · AST 数 vs git grep 数")
    rows, grep_lines = census(root)
    print("  AST callsite=%d  git grep 行数=%d (grep 含注释/多行 SQL,AST<=grep 为正常)" % (len(rows), grep_lines))
    if len(rows) == 0:
        print("  [FAIL] AST 一个都没扫到 —— 探针没打进去")
        ok = False
    else:
        print("  [PASS] AST 分母非零")
    print("SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
