# -*- coding: utf-8 -*-
"""复放器**硬门**的合同判据(Codex fix-of-fix2 P1-7 / P1-8 / P2-4 · 工单 V5-B B-2~B-4)。

═══ 病根不是"V3-C 少了两道闸",是"闸有两份" ═══

V4-C 是照着 V3-C 抄出来的,抄的时候 V4-C 补了基线硬门与逐变量 DSN 枚举,
V3-C 没跟上。于是同一条谓词在两个文件里各活一份,**必有一份没人验**。
所以本单把闸搬进 ``scripts/mutation_replay_common.py``,这里的判据用
**AST 机械枚举** ``scripts/mutation_replay_*.py`` —— 谁自己再写一份就红,
将来第三支复放器自动落进分母,不需要谁"记得"。

═══ 起跑前自证的两个洞(实测,不是读代码猜的)═══

在 tmp 上把 V4-C 的靶文件全拷一份、造出残留,拿**当时**的 V4-C 判:

    [P1-8② anchor-preserving 残留] MUT-V4C-C1b 的替换把锚行重新插在末尾
      → 当前 V4-C 判定:干净,放行
        ⇒ 拿污染树当基线,并把污染版 copy2 存成 .mutbak(备份本身就脏了)

    [P2-4 删除型残留] MUT-V4C-C2a 的替换是空串,`repl and repl in raw` 恒假
      → 当前 V4-C 判定:【锚点过期】…… 含【变异残留】=False
        ⇒ 会安全停机,但给出的修复动作是错的(把人引去找不存在的过期)

两个洞被**同一个信号**关掉:与 HEAD 逐字节比。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_replayer_hardgate_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import shutil
import sys


import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COMMON = SCRIPTS / "mutation_replay_common.py"

#: 🔴 冻结豁免集 —— 每一条都写明**理由**,大小钉死。
#:    豁免不写理由就是"我记得这个不用管",而"记得"迟早随交接消失。
LEGACY_EXEMPT: dict[str, str] = {
    "mutation_replay_common.py":
        "它**就是**公共谓词模块本身,不是复放器(没有 PACKAGES / _main_locked)",
    "mutation_replay_e3tail_2026_08_27.py":
        "E3 尾单复放器 · 本单(V5-B)只收编 V3-C / V4-C;收编排进 post-train 队列",
    "mutation_replay_extsel_e3_2026_08_27.py":
        "外选 E3 复放器 · 同上,本单未收编,排进 post-train 队列",
}
LEGACY_EXEMPT_SIZE = 3

#: 一条谓词只许有一处实现 —— 复放器里出现这些**同名定义**就是又抄了一份。
SHARED_PREDICATES = frozenset({
    "classify_target_state", "assert_tree_is_unmutated", "_assert_tree_is_unmutated",
    "baseline_offenders", "assert_baseline_clean",
    "package_dsn_vars", "_package_dsn_vars",
    "assert_every_dsn_var_is_declared", "_assert_every_dsn_var_is_declared",
    "assert_mutation_arm_trustworthy", "dsn_env",
})


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    return mod


def _common():
    assert COMMON.is_file(), (
        "公共谓词模块 scripts/mutation_replay_common.py 还不在 —— "
        "两支复放器各写一份闸,正是这次 P1-7/P1-8 的病根")
    return _load("mrcommon", COMMON)


def census() -> list[pathlib.Path]:
    """分母 = ``scripts/mutation_replay_*.py`` **机械枚举** − 冻结豁免。"""
    return sorted(p for p in SCRIPTS.glob("mutation_replay_*.py")
                  if p.name not in LEGACY_EXEMPT)


def _tree(p: pathlib.Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8-sig"))


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"找不到 {name} —— 判据坐标烂了,不许当通过")


def _idx_call(body: list, callee: str) -> int:
    for i, stmt in enumerate(body):
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call):
                nm = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if nm == callee:
                    return i
    return -1


def _idx_for_over(body: list, pred) -> int:
    for i, stmt in enumerate(body):
        for n in ast.walk(stmt):
            if isinstance(n, ast.For) and pred(n.iter):
                return i
    return -1


CENSUS = census()
IDS = [p.name for p in CENSUS]


# ══ 分母本身先立住 ═══════════════════════════════════════════════════════
def test_c00_the_census_is_not_empty_and_the_exemptions_are_pinned():
    assert CENSUS, "一支复放器都没扫到 —— 分母为 0 不是通过,是探针写废了"
    on_disk = {p.name for p in SCRIPTS.glob("mutation_replay_*.py")}
    stale = sorted(set(LEGACY_EXEMPT) - on_disk)
    assert not stale, f"豁免集里有盘上不存在的文件:{stale} —— 过期的豁免会悄悄放行别的"
    assert len(LEGACY_EXEMPT) == LEGACY_EXEMPT_SIZE, (
        f"豁免集大小 {len(LEGACY_EXEMPT)} ≠ 钉死的 {LEGACY_EXEMPT_SIZE} —— "
        "钉大小是为了让「顺手加一条豁免」必须过一次人")
    assert all(v.strip() for v in LEGACY_EXEMPT.values()), "有豁免没写理由"


# ══ B-3② / B-4 · 起跑前三态 ══════════════════════════════════════════════
def test_c01_an_anchor_preserving_leftover_is_residue_not_clean():
    """🔴 正样本 —— 点名规则:替换把锚原样留下,树照样是脏的。

    实测(本单动手前):`当前 V4-C 判定:干净,放行`。
    """
    c = _common()
    assert c.classify_target_state(differs_from_head=True, anchor_hits=1) == c.RESIDUE


def test_c02_a_deletion_type_leftover_is_residue_not_expired():
    """🔴 正样本 —— 点名规则:空串替换的残留,不许报成"锚点过期"。

    实测(本单动手前):`含【变异残留】=False · 含【锚点过期】=True`。
    """
    c = _common()
    assert c.classify_target_state(differs_from_head=True, anchor_hits=0) == c.RESIDUE


def test_c03_a_real_anchor_expiry_is_still_reported_as_expired():
    """反向对照:被测代码真变了(与 HEAD 一致、锚不在)⇒ 仍要报过期。

    只把所有异常都归成"残留"是另一种坏:那会把"该退役改锚"的发
    引去找不存在的残留 —— 判别力两向都要在。
    """
    c = _common()
    assert c.classify_target_state(differs_from_head=False, anchor_hits=0) == c.EXPIRED


def test_c04_a_clean_target_is_clean():
    c = _common()
    assert c.classify_target_state(differs_from_head=False, anchor_hits=1) == c.CLEAN


def test_c05_a_polluted_tree_stops_the_run_before_any_backup(tmp_path):
    """行为向:把 anchor-preserving 的残留摆在 tmp 树上,起跑前自证必须停。"""
    c = _common()
    rel = "svc/target.py"
    (tmp_path / "svc").mkdir()
    (tmp_path / rel).write_text("A\nKEEP\nB\n", encoding="utf-8", newline="")
    muts = [{"id": "MUT-X", "file": rel, "edits": [("KEEP\n", "NEW\nKEEP\n")]}]
    with pytest.raises(SystemExit) as exc:
        c.assert_tree_is_unmutated(tmp_path, muts, differs_from_head=lambda r: True)
    msg = str(exc.value)
    assert "变异残留" in msg and "锚点过期" not in msg


def test_c06_a_clean_tree_passes(tmp_path):
    c = _common()
    rel = "svc/target.py"
    (tmp_path / "svc").mkdir()
    (tmp_path / rel).write_text("A\nKEEP\nB\n", encoding="utf-8", newline="")
    muts = [{"id": "MUT-X", "file": rel, "edits": [("KEEP\n", "NEW\n")]}]
    c.assert_tree_is_unmutated(tmp_path, muts, differs_from_head=lambda r: False)


def test_c07_the_real_v4c_mutation_set_is_correctly_classified_when_polluted(tmp_path):
    """把 V4-C **真实的**两发残留摆出来,新闸必须都判成残留。

    这一条不用手编的假发 —— 用的就是产出上面那两行实测的同两发
    (C1b = anchor-preserving,C2a = 空串删除)。
    """
    c = _common()
    v4c = _load("v4c_probe", SCRIPTS / "mutation_replay_v4c_2026_08_28.py")
    muts = list(v4c.FIX_MUTATIONS) + list(v4c.SELF_MUTATIONS)
    for mid in ("MUT-V4C-C1b", "MUT-V4C-C2a"):
        stage = tmp_path / mid
        touched = set()
        for mut in muts:
            dst = stage / mut["file"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(ROOT / mut["file"], dst)
        target = next(m for m in muts if m["id"] == mid)
        p = stage / target["file"]
        raw = p.read_text(encoding="utf-8-sig")
        for anchor, repl in target["edits"]:
            assert raw.count(anchor) == 1, f"{mid} 的锚在真树上不唯一 —— 判据前提烂了"
            raw = raw.replace(anchor, repl, 1)
        p.write_text(raw, encoding="utf-8", newline="")
        touched.add(target["file"])
        with pytest.raises(SystemExit) as exc:
            c.assert_tree_is_unmutated(stage, muts,
                                       differs_from_head=lambda r: r in touched)
        assert "变异残留" in str(exc.value), f"{mid} 的残留没被判成残留"


# ══ B-2 · 基线硬门 ═══════════════════════════════════════════════════════
@pytest.mark.parametrize("rc,red", [(5, set()), (2, set()), (1, {"t"}), (0, {"t"})])
def test_c10_a_dirty_baseline_is_flagged(rc, red):
    """🔴 正样本:红基线**和**非零 rc 都算脏 —— rc=5/2 时 junit 里一条红都没有。"""
    c = _common()
    assert c.baseline_offenders({"w2": (rc, red)})


def test_c11_a_clean_baseline_is_not_flagged():
    c = _common()
    assert c.baseline_offenders({"w2": (0, set()), "e3": (0, set())}) == []


def test_c12_a_dirty_baseline_raises():
    c = _common()
    with pytest.raises(SystemExit):
        c.assert_baseline_clean({"w2": (5, set())})


@pytest.mark.parametrize("name", IDS)
def test_c13_every_replayer_stops_on_a_dirty_baseline_before_the_first_mutation(name):
    """结构锁 —— 分母 = 机械枚举的全部复放器,不是"V3-C 和 V4-C"两个手写名字。

    Codex 反例:`rc=5, red=∅` 与 `rc=1, red={baseline_red}` 两种情形下,
    V3-C 都**已经进到第一笔 artifact**。位置就是这条门的全部意义。
    """
    tree = _tree(SCRIPTS / name)
    fn = _fn(tree, "_main_locked")
    gate = _idx_call(fn.body, "assert_baseline_clean")
    loop = _idx_for_over(fn.body, lambda it: isinstance(it, ast.Name) and it.id == "queue")
    assert gate >= 0, f"{name} 没调 assert_baseline_clean"
    assert loop >= 0, f"{name} 找不到变异队列循环 —— 判据坐标烂了"
    assert gate < loop, f"{name}:基线闸在第 {gate} 条、变异循环在第 {loop} 条"


@pytest.mark.parametrize("name", IDS)
def test_c14_every_replayer_checks_its_dsn_vars_before_the_baseline_loop(name):
    tree = _tree(SCRIPTS / name)
    fn = _fn(tree, "_main_locked")
    gate = _idx_call(fn.body, "assert_every_dsn_var_is_declared")
    base = _idx_for_over(fn.body,
                         lambda it: isinstance(it, ast.Name) and it.id == "PACKAGES")
    assert gate >= 0, f"{name} 没做逐变量 DSN 机械枚举"
    assert base >= 0, f"{name} 找不到基线循环 —— 判据坐标烂了"
    assert gate < base, f"{name}:DSN 自证必须排在第一次真跑之前"


@pytest.mark.parametrize("name", IDS)
def test_c15_every_replayer_self_checks_the_tree_before_any_backup(name):
    tree = _tree(SCRIPTS / name)
    fn = _fn(tree, "_main_locked")
    gate = _idx_call(fn.body, "assert_tree_is_unmutated")
    loop = _idx_for_over(fn.body, lambda it: isinstance(it, ast.Name) and it.id == "queue")
    assert gate >= 0, f"{name} 起跑前没做树自证"
    assert loop >= 0, f"{name} 找不到变异队列循环"
    assert gate < loop, (
        f"{name}:树自证必须排在变异循环之前 —— 循环里第一件事就是 "
        "shutil.copy2 存 .mutbak,晚一步就会把污染版存成备份")


# ══ B-3① · 变异臂 rc / 用例数 ════════════════════════════════════════════
@pytest.mark.parametrize("rc", [2, 3, 4, 5, -1])
def test_c20_an_untrustworthy_rc_blocks_the_red_set_verdict(rc):
    """🔴 正样本 —— 点名规则:rc=2 配一份"只含预期红集"的 partial junit,
    旧代码照样报「红集精确」并返回 0。"""
    c = _common()
    with pytest.raises(SystemExit) as exc:
        c.assert_mutation_arm_trustworthy("MUT-X", "w4", rc=rc, total=20,
                                          baseline_total=20)
    assert str(rc) in str(exc.value)


def test_c21_a_partial_junit_blocks_even_when_rc_is_one():
    """只看 rc 挡不住"跑了一半就崩":rc=1 是合法的红臂,而条数少了 9 条。"""
    c = _common()
    with pytest.raises(SystemExit):
        c.assert_mutation_arm_trustworthy("MUT-X", "w4", rc=1, total=11,
                                          baseline_total=20)


@pytest.mark.parametrize("rc", [0, 1])
def test_c22_a_normal_arm_passes(rc):
    c = _common()
    c.assert_mutation_arm_trustworthy("MUT-X", "w4", rc=rc, total=20,
                                      baseline_total=20)


@pytest.mark.parametrize("name", IDS)
def test_c23_every_replayer_adjudicates_the_arm_before_subtracting_the_baseline(name):
    """结构锁:``fresh = red - baseline[...]`` 之前必须先判臂可不可信。

    顺序反过来 = 先拿不可信的读数算完差值,再"顺便"检查一下 —— 而红集裁定
    已经发生了。
    """
    tree = _tree(SCRIPTS / name)
    fn = _fn(tree, "_main_locked")
    inner = None
    for n in ast.walk(fn):
        if (isinstance(n, ast.For) and isinstance(n.iter, ast.Subscript)
                and isinstance(n.iter.value, ast.Name) and n.iter.value.id == "mut"):
            inner = n
            break
    assert inner is not None, f"{name} 找不到 `for pkg in mut[...]` 内层循环"
    gate = _idx_call(inner.body, "assert_mutation_arm_trustworthy")
    fresh = -1
    for i, stmt in enumerate(inner.body):
        if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "fresh" for t in stmt.targets):
            fresh = i
            break
    assert gate >= 0, f"{name} 变异臂没做 rc/条数裁定"
    assert fresh >= 0, f"{name} 找不到 fresh 的赋值 —— 判据坐标烂了"
    assert gate < fresh, f"{name}:臂可信度必须判在红集差值之前"


# ══ B-2 · DSN 逐变量 / 不共库 ════════════════════════════════════════════
@pytest.mark.parametrize("name", IDS)
def test_c30_every_replayer_feeds_every_dsn_var_the_package_reads(name):
    """行为向:拿**真实**包树跑一遍机械枚举 —— 漏喂 / 共库当场停。

    V3-C 的 w4 把 ``DEFGEO_W4_TEST_DB_URL`` 与 ``DEFGEO_W4_HTTP_DB_URL``
    指到同一个库,两个 fixture 各自 ``DROP SCHEMA public CASCADE``。
    """
    c = _common()
    mod = _load(f"census_{name[:-3]}", SCRIPTS / name)
    c.assert_every_dsn_var_is_declared(ROOT, mod.PACKAGES, 55495, label=name)


def test_c31_the_same_package_gets_the_same_dsn_shape_in_every_replayer():
    """🔴 跨复放器对账:同一个包在两支里必须是**同一个分法**(库名不比,结构比)。

    这条抓的正是本次的形状:V4-C 说 w4 要两个库,V3-C 说一个 ——
    「同一谓词写两处」的配置版。
    """
    c = _common()
    shapes: dict[str, dict[str, object]] = {}
    for name in IDS:
        mod = _load(f"shape_{name[:-3]}", SCRIPTS / name)
        for pkg, spec in mod.PACKAGES.items():
            shapes.setdefault(spec["path"], {})[name] = (
                frozenset(c.env_vars_of(spec)), c.env_partition_of(spec))
    shared = {p: v for p, v in shapes.items() if len(v) > 1}
    assert shared, "没有任何包被两支以上复放器共用 —— 这条对账在空转,先查探针"
    bad = {p: v for p, v in shared.items() if len({x for x in v.values()}) > 1}
    assert not bad, (
        "同一个包在不同复放器里的 DSN 形状不一致:"
        + "; ".join(f"{p}: " + " vs ".join(f"{n}={sorted(map(sorted, part))}"
                                           for n, (_vars, part) in v.items())
                    for p, v in bad.items()))


def test_c32_a_package_with_two_schema_rebuilders_must_not_share_one_db():
    """闸自己的正样本:两个 DROP SCHEMA 文件 + 一个库 ⇒ 必须红。"""
    c = _common()
    spec = {"path": "tests/defensive_geo_w4_2026_08_22", "db": "x",
            "env": ("DEFGEO_W4_TEST_DB_URL", "DEFGEO_W4_HTTP_DB_URL",
                    "TEST_DATABASE_URL")}
    assert len(c.schema_rebuilder_files(ROOT, spec["path"])) > 1, (
        "这条正样本的前提是该包**真有**多个 schema 重建者,前提没了就换个包")
    with pytest.raises(SystemExit) as exc:
        c.assert_every_dsn_var_is_declared(ROOT, {"w4": spec}, 55495)
    assert "共库" in str(exc.value)


# ══ 一条谓词一处实现 ═════════════════════════════════════════════════════
@pytest.mark.parametrize("name", IDS)
def test_c40_no_replayer_defines_its_own_copy_of_a_shared_predicate(name):
    """🔴 这条才是病根的锁:抄一份闸出来 ⇒ 必有一份没人验。"""
    tree = _tree(SCRIPTS / name)
    own = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    dup = sorted(own & SHARED_PREDICATES)
    assert not dup, (
        f"{name} 自己又定义了一份:{dup} —— 公共谓词只许在 "
        "scripts/mutation_replay_common.py 里有一处实现")


@pytest.mark.parametrize("name", IDS)
def test_c41_every_replayer_imports_the_common_module(name):
    src = (SCRIPTS / name).read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    hit = any(isinstance(n, ast.ImportFrom) and n.module == "mutation_replay_common"
              for n in ast.walk(tree))
    hit = hit or any(isinstance(n, ast.Import)
                     and any(a.name == "mutation_replay_common" for a in n.names)
                     for n in ast.walk(tree))
    assert hit, f"{name} 没有 import 公共谓词模块"


@pytest.mark.parametrize("name", IDS)
def test_c42_every_replayer_builds_its_env_through_the_shared_helper(name):
    """P1-7 的原址锁:``for key in spec["env"]: env[key] = dsn`` 那一手不许再出现。"""
    tree = _tree(SCRIPTS / name)
    fn = _fn(tree, "_run")
    assert _idx_call(fn.body, "dsn_env") >= 0, (
        f"{name} 的 _run 没走公共的 dsn_env —— 自己拼 env 就会再出现"
        "「一个 DSN 灌所有 key」")
