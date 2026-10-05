"""#197 c3 行为臂 —— 包装之后 job **真的执行**;裸传则一行不跑。

c2 那把锁钉的是「形状对不对」。这一条钉的是「形状对了之后真的会跑」——
本单的病因恰恰是**两者看起来一样**:调度器对裸 async 记 executed successfully,
而函数体一行没跑。
"""
import threading
import warnings

import pytest

pytest.importorskip("apscheduler")


def _run_one_tick(job_callable, *, seconds=1, wait=6.0):
    """真起一个 BackgroundScheduler,注册、跑、关。返回是否在超时内跑到。"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    sched = BackgroundScheduler()
    sched.add_job(job_callable, trigger=IntervalTrigger(seconds=seconds),
                  id="c197_probe", replace_existing=True,
                  coalesce=True, max_instances=1)
    sched.start()
    try:
        return _RAN.wait(timeout=wait)
    finally:
        sched.shutdown(wait=False)


_RAN = threading.Event()
_CALLS = {"n": 0}


async def _fake_lane_tick():
    """桩的 lane:被真正执行时才会 +1。"""
    _CALLS["n"] += 1
    _RAN.set()
    return {"production": 1}


@pytest.fixture(autouse=True)
def _reset():
    _RAN.clear()
    _CALLS["n"] = 0
    yield


def test_c3_wrapped_async_job_actually_executes():
    from api.scheduler import run_async_in_scheduler

    ran = _run_one_tick(run_async_in_scheduler(_fake_lane_tick))

    assert ran, "包装后仍然没跑到 —— 本单的修法没生效"
    assert _CALLS["n"] >= 1


def test_c3_bare_async_job_never_runs_and_warns():
    """🔴 反向对照 + 病因复现:裸传 async ⇒ 计数恒 0,且出 never awaited 警告。

    少了这条,「包了之后能跑」并不能说明「不包就不能跑」——
    而后者才是本单要证明的那一半(它解释了 57/57 executed successfully 是假的)。
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ran = _run_one_tick(_fake_lane_tick)          # 裸传,不包
        assert not ran, "裸传居然跑到了 —— 那本单的根因判断就错了"
        assert _CALLS["n"] == 0

    msgs = [str(w.message) for w in caught]
    assert any("never awaited" in m for m in msgs), (
        "没看到 never awaited 警告 —— 那条警告是生产上唯一的痕迹,"
        "判据要能证明它确实会出现;实得:%s" % msgs[:3])


def test_c3_sync_job_passes_through_unchanged():
    """反向对照:包装器对**同步**函数原样放行(动态派发那张表靠这条)。"""
    from api.scheduler import run_async_in_scheduler

    def _sync_job():
        _CALLS["n"] += 1
        _RAN.set()

    assert run_async_in_scheduler(_sync_job) is _sync_job, (
        "同步函数被包了一层 —— 那会改变既有 87 处注册的行为")
    assert _run_one_tick(_sync_job)


# ══════════════════════════════════════════════════════════════════════
# 197-c5 · 协程抛的异常必须有落点(否则又是一次静默)
# ══════════════════════════════════════════════════════════════════════
async def _boom():
    _RAN.set()
    raise RuntimeError("c5-probe-boom")


def _run_in_live_loop(job_callable, wait=6.0):
    """在**真有 running loop** 的环境下跑一拍。

    🔴 必须走 `run_coroutine_threadsafe` 那条分支才测得到本条:
       没有 running loop 时包装器走 `asyncio.run`,异常会自己抛回 APScheduler,
       那条路本来就不静默 —— 在那条路上测,判据会假绿。
    """
    import asyncio

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    async def _main():
        from api.scheduler import run_async_in_scheduler
        wrapped = run_async_in_scheduler(job_callable)   # 在 loop 里包,捕获它
        sched = BackgroundScheduler()
        sched.add_job(wrapped, trigger=IntervalTrigger(seconds=1),
                      id="c197c5_probe", replace_existing=True)
        sched.start()
        try:
            for _ in range(int(wait * 10)):
                if _RAN.is_set():
                    await asyncio.sleep(0.3)      # 让 done_callback 跑完
                    return True
                await asyncio.sleep(0.1)
            return False
        finally:
            sched.shutdown(wait=False)

    return asyncio.run(_main())


def test_c5_async_job_exception_is_logged(caplog):
    import logging

    caplog.set_level(logging.ERROR, logger="GEO-Scheduler")
    assert _run_in_live_loop(_boom), "探针没跑到 —— 后面的断言没有参照物"

    hits = [r for r in caplog.records
            if "async job" in r.getMessage() and "_boom" in r.getMessage()]
    assert hits, (
        "协程抛了异常却没有任何落点 —— fire-and-forget 的 Future 把它吃了。"
        "实得日志:%s" % [r.getMessage()[:60] for r in caplog.records][:4])
    assert "c5-probe-boom" in repr(hits[0].getMessage()) or hits[0].exc_info, (
        "记了一行但没带原始异常 —— 只知道'有异常'查不出是什么")
