"""
Social Studio 订阅 cron 调度模块 V3.1

来源: docs/AI-CONTEXT/SOCIAL_STUDIO_PRICING_V3_1_EXECUTION_2026-05-10.md
计划: .planning/phases/07-social-studio-subscription/PLAN.md B08
红线: RED_LINES.md R4(避免改 api/scheduler.py,本模块独立注册)

V3.1 5 个 cron task:
  1. 月底 1 号 00:01 — entitlements 重置 + auto_renew 续费尝试
  2. 每天 03:00 — grace period 检查(7 日宽限期)
  3. 每天 04:00 — T+3 佣金 settle
  4. 每天 05:00 — clawback 待清算审计(>30 天 written_off)
  5. 每小时 :15 — 漏佣 reconciliation(Bug 4 修 · Codex Round 3)

红线规避:
  - 不修改 api/scheduler.py
  - 复用现有 BackgroundScheduler 实例(get_scheduler())
  - 错峰编排:00:01 / 03 / 04 / 05 不撞现有 daily_monitoring(随用户 monitoring_start_hour) / daily_service_check(01:00) / daily_compliance_check
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict

from apscheduler.triggers.cron import CronTrigger

from services.sched_claim import sched_claim  # [WORKERS=4 · SPEC §2.2] per-job tick 级 claim(第二层防双跑)

logger = logging.getLogger("GEO-Subscription-Scheduler-V31")


# ==========================================================================
# Job entry: monthly entitlements reset
# ==========================================================================

def monthly_subscription_reset_all():
    """每月 1 号 00:01 — 遍历 active 订阅 reset entitlements + 触发 auto_renew

    幂等:month_end_reset 内部用 last_renewal_attempt_at 防重跑
    分批:每批 1000 用户,避免长事务
    """
    from db.connection import get_connection
    from middleware.subscription_billing import month_end_reset

    logger.info("[SubScheduler] monthly_subscription_reset_all start")
    start = datetime.utcnow()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM user_social_subscriptions
            WHERE status = 'active' AND expires_at > CURRENT_TIMESTAMP
            ORDER BY id
            LIMIT 100000
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    sub_ids = [int(r["id"]) for r in rows]
    success = 0
    failed = 0

    for sid in sub_ids:
        try:
            month_end_reset(sid)
            success += 1
        except Exception as e:
            logger.error(f"[SubScheduler] reset failed sub_id={sid} err={e}")
            failed += 1

    elapsed = (datetime.utcnow() - start).total_seconds()
    logger.info(f"[SubScheduler] monthly_subscription_reset_all done "
                f"total={len(sub_ids)} ok={success} fail={failed} elapsed={elapsed:.1f}s")
    return {"total": len(sub_ids), "success": success, "failed": failed}


# ==========================================================================
# Job entry: grace period 检查
# ==========================================================================

# ==========================================================================
# Job entry: T+3 佣金 settle
# ==========================================================================

# ==========================================================================
# Job entry: clawback 待清算审计
# ==========================================================================

# ==========================================================================
# 注册 4 cron task(在 server.py startup 调用)
# ==========================================================================

def register_subscription_jobs() -> Dict[str, Any]:
    """注册 1 个 cron(对齐 api.scheduler 的 BackgroundScheduler 单例)

    错峰编排(对齐 CST):
      00:01 monthly_subscription_reset_all(每月 1 号)

    🔴 WO_308 ④(2026-09-27):03:00 subscription_grace_period_check(按 auto_renew 从钱包扣 paid_points 续期)
       摘掉,函数本体做成 no-op;到期由读取侧的 expires_at 条件自然失效。

    🔴 WO_308(2026-09-27 · Review 定):订阅比例佣金已停用,原来的 3 个佣金 cron 摘掉 ——
       04:00 daily_subscription_commission_settle_job · 05:00 daily_clawback_resolve_check_job ·
       每小时 :15 hourly_commission_reconciliation_job。三个 job 函数本体留着(随 E3 删),只是不再注册。
       锁:tests/wo308_retire_subscription_commission_2026_09_27(调度表里不许再出现这 3 个 id)
    """
    try:
        from api.scheduler import get_scheduler, BEIJING_TZ
    except ImportError:
        logger.error("[SubScheduler] api.scheduler 不可用,无法注册 jobs")
        return {"success": False, "error": "scheduler_unavailable"}

    scheduler = get_scheduler()
    if not scheduler:
        return {"success": False, "error": "scheduler_not_initialized"}

    # [WORKERS=4 · SPEC §2.2] period = tick 级 claim 分桶秒(该 job cron 间隔);monthly 用 20d 粗桶(近似)。
    jobs_config = [
        {
            "id": "subscription_monthly_reset",
            "name": "订阅月底重置 entitlements",
            "func": monthly_subscription_reset_all,
            "trigger": CronTrigger(day=1, hour=0, minute=1, timezone=BEIJING_TZ),
            "period": 20 * 86400,
        },
    ]

    registered = []
    for job in jobs_config:
        try:
            scheduler.add_job(
                func=sched_claim(job["id"], job["period"])(job["func"]),  # [WORKERS=4] tick 级 claim(叠业务幂等)
                trigger=job["trigger"],
                id=job["id"],
                name=job["name"],
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
            registered.append(job["id"])
            logger.info(f"[SubScheduler] registered: {job['id']}")
        except Exception as e:
            logger.error(f"[SubScheduler] register {job['id']} failed: {e}")

    return {"success": True, "registered": registered}


def unregister_subscription_jobs():
    """回滚:从 scheduler 撤掉 4 个 jobs"""
    try:
        from api.scheduler import get_scheduler
        scheduler = get_scheduler()
        for job_id in ("subscription_monthly_reset", "subscription_grace_check",
                       "subscription_commission_settle", "subscription_clawback_resolve",
                       "subscription_commission_reconcile"):
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
                logger.info(f"[SubScheduler] unregistered: {job_id}")
    except Exception as e:
        logger.error(f"[SubScheduler] unregister failed: {e}")
