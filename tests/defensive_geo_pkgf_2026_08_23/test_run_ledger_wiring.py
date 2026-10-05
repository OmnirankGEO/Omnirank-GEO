"""包F ① 接线锁 —— 证明 attempt 账本**真的被现役执行链调用**。

一期的教训就在这个文件的存在理由里:``attempt_ledger`` 三个函数写完了、
文档写透了、判据也有,**全仓零调用者**。而"我们接了"这句话在复审里
没有任何证明力(本仓记过:仅测试调用 = 死函数 = NO-GO)。

所以这里全部从**真源码 AST** 机械导出,不手抄:

* 调用者集合双向判(多一个 / 少一个都红);
* 六个接线函数**逐个**都必须有生产调用点 —— 分母来自
  ``run_ledger_bridge.WIRED_ENTRYPOINTS``,不是这里写死的清单;
* 每个 chokepoint **逐个**点名验证(不是"至少有一处接了就算过")。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

ROOT = Path(__file__).resolve().parents[2]

#: 普查面 = 生产代码目录。**不含 tests/** ——
#: 「只有测试在调」正是死函数的标准形态。
_PRODUCTION_DIRS = ("db", "api", "services", "workflows", "agents", "tools")

#: 🔴 逐个 chokepoint → 它必须调的那个接线函数。
#:    这张表是**声称**;下面每条判据都去真源码里核它。
#:
#: 🔴 [R4] 这张表的**键域**曾经是纯手写的,那是一个假绿源:
#:    删掉一个键(Review 毒C 删 ``reserve_monitoring_cell_retry``)
#:    ⇒ wiring 22 条全绿。因为值域机械取自 ``BRIDGE.WIRED_ENTRYPOINTS``、
#:    ``open_for_claim`` 由 claim 那个键继续供值,分母自证不掉;而三条 meta 锁
#:    都在遍历**这张表的键**,键没了它们同盲。
#:    现在键域由 ``test_expected_hooks_key_domain_equals_the_production_census``
#:    与生产侧 AST 普查**双向相等**钉死:删键红、幽灵键红、生产侧新增调用方
#:    未登记也红。
#:
#: 🔴 R4 补登记的两项:``recover_abandoned_monitoring_task_execution`` 与
#:    ``revoke_monitoring_task_coverage_for_organization_refund`` ——
#:    它们是 **R2 F-3a/F-3b 我自己接的线,却一直没登记进本表**。
#:    后果不只是少一条结构锁:它们同时**不在 R3 meta 锁的分母里**,
#:    所以"必须有行为判据"那一条对它们从来没生效过。
#:    机械键域一上,当场把这两项翻出来 —— 手写分母漏掉的那一项
#:    不会让任何判据变红(本仓记过),这就是又一例。
EXPECTED_HOOKS: dict[str, tuple[str, ...]] = {
    "claim_monitoring_run_cell": ("open_for_claim",),
    "reserve_monitoring_cell_retry": ("open_for_claim",),
    "save_monitoring_result": ("close_for_result",),
    "finish_monitoring_cell_error": ("close_for_error",),
    "create_monitoring_run_cells": ("record_skip_for_plan",),
    "_recover_expired_monitoring_cells": ("reclaim_orphans",),
    "decide_monitoring_identity_review": ("close_for_human_resolution",),
    # [R2 F-3] 每小时无条件 cron 的两条收割路径,R4 补登记
    # [工单 C-3 · 2026-08-25] 这两条**各自都要接两跳**,不是一跳:
    #   · reclaim_orphans          → 收「已经在飞」的 attempt(账本有行);
    #   · close_unattempted_for_cells → 闭合「从未形成 attempt」的 queued 格
    #     (账本零行 ⇒ 上一跳命中零行却看起来干完了)。
    # 两个函数作用域互补且不重叠,所以必须**都**在。只写一跳时另一跳被摘掉
    # 不会有任何东西变红 —— 那正是 P1-10 那个第三态能长出来的原因。
    "recover_abandoned_monitoring_task_execution": (
        "reclaim_orphans", "close_unattempted_for_cells"),
    "revoke_monitoring_task_coverage_for_organization_refund": (
        "reclaim_orphans", "close_unattempted_for_cells"),
}


def _iter_production_py():
    for d in _PRODUCTION_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            yield p


#: 🔴 生产里有**一个**非 UTF-8 的 .py:``tools/scoring/geo_scorer_backup.py``
#:    (0xa9 = cp1252 的 ©)。它是个 ``_backup`` 文件、不在任何调用链上,
#:    但 census 必须能读完整棵树 —— 读到它就抛的话,普查会在那里**静默截断**,
#:    后面的目录一个都没扫到,而"扫不到"和"没有"在断言里长得一样。
#:    本仓记过:负面存在性结论禁出自截断视图。
#:    所以这里 errors="replace":宁可那一个文件的注释里出现替换符,
#:    也不让整个分母塌掉。判据 test_census_denominator_is_not_truncated 自证。
_NON_UTF8_KNOWN = ("tools/scoring/geo_scorer_backup.py",)


def _read(path: Path) -> str:
    return io.open(path, encoding="utf-8", newline="", errors="replace").read()


def _called_names(tree: ast.AST) -> set[str]:
    """这棵树里**真的被调用**的名字集合。

    🔴 只认 ``ast.Call``,**不**用裸符号名 grep:那样 import 语句、注释、
       字符串都会命中,而"import 了"不等于"调用了"
       (本仓记过:census 裸符号名 = 把 import 当调用)。
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            out.add(f.attr)
    return out


