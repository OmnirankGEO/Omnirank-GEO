#!/usr/bin/env python
"""按 `.tiprun/extsel_v2_replay.json` **复放**,清单从文件读、不从命令行抄。

Review 令③逐字:「清单导出不许抄,你自己的规矩」。所以这支驱动只做三件事:

  ① 读清单 → 与 runner 里钉死的 `REPLAY_LIST_SIZE` 对账,不等就停机;
  ② 把 id 灌进 `V2_ONLY`,交给 v2 runner 跑(它自己会过分母锁、树锁、在盘实证);
  ③ 把「复放前 / 复放后」两态并排落盘,交 Review 收账。

为什么不能手敲 11 个 id:抄一次少一项,而少的那一项**不会让任何判据变红** ——
它只会安静地不复放,然后那一发的"存活"就永远停在上一轮的账上。

用法::

    python scripts/gate9_replay_from_list.py            # 只跑清单里的 11 发
    python scripts/gate9_replay_from_list.py --plus MUT-EXTE2-04
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / ".tiprun" / "extsel_v2_replay.json"
PREV = ROOT / ".tiprun" / "extsel_v2_results.json"
RUNNER = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"


#: 🔴 [收口单② · 2026-08-28] 复放集合的**显式增删**。每一笔都必须引台账条目 ——
#:    没有出处的增删就是"我记得应该这样",而那正是清单会悄悄漂移的入口。
LEDGER = "2026-08-28 · B 尾单三件验收合流 @ fb0f07299"

REPLAY_ADJUSTMENTS: list[dict] = [
    {"op": "-", "id": "MUT-EXTE3-05",
     "why": "锚在 fb0f07299 上命中 0 次(窗口C 89a514e36 改写成 query_to_xml);"
            "Review 裁定重锚,原 id 退役",
     "ledger": LEDGER + "(收口单①)"},
    {"op": "+", "id": "MUT-EXTE3-05b",
     "why": "重锚继任者,只动锚一条轴(承 EXTC-04→04b 判例)",
     "ledger": LEDGER + "(收口单①)"},
    {"op": "+", "id": "MUT-EXTE2-04",
     "why": "令①收编 p03_settlement/p03c 之后,分母洞转正常裁定路,记正式杀",
     "ledger": LEDGER + "(令①收编)"},
]


def expected_replay_set(list_ids) -> set[str]:
    """清单集合 ± 显式增删 = 本轮**应该跑**的那一集。

    🔴 计数式对账会过期,集合式不会。本轮实跑就是活证据:
       跑的是「11 发清单 − EXTE3-05 + EXTE2-04」,集合已经变了,
       而 `REPLAY_LIST_SIZE` 那道**计数**闸从头到尾没响 —— 它数的是清单长度,
       不是"到底跑了哪几发"。同大小、不同集合,计数闸看不出来。
    """
    want = set(list_ids)
    for adj in REPLAY_ADJUSTMENTS:
        if not adj.get("ledger"):
            raise SystemExit(f"🔴 增删 {adj} 没有台账出处 —— 不许无出处改集合")
        if adj["op"] == "-":
            if adj["id"] not in want:
                raise SystemExit(
                    f"🔴 要删的 {adj['id']} 不在清单里 —— 这笔增删本身过期了")
            want.discard(adj["id"])
        elif adj["op"] == "+":
            if adj["id"] in want:
                raise SystemExit(f"🔴 要加的 {adj['id']} 本来就在清单里")
            want.add(adj["id"])
        else:
            raise SystemExit(f"🔴 未知增删动作:{adj['op']}")
    return want


def assert_set_equal(got: set[str], want: set[str], where: str) -> None:
    """两向差集都打出来 —— 只报"不相等"读的人还得自己去猜是多了还是少了。"""
    if got == want:
        return
    raise SystemExit(
        f"🔴 {where}:实跑集合 ≠ 应跑集合(同大小也不放过)\n"
        f"   多跑了:{sorted(got - want) or '无'}\n"
        f"   没跑到:{sorted(want - got) or '无'}\n"
        f"   (大小 {len(got)} vs {len(want)} —— 大小相等**不代表**集合相等,"
        "本轮实跑就踩过这一脚)")


def killed_verdicts() -> frozenset:
    """🔴 [V9-B] 「算杀」的家族**借** runner 那一张,不在这里抄第二份。

    修之前这里是一个写死的 frozenset。V9-B 新增
    `KILLED_BY_WIDER_DENOMINATOR_BLUNT` 之后它当场过期 ——
    而过期的表现不是报错,是那一发**从「杀」这一栏里静静漏掉**,
    然后被门④ 当成「异常 verdict」拒掉,报文指向完全错误的方向。
    """
    return frozenset(_v2().KILLED_FAMILY)


def assert_killed_set_equals(records: dict, want: set, where: str) -> None:
    """门④ 终审:**KILLED 集 ≡ expected 集**,双向相等,并拒绝任何异常 verdict。

    🔴 [Codex P1-7 · 我复核坐实] 原来这里是 `assert_set_equal(set(after), want)`,
       而 `after` 是 id→verdict 的字典,`set(after)` 只拿到 **id**。
       于是「27 个 ID 都在场」就算过 —— **27 个 SURVIVED 也一样过**。
       ID 在场证明的是"跑到了",不是"杀掉了";终审要的是后者。
    """
    ids = set(records)
    assert_set_equal(ids, want, where + "(ID 在场)")

    killed = {i for i, v in records.items() if v in killed_verdicts()}
    abnormal = {i: v for i, v in records.items() if v not in killed_verdicts()}
    if abnormal:
        raise SystemExit(
            f"🔴 {where}:这些发不是「杀」——" + "".join(
                f"\n     {i} → {v}" for i, v in sorted(abnormal.items()))
            + "\n   终审只认杀;存活/语法崩/包坏/未知都必须先有裁定,不许混进 27/27。")
    if killed != want:
        raise SystemExit(
            f"🔴 {where}:KILLED 集 ≠ 应杀集\n"
            f"   多杀:{sorted(killed - want) or '无'}\n"
            f"   少杀:{sorted(want - killed) or '无'}")
    print(f"✅ 终审({where}):KILLED 集 ≡ 应杀集,{len(killed)} 发双向相等")


def _v2():
    spec = importlib.util.spec_from_file_location("_v2replay", RUNNER)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_v2replay"] = m
    sys.path.insert(0, str(RUNNER.parent))
    spec.loader.exec_module(m)
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plus", default="",
                    help="额外带上的发(逗号分隔)—— 只用于收编后要正式改记的发")
    args = ap.parse_args()

    if not LIST.exists():
        raise SystemExit(f"🔴 没有复放清单 {LIST} —— 先跑 scripts/gate9_report.py 导出")
    items = json.loads(LIST.read_text(encoding="utf-8"))
    ids = [x["id"] for x in items]
    V2 = _v2()
    # 计数闸留着(它管"清单本身有没有被动过"),但它**不是**对账 ——
    # 对账是下面那条集合式的。本轮实跑证明:计数闸对"同大小换成员"是瞎的。
    if len(ids) != V2.REPLAY_LIST_SIZE:
        raise SystemExit(
            f"🔴 清单 {len(ids)} 发 ≠ runner 钉死的 {V2.REPLAY_LIST_SIZE} 发 —— "
            "两边必须同批改,停机")
    if len(set(ids)) != len(ids):
        raise SystemExit(f"🔴 清单里有重复 id:{ids}")

    want = expected_replay_set(ids)
    print(f"应跑集合({len(want)} 发)= 清单 {len(ids)} 发 "
          f"± {len(REPLAY_ADJUSTMENTS)} 笔登记增删:")
    for adj in REPLAY_ADJUSTMENTS:
        print(f"   {adj['op']} {adj['id']:<16} {adj['why']}")
        print(f"     台账:{adj['ledger']}")

    plus = [x.strip() for x in args.plus.split(",") if x.strip()]
    known = {m["id"] for m in V2.load_v2()}
    unknown = sorted(set(ids + plus) - known)
    if unknown:
        raise SystemExit(f"🔴 清单里这些 id 在 27 发里不存在:{unknown}")

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                         capture_output=True, text=True).stdout.strip()
    run_ids = sorted(set(ids) | set(plus))
    # 🔴 集合式对账:跑之前先核"我要跑的"就是"应该跑的"。
    assert_set_equal(set(run_ids), want, "开跑前")
    print(f"复放尖 {sha}")
    print(f"清单 {len(ids)} 发(导出,未抄)"
          + (f" + 额外 {len(plus)} 发 {plus}" if plus else ""))
    print("   " + " ".join(x.replace("MUT-", "") for x in run_ids))

    # 上一轮的定性,供并排对账(不参与裁定,只做「前/后」两态)
    before = {}
    if PREV.exists():
        for r in json.loads(PREV.read_text(encoding="utf-8")):
            before[r["id"]] = r["verdict"]

    # 🔴 复放产物**必须另存**:runner 是整份覆盖写的,只跑 12 发就会把 27 条
    #    记录换成 12 条 —— 终账当场不闭合,而"另外 15 发不见了"与"它们没跑过"
    #    在产物里长得一模一样。上一轮的定性也还要留着做「前 / 后」两态。
    out_name = "extsel_v2_results_replay.json"
    env = dict(os.environ, V2_ONLY=",".join(run_ids),
               V2_RESULTS_NAME=out_name, PYTHONIOENCODING="utf-8")
    rc = subprocess.run([sys.executable, str(RUNNER)], cwd=str(ROOT), env=env).returncode

    # 🔴 第二趟**必须**跑:快集只用来分流,`SURVIVED_QUICK` 不是裁定。
    #    上一轮我漏了这一趟,产物里就留下三条 SURVIVED_QUICK —— 而终单裁定③
    #    逐字写着「任何一发要落存活,必须在全分母上再跑一遍仍然零红」。
    #    (`ledger_of` 对 SURVIVED_QUICK 会停机,那道闸兜住了,但兜住不等于跑过。)
    print("\n── 第二趟:快集存活的发过全分母 ──")
    rc2 = subprocess.run([sys.executable, str(RUNNER)], cwd=str(ROOT),
                         env=dict(env, V2_MODE="full_survivors")).returncode
    rc = rc or rc2

    after = {}
    out_path = ROOT / ".tiprun" / out_name
    if out_path.exists():
        for r in json.loads(out_path.read_text(encoding="utf-8")):
            after[r["id"]] = r["verdict"]
    # 🔴 [门④] 跑完终审:不只核"跑到了",还要核"杀掉了"。
    #    "没跑到"与"没变"在报表里长得一模一样,只有集合式对账分得开;
    #    而"跑到了但没杀掉"与"全杀"在**只核 ID 的**对账里同样长得一模一样。
    assert_killed_set_equals(after, want, "跑完终审")
    print("\n── 复放前 / 复放后 ──")
    for i in run_ids:
        b, a = before.get(i, "?"), after.get(i, "?")
        mark = "  " if b == a else "🔀"
        print(f" {mark} {i:<16} {b:<38} -> {a}")
    print(f"\nrunner rc={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
