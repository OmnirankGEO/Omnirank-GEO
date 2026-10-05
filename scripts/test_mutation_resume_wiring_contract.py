# -*- coding: utf-8 -*-
"""续跑指纹守卫的**接线**判据(工单 V4-B · Codex fix-of-fix P1-4)。

═══ 为什么要单开一份只打「调用点」的判据 ═══
门③ 的 helper 我写对了,而且 `test_mutation_resume_cache_contract.py` 5 条全绿。
但 Codex 打的不是 helper,是**接线**:

    :775  resume_done_ids(results, _current_tip(), res_path.name)   ← 没传 fp
    :867  done = resume_done_ids(results, tip, res_path.name)       ← 没传 fp

而 `fp: str | None = None` 的默认值让"忘了传"**静默**变成"不检查"。
我原来唯一的接线检查是 `assert "resume_done_ids(" in src` —— **裸串在场**,
它证明的是"这个名字出现过",不是"这条守卫真的咬得到"。
**这就是「验标记 ≠ 验接线」长在我自己仪器上的形态。**

所以这份判据的分母是**调用点**,不是函数:
  · 每一处真实调用都必须传 per-id 指纹映射(AST,不是裸串);
  · 签名里**不许再有默认值**(有默认值 = 给"忘了传"留一条静默绕过的路);
  · 行为面复现 Codex 的反例:同 tip、错 fp 的旧记录必须被拒。

跑法::

    python -m pytest scripts/test_mutation_resume_wiring_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"

_spec = importlib.util.spec_from_file_location("resume_wiring_runner", RUNNER)
_R = importlib.util.module_from_spec(_spec)
sys.modules["resume_wiring_runner"] = _R
sys.path.insert(0, str(RUNNER.parent))
_spec.loader.exec_module(_R)

# 🔴 [V7-B] 夹具改用生产格式记录(带 run_meta),否则这一族会因**别的门**变红,
#    指纹那条规则被删掉它们照样红 —— 区分力就没了。
from mutation_criteria_fixtures import mfp, ok_meta, rec, tbi  # noqa: E402

SRC = RUNNER.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _real_calls() -> list[ast.Call]:
    """`resume_done_ids` 的**真实调用**(排除定义本身)。"""
    return [n for n in ast.walk(TREE)
            if isinstance(n, ast.Call)
            and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
            == "resume_done_ids"]


def test_wiring_01_there_really_are_call_sites_to_guard():
    """分母不能是 0:一个真实调用点都没扫到,下面几条就全是空转。"""
    calls = _real_calls()
    assert len(calls) >= 2, (
        f"只扫到 {len(calls)} 处 resume_done_ids 调用 —— 探针写废了,或入口被改名")


def test_wiring_02_every_call_site_passes_the_per_id_fingerprint_map():
    """🔴 正样本 —— 点名规则:**每一处真实调用**都必须传 per-id 指纹映射。

    这就是 Codex 打的那一枪:helper 有牙,两个入口(:775 / :867)一次都没传。
    用 AST 查**实参**,不用裸串查名字在不在 —— 名字在场不代表牙咬得到。
    """
    naked = []
    for call in _real_calls():
        has_kw = any(kw.arg == "fp_by_id" for kw in call.keywords)
        # 位置参数形态:results, tip, res_name, fp_by_id ⇒ 至少 4 个
        has_pos = len(call.args) >= 4
        if not (has_kw or has_pos):
            naked.append(call.lineno)
    assert not naked, (
        f"第 {naked} 行的 resume_done_ids **没传指纹映射** —— "
        "守卫在那一处是哑的(helper 有牙,接线没接)")


def test_wiring_03_the_guard_has_no_default_that_silently_disables_it():
    """签名里不许再有默认值 —— 有默认值 = 给「忘了传」留一条静默绕过的路。

    (这句话是我做 P2-5 时自己写下的先例,然后在这道门上违反了它。)
    """
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "resume_done_ids")
    names = [a.arg for a in fn.args.args]
    assert "fp_by_id" in names, "指纹映射参数没了"
    # 位置参数的默认值从右往左对齐
    defaulted = set(names[len(names) - len(fn.args.defaults):]) if fn.args.defaults else set()
    assert "fp_by_id" not in defaulted, (
        "fp_by_id 又有默认值了 —— 忘了传会变成静默不检查,而不是当场炸")
    kwonly_default = {a.arg for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d}
    assert "fp_by_id" not in kwonly_default, "fp_by_id 作为 kw-only 又带了默认值"


def test_wiring_04_codex_counterexample_same_tip_wrong_fp_is_refused():
    """🔴 Codex 的反例,原样复现:**同 tip、错 fp** 的旧记录不许算「已跑过」。

    修之前(不传 fp)它返回 `{'MUT-EXTE2-01'}` —— 被当成跑过、跳过。
    """
    tip = "a" * 40
    recs = [{"id": "MUT-A", "tip": tip, "criteria_fp": "deadbeefdeadbeef",
             "run_meta": ok_meta()}]
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids(recs, tip, "x.json", {"MUT-A": "0123456789abcdef"},
                           targets_by_id=tbi("MUT-A"))
    msg = str(exc.value)
    assert "判据指纹" in msg and "MUT-A" in msg


def test_wiring_05_per_id_not_one_global_fingerprint():
    """指纹必须**按发**比:27 发分属 9 个作用域,单一指纹要么全拒要么全放。

    构造:A 的指纹对、B 的指纹错 ⇒ 必须只因 B 被拒(且报文点名 B)。
    """
    tip = "a" * 40
    recs = [{"id": "MUT-A", "tip": tip, "criteria_fp": "aaaaaaaaaaaaaaaa",
             "run_meta": ok_meta()},
            {"id": "MUT-B", "tip": tip, "criteria_fp": "bbbbbbbbbbbbbbbb",
             "run_meta": ok_meta()}]
    fp_map = {"MUT-A": "aaaaaaaaaaaaaaaa", "MUT-B": "cccccccccccccccc"}
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids(recs, tip, "x.json", fp_map,
                           targets_by_id=tbi("MUT-A", "MUT-B"))
    msg = str(exc.value)
    assert "MUT-B" in msg, "没点名真正过期的那一发"
    assert "MUT-A" not in msg, "把指纹正确的那一发也算进去了 —— 那是单一指纹的行为"


def test_wiring_06_all_fingerprints_matching_passes():
    """阴性对照:全对时必须放行,否则上面几条红证明不了是「指纹不符」造成的。"""
    tip = "a" * 40
    recs = [rec("MUT-A", tip=tip, fp="aaaaaaaaaaaaaaaa", verdict=None)]
    assert _R.resume_done_ids(recs, tip, "x.json",
                              {"MUT-A": "aaaaaaaaaaaaaaaa"},
                              targets_by_id=tbi("MUT-A"),
                              mut_fp_by_id=mfp("MUT-A")) == {"MUT-A"}


def test_wiring_07_a_record_outside_this_rounds_roster_is_refused():
    """产物里出现本轮清单外的发 ⇒ 它的作用域指纹算不出来,不许当已跑过。"""
    tip = "a" * 40
    with pytest.raises(SystemExit):
        _R.resume_done_ids([{"id": "MUT-ZZ", "tip": tip, "criteria_fp": "x",
                             "run_meta": ok_meta()}],
                           tip, "x.json", {"MUT-A": "aaaaaaaaaaaaaaaa"},
                           targets_by_id=tbi("MUT-ZZ"))


def test_wiring_08_the_two_entries_build_the_map_from_the_real_roster():
    """两个入口的映射必须**从本轮清单机械生成**,不许手写常量表。

    手写映射 = 又一张会漂的清单;它必须来自 `load_v2()` / `muts` 与 `QUICK_SCOPE`。
    """
    for call in _real_calls():
        seg = ast.get_source_segment(SRC, call) or ""
        assert "criteria_fp_of(QUICK_SCOPE[" in seg, (
            f"第 {call.lineno} 行的指纹映射不是从 QUICK_SCOPE 机械生成的:{seg[:120]}")
        assert ("load_v2()" in seg) or ("muts" in seg), (
            f"第 {call.lineno} 行的映射分母不是本轮清单:{seg[:120]}")
