"""集成接线点(AI-2 提供,集成者调用;AI-2 不改 server.py/api/scheduler.py)。

- get_admin_router():集成者在 server.py 注册(fail-fast,不 except pass 静默缺功能)。
- register_geo_observation_jobs(scheduler):集成者在 api/scheduler.py 的 register_v32_core_tasks 内调用;
  只由 ROLE=cron 单控制面注册;每 job DB claim(sched_claim)+ 幂等。
- verify_schema_on_startup():集成者在 web/cron 启动做 fail-closed readiness。
- source hook 锚点(R8 同事务):集成者在真实终态后接线:
    · 付费诊断 commit 成功后 → source_hooks.register_paid_diagnosis(cur, run_token, finished_at)
    · 监测结果持久化后   → source_hooks.register_monitoring_result(cur, result_id)
    · 调研 raw 落库后     → source_hooks.register_research_raw(cur, raw_id)
  无法同事务接线的源 → 由 reconciler 从终态表补登记(已内置)。

晋升治理:全部在 geo_observation_policy DB SSOT(CAS)。worker 每次决策读新鲜值,晋升事务内 FOR SHARE 重读——
- 法务依据/同意版本:管理员经 `PUT /api/admin/geo-observation/policy/promotion-governance` 批准 promotion_legal_basis;
- 金标准门 outcome_gold_gate_passed:**不可直接提交**(P1-1),只能经 `POST /api/admin/geo-observation/policy/gold-evaluation`
  提交评估证据(样本≥100·macro-F1≥9000·高风险误推荐=0),由服务端派生 + DB CHECK 兜底;
- 关闭 promotion_enabled(policy PUT)立即停止晋升(提交返回后在途晋升读到关后值)。不再向 worker 传冻结 ctx。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import policy
from .promotion import process_next_pending
from .reconciler import run_reconciler

logger = logging.getLogger("GEO-ObservationWiring")


def get_admin_router():
    """返回管理治理 router(集成者在 server.py 注册)。"""
    from api.geo_observation_admin_api import router
    return router


def verify_schema_on_startup() -> None:
    from .readiness import verify_on_startup
    verify_on_startup()


async def _promotion_batch(max_events: int = 50) -> int:
    processed = 0
    for _ in range(max_events):
        r = await process_next_pending()   # ctx=None → 每次读新鲜 policy 治理(P1-1)
        if r is None:
            break
        processed += 1
    return processed


def promotion_worker_job(max_events: int = 50) -> int:
    """一 tick 处理最多 max_events 条 pending(同步入口,内部跑 async 管线;单条异常不中断批)。"""
    return asyncio.run(_promotion_batch(max_events=max_events))


def reconciler_job() -> dict:
    return run_reconciler(
        registration_enabled=policy.is_flag_enabled("ingest_enabled"),
    )


def reconciler_with_inventory_job(scheduler) -> dict:
    """Run reconciliation, then durably publish the real cron-leader job set."""
    result = reconciler_job()
    from .integration import record_scheduler_inventory

    record_scheduler_inventory([str(job.id) for job in scheduler.get_jobs()])
    return result


def register_geo_observation_jobs(scheduler) -> list[str]:
    """在给定 APScheduler 上注册晋升 worker + reconciler(ROLE=cron 单控制面)。返回注册的 job id。

    每 job 用 sched_claim(DB claim + 幂等 + 心跳);flag 关时 job 内部 no-op(不在 gated setup 里跳过注册)。
    晋升治理由 policy SSOT 控制,worker 每次读新鲜值,无需传 ctx。集成者从 register_v32_core_tasks 调用。
    """
    from apscheduler.triggers.interval import IntervalTrigger
    from services.sched_claim import sched_claim

    registered: list[str] = []

    def _promotion_tick():
        return promotion_worker_job(max_events=50)

    def _reconciler_tick():
        return reconciler_with_inventory_job(scheduler)

    if not scheduler.get_job("geo_observation_promotion"):
        scheduler.add_job(
            sched_claim("geo_observation_promotion", 60, reclaimable=True)(_promotion_tick),
            trigger=IntervalTrigger(seconds=60), id="geo_observation_promotion", replace_existing=True,
        )
        registered.append("geo_observation_promotion")

    if not scheduler.get_job("geo_observation_reconciler"):
        scheduler.add_job(
            sched_claim("geo_observation_reconciler", 300, reclaimable=True, fail_open=True)(_reconciler_tick),
            trigger=IntervalTrigger(seconds=300), id="geo_observation_reconciler", replace_existing=True,
        )
        registered.append("geo_observation_reconciler")

    logger.info("registered geo observation jobs: %s", registered)
    return registered
