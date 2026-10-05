"""调度接线纯函数(04 §5.2):注册 + sched_claim 包裹 + period 对齐 + 幂等 + 绝不 start/碰 leader。"""

from __future__ import annotations

import inspect

import pytest

from services.ai_surface_monitoring import scheduler_wiring as sw
from services.ai_surface_monitoring.scheduler_wiring import (
    default_job_specs,
    register_observation_collection_jobs,
)


class FakeScheduler:
    def __init__(self, existing=None):
        self.jobs = dict(existing or {})
        self.added = []
        self.started = False

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def add_job(self, func, trigger, *, id, name, replace_existing):
        self.jobs[id] = {"func": func, "trigger": trigger, "name": name, "replace_existing": replace_existing}
        self.added.append(id)

    def start(self):  # 绝不应被 register 调用
        self.started = True


def _spy_wrappers():
    claim_calls = []
    sync_calls = []

    def spy_sched_claim(job_name, period_seconds, **kw):
        claim_calls.append((job_name, period_seconds, kw))

        def deco(fn):
            fn._claimed = (job_name, period_seconds)
            return fn
        return deco

    def spy_sync(fn):
        sync_calls.append(fn)
        fn._synced = True
        return fn

    def fake_trigger(kind, kwargs, tz):
        return {"kind": kind, "kwargs": kwargs, "tz": bool(tz)}

    return spy_sched_claim, spy_sync, fake_trigger, claim_calls, sync_calls


def test_registers_all_default_jobs_wrapped_and_period_aligned():
    sched = FakeScheduler()
    claim, sync, trig, claim_calls, sync_calls = _spy_wrappers()
    result = register_observation_collection_jobs(
        sched, sched_claim=claim, scheduler_sync_callable=sync, trigger_factory=trig, timezone="TZ")

    specs = {s.job_id: s for s in default_job_specs()}
    assert set(result["registered"]) == set(specs.keys())
    assert set(sched.added) == set(specs.keys())
    # 每个 job:sched_claim(job_id, period)包裹 + scheduler_sync_callable 外层
    for job_name, period, _ in claim_calls:
        assert period == specs[job_name].period_seconds
    assert len(sync_calls) == len(specs)
    # period_seconds 必须等于 trigger 间隔(interval 类)
    for s in specs.values():
        if s.trigger_kind == "interval":
            secs = s.trigger_kwargs.get("hours", 0) * 3600 + s.trigger_kwargs.get("minutes", 0) * 60
            assert secs == s.period_seconds, f"{s.job_id} period 未对齐 trigger"


def test_register_never_starts_scheduler():
    sched = FakeScheduler()
    claim, sync, trig, *_ = _spy_wrappers()
    register_observation_collection_jobs(sched, sched_claim=claim, scheduler_sync_callable=sync, trigger_factory=trig)
    assert sched.started is False


def test_register_is_idempotent_skips_existing():
    sched = FakeScheduler(existing={"ai_surface_obs_monitoring_daily": {"func": None}})
    claim, sync, trig, *_ = _spy_wrappers()
    result = register_observation_collection_jobs(
        sched, sched_claim=claim, scheduler_sync_callable=sync, trigger_factory=trig)
    assert "ai_surface_obs_monitoring_daily" in result["skipped"]
    assert "ai_surface_obs_monitoring_daily" not in sched.added


def test_register_source_never_touches_leader_or_get_scheduler():
    """静态守卫:纯函数绝不 *调用* get_scheduler / scheduler.start / set_cron_active / LeaderLock。

    用 AST 检查真实调用(不误伤 docstring/注释里对"禁止项"的说明)。
    """
    import ast
    import textwrap

    src = textwrap.dedent(inspect.getsource(register_observation_collection_jobs))
    tree = ast.parse(src)
    called_names: set = set()
    used_names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                called_names.add(f.id)
            elif isinstance(f, ast.Attribute):
                called_names.add(f.attr)
        if isinstance(node, ast.Name):
            used_names.add(node.id)
    assert "get_scheduler" not in called_names   # 不自取 scheduler
    assert "set_cron_active" not in called_names  # 不碰 leader gate
    assert "start" not in called_names            # 不 scheduler.start()
    assert "acquire_lock" not in called_names
    assert "LeaderLock" not in used_names
    # 整个模块不得 import api.scheduler / 根 scheduler(不误伤对 "api/scheduler.py" 的散文引用)
    mod_src = inspect.getsource(sw)
    assert "from api.scheduler import" not in mod_src
    assert "import api.scheduler\n" not in mod_src


def test_default_job_periods_match_triggers():
    for s in default_job_specs():
        if s.trigger_kind == "interval":
            secs = s.trigger_kwargs.get("hours", 0) * 3600 + s.trigger_kwargs.get("minutes", 0) * 60
            assert secs == s.period_seconds
        assert s.period_seconds > 0
