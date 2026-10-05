# -*- coding: utf-8 -*-
"""#53 注毒 —— 证明「未消费表登记锁」有牙,且红在对的那条上。

每发毒先 `mutate()` **自证字节真的变了**(见 #51 的教训:
`replace(a, a)` 那种 no-op 会让绿被误读成「锁没牙」)。

🔴 还要防第三种误读:**毒下成了但落在分母之外**(不变量真空成立)。
   Review 2026-09-04 给 #51 下第一发毒时就撞到:加了个**不存在的** old id,
   循环体一次都没进,判据 6 passed。
   所以下面每发毒都打在**真实存在的对象**上,并在跑判据前断言它进了分母。

三发:
  ① 取消 `DENOMINATOR_ADJUDICATIONS` 的登记      ⇒ 锁② 红且点名(正样本回放)
  ② 把某个登记项的理由清空                        ⇒ 锁② 红且点名
  ③ 给登记表塞一个**不在本轮清单**里的键          ⇒ 锁③ 红且点名(锚过期)
"""
from __future__ import annotations

import os
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
G = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"
LOCKS = "tests/wob39_collection_scope_2026_09_03/test_doc_only_tables_contract.py"
ENV = {**os.environ,
       "TEST_DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
       "DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
       "PYTHONIOENCODING": "utf-8"}
ORIG = G.read_bytes()


def mutate(transform) -> None:
    new = transform(ORIG.decode("utf-8")).encode("utf-8")
    if new == ORIG:
        raise SystemExit("🔴 毒没有改变文件 —— 施毒失败。停,别把随后的绿读成「锁没牙」。")
    G.write_bytes(new)


def run():
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header",
                        "-p", "no:cacheprovider", "--tb=long", LOCKS],
                       cwd=ROOT, capture_output=True, env=ENV)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def shot(label, transform, must_lock, needle) -> bool:
    try:
        mutate(transform)
        rc, out = run()
        named, hit = must_lock in out, needle in out
        ok = rc != 0 and named and hit
        print(f"  {'OK ' if ok else 'XX '}{label}")
        print(f"       rc={rc} · 点名判据={named} · 报文含毒证据={hit}")
        if not ok:
            for l in out.splitlines():
                if "AssertionError" in l:
                    print(f"       {l.strip()[:150]}")
        return ok
    finally:
        G.write_bytes(ORIG)
        assert G.read_bytes() == ORIG, f"{label} 未还原!"


res = []

res.append(shot(
    "毒① 取消 DENOMINATOR_ADJUDICATIONS 的登记(正样本回放)",
    lambda t: t.replace('    "DENOMINATOR_ADJUDICATIONS":\n', '    "WOB53_POISON_UNREGISTERED":\n', 1),
    "test_w53_02_every_zero_read_table_is_registered_with_a_reason",
    "DENOMINATOR_ADJUDICATIONS"))

res.append(shot(
    "毒② 把 ZERO_DISCRIMINATION 的登记理由清空",
    lambda t: t.replace(
        '        "留档一次已完成的定性(草单预测被杀 / 实跑证明该判据对本形态零判别力),"\n'
        '        "不参与任何终态计算;键必须仍在清单内,由判据守着不许过期",\n',
        '        "",\n', 1),
    "test_w53_02_every_zero_read_table_is_registered_with_a_reason",
    "ZERO_DISCRIMINATION"))

res.append(shot(
    "毒③ 登记表的键不在本轮清单里(锚过期)",
    lambda t: t.replace('ZERO_DISCRIMINATION: dict[str, dict] = {\n',
                        'ZERO_DISCRIMINATION: dict[str, dict] = {\n'
                        '    "MUT-EXTE9-99": {"criterion": "x", "why": "毒"},\n', 1),
    "test_w53_03_registered_tables_keys_are_still_in_the_current_roster",
    "MUT-EXTE9-99"))

rc, out = run()
print(f"\n  还原后复跑 rc={rc}（须 0)· {out.strip().splitlines()[-1]}")
res.append(rc == 0)
print(f"\n  {'OK  三发毒全部红对了,还原后回绿' if all(res) else 'XX  有毒没红对'}")
sys.exit(0 if all(res) else 1)
