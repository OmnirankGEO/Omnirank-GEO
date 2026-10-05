# -*- coding: utf-8 -*-
"""#48 v2 毒 —— 要求两个方向都在**第一条**报文里点名。

Review 2026-09-04 下毒发现:往 `DISK_PACKAGES` 里加一个**盘上没有**的名字时,
先响的是 `ea_12` 的计数断言,它的报文走 `_census_diag`;而 v1 的 `_census_diag`
只列「无 git 跟踪文件的目录」—— 幽灵不在盘上,那一栏打出「0 个:(无)」,
**幽灵名一个字没出现**。「盘上多了/少了」在后面的断言里,被遮住了。

⇒ 与 `s08` 那课同形:**第一条断言的报文没带上原因**。
   本毒因此只认**第一条** FAILED 的报文 —— 后面的断言点名不算数。
"""
from __future__ import annotations

import os
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
G9 = ROOT / "scripts" / "gate9_full_denominator_baseline.py"
EA12 = ("scripts/test_mutation_gate9_extra_args_contract.py::"
        "test_ea_12_disk_census_matches_and_the_delta_is_exactly_this_package")
ENV = {**os.environ,
       "TEST_DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
       "DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
       "PYTHONIOENCODING": "utf-8"}


def first_failure_report() -> tuple[int, str]:
    """只跑 `ea_12` 一条 —— 它就是 Review 那条路径上**先响**的那条。"""
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header",
                        "-p", "no:cacheprovider", "--tb=long", EA12],
                       cwd=ROOT, capture_output=True, env=ENV)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


res = []

# ── 方向 ① 盘上多一个(名单没有)──────────────────────────────────
d = ROOT / "tests" / "wob48v2_extra_on_disk_2026_09_04"
try:
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    assert (d / "__init__.py").is_file(), "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    rc, out = first_failure_report()
    named = d.name in out
    ok = rc != 0 and named
    print(f"  {'OK ' if ok else 'XX '}① 盘上多一个 ⇒ **第一条**报文点名")
    print(f"       rc={rc} · 点名={named}")
    if not ok:
        for l in out.splitlines():
            if "盘上有而" in l or "名单有而" in l or "assert" in l:
                print(f"       {l.strip()[:150]}")
    res.append(ok)
finally:
    shutil.rmtree(d, ignore_errors=True)
    assert not d.exists(), "① 未清理!"

# ── 方向 ② 名单多一个(盘上没有)—— Review 那一发 ──────────────────
GHOST = "wob48v2_ghost_never_on_disk_2026_09_04"
orig = G9.read_bytes()
try:
    txt = orig.decode("utf-8")
    a = 'DISK_PACKAGES: tuple[str, ...] = (\n'
    assert txt.count(a) == 1, "名单锚点不唯一,不下毒"
    G9.write_bytes(txt.replace(a, a + '    "' + GHOST + '",\n', 1).encode("utf-8"))
    assert G9.read_bytes() != orig, "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    rc, out = first_failure_report()
    named = GHOST in out
    ok = rc != 0 and named
    print(f"  {'OK ' if ok else 'XX '}② 名单多一个 ⇒ **第一条**报文点名(Review 那一发)")
    print(f"       rc={rc} · 点名={named}")
    if not ok:
        for l in out.splitlines():
            if "盘上有而" in l or "名单有而" in l or "无 git 跟踪" in l:
                print(f"       {l.strip()[:150]}")
    res.append(ok)
finally:
    G9.write_bytes(orig)
    assert G9.read_bytes() == orig, "② 未还原!"

rc, out = first_failure_report()
print(f"\n  还原后 ea_12 rc={rc}（须 0)")
res.append(rc == 0)
print(f"\n  {'OK  两个方向都在第一条报文里点名了' if all(res) else 'XX  有一个方向没点名'}")
sys.exit(0 if all(res) else 1)
