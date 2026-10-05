"""A3 回归防护:飞轮 job 必须**无条件注册**,禁止再放回 gated setup_schedule。

真实事故(生产实证 2026-07-28):`publish_media_effective_pool_distill` 与 `publish_outcome_sync`
只写在 `setup_schedule` 里,而 `setup_schedule` 只有 monitoring_config._global_.auto_monitor_enabled=1
才被 `load_saved_schedule` 调用 —— 生产该值 =0,于是这两个 job 从未被注册,
media_effective_pool 停在 2026-05-11 整整 78 天。这是 BUG-004 / P0-G / B1-4 / T5 同坑第五次复发。

本测直接跑 register_v32_core_tasks,断言它们真被注册。谁再把它们挪回 setup_schedule,这里立刻变红。
"""
import inspect

import pytest


def _register_into(fake, monkeypatch):
    """跑一次真实的 register_v32_core_tasks,把 job 收进传入的假 scheduler。

    这里刻意**不吞任何异常**:注册路径炸了就应该红,不能让本测试自己变成又一个静默吞。
    (测试库需要 geo_observation 相关迁移已 apply —— 尾部那段注册是 fail-loud 的。)
    """
    import api.scheduler as sch

    monkeypatch.setattr(sch, "get_scheduler", lambda: fake)
    sch.register_v32_core_tasks()
    return fake


def test_flywheel_block_registers_before_failloud_tail():
    """飞轮注册块必须排在"会抛异常的尾部注册"之前,否则一次尾部故障就把飞轮全带走。"""
    import inspect

    import api.scheduler as sch

    source = inspect.getsource(sch.register_v32_core_tasks)
    flywheel_at = source.index("A 段 · 飞轮闭环")
    tail_at = source.index("register_geo_observation_integration_jobs")
    assert flywheel_at < tail_at, "飞轮注册块被挪到 fail-loud 尾部之后了"


@pytest.fixture
def registered_jobs(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    fake = BackgroundScheduler()
    try:
        yield _register_into(fake, monkeypatch)
    finally:
        try:
            if fake.running:
                fake.shutdown(wait=False)
        except Exception:
            pass


@pytest.mark.parametrize("job_id", [
    "publish_media_effective_pool_distill",
    "publish_outcome_sync",
    "media_entity_outcome_sync",
    "writing_assignment_outcome_backfill",
    "source_signal_lineage_backfill",
    "flywheel_job_watchdog",
])
def test_flywheel_job_registered_unconditionally(registered_jobs, job_id):
    job = registered_jobs.get_job(job_id)
    assert job is not None, (
        f"{job_id} 未在 register_v32_core_tasks 注册 —— "
        "放回 gated setup_schedule(auto_monitor_enabled·prod=0)= 生产永不触发"
    )
    assert not inspect.iscoroutinefunction(job.func), \
        "BackgroundScheduler 不 await coroutine,必须注册同步 callable"


def test_selftest_probe_not_registered_by_default(registered_jobs):
    """必失败探针默认不进生产调度,只有显式开 FLYWHEEL_HEARTBEAT_SELFTEST 才注册。"""
    assert registered_jobs.get_job("flywheel_selftest_failing") is None


def test_selftest_probe_registered_when_enabled(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setenv("FLYWHEEL_HEARTBEAT_SELFTEST", "1")
    fake = BackgroundScheduler()
    try:
        _register_into(fake, monkeypatch)
        assert fake.get_job("flywheel_selftest_failing") is not None
    finally:
        try:
            if fake.running:
                fake.shutdown(wait=False)
        except Exception:
            pass


def test_every_registered_flywheel_job_is_wrapped_by_heartbeat(registered_jobs):
    """注册进调度器的飞轮 job 必须都带心跳包裹,否则它跑没跑成又看不见了。"""
    expected = {
        "publish_media_effective_pool_distill": "media_effective_pool_distill",
        "publish_outcome_sync": "publish_outcome_sync",
        "media_entity_outcome_sync": "media_entity_outcome_sync",
        "writing_assignment_outcome_backfill": "writing_assignment_outcome_backfill",
        "source_signal_lineage_backfill": "source_signal_lineage_backfill",
        "flywheel_job_watchdog": "flywheel_watchdog",
    }
    for job_id, job_key in expected.items():
        func = registered_jobs.get_job(job_id).func
        assert getattr(func, "__flywheel_job_key__", None) == job_key, (
            f"{job_id} 没有被 @flywheel_job('{job_key}') 包裹 —— 失败会重新变成静默"
        )


def test_research_round_and_catchup_are_heartbeat_wrapped():
    """采集轮与漏跑自愈同样必须留痕(它们由 schedule_research_monitor_jobs 注册)。"""
    from services.research_monitor import scheduler_setup

    assert getattr(
        scheduler_setup.trigger_research_round_sync, "__flywheel_job_key__", None
    ) == "research_auto_round"
    assert getattr(
        scheduler_setup.run_missed_round_catchup, "__flywheel_job_key__", None
    ) == "research_missed_round_catchup"


def test_registry_covers_every_wrapped_job_key():
    """看门狗只盯注册表里的 job;有 job 被包了心跳却没登记 = 停跑了也没人报。"""
    from services.flywheel_heartbeat import FLYWHEEL_JOBS, WATCHDOG_JOB_KEY

    wrapped_keys = {
        "research_auto_round",
        "research_missed_round_catchup",
        "media_effective_pool_distill",
        "publish_outcome_sync",
        "media_entity_outcome_sync",
        "writing_outcome_backfill",
        "writing_effectiveness_review",
        "writing_assignment_outcome_backfill",
        "source_signal_lineage_backfill",
    }
    missing = wrapped_keys - set(FLYWHEEL_JOBS)
    assert not missing, f"这些 job 有心跳但没进看门狗注册表:{sorted(missing)}"
    # 看门狗自己刻意不进注册表(不自我监控),但必须有独立 job_key。
    assert WATCHDOG_JOB_KEY not in FLYWHEEL_JOBS
