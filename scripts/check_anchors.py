# -*- coding: utf-8 -*-
"""防回退锚登记表核对器(ENG_MGMT_REFORM_2026-09-05 · R2 · 窗口B)。

    python scripts/check_anchors.py            # 核对,rc=0 全过 / rc=1 有 FAIL
    python scripts/check_anchors.py --selftest # 自证:每种坏法各造一次,每种必须被抓

## 它管什么

| 检查 | 级别 | 抓的是 |
|---|---|---|
| 在役锚的 `pattern` 必须在文件里命中 | FAIL | 锚被删/被改名而没人登记 ⇒ 判据静默失效 |
| 在役 count 锚的 `expected` 必须与文件里的数一致 | FAIL | 期望值被改错 |
| **退役锚的 `pattern` 必须不命中** | FAIL | 有人把已退役的锚重新启用 |
| 退役锚必须有 `successor`,且每个继任者都存在 | FAIL | 退役 = 换人守,不是不守了 |
| 每条都要有非空 `why` | FAIL | 空理由等于没登记 |
| **`set` 型锚的 `expected` 必须是 null** | FAIL | 它不参与比对;留个数在那儿会让人**以为**它被核着 |
| `count` 型锚必须有 `expected` | FAIL | 计数锚不带数,比什么 |
| 在役 count 锚为 0 条 | WARN | 「expected 与树一致」那条检查**分母为空、恒真** |
| count 型在役锚超过 2 班未复核 | WARN | 计数锚会过期,而过期有恒红/恒绿两种坏法 |
| `trains` 末项必须是 HEAD 或其祖先 | WARN | 登记表落后于树 |

## 🔴 它**不**管什么(边界写在这里,免得被当成全覆盖)

**本表的分母 = 本表列出的条目。它回答不了「仓里有没有没登记的锚」。**
拿本表当唯一分母 ⇒ 没登记的那条永远在分母之外、永远恒绿 ——
同仓 `DOC_ONLY_TABLES` 就是这么躺了很久(它承诺配判据,判据从没写,
于是第二个违规者无声躺进来,而它的存在让人**停止检查**)。

「有没有漏登记」需要另一条判据:机械枚举仓里的计数型断言,逐条要求在本表里有 id。
那条**尚未建**(本单只交登记表 + 核对器)。⚠️ 这句必须留着 ——
删掉它,下一个人会以为锚这件事已经全覆盖了。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(__file__).resolve().parent.parent
REG = ROOT / "scripts" / "anchors_registry.json"
STALE_AFTER_TRAINS = 2

REQUIRED = ("id", "introduced_train", "kind", "file", "pattern",
            "expected", "retired_by", "successor", "last_reviewed_train", "why")


def load() -> dict:
    return json.loads(REG.read_text(encoding="utf-8"))


def _num_near(text: str, pattern: str):
    """在 pattern 命中处附近取出那个数 —— count 锚的 expected 要与文件里的数对上。

    只在**命中片段本身**里找数字,不向外扩窗口:
    向外扩会把邻近无关的数字读进来,而那种误读的读数**看起来完全正常**。
    """
    i = text.find(pattern)
    if i < 0:
        return None
    nums = re.findall(r"-?\d+", pattern)
    return int(nums[-1]) if nums else None


def check(reg: dict) -> tuple[list[str], list[str]]:
    fails, warns = [], []
    anchors = reg.get("anchors", [])
    if not anchors:
        fails.append("registry 里一条锚都没有 —— 分母为空,下面所有检查恒真")
        return fails, warns

    trains = [t["label"] for t in reg.get("trains", [])]
    if not trains:
        warns.append("trains 为空 —— 「几班没复核」这条检查失效")

    ids = [a.get("id") for a in anchors]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        fails.append(f"id 重复:{dup} —— id 是稳定标识,退役后也不许复用")

    known = set(ids)
    for a in anchors:
        aid = a.get("id", "(无 id)")
        miss = [f for f in REQUIRED if f not in a]
        if miss:
            fails.append(f"[{aid}] 缺字段 {miss}")
            continue
        if not str(a["why"]).strip():
            fails.append(f"[{aid}] why 为空 —— 空理由等于没登记")
        if a["kind"] not in ("count", "set"):
            fails.append(f"[{aid}] kind={a['kind']!r},只许 count / set")
        # 🔴 [2026-09-05 · Review 毒存活后补] `set` 型的 expected **不在比对轴上**,
        #    所以它必须是 null。写个数在那儿,读起来像被核着,其实是**死字段** ——
        #    Review 把一条 set 锚的 expected 改成 999,四条判据全绿,就是这么来的。
        #    (同仓已有前例:只声明不消费的表让人以为终态被它管着。)
        if a["kind"] == "set" and a["expected"] is not None:
            fails.append(
                f"[{aid}] kind=set 却有 expected={a['expected']!r} —— "
                f"set 型的 expected **不参与任何比对**,留个数在那儿会让人以为它被核着。"
                f"要么置 null,要么把这条改成 kind=count(那样 expected 才真的被比)。")
        if a["kind"] == "count" and a["expected"] is None:
            fails.append(f"[{aid}] kind=count 却没有 expected —— 计数锚不带数,比什么?")

        p = ROOT / a["file"]
        exists = p.is_file()
        # 🔴 [#103 后续 · 合流后兑现] 文件存在性**不能**在退役分支之前无条件判。
        #    退役常常就意味着那个文件被整份删掉(本例:A 的 c6aaed780 删
        #    planSideConverge.ts)。原来这一句在退役判定**之前** ⇒ 一条**正确退役**
        #    的锚永远绿不了,而它给出的报文是「文件不在」—— 读起来像锚坏了,
        #    实际是锚退休了。两者处置相反:一个要去修,一个要去销账。
        if not exists and not a["retired_by"]:
            fails.append(f"[{aid}] 文件不在:{a['file']}")
            continue
        text = p.read_text(encoding="utf-8-sig") if exists else ""
        hit = exists and a["pattern"] in text
        # ⚠️ 边界:文件已删的退役锚,「已退役却仍在」那条就无从判起了。
        #    也就是说**改名**(文件搬走、锚照旧活着)这一档,退役锚这边看不见。
        #    写出来,免得下一个人以为这一层管住了全部。

        if a["retired_by"]:
            # 退役锚:不许还在,且必须有活着的继任者
            if hit:
                fails.append(
                    f"[{aid}] **已退役却仍在文件里**:{a['file']}。"
                    f"退役于 {a['retired_by']}。要么它被重新启用了(那要撤销退役并说明),"
                    f"要么退役时没删干净。")
            if not a["successor"]:
                fails.append(f"[{aid}] 已退役却没有 successor —— 退役 = 换人守,不是不守了")
            for s in a["successor"]:
                if s not in known:
                    fails.append(f"[{aid}] successor {s!r} 不在 registry 里")
                else:
                    su = next(x for x in anchors if x["id"] == s)
                    if su["retired_by"]:
                        fails.append(f"[{aid}] 的继任者 {s} 自己也退役了 —— 断链")
        else:
            # 在役锚:必须命中
            if not hit:
                fails.append(
                    f"[{aid}] **在役却找不到**:{a['file']} 里没有 {a['pattern']!r}。"
                    f"锚被删/改名而没登记 ⇒ 那条判据已经静默失效。")
            elif a["kind"] == "count":
                got = _num_near(text, a["pattern"])
                if got is not None and got != a["expected"]:
                    fails.append(
                        f"[{aid}] expected={a['expected']} 与文件里的 {got} 不符")
            # 🔴 [#103] 第三态「待退役」:已指定继任者、尚未退役。
            #    原来这里一律 FAIL(「在役锚不该有 successor」)⇒ 这一档**被规则主动禁止**,
            #    写进去当场红。与「遗漏第三态」症状相同(写不进去),处置不同:
            #    遗漏是补一档,禁止是**先改那条规则**。
            if a.get("retiring_in"):
                if not a["successor"]:
                    fails.append(
                        f"[{aid}] 标了 retiring_in={a['retiring_in']!r} 却没有 successor —— "
                        f"「待退役」没有继任者就是一个**永不到期的挂起状态**,"
                        f"它会一直挂着而没人被指派去守。")
                for s in a["successor"]:
                    if s not in known:
                        fails.append(f"[{aid}] successor {s!r} 不在 registry 里")
                # 🔴 继任者不许是**缺席锁**:「旧符号不再出现」这一个读数,
                #    被「功能被正确替换」与「整块功能被误删」**同时**满足,而两者含义相反。
                #    退役 = 换人守,不是不守了 ⇒ successor 必须钉替代物的**在与对**。
                #    ⚠️ 本检查很弱:它只拦得住**字面**写成否定式的措辞,
                #       拦不住「肯定式措辞但实际没接线」。弱在哪必须写出来,
                #       否则下一个人会以为这一层管住了全部。
                for s in a["successor"]:
                    if s not in known:
                        continue
                    sp = next(x for x in anchors if x["id"] == s)
                    neg = ("不再", "不存在", "no longer", "absent", "0 处", "零处", "removed")
                    blob = f"{sp.get('pattern','')} {sp.get('why','')}"
                    if any(n in blob for n in neg) and not any(
                            k in blob for k in ("仍在", "存在且", "必须有", "must", "present")):
                        fails.append(
                            f"[{aid}] 的继任者 {s} 看起来是**缺席锁**(措辞含 {neg!r} 之一,"
                            f"且没有任何肯定式表述)。「旧的不见了」同时被『正确替换』与"
                            f"『整块被误删』满足 —— 改成钉替代物的在与对。"
                            f"(本检查只拦字面形态,拦不住『肯定式措辞但未接线』。)")
            elif a["successor"]:
                fails.append(
                    f"[{aid}] 在役锚不该有 successor(它还没退役)。"
                    f"若确实是**待退役**(继任者已定、退役随某班一起做),"
                    f"加 `retiring_in: \"<班次>\"` —— 那是第三态,不是违规。")

        # count 锚过期 WARN
        if a["kind"] == "count" and not a["retired_by"] and trains:
            lr = a["last_reviewed_train"]
            if lr not in trains:
                warns.append(f"[{aid}] last_reviewed_train={lr!r} 不在 trains 表里")
            else:
                behind = len(trains) - 1 - trains.index(lr)
                if behind > STALE_AFTER_TRAINS:
                    warns.append(
                        f"[{aid}] count 锚已 {behind} 班未复核(阈值 {STALE_AFTER_TRAINS})"
                        f" —— 计数锚会过期,而过期有恒红/恒绿两种坏法,后者不会报警")

    # 🔴 [2026-09-05] 分母自证:在役 count 锚为 0 时,「expected 与树一致」这条
    #    检查**分母为 0、恒真** —— 代码在 ≠ 它在守东西。必须说出来,否则
    #    读的人会以为「计数锚会被核」这件事已经有人管了。
    live_count = [a for a in anchors
                  if a.get("kind") == "count" and not a.get("retired_by")]
    if not live_count:
        warns.append(
            "在役 count 锚 = 0 条 ⇒ 「expected 必须与树实测一致」这条检查**当前恒真**"
            "(分母为空)。它的判别力由 --selftest 的合成样本提供,**不由真表提供**。"
            "下一条真的计数锚登记进来时,这条 WARN 会自己消失。")

    # trains 末项 vs HEAD
    if reg.get("trains"):
        tip = reg["trains"][-1]["tip"]
        r = subprocess.run(["git", "merge-base", "--is-ancestor", tip, "HEAD"],
                           cwd=ROOT, capture_output=True)
        if r.returncode != 0:
            warns.append(
                f"trains 末项 {tip[:9]} 不是 HEAD 的祖先 —— 登记表落后于树,"
                f"「几班没复核」的算法会偏小")
    return fails, warns


# ══ 自证:每种坏法各造一次,都必须被抓(条数由 cases 派生,不手写)═══════
def selftest() -> int:
    """🔴 判据不是「跑完没报错」,是**每种坏法都被抓到,且抓的是对的那一种**。

    合成 registry,不碰真文件 —— 用真文件做自证等于把树弄脏。
    """
    import copy
    base = load()
    live = next(a for a in base["anchors"] if not a["retired_by"] and a["kind"] == "set")
    dead = next(a for a in base["anchors"] if a["retired_by"])

    cases = []

    def mk(label, mutate, needle):
        r = copy.deepcopy(base)
        mutate(r)
        cases.append((label, r, needle))

    mk("① 在役锚的 pattern 改成找不到的",
       lambda r: r["anchors"].__setitem__(
           r["anchors"].index(next(a for a in r["anchors"] if a["id"] == live["id"])),
           {**live, "pattern": "def test_this_never_exists_zzz()"}),
       "在役却找不到")

    mk("② 退役锚的 pattern 改成命中(= 被重新启用)",
       lambda r: r["anchors"].__setitem__(
           r["anchors"].index(next(a for a in r["anchors"] if a["id"] == dead["id"])),
           {**dead, "file": live["file"], "pattern": live["pattern"]}),
       "已退役却仍在文件里")

    mk("②b 在役锚的文件不在 ⇒ 必须报「文件不在」(阴性对照:这一档不许被下面那条放过)",
       lambda r: r["anchors"].__setitem__(
           r["anchors"].index(next(a for a in r["anchors"] if a["id"] == live["id"])),
           {**live, "file": "frontend/src/pages/__never_exists_zzz__.ts"}),
       "文件不在")

    mk("③ 退役锚没有 successor",
       lambda r: r["anchors"].__setitem__(
           r["anchors"].index(next(a for a in r["anchors"] if a["id"] == dead["id"])),
           {**dead, "successor": []}),
       "没有 successor")

    mk("④ why 为空",
       lambda r: r["anchors"].__setitem__(
           r["anchors"].index(next(a for a in r["anchors"] if a["id"] == live["id"])),
           {**live, "why": "   "}),
       "why 为空")

    mk("⑤ count 锚 expected 改错",
       lambda r: r["anchors"].append(
           {**dead, "id": "selftest-count-wrong", "retired_by": None, "successor": [],
            "file": "scripts/check_anchors.py",
            "pattern": "STALE_AFTER_TRAINS = 2", "kind": "count", "expected": 99,
            "last_reviewed_train": r["trains"][-1]["label"], "why": "自证用"}),
       "与文件里的 2 不符")

    mk("⑥ count 锚超过 2 班未复核",
       lambda r: r["anchors"].append(
           {**dead, "id": "selftest-stale", "retired_by": None, "successor": [],
            "file": "scripts/check_anchors.py",
            "pattern": "STALE_AFTER_TRAINS = 2", "kind": "count", "expected": 2,
            "last_reviewed_train": r["trains"][0]["label"], "why": "自证用"}),
       "班未复核")

    mk("⑦ set 型锚带了 expected(死字段,读起来像被核着)",
       lambda r: r["anchors"].__setitem__(
           r["anchors"].index(next(a for a in r["anchors"] if a["id"] == live["id"])),
           {**live, "expected": 999}),
       "set 却有 expected")

    mk("⑧ count 型锚没有 expected",
       lambda r: r["anchors"].append(
           {**dead, "id": "selftest-count-no-expected", "retired_by": None,
            "successor": [], "file": "scripts/check_anchors.py",
            "pattern": "STALE_AFTER_TRAINS = 2", "kind": "count", "expected": None,
            "last_reviewed_train": r["trains"][-1]["label"], "why": "自证用"}),
       "count 却没有 expected")

    ok = True
    for label, r, needle in cases:
        fails, warns = check(r)
        msgs = "\n".join(fails + warns)
        hit = needle in msgs
        print(f"  {'OK ' if hit else 'XX '}{label}")
        if not hit:
            print(f"       没抓到「{needle}」· 实得:{(fails + warns)[:2]}")
        ok = ok and hit

    # 阴性对照:原表本身必须无 FAIL —— 否则上面每一条都可能是它自己在红
    fails, _ = check(base)
    clean = not fails
    print(f"  {'OK ' if clean else 'XX '}阴性对照:未改动的 registry 无 FAIL")
    if not clean:
        for f in fails:
            print(f"       {f}")
    ok = ok and clean
    # 🔴 条数从 len(cases) **派生**,不手写。写死「六种」之后我加到八种,
    #    那句话仍然说六种 —— 一个陈旧的数字读起来完全正常,没有任何东西会红。
    n = len(cases)
    print(f"\n  {'OK  ' + str(n) + ' 种坏法全部被抓,且阴性对照干净' if ok else 'XX  有坏法没被抓'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    reg = load()
    fails, warns = check(reg)
    # 🔴 [#103] 三态各自打印、各自计数 —— 「待退役」压进「在役」的话,
    #    「这条锚什么时候该消失」这件事就没有任何输出在跟踪。
    dead = [a for a in reg["anchors"] if a["retired_by"]]
    pending = [a for a in reg["anchors"] if not a["retired_by"] and a.get("retiring_in")]
    live = [a for a in reg["anchors"] if not a["retired_by"] and not a.get("retiring_in")]
    print(f"  锚 {len(reg['anchors'])} 条(在役 {len(live)} · **待退役 {len(pending)}** · 已退役 {len(dead)})"
          f" · 班次 {len(reg.get('trains', []))} 个")
    for a in pending:
        print(f"    ⏳ 待退役 {a['id']} —— 随 {a['retiring_in']} 退役,继任者 {a['successor']}")
    for w in warns:
        print(f"  WARN {w}")
    for f in fails:
        print(f"  FAIL {f}")
    if not fails:
        print("  OK  登记表与树一致")
    print("\n  ⚠️ 边界:本器只核**已登记**的锚。「仓里有没有没登记的锚」它答不了 ——"
          "\n     那需要另一条判据(机械枚举仓里的计数断言,逐条要求有 id),**尚未建**。")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
