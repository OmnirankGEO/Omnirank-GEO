# -*- coding: utf-8 -*-
"""#51 注毒 —— 证明「换 id 全表同步锁」真的会红,而且红在对的那条上。

🔴 **每发毒必须先自证「毒真的下进去了」。**
   2026-09-04 我第一版毒③ 写成 `txt.replace(a, a)`(自我替换,纯 no-op)+ 只掐前缀,
   结果理由**没被清空**、锁正确地保持绿,而我差点把它读成「锁③没牙」。
   **绿有两种解释 —— 「锁没牙」与「毒没下成」,方向完全相反。**
   验了还原不够,还要验施加:`mutate()` 断言字节确实变了,不变就当场停。
   (今天写过 4 个毒脚本,全都验了还原、没一个验施加 —— 这是共同缺口。)

四发:
  ① 新增一张 id 索引表**不分类** ⇒ 锁① 红(防「新增表被静默跳过」)
  ② `DB_DOUBLE_HIT`(读点用 `.get()` **不吭声**那张)缺继任者 ⇒ 锁③ 红
     —— 现实里正是这张不会自己报错,所以这发最有价值
  ③ 分类项**理由为空** ⇒ 锁② 红
  ④ 给 pre-rename 表补新 id ⇒ 锁④ 红(防「顺手修」正确代码)
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
LOCKS = "tests/wob39_collection_scope_2026_09_03/test_id_table_rename_phase_contract.py"
ENV = {**os.environ,
       "TEST_DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
       "DATABASE_URL": "postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test",
       "PYTHONIOENCODING": "utf-8"}
ORIG = G.read_bytes()


def mutate(transform) -> None:
    """施加毒,并**自证它真的改了字节**。不变就停 —— 绿会被误读成「锁没牙」。"""
    new = transform(ORIG.decode("utf-8")).encode("utf-8")
    if new == ORIG:
        raise SystemExit("🔴 毒没有改变文件 —— 施毒失败。停,别把随后的绿读成「锁没牙」。")
    G.write_bytes(new)


def restore() -> None:
    G.write_bytes(ORIG)
    assert G.read_bytes() == ORIG, "未还原!"


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
        print(f"       rc={rc} · 点名 {must_lock.split('_the')[0]}={named} · 报文含毒证据={hit}")
        if not ok:
            for l in out.splitlines():
                if "AssertionError" in l:
                    print(f"       {l.strip()[:150]}")
        return ok
    finally:
        restore()


res = []

res.append(shot(
    "毒① 新增 id 索引表但不分类",
    lambda t: 'WOB51_POISON_TABLE: dict[str, dict] = {"MUT-EXTE3-01": {"x": 1}}\n\n\n' + t,
    "test_w51_01_every_id_keyed_table_is_classified", "WOB51_POISON_TABLE"))

res.append(shot(
    "毒② post-rename 表(.get 不吭声那张)缺继任者",
    lambda t: t.replace("DB_DOUBLE_HIT: dict[str, dict] = {\n",
                        'DB_DOUBLE_HIT: dict[str, dict] = {\n'
                        '    "MUT-EXTE3-05": {"dbs": (), "verify_db": None},\n', 1),
    "test_w51_03_post_rename_tables_carry_both_the_old_and_the_new_id",
    "DB_DOUBLE_HIT 有 MUT-EXTE3-05 缺 MUT-EXTE3-05b"))

res.append(shot(
    "毒③ 分类项理由为空",
    lambda t: t.replace(
        '"rule-source|它就是重锚规则本身,是同步的**来源**不是**对象**,不参与。"',
        '"rule-source|"', 1),
    "test_w51_02_every_classification_has_a_legal_phase_and_a_reason", "V2_REANCHORS"))

res.append(shot(
    "毒④ 给 pre-rename 表补新 id",
    lambda t: t.replace('    "MUT-EXTE3-07": {\n',
                        '    "MUT-EXTE3-07b": {"why": "毒", "file": "x", "pairs": []},\n'
                        '    "MUT-EXTE3-07": {\n', 1),
    "test_w51_04_pre_rename_tables_are_not_required_to_carry_the_successor",
    "MUT-EXTE3-07b"))

rc, out = run()
print(f"\n  还原后复跑 rc={rc}（须 0)· {out.strip().splitlines()[-1]}")
res.append(rc == 0)
print(f"\n  {'OK  四发毒全部红对了,还原后回绿' if all(res) else 'XX  有毒没红对'}")
sys.exit(0 if all(res) else 1)
