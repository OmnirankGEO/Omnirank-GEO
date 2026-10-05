# -*- coding: utf-8 -*-
"""钝杀分类的合同判据(V9-B · Codex fof7 P2-1 + P2-2)。

两条缺陷,同一个根:**「钝」这条轴没有自己的终态,也没有人按数重算它**。

P2-1 —— 分类只是「跑那一刻算出来的标签」。缓存里把 `KILLED_BLUNT` 改成
        `KILLED`(或反过来),没有任何一道闸看一眼;而重算需要的两个数
        (这一侧的基线绿 + 这一侧的红数)就写在同一条记录里。
        「信标签」纯粹是没去算,不是算不了。

P2-2 —— 全分母那一轮若判钝杀,`_verdict` 返 `KILLED_BLUNT`,于是
        `if v == "KILLED"` 整段被跳过:
          ① KBWD 的正当性(新红须来自 QUICK 之外)对钝杀从不检查;
          ② 记录顶着快集那一族的终态落盘,而它 quick 侧红是空的 ⇒
             quick 语义分支按 `KILL_VERDICTS` 要求它有红,
             **把一条合法的宽分母钝杀误判成「杀却没红」**。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_blunt_classification_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE / "mutation_runner_extsel_v2_2026_08_27.py"
sys.path.insert(0, str(HERE))


def _load():
    spec = importlib.util.spec_from_file_location("_v2blunt", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_v2blunt"] = mod
    spec.loader.exec_module(mod)
    return mod


_R = _load()
SRC = RUNNER.read_text(encoding="utf-8-sig")
TGT = "tests/pkg_a"
# 🔴 full 侧一律用**真包**:带 full_* 的记录会去算 `full_criteria_fp`,
#    合成包名会撞树锁「判据目标不存在」停机 —— 那是夹具的错,不是被测代码红了。
FULLT = tuple(_R.FULL[:1])
FULLFP = _R.criteria_fp_of(FULLT)
MID = "MUT-A"
FPOK = "1" * 64


def _rec(*, verdict, red, base, blunt=None, collected=None, **extra):
    """一条**结构上完全干净**的记录 —— 只有分类这一轴可能不对。

    别的轴(类型 / 加法 / 绿数 / 红集元素 / fp)全部造成合法值,
    否则这一族的红会被别的规则先报掉,判别力就说不清了。
    """
    collected = base if collected is None else collected
    meta = {TGT: {"collected": collected, "passed": collected - red,
                  "red": red, "skipped": 0}}
    r = {"id": MID, "tip": "a" * 40, "criteria_fp": "cfp", "verdict": verdict,
         "mutation_fp": FPOK, "mutation_fp_version": _R.FP_VERSION,
         "run_meta": meta,
         "quick_red": [f"m::t{i}" for i in range(red)],
         "quick_green": collected - red,
         "scope_green": collected - red, "scope_base_green": base}
    if blunt is not None:
        r["blunt"] = blunt
    r.update(extra)
    return r


def _why(rec, **kw):
    return _R.validate_record_run_meta(rec, (TGT,), mut_fp_by_id={MID: FPOK},
                                       **kw)


# ══ ① is_blunt:唯一一处判据,含边界与「算不出」 ═══════════════════════════
@pytest.mark.parametrize("red,base,want", [
    (0, 100, False), (49, 100, False), (50, 100, True), (51, 100, True),
    (1, 2, True), (1, 3, False), (0, 0, False),
])
def test_bl_01_is_blunt_at_the_boundary(red, base, want):
    """阈值是 `>=`:恰好一半就算钝。边界写死在判据里,免得有人「顺手」改成 >。"""
    assert _R.is_blunt(red, base) is want


@pytest.mark.parametrize("base", [None, "100", 100.0, True, -1])
def test_bl_02_an_uncomputable_baseline_is_not_not_blunt(base):
    """🔴 「算不出」必须与「不钝」分开 —— 合成一格就是又一处「坏型就不比」。"""
    assert _R.is_blunt(3, base) is None


# ══ ② P2-1:按数重算,不信标签 ═══════════════════════════════════════════
def test_bl_03_a_blunt_kill_labelled_plain_killed_is_refused():
    """🔴 正样本:红 60 / 基线绿 100 = 0.6 ≥ 阈,标签却是 KILLED。"""
    why = _why(_rec(verdict="KILLED", red=60, base=100))
    assert why and "重算应是 KILLED_BLUNT" in why, why


def test_bl_04_a_plain_kill_labelled_blunt_is_refused():
    """🔴 反方向同样要抓:钝杀单独成栏,乱贴也会把一发送进错的栏。"""
    why = _why(_rec(verdict="KILLED_BLUNT", red=3, base=100, blunt=True))
    assert why and "重算应是 KILLED" in why, why


def test_bl_05_the_blunt_flag_must_agree_with_the_recomputation():
    """标志位与终态**同源**:终态对而标志位说反话,一样是编的。"""
    why = _why(_rec(verdict="KILLED_BLUNT", red=60, base=100, blunt=False))
    assert why and "与重算的钝杀结论" in why, why


@pytest.mark.parametrize("base", [None, "100", 100.0])
def test_bl_06_a_missing_or_malformed_baseline_is_refused(base):
    """fail-closed:重算不出来 ⇒ 拒。不许「算不出就当它对」。"""
    r = _rec(verdict="KILLED", red=3, base=100)
    if base is None:
        r.pop("scope_base_green")
    else:
        r["scope_base_green"] = base
    why = _why(r)
    assert why and "钝杀分类无从重算" in why, why


@pytest.mark.parametrize("verdict,red,blunt", [
    ("KILLED", 3, None), ("KILLED", 3, False), ("KILLED_BLUNT", 60, True)])
def test_bl_07_a_consistent_record_is_accepted(verdict, red, blunt):
    """阴性对照 —— 过严也是坏:重算一致就该放行,否则上面几条红说明不了问题。"""
    assert _why(_rec(verdict=verdict, red=red, base=100, blunt=blunt)) is None


def test_bl_08_both_entry_points_refuse_a_mislabelled_record():
    """🔴 两个入口都要守:只守一个等于半守(续跑那条路会把它当已跑过)。"""
    bad = _rec(verdict="KILLED", red=60, base=100)
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids([bad], "a" * 40, "x.json", {MID: "cfp"},
                           targets_by_id={MID: (TGT,)},
                           mut_fp_by_id={MID: FPOK})
    assert "run_meta 不可信" in str(exc.value)
    assert _R.hard_gate_rc([bad], "自证", expected={MID},
                           targets_by_id={MID: (TGT,)},
                           mut_fp_by_id={MID: FPOK}) != 0


# ══ ③ P2-2:全分母钝杀有自己的终态 ═══════════════════════════════════════
def _kbwd(verdict, *, full_red, full_base):
    """快集存活形状 + full 侧带杀证 —— 「更宽分母下被杀」的真实形状。"""
    fmeta = {FULLT[0]: {"collected": full_base, "passed": full_base - full_red,
                        "red": full_red, "skipped": 0}}
    return _rec(verdict=verdict, red=0, base=100,
                full_run_meta=fmeta, full_criteria_fp=FULLFP,
                full_red=[f"f::t{i}" for i in range(full_red)],
                full_green=full_base - full_red,
                full_scope_green=full_base - full_red,
                full_scope_base_green=full_base,
                full_blunt=(full_red * 2 >= full_base) or None)


def test_bl_09_the_old_shape_a_blunt_full_kill_wearing_the_quick_family_verdict():
    """🔴 红臂(修前生产真会写出来的形状):全分母钝杀落 `KILLED_BLUNT`。

    它 quick 侧红是空的(本来就是快集存活才轮到全分母复核),
    而 `KILLED_BLUNT ∈ KILL_VERDICTS` ⇒ quick 语义分支要求它有红。
    修前这条记录**会被自己的语义闸误红**,而它描述的事情完全合法。
    """
    why = _why(_kbwd("KILLED_BLUNT", full_red=60, full_base=100),
               full_targets=FULLT)
    assert why and "quick_red" in why, why


def test_bl_10_the_new_terminal_accepts_a_legitimate_blunt_wide_kill():
    """🔴 绿臂:同一件事换上自己的终态 ⇒ 必须被收下。

    这一对(红臂 / 绿臂)才说明修的是「这类事实没有可表达的终态」,
    而不是「把闸放松了」。
    """
    r = _kbwd("KILLED_BY_WIDER_DENOMINATOR_BLUNT", full_red=60, full_base=100)
    assert _why(r, full_targets=FULLT) is None


def test_bl_11_the_wide_kill_family_is_recomputed_too():
    """full 侧同样按数重算:钝的事实配不钝的终态 ⇒ 拒。"""
    r = _kbwd("KILLED_BY_WIDER_DENOMINATOR", full_red=60, full_base=100)
    why = _why(r, full_targets=FULLT)
    assert why and "KILLED_BY_WIDER_DENOMINATOR_BLUNT" in why, why
    r2 = _kbwd("KILLED_BY_WIDER_DENOMINATOR", full_red=3, full_base=100)
    assert _why(r2, full_targets=FULLT) is None


# ══ ④ 接线与登记:新终态不许只存在于语义层 ═══════════════════════════════
def test_bl_12_the_full_arm_branches_on_the_whole_kill_family():
    """🔴 接线锁:全分母臂里那个 `if` 必须比**家族集合**,不是比字符串。

    修前是 `if v == "KILLED"` —— 钝杀直接绕过 KBWD 正当性检查。
    这里锁的是「它比的是 KILL_VERDICTS 这个名字」,不是「源码里出现过这个词」:
    出现在注释、出现在别的表达式里都不算(裸串锁只证谓词存在,不证它有牙)。
    """
    fn = next(n for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.FunctionDef)
              and n.name == "run_full_survivors")
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.If)
              and isinstance(n.test, ast.Compare)
              and len(n.test.ops) == 1 and isinstance(n.test.ops[0], ast.In)
              and isinstance(n.test.comparators[0], ast.Name)
              and n.test.comparators[0].id == "KILL_VERDICTS"]
    assert guards, "全分母臂里没有一处按 KILL_VERDICTS 分支 —— 钝杀会绕过改判"
    calls = [n for n in ast.walk(guards[0]) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_kbwd_outside_quick"]
    assert calls, "那个分支里没有调 _kbwd_outside_quick —— 正当性检查没接上"


@pytest.mark.parametrize("v", sorted(_R.BLUNT_VERDICTS | _R.KBWD_VERDICTS))
def test_bl_13_every_new_terminal_is_fully_registered(v):
    """🔴 分母机械枚举:新终态必须在**每一张**表里都有位置。

    只加进语义层的话,它会在终账栏 / 已解决集 / 渲染名里各漏一次,
    而每一次漏掉都不会有判据变红 —— 那一发只是静静地从那一栏里消失。
    """
    assert v in _R.RESOLVED_VERDICTS or v in _R.UNRESOLVED_VERDICTS
    assert _R.ledger_of(v)                       # 未登记会 SystemExit
    import gate9_report
    assert v in gate9_report._VERDICT_CN, f"{v} 没有渲染名"


def test_bl_14_no_module_hand_writes_the_killed_family():
    """🔴 单点锁:除了家族定义本身,任何非判据文件不许再手写一份「算杀」清单。

    抓的毒:`r["verdict"] in ("KILLED", "KILLED_BY_WIDER_DENOMINATOR")` ——
    新增一种杀之后它不会红,只会把那一发从「杀」那一栏里漏掉。
    """
    #: 豁免**逐条写理由**,并且每条豁免都要有一条判据把副本钉成相等 ——
    #: 无理由的豁免名单就是垃圾桶,下一个人往里塞东西不会有人拦。
    EXEMPT = {
        "mutation_criteria_fixtures.py":
            "夹具刻意不 import runner(那边 import 期会加载清单、打台账);"
            "副本由 test_bl_15 钉成相等",
    }
    offenders = []
    for f in sorted(HERE.glob("*.py")):
        if f.name.startswith("test_") or f.name == RUNNER.name or f.name in EXEMPT:
            continue
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8-sig"))):
            if not isinstance(n, (ast.Tuple, ast.List, ast.Set)):
                continue
            vals = {e.value for e in n.elts if isinstance(e, ast.Constant)}
            if "KILLED" in vals and any(isinstance(x, str)
                                        and x.startswith("KILLED_BY")
                                        for x in vals):
                offenders.append(f"{f.name}:{n.lineno}")
    assert not offenders, ("这些地方自己抄了一份「算杀」清单:" + str(offenders)
                           + " —— 借 KILLED_FAMILY,别各抄一份")


def test_bl_15_the_fixture_mirror_equals_the_single_point():
    """🔴 受钉副本:夹具那份「算杀」清单必须**逐字等于** runner 的家族。

    豁免一份副本的唯一代价就是这条判据。没有它,`test_bl_14` 的豁免名单
    就变成「允许漂移」的许可证。
    """
    import mutation_criteria_fixtures as _F
    assert _F.KILL_VERDICTS_MIRROR == _R.KILLED_FAMILY, (
        f"夹具副本 {sorted(_F.KILL_VERDICTS_MIRROR)} != "
        f"单点 {sorted(_R.KILLED_FAMILY)}")


def test_bl_16_the_kill_family_is_pinned_to_the_ledger_not_to_a_fixture():
    """🔴 家族的分母来自**生产表**,不是夹具副本。

    自打毒实测(R10「算杀家族缩回不含 KBWD_BLUNT」):21 发毒里只有这一发
    的响应者是 `test_bl_15` —— 而 bl_15 比的是**夹具副本**。
    那是个替身信号:它响是因为副本对不上,不是因为任何用 `KILLED_FAMILY`
    的地方出了错。于是「家族和夹具一起改小」这条路上没有任何东西会红。

    真正的 SSOT 是终账表:`ledger_of` 把哪些裁定判给「杀 · 已结」,
    哪些就是杀。两张生产表互钉,谁也别想单方面缩水。
    """
    from_ledger = {v for v, lab in _R._VERDICT_TO_LEDGER.items()
                   if lab == _R.LEDGER_KILLED}
    assert _R.KILLED_FAMILY == from_ledger, (
        f"算杀家族 {sorted(_R.KILLED_FAMILY)} != 终账判为杀的 {sorted(from_ledger)}")
    # 消费方也要真的拿到新终态(借用而不是自己抄的行为面证据)
    import gate9_replay_from_list as _RP
    assert _RP.killed_verdicts() == _R.KILLED_FAMILY
