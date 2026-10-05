#!/usr/bin/env python
"""存活发的**全分母复核** —— 每发存活补跑一次全七包。

为什么必须做:每发变异只跑了**本族**判据包,而「存活」是要往下游传的结论。
本仓记过:**分母比结论小,结论就是假的**。
所以在说一发"没有任何判据在守"之前,得让它面对全部判据。

🔴 输出口径:全分母存活 = **没有任何判据在看这一维**,它**不等于「判据洞」**。
   是洞还是冗余,取决于闭环第一问 —— 有没有**可观测差**。
   两者观测相同,必须另行定性;把这里当成洞会直接派错补判据单。

判红同一套规矩(与主 runner 一致):
  · 红集只认 pytest 短摘要里的节点(`-ra`,failed **与** error 都报);
  · **绿数守恒**是主防线:红 0 但绿数 ≠ 基线 ⇒ 有测试没跑,不算存活;
  · 备份落盘 + 原子还原 + sha 逐字节核;禁 git checkout;串行。
"""
from __future__ import annotations

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
STATE = ROOT / ".tiprun"

_spec = importlib.util.spec_from_file_location(
    "extsel", ROOT / "scripts" / "mutation_runner_extsel_2026_08_26.py")
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)

OUT = STATE / "extsel_survivor_full.json"

#: [E1-3③ = Codex 二审 §8] **冗余裁定表**。
#:
#: 🔴 「全分母下存活」只说明没有任何判据在看这一维;是**洞**还是**冗余**,
#:    取决于有没有**可观测差**。裁定写在台账里而机器产物仍写 SURVIVES_FULL,
#:    下游只读机器账就会照着开补判据单 —— 机器账也要归位。
#: 🔴 但**不许手改产物**:要落 REDUNDANT 就必须在这里写下**理由 + 可执行证据指针**。
#:    没有条目的存活一律落 ``SURVIVES_FULL_UNADJUDICATED`` 并让 runner 非零退出 ——
#:    「还没定性」和「已定性为冗余」必须长得不一样。
REDUNDANCY_ADJUDICATIONS: dict[str, dict[str, str]] = {
    "MUT-EXTC-04": {
        "why": "逐引擎封顶落地后,外层 min(succeeded, per_engine*len(billable)) "
               "在数学上恒等于 succeeded —— 摘掉它没有可观测差。",
        "evidence": "tests/defgeo_woc_closure_2026_08_25/test_c2_settlement_denominator.py"
                    "::test_c2_13 的 docstring 记有判别力实测:单摘外层绿 / 单摘逐引擎绿"
                    "(由 test_c2_15 杀)/ 两处同时摘则红。",
        "adjudicated_by": "工单C(实测)+ Review 裁定 2026-08-26",
    },
    "MUT-EXTC-05": {
        "why": "asyncio.wait_for 超时时自己就会取消被 await 的 gather 并等取消完成,"
               "``t.cancel()`` 是冗余的第二道 —— 走到那一行时任务已是 cancelled。",
        "evidence": "工单C 同形状独立小程序实测:cancelled()=[True,False]、"
                    "t.cancel()=[False,False];性质由 "
                    "tests/defgeo_woc_closure_2026_08_25/test_c2_total_timeout_degrade.py"
                    "::test_c2_24 钉住(docstring 如实写明它杀不动这一发)。",
        "adjudicated_by": "工单C(实测)+ Review 裁定 2026-08-26",
    },
}


#: **语义 no-op 阴性对照**。这些发在数学上不改变任何行为 —— 它们红几条,
#: 就是这台仪器这一轮的**假阳性数**。
#:
#: 🔴 进这张表的门槛:必须写得出**可自己复核的证明**,不是"看起来没影响"。
#:    不许拿"跑出来是绿的"当理由 —— 那是拿观测当前提。
SEMANTIC_NOOP_CONTROLS: dict[str, str] = {
    "MUT-EXTC-04": (
        "外层 min 恒等于 succeeded,证明可自行复核:"
        "diagnosis_sample_contract 里 `succeeded += min(per_engine, ...)` "
        "对 billable 逐个累加,共 len(billable) 项、每项 ≤ per_engine ⇒ "
        "succeeded ≤ per_engine*len(billable) ⇒ "
        "min(succeeded, per_engine*len(billable)) ≡ succeeded。"
        "摘掉它什么都不改。定向复跑已坐实:全分母下报红的那 4 条判据,"
        "在带/不带这发变异时**单跑全部 passed**。"
    ),
}

