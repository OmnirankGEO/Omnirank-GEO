# -*- coding: utf-8 -*-
"""R2 · 让 `anchors_registry.json` + `check_anchors.py` **真的在跑**。

🔴 为什么这个文件必须存在:
   `scripts/check_anchors.py` 自带 `--selftest`,但**自带自证的脚本没人调用时,
   它跟不存在没区别** —— 同仓 `DOC_ONLY_TABLES` 就是这么躺着的:
   注释里承诺「配判据」,判据从没写,于是违规者无声躺进来,
   而那张表的**存在本身**让人停止检查。

   本文件就是那条「谁在跑它」的答案。它住在已登记的判据包里 ⇒ 进默认收集范围。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
REG = ROOT / "scripts" / "anchors_registry.json"
CHK = ROOT / "scripts" / "check_anchors.py"


def _mod():
    spec = importlib.util.spec_from_file_location("_chk_anchors", CHK)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_chk_anchors"] = m
    spec.loader.exec_module(m)
    return m


# ══ ① 登记表与树一致 ═════════════════════════════════════════════════
def test_r2_01_registry_agrees_with_the_tree():
    m = _mod()
    fails, _warns = m.check(m.load())
    assert not fails, "锚登记表与树不一致:\n    " + "\n    ".join(fails)


# ══ ② 核对器**有牙** —— 六种坏法各造一次,每种必须被抓 ════════════
def test_r2_02_the_checker_catches_every_known_breakage():
    """🔴 这条是 R2 的命门。

    没有它,`check_anchors.py` 可能是一把**永远绿**的尺子:
    登记表怎么写它都说 OK,而它的存在会让人以为锚这件事有人管了。
    (今天已经为 `DOC_ONLY_TABLES` 挖出过一次完全同形的病。)

    `--selftest` 内部含**阴性对照**(未改动的 registry 必须无 FAIL)——
    没有它,那六个「抓到了」可能只是登记表自己在红。
    """
    m = _mod()
    rc = m.selftest()
    assert rc == 0, "check_anchors --selftest 未全过:有坏法没被抓到,或阴性对照不干净"


# ══ ③ 退役锚必须有活着的继任者(链不许断)═════════════════════════
def test_r2_03_every_retired_anchor_has_a_live_successor():
    reg = json.loads(REG.read_text(encoding="utf-8"))
    by_id = {a["id"]: a for a in reg["anchors"]}
    retired = [a for a in reg["anchors"] if a["retired_by"]]
    assert retired, ("registry 里一条退役锚都没有 ⇒ 本条恒真。"
                     "退役链是这张表存在的主要理由;若真的一条都没有,"
                     "说明样板还没录进去。")
    for a in retired:
        assert a["successor"], f"[{a['id']}] 退役却无继任者 —— 退役 = 换人守,不是不守了"
        for s in a["successor"]:
            assert s in by_id, f"[{a['id']}] 继任者 {s} 不在表里"
            assert not by_id[s]["retired_by"], f"[{a['id']}] 的继任者 {s} 也退役了 —— 断链"


# ══ ④ 🔴 边界必须写在脚本里(防止它被当成全覆盖)═════════════════
def test_r2_04_the_checker_states_what_it_does_not_cover():
    """本器的分母 = **已登记**的锚。「仓里有没有没登记的锚」它答不了。

    这条锁守的是**那句话别被删掉**。删了它,下一个人会以为锚这件事已全覆盖 ——
    而真相是「没登记的那条永远在分母之外」,正是 `DOC_ONLY_TABLES` 的处境。
    """
    src = CHK.read_text(encoding="utf-8-sig")
    assert "没登记的锚" in src and "尚未建" in src, (
        "`check_anchors.py` 里那段「它不管什么」的边界说明不见了。"
        "一个不写边界的核对器,会被当成全覆盖 —— 而它的分母只有已登记的那些。")


# ══ ⑤ 🔴 Review 2026-09-05 毒存活后补:expected 必须真的被比对 ═══════
def test_r2_05_a_live_count_anchor_expected_is_compared_against_the_tree():
    """Review 把一条 **set** 锚的 `expected` 改成 999 ⇒ 四条判据全绿。

    实测两解**都成立**,各带一个我没写的洞:
    ① `set` 型的 `expected` **不在比对轴上** —— 但当时没有检查要求它必须是 `null`,
       所以留个数在那儿读起来像被核着,其实是**死字段**;
    ② 比对 `expected` 的代码**有**,但真表里在役 count 锚 **= 0 条**
       ⇒ 那条检查**分母为 0、恒真**。**代码在 ≠ 它在守东西。**

    本条用**合成 registry**打真树:造一条在役 count 锚,指向本仓真实文件里的真实常量。
      · expected 与树一致 ⇒ 必须无 FAIL(阴性对照:证明不是「怎么写都红」)
      · expected 改错     ⇒ 必须 FAIL 且点名        ← Review 要的第一发
      · 树上目标不见了     ⇒ 必须 FAIL 且点名        ← Review 要的第二发
    """
    import copy
    m = _mod()
    reg = m.load()

    def with_count(expected, pattern="STALE_AFTER_TRAINS = 2",
                   file="scripts/check_anchors.py"):
        r = copy.deepcopy(reg)
        r["anchors"].append({
            "id": "r2-05-synthetic-live-count", "introduced_train": r["trains"][-1]["label"],
            "kind": "count", "file": file, "pattern": pattern, "expected": expected,
            "retired_by": None, "successor": [],
            "last_reviewed_train": r["trains"][-1]["label"], "why": "判据合成,不落盘",
        })
        return r

    # 阴性对照 —— 没有它,下面两发红可能只是「怎么写都红」
    fails, _ = m.check(with_count(2))
    assert not fails, f"expected 与树一致却报 FAIL:{fails}"

    # 发一:expected 改错 ⇒ 必须红且点名
    fails, _ = m.check(with_count(99))
    assert any("r2-05-synthetic-live-count" in f and "不符" in f for f in fails), (
        f"改错 expected 没被抓到(或没点名)—— 那 registry 里的数字就是装饰。实得:{fails}")

    # 发二:树上目标不见了 ⇒ 必须红且点名
    fails, _ = m.check(with_count(2, pattern="STALE_AFTER_TRAINS_THIS_NEVER_EXISTS = 2"))
    assert any("r2-05-synthetic-live-count" in f and "在役却找不到" in f for f in fails), (
        f"树上目标消失没被抓到 —— 锚可以被静默删掉。实得:{fails}")


def test_r2_06_set_anchors_must_not_carry_a_meaningless_expected():
    """真表侧:`set` 型锚的 `expected` 必须是 `null`。

    它不参与比对;留个数在那儿的唯一效果是**让人以为它被核着** ——
    这跟同仓「只声明不消费的表让人以为终态被它管着」是同一个病。
    """
    reg = json.loads(REG.read_text(encoding="utf-8"))
    bad = [a["id"] for a in reg["anchors"]
           if a["kind"] == "set" and a["expected"] is not None]
    assert not bad, f"这些 set 型锚带了无意义的 expected:{bad}"
    nocount = [a["id"] for a in reg["anchors"]
               if a["kind"] == "count" and a["expected"] is None]
    assert not nocount, f"这些 count 型锚没有 expected:{nocount}"
