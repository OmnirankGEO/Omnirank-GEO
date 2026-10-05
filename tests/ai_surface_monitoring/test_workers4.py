"""调度单发**契约**仿真(04 §5.2)· 复审 P2-9 诚实口径。

本文件用内存 claim 仿真 sched_claim 的 UNIQUE(job_name, scheduled_at) 语义,验证**本包 job 骨架
正确依赖 claim 去重**(同 bucket 4 实例恰 1 执行)。它 **不是** 四进程真 PostgreSQL 证据:
  - 真 sched_claim(PostgreSQL sched_job_runs UNIQUE)是**既有设施**,本包只 REUSE 不改;
    其四进程/蓝绿真库单发由既有 sched_claim + 集成者在有 DB 的 staging 验证。
  - 本包**预算跨实例原子性**由真 threading.Lock 共享账本证明(见
    test_review_fixes.py::test_two_service_instances_share_ledger_atomic,精确复现 P1-3 并阻止越上限);
    生产 PostgreSQL 原子 SQL(advisory lock + 条件插入 + ON CONFLICT 幂等)见 migrations_stub.py。
"""

from __future__ import annotations

import asyncio

import pytest

from services.ai_surface_monitoring import scheduler_wiring as sw
from services.ai_surface_monitoring.scheduler_wiring import register_observation_collection_jobs, set_sampling_driver


class _FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, func, trigger, *, id, name, replace_existing):
        self.jobs[id] = func


class _CountingDriver:
    def __init__(self):
        self.calls = 0

    async def run_tick(self, source_kind: str):
        self.calls += 1
        await asyncio.sleep(0.005)
        return {"source_kind": source_kind, "ok": True}


def _bucketed_fake_claim(fixed_bucket: int, seen: set):
    """模拟 sched_claim:同 (job_name, bucket) 只放行一次(UNIQUE 去重)。"""
    def sched_claim(job_name, period_seconds, **kw):
        def deco(fn):
            async def wrapper(*a, **k):
                key = (job_name, fixed_bucket)
                if key in seen:
                    return None          # 已被别的实例 claim → 跳过(不执行业务)
                seen.add(key)
                return await fn(*a, **k)
            return wrapper
        return deco
    return sched_claim


async def test_single_fire_under_four_concurrent_leaders():
    driver = _CountingDriver()
    set_sampling_driver(driver)
    try:
        seen: set = set()
        claim = _bucketed_fake_claim(fixed_bucket=42, seen=seen)
        sched = _FakeScheduler()
        # identity sync(测试在事件循环内,直接 await 异步 job)
        register_observation_collection_jobs(
            sched, sched_claim=claim, scheduler_sync_callable=lambda fn: fn,
            trigger_factory=lambda kind, kw, tz: None, timezone=None)

        job = sched.jobs["ai_surface_obs_monitoring_daily"]
        # 4 个实例同一 tick 并发触发同一 job
        results = await asyncio.gather(*[job() for _ in range(4)])
        executed = [r for r in results if r is not None]
        assert driver.calls == 1, f"同一 tick 执行了 {driver.calls} 次(应恰 1)"
        assert len(executed) == 1
    finally:
        set_sampling_driver(sw._NoopDriver())


async def test_distinct_ticks_each_fire_once():
    driver = _CountingDriver()
    set_sampling_driver(driver)
    try:
        seen: set = set()
        sched = _FakeScheduler()
        # 两个不同 bucket → 两次执行
        for bucket in (100, 101):
            claim = _bucketed_fake_claim(fixed_bucket=bucket, seen=seen)
            s = _FakeScheduler()
            register_observation_collection_jobs(
                s, sched_claim=claim, scheduler_sync_callable=lambda fn: fn,
                trigger_factory=lambda kind, kw, tz: None)
            await s.jobs["ai_surface_obs_monitoring_daily"]()
        assert driver.calls == 2
    finally:
        set_sampling_driver(sw._NoopDriver())


async def test_noop_driver_is_safe_when_unwired():
    # 默认 driver 未接线:tick 返回 wired=False,不抛(集成者接线前不炸)
    res = await sw._NoopDriver().run_tick("monitoring")
    assert res["wired"] is False
