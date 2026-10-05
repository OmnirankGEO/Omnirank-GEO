# -*- coding: utf-8 -*-
"""Gate9 「skip 当成绿」一族的合同判据(Codex fix-of-fix3 P1-NEW-1 / P2-NEW-3 · 工单 V6-B)。

═══ 这一族的共同形状:**没跑** 与 **跑了没红** 同形 ═══

`tests/p03_settlement_2026_08_24` 与 `p03c_org_guards_2026_08_25` 的 conftest 里,
prod schema 路径变量的**默认值是 Windows 绝对路径**。容器里那个路径必不存在 ⇒
两包整包 `skip`。而当时的仪器:

  · `SCHEMA_PATH_ENVS` 只列了 2 个,**没有** P03 / P03C —— census 于是不要求落值,
    只在读到时打一句「用默认值」;
  · 退出条件 `if broken or live_bad or tot["red"]` —— **`skipped` 不在里面**;
  · liveness 一条 `CREATE DATABASE` 都没有时 `return bad`(空),
    「探针不可信」与「一切正常」用同一个返回值;
  · V2 的 `_run_targets` 只把 `red / passed / broken` 传出去,
    **`collected` 与 `skipped` 当场丢掉**。

四条叠起来的后果是 Codex 的那个反例:缺 P03 schema 时,`MUT-EXTE2-04` 的两条杀手判据
**被 skip**,该发零新增红 ⇒ 记 `SURVIVED_QUICK`;而整轮 `rc=0`、六数好看。
**「判据没跑」被读成了「判据没抓住」,再被读成「基线干净」。**

跑法::

    python -m pytest scripts/test_mutation_gate9_skip_schema_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
G9 = ROOT / "scripts" / "gate9_full_denominator_baseline.py"
V2 = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"


def _load(name: str, path: pathlib.Path, env: dict | None = None):
    saved = {k: os.environ.get(k) for k in (env or {})}
    try:
        for k, v in (env or {}).items():
            os.environ[k] = v
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        sys.path.insert(0, str(path.parent))
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_G = _load("g9_skip", G9)
_V = _load("v2_skip", V2)
_G_TREE = ast.parse(G9.read_text(encoding="utf-8-sig"))
_V_TREE = ast.parse(V2.read_text(encoding="utf-8-sig"))


def _fn(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"找不到 {name} —— 判据坐标烂了,不许当通过")


# ══ B-1 schema 夹具 env:机械枚举 + 强制显式落值 ═══════════════════════════
def test_s01_the_fixture_env_denominator_is_enumerated_not_hand_written():
    """🔴 分母由**枚举**给,不是手写的两项。

    手写那份漏了 P03 / P03C —— 而漏掉的那一项不会让任何判据变红,
    它只会让两个包在容器里静默整包 skip。
    """
    got = _G.schema_fixture_envs(ROOT, _G.GATE9_DENOMINATOR)
    must = {"P03_PROD_SCHEMA_SQL", "P03C_PROD_SCHEMA_SQL",
            "GEOIMG_PROD_SCHEMA_SQL", "DEFGEO_P0FIX_PROD_SCHEMA_SQL"}
    assert must <= got, f"枚举漏了:{sorted(must - got)}(实得 {sorted(got)})"


def test_s02_the_frozen_list_and_the_enumeration_reconcile_both_ways():
    """冻结清单与枚举**两向**对账 —— 单向只能证明一半。"""
    got = _G.schema_fixture_envs(ROOT, _G.GATE9_DENOMINATOR)
    frozen = set(_G.SCHEMA_PATH_ENVS)
    assert not (got - frozen), f"枚举到但清单里没有:{sorted(got - frozen)}"
    assert not (frozen - got), f"清单里有但没人读:{sorted(frozen - got)}"


def test_s03_an_unset_fixture_env_stops_the_run(tmp_path):
    """🔴 正样本 —— 点名规则:少设一个夹具 env ⇒ 第一个包跑之前停机。

    Codex 反例逐字:撤掉 `P03_PROD_SCHEMA_SQL` 之后 `--only p03_settlement`
    **不得再出 62/30/rc0**(那是「30 条被 skip」被读成干净的样子)。
    """
    f = tmp_path / "prod.sql"
    f.write_text("-- x\n", encoding="utf-8")
    env = {n: str(f) for n in _G.schema_fixture_envs(ROOT, _G.GATE9_DENOMINATOR)}
    env.pop("P03_PROD_SCHEMA_SQL")
    with pytest.raises(SystemExit) as exc:
        _G.assert_schema_fixtures(env, ROOT, _G.GATE9_DENOMINATOR)
    assert "P03_PROD_SCHEMA_SQL" in str(exc.value)


def test_s04_a_fixture_env_pointing_at_a_missing_file_stops_the_run(tmp_path):
    """设了但指向不存在的文件 —— 与没设同罪(容器里那些 Windows 绝对路径正是这一形)。"""
    env = {n: str(tmp_path / "nope.sql")
           for n in _G.schema_fixture_envs(ROOT, _G.GATE9_DENOMINATOR)}
    with pytest.raises(SystemExit) as exc:
        _G.assert_schema_fixtures(env, ROOT, _G.GATE9_DENOMINATOR)
    assert "不存在" in str(exc.value) or "is_file" in str(exc.value)


def test_s05_all_present_yields_sha256_provenance(tmp_path):
    f = tmp_path / "prod.sql"
    f.write_bytes(b"-- schema\n")
    names = _G.schema_fixture_envs(ROOT, _G.GATE9_DENOMINATOR)
    env = {n: str(f) for n in names}
    prov = _G.assert_schema_fixtures(env, ROOT, _G.GATE9_DENOMINATOR)
    assert set(prov) == names
    import hashlib

    want = hashlib.sha256(f.read_bytes()).hexdigest()
    for n in names:
        assert prov[n]["sha256"] == want, n
        assert prov[n]["path"] == str(f)


def test_s06_the_entrypoint_checks_fixtures_before_the_first_package_runs():
    """结构锁:位置就是这道门的意义 —— 晚一步,第一个包已经空跑过了。"""
    fn = _fn(_G_TREE, "_main_locked")

    def idx(callee):
        for i, st in enumerate(fn.body):
            for n in ast.walk(st):
                if isinstance(n, ast.Call) and (
                        getattr(n.func, "attr", None)
                        or getattr(n.func, "id", None)) == callee:
                    return i
        return -1

    gate = idx("assert_schema_fixtures")
    census = idx("env_census")
    assert gate >= 0, "_main_locked 没做夹具 env 强制"
    assert census >= 0, "找不到 env_census —— 判据坐标烂了"
    assert gate <= census + 1, f"夹具门在第 {gate} 条,太靠后(env_census 在第 {census} 条)"


# ══ B-2 非白名单 skip 一律红 ══════════════════════════════════════════════
WL = "tests/defensive_geo_w3_2026_08_21/test_wp6_structural_anchors.py::test_zero_diff_check_has_discriminating_power"


def test_s10_the_skip_whitelist_is_nodeid_exact_not_a_count():
    """白名单按 **nodeid**,不按数量 —— 「skip 1」放行的是"某一条",不是"任意一条"。"""
    assert isinstance(_G.EXPECTED_SKIP_NODEIDS, frozenset)
    assert _G.EXPECTED_SKIP_NODEIDS == frozenset({WL}), sorted(_G.EXPECTED_SKIP_NODEIDS)


def test_s11_an_unexpected_skip_is_reported():
    """🔴 正样本 —— 点名规则:白名单外的 skip 必须被点名。"""
    recs = [{"target": "tests/x", "skipped_nodes": ["tests/x/test_a.py::test_b"]}]
    bad = _G.unexpected_skips(recs)
    assert bad and "test_b" in bad[0]


def test_s12_the_whitelisted_skip_alone_is_accepted():
    assert _G.unexpected_skips([{"target": "tests/w3", "skipped_nodes": [WL]}]) == []


def test_s13_a_whole_package_skip_is_reported():
    """整包 skip(30 条)——正是缺 schema 时 p03 的样子。"""
    recs = [{"target": "tests/p03_settlement_2026_08_24",
             "skipped_nodes": [f"tests/p03_settlement_2026_08_24/t.py::t{i}"
                               for i in range(30)]}]
    assert len(_G.unexpected_skips(recs)) == 30


@pytest.mark.parametrize("kw", ["broken", "live_bad", "red", "skip_bad"])
def test_s14a_every_input_alone_drives_the_exit_code_to_one(kw):
    """🔴 正样本 —— 点名规则:四路输入**任一**非空都必须让 rc=1。

    [2026-08-29] 这条判据是被 Review 的毒**打红过之后**才有的:
    上一版我写的是 `assert "unexpected_skips" in body`(裸串)。
    把 `skip_bad` 从退出条件里拿掉后,赋值行还在、位置也还在 `return 0` 之前,
    209 条判据**全绿存活** —— 而 Codex P1-NEW-1 的正中心恰恰就是那一格。
    **验标记 ≠ 验接线**:同一谓词有牙,不代表它的结果进了退出码。
    """
    args = {"broken": [], "live_bad": [], "red": 0, "skip_bad": []}
    args[kw] = 1 if kw == "red" else ["x"]
    assert _G.baseline_rc(**args) == 1, f"{kw} 非空却拿到 rc 0"


def test_s14b_all_clean_is_the_only_zero():
    assert _G.baseline_rc(broken=[], live_bad=[], red=0, skip_bad=[]) == 0


def test_s14c_the_entrypoint_feeds_unexpected_skips_into_the_exit_code():
    """🔴 **数据流**锁,不是裸串:`baseline_rc(skip_bad=X)` 里的 X,
    必须能在同一函数里回溯到一次 `unexpected_skips(...)` 调用。"""
    fn = _fn(_G_TREE, "_main_locked")
    call = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and (
                getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "baseline_rc":
            call = n
            break
    assert call is not None, "_main_locked 没调 baseline_rc —— 裁决又内联回去了"
    kw = {k.arg: k.value for k in call.keywords}
    assert "skip_bad" in kw, f"baseline_rc 调用没传 skip_bad(实传 {sorted(kw)})"
    v = kw["skip_bad"]
    if isinstance(v, ast.Call):
        src_name = getattr(v.func, "attr", None) or getattr(v.func, "id", None)
    else:
        assert isinstance(v, ast.Name), f"skip_bad 传的是 {type(v).__name__},回溯不了"
        src_name = None
        for st in ast.walk(fn):
            if (isinstance(st, ast.Assign) and st.targets
                    and isinstance(st.targets[0], ast.Name)
                    and st.targets[0].id == v.id and isinstance(st.value, ast.Call)):
                src_name = (getattr(st.value.func, "attr", None)
                            or getattr(st.value.func, "id", None))
    assert src_name == "unexpected_skips", (
        f"skip_bad 回溯到的是 {src_name!r},不是 unexpected_skips —— "
        "闸有牙但没接进退出码")


def test_s14d_the_entrypoint_has_no_literal_return_that_bypasses_the_verdict():
    """任何一条出路都必须过 `baseline_rc` —— 裸 `return 0/1` 就是旁路。"""
    fn = _fn(_G_TREE, "_main_locked")
    lit = [n.lineno for n in ast.walk(fn)
           if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)]
    assert not lit, f"第 {lit} 行是字面量 return —— 绕过了最终裁决"


def test_s15_run_target_records_the_skipped_nodeids():
    """`skipped` 只有一个数字时,白名单无从比对 —— 必须记下**是哪几条**。"""
    fn = _fn(_G_TREE, "run_target")
    stores = {n.slice.value for n in ast.walk(fn)
              if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
              and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)}
    assert "skipped_nodes" in stores, (
        f"run_target 没往 rec 里写 skipped_nodes(实写 {sorted(stores)})—— "
        "只有一个 skip 数字时,白名单无从比对")


# ══ B-3 liveness 无 CREATE ⇒ 失败 ════════════════════════════════════════
def test_s20_no_create_at_all_is_a_failure_not_an_empty_bad():
    """🔴 正样本:一条 CREATE DATABASE 都没有 ⇒ bad 非空。

    「探针不可信」是**更坏**的消息,不是更好的 —— 它当时和「一切正常」共用返回值。
    """
    bad = _G.liveness_from_logs("", ["tests/p03_settlement_2026_08_24"])
    assert bad, "无 CREATE 却返回空 bad —— 查无证据被当成了证据表明没有"
    assert "探针" in " ".join(bad) or "CREATE" in " ".join(bad)


def test_s21_a_real_create_for_the_selected_package_passes():
    logs = ('2026-08-29 07:00:00.000 UTC [1] LOG:  statement: '
            'CREATE DATABASE "p03_deadbeef_test"\n')
    assert _G.liveness_from_logs(logs, ["tests/p03_settlement_2026_08_24"]) == []


def test_s22_a_selected_minting_package_without_its_own_create_is_bad():
    """反向对照:日志里有别的包的 CREATE,但**本包**没有 ⇒ 仍要红。"""
    logs = ('2026-08-29 07:00:00.000 UTC [1] LOG:  statement: '
            'CREATE DATABASE "p03c_dead_test"\n')
    bad = _G.liveness_from_logs(logs, ["tests/p03_settlement_2026_08_24"])
    assert bad and "p03_" in " ".join(bad)


# ══ B-5 --only 时 liveness 只核本轮选择的包 ═══════════════════════════════
def test_s30_only_mode_does_not_demand_creates_from_unselected_packages():
    """🔴 正样本 —— 点名规则:`--only p03c` 时不许要求 p03 / v3a / v5a 也建库。

    当时 `liveness` 遍历的是**整张** `MINTING_DB`,于是 `--only` 永远不可能干净
    (干跑实测:5 个未选包各报 ×0)。
    """
    logs = ('2026-08-29 07:00:00.000 UTC [1] LOG:  statement: '
            'CREATE DATABASE "p03c_abc_test"\n')
    assert _G.liveness_from_logs(logs, ["tests/p03c_org_guards_2026_08_25"]) == []


def test_s31_full_run_still_demands_every_minting_package():
    """不变臂:全 16 包时,少任何一个 minting 包的 CREATE 都要红。"""
    logs = ('2026-08-29 07:00:00.000 UTC [1] LOG:  statement: '
            'CREATE DATABASE "p03c_abc_test"\n')
    bad = _G.liveness_from_logs(logs, list(_G.GATE9_DENOMINATOR))
    names = " ".join(bad)
    assert bad and "p03_" in names and "defgeo_v5a_" in names


# ══ B-4 V2:预期杀手必须 collected 且未 skip 且真入红集 ═══════════════════
def test_s40_run_targets_keeps_collected_and_skipped():
    """结构锁:`_run_targets` 不许把 collected / skipped 丢掉。"""
    fn = _fn(_V_TREE, "_run_targets")
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            keys |= {k.value for k in n.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    for k in ("collected", "skipped"):
        assert k in keys, (
            f"_run_targets 没把 {k} 放进任何字典(实有 {sorted(keys)})—— "
            "丢掉它,「没跑」与「跑了没红」在上层就同形了")
    # 🔴 [2026-08-29 被自己咬] 原来这里是 `ast.walk(fn)` 取最后一个 Return ——
    #    我在 _run_targets 里加了嵌套函数 `_bare` 之后,拿到的是**嵌套函数的**
    #    return,判据当场假红。ast.walk 不跳子树,这条我 08-28 记过,
    #    这次长在自己的判据上。改成只认**本函数自己**的 return。
    def _own_returns(f):
        out = []
        stack = list(f.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                              ast.ClassDef)):
                continue                       # 不下降进嵌套定义
            if isinstance(n, ast.Return):
                out.append(n)
            stack.extend(ast.iter_child_nodes(n))
        return out

    ret = _own_returns(fn)
    assert ret, "_run_targets 自己一条 return 都没有 —— 判据坐标烂了"
    tup = [r for r in ret if isinstance(r.value, ast.Tuple)]
    assert tup and all(len(r.value.elts) == 4 for r in tup), (
        f"本函数的 return 不是 4 元组(实得 {[len(r.value.elts) for r in tup]})—— "
        "meta 没被交出去,记在内存里等于没记")


def test_s41_an_expected_killer_that_was_skipped_is_broken_not_survived():
    """🔴 正样本 —— Codex 那个反例的核心:杀手被 skip,**不许**记 SURVIVED。"""
    mut = {"expect_red_superset": ("test_e2_04_killer_a", "test_e2_04_killer_b")}
    status = {"test_e2_04_killer_a": "skipped", "test_e2_04_killer_b": "skipped"}
    v = _V.assert_expected_killers_ran("MUT-EXTE2-04", mut, status)
    assert v == "EXPECTED_KILLER_NOT_RUN", v
    assert v in _V.UNRESOLVED_VERDICTS, "这个裁定必须算「未解决」,否则硬门放它过"


def test_s42_an_expected_killer_absent_from_junit_is_also_broken():
    mut = {"expect_red_superset": ("test_e2_04_killer_a",)}
    assert _V.assert_expected_killers_ran("MUT-X", mut, {}) == "EXPECTED_KILLER_NOT_RUN"


@pytest.mark.parametrize("st", ["failed", "error"])
def test_s43_a_killer_that_really_went_red_passes(st):
    mut = {"expect_red_superset": ("test_e2_04_killer_a",)}
    assert _V.assert_expected_killers_ran("MUT-X", mut, {"test_e2_04_killer_a": st}) is None


def test_s44_a_killer_that_stayed_green_is_not_this_gates_business():
    """判别力边界:杀手**跑了但没红**是 `check_reanchor_expectations` 的事,
    这道门只管「有没有跑」。两道门各管一件,不许互相顶替。"""
    mut = {"expect_red_superset": ("test_e2_04_killer_a",)}
    assert _V.assert_expected_killers_ran("MUT-X", mut, {"test_e2_04_killer_a": "passed"}) is None


def test_s45_the_verdict_writer_calls_the_killer_gate():
    """分母 = **所有写裁定的函数**,机械反查(与 B-1-3 同一手法)。"""
    writers, callers = set(), set()
    for fn in [n for n in ast.walk(_V_TREE) if isinstance(n, ast.FunctionDef)]:
        for n in ast.walk(fn):
            if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                    and n.slice.value == "verdict" and isinstance(n.ctx, ast.Store)):
                writers.add(fn.name)
            if isinstance(n, ast.Call) and (
                    getattr(n.func, "attr", None)
                    or getattr(n.func, "id", None)) == "assert_expected_killers_ran":
                callers.add(fn.name)
    assert writers, "一处写裁定的都没扫到 —— 探针写废了"
    holes = sorted(writers - callers)
    assert not holes, f"这些函数写裁定却不核「杀手跑没跑」:{holes}"


# ══ 第三类「纯静态无库」的归属 —— 它极易变成垃圾桶 ═══════════════════════
def test_s50_the_no_db_class_is_pinned_and_每条都有理由():
    assert len(_G.NO_DB_PACKAGES) == _G.NO_DB_PACKAGES_SIZE
    assert all(v.strip() for v in _G.NO_DB_PACKAGES.values())
    assert set(_G.NO_DB_PACKAGES) <= set(_G.GATE9_DENOMINATOR), "有条目不在分母里"


def test_s51_the_three_classes_are_pairwise_disjoint_and_cover_the_denominator():
    """三类两两互斥,且并集 == 分母 —— 少一条这个豁免就是垃圾桶。"""
    ip, mt = set(_G.INPLACE_DB), set(_G.MINTING_DB)
    nd = set(_G.NO_DB_PACKAGES)
    assert not (ip & mt) and not (ip & nd) and not (mt & nd), "三类有重叠"
    assert (ip | mt | nd) == set(_G.GATE9_DENOMINATOR), (
        f"并集 ≠ 分母:分母多 {sorted(set(_G.GATE9_DENOMINATOR) - (ip|mt|nd))} · "
        f"三类多 {sorted((ip|mt|nd) - set(_G.GATE9_DENOMINATOR))}")


def test_s52_putting_a_db_touching_package_into_the_no_db_class_goes_red():
    """🔴 正样本 —— 点名规则:塞一个**真碰库**的包进去必须红。

    大小要**补齐到真实大小**,它证的才是**重叠闸**而不是大小闸(不然分不清哪一道拦的)。
    🔴 09-02:`NO_DB_PACKAGES_SIZE` 从 1 涨到 4 之后,这里仍只塞 1 个 ⇒ **大小闸先响**,
       报文是「大小 1 ≠ 钉死的 N」,而这条判据要的是「重叠」——它再也够不到重叠闸,
       却仍以「红了」的形式存在。**红对了,但红的是另一道闸**。
    """
    poisoned = dict(_G.NO_DB_PACKAGES)
    poisoned["tests/defgeo_v3a_2026_08_28"] = "假理由:它其实每 module 现建库"
    # 补齐大小:换掉一个真的,而不是净增 —— 否则大小闸又会先响
    for k in list(poisoned):
        if k != "tests/defgeo_v3a_2026_08_28" and len(poisoned) > _G.NO_DB_PACKAGES_SIZE:
            del poisoned[k]
    with pytest.raises(SystemExit) as exc:
        _G.assert_denominator_frozen(nodb=poisoned)
    assert "重叠" in str(exc.value), f"红了,但红的不是重叠闸:{exc.value}"


def test_s53_the_no_db_package_really_touches_no_db():
    """前提自证:被豁免的那个包**确实**不碰库(AST 实测,不是我说它不碰)。

    豁免的正当性全部建立在这个前提上;前提没人守,豁免就是一句声明。
    """
    common = _load("mrc_nodb", ROOT / "scripts" / "mutation_replay_common.py")
    for pkg in _G.NO_DB_PACKAGES:
        assert common.package_dsn_vars(ROOT, pkg) == set(), f"{pkg} 读了 DSN 变量"
        assert common.schema_rebuilder_files(ROOT, pkg) == [], f"{pkg} 有 DROP SCHEMA"
        assert _G.schema_fixture_envs(ROOT, [pkg]) == set(), f"{pkg} 读了夹具 schema"


def test_s54_liveness_skips_the_no_db_class():
    """结构锁:活性反证必须显式跳过第三类,而不是靠它恰好没有 INPLACE/MINTING 条目。"""
    fn = _fn(_G_TREE, "liveness")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "NO_DB_PACKAGES" in names, "liveness 没有显式跳过第三类"


def test_s55_every_run_targets_call_site_unpacks_four():
    """🔴 分母 = **调用点**(AST 机械枚举),不是函数定义。

    [2026-08-29 实跑咬到] 我把 `_run_targets` 从 3 元组改成 4 元组时,
    只用两次字符串替换改了两个调用点,**漏了第三个**(真库双打的重装那一处)——
    整轮 27 发在第 22 发 `ValueError: too many values to unpack` 崩掉。
    而 s40 只验了「定义返回 4 元组」,没验「每个调用点都解包 4 个」。
    **改函数契约要做调用点普查,不是两次 grep。**
    """
    tree = _V_TREE
    par = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            par[c] = n
    sites = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and (getattr(n.func, "attr", None)
                  or getattr(n.func, "id", None)) == "_run_targets"]
    assert len(sites) >= 4, f"只扫到 {len(sites)} 个调用点 —— 分母塌了,不是通过"
    bad = []
    for n in sites:
        up = par.get(n)
        k = (len(up.targets[0].elts)
             if isinstance(up, ast.Assign) and isinstance(up.targets[0], ast.Tuple)
             else -1)
        if k != 4:
            bad.append((n.lineno, k))
    assert not bad, f"这些调用点没解包 4 个:{bad}"


def test_s56_the_killer_gate_normalises_parametrised_ids():
    """🔴 实跑咬到:junit 里是 `name[param]`,登记的是裸名。

    不剥参数后缀的话,「跑了而且红了」会被读成「没跑」—— MUT-EXTE2-07b 就是
    这么被我自己的新门误判的。老门 `check_reanchor_expectations` 一直剥,
    新门当时没剥:**同一件事两处各写一遍,必有一处不一样**。
    """
    src = ast.get_source_segment(V2.read_text(encoding="utf-8-sig"),
                                 _fn(_V_TREE, "_run_targets")) or ""
    assert '.split("[")[0]' in src, "_run_targets 没剥参数后缀"
    mut = {"expect_red_superset": ("test_x",)}
    # 归并优先级:有一例红 ⇒ 算跑过;全 skipped ⇒ 算没跑
    assert _V.assert_expected_killers_ran("M", mut, {"test_x": "failed"}) is None
    assert _V.assert_expected_killers_ran(
        "M", mut, {"test_x": "skipped"}) == "EXPECTED_KILLER_NOT_RUN"


# ══ Codex fof4 P1:collected 必须是能与 junit 对账的整数 ═══════════════════
def test_s60_a_null_collected_is_rejected():
    """🔴 正样本 —— 点名规则:`collected` 是 None ⇒ 该发作废。

    产物实证(上一轮):33 条 run_meta 的 collected **全是 null** ——
    变异臂走 `run_target(collect=False)`,那一路根本不跑 `--collect-only`。
    而当时的判据只查「字典里有这个键名」:**只写不读的字段,锁住的是个空值**。
    """
    v = _V.assert_run_meta_sane("MUT-X", {"tests/p": {
        "collected": None, "passed": 1, "red": 0, "skipped": 0}}, ("tests/p",))
    assert v == "RUN_META_UNUSABLE", v
    assert v in _V.UNRESOLVED_VERDICTS, "这个裁定必须算「未解决」,否则硬门放它过"


def test_s60b_a_non_int_that_happens_to_add_up_is_still_rejected():
    """🔴 **判别力**样本 —— 专打 `isinstance` 那一支。

    自打毒时发现:把 `if not isinstance(c, int)` 改成 `if False`,
    **一条判据都不红** —— 因为 s60 喂的是 `None`,而 `None != parts` 让
    加法那一支照样拦住。也就是说 s60 过的是**另一条分支**,类型检查没人守。
    这不是纯冗余:`12.0 == 12` 为真,浮点在没有类型检查时会溜过去。
    (本仓判例:变异存活先分「冗余」还是「洞」——这次是洞。)
    """
    v = _V.assert_run_meta_sane("MUT-X", {"tests/p": {
        "collected": 12.0, "passed": 10, "red": 1, "skipped": 1}}, ("tests/p",))
    assert v == "RUN_META_UNUSABLE", f"浮点 collected 溜过去了:{v!r}"


def test_s61_a_collected_that_does_not_add_up_is_rejected():
    """两向的另一向:是整数还不够,得**和三分量对得上**。"""
    assert _V.assert_run_meta_sane("MUT-X", {"tests/p": {
        "collected": 99, "passed": 1, "red": 0, "skipped": 0}},
        ("tests/p",)) == "RUN_META_UNUSABLE"


def test_s62_a_consistent_run_meta_passes():
    assert _V.assert_run_meta_sane("MUT-X", {"tests/p": {
        "collected": 12, "passed": 10, "red": 1, "skipped": 1}},
        ("tests/p",)) is None


def test_s63_collected_is_derived_from_the_parsed_junit_not_from_collect_only():
    """结构锁:`_run_targets` 必须从 junit 的 `tests` 派生,
    不许再退回 `rec.get("collected")`(那一路在 collect=False 下恒 None)。"""
    # 🔴 这条判据第一版是**裸串**(`'rec.get("collected")' not in src`),
    #    结果被我**自己写的注释**触发 —— 注释里逐字引了那句原文。
    #    「散文触发裸串锁」本仓记过两次,写「别用裸串」的判据时我又用了裸串。
    #    改成 AST:只看**真实的调用**,注释与文档串自然出局。
    fn = _fn(_V_TREE, "_run_targets")
    gets = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get"
                and getattr(getattr(n.func, "value", None), "id", None) == "rec"
                and n.args and isinstance(n.args[0], ast.Constant)):
            gets.add(n.args[0].value)
    assert "collected" not in gets, (
        f"_run_targets 又去 rec.get('collected') 了 —— 变异臂那一路它恒 None(实读 {sorted(gets)})")
    assert "tests" in gets, f"没有从 junit 的 tests 派生(实读 {sorted(gets)})"


def test_s64_the_verdict_writers_call_the_run_meta_gate():
    """分母 = 所有写裁定的函数(AST 反查),与前两道门同一手法。"""
    writers, callers = set(), set()
    for fn in [n for n in ast.walk(_V_TREE) if isinstance(n, ast.FunctionDef)]:
        for n in ast.walk(fn):
            if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                    and n.slice.value == "verdict" and isinstance(n.ctx, ast.Store)):
                writers.add(fn.name)
            if isinstance(n, ast.Call) and (
                    getattr(n.func, "attr", None)
                    or getattr(n.func, "id", None)) == "assert_run_meta_sane":
                callers.add(fn.name)
    assert writers, "一处写裁定的都没扫到 —— 探针写废了"
    holes = sorted(writers - callers)
    assert not holes, f"这些函数写裁定却不核 run_meta:{holes}"


# ══ 产物必须自带树身份(Review 令 2026-08-29)═══════════════════════════
def test_s70_identity_carries_tip_tree_and_started_at():
    """真跑一次 `repo_identity()`,tip/tree 必须与本仓 git 对得上。"""
    import datetime as _dt

    t0 = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    #    🔴 [V9-B] 显式传 "host":判据跑在宿主上,而 `repo_identity` 的默认段是
    #    container,新加的反证闸(声称容器却不在容器里 ⇒ 停机)会**按设计**拒掉
    #    裸调用。这不是判据坏了,是它现在必须说清自己是哪一段。
    ident = _G.repo_identity("host")
    t1 = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    for k in ("tip", "tree", "started_at"):
        assert ident.get(k), f"identity 缺 {k}"
    # 🔴 [2026-08-30] 原来这里只验「非空」—— 又是「只写不读的字段」那一族:
    #    字段在、值的语义没人守。实测后果:同一轮 log 与产物的 started_at 差 27 分钟。
    #    现在验它**真的是 ISO-8601 UTC**,且落在本次调用的时间窗 [t0, t1] 内。
    got = _dt.datetime.strptime(ident["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=_dt.timezone.utc)
    assert t0 <= got <= t1, f"started_at={got} 不在本次调用窗 [{t0}, {t1}] 内"
    import subprocess

    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                         capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=str(ROOT),
                          capture_output=True, text=True).stdout.strip()
    assert ident["tip"] == tip, f"{ident['tip']} != {tip}"
    assert ident["tree"] == tree, "tree 与本仓对不上"
    assert "image_id" in ident, "缺 image_id 键(宿主上可以是 None,但键要在)"


def test_s71_a_payload_without_tip_is_refused():
    """🔴 正样本 —— 点名规则:产物没有 tip ⇒ 停机。

    实证由来:两个**不同的尖**(`d22a567f7` / `643bb6e7a`)跑出的 baseline.json
    **sha256 逐字节相同** —— 因为里面没有任何随尖变化的东西。
    「产物必须绑尖」这条我给 27 发做了(门③),给 17 包漏了。
    """
    with pytest.raises(SystemExit) as exc:
        _G.assert_identity_recorded({"identity": {"tree": "t"}})
    assert "identity.tip" in str(exc.value)


def test_s72_a_payload_without_tree_is_refused():
    with pytest.raises(SystemExit):
        _G.assert_identity_recorded({"identity": {"tip": "a" * 40}})


def test_s73_a_payload_bound_to_another_tip_is_refused():
    """🔴 两向:tip 在还不够,还得**是本轮这个**。"""
    with pytest.raises(SystemExit) as exc:
        _G.assert_identity_recorded({"identity": {"tip": "b" * 40, "tree": "t"}},
                                    expect_tip="a" * 40)
    assert "≠ 本轮 HEAD" in str(exc.value)


def test_s74_a_matching_payload_passes():
    _G.assert_identity_recorded({"identity": {"tip": "a" * 40, "tree": "t"}},
                                expect_tip="a" * 40)


def test_s75_the_entrypoint_records_identity_and_gates_on_it():
    """结构锁:①payload 里有 identity;②硬门在最终裁决**之前**调。"""
    fn = _fn(_G_TREE, "_main_locked")
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            keys |= {k.value for k in n.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    assert "identity" in keys, "baseline.json 的 payload 里没有 identity"

    def idx(callee):
        for i, st in enumerate(fn.body):
            for n in ast.walk(st):
                if isinstance(n, ast.Call) and (
                        getattr(n.func, "attr", None)
                        or getattr(n.func, "id", None)) == callee:
                    return i
        return -1

    g, r = idx("assert_identity_recorded"), idx("baseline_rc")
    assert g >= 0, "_main_locked 没调 assert_identity_recorded"
    assert r >= 0, "找不到 baseline_rc —— 判据坐标烂了"
    assert g < r, f"身份门在第 {g} 条、最终裁决在第 {r} 条 —— 门要排在前面"


def test_s76_the_image_id_must_come_from_outside_when_in_a_container(monkeypatch):
    """容器里跑却不给 `GATE9_IMAGE_ID` ⇒ 停机 ——
    容器无法可靠自报镜像 digest,所以它只能带外传入;不给等于这份证据没有环境身份。
    结构锁:那条判断必须同时看 `/.dockerenv` 与该 env。"""
    src = ast.get_source_segment(G9.read_text(encoding="utf-8-sig"),
                                 _fn(_G_TREE, "repo_identity")) or ""
    assert "/.dockerenv" in src and "GATE9_IMAGE_ID" in src, (
        "镜像身份没有「在容器里就必须带外给」这道判断")


def test_s77_identity_is_computed_exactly_once_per_round():
    """🔴 单源锁 —— 身份在 `_main_locked` 里只许算**一次**。

    [2026-08-30 实测] 原来 `print_transport_identity` 自己也调一次、
    payload 与硬门又各调一次 ⇒ 同一轮里三个 `started_at`,
    log 记 07:02:41Z 而产物记 07:29:26Z,**差 27 分钟**。
    字段名叫 started_at,记的却是「payload 写盘那一刻」——
    同一份证据里两个时间戳互相打架,是自立字段带病。
    """
    fn = _fn(_G_TREE, "_main_locked")
    n = [x.lineno for x in ast.walk(fn) if isinstance(x, ast.Call)
         and (getattr(x.func, "attr", None)
              or getattr(x.func, "id", None)) == "repo_identity"]
    assert len(n) == 1, f"_main_locked 里调了 {len(n)} 次 repo_identity(){n} —— 必须恰 1 次"
    # 其余用到身份的地方必须**收参数**,不许自己去取
    pt = _fn(_G_TREE, "print_transport_identity")
    assert [a.arg for a in pt.args.args] == ["identity"], (
        f"print_transport_identity 的形参是 {[a.arg for a in pt.args.args]} —— 它必须收身份")
    assert not [x for x in ast.walk(pt) if isinstance(x, ast.Call)
                and (getattr(x.func, "attr", None)
                     or getattr(x.func, "id", None)) == "repo_identity"], (
        "print_transport_identity 又自己去取身份了 —— 两处各取一个,时间戳必然打架")


def test_s78_the_printed_line_carries_exactly_the_recorded_started_at(capsys):
    """行为向:打印出来的那一行,`started_at` 必须与传进去的**逐字相同**。

    这条守的是「log 与产物两向比」的**单元级**那一半;
    整轮级的两向比在跑完之后对真 log + baseline.json 做(交付文里给)。
    """
    ident = {"tip": "a" * 40, "tree": "b" * 40, "image_id": "sha256:cafe",
             "started_at": "2026-08-30T01:02:03Z"}
    capsys.readouterr()
    _G.print_transport_identity(ident)
    out = capsys.readouterr().out
    assert "2026-08-30T01:02:03Z" in out, "打印的时间戳与传入的不一致"
    assert ident["tip"] in out and ident["tree"] in out and "sha256:cafe" in out


def test_s79_a_payload_identity_that_differs_from_what_was_printed_is_refused():
    """🔴 正样本 —— 点名规则:tip 相同但**别的字段被改过**,照样要拦。

    [2026-08-30 自打毒挖到的洞] 我先写的门只比 `tip`,于是「写盘前把 started_at
    覆盖成此刻」那一形**存活** —— 而那正是原 bug 的形状(log 与产物差 27 分钟)。
    毒存活时我没当成毒废:先看毒是不是 no-op(第一发 `sleep(0)` 确实是废毒),
    重造一发真毒之后仍存活 ⇒ **是洞**。现在硬门比**整份**。
    """
    printed = {"tip": "a" * 40, "tree": "b" * 40, "image_id": None,
               "started_at": "2026-08-30T01:00:00Z"}
    written = dict(printed, started_at="2026-08-30T01:27:00Z")   # 只有时间被覆盖
    with pytest.raises(SystemExit) as exc:
        _G.assert_identity_recorded({"identity": written}, expect_tip="a" * 40,
                                    expect_identity=printed)
    assert "started_at" in str(exc.value) and "自相矛盾" in str(exc.value)


def test_s7a_an_identical_identity_passes():
    ident = {"tip": "a" * 40, "tree": "b" * 40, "image_id": None,
             "started_at": "2026-08-30T01:00:00Z"}
    _G.assert_identity_recorded({"identity": dict(ident)}, expect_tip="a" * 40,
                                expect_identity=ident)


def test_s7b_the_printer_returns_what_it_printed():
    """门的前提:打印函数必须**把它打印的那份交出来**,否则整份比无从谈起。"""
    ident = {"tip": "c" * 40, "tree": "d" * 40, "image_id": "sha256:x",
             "started_at": "2026-08-30T02:00:00Z"}
    assert _G.print_transport_identity(ident) == ident


def test_s7c_identity_is_not_rebound_after_it_has_been_printed():
    """🔴 数据流锁 —— 打印之后不许再动 `_identity`。

    [2026-08-30] 自打毒:「写盘前把 started_at 覆盖成此刻」(原 bug 的形状)
    在单元级**存活**。查因不是门漏了(毒④ 证明门有牙),而是
    **没有任何单元判据会执行 `_main_locked`** —— 那一形只有真跑时才撞上硬门。
    所以补这条结构锁:它在**源码层**盯住同一件事,单元级就能红。
    「运行期有门」和「单元级有判据」是两件事,两样都要。
    """
    fn = _fn(_G_TREE, "_main_locked")
    printed_at = None
    for i, st in enumerate(fn.body):
        for n in ast.walk(st):
            if (isinstance(n, ast.Call) and (getattr(n.func, "attr", None)
                    or getattr(n.func, "id", None)) == "print_transport_identity"):
                printed_at = i
                break
        if printed_at is not None:
            break
    assert printed_at is not None, "_main_locked 没调 print_transport_identity"
    rebinds = []
    for st in fn.body[printed_at + 1:]:
        for n in ast.walk(st):
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                    and n.id == "_identity"):
                rebinds.append(n.lineno)
    assert not rebinds, (
        f"第 {rebinds} 行在打印之后又重绑了 _identity —— "
        "打印出去的和写进产物的会是两份,同一轮证据自相矛盾")
