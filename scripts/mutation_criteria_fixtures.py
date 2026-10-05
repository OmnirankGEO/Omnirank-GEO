# -*- coding: utf-8 -*-
"""判据夹具 —— 造**生产真会写出来的**记录。

🔴 [V7-B · 2026-08-30] 这个文件为什么存在:

   V7-B 给续跑缓存与终门加上 run_meta 校验之后,**31 条既有判据当场全红**。
   不是新门写错了,是那些夹具造的记录长这样::

       {"id": "MUT-A", "tip": TIP, "criteria_fp": "aaaa"}

   而**生产从来不写这种记录** —— 真产物每条都带 `run_meta`(27 发 33 条,实测)。
   夹具漏了生产一定会供的东西,那条判据的分母就是假的:它验的是一种
   现实中不存在的状态。(同族:夹具供了生产不会供的东西 ⇒ 夹具就是那条锁的分母。)

   把「一条合法记录长什么样」收在一处,免得四个判据文件各写一版然后各自漂。

🔴 它**只造夹具,不含任何判断**。判断全部在
   `mutation_runner_extsel_v2_2026_08_27.validate_record_run_meta` 单点 ——
   夹具里再写一份「什么算合法」等于同一谓词写两处,必有一处没人验。
"""
from __future__ import annotations

DEFAULT_TARGET = "tests/pkg_a"

#: 🔴 [V9-B] runner `KILLED_FAMILY` 的一份**受钉副本**。
#:    夹具刻意不 import runner(那边 import 期会加载抽取清单、打重锚台账),
#:    所以这里允许存在第二份 —— 代价是必须有判据把两份钉成相等:
#:    `test_mutation_blunt_classification_contract.py::test_bl_15_...`。
#:    没有那条判据,这就是标准的「同一谓词写两处,必有一处没跟上」。
#: 🔴 runner `FP_VERSION` 的**受钉副本**(夹具不 import runner,理由同下)。
#: 判据 `test_fp_14` 把它钉成相等。
FP_VERSION_FOR_FIXTURES = 2

KILL_VERDICTS_MIRROR = frozenset({
    "KILLED", "KILLED_BLUNT", "KILLED_BY_WIDER_DENOMINATOR",
    "KILLED_BY_WIDER_DENOMINATOR_BLUNT",
})


def ok_meta(targets=(DEFAULT_TARGET,), *, collected: int = 12,
            red: int = 2, skipped: int = 0) -> dict:
    """一份**加得平**的 run_meta:collected == passed + red + skipped,全 int、全非负。"""
    return {t: {"collected": collected, "passed": collected - red - skipped,
                "red": red, "skipped": skipped} for t in targets}


def rec(mid: str, *, tip=None, fp=None, verdict: str = "KILLED",
        targets=(DEFAULT_TARGET,), meta=..., **extra) -> dict:
    """一条**生产格式**的记录。

    `tip` / `fp` 传 None 表示**故意不写这个键**(有判据专考「老格式没钉尖」)。
    `meta` 传别的值可以造坏记录;不传就是合法的。
    """
    # `verdict` 传 None 与 `tip`/`fp` 同一约定:**故意不写这个键**
    # (真缓存里确实有没写裁定的旧记录,那是一种要被守住的形状,不是 None 值)。
    r: dict = {"id": mid} if verdict is None else {"id": mid, "verdict": verdict}
    if tip is not None:
        r["tip"] = tip
    if fp is not None:
        r["criteria_fp"] = fp
    r["run_meta"] = ok_meta(targets) if meta is ... else meta
    # 🔴 [V9-B] 生产记录一定带 mutation_fp(绑「这一发是什么变异」)。
    r.setdefault("mutation_fp", mfp_of(mid))
    # 🔴 [fof8 P1-1] 生产每条同时写指纹**口径版本**;夹具漏了它,
    #    那条闸的分母就是假的(它验的是一种生产不会产出的记录)。
    r.setdefault("mutation_fp_version", FP_VERSION_FOR_FIXTURES)
    r.update(extra)
    return r


#: 夹具用的定义指纹(64 位 hex,内容无所谓,形状必须对)。
def mfp_of(mid: str) -> str:
    """给合成 id 造一个稳定的 64 位指纹 —— 形状与生产一致(sha256 hex)。"""
    import hashlib
    return hashlib.sha256(("fixture::" + mid).encode("utf-8")).hexdigest()


def mfp(*mids: str) -> dict:
    """`mut_fp_by_id` 映射 —— 与 `tbi` 同形,显式注入不走隐式全局。"""
    return {m: mfp_of(m) for m in mids}


def tbi(*mids: str, targets=(DEFAULT_TARGET,)) -> dict:
    """`targets_by_id` 映射:每发应跑哪些 target。"""
    return {m: tuple(targets) for m in mids}


def sem(meta: dict, verdict: str = "KILLED", *, label: str = "quick") -> dict:
    """与 `run_meta` **精确自洽**的语义字段。

    🔴 [V8-B] 为什么又要加:V8-B 给谓词补了 verdict ↔ 红集的语义规则之后,
       只有 `run_meta` 的夹具再次落后于生产 —— 真产物每条都带
       `quick_red` / `quick_green`,且实测 27/27 满足
       `sum(run_meta.*.red) == len(quick_red)` 与 `sum(passed) == quick_green`。
       夹具漏了生产一定会供的东西,那条判据验的就是一种现实中不存在的状态。

    「杀」类按红数造等长的 nodeid 列表;「存活」类给空列表(并要求 red 全 0)。
    """
    pre = "" if label == "quick" else "full_"
    rk, gk = pre + ("quick_red" if label == "quick" else "red"),         pre + ("quick_green" if label == "quick" else "green")
    red = sum(m["red"] for m in meta.values())
    passed = sum(m["passed"] for m in meta.values())
    kill = verdict in KILL_VERDICTS_MIRROR
    out = {rk: [f"test_mod::test_r{i}" for i in range(red)] if kill else [],
           gk: passed}
    # 🔴 [V9-B] 作用域绿数两格 —— 生产每条记录都由 `_verdict` 写下它们,
    #    钝杀分类要**按这一侧的基线绿重算**,缺了就重算不出来。
    #    基线绿 == collected 不是我拍的:门① `assert_baseline_clean` 强制基线零红,
    #    所以基线那一趟每个 target 的绿数就是它的 collected。
    out[pre + "scope_green"] = passed
    out[pre + "scope_base_green"] = sum(m["collected"] for m in meta.values())
    return out
