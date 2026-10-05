"""热修判别锁 · recommend_deep 的 media_balance_enabled NameError(生产 live bug)。

背景:灰度提交 c3fdc8a3 给其余调用点都接了 `media_balance_enabled`,唯独漏了
`recommend_deep`。该名字在函数里从未被定义 → 编译成**全局查找**,而模块无此全局
→ `scored = [... _calc_score(n, d) ...]` 对第一个平台求值时抛 NameError。
调用点 `api/publish_api.py::deep_analyze_for_publish` 外层 0 层 try → 端点 100% 返 500。

**为什么测代码对象而不是测源码文本**:这个缺陷的本质是**名字解析**(全局 vs 闭包),
不是"某行字在不在"。`co_names` / `co_freevars` / `co_cellvars` 由编译器产出,
改注释、改措辞都骗不过它;而源码 grep 会被"名字写了但没接线"骗过去 ——
本仓刚吃过这个亏(某条 grep 锁在三种打残方式下全绿)。
"""
from __future__ import annotations

import types

import pytest


def _code_of(func):
    return func.__code__


def _nested(code, name: str):
    for const in code.co_consts:
        if isinstance(const, types.CodeType) and const.co_name == name:
            return const
    raise AssertionError(f"没找到嵌套函数 {name}(实现被重构了?锁需要同步更新)")


@pytest.fixture(scope="module")
def recommend_deep_code():
    from services.placement_service import recommend_deep
    return _code_of(recommend_deep)


NAME = "media_balance_enabled"


def test_recommend_deep_defines_the_name_itself(recommend_deep_code):
    """🔒 `media_balance_enabled` 必须由 recommend_deep 自己定义。

    局部(co_varnames)或闭包供给(co_cellvars)都算;**只要它出现在 co_names
    就是全局查找** —— 而模块没有这个全局,运行时必抛 NameError。
    """
    defined = (NAME in recommend_deep_code.co_varnames
               or NAME in recommend_deep_code.co_cellvars)
    assert defined, (
        f"{NAME} 不是 recommend_deep 的局部/闭包变量 —— "
        f"co_varnames={NAME in recommend_deep_code.co_varnames} "
        f"co_cellvars={NAME in recommend_deep_code.co_cellvars}"
    )
    assert NAME not in recommend_deep_code.co_names, (
        f"{NAME} 出现在 co_names = 运行时走全局查找 → NameError(这正是生产 live bug)"
    )


def test_calc_score_reads_it_from_closure_not_globals(recommend_deep_code):
    """🔒 嵌套的 _calc_score 必须从闭包读,不能走全局。"""
    calc = _nested(recommend_deep_code, "_calc_score")
    assert NAME in calc.co_freevars, f"{NAME} 不在 _calc_score 的闭包里"
    assert NAME not in calc.co_names, (
        f"{NAME} 出现在 _calc_score.co_names = 全局查找 → NameError"
    )


def test_module_has_no_such_global_so_global_lookup_would_crash():
    """佐证:模块级确实没有这个全局 —— 所以一旦退回全局查找就是必抛,不是"可能抛"。"""
    import services.placement_service as ps
    assert not hasattr(ps, NAME), (
        f"模块级出现了 {NAME} —— 那会把 NameError 掩盖成'看起来能跑但灰度态是错的',"
        f"比崩溃更难查。本锁要求它不存在。"
    )


def _all_codes(code):
    """函数本身 + 它里面**所有层**的嵌套函数 / lambda / 推导式。"""
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _all_codes(const)


def _global_loads(code) -> set:
    """这段字节码里**走全局查找**的名字(`LOAD_GLOBAL`)。

    不用 `co_names`:它还装着属性名(`.get` / `.sort` …),拿它判会误报;
    `LOAD_GLOBAL` 才精确等于「运行时去模块全局 / 内置里找这个名字」。
    """
    import dis

    return {str(ins.argval) for ins in dis.get_instructions(code)
            if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME")}


def test_every_global_name_in_recommend_deep_and_its_lambdas_is_defined(recommend_deep_code):
    """🔒 [WO_274 · 2026-09-23] 扩锁:不只钉 `media_balance_enabled` 一个名字。

    07-28 那次只锁了当时出事的那个名字;同一函数里排序用的两个 lambda 还引用着
    **从未定义**的 `success_table`(v2 里有,deep 里漏了)—— 候选非空一排序就 NameError,
    `deep_analyze_for_publish` 外层 0 层 try → 端点 500。锁一个名字,挡不住旁边那个。
    这里改成**形状**:函数及其所有嵌套 lambda 里,每个全局查找的名字都必须在模块全局或内置里有定义。
    """
    import builtins

    import services.placement_service as ps

    missing: dict = {}
    for code in _all_codes(recommend_deep_code):
        for name in _global_loads(code):
            if not hasattr(ps, name) and not hasattr(builtins, name):
                missing.setdefault(name, set()).add(code.co_name)
    assert not missing, (
        "recommend_deep(含嵌套 lambda)里这些名字走全局查找、模块与内置里都没有 ⇒ 运行时 NameError:"
        + ", ".join("%s(在 %s)" % (n, "/".join(sorted(where))) for n, where in sorted(missing.items()))
    )


def test_the_shape_lock_is_not_vacuous(recommend_deep_code):
    """对照臂:上一格要真的看到了字节码 —— 至少有若干个全局查找、并且看到了嵌套 lambda。
    (尺子如果因为 Python 版本把指令名改了而一条都取不到,上一格会**恒绿**。)"""
    codes = list(_all_codes(recommend_deep_code))
    assert any(c.co_name == "<lambda>" for c in codes), "没看到任何嵌套 lambda —— 尺子取错了对象"
    total = set().union(*(_global_loads(c) for c in codes))
    assert {"get_placement_service", "_quality_score_v2f"} <= total, (
        "全局查找里连 get_placement_service / _quality_score_v2f 都没取到 —— 尺子坏了: %r" % sorted(total)[:20])


def test_wemedia_call_passes_the_gate_explicitly():
    """🔒 同一次响应里媒体与自媒体必须用同一个灰度态。

    `_match_wemedia` 的默认值是 True(fail-open),不显式传就会出现
    「媒体走老口径、自媒体走新口径」。NameError 修好前这行根本跑不到,所以从没暴露过。
    """
    import inspect
    from services.placement_service import recommend_deep

    src = inspect.getsource(recommend_deep)
    assert "_match_wemedia(" in src, "实现被重构了,锁需同步更新"
    # 取 _match_wemedia 那一次调用的完整实参(可能跨行)
    idx = src.index("_match_wemedia(")
    depth, end = 0, idx
    for i in range(idx + len("_match_wemedia"), len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    call = src[idx:end + 1]
    assert f"{NAME}={NAME}" in call.replace(" ", "").replace("\n", ""), (
        f"_match_wemedia 调用没有显式传 {NAME} → 会吃 default=True(fail-open),"
        f"导致同一响应里媒体与自媒体分组口径不一致。实参={call!r}"
    )
