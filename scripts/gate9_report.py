#!/usr/bin/env python
"""把 V2 27 发的机器产物渲染成**报数三栏 + 每发在盘三字段**的表。

为什么要脚本渲染而不是我手打:报数纪律是「自选/他选分栏,永不合并成一个数」,
而手打表格正是那两栏被悄悄合并的地方 —— 顺带,手抄 27 行会再抄错一次
(本轮我已经因为转录错停过一次机)。表从 JSON 来,数从 JSON 数。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / ".tiprun" / "extsel_v2_results.json"
REPLAY = ROOT / ".tiprun" / "extsel_v2_replay.json"


def _v2():
    """终账口径长在 v2 runner 里(裁定词汇的家),这里只借来渲染,不另抄一份。"""
    import importlib.util

    p = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"
    spec = importlib.util.spec_from_file_location("_v2ledger", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_v2ledger"] = m
    sys.path.insert(0, str(p.parent))
    spec.loader.exec_module(m)
    return m

#: 🔴 [V9-B] 渲染名要**覆盖 runner 认识的全部裁定**,下面 `_assert_cn_covers`
#:    机械核一遍:新增一种裁定而忘了给它中文名 ⇒ 报表里出现裸的英文常量,
#:    读表的人不会知道那是一种新终态,只会以为是别的什么。
_VERDICT_CN = {
    "KILLED": "杀",
    "SURVIVED_IN_11PKG_BUT_KILLED_OUTSIDE": "旧 11 包内存活 · **分母洞**(已收编,见尾单)",
    "SURVIVED_QUICK": "快集存活(未过全分母)",
    "SURVIVED_FULL": "存活(全分母)",
    "KILLED_BY_WIDER_DENOMINATOR": "杀(更宽分母)",
    "KILLED_BLUNT": "杀 · **钝**(红集不可用于定位)",
    "KILLED_BY_WIDER_DENOMINATOR_BLUNT": "杀(更宽分母)· **钝**",
    "INVALID_SYNTAX": "作废 · 变异后语法不合法(出题/抽取错,**不是杀**)",
    "PACKAGE_BROKEN_NEEDS_REVIEW": "需裁定 · 有包跑不起来(**不记存活**)",
    "GREEN_NOT_CONSERVED": "需裁定 · 零红但绿数没守恒(有判据没跑)",
    "REANCHOR_EXPECT_VIOLATED": "需裁定 · 重锚期望未命中",
    "EXPECTED_KILLER_NOT_RUN": "需裁定 · 登记的杀手根本没跑(skip/未收集)",
    "RUN_META_UNUSABLE": "需裁定 · 这一发的读数本身不可信",
    "REPLAY_DISAGREES_WITH_QUICK": "需裁定 · 快集判存活、全分母判红且新红全在快集内(两次结果不一致)",
}


def _assert_cn_covers() -> None:
    """渲染名的分母 = runner 的裁定全集,不是我手写的那几行。"""
    m = _v2()
    known = set(m.RESOLVED_VERDICTS) | set(m.UNRESOLVED_VERDICTS)
    missing = sorted(known - set(_VERDICT_CN))
    if missing:
        raise SystemExit(
            f"🔴 这些裁定没有渲染名:{missing} —— 新终态必须同批决定它怎么显示")


def _proof_cell(rec: dict) -> str:
    """在盘三字段:anchor_hits · sha 前≠后 · 落盘实证。**按 proof 类型渲染**。

    🔴 [P2-10] 原来这里只看 `readback_needle_gone`。对**插入型**变异
       (替换串**包含**锚,如 `…现取…` → `**并非**…现取…`)毒完全落盘,
       该字段却是 False ⇒ **合法 proof 被渲染成叉**,读表的人会以为毒没落盘。
       引擎 2026-08-27 已经因为同一条规则的适用域太小停过一次机
       (MUT-EXTE2-10),那次修的是判定、这次修的是渲染 —— 同一个坑的两侧。

    规则:带 `exact_match` 的是**逐字节等价**型 proof,以它为准(最硬);
         没有的才回落到「锚不见了 / 替换在场」两字段。
    """
    out = []
    for pf in rec.get("on_disk_proof") or []:
        head = (f"锚{pf.get('pair', 0)}:hits={pf['anchor_hits']} · "
                f"{pf['sha_before']}≠{pf['sha_after']} · ")
        if "exact_match" in pf:
            out.append(head + ("逐字节✓" if pf["exact_match"] else "逐字节✗"))
        else:
            out.append(
                head
                + f"回读{'✓' if pf.get('readback_needle_gone') else '✗'}"
                + f"{'/✓' if pf.get('readback_repl_present') else '/✗'}")
    return " ; ".join(out) or "—"


def _load_recs() -> list[dict]:
    """原 27 发 + (若有)复放产物**叠加**。

    🔴 为什么不是"复放产物就是结果":runner 是整份覆盖写的,复放只跑 12 发 ⇒
       那份文件只有 12 条。直接拿它当结果,终账立刻不闭合,而"另外 15 发不见了"
       与"它们没跑过"在产物里长得一模一样。所以按 id 叠加,并留下 `replayed_from`。
    """
    if not RES.exists():
        raise SystemExit(f"🔴 没有结果:{RES}")
    recs = json.loads(RES.read_text(encoding="utf-8"))
    rp = ROOT / ".tiprun" / "extsel_v2_results_replay.json"
    if not rp.exists():
        return recs
    over = {r["id"]: r for r in json.loads(rp.read_text(encoding="utf-8"))}
    merged = []
    for r in recs:
        if r["id"] in over:
            n = dict(over.pop(r["id"]))
            n["replayed_from"] = r["verdict"]
            merged.append(n)
        else:
            merged.append(r)
    if over:
        raise SystemExit(f"🔴 复放产物里有不在原 27 发里的 id:{sorted(over)}")
    n_re = sum(1 for r in merged if "replayed_from" in r)
    print(f"> 已叠加复放产物 `{rp.name}`:{len(merged)} 发里有 **{n_re}** 发是复放后的读数。")
    print("")
    return merged


def main() -> int:
    _assert_cn_covers()
    recs = _load_recs()

    for fam in ("E2", "E3"):
        g = [r for r in recs if r["family"] == fam]
        print(f"\n### {fam} 栏({len(g)} 发)\n")
        print("| 发 | 预测 | 实得 | 实际红集 | 在盘三字段 |")
        print("|---|---|---|---|---|")
        for r in sorted(g, key=lambda x: x["id"]):
            red = r.get("full_red") if r.get("full_red") is not None else r.get("quick_red")
            red_s = ("、".join(x.split("::")[-1] for x in (red or [])[:4])
                     + (f" …共 {len(red)}" if red and len(red) > 4 else "")) or "(零红)"
            # 🔴 抖动改判过的发:红集要标出来"这条红已被坐实为没验到",
            #    否则表里一条红配一个"存活"的裁定,读起来像自相矛盾。
            if r.get("flake_adjudication"):
                red_s += "（**经双臂坐实为「没验到」**,非杀 —— 见改判表）"
            pred = r["predict"]["verdict"]
            if r["predict"].get("exact_n"):
                pred += f"/恰{r['predict']['exact_n']}"
            print(f"| {r['id']} | {pred} | {_VERDICT_CN.get(r['verdict'], r['verdict'])} "
                  f"| {red_s} | {_proof_cell(r)} |")

    print("\n### 汇总栏\n")
    print("| 栏 | 发数 | 杀 | 11 包内存活 | 分母洞 | 快集存活未复核 |")
    print("|---|---|---|---|---|---|")
    tot = [0, 0, 0, 0, 0]
    for fam in ("E2", "E3"):
        g = [r for r in recs if r["family"] == fam]
        k = sum(1 for r in g if r["verdict"] in _v2().KILLED_FAMILY)
        sf = sum(1 for r in g if r["verdict"] == "SURVIVED_FULL")
        dh = sum(1 for r in g if r["verdict"] == "SURVIVED_IN_11PKG_BUT_KILLED_OUTSIDE")
        sq = sum(1 for r in g if r["verdict"] == "SURVIVED_QUICK")
        print(f"| {fam} | {len(g)} | {k} | {sf} | {dh} | {sq} |")
        for i, v in enumerate((len(g), k, sf, dh, sq)):
            tot[i] += v
    print(f"| **合计** | **{tot[0]}** | **{tot[1]}** | **{tot[2]}** | **{tot[3]}** | **{tot[4]}** |")
    # 🔴 每一发必须恰好落进一栏 —— 不许有发从所有栏里漏出去。
    assert tot[0] == tot[1] + tot[2] + tot[3] + tot[4], (
        f"分栏不闭合:总 {tot[0]} ≠ 杀 {tot[1]} + 存活 {tot[2]} + 分母洞 {tot[3]} + 未复核 {tot[4]}")
    print("")
    print(f"> 分栏闭合自证:{tot[1]} + {tot[2]} + {tot[3]} + {tot[4]} = **{tot[0]}** ✅")

    # 预测 vs 实得(草单预测是**机械抽**的,只做对账不做裁定)
    agree = dis = 0
    rows = []
    for r in recs:
        pv = r["predict"]["verdict"]
        killed = r["verdict"] in _v2().KILLED_FAMILY
        if pv == "conditional" or pv == "?":
            continue
        ok = (pv == "killed") == killed
        agree += ok
        dis += (not ok)
        if not ok:
            rows.append(f"{r['id']}(草单预测 {pv} · 实得 {'杀' if killed else '存活'})")
    print(f"\n**预测对账**:相符 {agree} · 不符 {dis}")
    for x in rows:
        print(f"  - {x}")
    print("\n> 草单预测是**机械抽自**草单「- 预测:」行,只用于对账;"
          "不符不等于谁错 —— 交 Review 裁定。")

    # ── 终账栏(报数 ≠ 终账:报数说测出了什么,终账说接下来归谁)──────────
    V2 = _v2()
    print("\n### 终账栏(每发接下来归谁)\n")
    print("| 终账 | 发数 | 发 |")
    print("|---|---|---|")
    order = [V2.LEDGER_KILLED, V2.LEDGER_REPLAY, V2.LEDGER_CLOSED_OUTSIDE]
    for lab in order:
        ids = sorted(r["id"] for r in recs if V2.ledger_of(r["verdict"]) == lab)
        print(f"| {lab} | {len(ids)} | {'、'.join(ids) or '—'} |")
    replay = V2.build_replay_list(recs)          # 内含钉大小 + 闭合自证
    REPLAY.write_text(json.dumps(replay, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"\n> 复放清单已导出 `{REPLAY.relative_to(ROOT)}` · "
          f"**{len(replay)} 发**(钉死 {V2.REPLAY_LIST_SIZE});"
          "A/C 合流后按这份清单复放,不凭记忆重列。")
    print("> 🔴 **MUT-EXTE2-04 不在复放清单里**:它在 11 包分母下确属存活(报数为真),"
          "但杀手判据仓里**已经有**(`tests/p03_settlement_2026_08_24/test_closeout_p03b.py` "
          "逐池断言 + 判别力自证),决定性双臂坐实(臂A 32 passed 证明真跑非 skip / 臂B 2 failed)。"
          "它的处方是**补分母**,不是补判据 —— 留在存活栏会派人去写一条已经存在的判据。")

    surv = [r for r in recs if r["verdict"] == "SURVIVED_FULL"]
    if surv:
        print(f"\n### 存活汇总表(交 Review 定「洞 / 冗余」· {len(surv)} 发)\n")
        print("| 发 | 文件 | 全分母绿 | 在盘三字段 | 草单自陈的判据洞 |")
        print("|---|---|---|---|---|")
        for r in sorted(surv, key=lambda x: x["id"]):
            print(f"| {r['id']} | `{r['file']}` | {r.get('full_green', '—')} "
                  f"| {_proof_cell(r)} | {r['predict']['text'][:90]} |")
        print("\n> 冗余须**双发反证或数学证明**(照 MUT-EXTC-04/05 判例);"
              "没有证据的一律记「未定性」,不写成冗余。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
