# -*- coding: utf-8 -*-
"""#39 四道锁逐条注毒 —— 证明每道锁**在它自己那件事上**会红。

🔴 判据不是「退出码非 0」:一条锁可能红在**别的**锁上(我今天已经踩过两次
   —— 守卫崩溃与检出污染同码、哑 DSN 触发 conftest 安全闸)。
   所以每发毒都要求:**指定的那条锁 failed,且报文点名了毒**。

每发毒都在 `finally` 里逐字节还原(存原始 bytes,不是「再写一遍我以为的内容」)。
"""
from __future__ import annotations

import pathlib
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
LOCKS = "tests/wob39_collection_scope_2026_09_03/"


def run() -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", LOCKS, "--no-header", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def expect_red(label: str, must_fail: str, needle: str) -> bool:
    rc, out = run()
    failed_named = f"{must_fail}" in out and ("FAILED" in out or "failed" in out)
    hit = needle in out
    ok = rc != 0 and failed_named and hit
    print(f"  {'✅' if ok else '🔴'} {label}")
    print(f"       rc={rc} · 点名 {must_fail}={failed_named} · 报文含毒证据={hit}")
    if not ok:
        print("       " + " | ".join(l for l in out.splitlines() if "assert" in l or "Error" in l)[:300])
    return ok


results = []

# ── 毒 ① 把 scripts 从 testpaths 摘掉 ────────────────────────────
p = ROOT / "pytest.ini"
orig = p.read_bytes()
try:
    p.write_bytes(orig.replace(b"testpaths = tests scripts", b"testpaths = tests"))
    assert p.read_bytes() != orig, "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    results.append(expect_red("毒① testpaths 摘掉 scripts", "test_w39_01_", "不含 `scripts`"))
finally:
    p.write_bytes(orig)
    assert p.read_bytes() == orig, "pytest.ini 未还原!"

# ── 毒 ② 在 scripts/ 里种一个零判据的 test_ 文件 ─────────────────
planted = ROOT / "scripts" / "test_wob39_poison_zero.py"
try:
    planted.write_text("import os\nX = 1\n", encoding="utf-8")
    assert planted.is_file(), "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    results.append(expect_red("毒② scripts/ 种零判据文件", "test_w39_02_", "test_wob39_poison_zero.py"))
finally:
    planted.unlink(missing_ok=True)
    assert not planted.exists(), "毒② 文件未删除!"

# ── 毒 ③ 让 manual/ 里出现一个 test_ 名字 ────────────────────────
mp = ROOT / "scripts" / "manual" / "test_wob39_poison_manual.py"
try:
    mp.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    assert mp.is_file(), "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    results.append(expect_red("毒③ manual/ 出现 test_ 名字", "test_w39_04_", "test_wob39_poison_manual.py"))
finally:
    mp.unlink(missing_ok=True)
    assert not mp.exists(), "毒③ 文件未删除!"

# ── 毒 ④ 抽掉一个判据文件(模拟判据消失)────────────────────────
victim = ROOT / "scripts" / "test_mutation_evidence_verifier_contract.py"
vb = victim.read_bytes()
try:
    victim.unlink()
    assert not victim.exists(), "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    results.append(expect_red("毒④ 抽掉一个判据文件", "test_w39_03_", "< 基线"))
finally:
    victim.write_bytes(vb)
    assert victim.read_bytes() == vb, "毒④ 文件未还原!"

rc, out = run()
print(f"\n  还原后复跑:{'✅ 全绿' if rc == 0 else '🔴 仍红 —— 还原不干净'}  ({out.strip().splitlines()[-1]})")
print(f"  四发毒全部红对了:{'✅' if all(results) else '🔴 有锁没牙'}")
sys.exit(0 if all(results) and rc == 0 else 1)
