# -*- coding: utf-8 -*-
"""V2 变异 runner 四条**假绿**路径的合同判据(Codex fix-of-fix2 P1-6 · 工单 V5-B B-1)。

四条的共同形状是本仓记过的老病:**分母悄悄变小,而「全绿」在小分母上永远成立**。
这次它们各自换了一件衣服:

  · B-1-1 `:890` —— 全分母段续跑只核 **quick** 指纹。走过全分母臂的那些记录
    带的是 `full_criteria_fp`,**没人核**。于是:全分母判据改了 ⇒ 尖没变、
    quick 指纹没变 ⇒ 旧的 `KILLED_BY_WIDER_DENOMINATOR` 原样复用 ⇒
    `surv` 为空 ⇒ 零存活早退 ⇒ rc 0。**过期的杀证冒充本轮杀证。**
  · B-1-2 `:729` —— 只核「27 条」,不核「27 个**不同的** id」。清单里一条重复
    配一条缺失时:`len(muts)==27` 过、`expected={...}` 折成 26、续跑跳过重复的
    那一条 ⇒ 26 条结果、26 个应跑、集合相等、计数相等 ⇒ rc 0。
    真正少跑的那一发**在任何一处都不出现**。
  · B-1-3 `:939` —— 重锚必红/必绿期望只在 quick 段机械核。全分母段被更宽分母
    「误杀」时不核 ⇒ **无关的红可以冒充重锚命中**,而重锚的全部正当性就是
    「只动锚一条轴,语义一字不改」。
  · B-1-4 `:971` —— `V2_ONLY` 拼错 ⇒ `muts` 空 ⇒ `expected=set()` ⇒
    `hard_gate_rc` 里 `if expected` 为假 ⇒ 完整性检查整段跳过 ⇒
    打印「0 发全部为「杀」」并返回 0。**空分母不是通过,是探针没打着。**

每条都配「点名规则的正样本」——门没有正样本就只是一句"我加了闸"。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_v2_falsegreen_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    return mod


_R = _load("v2fg_runner", RUNNER)
# 🔴 [V7-B] 夹具改用生产格式记录(带 run_meta):这一族每条必须只因自己那条
#    规则变红,否则终门的 run_meta 门会把 dup / 空分母的区分力顶掉。
from mutation_criteria_fixtures import mfp, ok_meta, rec, sem, tbi  # noqa: E402
_SRC = RUNNER.read_text(encoding="utf-8-sig")
_TREE = ast.parse(_SRC)

TIP = "a" * 40
QFP = "q" * 64
FFP = "f" * 64


def _fn(name: str) -> ast.FunctionDef:
    for n in ast.walk(_TREE):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} 不在 runner 里 —— 判据的坐标先烂了")


def _stmt_index_of_call(fn: ast.FunctionDef, callee: str) -> int:
    """`fn` 顶层语句里**第几条**含有对 `callee` 的调用(找不到返回 -1)。

    只认顶层语句序号 —— 「在早退之前」是一句关于**顺序**的话,
    而顺序只在同一层的语句列表里才有意义。
    """
    for i, stmt in enumerate(fn.body):
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call):
                f = n.func
                nm = getattr(f, "attr", None) or getattr(f, "id", None)
                if nm == callee:
                    return i
    return -1


def _stmt_index_of_assign(fn: ast.FunctionDef, target: str) -> int:
    for i, stmt in enumerate(fn.body):
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == target:
                    return i
    return -1


# ══ B-1-1 · 全分母段的裁定必须绑**当轮**全分母指纹 ═══════════════════════
def test_b11_a_full_derived_verdict_with_a_stale_full_fingerprint_is_refused():
    """🔴 正样本 —— 点名规则:走过全分母臂的记录,`full_criteria_fp` 对不上就停机。

    这一条 record 的 quick 指纹与尖**全对**(所以 `resume_done_ids` 放它过),
    坏的只有全分母指纹。旧代码里没有任何一处读它。
    """
    recs = [{"id": "MUT-A", "verdict": "KILLED_BY_WIDER_DENOMINATOR",
             "tip": TIP, "criteria_fp": QFP,
             "full_red": ["pkg::t1"], "full_criteria_fp": "0" * 64}]
    with pytest.raises(SystemExit) as exc:
        _R.assert_full_fp_current(recs, FFP, "自证.json")
    msg = str(exc.value)
    assert "全分母" in msg and "指纹" in msg


def test_b11_a_record_that_went_through_the_full_arm_without_any_fingerprint_is_refused():
    """有 `full_red` 却没绑指纹 —— 「没绑」与「绑错」处置相同,都不许复用。"""
    recs = [{"id": "MUT-A", "verdict": "SURVIVED_FULL",
             "tip": TIP, "criteria_fp": QFP, "full_red": []}]
    with pytest.raises(SystemExit):
        _R.assert_full_fp_current(recs, FFP, "自证.json")


def test_b11_quick_only_records_are_untouched():
    """只跑过快集的记录**不该**被这道门拦 —— 它们本来就还没进全分母臂。

    (分母对不齐的判据比没有判据更坏:它会把每一轮都染红,然后被当成"太严"关掉。)
    """
    recs = [{"id": "MUT-A", "verdict": "SURVIVED_QUICK",
             "tip": TIP, "criteria_fp": QFP},
            {"id": "MUT-B", "verdict": "KILLED", "tip": TIP, "criteria_fp": QFP}]
    _R.assert_full_fp_current(recs, FFP, "自证.json")        # 不抛即通过


def test_b11_matching_full_fingerprint_passes():
    recs = [{"id": "MUT-A", "verdict": "KILLED_BY_WIDER_DENOMINATOR",
             "tip": TIP, "criteria_fp": QFP,
             "full_red": ["pkg::t1"], "full_criteria_fp": FFP}]
    _R.assert_full_fp_current(recs, FFP, "自证.json")


def test_b11_the_full_arm_really_writes_at_least_one_full_prefixed_field():
    """锁住上一条门的**前提**:它靠 `full_` 前缀认出"走过全分母臂"的记录。

    前提没人守的话,全分母臂哪天改成写 `wide_red`,探针就一条都认不出来 ——
    而认不出来的记录会被当成「只跑过快集」**静默放行**。
    这不是理论:本仓的假绿几乎全是「分母悄悄变空」这一形。
    """
    fn = _fn("run_full_survivors")
    keys = {n.slice.value for n in ast.walk(fn)
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
            and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)}
    assert any(k.startswith("full_") for k in keys), (
        f"全分母臂写的键是 {sorted(keys)} —— 没有 full_ 前缀的键,"
        "assert_full_fp_current 的前缀探针就认不出任何记录")


def test_b11_the_gate_runs_before_the_zero_survivor_early_return():
    """结构锁 —— 位置就是这条门的全部意义。

    早退分支(`surv` 为空 ⇒ `hard_gate_rc` ⇒ 0)正是过期杀证被复用的出口。
    门排在 `surv` 算出来**之后**,等于没有门。
    """
    fn = _fn("run_full_survivors")
    gate = _stmt_index_of_call(fn, "assert_full_fp_current")
    surv = _stmt_index_of_assign(fn, "surv")
    assert gate >= 0, "run_full_survivors 里根本没调这道门"
    assert surv >= 0, "找不到 surv 的赋值 —— 判据坐标烂了"
    assert gate < surv, (
        f"门在第 {gate} 条、surv 在第 {surv} 条 —— "
        "门必须排在零存活早退之前,否则过期杀证照样从早退口出去")


# ══ B-1-2 · 应跑清单必须 27/27 **唯一** ═══════════════════════════════════
def test_b12_a_duplicated_id_in_the_roster_stops_the_run():
    """🔴 正样本 —— 点名规则:清单里 id 重复,立刻停机。"""
    roster = [{"id": "MUT-A"}, {"id": "MUT-B"}, {"id": "MUT-A"}]
    with pytest.raises(SystemExit) as exc:
        _R.assert_unique_roster(roster, "自证")
    assert "MUT-A" in str(exc.value)


def test_b12_a_unique_roster_passes():
    _R.assert_unique_roster([{"id": "MUT-A"}, {"id": "MUT-B"}], "自证")


def test_b12_one_dup_plus_one_missing_is_invisible_to_every_other_check():
    """🔴 正样本(**点名规则**):一条重复 + 一条缺失,大小不变。

    这条把「为什么必须有唯一性闸」摆出来:同一份清单,
      · 计数闸 `len(muts)==27` —— 过;
      · 硬门的集合比 / 计数比 / 重复比 —— 全过,rc 0;
    而真正少跑的那一发在**任何一处**都不出现。
    只有唯一性闸抓得住它。
    """
    roster = [{"id": f"MUT-{i:02d}"} for i in range(26)] + [{"id": "MUT-00"}]
    assert len(roster) == 27, "这条正样本的前提是**计数照样对**"
    expected = {m["id"] for m in roster}
    assert len(expected) == 26, "折叠没发生的话这条正样本证不到点子上"

    # 续跑语义:重复的那一发只会产出一条记录 ⇒ 26 条结果对 26 个应跑。
    results = [rec(i, **sem(ok_meta())) for i in sorted(expected)]
    assert _R.hard_gate_rc(results, "自证", expected=expected,
                           targets_by_id=tbi(*expected),
                           mut_fp_by_id=mfp(*expected)) == 0, (
        "硬门在这份折叠过的分母上本来就该返回 0 —— 这正是它看不见的那一脚")

    with pytest.raises(SystemExit):
        _R.assert_unique_roster(roster, "自证")


def test_b12_load_v2_takes_the_uniqueness_gate_after_reanchoring():
    """结构锁:重锚会**改 id**(06→06b),所以唯一性必须在重锚**之后**再核一次。

    只在重锚前核 = 只证明抽取产物干净,证不了重锚没撞车。
    """
    fn = _fn("load_v2")
    gate = _stmt_index_of_call(fn, "assert_unique_roster")
    rean = _stmt_index_of_call(fn, "_apply_reanchors")
    assert gate >= 0, "load_v2 没调唯一性闸"
    assert rean >= 0, "找不到 _apply_reanchors —— 判据坐标烂了"
    assert gate > rean, (
        f"唯一性闸在第 {gate} 条、重锚在第 {rean} 条 —— "
        "重锚改 id,闸必须排在它之后")


# ══ B-1-3 · 重锚期望要贯穿 quick / full 两段 ═════════════════════════════
def test_b13_every_verdict_writing_function_also_checks_the_reanchor_expectation():
    """🔴 分母 = **所有写裁定的函数**,机械反查,不是手写两个名字。

    「写下裁定」与「核重锚期望」必须是同一处的两句话。分成两处、只在一处核,
    就是本仓记过的「同一谓词写两处 ⇒ 必有一处没人验」。
    """
    writers, checkers = set(), set()
    for fn in [n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)]:
        for n in ast.walk(fn):
            if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                    and n.slice.value == "verdict"
                    and isinstance(n.ctx, ast.Store)):
                writers.add(fn.name)
            if isinstance(n, ast.Call):
                nm = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if nm == "check_reanchor_expectations":
                    checkers.add(fn.name)
    assert writers, "一处写裁定的地方都没扫到 —— 探针写废了(分母为 0 不是通过)"
    holes = sorted(writers - checkers)
    assert not holes, (
        f"这些函数写裁定却不核重锚期望:{holes} —— "
        "无关的红会冒充重锚命中,而重锚的正当性全部建立在「只动锚一条轴」上")


def test_b13_a_registered_must_red_that_did_not_go_red_is_a_violation():
    """行为向:登记的必红没红 ⇒ 裁定改判,不许算杀。"""
    mut = {"expect_red_superset": ("test_x", "test_y")}
    assert _R.check_reanchor_expectations(
        "MUT-X", mut, ["pkg.py::test_x"]) == "REANCHOR_EXPECT_VIOLATED"


def test_b13_a_registered_must_green_that_went_red_is_a_violation():
    mut = {"expect_red_superset": ("test_x",), "must_stay_green": ("test_z",)}
    assert _R.check_reanchor_expectations(
        "MUT-X", mut, ["pkg.py::test_x", "pkg.py::test_z"]
    ) == "REANCHOR_EXPECT_VIOLATED"


def test_b13_expectations_met_returns_none():
    mut = {"expect_red_superset": ("test_x",), "must_stay_green": ("test_z",)}
    assert _R.check_reanchor_expectations("MUT-X", mut, ["pkg.py::test_x"]) is None


# ══ B-1-4 · 部分跑选择器:请求集合 vs 命中集合 ════════════════════════════
def test_b14_an_empty_expected_set_is_not_a_pass():
    """🔴 正样本 —— 点名规则:0 条结果 + 0 个应跑,**不许**是退出码 0。

    这就是拼错 `V2_ONLY` 的下场:旧代码打印「0 发全部为「杀」」并返回 0。
    「分母为 0 不是通过,是探针写废了」——本仓老规矩,这次长在自己身上。
    """
    assert _R.hard_gate_rc([], "自证", expected=set(), targets_by_id={}) != 0


def test_b14_a_typo_in_v2_only_stops_the_run():
    """🔴 正样本:点名一个清单里没有的 id ⇒ 停机,不是静默跑 0 发。"""
    roster = [{"id": "MUT-EXTE2-01", "family": "E2"}]
    with pytest.raises(SystemExit) as exc:
        _R.assert_selector_hits({"MUT-EXTE2-O1"}, set(), roster, [])
    assert "MUT-EXTE2-O1" in str(exc.value)


def test_b14_a_typo_in_v2_family_stops_the_run():
    roster = [{"id": "MUT-EXTE2-01", "family": "E2"}]
    with pytest.raises(SystemExit) as exc:
        _R.assert_selector_hits(set(), {"E4"}, roster, [])
    assert "E4" in str(exc.value)


def test_b14_an_empty_match_stops_the_run_even_when_every_name_is_valid():
    """名字都对、交集为空(E2 家族 ∩ 某个 E3 的 id)—— 同样是 0 发,同样停机。"""
    roster = [{"id": "MUT-EXTE2-01", "family": "E2"},
              {"id": "MUT-EXTE3-01", "family": "E3"}]
    with pytest.raises(SystemExit) as exc:
        _R.assert_selector_hits({"MUT-EXTE3-01"}, {"E2"}, roster, [])
    assert "0 发" in str(exc.value) or "空" in str(exc.value)


def test_b14_a_legitimate_partial_selection_passes():
    roster = [{"id": "MUT-EXTE2-01", "family": "E2"},
              {"id": "MUT-EXTE3-01", "family": "E3"}]
    _R.assert_selector_hits({"MUT-EXTE2-01"}, set(), roster, [roster[0]])


def test_b14_the_selector_reconciliation_runs_before_the_mutation_loop():
    """结构锁:对账必须在**过滤之后、开跑之前**。"""
    fn = _fn("_main_locked")
    gate = _stmt_index_of_call(fn, "assert_selector_hits")
    assert gate >= 0, "_main_locked 没做请求/命中对账"
    base = _stmt_index_of_call(fn, "_run_targets")
    assert base >= 0, "找不到基线开跑点 —— 判据坐标烂了"
    assert gate < base, "对账必须排在第一次真跑之前(拼错的一轮不该烧基线)"


def test_b14_hard_gate_rc_cannot_be_called_without_an_expected_set():
    """`expected` 不许有默认值 —— 有默认值 = 忘了传的调用点静默拿到不设防的门。

    (与 P2-5 同一条判例:`fp: str | None = None` 那次我自己犯过。)
    """
    import inspect

    sig = inspect.signature(_R.hard_gate_rc)
    p = sig.parameters["expected"]
    assert p.default is inspect.Parameter.empty, (
        "hard_gate_rc(expected=...) 还有默认值 —— 漏传的调用点会拿到"
        "「(⚠️ 未核完整性)」那条静默路径")


def test_b14_every_call_site_passes_a_non_empty_expected_set():
    """分母 = **调用点**(AST),不是函数。验标记 ≠ 验接线。"""
    sites = [n for n in ast.walk(_TREE)
             if isinstance(n, ast.Call)
             and (getattr(n.func, "attr", None)
                  or getattr(n.func, "id", None)) == "hard_gate_rc"]
    assert sites, "一个调用点都没扫到 —— 探针写废了"
    bad = [s.lineno for s in sites
           if not any(k.arg == "expected" for k in s.keywords)]
    assert not bad, f"第 {bad} 行的 hard_gate_rc 没传 expected"