def _production_call_sites(fn: str) -> set[str]:
    hits: set[str] = set()
    for path in _iter_production_py():
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:            # pragma: no cover
            continue
        if fn in _called_names(tree):
            hits.add(str(path.relative_to(ROOT)).replace("\\", "/"))
    return hits


def _function_node(module_rel: str, fn_name: str) -> ast.FunctionDef:
    tree = ast.parse(_read(ROOT / module_rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == fn_name:
            return node
    raise AssertionError(
        f"{module_rel} 里找不到函数 {fn_name} —— 分母塌了(函数被改名/删了?)")


# ══════════════════════════════════════════════════════════════════════
# 1. 六个接线函数逐个都有生产调用点
# ══════════════════════════════════════════════════════════════════════

#: ``reclaim_superseded`` 是**桥内部**的 helper:它只在 ``open_for_claim``
#: 里被调(赢下 claim CAS 之后收口旧在飞行)。它没有、也不该有外部调用点。
#:
#: 🔴 把它列成例外不是放宽,而是把"接线"和"内部调用"这两件事分开判:
#:    下面 test_internal_recovery_helper_is_called_from_open 亲自证明它
#:    真的挂在 open_for_claim 上。两条合起来的强度**高于**把它塞进
#:    外部接线分母(那样只要桥文件本身出现在集合里就算过)。
_INTERNAL_ONLY = ("reclaim_superseded",)

#: 外部接线分母 = WIRED_ENTRYPOINTS 减去内部 helper。**算出来的**,不手抄 ——
#: 手抄的那份会在新增接线函数时静默漏项。
EXTERNAL_ENTRYPOINTS = tuple(
    f for f in BRIDGE.WIRED_ENTRYPOINTS if f not in _INTERNAL_ONLY)


def test_every_wired_entrypoint_has_a_production_caller():
    """分母来自 ``WIRED_ENTRYPOINTS``,**不手抄**。

    🔴 本仓记过:「手写分母漏掉的那一项不会让任何判据变红」。
       所以这里遍历常量本身 —— 新加一个接线函数而忘了接,这条立刻红。
    """
    assert EXTERNAL_ENTRYPOINTS, "分母是空的"
    # 分母自证:内部 helper 必须真的在 WIRED_ENTRYPOINTS 里,否则
    # _INTERNAL_ONLY 是在减一个不存在的东西(减法看起来生效,其实没减)。
    for f in _INTERNAL_ONLY:
        assert f in BRIDGE.WIRED_ENTRYPOINTS, (
            f"{f} 不在 WIRED_ENTRYPOINTS 里 —— 例外清单已过期")
    dead = []
    for fn in EXTERNAL_ENTRYPOINTS:
        sites = _production_call_sites(fn)
        # 排除桥自己(它内部互调)—— 只有外部生产调用才算接线
        sites -= {"services/defensive_geo/monitoring/run_ledger_bridge.py"}
        if not sites:
            dead.append(fn)
    assert not dead, (
        f"这些接线函数在生产代码里零调用点 = 死函数:{dead}。"
        "一期 attempt_ledger 三个函数就是这么变成 P1-B 的。")


def test_internal_recovery_helper_is_called_from_open():
    """``reclaim_superseded`` 必须真的挂在 ``open_for_claim`` 上。

    🔴 这是"崩在中途永久占位"那个雷的**唯一**结构性防线所在的位置。
       它如果被摘掉,``uq_defgeo_attempt_single_inflight`` 会让那一格
       从此再也 open 不进第二个 attempt —— 而现象是"重试永远不进账本",
       非常难从症状反推。
    """
    node = _function_node("services/defensive_geo/monitoring/run_ledger_bridge.py",
                          "open_for_claim")
    assert "reclaim_superseded" in _called_names(node), (
        "open_for_claim 不再调 reclaim_superseded —— 崩溃占位的防线被摘了")
    # 反向:它**不该**调 reclaim_orphans(那条在 cell 已是 running 时恒空转,
    # 是一个"看起来接了防护、实际什么都不做"的调用)
    assert "reclaim_orphans" not in _called_names(node), (
        "open_for_claim 调了 reclaim_orphans —— 此刻 cell 已是 running,"
        "那条判据(cell.state <> 'running')恒 false,一条都收不到:"
        "这是个恒空转的假防护")


def test_live_chain_call_sites_match_expected():
    """调用点集合**双向**判 —— 多一个 / 少一个都红。

    · 实际 ⊋ 期望 ⇒ 有人在别处接了一跳,没人复核过;
    · 实际 ⊊ 期望 ⇒ 接线被摘掉了(死函数复发)。
    只判一个方向的锁抓不到另一半。
    """
    actual: set[str] = set()
    for fn in BRIDGE.WIRED_ENTRYPOINTS:
        actual |= _production_call_sites(fn)
    actual -= {"services/defensive_geo/monitoring/run_ledger_bridge.py",
               # legacy_bridge 复用 guarded,不是接线点
               "services/defensive_geo/monitoring/legacy_bridge.py"}
    expected = set(BRIDGE.EXPECTED_CALL_SITES)
    assert actual == expected, (
        f"接线点漂移:\n  多出来(没人复核过)= {sorted(actual - expected)}"
        f"\n  少掉了(接线被摘)  = {sorted(expected - actual)}")


@pytest.mark.parametrize("chokepoint,required", sorted(EXPECTED_HOOKS.items()))
def test_each_chokepoint_calls_its_hook(chokepoint: str, required: tuple[str, ...]):
    """🔴 **逐个** chokepoint 点名验证,不是"至少有一处接了就算过"。

    本仓记过「两把锁叠在同一条路径上时『相关判据全绿』证明不了那一行被验过」。
    上面两条判的是集合;集合过了,仍然可能是"五点里只接了一点,而那一点
    让整个文件出现在集合里"。所以这里按**函数体**逐个查。
    """
    node = _function_node("db/monitoring_db.py", chokepoint)
    called = _called_names(node)
    missing = [f for f in required if f not in called]
    assert not missing, (
        f"db/monitoring_db.py::{chokepoint} 没有调 {missing} —— "
        "这一点的接线被摘了(或从来没接上)")


# ══════════════════════════════════════════════════════════════════════
# 2. fail-soft 的**机制**必须是 SAVEPOINT,不是裸 try/except
# ══════════════════════════════════════════════════════════════════════

def test_every_bridge_write_goes_through_the_savepoint_guard():
    """桥里**没有**任何一条 ``cur.execute`` 走在 :func:`guarded` 外面。

    🔴 这条判的是**机制**不是意图。一期 ``legacy_bridge`` 那句
       "fail-open:观测账本不该挡住用户的重试" 是**反的**:裸 try/except
       包 SQL,报错后整个事务 aborted,后面 retry_requests INSERT 与
       cell UPDATE 全部失败 —— 账本一失败,用户的重试就被账本挡下来了。
       本仓记过这一条(try/except 包 SQL 无 SAVEPOINT = 打废调用方事务)。

    实现:桥里每个 ``cur.execute`` 都必须在某个嵌套函数(交给 guarded 的那个
    闭包)或 guarded 自己里面。顶层函数体里直接 execute = 红。
    """
    rel = "services/defensive_geo/monitoring/run_ledger_bridge.py"
    tree = ast.parse(_read(ROOT / rel))

    offenders = []
    # _GUARDED_HELPERS 的豁免**不是**白送的 —— 下面那条
    # test_guarded_helpers_are_only_called_from_inside_closures 亲自证明
    # 它们真的只在 guarded 闭包里被调用。没有那一条,这里的豁免就是个洞。
    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if top.name in ("guarded",) + _GUARDED_HELPERS:
            continue                      # guarded 本体 + 只在闭包里被调的 helper
        # 嵌套函数(闭包)里的 execute 是合法的 —— 它们由 guarded 执行
        nested_bodies = [n for n in ast.walk(top)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n is not top]
        nested_calls = set()
        for nb in nested_bodies:
            for n in ast.walk(nb):
                nested_calls.add(id(n))
        for n in ast.walk(top):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "execute" and id(n) not in nested_calls:
                offenders.append(f"{top.name}:{n.lineno}")
    assert not offenders, (
        f"这些 cur.execute 走在 SAVEPOINT 外面:{offenders} —— "
        "它们一报错就会把现役监测的事务打废,而承诺是'不阻断'")


#: 只在 ``guarded`` 闭包内部被调用的私有 helper。它们自己发 SQL 是安全的,
#: 因为执行时已经在 SAVEPOINT 里。豁免由下一条判据**证明**,不是声明。
_GUARDED_HELPERS: tuple[str, ...] = ("_inflight_attempt_id",)


def test_guarded_helpers_are_only_called_from_inside_closures():
    """豁免的**证明**:每个 helper 的调用点都在某个嵌套闭包里。

    🔴 没有这一条,``_GUARDED_HELPERS`` 就是一张"想豁免谁写谁"的白名单 ——
       本仓记过「白名单一个不加」。这里机械核对:helper 出现在顶层函数体
       (而不是嵌套闭包)里 = 红,因为那时它不在 SAVEPOINT 保护下。
    """
    rel = "services/defensive_geo/monitoring/run_ledger_bridge.py"
    tree = ast.parse(_read(ROOT / rel))
    offenders = []
    for top in tree.body:
        if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nested_ids = set()
        for n in ast.walk(top):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not top:
                for m in ast.walk(n):
                    nested_ids.add(id(m))
        for n in ast.walk(top):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)                     and n.func.id in _GUARDED_HELPERS and id(n) not in nested_ids:
                offenders.append(f"{top.name}:{n.lineno} -> {n.func.id}")
    assert not offenders, (
        f"这些 helper 在 SAVEPOINT 之外被调用:{offenders} —— "
        "它们发的 SQL 一报错会打废调用方事务")
    # 反向:helper 必须真的**有**调用点,否则豁免的是个死函数
    called = _called_names(tree)
    for h in _GUARDED_HELPERS:
        assert h in called, f"{h} 在桥里零调用点 —— 豁免了一个死函数"


def test_census_denominator_is_not_truncated():
    """普查分母活性自证:那个非 UTF-8 文件读得出来,且分母够大。

    🔴 本仓记过「负面存在性结论禁出自截断视图」。如果 ``_read`` 在
       ``geo_scorer_backup.py`` 上抛,普查会在 tools/ 里静默停住 ——
       于是"没有别的调用点"这个结论出自一个截断的视图。
    """
    files = list(_iter_production_py())
    assert len(files) > 300, f"生产 .py 分母只有 {len(files)} 个 —— 太少,像是被截断了"
    for rel in _NON_UTF8_KNOWN:
        txt = _read(ROOT / rel)
        assert txt, f"{rel} 读成空 —— 普查在这里会截断"
    # 每个目录都必须真的扫到东西(不是只有第一个目录有货)
    for d in _PRODUCTION_DIRS:
        assert any(str(f).replace("\\", "/").split("/")[-2:] and
                   f.is_relative_to(ROOT / d) for f in files),             f"目录 {d} 一个文件都没进分母"


def test_legacy_bridge_no_longer_uses_a_bare_try_except_around_sql():
    """一期那处裸 try/except **已改**走同一个出口。

    反向对照的价值:如果有人把 ``guarded`` 换回裸 try/except,
    上面那条只看 ``run_ledger_bridge``,看不到 ``legacy_bridge``。
    """
    rel = "services/defensive_geo/monitoring/legacy_bridge.py"

    # 🔴 作用域**必须**收到那一个函数上。
    #    第一版写的是"整个模块里有人调 guarded 就算过" —— MUT-F09
    #    (把 capture_before_retry_overwrite 改回裸 try/except)**存活**了,
    #    因为同模块的 ``_cell_is_already_ledgered`` 也调 guarded,
    #    于是模块级断言照样绿。这正是本仓记过的
    #    「两把锁叠在同一条路径上时『相关判据全绿』证明不了那一行被验过」。
    node = _function_node(rel, "capture_before_retry_overwrite")
    called = _called_names(node)
    assert "guarded" in called, (
        "capture_before_retry_overwrite 没走 run_ledger_bridge.guarded —— "
        "它那条 INSERT 一报错就会把用户的重试事务打废")

    # 成对的负向锁:这个函数体里不许再出现裸 ``try/except`` 包着 execute。
    handlers = [n for n in ast.walk(node) if isinstance(n, ast.Try)]
    for t in handlers:
        for n in ast.walk(t):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "execute":
                pytest.fail(
                    f"capture_before_retry_overwrite 里第 {n.lineno} 行的 execute "
                    "又被裸 try/except 包起来了 —— 那会打废调用方事务")


def test_savepoint_name_is_a_constant_not_interpolated():
    """SAVEPOINT 名不许拼入参 —— 拼入参就是 SQL 注入面。"""
    src = _read(ROOT / "services/defensive_geo/monitoring/run_ledger_bridge.py")
    assert 'f"SAVEPOINT {_SAVEPOINT}"' in src, "SAVEPOINT 语句形状变了,重核这条锁"
    assert isinstance(BRIDGE._SAVEPOINT, str) and BRIDGE._SAVEPOINT.isidentifier(), \
        f"SAVEPOINT 名 {BRIDGE._SAVEPOINT!r} 不是合法标识符"


# ══════════════════════════════════════════════════════════════════════
# 3. 夹具键完整性 —— 防「生产从来不会发的键」
# ══════════════════════════════════════════════════════════════════════

def test_open_for_claim_only_reads_keys_the_live_cell_row_really_has():
    """``open_for_claim`` 从 cell 上读的每个键,现役 cell 行必须真有。

    🔴 本仓记过一次很贵的:**夹具用的键是生产从来不会发的键**
       (``id`` vs 中间件写的 ``user_id``)—— 整片端点生产必 500 而判据全绿。
       这里反过来防:桥读的键必须是 ``monitoring_run_cells`` 真列名。
       写错一个(比如 ``owner_user_id`` 而实际列叫 ``tenant_owner_user_id``),
       生产会静默拿到 0/None 而判据不会红 —— 除了这一条。

    🔴 [工单 E3-1 · 2026-08-26] 这条锁**曾经恒绿**,而且是最贵的那种恒绿:
       它的分母 ``_LIVE_SCHEMA`` 是本包自己手写的夹具,而那份夹具凭空多了
       一列 ``billing_user_id``(生产 monitoring_run_cells 上没有)。
       桥读 ``cell.get("billing_user_id")`` ⇒ 在夹具里读得到、在生产里恒 None,
       而这条锁拿被污染的分母去判,判不出来。
       分母与被测对象同源 = 没有判据在守。
       真正独立的分母(migration 建库后的 information_schema)现在在
       ``tests/defgeo_e3_2026_08_26/test_e1_tenant_owner_pg.py`` 里,
       并且额外锁住「本夹具的列集合 ⊆ 真列」——
       下一次有人往夹具里加一列生产没有的,那一条会红。
    """
    node = _function_node("services/defensive_geo/monitoring/run_ledger_bridge.py",
                          "open_for_claim")
    def _is_cell(node_) -> bool:
        """只认对 ``cell`` 这个名字的读取。

        🔴 不加这个限定就会把 ``lineage["actual_provider"]`` 一起收进来 ——
           那是本模块自己造的字典,不是 cell 行。第一版就是这么假红的,
           而假红与真红一样会浪费下一个人的时间。
        """
        return isinstance(node_, ast.Name) and node_.id == "cell"

    read_keys = set()
    for n in ast.walk(node):
        # cell.get("xxx") / cell["xxx"] —— 只看 cell,不看别的字典
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "get" and _is_cell(n.func.value) and n.args \
                and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str):
            read_keys.add(n.args[0].value)
        if isinstance(n, ast.Subscript) and _is_cell(n.value) \
                and isinstance(n.slice, ast.Constant) \
                and isinstance(n.slice.value, str):
            read_keys.add(n.slice.value)
    assert read_keys, "一个 cell 键都没解析出来 —— 探针失效(分母是空的)"

    # 现役列名的**真相来源** = 本包底座里那份最小真 schema
    from tests.defensive_geo_pkgf_2026_08_23.conftest import _LIVE_SCHEMA
    import re as _re
    block = _LIVE_SCHEMA.split("CREATE TABLE IF NOT EXISTS monitoring_run_cells")[1]
    block = block.split("CREATE TABLE")[0]
    real_cols = set(_re.findall(r"(?m)^\s{4}([a-z_]+)\s+[A-Z]", block))
    assert len(real_cols) > 15, f"列名分母塌了,只解析出 {sorted(real_cols)}"

    unknown = sorted(read_keys - real_cols)
    assert not unknown, (
        f"open_for_claim 读了 monitoring_run_cells 上不存在的键:{unknown}\n"
        f"(真列名 = {sorted(real_cols)})—— 生产会静默拿到 None/0")


