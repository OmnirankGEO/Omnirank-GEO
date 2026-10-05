# -*- coding: utf-8 -*-
"""#41 口径改版的三格破坏性臂 —— 手动跑,不进常驻判据。

为什么不进常驻判据:它要往 `tests/` 里种目录。常驻判据每跑一次就动一次树,
两个窗口并发跑就互相污染 —— 2026-09-04 上午 `test_ea_12` 两次瞬态红,
真因正是另一个窗口在这棵**正在跑的树**里种了目录。
把破坏性动作放进常驻判据,等于把那次事故做成每日重演。

三格(状态矩阵里要种东西的那三格):
  A 未跟踪 + 有内容(毒/残留)  ⇒ 计数 +1,且 `test_ea_12` **红**且报文点名
  B 只剩 `__pycache__`         ⇒ 计数**不变**
  C 空目录                     ⇒ 计数**不变**

🔴 **A 是这次改口径的命门。** 原提案「只数含 tracked 文件的目录」在 A 格是「不计」,
   那等于把 2026-09-04 上午抓住 `rv_poison_pkg_2026_09_04` 的那口牙拔掉,
   而且拔掉之后**没有任何判据会红** —— 没人会发现。
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import pathlib
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

# 🔴 [2026-09-04] 环境闸:**「跑不起来」不许被报成「锁没牙」。**
#    重跑这批时我忘了 export TEST_DATABASE_URL,判据在 conftest 就 RuntimeError,
#    而本脚本的汇总行照样打印「有锁没牙」—— 一个具体但**错误**的诊断,
#    会让下一个人去改一把本来好的锁。宁可当场停机,也不要给出错的归因。
import os as _os
if not _os.environ.get("TEST_DATABASE_URL"):
    raise SystemExit(
        "🔴 缺 TEST_DATABASE_URL —— 判据会在 conftest 就炸,而那**不是**锁没牙。\n"
        "   先 export TEST_DATABASE_URL=postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test")

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_gate9():
    """每次重新加载 —— 模块级缓存会让第二次读到第一次的数。"""
    sp = importlib.util.spec_from_file_location(
        "_g9_probe", ROOT / "scripts" / "gate9_full_denominator_baseline.py")
    m = importlib.util.module_from_spec(sp)
    with contextlib.redirect_stdout(io.StringIO()):
        sp.loader.exec_module(m)
    return m


def count() -> int:
    return len(_load_gate9().disk_package_names())


def run_ea12() -> tuple[int, str]:
    env = {**dict(__import__("os").environ),
           "TEST_DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
           "DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
           "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "--tb=long",
         "scripts/test_mutation_gate9_extra_args_contract.py::"
         "test_ea_12_disk_census_matches_and_the_delta_is_exactly_this_package"],
        cwd=ROOT, capture_output=True, env=env)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


BASE = count()
print(f"  基线计数 = {BASE}\n")
res = []

# ── A 未跟踪 + 有内容 ⇒ 必须计入,且 ea_12 红且点名 ──────────────────
a = ROOT / "tests" / "wob41_A_untracked_content_2026_09_04"
try:
    a.mkdir(parents=True, exist_ok=True)
    (a / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    assert (a / "__init__.py").is_file(), "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    n = count()
    rc, out = run_ea12()
    named = a.name in out
    ok = (n == BASE + 1) and rc != 0 and named
    print(f"  {'OK ' if ok else 'XX '}A 未跟踪+有内容(毒/残留)")
    print(f"       计数 {BASE} -> {n}（须 {BASE + 1}）· ea_12 rc={rc}（须非 0)· 报文点名={named}")
    if not ok:
        for l in out.splitlines():
            if "assert" in l or "无 git 跟踪" in l:
                print(f"       {l.strip()[:150]}")
    res.append(ok)
finally:
    shutil.rmtree(a, ignore_errors=True)
    assert not a.exists(), "A 未清理!"

# ── B 只剩 __pycache__ ⇒ 计数不变 ───────────────────────────────────
b = ROOT / "tests" / "wob41_B_pycache_only_2026_09_04"
try:
    (b / "__pycache__").mkdir(parents=True, exist_ok=True)
    (b / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    assert (b / "__pycache__" / "x.pyc").is_file(), "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    n = count()
    ok = n == BASE
    print(f"  {'OK ' if ok else 'XX '}B 只剩 __pycache__        计数 {BASE} -> {n}（须不变)")
    res.append(ok)
finally:
    shutil.rmtree(b, ignore_errors=True)
    assert not b.exists(), "B 未清理!"

# ── C 空目录 ⇒ 计数不变 ─────────────────────────────────────────────
c = ROOT / "tests" / "wob41_C_empty_2026_09_04"
try:
    c.mkdir(parents=True, exist_ok=True)
    assert c.is_dir(), "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    n = count()
    ok = n == BASE
    print(f"  {'OK ' if ok else 'XX '}C 空目录                  计数 {BASE} -> {n}（须不变)")
    res.append(ok)
finally:
    shutil.rmtree(c, ignore_errors=True)
    assert not c.exists(), "C 未清理!"

# ── 收尾:三格清完之后必须回到基线,且 ea_12 回绿 ──────────────────
n = count()
rc, _ = run_ea12()
print(f"\n  清理后计数 = {n}（须 {BASE}）· ea_12 rc={rc}（须 0)")
res.append(n == BASE and rc == 0)

left = [p.name for p in (ROOT / "tests").iterdir() if p.name.startswith("wob41_")]
print(f"  残留目录:{left or '（无)'}")
res.append(not left)

print(f"\n  {'OK  三格全部符合口径,且清理后回到基线' if all(res) else 'XX  有一格不合格'}")
sys.exit(0 if all(res) else 1)
