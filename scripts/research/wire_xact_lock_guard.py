#!/usr/bin/env python3
"""§1 接线器 · 把 require_xact_scope() 插到普查判为「静态追不出连接来源」的每一处

不手改 —— 手改 28 处必漏。跑普查拿 UNRESOLVED/UNKNOWN 集合,按 AST 行号插一行 + 补 import。
🔴 Windows 换行陷阱:读写都带 newline=""(默认 newline=None 会把整文件 LF 翻成 CRLF,
   diff 变成"全文件重写")。

用法:
  python scripts/research/wire_xact_lock_guard.py --dry-run   # 只看要改哪些行
  python scripts/research/wire_xact_lock_guard.py             # 真改
  python scripts/research/wire_xact_lock_guard.py --verify    # 校验:每一处都已接线(RC=1 则漏)
"""
from __future__ import annotations

import argparse
import ast
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from advisory_lock_autocommit_census import census  # noqa: E402

GUARD_IMPORT = "from db.xact_lock_guard import require_xact_scope"
NEEDS_GUARD = ("UNRESOLVED", "UNKNOWN")


def _read(path):
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _eol(text):
    return "\r\n" if "\r\n" in text else "\n"


def targets(root):
    rows, _ = census(root)
    return [r for r in rows if r["kind"] in NEEDS_GUARD]


def wire(root, dry_run=False):
    rows = targets(root)
    by_file = {}
    for r in rows:
        by_file.setdefault(r["file"], []).append(r)

    changed = 0
    for rel, hits in sorted(by_file.items()):
        path = os.path.join(root, rel)
        text = _read(path)
        eol = _eol(text)
        lines = text.split(eol)

        # 倒序插入,保证前面的行号不被后插入的行顶偏
        for r in sorted(hits, key=lambda x: -x["line"]):
            idx = r["line"] - 1
            target_line = lines[idx]
            indent = target_line[: len(target_line) - len(target_line.lstrip())]
            recv = r["recv"] or "cur"
            where = "%s.%s" % (os.path.basename(rel)[:-3], r["fn"])
            guard = '%srequire_xact_scope(%s, where="%s")  # §1 硬闸:autocommit 下取事务锁=没锁' % (
                indent, recv, where,
            )
            if idx > 0 and "require_xact_scope(" in lines[idx - 1]:
                continue  # 幂等:已接过
            print("  %s:%d  ← %s" % (rel, r["line"], guard.strip()[:72]))
            if not dry_run:
                lines.insert(idx, guard)
                changed += 1

        if dry_run:
            continue

        new_text = eol.join(lines)
        if GUARD_IMPORT not in new_text:
            new_text = _insert_import(new_text, eol)
        if new_text != text:
            _write(path, new_text)
    return changed


def _insert_import(text, eol):
    """插到**最后一个顶层 import 语句之后**。

    🔴 这里踩过一次:按"最后一行以 import/from 开头"找位置,
    碰上多行括号 import(`from .contract import (` 换行续写)就会把新 import 插进括号里,
    整个文件 SyntaxError —— 而普查脚本原本 `except SyntaxError: continue` 会**静默跳过整文件**,
    分母从 87 掉到 84 都不报错。所以改用 AST 的 end_lineno,并且普查改成对语法错**响亮失败**。
    """
    tree = ast.parse(text)
    last_end = None
    for node in tree.body:                       # 只看顶层,不进函数/try
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                last_end = max(last_end or 0, node.end_lineno)
                continue
            last_end = max(last_end or 0, node.end_lineno)
    lines = text.split(eol)
    if last_end is None:
        # 无顶层 import:插在模块 docstring 之后
        last_end = 0
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(
            getattr(tree.body[0], "value", None), ast.Constant
        ):
            last_end = tree.body[0].end_lineno
    lines.insert(last_end, GUARD_IMPORT)
    out = eol.join(lines)
    ast.parse(out)                               # 自证:插完仍可解析,否则当场抛
    return out


def verify(root):
    rows = targets(root)
    missing = []
    for r in rows:
        path = os.path.join(root, r["file"])
        lines = _read(path).replace("\r\n", "\n").split("\n")
        idx = r["line"] - 1
        window = lines[max(0, idx - 2): idx]
        if not any("require_xact_scope(" in w for w in window):
            missing.append(r)
    print("需接线处 = %d   已接线 = %d   漏 = %d" % (len(rows), len(rows) - len(missing), len(missing)))
    for r in missing:
        print("  MISSING %s:%d %s" % (r["file"], r["line"], r["fn"]))
    return 1 if missing else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.verify:
        return verify(root)
    print("=== 需要硬闸的 callsite(普查判为 UNRESOLVED / UNKNOWN)===")
    n = wire(root, dry_run=a.dry_run)
    print("插入 %d 行" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