def test_the_column_probe_can_actually_fail():
    """上一条的判别力自证:它必须能认出一个假列名。

    没有这一条,``real_cols`` 解析成空集时上面那条恒绿
    (``read_keys - set()`` 后 assert 反而会红……但解析成"包含一切"就恒绿)。
    这里直接验一个明知不存在的列不在真列名里。
    """
    from tests.defensive_geo_pkgf_2026_08_23.conftest import _LIVE_SCHEMA
    import re as _re
    block = _LIVE_SCHEMA.split("CREATE TABLE IF NOT EXISTS monitoring_run_cells")[1]
    block = block.split("CREATE TABLE")[0]
    real_cols = set(_re.findall(r"(?m)^\s{4}([a-z_]+)\s+[A-Z]", block))
    assert "owner_user_id" not in real_cols, (
        "列名探针把一个不存在的列认成存在 —— 上一条判据没有区分力")
    assert "tenant_owner_user_id" in real_cols, (
        "真列名解析漏了 tenant_owner_user_id(迁移 054 冻结租户归属那一列)")


# ══════════════════════════════════════════════════════════════════════
# 4. 身份形态 —— 别把 cell 域 id 当成 §6.1 id
# ══════════════════════════════════════════════════════════════════════

def test_live_attempt_id_is_not_the_spec_formula():
    """执行链的 attempt_id 走 ``cell:`` 域,**不是** §6.1 公式的产出。

    §6.1 公式吃 ``actual_model_revision``,而 revision 只有拿到回答之后才知道;
    而 open 必须发生在派发之前。两个要求不可能同时满足 —— 所以这里诚实地
    另起一个域,而不是硬塞占位 revision 造一个"看起来符合 §6.1 其实是编的"身份。

    这条锁住的是**别人日后的误用**:把它当 observation_cell_id 去算 projection_id。
    """
    from services.defensive_geo.monitoring import lineage as lin

    pcid = "a" * 64
    live = BRIDGE._cell_attempt_id(pcid, 1)
    spec = lin.attempt_id(
        plan_cell_id=pcid, attempt_ordinal=1,
        actual_provider="dashscope", actual_model="qwen3.7-plus",
        actual_model_revision=None, actual_surface="ai_search",
        actual_search_mode="enhanced", request_hash=pcid)
    assert live != spec, (
        "执行链 id 与 §6.1 公式产出相等了 —— 那意味着有人把占位值塞进了公式,"
        "于是一个编出来的身份会被当成合法 observation_cell_id")
    assert len(live) == 64 and all(c in "0123456789abcdef" for c in live)
    # 域分离:human 域与 cell 域不许撞
    assert BRIDGE._human_attempt_id(pcid, 1) != live


