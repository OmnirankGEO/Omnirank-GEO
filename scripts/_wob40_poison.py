# -*- coding: utf-8 -*-
"""#40 重锚后的真缺陷毒 —— 证明改锚**没有改成恒绿**。

Review 2026-09-04 的要求原文:
  A 把树里分母改错 / 把 CONTRACT 改成别的数 须红
  C 把插件后端 INSERT 里的 article_id 真删掉 须红
    —— [WO_273 · 2026-09-23 退役] 毒 C 的靶文件(插件后端)整体删除,它毒的判据
       `test_progress_insert_writes_article_id` 同单肯定式退役;本脚本只剩毒 A。

判据不是「退出码非 0」——一条判据可能红在别的事上(今天已踩过三次)。
每发毒都要求:**指定的那条判据 failed,且报文点名了毒**。
每发毒 `finally` 里按原始 bytes 逐字节还原,并在还原后**复跑一次**确认回绿。
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


def run(target: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", target,
                        "--no-header", "-p", "no:cacheprovider", "--tb=line"],
                       cwd=ROOT, capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def expect_red(label: str, target: str, must_name: str, needle: str) -> bool:
    rc, out = run(target)
    named = must_name in out
    hit = needle in out
    ok = rc != 0 and named and hit
    print(f"  {'OK ' if ok else 'XX '}{label}")
    print(f"       rc={rc} · 点名 {must_name}={named} · 报文含毒证据={hit}")
    if not ok:
        for l in out.splitlines():
            if "assert" in l or "Error" in l or "FAIL" in l:
                print(f"       {l[:150]}")
    return ok


res = []

# ── 毒 A:把验收器的 CONTRACT 改成别的数 ───────────────────────────
pa = ROOT / "scripts" / "verify_fof_evidence.py"
oa = pa.read_bytes()
try:
    pa.write_bytes(oa.replace(b'"packages": 23', b'"packages": 22'))
    assert pa.read_bytes() != oa, "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    res.append(expect_red("毒A CONTRACT packages 23 -> 22",
                          "scripts/test_mutation_evidence_verifier_contract.py",
                          "test_ev_", "22"))
finally:
    pa.write_bytes(oa)
    assert pa.read_bytes() == oa, "毒A 未还原!"

# ── 毒 C:[WO_273 · 2026-09-23 退役] 靶文件(插件后端)整体删除,见文件头 ──

# ── 还原后复跑:必须回绿 ──────────────────────────────────────────
print()
for t, want in (("scripts/test_mutation_evidence_verifier_contract.py", "103 passed"),):
    rc, out = run(t)
    last = out.strip().splitlines()[-1] if out.strip() else "(空)"
    print(f"  还原后 {t.split('/')[-1]:<48} rc={rc} · {last}")
    res.append(rc == 0)

print(f"\n  {'OK  毒 A 红对了,且还原后回绿' if all(res) else 'XX  有一项不合格'}")
sys.exit(0 if all(res) else 1)
