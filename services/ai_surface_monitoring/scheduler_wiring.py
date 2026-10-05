"""调度接线(纯函数)· 由最终集成者调用,AI-1 绝不改 api/scheduler.py / scheduler.py。

约定(复用 register_v3_3_1_jobs / register_channel_tier_jobs 惯例 · 见 04 §5.2 复用地图):
  - ``register_observation_collection_jobs(scheduler)`` 只接受 scheduler 参数,只做
    ``scheduler.get_job(id)`` + ``scheduler.add_job(..., replace_existing=True)``。
  - **禁止** 在本函数内:``get_scheduler()`` / ``scheduler.start()`` / ``set_cron_active()`` /
    获取 LeaderLock / 重实现 ROLE/backup 判断。单发由中央 leader 闸(层1)+ sched_claim(层2)保证。
  - 异步 job 必须 ``scheduler_sync_callable(sched_claim(name, period)(async_fn))`` 包裹
    (BackgroundScheduler worker 无事件循环;period_seconds 必须等于 trigger 间隔秒)。
  - money/副作用 job 保持 sched_claim 默认 ``fail_open=False, reclaimable=False``。

真实采样 driver(工作清单拉取:订阅/轮次)属集成语义接线:本包提供 job 骨架 + 注入点
``set_sampling_driver``,由集成者提供真实 driver。默认 driver 为安全 no-op(仅告警日志)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol

from services.geo_observation.collection_mode import (
    ObservationCollectionMode,
    parse_collection_mode,
)

logger = logging.getLogger("GEO-AISurface-Sched")

# job_name 命名空间(避免撞既有 sched_job_runs 行)
JOB_MONITORING_DAILY = "ai_surface_obs_monitoring_daily"
JOB_RESEARCH_TICK = "ai_surface_obs_research_tick"
JOB_RECONCILER = "ai_surface_obs_reconciler"


class ObservationSamplingDriver(Protocol):
    """采样 driver:拉取当期工作清单并执行采集。由集成者注入真实实现。"""

    async def run_tick(self, source_kind: str) -> Dict[str, Any]:
        ...


class _NoopDriver:
    async def run_tick(self, source_kind: str) -> Dict[str, Any]:
        logger.warning("[ai_surface] 采样 driver 未接线(source_kind=%s)· 集成者需 set_sampling_driver", source_kind)
        return {"source_kind": source_kind, "wired": False}


_driver: ObservationSamplingDriver = _NoopDriver()
# 预留回收器(集成者注入 ledger.reap_stale_reservations;默认 no-op)。防孤儿预留饿死预算。
_reservation_reaper: Optional[Callable[[], int]] = None


def set_sampling_driver(driver: ObservationSamplingDriver) -> None:
    """集成者在启动时注入真实采样 driver。"""
    global _driver
    _driver = driver


def set_reservation_reaper(reaper: Optional[Callable[[], int]]) -> None:
    """集成者注入 ledger.reap_stale_reservations,reconciler tick 会调用它回收孤儿预留。"""
    global _reservation_reaper
    _reservation_reaper = reaper


def _get_driver() -> ObservationSamplingDriver:
    return _driver


def _driver_wired() -> bool:
    return not isinstance(_driver, _NoopDriver)


def check_readiness(
    mode: ObservationCollectionMode | str = ObservationCollectionMode.NATIVE_SAMPLING_DRIVER,
) -> Dict[str, Any]:
    """Return mode-aware control-plane readiness.

    Existing-collector mode deliberately owns no sampling driver or reservation
    budget, so requiring them would make its truthful reconciler path impossible
    to open. Native mode keeps the original fail-closed driver/reaper contract.
    """
    selected = parse_collection_mode(mode)
    if selected is ObservationCollectionMode.EXISTING_COLLECTORS_RECONCILED:
        return {
            "mode": selected.value,
            "ready": True,
            "status": "ready",
            "problems": [],
        }
    problems = []
    if not _driver_wired():
        problems.append("sampling driver 未注入(set_sampling_driver);采集不能实跑")
    if _reservation_reaper is None:
        problems.append("reservation reaper 未注入(set_reservation_reaper);孤儿预留无法回收")
    return {
        "mode": selected.value,
        "ready": not problems,
        "status": "ready" if not problems else "unavailable",
        "problems": problems,
    }


# ---- job 协程体(每个都是幂等的 tick;真正工作交给注入的 driver) ----
async def _job_monitoring_daily() -> Dict[str, Any]:
    if not _driver_wired():
        logger.error("[ai_surface] monitoring tick 触发但 driver 未接线 → 失败(非假成功)")
        return {"source_kind": "monitoring", "ok": False, "wired": False}
    return await _get_driver().run_tick("monitoring")


async def _job_research_tick() -> Dict[str, Any]:
    if not _driver_wired():
        logger.error("[ai_surface] research tick 触发但 driver 未接线 → 失败(非假成功)")
        return {"source_kind": "research", "ok": False, "wired": False}
    return await _get_driver().run_tick("research")


async def _job_reconciler() -> Dict[str, Any]:
    reaped = 0
    if _reservation_reaper is not None:
        try:
            reaped = await __import__("asyncio").to_thread(_reservation_reaper)  # 同步 DB 调用卸载到线程
        except Exception as exc:
            logger.error("[ai_surface] 预留回收失败: %s", exc)
    out = await _get_driver().run_tick("reconcile")
    if isinstance(out, dict):
        out["reservations_reaped"] = reaped
    return out


@dataclass(frozen=True)
class ObservationJobSpec:
    job_id: str
    async_fn: Callable
    period_seconds: int          # 必须等于 trigger 间隔秒(sched_claim _bucket 对齐)
    trigger_kind: str            # 'cron' | 'interval'
    trigger_kwargs: Dict[str, Any]


def default_job_specs() -> List[ObservationJobSpec]:
    return [
        # 监测:每日 09:00(与既有 daily_monitoring 同量级);period=1 天
        ObservationJobSpec(JOB_MONITORING_DAILY, _job_monitoring_daily, 86400, "cron", {"hour": 9, "minute": 0}),
        # 调研分层 tick:每 6 小时检查当期该跑什么(实际频次由 policy/工作清单决定);period=6h
        ObservationJobSpec(JOB_RESEARCH_TICK, _job_research_tick, 21600, "interval", {"hours": 6}),
        # reconciler:每 10 分钟补登失败的来源登记;period=10min
        ObservationJobSpec(JOB_RECONCILER, _job_reconciler, 600, "interval", {"minutes": 10}),
    ]


def _default_trigger_factory(kind: str, kwargs: Dict[str, Any], timezone: Any) -> Any:
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    if kind == "cron":
        return CronTrigger(timezone=timezone, **kwargs) if timezone else CronTrigger(**kwargs)
    if kind == "interval":
        return IntervalTrigger(timezone=timezone, **kwargs) if timezone else IntervalTrigger(**kwargs)
    raise ValueError(f"未知 trigger_kind: {kind}")


def _beijing_tz() -> Any:
    try:
        import pytz

        return pytz.timezone("Asia/Shanghai")
    except Exception:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo("Asia/Shanghai")
        except Exception:
            return None


def register_observation_collection_jobs(
    scheduler: Any,
    *,
    jobs: Optional[List[ObservationJobSpec]] = None,
    sched_claim: Optional[Callable] = None,
    scheduler_sync_callable: Optional[Callable] = None,
    trigger_factory: Optional[Callable] = None,
    timezone: Any = None,
) -> Dict[str, List[str]]:
    """纯函数:把观测采集 job 注册到传入的 scheduler。集成者调用(如 get_scheduler() 创建块旁)。

    返回 {'registered': [...], 'skipped': [...]}。绝不 start scheduler、绝不碰 leader/gate。
    """
    specs = jobs if jobs is not None else default_job_specs()

    # 惰性拿真实 sched_claim / scheduler_sync_callable(测试可注入 spy)
    if sched_claim is None or scheduler_sync_callable is None:
        from services.sched_claim import (
            sched_claim as _real_sched_claim,
            scheduler_sync_callable as _real_sync,
        )
        sched_claim = sched_claim or _real_sched_claim
        scheduler_sync_callable = scheduler_sync_callable or _real_sync

    trigger_factory = trigger_factory or _default_trigger_factory
    if timezone is None:
        timezone = _beijing_tz()

    registered: List[str] = []
    skipped: List[str] = []
    for spec in specs:
        if scheduler.get_job(spec.job_id) is not None:
            skipped.append(spec.job_id)
            continue
        # 层2 单发:sched_claim 内层(异步 fn → 异步 wrapper),scheduler_sync_callable 外层(loop-less worker)
        claimed = sched_claim(spec.job_id, spec.period_seconds)(spec.async_fn)
        runnable = scheduler_sync_callable(claimed)
        trigger = trigger_factory(spec.trigger_kind, spec.trigger_kwargs, timezone)
        scheduler.add_job(runnable, trigger, id=spec.job_id, name=spec.job_id, replace_existing=True)
        registered.append(spec.job_id)

    logger.info("[ai_surface] 观测采集 job 注册: registered=%s skipped=%s", registered, skipped)
    return {"registered": registered, "skipped": skipped}