def test_attempt_lease_is_strictly_longer_than_the_live_cell_lease():
    """账本租约必须**严格大于**现役 cell 租约上限。

    否则账本会在现役还认为这一格在跑的时候先把 attempt 收掉,
    造出"账本说错了、cell 说在跑"的对不上账。
    """
    assert BRIDGE.ATTEMPT_LEASE_SECONDS > BRIDGE.LIVE_CELL_MAX_LEASE_SECONDS, (
        f"账本租约 {BRIDGE.ATTEMPT_LEASE_SECONDS}s 不大于现役上限 "
        f"{BRIDGE.LIVE_CELL_MAX_LEASE_SECONDS}s")
    # 现役上限的**真相来源**:monitoring_db 那句 min(..., 1800)
    src = _read(ROOT / "db/monitoring_db.py")
    assert "min(int(lease_seconds), 1800)" in src, (
        "现役租约上限的形状变了 —— 本常量的依据失效,重核 "
        "run_ledger_bridge.LIVE_CELL_MAX_LEASE_SECONDS")


def test_reclaim_codes_are_the_live_chains_own_words():
    """收口码逐字沿用现役自己的恢复码,不另造一套词。

    两套词会让同一件事在两张表里叫两个名字,排查时对不上。
    """
    src = _read(ROOT / "db/monitoring_db.py")
    for code in (BRIDGE.RECLAIM_CODE_DISPATCHED, BRIDGE.RECLAIM_CODE_NOT_DISPATCHED):
        assert f"'{code}'" in src, (
            f"{code!r} 不是现役 monitoring_db 里的词 —— 账本在自造词汇")
    assert BRIDGE.POLICY_SKIP_REASON in src, (
        f"{BRIDGE.POLICY_SKIP_REASON!r} 不是现役建格时写的 error_code")


