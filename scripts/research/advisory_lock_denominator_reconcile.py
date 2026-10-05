#!/usr/bin/env python3
"""§1 分母对账 · AST callsite 数 vs git grep 行数,逐行说清差在哪(WO-LATENT-TRAPS 2026-08-17)

**为什么必须有这个**:普查表说「AST 87 / grep 106」时,那 19 行的差**必须逐条落地**,
否则「AST <= grep 是正常的」就是一句自我安慰 —— 差的那些里若藏着一个真 callsite,
整张普查表的分母就是假的。本脚本把 grep 每一行归入且仅归入下列之一:

  CALLSITE   AST 已收(在普查表里)
  COMMENT    注释行(# 开头,或行内 # 之后才出现 token)
  DOCSTRING  文档字符串 / 模块注释里的说明文字
  SQL_TEXT   多行 SQL 字面量的续行(该 execute 的首行已被 AST 收过)
  NON_PY     非 .py(.sql / .md)—— 本对账只管 .py,列出即可
  ORPHAN     以上都不是 ← **一条都不许有**,有就是普查表漏了

用法: python scripts/research/advisory_lock_denominator_reconcile.py
退出码 0 = 无 ORPHAN;1 = 有 ORPHAN(普查表分母不可信)
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tokenize
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from advisory_lock_autocommit_census import (  # noqa: E402
    LOCK_TOKEN,
    _LockVisitor,
    _read,
)


def _grep_lines(root: str, pathspec: str):
    out = subprocess.run(
        ["git", "grep", "-n", LOCK_TOKEN, "--", pathspec],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            rows.append((parts[0], int(parts[1]), parts[2]))
    return rows


def _comment_and_string_lines(path: str):
    """用 tokenize 拿到真注释行号,用 AST 拿到字符串字面量覆盖的行号区间。"""
    src = _read(path)
    comment_lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT and LOCK_TOKEN in tok.string:
                comment_lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass

    str_spans = []       # (start, end, is_docstring)
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return comment_lines, str_spans, None

    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
               and isinstance(body[0].value.value, str):
                docstring_nodes.add(id(body[0].value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and LOCK_TOKEN in node.value:
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            str_spans.append((start, end, id(node) in docstring_nodes))
    return comment_lines, str_spans, tree


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

    # 1) AST 收到的 callsite:(file, execute 调用所覆盖的全部行)
    ast_covered = defaultdict(set)   # rel -> set(lines)
    ast_anchor = defaultdict(set)    # rel -> set(execute 首行)
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout.splitlines()
    for rel in tracked:
        path = os.path.join(root, rel)
        src = _read(path)
        if LOCK_TOKEN not in src:
            continue
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError:
            continue
        # AST callsite 锚点
        v = _LockVisitor()
        v.visit(tree)
        for hit in v.hits:
            ast_anchor[rel].add(hit["line"])
        # execute 调用整体覆盖的行(用于把多行 SQL 续行认领回去)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
               and node.func.attr in ("execute", "executemany") and node.args:
                for sub in ast.walk(node.args[0]):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and LOCK_TOKEN in sub.value:
                        for ln in range(node.lineno, (getattr(node, "end_lineno", node.lineno) or node.lineno) + 1):
                            ast_covered[rel].add(ln)

    verdicts = []
    counts = Counter()

    # 2) .py 逐行归类
    for rel, lineno, text in _grep_lines(root, "*.py"):
        path = os.path.join(root, rel)
        comments, spans, _tree = _comment_and_string_lines(path)
        if lineno in ast_anchor.get(rel, set()):
            v = "CALLSITE"
        elif lineno in comments:
            v = "COMMENT"
        elif lineno in ast_covered.get(rel, set()):
            v = "SQL_TEXT"
        elif any(s <= lineno <= e and doc for s, e, doc in spans):
            v = "DOCSTRING"
        elif any(s <= lineno <= e for s, e, doc in spans):
            v = "SQL_TEXT"
        else:
            v = "ORPHAN"
        counts[v] += 1
        verdicts.append((v, rel, lineno, text.strip()[:80]))

    # 3) 非 .py(.sql / .md)—— 列出,不参与 .py 分母
    nonpy = [
        (rel, ln, tx.strip()[:80])
        for rel, ln, tx in _grep_lines(root, ".")
        if not rel.endswith(".py")
    ]

    # 2b) 反方向:每个 AST callsite 必须至少被一条 grep 行覆盖(execute 跨行时 token 在续行)
    grep_by_file = defaultdict(set)
    for rel, ln, _tx in _grep_lines(root, "*.py"):
        grep_by_file[rel].add(ln)
    callsite_span = defaultdict(dict)   # rel -> anchor -> (start,end)
    for rel in tracked:
        path = os.path.join(root, rel)
        src = _read(path)
        if LOCK_TOKEN not in src:
            continue
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)                and node.func.attr in ("execute", "executemany") and node.args:
                for sub in ast.walk(node.args[0]):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and LOCK_TOKEN in sub.value:
                        callsite_span[rel][node.lineno] = (node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno)
    uncovered = []
    covered_multiline = 0
    for rel, spans in callsite_span.items():
        for anchor, (s, e) in spans.items():
            hits = [ln for ln in grep_by_file.get(rel, ()) if s <= ln <= e]
            if not hits:
                uncovered.append((rel, anchor))
            elif anchor not in hits:
                covered_multiline += 1

    print("=== §1 分母对账 · git grep(*.py) 逐行归类 ===")
    for v in ("ORPHAN", "CALLSITE", "SQL_TEXT", "COMMENT", "DOCSTRING"):
        for kind, rel, ln, tx in verdicts:
            if kind == v:
                print("  %-9s %s:%d  %s" % (kind, rel, ln, tx))
    print()
    print("--- .py 分母合计 ---")
    total = sum(counts.values())
    for v in ("CALLSITE", "SQL_TEXT", "COMMENT", "DOCSTRING", "ORPHAN"):
        print("  %-9s %d" % (v, counts[v]))
    print("  %-9s %d   (= git grep -n 行数)" % ("合计", total))
    print("  (注:普查表分母不是这里的 CALLSITE 数 —— 多行 SQL 的 token 落在续行,见下方反方向对账)")
    print()
    print("--- 非 .py 命中(不参与 .py 分母 · 只做全量可见)---")
    for rel, ln, tx in nonpy:
        print("  NON_PY    %s:%d  %s" % (rel, ln, tx))
    print("  非 .py 合计 %d" % len(nonpy))
    print()
    total_callsites = sum(len(v) for v in callsite_span.values())
    print("--- 反方向对账 · 每个 AST callsite 是否都被 grep 看见 ---")
    print("  AST callsite 总数(= 普查表分母)      %d" % total_callsites)
    print("    其中 token 就在 execute 首行         %d   ← 上表 CALLSITE 那一档" % counts["CALLSITE"])
    print("    其中 token 在多行 SQL 的续行上       %d   ← 上表被记成 SQL_TEXT" % covered_multiline)
    print("    grep 一行都没覆盖到的 callsite       %d   ← 必须为 0" % len(uncovered))
    for rel, anchor in uncovered:
        print("      UNCOVERED %s:%d" % (rel, anchor))
    print()
    if counts["ORPHAN"] or uncovered:
        print("对账结果: FAIL —— ORPHAN=%d / UNCOVERED=%d,普查表分母不可信"
              % (counts["ORPHAN"], len(uncovered)))
        return 1
    print("对账结果: PASS —— 正方向 %d 行 git grep 全部归类(ORPHAN=0);"
          "反方向 %d 个 AST callsite 全部被 grep 覆盖(UNCOVERED=0)。普查表分母 %d 可信"
          % (total, total_callsites, total_callsites))
    return 0


if __name__ == "__main__":
    sys.exit(main())
