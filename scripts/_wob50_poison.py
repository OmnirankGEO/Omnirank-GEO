# -*- coding: utf-8 -*-
"""#50 端到端毒 —— 复现 2026-09-04 那次误判,证明新报文能救回来。

那次误判的经过:6 条 `evidence_verifier` 红,我 grep「权威合同变了」,
只有 `ev_00` 命中,另 5 条 **0 命中**,于是报了「不是同一根因」。
真相是那 5 条的报文写作 `out[-800:]`(尾截断),而这句话出现在验收器输出的
**第 0 项、最开头** —— 报文把原因切掉了,不是根因不同。

本毒把 `CONTRACT` 改成漂移值(让验收器在第 0 项失败,原因落在输出开头),
然后跑一条**用 `_reason` 的**判据,要求:

  ① pytest 报文里**含**那条 `XX 树里的权威合同变了` —— 新写法救回来了
  ② 同一份原始输出的**尾 800 字符里不含**它 —— 证明旧写法必然漏掉
     (没有②,①只能说明「原因碰巧在尾部」,证明不了新写法有用)
"""
from __future__ import annotations

import io
import contextlib
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile

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
NEEDLE = "树里的权威合同变了"
TARGET = ("scripts/test_mutation_evidence_verifier_contract.py::"
          "test_ev_s08_a_clean_two_segment_package_passes")

ENV = {**os.environ,
       "TEST_DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
       "DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
       "PYTHONIOENCODING": "utf-8"}

pv = ROOT / "scripts" / "verify_fof_evidence.py"
orig = pv.read_bytes()
res = []
try:
    assert orig.count(b'"packages": 23') == 1, "毒锚不唯一,不下毒"
    pv.write_bytes(orig.replace(b'"packages": 23', b'"packages": 21'))

    # ── ② 先证「旧写法必然漏掉」:直接取原始输出的尾 800 ────────────
    spec = importlib.util.spec_from_file_location(
        "_evp", ROOT / "scripts" / "test_mutation_evidence_verifier_contract.py")
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    with tempfile.TemporaryDirectory() as td:
        root = m.build(pathlib.Path(td) / "ev")
        rc0, raw, _ = m.verify(root)
    in_tail = NEEDLE in raw[-800:]
    in_full = NEEDLE in raw
    print(f"  ② 原始输出 {len(raw)} 字符 · 尾 800 含原因={in_tail}（须 False)"
          f" · 全文含={in_full}（须 True)")
    res.append((not in_tail) and in_full)

    # ── ① 再证「新写法带上了」:真跑一条判据,读它的 pytest 报文 ────
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header",
                        "-p", "no:cacheprovider", "--tb=long", TARGET],
                       cwd=ROOT, capture_output=True, env=ENV)
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    named = NEEDLE in out
    print(f"  ① 判据红了={r.returncode != 0}（须 True)· 报文含原因={named}（须 True)")
    if not named:
        for l in out.splitlines():
            if "XX" in l or "assert" in l:
                print(f"       {l.strip()[:140]}")
    res.append(r.returncode != 0 and named)
finally:
    pv.write_bytes(orig)
    assert pv.read_bytes() == orig, "毒未还原!"

r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header",
                    "-p", "no:cacheprovider", TARGET], cwd=ROOT,
                   capture_output=True, env=ENV)
print(f"\n  还原后复跑 rc={r.returncode}（须 0)· "
      f"{r.stdout.decode('utf-8','replace').strip().splitlines()[-1]}")
res.append(r.returncode == 0)

print(f"\n  {'OK  新报文带上了原因,而旧写法必然漏掉' if all(res) else 'XX  有一项不合格'}")
sys.exit(0 if all(res) else 1)