# ══════════════════════════════════════════════════════════════════════
# [R3] meta 锁:每个接线点都必须有**行为**判据,不许只带结构锁出厂
# ══════════════════════════════════════════════════════════════════════

#: 真链判据文件 —— 行为判据的唯一去处。
_REALCHAIN = "tests/defensive_geo_pkgf_2026_08_23/test_run_ledger_realchain_pg.py"


def _driven_from_source(src: str) -> dict[str, set[str]]:
    """每个 ``db.monitoring_db`` 生产函数 → 哪些判据**真的调了**它。

    🔴 要求 import **且** 调用,不只是 import ——
       本仓记过「census 裸符号名 = 把 import 当调用」。
       一条只 import 不调的判据,对接线一个字都没证明。

    🔴 这是**唯一一份**实现:下面的真扫与它的判别力自证都走这里。
       写两份的话必有一份没人验(本仓记过),而没人验的那份恰好会是
       自证那份 —— 于是扫描逻辑退化时自证**恒绿**。
       (这一条是 MUT-F42 逼出来的:第一版我把自证写成了内联复制品。)
    """
    driven: dict[str, set[str]] = {}
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("test_"):
            continue
        imported: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("monitoring_db"):
                imported.update(a.asname or a.name for a in n.names)
        if not imported:
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in imported:
                driven.setdefault(n.func.id, set()).add(fn.name)
    return driven


