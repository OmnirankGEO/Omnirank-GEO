"""接线锁 —— **运行期**,不是 grep(工单①;G9 教训)。

═══════════════════════════════════════════════════════════════════════
🔴 为什么不能用 AST / grep 扫 ``add_job``
═══════════════════════════════════════════════════════════════════════
门三 G9 那把锁是这么写的:

    tree = ast.parse(inspect.getsource(sched))
    …找一个 id='defgeo_run_execute' 的 add_job 调用…

它**杀不掉** ``if False:`` 这一类变异 —— 语法树里那条 ``add_job`` 一个字没少,
只是永远不会被执行。同理杀不掉:早 return、把整块缩进进一个不会满足的分支、
把注册块搬进一个没人调的函数。

本文件的做法:塞一个**桩 scheduler**,让 ``register_v32_core_tasks()``
**真的跑一遍**,然后看桩收到了哪些作业。没被执行到的 ``add_job`` 不会出现在
集合里 —— 这才叫"从调度摘掉→红"。

═══════════════════════════════════════════════════════════════════════
🔴 三条判据,三个不同的失效方向
═══════════════════════════════════════════════════════════════════════
  · job 在不在        —— 摘掉 / if False / 早 return 都会红;
  · job 绑的是不是**那个函数** —— 绑错函数(比如绑到只读告警)会红;
  · 注册在**无条件**那一段 —— 挂进 gated 的 ``setup_schedule`` 会红
    (本仓五次同坑:注册了但生产永远不跑)。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

#: 🔴 分母 = 「必须由调度器推进」的四条。前三条是**推进**(改状态、动钱),
#:    第四条是只读告警。它们放在同一张表里,是为了让第三条判据能同时验
#:    「四条都在无条件段」——包E 之前那条告警就挂在 gated 的 setup_schedule 里
#:    (生产 auto_monitor_enabled=0 ⇒ 从来没注册过 = 冻 7 天也没人收到告警)。
EXPECTED_JOBS: dict[str, tuple[str, str, bool]] = {
    "defgeo_activation_materialize": (
        "services.defensive_geo.activation_materializer",
        "materialize_pending_sync",
        True,
    ),
    "defgeo_publish_dispatch": (
        "services.defensive_geo.publish.publish_worker",
        "dispatch_pending_sync",
        True,
    ),
    "defgeo_publish_reconcile": (
        "services.defensive_geo.publish.publish_worker",
        "reconcile_tick_sync",
        True,
    ),
    "defgeo_publish_settlement_pending_alert": (
        "services.defensive_geo.publish.settlement_alerts",
        "scan_and_alert",
        False,
    ),
}


class _StubScheduler:
    """只记账,不真跑。``get_job`` 必须真的返回已加的那条 ——
    注册块用 ``if not scheduler.get_job(id)`` 做幂等,返回恒 None 会让
    同一条被重复 add,那时"注册了几条"就数不准了。"""

    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}
        self.kwargs: dict[str, dict[str, Any]] = {}

    def get_job(self, job_id):                            # noqa: ANN001
        return self.jobs.get(job_id)

    def add_job(self, func, **kw):                        # noqa: ANN001
        job_id = kw.get("id")
        self.jobs[job_id] = func
        self.kwargs[job_id] = kw
        return job_id

    def remove_job(self, job_id):                         # noqa: ANN001
        self.jobs.pop(job_id, None)


def _run_registration(entry: str) -> tuple[_StubScheduler, str | None]:
    """真跑一遍注册函数,返回(桩, 中断原因)。

    ``entry`` = ``register_v32_core_tasks`` 或 ``setup_schedule``。
    中断原因不吞:本仓有若干 fail-closed 注册块在一次性库上会 raise,
    抛之前加进去的仍然算数,但"少了一条"必须能说清是哪一种。
    """
    import api.scheduler as sched

    stub = _StubScheduler()
    original = sched.get_scheduler
    sched.get_scheduler = lambda: stub                    # type: ignore[assignment]
    aborted: str | None = None
    try:
        try:
            if entry == "setup_schedule":
                sched.setup_schedule(0, 0, enabled=True)
            else:
                sched.register_v32_core_tasks()
        except Exception as exc:                          # noqa: BLE001
            aborted = f"{type(exc).__name__}: {exc}"
    finally:
        sched.get_scheduler = original                    # type: ignore[assignment]
    return stub, aborted


@pytest.fixture(scope="module")
def unconditional() -> tuple[_StubScheduler, str | None]:
    return _run_registration("register_v32_core_tasks")


def test_00_stub_actually_collected_jobs(unconditional) -> None:
    """判据活性:桩必须真的收到一堆作业。

    收到 0 条时,下面每一条「这条 job 在不在」都会以同一种方式红/绿 ——
    分母是空的,断言就没有判别力。
    """
    stub, aborted = unconditional
    assert len(stub.jobs) > 10, (
        f"桩只收到 {len(stub.jobs)} 条 job(中断原因 {aborted})—— 注册函数没真跑起来"
    )


@pytest.mark.parametrize("job_id", sorted(EXPECTED_JOBS))
def test_01_job_is_registered_at_runtime(unconditional, job_id) -> None:
    """🔴 「从调度摘掉 → 红」。

    拆红实录(逐条都试过,见交付单):
      · 把该条从元组里删掉        → 红
      · 在注册块前加 ``if False:`` → 红(**这一发是 AST 锁杀不掉的那个**)
      · 把整块搬进 gated setup_schedule → 红
    """
    stub, aborted = unconditional
    assert job_id in stub.jobs, (
        f"调度器里没有 {job_id}(中断原因 {aborted};实注册 {len(stub.jobs)} 条)。"
        "零调度 = 客户确认发布后没有任何东西接手 —— 终审 P0-1 原样复发"
    )


@pytest.mark.parametrize("job_id", sorted(EXPECTED_JOBS))
def test_02_job_is_bound_to_the_exact_function(unconditional, job_id) -> None:
    """绑的必须是**那个**函数对象 —— 不是同名的、不是别的模块里的。

    只验"有这条 job"挡不住「id 对了但绑到只读告警上」:那样调度器里
    看得见一条叫 dispatch 的 job,它一次状态都不会推进。
    """
    stub, _ = unconditional
    module_name, fn_name, _advancing = EXPECTED_JOBS[job_id]
    expected = getattr(importlib.import_module(module_name), fn_name)
    assert stub.jobs.get(job_id) is expected, (
        f"{job_id} 绑的不是 {module_name}:{fn_name},实得 {stub.jobs.get(job_id)!r}"
    )


def test_03_advancing_jobs_are_not_in_the_gated_path() -> None:
    """🔴 必须注册在**无条件**那一段,不是 gated 的 ``setup_schedule``。

    ``setup_schedule`` 受 ``auto_monitor_enabled`` 门控(生产实测 = 0),
    挂在那里 = 注册了但永远不跑。本仓已记过五次同坑
    (BUG-004 / P0-G / B1-4 / T5 / 飞轮),包E 之前的 Z-1 告警是第六次。

    判法是**对照**:同一把桩分别跑两个入口,四条必须只出现在无条件那一边。
    """
    gated, _ = _run_registration("setup_schedule")
    uncond, _ = _run_registration("register_v32_core_tasks")

    # 分母自证:gated 入口本身要真的注册了东西,否则"没出现在这里"是废话。
    assert len(gated.jobs) >= 2, (
        f"gated 入口只注册了 {len(gated.jobs)} 条 —— 它没真跑,"
        "那么『四条不在 gated 里』这句话没有判别力"
    )

    leaked = sorted(set(EXPECTED_JOBS) & set(gated.jobs))
    assert not leaked, (
        f"这些 job 挂在 gated 的 setup_schedule 里:{leaked} —— "
        "生产 auto_monitor_enabled=0,挂在那里等于永远不跑"
    )
    missing = sorted(set(EXPECTED_JOBS) - set(uncond.jobs))
    assert not missing, f"这些 job 不在无条件段里:{missing}"


def test_04_dispatch_and_reconcile_are_fail_closed() -> None:
    """三条**推进** job 注册失败必须让 cron 拒绝启动,不是 warning 后带病上岗。

    与 ``register_monitoring_delivery_jobs`` 同口径:少一个推进器的 cron
    不是健康的 cron。用 monkeypatch 让导入失败,看它抛不抛。
    """
    import builtins

    import api.scheduler as sched

    real_import = builtins.__import__

    def _boom(name, *a, **kw):                            # noqa: ANN001
        if name == "services.defensive_geo.publish.publish_worker":
            raise ImportError("注入的导入失败(判据)")
        return real_import(name, *a, **kw)

    stub = _StubScheduler()
    original = sched.get_scheduler
    sched.get_scheduler = lambda: stub                    # type: ignore[assignment]
    builtins.__import__ = _boom
    try:
        with pytest.raises(RuntimeError, match="拒绝带病启动"):
            sched.register_v32_core_tasks()
    finally:
        builtins.__import__ = real_import
        sched.get_scheduler = original                    # type: ignore[assignment]

    # 反向对照:不注入失败时**不许**抛这个 RuntimeError,
    # 否则上面那条 raises 会被一个恒抛的实现满足。
    stub2, aborted = _run_registration("register_v32_core_tasks")
    assert "拒绝带病启动" not in (aborted or ""), (
        f"正常路径也抛了 fail-closed 异常:{aborted} —— 上面那条判据是恒真的"
    )
    assert "defgeo_publish_dispatch" in stub2.jobs


def test_05_alert_job_is_not_fail_closed() -> None:
    """反向对照:**只读告警**不该 fail-closed。

    告警挂了不该拦住整个 cron 起不来 —— 那会把"少一条告警"升级成
    "全部定时任务停摆"。这条判据钉住这个不对称是**有意的**,
    免得有人顺手把四条都改成一样。
    """
    import builtins

    import api.scheduler as sched

    real_import = builtins.__import__

    def _boom(name, *a, **kw):                            # noqa: ANN001
        if name == "services.defensive_geo.publish.settlement_alerts":
            raise ImportError("注入的导入失败(判据)")
        return real_import(name, *a, **kw)

    stub = _StubScheduler()
    original = sched.get_scheduler
    sched.get_scheduler = lambda: stub                    # type: ignore[assignment]
    builtins.__import__ = _boom
    try:
        try:
            sched.register_v32_core_tasks()
        except RuntimeError as exc:
            assert "拒绝带病启动" not in str(exc), (
                "只读告警注册失败把整个 cron 拒绝启动了 —— 不对称被抹掉了"
            )
    finally:
        builtins.__import__ = real_import
        sched.get_scheduler = original                    # type: ignore[assignment]
    assert "defgeo_publish_settlement_pending_alert" not in stub.jobs
    # 推进 job 仍然注册得上(证明注入只打中了告警那一条)
    assert "defgeo_publish_dispatch" in stub.jobs


def test_06_no_production_caller_is_a_test_file() -> None:
    """「只有测试在调」= 死函数的标准形态(本仓记过:仅测试调用 = NO-GO)。

    这里反过来验:三个执行侧函数的生产调用者集合里,一个 tests/ 都不许有,
    而且必须非空。
    """
    from tests.defensive_geo_pkge_2026_08_24._census import production_callers

    targets = ("dispatch_once", "claim_outbox", "reconcile_once")
    found = {t: production_callers(t) for t in targets}

    for name, sites in found.items():
        assert sites, (
            f"{name} 零生产调用点 —— 它是个死函数,而客户入口是开着的。"
            "这正是终审 P0-1 当初关闸的理由"
        )
        assert not any(s.startswith("tests/") for s in sites), (
            f"{name} 只被测试调用:{sorted(sites)}"
        )
