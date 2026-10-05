# -*- coding: utf-8 -*-
"""变异 runner **硬门**的合同判据(Codex 三审 P1-7 · 工单 V3-B B-1)。

Codex 报「自动门不闭」,Review 亲读坐实,**我先端到端复现再动手**:
把 MUT-EXTE3-05b 的两条杀手判据(e5_10 / e5_11)在**基线里**打红,同一发
在零红基线下是确认的杀,红基线下:

    基线:绿 82 · 红 2  →  ⚠️ 只打印  →  quick_red=[]  →  SURVIVED_QUICK  →  rc=0

一幕同时坐实两条:①基线红不拦;②`new_red = red - base_red` 在红基线下
**降级杀证**(被基线红减掉的那几条,正是本该证明"杀"的那几条)。

四道门各配一条判据,每条都带**点名规则的正样本** —— 门没有正样本
就只是一句"我加了闸"。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_hardgate_contract.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"
DRIVER = ROOT / "scripts" / "gate9_replay_from_list.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    return mod


_R = _load("hardgate_runner", RUNNER)
# 🔴 [V7-B] 夹具改用生产格式记录(带 run_meta)。这一族每条都必须**只因自己那条规则**
#    变红 —— 记录若不合法,终门会因 run_meta 变红,把 dup / 缺发 / 未解决那些规则
#    的区分力整个顶掉(删掉它们判据照样红)。
from mutation_criteria_fixtures import mfp, ok_meta, rec, sem, tbi  # noqa: E402
_D = _load("hardgate_driver", DRIVER)


# ── 门① 基线有红 ⇒ 非零退出且零落盘 ──────────────────────────────────────
def test_gate1_a_red_baseline_stops_the_run(tmp_path):
    """🔴 正样本 —— 点名规则:基线带红不许开跑。

    这就是复现里那一幕:基线红 2 条,而它们**正是**这一发的杀手判据。
    """
    with pytest.raises(SystemExit) as exc:
        _R.assert_baseline_clean({"pkg::test_a"}, "自证", None)
    assert "基线有红" in str(exc.value)


def test_gate1_a_clean_baseline_passes():
    _R.assert_baseline_clean(set(), "自证", None)          # 不抛即通过


def test_gate1_refuses_to_run_when_a_half_written_result_file_exists(tmp_path):
    """半份结果比没有更毒:基线红时若产物已在,必须点名它、让人先挪走。"""
    res = tmp_path / "extsel_v2_results_X.json"
    res.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _R.assert_baseline_clean({"pkg::test_a"}, "自证", res)
    assert res.name in str(exc.value)


# ── 门② 任一未解决裁定 ⇒ 非零退出 ────────────────────────────────────────
@pytest.mark.parametrize("verdict", sorted(_R.UNRESOLVED_VERDICTS))
def test_gate2_every_unresolved_verdict_makes_rc_nonzero(verdict):
    """🔴 正样本:每一种**已知的未解决**裁定都必须让退出码非零。

    分母不是我手写的 —— 直接从 `UNRESOLVED_VERDICTS` 参数化,
    以后新增一种裁定,这条判据自动把它算进来。
    """
    assert _R.hard_gate_rc([rec("X", verdict=verdict)], "自证",
                           expected={"X"}, targets_by_id=tbi("X"),
                           mut_fp_by_id=mfp("X")) != 0


def test_gate2_an_unknown_verdict_also_makes_rc_nonzero():
    """未知裁定不许落进兜底 —— 「没见过」和「没问题」不是一回事。"""
    assert _R.hard_gate_rc([rec("X", verdict="SOMETHING_NEW")], "自证",
                           expected={"X"}, targets_by_id=tbi("X"),
                           mut_fp_by_id=mfp("X")) != 0


def test_gate2_all_killed_is_the_only_zero():
    # 🔴 [V8-B] KBWD 在 **quick 侧是存活形状**(所以才轮到全分母复核),杀证在 full 侧;
    #    并且它必须带 `full_run_meta`,否则「更宽分母下被杀」这句话的证据面是空的。
    #    这里用可注入的 `full_targets` 给它配一套完整的 full 侧 ——
    #    顺带证明那个参数真的被两个入口读了,而不是只写在签名上。
    # 🔴 [V9-B] full 侧用**真包**:P1-3 之后 full_criteria_fp 要真算得出来,
    #    合成包名会撞树锁「判据目标不存在」停机 —— 生产的 full_targets 永远是真包。
    ftg = tuple(_R.FULL[:1])
    fmeta = ok_meta(ftg, collected=20, red=3)
    got = _R.hard_gate_rc(
        [rec("A", **sem(ok_meta())),
         rec("B", verdict="KILLED_BY_WIDER_DENOMINATOR", meta=ok_meta(red=0),
             **sem(ok_meta(red=0), "SURVIVED_QUICK"),
             full_run_meta=fmeta, full_criteria_fp=_R.criteria_fp_of(ftg),
             **sem(fmeta, "KILLED_BY_WIDER_DENOMINATOR", label="full"))],
        "自证",
        expected={"A", "B"}, targets_by_id=tbi("A", "B"),
        mut_fp_by_id=mfp("A", "B"), full_targets=ftg)
    assert got == 0


def test_gate2_the_zero_survivor_early_return_does_not_bypass_the_gate():
    """🔴 正样本 —— 点名规则:**「零存活」不等于「全杀」**。

    第二趟开头有一句 `if not surv: return 0`,而 `surv` 只筛 `SURVIVED_QUICK`。
    产物里若有 `INVALID_SYNTAX` / `PACKAGE_BROKEN_NEEDS_REVIEW` /
    `GREEN_NOT_CONSERVED`,它们**都不是** `SURVIVED_QUICK` ⇒ `surv` 为空 ⇒
    直接返 0,**门②整个被绕过**。
    (这条是我自己复核硬门时挖到的旁路,不是 Codex 报的四条之一。)
    """
    import ast as _ast

    tree = _ast.parse(RUNNER.read_text(encoding="utf-8"))
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "run_full_survivors")
    bare_zero = [n.lineno for n in _ast.walk(fn)
                 if isinstance(n, _ast.Return)
                 and isinstance(n.value, _ast.Constant) and n.value.value == 0]
    assert not bare_zero, (
        f"第 {bare_zero} 行是无条件 `return 0` —— 任何一条出路都必须过硬门,"
        "「零存活」只说没人待复核,不说全杀")

    # 行为面:一份「没有存活、但有语法崩」的产物必须非零。
    assert _R.hard_gate_rc(
        [rec("A"), rec("B", verdict="INVALID_SYNTAX")], "自证",
        expected={"A", "B"}, targets_by_id=tbi("A", "B"),
        mut_fp_by_id=mfp("A", "B")) != 0


def test_gate2_all_killed_but_truncated_must_go_red():
    """🔴 正样本 —— 点名规则:**「全是杀」不等于「该跑的都跑了」**。

    2026-08-28 终放当场暴露:`MUT-EXTE3-07` 锚命中 2 次 ⇒ runner 停机(对的),
    可第二趟对着**只有 18 条**的半份产物打印「18 发全部为「杀」,退出码 0」——
    18 条确实全是杀,**应有 27**。门② 当时只查"有没有坏裁定",没查分母。
    「全绿」在一个悄悄变小的分母上永远成立。
    """
    want = {f"MUT-{i}" for i in range(27)}
    tb = tbi(*want)
    mf = mfp(*want)
    truncated = [rec(f"MUT-{i}", **sem(ok_meta())) for i in range(18)]
    assert _R.hard_gate_rc(truncated, "自证", expected=want,
                           targets_by_id=tb, mut_fp_by_id=mf) != 0, (
        "截断到 18 发还给 0 —— 分母没人守")
    # 阴性对照:补齐就该是 0,否则上面那条红证明不了是"缺发"造成的。
    full = [rec(f"MUT-{i}", **sem(ok_meta())) for i in range(27)]
    assert _R.hard_gate_rc(full, "自证", expected=want, targets_by_id=tb,
                           mut_fp_by_id=mf) == 0


def test_gate2_an_extra_shot_not_in_the_expected_set_also_goes_red():
    """产物里多出应跑集合之外的发,同样非零 —— 双向,不是只防少。"""
    want = {"MUT-A"}
    recs = [rec("MUT-A"), rec("MUT-ZZ")]
    assert _R.hard_gate_rc(recs, "自证", expected=want,
                           targets_by_id=tbi("MUT-A", "MUT-ZZ"),
                           mut_fp_by_id=mfp("MUT-A", "MUT-ZZ")) != 0


def test_gate2_every_call_site_passes_the_expected_set():
    """结构锁:三处 `hard_gate_rc` 调用点**都**要传 expected,漏一处就是漏一个分母。"""
    import ast as _ast

    tree = _ast.parse(RUNNER.read_text(encoding="utf-8"))
    calls = [n for n in _ast.walk(tree)
             if isinstance(n, _ast.Call)
             and getattr(n.func, "id", None) == "hard_gate_rc"]
    assert calls, "一个调用点都没扫到 —— 探针写废了"
    naked = [n.lineno for n in calls
             if not any(kw.arg == "expected" for kw in n.keywords)]
    assert not naked, f"第 {naked} 行的 hard_gate_rc 没传 expected —— 那一处不核分母"


def test_gate2_resolved_and_unresolved_do_not_overlap():
    """两张表不许有交集 —— 有交集的话同一种裁定既算解决又算未解决。"""
    assert not (_R.RESOLVED_VERDICTS & _R.UNRESOLVED_VERDICTS)


# ── 门③ 每条结果绑 tip + criteria_fp ─────────────────────────────────────
def test_gate3_a_record_whose_criteria_fingerprint_differs_is_refused():
    """🔴 正样本 —— 点名规则:**判据变了**而尖没变,旧读数不许冒充本轮结果。

    尖只说「哪棵树」,指纹才说「哪套判据」。criteria-only 的改动落进工作区
    而还没提交时,`git rev-parse HEAD` 一个字都不变 —— 只钉尖是拦不住的。

    (「指纹会跟着判据一字节变动而变」那条**不在这里重写** ——
     `test_mutation_tree_lock.py::test_fingerprint_changes_when_a_criteria_file_changes`
     已经在守它了。同一个谓词写两处,必有一处没人验。)
    """
    tip = "a" * 40
    recs = [{"id": "MUT-A", "tip": tip, "criteria_fp": "0123456789abcdef",
             "run_meta": ok_meta()}]
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids(recs, tip, "x.json", {"MUT-A": "fedcba9876543210"},
                           targets_by_id=tbi("MUT-A"))
    assert "判据指纹" in str(exc.value)


def test_gate3_a_record_without_a_fingerprint_is_refused():
    """没绑指纹的旧格式产物同样不作数 —— 它是这道闸之前写的,来路不明。"""
    tip = "a" * 40
    with pytest.raises(SystemExit):
        _R.resume_done_ids([{"id": "MUT-A", "tip": tip,
                             "run_meta": ok_meta()}], tip, "x.json",
                           {"MUT-A": "0123456789abcdef"},
                           targets_by_id=tbi("MUT-A"))


def test_gate3_matching_tip_and_fingerprint_pass():
    tip = "a" * 40
    fp = "0123456789abcdef"
    recs = [rec("MUT-A", tip=tip, fp=fp, **sem(ok_meta()))]
    assert _R.resume_done_ids(recs, tip, "x.json", {"MUT-A": fp},
                              targets_by_id=tbi("MUT-A"),
                              mut_fp_by_id=mfp("MUT-A")) == {"MUT-A"}


def test_gate3_both_write_paths_bind_the_fingerprint():
    """结构锁:快集相与全分母相**两处落盘**都要绑指纹,只绑一处等于半守。"""
    src = RUNNER.read_text(encoding="utf-8")
    assert 'rec["criteria_fp"] = criteria_fp_of(' in src, "快集落盘没绑指纹"
    assert 'r["full_criteria_fp"] = full_fp' in src, "全分母落盘没绑指纹"
    assert "def criteria_fp_of(" in src, "指纹取值函数没接进来"


# ── 门④ 终审:KILLED 集 ≡ expected 集,拒绝异常 verdict ───────────────────
def test_gate4_all_ids_present_but_all_survived_must_go_red():
    """🔴 正样本 —— 点名规则:**ID 全在场但一个都没杀**,旧写法会放行。

    这正是 Codex 打的那一枪:`set(after)` 只拿到 id,
    「27 个 ID 都在场」= 过,**27 个 SURVIVED 也 = 过**。
    """
    want = {"MUT-A", "MUT-B"}
    records = {"MUT-A": "SURVIVED_FULL", "MUT-B": "SURVIVED_FULL"}
    # 旧写法(只核 ID 在场)会放行 —— 先证明这条正样本打的是真差异:
    _D.assert_set_equal(set(records), want, "旧写法自证")     # 不抛
    with pytest.raises(SystemExit) as exc:
        _D.assert_killed_set_equals(records, want, "自证")
    assert "不是「杀」" in str(exc.value)


def test_gate4_one_survivor_among_killed_still_goes_red():
    want = {"MUT-A", "MUT-B"}
    records = {"MUT-A": "KILLED", "MUT-B": "SURVIVED_QUICK"}
    with pytest.raises(SystemExit):
        _D.assert_killed_set_equals(records, want, "自证")


def test_gate4_an_unknown_verdict_is_rejected():
    want = {"MUT-A"}
    with pytest.raises(SystemExit):
        _D.assert_killed_set_equals({"MUT-A": "WEIRD"}, want, "自证")


def test_gate4_all_killed_passes():
    want = {"MUT-A", "MUT-B"}
    _D.assert_killed_set_equals(
        {"MUT-A": "KILLED", "MUT-B": "KILLED_BY_WIDER_DENOMINATOR"}, want, "自证")


def test_gate4_the_driver_actually_calls_the_verdict_level_check():
    """结构锁:驱动的**终审**必须调裁定级那条,不能退回只核 ID 在场。

    🔴 用 AST 找**真实调用**,不用裸串扫全文:第一版就是裸串,
       结果命中了我写在 docstring 里引用的旧写法原文,当场自己判自己红
       (本仓记过:引用裁决原文会让裸串结构锁误判)。锁要精确,不靠白名单。
    """
    import ast as _ast

    tree = _ast.parse(DRIVER.read_text(encoding="utf-8"))
    called = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name:
                called.add(name)
    assert "assert_killed_set_equals" in called, "终审没接裁定级对账(AST 里查无调用)"

    # 且不许还有「只核 ID 在场」的**终审**调用:找 assert_set_equal(set(after), ...)
    legacy = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        if (getattr(node.func, "id", None) != "assert_set_equal") or not node.args:
            continue
        seg = _ast.dump(node.args[0])
        if "'after'" in seg or '"after"' in seg or "id='after'" in seg:
            legacy.append(node.lineno)
    assert not legacy, (
        f"第 {legacy} 行仍在用只核 ID 在场的写法做终审 —— 27 个 SURVIVED 也会被判成过")