def _realchain_driven_production_functions() -> dict[str, set[str]]:
    return _driven_from_source(
        io.open(ROOT / _REALCHAIN, encoding="utf-8", newline="",
                errors="replace").read())


def test_every_wired_chokepoint_has_a_behavioural_criterion():
    """🔴 **本文件里最重要的一条**:接线表每个 key 都要有真链行为判据。

    ── 它存在的理由(2026-08-24 R3,Review 亲毒实证)──────────────────
    ④ ``record_skip_for_plan`` 与 ⑤ ``close_for_human_resolution`` 被改绑成
    no-op(**调用行保留**,所以本文件的 AST 接线锁照绿)⇒ pkgF 90 条全绿存活。
    真因:这两点的"判据"直接调桥函数本身 —— **那是自证桥,不证接线**。

    结构锁能证明"这一行写在那儿了",证明不了"它干的事真的发生了"。
    两者缺一不可,而缺的那一半此前没有任何东西在管。

    ── 这条锁做什么 ────────────────────────────────────────────────
    对 :data:`EXPECTED_HOOKS` 的**每一个** chokepoint,要求真链文件里存在
    至少一条判据 **import 并调用**该生产函数本体。
    于是**下一个新接线点不可能只带结构锁出厂** —— 它一进 EXPECTED_HOOKS,
    这条就红,直到有人给它写行为判据。

    ── 它当场抓到的 ────────────────────────────────────────────────
    Review 点名的是 ④⑤ 两个。这条锁机械扫出来的是**四个**:另外两个是
    ``reserve_monitoring_cell_retry``(①的重试臂)与
    ``_recover_expired_monitoring_cells``(租约恢复)—— 它们的老判据用
    "手工 UPDATE + 再 claim 一次"**等价模拟**生产动作,注释里就写着
    "现役重试链的等价动作"。**等价声明不是判据**;两者各毒一发,
    同样 90 条全绿存活。四个缺口现在都补了真链判据。
    """
    driven = _realchain_driven_production_functions()

    # 活性自证:分母不许是 0(文件读空 / 解析失败 / 命名约定变了都会静默恒绿)
    assert len(driven) >= 4, (
        f"真链文件里只扫到 {len(driven)} 个被驱动的生产函数 —— "
        f"扫描面已经失效,这条锁此刻零区分力:{sorted(driven)}")

    missing = {}
    for chokepoint in EXPECTED_HOOKS:
        tests = sorted(driven.get(chokepoint, ()))
        if not tests:
            missing[chokepoint] = EXPECTED_HOOKS[chokepoint]

    assert not missing, (
        "以下 chokepoint **只有结构锁,没有行为判据**:\n  " +
        "\n  ".join(f"{fn}()  应调 {hooks}" for fn, hooks in missing.items()) +
        "\n\n结构锁只证明'调用行写在那儿了'。把那个钩子改绑成 no-op"
        "(调用行保留)结构锁照样绿 —— Review 2026-08-24 就是这么把"
        "④⑤ 打穿的。\n"
        f"改法:在 {_REALCHAIN} 里加一条判据,**import 并调用**该生产函数本体,"
        "断言账本行为(不是调桥函数,那是自证桥)。")


