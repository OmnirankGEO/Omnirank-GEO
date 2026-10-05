# -*- coding: utf-8 -*-
"""`run_meta` 校验单点化 + 续跑/终门两入口的合同判据(V7-B · Codex fof5 P1-1)。

**修之前的实测**(纯机制反例,`scratchpad/v7b/poison_probe.py`):
同尖、同判据指纹、verdict=KILLED 的缓存记录,`run_meta` 注入 11 种坏形态 ——

    resume_done_ids  →  11/11 返回 {'M'}(当成已跑过,跳过重跑)
    hard_gate_rc     →  11/11 rc=0

因为两个入口**一次都没读过 run_meta**:前者只比 tip + 逐发指纹 + 归属,
后者只比 verdict / 缺发 / 多发 / 重复。而唯一读它的 `assert_run_meta_sane`
只挂在「新产物」那一路上 —— 续跑缓存与终门两条路完全没人验。

三层是三件事,缺一层就有一整类假绿::

    裁定对了吗  →  该跑的都在吗  →  **这些读数本身可信吗**

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_run_meta_gate_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"

_spec = importlib.util.spec_from_file_location("rmg_runner", RUNNER)
_R = importlib.util.module_from_spec(_spec)
sys.modules["rmg_runner"] = _R
sys.path.insert(0, str(RUNNER.parent))
_spec.loader.exec_module(_R)

from mutation_criteria_fixtures import mfp, ok_meta, rec, sem, tbi  # noqa: E402

TIP = "c" * 40
FP = "deadbeef" * 8
TGT = "tests/pkg_a"

#: 11 种坏 run_meta。**逐条点名它该踩的那条规则** —— 判据不点名规则,
#: 就只是「我加了个闸」。第 7 条(空 map)是我改这一版时挖到的第 4 个洞:
#: 老实现 `for t, m in (meta or {}).items()` 在空 dict 上是空循环 ⇒ bad 为空 ⇒ 返 None。
POISONS = [
    ("null", {TGT: {"collected": None, "passed": 10, "red": 2, "skipped": 0}},
     "不是 int"),
    ("str", {TGT: {"collected": "12", "passed": 10, "red": 2, "skipped": 0}},
     "不是 int"),
    ("float", {TGT: {"collected": 12.0, "passed": 10, "red": 2, "skipped": 0}},
     "不是 int"),
    ("bool", {TGT: {"collected": True, "passed": True,
                    "red": False, "skipped": False}}, "不是 int"),
    ("negative", {TGT: {"collected": -12, "passed": -10, "red": -2, "skipped": 0}},
     "是负数"),
    ("sum_broken", {TGT: {"collected": 12, "passed": 10, "red": 5, "skipped": 0}},
     "!="),
    ("empty_map", {}, "空 map"),
    ("missing_tgt", {"tests/pkg_OTHER": {"collected": 12, "passed": 12,
                                         "red": 0, "skipped": 0}}, "target 集"),
    ("extra_tgt", {TGT: {"collected": 12, "passed": 10, "red": 2, "skipped": 0},
                   "tests/pkg_EXTRA": {"collected": 1, "passed": 1,
                                       "red": 0, "skipped": 0}}, "target 集"),
    ("missing_key", {TGT: {"collected": 12}}, "缺字段"),
    ("not_a_dict", "12/10/2/0", "不是 dict"),
]
IDS = [p[0] for p in POISONS]


def _rec(meta, mid="MUT-A"):
    # 🔴 [V9-B] 不在这里再写一遍「生产格式长什么样」—— 那是夹具单点的事。
    #    上一版这里手写 dict,于是 mutation_fp 落地时它整批漏掉:
    #    同一谓词写两处,必有一处没跟上。
    extra = {}
    try:
        extra = sem(meta)     # 坏 meta 算不出来也不必算,结构面会先拒
    except Exception:
        pass
    return rec(mid, tip=TIP, fp=FP, meta=meta, **extra)


# ══ B-1-1 单一纯谓词:每一种坏形态都点名自己那条规则 ══════════════════════
@pytest.mark.parametrize("name,meta,needle", POISONS, ids=IDS)
def test_rmg_01_the_predicate_names_the_rule_it_broke(name, meta, needle):
    """🔴 正样本 —— 每条反例的报文必须**点名它踩的那条规则**。

    只断言「返回了非 None」不够:那样把类型检查换成加法检查也照样绿,
    而两者抓的是不同的毒(`12.0 == 12` 为真,浮点只有类型检查抓得住)。
    """
    why = _R.validate_record_run_meta(_rec(meta), (TGT,), mut_fp_by_id=mfp("MUT-A"))
    assert why, f"{name} 被判为好"
    assert needle in why, f"{name} 的报文没点名规则:{why}"


def test_rmg_02_a_real_format_record_passes():
    """阴性对照 —— **真产物格式**(真 mutation id + 它真正的 QUICK_SCOPE)必须放行。

    不用合成 id:合成 id 配合成 scope,证不到「生产真会写出来的那种记录能过」。
    过严也是坏 —— 上面 11 条红只有在这条绿成立时才说明是「坏记录被拦」,
    而不是「这道闸谁都拦」。
    """
    mid = "MUT-EXTE2-01"
    scope = _R.QUICK_SCOPE[mid]
    assert scope, "取真作用域失败,判据坐标烂了"
    meta = {t: {"collected": 96, "passed": 93, "red": 3, "skipped": 0}
            for t in scope}
    assert _R.validate_record_run_meta(_rec(meta, mid), scope,
                                       mut_fp_by_id=mfp(mid)) is None


# ══ B-1-2 续跑臂:11 种坏记录都必须停机 ═══════════════════════════════════
@pytest.mark.parametrize("name,meta,needle", POISONS, ids=IDS)
def test_rmg_03_resume_refuses_every_poison(name, meta, needle):
    """🔴 续跑臂正样本:同尖同指纹的坏缓存**不许**当「已跑过」。

    Codex 反例逐字:注入这些形态后,修前 `resume_done_ids` 返回 `{'MUT-A'}`
    ⇒ 那一发被跳过重跑,坏读数直接进终局,而且长着「已跑过」的样子。
    """
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids([_rec(meta)], TIP, "x.json", {"MUT-A": FP},
                           targets_by_id=tbi("MUT-A"), mut_fp_by_id=mfp("MUT-A"))
    assert "run_meta 不可信" in str(exc.value)


def test_rmg_04_resume_accepts_a_clean_record():
    """阴性对照:合法记录仍要被收下,否则上面 11 条红证明不了是 run_meta 造成的。"""
    assert _R.resume_done_ids([_rec(ok_meta())], TIP, "x.json", {"MUT-A": FP},
                              targets_by_id=tbi("MUT-A"), mut_fp_by_id=mfp("MUT-A")) == {"MUT-A"}


# ══ B-1-3 终门臂:同样 11 种,rc 必须非零 ═════════════════════════════════
@pytest.mark.parametrize("name,meta,needle", POISONS, ids=IDS)
def test_rmg_05_the_hard_gate_refuses_every_poison(name, meta, needle):
    """🔴 终门臂正样本:27 条全 KILLED、集合两向都对,**读数坏**照样不许拿 0。

    「都跑了」不等于「读数可信」—— 终门过去只到前一层为止。
    """
    assert _R.hard_gate_rc([_rec(meta)], "自证", expected={"MUT-A"},
                           targets_by_id=tbi("MUT-A"), mut_fp_by_id=mfp("MUT-A")) != 0


def test_rmg_06_the_hard_gate_still_returns_zero_on_clean_records():
    assert _R.hard_gate_rc([_rec(ok_meta())], "自证", expected={"MUT-A"},
                           targets_by_id=tbi("MUT-A"), mut_fp_by_id=mfp("MUT-A")) == 0


def test_rmg_07_an_unknown_id_has_no_expected_targets_and_is_refused():
    """fail-closed:`targets_by_id` 里查不到这一发 ⇒ 应跑集为 None ⇒ 拒。

    不许「查不到就跳过」:那正是空分母恒真的另一种写法。
    """
    assert _R.hard_gate_rc([_rec(ok_meta())], "自证", expected={"MUT-A"},
                           targets_by_id={}, mut_fp_by_id=mfp("MUT-A")) != 0


# ══ 门② 重复 ID —— 存量洞:七路毒实测它一条判据都没有 ═════════════════════
def test_rmg_08_a_duplicate_id_makes_the_hard_gate_nonzero():
    """🔴 正样本 —— 点名规则:**一条重复配一条缺,集合与计数都看不出来**。

    这条不是 V7-B 新加的门,代码里 `[B-2①]` 那段一直在。
    但我把 `gate_rc` 七路输入逐个毒掉之后实测:毒掉 `dup` 那一路,
    **243 条判据一条都不红** —— 门在,驱动它的判据不在。
    (「谓词有牙」与「结果进退出码」是两件事,本仓记过;这次是第三种形态:
     牙有了、线接了,**没人来咬一口试试**。)
    """
    recs = [_rec(ok_meta(), "MUT-A"), _rec(ok_meta(), "MUT-A"),
            _rec(ok_meta(), "MUT-B")]
    # 集合相等 —— 只有唯一性那一路看得见。
    assert {r["id"] for r in recs} == {"MUT-A", "MUT-B"}
    assert _R.hard_gate_rc(recs, "自证", expected={"MUT-A", "MUT-B"},
                           targets_by_id=tbi("MUT-A", "MUT-B"), mut_fp_by_id=mfp("MUT-A", "MUT-B")) != 0


# ══ 接线三锁(V6-B `baseline_rc` 同形):纯裁决 + 数据流 + 禁字面量 ═════════
def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(RUNNER.read_text(encoding="utf-8-sig"))
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


@pytest.mark.parametrize("path", ["empty_expected", "unresolved", "unknown",
                                  "missing", "extra", "dup", "meta_bad"])
def test_rmg_09_every_gate_rc_path_alone_drives_one(path):
    """🔴 纯函数正样本:七路**各自单独**驱动 rc→1。

    抓的毒:「裁决函数无视某一路」。七路写在一个 or 里,漏掉一路
    不改任何字符串、不动任何位置 —— 只有逐路正样本抓得住。
    """
    zero = {"empty_expected": False, "unresolved": [], "unknown": [],
            "missing": [], "extra": [], "dup": [], "meta_bad": []}
    assert _R.gate_rc(**zero) == 0, "全清零时不该非零"
    one = dict(zero)
    one[path] = True if path == "empty_expected" else ["x"]
    assert _R.gate_rc(**one) == 1, f"{path} 单独一路没能驱动 rc→1"


def test_rmg_10_hard_gate_rc_has_no_literal_return():
    """🔴 禁字面量 return:入口不许有 `return 0` / `return 1`。

    抓的毒:「整段退回内联 if」—— 那样纯函数还在、正样本还绿,而闸已经不接线。
    """
    fn = _fn("hard_gate_rc")
    lits = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)]
    assert not lits, f"第 {lits} 行是字面量 return —— 终态必须过 gate_rc"
    rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(rets) == 1, f"出口不止一个({len(rets)} 个),单出口才锁得住"


def test_rmg_11_meta_bad_traces_back_to_the_real_predicate():
    """🔴 数据流锁:`gate_rc(meta_bad=X)` 的 X 必须回溯到同函数内
    一次真正的 `validate_record_run_meta(...)` 调用。

    抓的毒:「调用点把它换成 `[]`」—— 那样禁字面量锁、纯函数正样本全都还绿,
    而终门读的是个恒空列表。(V6-B 的 `skip_bad` 就是这么被 Review 毒穿的。)
    """
    fn = _fn("hard_gate_rc")
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "gate_rc")
    arg = next(k.value for k in call.keywords if k.arg == "meta_bad")
    assert isinstance(arg, ast.Name), f"meta_bad 实参不是变量:{ast.dump(arg)[:80]}"
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == arg.id
                       for t in n.targets)
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "validate_record_run_meta"
                       for c in ast.walk(n.value))]
    assert assigns, (
        f"`{arg.id}` 在 hard_gate_rc 里没有一次由 validate_record_run_meta "
        "计算的赋值 —— 终门读的可能是个恒空列表")


def test_rmg_12_resume_stops_on_bad_meta_by_raising():
    """🔴 数据流锁(续跑臂):`validate_record_run_meta(...)` 的结果必须
    **既被算出来、又真的当成那个 `raise` 的条件**。

    自打毒实测,这把锁分两段各有各的牙(说准哪一把响,不含糊):

    · 毒「整段删掉(连 raise)」  ⇒ 下面第 ① 段响(找不到谓词调用);
    · 毒「`if meta_bad:` 改成 `if False:`」⇒ 第 ① 段**不响** —— raise 的文本还在、
      赋值也还在。第一版判据到此为止,只靠行为臂 `test_rmg_03` 兜着。
      所以补了第 ② 段:守着 raise 的那个 `if`,它的条件必须**就是**
      由谓词算出来的那个名字。
    """
    fn = _fn("resume_done_ids")
    # ① 谓词得真被调用,且它算出来的名字要拿得到
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "validate_record_run_meta"
                       for c in ast.walk(n.value))]
    assert assigns, "续跑臂没有一处由 validate_record_run_meta 计算的赋值"
    names = {t.id for a in assigns for t in a.targets if isinstance(t, ast.Name)}
    assert names, "谓词的结果没赋给任何名字 —— 它算了也没人用"

    # ② 那个名字必须**当条件**守着一个 raise SystemExit
    # 🔴 [同 p04 那笔:「出现过」不等于「就是」] 条件必须**就是**那个名字。
    #    写成 `used = 条件里出现过的所有名字` 时,`if False and meta_bad:`
    #    照样命中 —— 短路之后那个 raise 永远不执行,而锁看不出来。
    guarded = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if not (isinstance(node.test, ast.Name) and node.test.id in names):
            continue
        if any(isinstance(x, ast.Raise) for x in ast.walk(node)):
            guarded.append(node.lineno)
    assert guarded, (
        f"没有任何 `if <{'/'.join(sorted(names))}>:` 守着一个 raise —— "
        "谓词算了、结果也赋值了,但停机那一步不由它决定"
        "(毒「if meta_bad → if False」就长这样)")


# ══ 分母锁:所有调用点都必须传 targets_by_id ═════════════════════════════
def test_rmg_13_every_call_site_passes_targets_by_id():
    """🔴 分母 = **AST 机械枚举的调用点**,含判据文件自己。

    本仓两次被同一形咬过:改函数契约只验了定义,调用点没人验
    (`_run_targets` 3→4 元组漏第三处 / `print_transport_identity` 咬三处判据)。
    第三次不许再靠人记得。
    """
    need = {"hard_gate_rc": {"expected", "targets_by_id"},
            "resume_done_ids": {"targets_by_id"}}
    files = sorted((ROOT / "scripts").glob("*.py"))
    assert len(files) >= 10, f"分母守卫:只扫到 {len(files)} 个文件,cwd 漂了?"
    bad, seen = [], 0
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            # 🔴 两种形态都要认:runner 里是裸名 `hard_gate_rc(...)`,
            #    判据文件里是 `_R.hard_gate_rc(...)`(Attribute)。
            #    只认 Name 时这条普查只数到 5 处,而真数是 29 ——
            #    这一笔是本判据**自己的分母守卫**抓出来的,不是我先想到的。
            nm = (n.func.id if isinstance(n.func, ast.Name)
                  else n.func.attr if isinstance(n.func, ast.Attribute) else None)
            if nm not in need:
                continue
            seen += 1
            kw = {k.arg for k in n.keywords if k.arg}
            if not need[nm] <= kw:
                bad.append(f"{f.name}:{n.lineno} {nm} 缺 "
                           f"{sorted(need[nm] - kw)}")
    assert seen >= 25, f"只枚举到 {seen} 处调用点 —— 分母可疑"
    assert not bad, "这些调用点没传全分母参数:" + " · ".join(bad)


# ══════════════════════════════════════════════════════════════════════════
# V8-B · verdict ↔ 红集 的**语义**一致性(Codex fof6 P1-1)
# ══════════════════════════════════════════════════════════════════════════
#
# 修之前:`verdict=KILLED + quick_red=[] + run_meta 全 0` 结构完全合法 ——
# 类型对、加法平、target 集对 —— 于是续跑收下它、终门返 0,还打印「run_meta 逐条可信」。
# 而它是**语义不可能**的:`_verdict` 里 `if not new_red: return SURVIVED_*`,
# KILLED / KILLED_BLUNT 只在 `new_red` 非空时可达。
#
# 严格相等的依据不是拍脑袋:快集基线由 `assert_baseline_clean` 强制零红 ⇒
# `new_red = red - base_red` 里 base_red 为空 ⇒ `sum(red) == len(quick_red)`
# 是那道闸的**推论**。27 发真产物逐条实测 27/27 精确相等。

_OK = {TGT: {"collected": 96, "passed": 93, "red": 3, "skipped": 0}}
_ZERO = {TGT: {"collected": 96, "passed": 96, "red": 0, "skipped": 0}}
_ALLPASS = {TGT: {"collected": 12, "passed": 12, "red": 0, "skipped": 0}}


def _semrec(**kw):
    base = {"quick_red": [f"m::t{i}" for i in range(3)], "quick_green": 93}
    base.update(kw)
    return rec("MUT-A", tip=TIP, fp=FP, meta=_OK, **base)


# 🔴 [V9-B] full 侧一律用**真包**:P1-3 之后 `full_criteria_fp` 要真算得出来。
#    喂 "tests/pkg_a" 这种合成名会让树锁停机(判据目标不存在)——
#    那是夹具供了生产不会供的东西,红的不是被测代码。
FULLT = tuple(_R.FULL[:1])
FULLFP = _R.criteria_fp_of(FULLT)

SEM_POISONS = [
    ("kill_all_zero", dict(run_meta=_ZERO, quick_red=[], quick_green=96),
     "quick_red 为空"),
    ("kill_all_pass", dict(run_meta=_ALLPASS, quick_red=[], quick_green=12),
     "quick_red 为空"),
    ("kill_empty_red_nonzero_meta", dict(quick_red=[], quick_green=93),
     "quick_red 为空"),
    ("kill_red_count_mismatch", dict(quick_red=["m::t0"]),
     "!= len(quick_red)"),
    ("kill_green_mismatch", dict(quick_green=999), "quick_green=999"),
    ("survived_with_red", dict(verdict="SURVIVED_QUICK"), "却 quick_red 非空"),
    ("survived_zero_red_but_meta_red", dict(verdict="SURVIVED_QUICK", quick_red=[],
                                            quick_green=93), "红总数 3"),
    ("kbwd_without_full_run_meta",
     dict(verdict="KILLED_BY_WIDER_DENOMINATOR", run_meta=_ZERO, quick_red=[],
          quick_green=96, full_red=["m::x"], full_green=1,
          full_criteria_fp=FULLFP), "没有 full_run_meta"),
    ("quick_red_not_a_list", dict(quick_red="m::t0"), "不是 list"),
]
SEM_IDS = [p[0] for p in SEM_POISONS]


@pytest.mark.parametrize("name,kw,needle", SEM_POISONS, ids=SEM_IDS)
def test_rmg_18_the_predicate_names_the_semantic_rule(name, kw, needle):
    """🔴 每一种语义反例都要**点名它踩的那条语义规则**。

    只断言「返回非 None」不够:那样把「杀必须有红」换成「绿数必须对」
    也照样绿,而两者抓的是不同的伪造。
    """
    why = _R.validate_record_run_meta(_semrec(**kw), (TGT,), full_targets=FULLT,
                                  mut_fp_by_id=mfp("MUT-A"))
    assert why, f"{name} 被判为好 —— 语义面放行了一条不可能的记录"
    assert needle in why, f"{name} 的报文没点名规则:{why}"


@pytest.mark.parametrize("name,kw,needle", SEM_POISONS, ids=SEM_IDS)
def test_rmg_19_resume_refuses_every_semantic_poison(name, kw, needle):
    """🔴 续跑臂:同尖同指纹的**语义不可能**缓存不许当「已跑过」。

    Codex 反例逐字:修前 `resume_done_ids` 收下它并返回 {'MUT-A'}。
    """
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids([_semrec(**kw)], TIP, "x.json", {"MUT-A": FP},
                           targets_by_id=tbi("MUT-A"), mut_fp_by_id=mfp("MUT-A"),
                           full_targets=FULLT)
    assert "run_meta 不可信" in str(exc.value)


@pytest.mark.parametrize("name,kw,needle", SEM_POISONS, ids=SEM_IDS)
def test_rmg_20_the_hard_gate_refuses_every_semantic_poison(name, kw, needle):
    """🔴 终门臂:同上,rc 必须非零。修前它返 0 并打印「run_meta 逐条可信」。"""
    assert _R.hard_gate_rc([_semrec(**kw)], "自证", expected={"MUT-A"},
                           targets_by_id=tbi("MUT-A"), mut_fp_by_id=mfp("MUT-A"),
                           full_targets=FULLT) != 0


def test_rmg_21_the_real_27_records_all_pass_the_semantic_rules():
    """🔴 绿臂用**真产物**,不是合成记录 —— 三条臂,各证一件事。

    27 发实测:`sum(run_meta.*.red) == len(quick_red)` 与
    `sum(passed) == quick_green` 各 27/27 —— 语义规则定成严格相等有实证依据。
    这条同时是「过严也是坏」的守卫:规则一旦收得比现实紧,它先红。

    🔴 [fof8 P1-1] 那份冻结产物写在 `mutation_fp` **与口径版本**落地之前。
    拆三臂,每臂只证一件事:
      · 臂A(回归):补齐指纹 + 版本之后 27 条必须全过 —— 老规则没被新字段带崩;
      · 臂B(版本闸有牙):只缺版本 ⇒ 全被拒,且报文点名 **mutation_fp_version**;
      · 臂C(指纹闸有牙):给了版本、缺指纹 ⇒ 全被拒,且报文点名 **mutation_fp**。
    分成 B/C 两臂是因为两者**处置不同**:口径变了要全部重跑,
    单发定义变了只重跑那一发 —— 报文混在一起,处置也就混在一起。
    """
    import json
    ev = pathlib.Path("C:/AI-Test/_v6b_fof6_evidence_20260830/shots27/"
                      "extsel_v2_results_final.json")
    if not ev.is_file():
        pytest.skip(f"上一轮冻结产物不在这台机器上:{ev}")
    recs = json.loads(ev.read_text(encoding="utf-8"))
    assert len(recs) == 27, f"真产物只有 {len(recs)} 条"
    assert not any("mutation_fp" in r for r in recs), (
        "这份冻结产物已经带 fp 了 —— 这几臂的前提没了,判据要重写")
    table = _R.mutation_fp_by_id()
    # 🔴 [#91 · 2026-09-06] 这份冻结产物出在 2026-09-06 重锚**之前**,里面是旧 id
    #    (MUT-EXTE3-09 / -10);名册现在是 09b / 10b。旧 id 在这里 KeyError,
    #    不是「历史产物坏了」,是同一发换了身份 —— 在**消费方**转,
    #    别把别名掺进 mutation_fp_by_id()(那张表要保持单射,掺了 fp_02 就红)。
    _alias = _R.reanchor_alias()

    def _cur(mid):
        return _alias.get(mid, mid)

    # 🔴 在**入口处**把 id 前移一次,而不是只在查表时转:验证器本身也按 id 查
    #    「本轮清单里有没有这一发」,只转查表的话它会以「查不到定义指纹」拒掉,
    #    报文指向的处置(去补指纹)与真实原因(这一发改名了)**不一样**。
    #    历史记录记的是同一发,重锚只改身份不改它跑过这件事。
    recs = [dict(r, id=_cur(r["id"])) for r in recs]

    def _why(r):
        return _R.validate_record_run_meta(r, _R.QUICK_SCOPE[_cur(r["id"])],
                                           full_targets=_R.FULL)

    # 臂A:补齐 ⇒ 全过
    bad = [(r["id"], w) for r, w in
           ((r, _why(dict(r, mutation_fp=table[_cur(r["id"])],
                          mutation_fp_version=_R.FP_VERSION))) for r in recs) if w]
    assert not bad, f"补齐之后真产物仍被规则误杀:{bad[:3]}"

    # 臂B:只缺版本 ⇒ 全拒且点名版本
    b = [(r["id"], _why(dict(r, mutation_fp=table[_cur(r["id"])]))) for r in recs]
    assert all(w for _, w in b), "缺口径版本的旧记录被放行"
    off = [(i, w) for i, w in b if "没有 mutation_fp_version" not in w]
    assert not off, f"拒的理由没点名版本:{off[:3]}"

    # 臂C:给版本、缺指纹 ⇒ 全拒且点名指纹
    c = [(r["id"], _why(dict(r, mutation_fp_version=_R.FP_VERSION))) for r in recs]
    assert all(w for _, w in c), "没绑变异定义的旧记录被放行"
    off2 = [(i, w) for i, w in c if "没有 mutation_fp" not in w]
    assert not off2, f"拒的理由没点名指纹:{off2[:3]}"


def test_rmg_22_the_fresh_path_skips_what_it_cannot_yet_have():
    """新产物那一路手上**还没有裁定、也还没有 fp** —— 在那里要求它们,
    等于要求「还没发生的事已经写下来了」。所以 `stage="fresh"` 放过语义与 fp,
    结构面照验。

    🔴 [V9-B] 关键在于这个豁免必须由**调用点显式声明**。上一版拿
    「这条记录没有 verdict」当「这是新产物路」的代理 —— 代理信号顺带放过了
    另一样东西:**一条真缓存记录恰好没写 verdict**。它会被 `resume_done_ids`
    当「已跑过」收下,而 fp 校验整条跳过。第三臂就是钉这个缺口。
    """
    fresh = {"id": "X", "run_meta": _OK}
    assert _R.validate_record_run_meta(fresh, (TGT,), stage="fresh") is None
    assert _R.validate_record_run_meta({"id": "X", "run_meta": {}}, (TGT,),
                                       stage="fresh")
    # 🔴 第三臂:同一条记录走**缓存路**(默认 stage)必须被拒 —— 没有 fp。
    why = _R.validate_record_run_meta(fresh, (TGT,))
    assert why and "没有 mutation_fp" in why, (
        f"无裁定的缓存记录绕过了 fp 校验:{why!r}")
    # 第四臂:调用点乱报一个 stage 不许被当成豁免
    assert _R.validate_record_run_meta(fresh, (TGT,), stage="whatever")


def test_rmg_23_the_semantic_rules_live_in_one_place():
    """🔴 单点锁:语义规则只许有**一处**实现。

    抓的毒:「续跑那边补一份、终门这边再补一份」—— 两份必有一份没跟上。
    """
    src = RUNNER.read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    defs = [n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_semantic_mismatch"]
    assert len(defs) == 1, f"_semantic_mismatch 定义了 {len(defs)} 次"
    caller = _fn("validate_record_run_meta")
    calls = [n for n in ast.walk(caller) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_semantic_mismatch"]
    assert len(calls) == 2, f"唯一谓词里调语义函数 {len(calls)} 次(quick + full 各一)"
    # 两个入口都不许自己再判语义
    for entry in ("resume_done_ids", "hard_gate_rc"):
        body = ast.get_source_segment(src, _fn(entry)) or ""
        assert "_semantic_mismatch" not in body, f"{entry} 里自己判语义 = 第二份实现"


def test_rmg_24_full_run_meta_is_persisted_by_the_full_arm():
    """🔴 [P2-1] 全分母那一轮的 run_meta 必须**落盘**,不是瞬时检查一次就丢。

    不落盘的后果:带 `KILLED_BY_WIDER_DENOMINATOR + full_*` 的缓存,
    续跑与终门都无从复核它的全分母读数 —— 那句「更宽分母下被杀」证据面是空的。
    """
    src = RUNNER.read_text(encoding="utf-8-sig")
    fn = _fn("run_full_survivors")
    body = ast.get_source_segment(src, fn) or ""
    assert 'r["full_run_meta"]' in body, "全分母段没有落盘 full_run_meta"
    # 数据流:落的必须是这一轮真跑出来的 _rmeta,不是空字典占位
    assert '_rmeta.get("per_target"' in body or '_rmeta["per_target"]' in body, \
        "full_run_meta 不是从本轮 _rmeta 取的"


# ══════════════════════════════════════════════════════════════════════════
# V8-B 补:起跑前**内容级**残留自证(Deploy 建议 → 我核出的真缺口)
# ══════════════════════════════════════════════════════════════════════════
def test_rmg_25_the_startup_gate_is_content_based_not_marker_based():
    """🔴 内容毒一发 ⇒ 起跑必红;而**同一状态下标记法是绿的**。

    不等价证明**进判据,不进散文** —— 只写「两者不等价」是我说的,
    这条是机器每次跑都要重新证一遍的。

    差的那一格:还原被 ENOSPC 截断 / 进程被 kill 时,`.mutbak` 可能已经不在,
    而树是脏的。标记法看不见,内容法看得见。
    """
    import mutation_replay_common as RC
    roster = _R.load_v2()
    muts = [{"id": m["id"], "file": m["file"],
             "edits": [(p["from"], p["to"]) for p in m["pairs"]]} for m in roster]
    victim = muts[0]["file"]

    # ── 内容法:说 victim 与 HEAD 不同 ⇒ 必须停机,且点名「变异残留」──
    with pytest.raises(SystemExit) as exc:
        RC.assert_tree_is_unmutated(ROOT, muts,
                                    differs_from_head=lambda rel: rel == victim)
    msg = str(exc.value)
    assert "变异残留" in msg and victim in msg, msg[:400]

    # ── 阴性对照:全部与 HEAD 相同 ⇒ 放行(过严也是坏)──
    RC.assert_tree_is_unmutated(ROOT, muts, differs_from_head=lambda rel: False)

    # ── 标记法在**同一状态**下是绿的:树上本来就没有 .mutbak / .mutrestore ──
    stale = [p for p in ROOT.rglob("*.mutbak")] + [p for p in ROOT.rglob("*.mutrestore")]
    assert not stale, f"这台机器上有残留标记 {stale[:3]} —— 这条对照的前提不成立"
    _R._E._assert_no_concurrent_runner()      # 不抛 ⇒ 标记法判绿  # noqa: SLF001
    # 走到这里 = 同一状态下「内容法红、标记法绿」,两者**不等价**已被机器证过


def test_rmg_26_both_runner_entries_run_the_content_gate():
    """🔴 接线锁:两个入口(`_main_locked` / `run_full_survivors`)都要过这道闸。

    只接第一个入口不够:全分母复核是**独立进程**,上一趟若死在还原上,
    这一趟会在残留之上再打一层变异 —— 两层叠加的读数没有任何东西在证。
    """
    def _top_call(fn, pred):
        """函数体**顶层**语句里的调用 —— 不许藏在 if / try 之类里面。"""
        return [s for s in fn.body if isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Call) and pred(s.value.func)]

    def _unconditional_return_before(fn, lineno):
        """顶层出现在 `lineno` 之前的无条件 return/raise —— 它让后面整段不可达。"""
        return [s.lineno for s in fn.body
                if isinstance(s, (ast.Return, ast.Raise)) and s.lineno < lineno]

    for entry in ("_main_locked", "run_full_survivors"):
        fn = _fn(entry)
        calls = _top_call(fn, lambda f: isinstance(f, ast.Name)
                          and f.id == "_assert_no_mutation_residue")
        assert calls, f"{entry} 没过内容级自证(或调用不在函数体顶层)"
        dead = _unconditional_return_before(fn, calls[0].lineno)
        assert not dead, (f"{entry} 在第 {dead} 行就无条件返回了 —— "
                          "后面那道闸不可达")

    # 谓词只许有一处实现 —— 不许 runner 里再抄一份
    gate = _fn("_assert_no_mutation_residue")
    inner = _top_call(gate, lambda f: isinstance(f, ast.Attribute)
                      and f.attr == "assert_tree_is_unmutated")
    assert inner, "内容级自证没有落到共享谓词上(或调用不在函数体顶层)"

    # 🔴 [自打 U3 存活后补] **AST「调用存在」看不见不可达**。
    #    毒:在 `_RC.assert_tree_is_unmutated(...)` 前面插一个 `return` ——
    #    函数变成空转,而调用原封不动在 AST 里,「存在」判定照样绿。实测存活。
    #    断言型函数里出现 return,要么是死代码要么是旁路,一律拒。
    #    这是同族第四层:①存在≠有牙 ②有牙≠进终态 ③接线锁用包含判定
    #    ④**存在≠可达**。
    rets = [n.lineno for n in ast.walk(gate) if isinstance(n, ast.Return)]
    assert not rets, (f"_assert_no_mutation_residue 里有 return(第 {rets} 行)—— "
                      "断言型函数不该有 return;它让后面的闸不可达,"
                      "而『调用存在』看不见这件事")


def test_rmg_27_every_mutating_entry_point_consumes_the_same_predicate():
    """🔴 消费方全集锁:**机械枚举** scripts/ 下所有会就地改源的入口,
    它们必须都调同一个 `assert_tree_is_unmutated`。

    这条抓的是本次这个洞本身:谓词写好了、两个 replayer 都用了,
    **最重要的那个消费方(主 runner)没接** —— 而「谁该用它」这张表
    从来没被机械枚举过,所以漏掉的那个不会让任何判据变红。
    """
    want = {"mutation_runner_extsel_v2_2026_08_27.py",
            "mutation_replay_v3c_2026_08_28.py",
            "mutation_replay_v4c_2026_08_28.py"}
    got = set()
    files = sorted((ROOT / "scripts").glob("*.py"))
    assert len(files) >= 10, f"分母守卫:只扫到 {len(files)} 个文件,cwd 漂了?"
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                nm = (n.func.attr if isinstance(n.func, ast.Attribute)
                      else getattr(n.func, "id", None))
                if nm == "assert_tree_is_unmutated":
                    got.add(f.name)
    missing = sorted(want - got)
    assert not missing, f"这些会就地改源的入口没接内容级自证:{missing}"


def test_rmg_27_the_reanchor_alias_is_live_and_not_stale():
    """[#91 · 2026-09-06] `reanchor_alias()` 的接线锁 —— 新增函数不能只靠调用点。

    它把**历史冻结产物里的旧 id** 映到当前名册的新 id。三条不变量:
      · 非空 —— 空表意味着 rmg_21 的旧 id 又会 KeyError,而那是**静默**长出来的;
      · 每个旧 id 都**不在**当前名册里(在的话说明重锚没生效,别名是多余的谎);
      · 每个新 id 都**在**当前名册里(不在说明别名指向了不存在的发)。

    🔴 顺带钉死:别名**不许**掺进 `mutation_fp_by_id()` —— 那张表要保持单射,
       `test_fp_02_every_shot_in_the_roster_has_a_distinct_definition` 要求
       27 发定义两两不同。我第一版就是掺了别名,rmg_21 好了、fp_02 当场红。
    """
    alias = _R.reanchor_alias()
    assert alias, "别名表空了 —— 重锚裁定表被清空?rmg_21 的旧 id 会重新 KeyError"
    roster = set(_R.mutation_fp_by_id())
    stale = sorted(k for k in alias if k in roster)
    assert not stale, f"这些旧 id 仍在名册里,别名是多余的:{stale}(重锚没生效?)"
    dangling = sorted(v for v in alias.values() if v not in roster)
    assert not dangling, f"别名指向名册里没有的 id:{dangling}"
    fps = list(_R.mutation_fp_by_id().values())
    assert len(fps) == len(set(fps)), (
        "mutation_fp_by_id() 不再单射 —— 有人把别名掺进去了。别名属于消费方。")
