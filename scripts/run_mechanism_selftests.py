#!/usr/bin/env python
"""跑**全部**机制合同判据,并把真退出码原样交出去。

═══ 为什么要这支脚本(两个我自己犯过的错,各修一个)═══

**① 管道换掉退出码。** 2026-08-28 我跑的是:

    pytest <7 个文件> -q | tail -2 && git commit ...

管道的退出码是 `tail` 的 **0** ⇒ `&&` 不短路 ⇒ pytest 的失败被吃掉、commit 照跑,
而我照着 tail 出来的最后两行把「72 passed / 1 failed」写成了「73 passed」。
(这条本来就在我 memory 里:「退出码 0 三种假绿 · 管道换掉码」。)

**② 手写分母。** 那条命令里的 7 个文件名是**我手打的**。
再加第八个合同文件而忘了写进命令,它会**静默不跑** —— 而总数照样好看。
「手写分母漏掉的那一项不会让任何判据变红」,这是本仓记过的老病。

所以:文件集**机械枚举**(glob),退出码**原样返回**,并打印树身份
(核验命令必须自带树身份 —— 另一条我踩过的坑)。

用法::

    python scripts/run_mechanism_selftests.py          # 逐文件 + 合跑
    python scripts/run_mechanism_selftests.py --quiet  # 只要合计与退出码
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

#: 机械枚举的口径:`scripts/` 下所有机制合同判据。
#: 🔴 **不许改成手写清单** —— 手写清单漏掉的那一项不会让任何判据变红。
GLOBS = ("test_mutation_*.py",)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def discover() -> list[Path]:
    found: set[Path] = set()
    for g in GLOBS:
        found.update(SCRIPTS.glob(g))
    return sorted(found)


def _tree_id() -> str:
    def _git(*a: str) -> str:
        r = subprocess.run(["git", *a], cwd=str(ROOT), capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else "?"

    return f"TREE={_git('rev-parse', '--show-toplevel')} HEAD={_git('rev-parse', '--short', 'HEAD')}"


def _run(targets: list[str]) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    lines = [x for x in (r.stdout + r.stderr).splitlines() if x.strip()]
    return r.returncode, (lines[-1] if lines else "(无输出)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    files = discover()
    if not files:
        print("🔴 一个机制合同判据都没扫到 —— 分母为 0 不是通过,是探针写废了")
        return 2

    print(_tree_id())
    print(f"机械枚举到 {len(files)} 个合同文件(glob,不是手写清单)")
    if not args.quiet:
        for f in files:
            rc, last = _run([f"scripts/{f.name}"])
            flag = "✅" if rc == 0 else "🔴"
            print(f"  {flag} {f.name:<52} {last}")

    rc, last = _run([f"scripts/{f.name}" for f in files])
    print(f"合跑:{last}")
    # 🔴 原样返回 pytest 的退出码 —— 调用方拿到的必须是它,而不是某个管道尾巴的。
    print(f"SELFTEST_RC={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