def test_the_behavioural_census_can_tell_import_from_call():
    """上一条的判别力自证:只 import 不调用**不算**被驱动。

    没有这条,把上面那句 ``ast.Call`` 判断写错(比如退化成只看 import)
    也不会有人发现 —— 而那正好让"只 import 不调"的空判据蒙混过关。
    """
    # 🔴 两个样本都喂给**同一把尺子** ``_driven_from_source`` ——
    #    自己内联复制一份逻辑的话,尺子退化时这条自证会恒绿(MUT-F42 抓的就是它)。
    only_imports = (
        "def test_probe():\n"
        "    from db.monitoring_db import save_monitoring_result\n"
        "    assert save_monitoring_result is not None\n"
    )
    assert _driven_from_source(only_imports) == {}, (
        "只 import 不调用被算成了'驱动' —— 上一条锁会放过空判据")

    # 正样本:同一把尺子必须认得出**真调用**。
    # 没有它,上面那句会在"尺子恒返空"时恒真 —— 那是零区分力,不是判别力。
    # (本仓记过:多规则扫描器的正样本只断言"有命中"是不够的,要点名。)
    imports_and_calls = (
        "def test_probe():\n"
        "    from db.monitoring_db import save_monitoring_result\n"
        "    save_monitoring_result(task_id=1)\n"
    )
    assert _driven_from_source(imports_and_calls) == {
        "save_monitoring_result": {"test_probe"}}, "尺子认不出真调用"


def test_expected_hooks_covers_every_bridge_entrypoint_used_in_production():
    """接线表本身的分母自证:桥的外部入口都要在 EXPECTED_HOOKS 里露面。

    否则有人加了新桥函数、在生产里调了它,却没登记进 EXPECTED_HOOKS ——
    上面那条 meta 锁的分母就少了一项,而**少掉的那一项不会让任何判据变红**。
    """
    declared_hooks = {h for hooks in EXPECTED_HOOKS.values() for h in hooks}
    unlisted = sorted(set(EXTERNAL_ENTRYPOINTS) - declared_hooks)
    assert not unlisted, (
        f"桥的这些外部入口没在 EXPECTED_HOOKS 里登记:{unlisted}\n"
        f"后果:它们的 chokepoint 不在 meta 锁的分母里,少一个也不会红。")


# ══════════════════════════════════════════════════════════════════════
# [R4] EXPECTED_HOOKS 的**键域**也要有机械分母
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 Review 毒C:从 EXPECTED_HOOKS 删掉 `reserve_monitoring_cell_retry` 键
#    ⇒ wiring **22 条全绿**。三处一起失效,谁也补不了谁:
#      · 值域机械取自 `BRIDGE.WIRED_ENTRYPOINTS`,而 `open_for_claim`
#        仍由 `claim_monitoring_run_cell` 那个键供值 ⇒ 分母自证不掉;
#      · `test_each_chokepoint_calls_its_hook` 是 parametrize **在这张表上**,
#        键没了它就少跑一个参数 —— 少跑不会红;
#      · R3 那三条 meta 锁同样遍历这张表的键 ⇒ **同盲**。
#    共享 hook 的键被删,整条链上没有一个人会说话。
#
# 修法:键域不再手写背书,由生产侧 AST 普查双向钉死。

#: 生产侧唯一接线文件。
#:
#: 🔴 只扫这一个文件不是偷懒,是**有另一条锁在守这个作用域**:
#:    `BRIDGE.EXPECTED_CALL_SITES` 声明接线只发生在这里,而
#:    `test_live_chain_call_sites_match_expected` 对它做**双向**判 ——
#:    有人在别的文件里接了一跳,那条会先红。
#:    两条合起来才是完整分母:那条管"接线只许发生在这个文件",
#:    这条管"这个文件里每一个接线的函数都必须登记"。
_WIRING_FILE = "db/monitoring_db.py"

#: 模块级(不在任何函数里)调用 hook 的哨兵键。
#: 那种写法意味着 import 时就动账本 —— 本仓有过 import 触发 init_db
#: 抢锁把生产打成 503 的先例,所以宁可当场红。
_MODULE_LEVEL = "<module-level>"