def run_full() -> tuple[set[str], int]:
    red: set[str] = set()
    green = 0
    for target, dsn, extra in _m.FULL_DENOMINATOR:
        r, g, _rc = _m._one_target(target, dsn, extra)
        red |= r
        green += g
    return red, green


def main() -> int:
    # 🔴 [Review 机制令 ①] 树级排他锁 —— 这个脚本同样就地改源文件。
    with _m.tree_lock("extsel_survivor_full_denominator"):
        return _main_locked()


def _main_locked() -> int:
    _m._assert_disk_headroom()
    _m._assert_no_concurrent_runner()

    # 🔴 [Review 机制令 ②] 全分母的判据指纹。全七包里**任何**一个判据文件变了,
    #    之前的全分母结论就不再成立 —— 缓存必须失效。
    fp, meta = _m.criteria_fingerprint([t for t, _d, _e in _m.FULL_DENOMINATOR])
    print(f"全分母判据指纹 {fp} · {meta['n_files']} 个判据文件")

    results = json.loads((STATE / "extsel_results.json").read_text(encoding="utf-8"))
    survivors = [r["id"] for r in results if r.get("killed") is False]
    muts = {m["id"]: m for m in _m.load_all()}
    print(f"存活发 {len(survivors)}:{survivors}")

    done: list[dict] = []
    if OUT.exists():
        done = json.loads(OUT.read_text(encoding="utf-8"))
    fresh = [d for d in done if _m.cache_is_fresh(d, fp)]
    if len(fresh) != len(done):
        print(f"♻️  判据已变,{len(done) - len(fresh)} 条全分母缓存作废、必须重跑")
    done = fresh
    seen = {d["id"] for d in done}

    base_red, base_green = run_full()
    print(f"全分母基线:绿 {base_green} / 红 {len(base_red)} {sorted(base_red) if base_red else ''}")
    if base_red:
        raise SystemExit("🔴 全分母基线不是全绿 —— 后面全部作废")

    for mid in survivors:
        if mid in seen:
            print(f"⏭  {mid} 已有全分母结果")
            continue
        mut = muts[mid]
        path = ROOT / mut["file"]
        # 🔴 [Review 令 ④③] 字节路径:decode 出来的 str 与磁盘字节是两种视图。
        original = path.read_bytes()
        needle, _c = _m._fit_newlines_bytes(original, mut["from"])
        repl = _m._fit_repl_bytes(original, needle, mut["to"])
        hits = original.count(needle)
        if hits != 1:
            raise SystemExit(f"🔴 {mid} 锚点命中 {hits} 次(要求 1)—— 停下报 Review")
        mutated = original.replace(needle, repl, 1)
        if mutated == original:
            raise SystemExit(f"🔴 {mid} 替换后无变化 —— 这是 no-op 不是变异,停")

        backup = path.with_suffix(path.suffix + ".mutbak")
        backup.write_bytes(original)
        path.write_bytes(mutated)
        # 🔴 [Review 令 ④①] 在盘实证:没有这三字段的「存活」一律记未验。
        #    上一轮 20 发 SURVIVES_FULL 正是**缺这个**才被判为「未验记成坐实」。
        proof = _m._on_disk_proof(path, anchor_hits=hits, before=original,
                                  needle=needle, repl=repl)
        if not proof["ok"]:
            _m._restore(path, original)
            backup.unlink(missing_ok=True)
            raise SystemExit(f"🔴 {mid} 变异没有真的落盘 —— 停。实证={proof}")
        try:
            red, green = run_full()
        finally:
            _m._restore(path, original)
            backup.unlink(missing_ok=True)

        new_red = sorted(red - base_red)
        rec = {"id": mid, "family": mut["family"], "title": mut.get("title", ""),
               "full_red": new_red, "full_green": green, "base_green": base_green,
               "on_disk_proof": proof, "criteria_fp": fp}
        control_proof = SEMANTIC_NOOP_CONTROLS.get(mid)
        if control_proof is not None:
            # 阴性对照:它的红**一定**是仪器假阳性,不是杀。
            rec["verdict"] = ("NOOP_CONTROL_CLEAN" if not new_red
                              else "INSTRUMENT_FALSE_POSITIVE")
            rec["noop_proof"] = control_proof
            rec["instrument_false_positives"] = new_red
            if new_red:
                print(f"🟠 {mid} 是**语义 no-op 阴性对照**,却红了 {len(new_red)} 条 —— "
                      f"这不是杀,是**这台仪器的假阳性**:{new_red[:6]}")
                print("    (no-op 不可能致红。这个数就是本轮全分母的假阳性率读数。)")
            else:
                print(f"🟢 {mid} 阴性对照干净:全分母零红 —— 本轮仪器无假阳性读数")
        elif not new_red and green != base_green:
            rec["verdict"] = "GREEN_NOT_CONSERVED"
            print(f"🔴 {mid} 全分母下红 0 但绿 {base_green} → {green} —— 有测试没跑,不算存活")
        elif new_red:
            rec["verdict"] = "KILLED_ONLY_BY_WIDER_DENOMINATOR"
            print(f"💀 {mid} **本族存活、全分母下被杀** 红 {len(new_red)}:{new_red[:6]}")
        else:
            # [E1-3③] 落**终态**:有裁定条目 ⇒ REDUNDANT;没有 ⇒ 未定性,非零退出。
            adj = REDUNDANCY_ADJUDICATIONS.get(mid)
            rec["verdict"] = "REDUNDANT" if adj else "SURVIVES_FULL_UNADJUDICATED"
            if adj:
                rec["adjudication"] = dict(adj)
            # 🔴 措辞要紧:「全分母下存活」**不等于**「判据洞」。它只说明
            #    没有任何判据在看这一维。是洞还是冗余,取决于闭环第一问
            #    ——**有没有可观测差**。EXTC-04/05 两发都是全分母存活,
            #    但都被独立实证为「无可观测差 ⇒ 冗余」。把这里写成「洞坐实」
            #    会让下游直接开补判据单,而那是错的。
            print(f"🟢 {mid} 全分母下仍然存活(绿 {green} = 基线)—— "
                  "没有任何判据在看这一维;是**洞**还是**冗余**,"
                  "取决于有没有可观测差,须另行定性")
        done.append(rec)
        OUT.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")

    print("═" * 72)
    fp_ctrl = [d for d in done if d.get("verdict") == "INSTRUMENT_FALSE_POSITIVE"]
    if fp_ctrl:
        n = sum(len(d.get("instrument_false_positives") or []) for d in fp_ctrl)
        print(f"🟠 阴性对照报出 {n} 条假阳性 —— 本轮全分母的红**不能按面值读**;"
              "定位类结论要另行单跑坐实。")
    real = [d for d in done if d.get("verdict") == "SURVIVES_FULL_UNADJUDICATED"]
    redundant = [d for d in done if d.get("verdict") == "REDUNDANT"]
    wider = [d for d in done if d.get("verdict") == "KILLED_ONLY_BY_WIDER_DENOMINATOR"]
    for d in redundant:
        print(f"  🟡 {d['id']} 全分母下存活,但**已裁定为冗余**(无可观测差):"
              f"{d['adjudication']['why'][:60]}")
    print(f"全分母复核完:已裁定冗余 {len(redundant)} · "
          f"**未定性** {len(real)} · 被更宽分母杀掉 {len(wider)}")
    for d in wider:
        print(f"  💀 {d['id']} —— 本族分母开小了,不是洞")
    if real:
        print("🔴 有存活发**没有冗余裁定条目** —— 它们是候选判据洞,"
              f"必须定性后才能收口:{[d['id'] for d in real]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