def _hook_callers_from_source(src: str, hooks: frozenset[str]) -> dict[str, set[str]]:
    """<最外层函数名> → 它(含其闭包内)**真调用**到的 hook 名字集合。

    🔴 **唯一一份**实现:真扫与判别力自证共用同一把尺子。
       写两份的话必有一份没人验,而没人验的恰好会是自证那一份
       (R3 我在 meta 锁上刚踩过一次)。

    🔴 归属取**最外层**函数而不是最内层:hook 若被包进闭包
       (桥自己的 `guarded` 就是这个形态),语义上仍然是"外层那个生产
       入口在接线"。取最内层会把闭包名当成 chokepoint,红得莫名其妙。

    🔴 只认 `ast.Call`,不认裸符号名 —— import 了不等于调用了。
    """
    tree = ast.parse(src)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def _outermost_function(node: ast.AST):
        found = None
        cur = node
        while cur in parent:
            cur = parent[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found = cur
        return found

    census: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in hooks:
            continue
        owner = _outermost_function(node)
        key = owner.name if owner is not None else _MODULE_LEVEL
        census.setdefault(key, set()).add(node.func.id)
    return census


def _production_hook_callers() -> dict[str, set[str]]:
    return _hook_callers_from_source(
        _read(ROOT / _WIRING_FILE), frozenset(BRIDGE.WIRED_ENTRYPOINTS))


def test_expected_hooks_key_domain_equals_the_production_census():
    """🔴 键域 **双向**等于生产侧机械普查。

    三个方向一条判据全覆盖:
      · **删键** → 普查里有、表里没有 → 红(Review 毒C);
      · **幽灵键** → 表里有、普查里没有 → 红(接线被摘 / 函数改名);
      · **生产侧新增调用方未登记** → 普查里多出来 → 红。

    最后一个方向不是假想:R4 这条一上就翻出
    ``recover_abandoned_monitoring_task_execution`` 与
    ``revoke_monitoring_task_coverage_for_organization_refund`` ——
    **R2 我自己接的两条线,一直没登记**。它们因此也不在 R3 meta 锁的
    分母里,"必须有行为判据"那条对它们从来没生效过。
    """
    census = _production_hook_callers()

    # 分母活性自证:普查不许是空的(文件读空 / 解析失败 / hook 改名都会静默恒绿)
    assert len(census) >= 5, (
        f"生产侧只普查到 {len(census)} 个接线函数 —— 扫描面失效,本条零区分力:"
        f"{sorted(census)}")

    assert _MODULE_LEVEL not in census, (
        f"有 hook 在**模块级**被调用(import 时就动账本):"
        f"{sorted(census[_MODULE_LEVEL])}")

    declared, actual = set(EXPECTED_HOOKS), set(census)
    assert declared == actual, (
        "EXPECTED_HOOKS 的键域与生产侧真实接线对不上:\n"
        f"  生产在接、表里没登记 = {sorted(actual - declared)}\n"
        f"      ↑ 这些 chokepoint 既没有逐点结构锁,也**不在 meta 锁的分母里**,\n"
        f"        于是'必须有行为判据'那条对它们不生效。\n"
        f"  表里有、生产没在接 = {sorted(declared - actual)}\n"
        f"      ↑ 幽灵键:接线被摘了,或者函数改名了。")

    # 值域也逐键对齐:生产多调了一个 hook 而没登记,同样要红。
    # (``test_each_chokepoint_calls_its_hook`` 判的是 required ⊆ called,
    #  漏掉"多调了一个"这个方向。)
    drift = {k: (sorted(census[k]), sorted(EXPECTED_HOOKS[k]))
             for k in sorted(declared & actual)
             if census[k] != set(EXPECTED_HOOKS[k])}
    assert not drift, (
        "有 chokepoint 实际调的 hook 与登记的不一致(实际, 登记):\n  " +
        "\n  ".join(f"{k}: {v}" for k, v in drift.items()))


def test_the_production_hook_census_has_discriminating_power():
    """上一条的判别力自证 —— 负样本正样本共用**同一把尺子**。

    没有这条,``_hook_callers_from_source`` 退化成恒返 ``{}`` 时,
    上一条会因为 declared/actual 双空而恒绿(活性自证挡住一部分,
    但挡不住"把 ast.Call 判断写松/写死"这类退化)。
    """
    hooks = frozenset({"open_for_claim", "reclaim_orphans"})

    # 负样本①:只 import 不调用 —— 不算接线
    only_import = (
        "def f():\n"
        "    from x import open_for_claim\n"
        "    return open_for_claim\n"
    )
    assert _hook_callers_from_source(only_import, hooks) == {}, "import 被当成了调用"

    # 负样本②:调的是别的名字
    other_call = "def f():\n    something_else(1)\n"
    assert _hook_callers_from_source(other_call, hooks) == {}, "尺子命中了不相干的调用"

    # 正样本①:真调用,**点名**到函数与 hook(不只断言"有命中")
    real_call = "def f():\n    open_for_claim(cur, row)\n"
    assert _hook_callers_from_source(real_call, hooks) == {"f": {"open_for_claim"}}, (
        "尺子认不出真调用")

    # 正样本②:闭包里的调用归给**最外层**函数
    nested = (
        "def outer():\n"
        "    def inner():\n"
        "        reclaim_orphans(cur)\n"
        "    return guarded(inner)\n"
    )
    assert _hook_callers_from_source(nested, hooks) == {"outer": {"reclaim_orphans"}}, (
        "闭包里的接线没归给最外层函数 —— 会把闭包名当成 chokepoint")

    # 正样本③:模块级调用要被点名,不许静默丢掉
    module_level = "open_for_claim(cur, row)\n"
    assert _hook_callers_from_source(module_level, hooks) == {
        _MODULE_LEVEL: {"open_for_claim"}}, "模块级调用被静默丢掉了"
