"""
V3.3.1 服务费定时任务

3 个 cron:
    - service_fee_t3_settle()        每日 04:00 · T+3 pending → settled
    - service_fee_t7_bonus_settle()  每日 04:15 · bonus T+7 pending → settled · 入账 bonus_points
    - service_fee_anomaly_check()    每日 05:00 · 扫 30 天内异常退款 4 类 · 触发追索
    - service_fee_quota_reset_check() 每月 1 号 06:00 · 月度配额行预创建(可选)

注意:
- 调度集中由 api/scheduler.py 注册 · 本模块只提供函数
- 全部幂等 · 重跑 0 错
- V3_3_1_ENABLED 关时直接 skip

关联:
- 决策书 §3.2 §3.4
- RED_LINES R8
- IDENTITY_DECISIONS_LOCK Q12
"""

import logging
from datetime import datetime
from typing import Dict, Any

from db.connection import get_db
from config.v3_3_1_flags import is_v3_3_1_enabled
from services.service_fee_engine import settle_due_service_fees
from services.sched_claim import scheduler_sync_callable

logger = logging.getLogger("GEO-ServiceFee-Cron")


async def service_fee_t3_settle() -> Dict[str, Any]:
    """T+3 cron · pending → settled"""
    if not is_v3_3_1_enabled():
        logger.info("service_fee_t3_settle: V3.3.1 disabled · skip")
        return {"skipped": True, "reason": "v3_3_1_disabled"}
    try:
        result = settle_due_service_fees(dry_run=False)
        logger.info("service_fee_t3_settle: %s", result)
        return result
    except Exception as exc:
        logger.exception("service_fee_t3_settle failed: %s", exc)
        return {"skipped": False, "error": str(exc)}


async def service_fee_t7_bonus_settle() -> Dict[str, Any]:
    """T+7 cron · pending_bonus_records pending → settled · 入账 bonus_points"""
    if not is_v3_3_1_enabled():
        return {"skipped": True, "reason": "v3_3_1_disabled"}

    settled = 0
    skipped_refunded = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, referrer_id, bonus_points, recharge_order_id
                  FROM pending_bonus_records
                 WHERE status = 'pending' AND settle_at <= NOW()
                """,
            )
            rows = cur.fetchall()
            for r in rows:
                rid = r["id"] if isinstance(r, dict) else r[0]
                referrer_id = r["referrer_id"] if isinstance(r, dict) else r[1]
                bonus_pts = int(r["bonus_points"] if isinstance(r, dict) else r[2])
                order_id = r["recharge_order_id"] if isinstance(r, dict) else r[3]

                # 二次核对源订单退款(recharge_orders 实际字段 payment_status)
                if order_id:
                    cur.execute(
                        "SELECT payment_status FROM recharge_orders WHERE id=%s",
                        (order_id,),
                    )
                    o = cur.fetchone()
                    if o:
                        status = o["payment_status"] if isinstance(o, dict) else o[0]
                        if status in ("refunded", "partially_refunded"):
                            cur.execute(
                                "UPDATE pending_bonus_records SET status='cancelled', cancel_reason='auto:refunded_before_settle' WHERE id=%s",
                                (rid,),
                            )
                            skipped_refunded += 1
                            continue

                # 入账 bonus_points（先 claim:CAS 翻 settled）
                cur.execute(
                    """
                    UPDATE pending_bonus_records SET status='settled', settled_at=NOW()
                     WHERE id = %s AND status='pending'
                    """,
                    (rid,),
                )
                # [蓝绿双跑幂等] settle CAS rowcount==0 = 本行已被另一实例/重跑 settle 掉 → 跳过,绝不二次入账。
                # 锁层非唯一防线:redis scheduler 锁兜单实例,此处数据层 CAS 兜锁失效/蓝绿切换窗口双跑双发。
                # 必须在【入账前】判:原代码 settle 后无视 rowcount 直接加 bonus → 双跑双发。
                if cur.rowcount == 0:
                    continue
                cur.execute(
                    """
                    UPDATE user_wallets SET bonus_points = COALESCE(bonus_points, 0) + %s
                     WHERE user_id = %s
                    """,
                    (bonus_pts, referrer_id),
                )
                # [BUG-P3] referrer 无 user_wallets 行 → UPDATE 0 行 + 下方 INSERT...SELECT 也插 0 行
                # → bonus 静默丢失但记录已置 settled(无日志无告警 · 事后无法发现)。
                # rowcount==0 → 撤销 settled 标 failed 留人工(勿先置 settled 后入账)。
                if cur.rowcount == 0:
                    cur.execute(
                        "UPDATE pending_bonus_records SET status='failed', settled_at=NULL WHERE id=%s",
                        (rid,),
                    )
                    logger.error(
                        "[T+7 bonus settle] referrer_id=%s 无 user_wallets 行 · bonus %s 未入账 · "
                        "记录 #%s 标 failed 留人工(防静默丢失)", referrer_id, bonus_pts, rid,
                    )
                    continue
                cur.execute(
                    """
                    INSERT INTO point_transactions (
                        user_id, type, point_type, amount, balance_after,
                        description, order_id, source, created_at
                    )
                    SELECT %s, 'referral_bonus_settled', 'bonus', %s,
                           COALESCE(bonus_points, 0),
                           %s, %s, 'external_cash_payment', NOW()
                      FROM user_wallets WHERE user_id = %s
                    """,
                    (referrer_id, bonus_pts,
                     f"推荐奖励 settled #{rid}", order_id, referrer_id),
                )
                settled += 1
            conn.commit()
    logger.info("service_fee_t7_bonus_settle: settled=%d cancelled=%d", settled, skipped_refunded)
    return {"settled": settled, "cancelled_due_refund": skipped_refunded}


async def service_fee_failed_jobs_retry() -> Dict[str, Any]:
    """Codex 三审 P1-4:补扫 failed_service_fee_jobs · 重试 V3.3.1 hook 失败的充值返佣

    流程:
    1. 取 status='pending' AND retry_count < max_retries 的记录
    2. 每条调 record_v3_3_1_rewards 重试
    3. 成功 → status='succeeded' / 失败 → retry_count++
    4. retry_count >= max_retries → status='abandoned'(财务手动处理)
    """
    if not is_v3_3_1_enabled():
        return {"skipped": True, "reason": "v3_3_1_disabled"}

    import json
    from services.service_fee_calculator import record_v3_3_1_rewards

    retried = 0
    succeeded = 0
    abandoned = 0

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_order_id, user_id, amount_cents, order_extras,
                       retry_count, max_retries
                  FROM failed_service_fee_jobs
                 WHERE status = 'pending' AND retry_count < max_retries
                 ORDER BY created_at
                 LIMIT 100
                """,
            )
            rows = cur.fetchall()

    for r in rows:
        d = dict(r) if isinstance(r, dict) else {
            "id": r[0], "source_order_id": r[1], "user_id": r[2],
            "amount_cents": r[3], "order_extras": r[4],
            "retry_count": r[5], "max_retries": r[6],
        }
        retried += 1
        try:
            extras = d.get("order_extras") or {}
            if isinstance(extras, str):
                extras = json.loads(extras)
            order_dict = {
                "id": d["source_order_id"],
                "user_id": d["user_id"],
                "paid_amount_yuan": d["amount_cents"] / 100,
                "paid_amount_cents": d["amount_cents"],
                "source": extras.get("source", "external_cash_payment"),
                "order_type": extras.get("order_type", "recharge"),
                "payment_method": extras.get("payment_method"),
                **{k: extras.get(k) for k in (
                    "refund_amount_yuan", "media_cost_yuan", "coupon_yuan",
                    "bonus_points_used", "granted_points_deducted_yuan",
                    "gateway_fee_yuan",
                )},
            }
            result = record_v3_3_1_rewards(order_dict)
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE failed_service_fee_jobs
                           SET status = 'succeeded', succeeded_at = NOW(),
                               retry_count = retry_count + 1,
                               last_retry_at = NOW()
                         WHERE id = %s
                        """,
                        (d["id"],),
                    )
                    conn.commit()
            succeeded += 1
            logger.info("failed_service_fee_jobs retry SUCCESS · id=%s order=%s result=%s",
                        d["id"], d["source_order_id"], result)
        except Exception as exc:
            new_retry = (d["retry_count"] or 0) + 1
            max_r = d["max_retries"] or 3
            new_status = "abandoned" if new_retry >= max_r else "pending"
            if new_status == "abandoned":
                abandoned += 1
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE failed_service_fee_jobs
                           SET retry_count = %s,
                               last_retry_at = NOW(),
                               error_message = %s,
                               error_class = %s,
                               status = %s
                         WHERE id = %s
                        """,
                        (new_retry, str(exc)[:1000], type(exc).__name__, new_status, d["id"]),
                    )
                    conn.commit()
            logger.warning("failed_service_fee_jobs retry FAIL · id=%s order=%s retry=%s/%s status=%s: %s",
                           d["id"], d["source_order_id"], new_retry, max_r, new_status, exc)

    return {
        "retried": retried,
        "succeeded": succeeded,
        "abandoned": abandoned,
        "remaining_pending": max(0, retried - succeeded - abandoned),
    }


async def service_fee_anomaly_check() -> Dict[str, Any]:
    """长期挂账债务提醒 + 老 service_fee_clawback_pending 状态推进

    注:recharge_orders 现有 schema 无 refund_reason/refunded_at 字段 · 异常追索由两个入口触发:
      1. on_recharge_refund 调用 process_recharge_refund(order_id, refund_reason='legal_dispute' 等)
      2. admin/service_fee_review_api.py:manual_clawback (财务发起)
    本 cron 只做扫尾:把 90 天未清的债务标 'written_off' + 通知 admin。
    """
    if not is_v3_3_1_enabled():
        return {"skipped": True, "reason": "v3_3_1_disabled"}

    written_off = 0
    long_pending = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            # 90 天仍 pending 转 written_off · 提醒 admin 走法务追索
            cur.execute(
                """
                UPDATE service_fee_clawback_pending
                   SET status = 'written_off'
                 WHERE status IN ('pending', 'partial_settled')
                   AND created_at < NOW() - INTERVAL '90 days'
                 RETURNING id, user_id, source_order_id, amount_due - amount_settled AS remaining
                """,
            )
            for row in cur.fetchall():
                written_off += 1
                logger.warning(
                    "anomaly_check: clawback 90d unsettled · id=%s user=%s order=%s remaining=¥%s",
                    row["id"] if isinstance(row, dict) else row[0],
                    row["user_id"] if isinstance(row, dict) else row[1],
                    row["source_order_id"] if isinstance(row, dict) else row[2],
                    row["remaining"] if isinstance(row, dict) else row[3],
                )

            # 30 天以上仍 pending 的统计 · 给 admin 告警
            cur.execute(
                """
                SELECT COUNT(*) AS c FROM service_fee_clawback_pending
                 WHERE status IN ('pending', 'partial_settled')
                   AND created_at < NOW() - INTERVAL '30 days'
                """,
            )
            r = cur.fetchone()
            long_pending = int((r["c"] if isinstance(r, dict) else r[0]) or 0)
            conn.commit()

    if long_pending > 0:
        logger.warning("anomaly_check: %d clawback records pending > 30 days · 财务请关注", long_pending)

    return {"written_off_90d": written_off, "pending_over_30d": long_pending}


async def service_fee_quota_init_for_month() -> Dict[str, Any]:
    """月初预创建当月配额行(可选 · 转换时也会自动创建)"""
    if not is_v3_3_1_enabled():
        return {"skipped": True}
    yyyymm = int(datetime.now().strftime("%Y%m"))
    created = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, agent_tier FROM user_wallets
                 WHERE COALESCE(agent_level, 0) >= 2
                """
            )
            for r in cur.fetchall():
                uid = r["user_id"] if isinstance(r, dict) else r[0]
                tier = (r["agent_tier"] if isinstance(r, dict) else r[1]) or "standard"
                from config.v3_3_1_flags import get_conversion_quota_yuan
                quota = get_conversion_quota_yuan(tier)
                cur.execute(
                    """
                    INSERT INTO service_fee_conversion_quota (user_id, yyyymm, quota_yuan, used_yuan, tier)
                    VALUES (%s, %s, %s, 0, %s)
                    ON CONFLICT (user_id, yyyymm) DO NOTHING
                    """,
                    (uid, yyyymm, float(quota), tier),
                )
                created += 1
            conn.commit()
    return {"created_or_existing": created, "yyyymm": yyyymm}


# Scheduler 注册入口(api/scheduler.py 调用)
def register_v3_3_1_jobs(scheduler) -> None:
    """把 V3.3.1 cron 注册到 APScheduler · 由 api/scheduler.py 引用

    Codex 反馈:显式传 timezone(虽然 scheduler default 有 BEIJING_TZ · 显式更可读)

    Codex 四审 P1-3:加 ROLE gate 防蓝绿双跑
    - 读 env ROLE(蓝绿部署应设 primary/backup)
    - 读 system_settings.V3_3_1_CRON_ROLE_GATE(any / primary / active · default any)
    - gate=any → 所有容器都注册(老行为)
    - gate=primary → 只 ROLE=primary 容器注册(蓝绿规范)
    - 任何 mismatch → 拒绝注册 + warning(防 T+3/T+7/failed_jobs_retry 双跑)
    """
    import os
    try:
        from config.v3_3_1_flags import get_flag
        allowed_role = (get_flag("V3_3_1_CRON_ROLE_GATE") or "any").strip().lower()
    except Exception:
        allowed_role = "any"
    current_role = os.environ.get("ROLE", "").strip().lower()

    if allowed_role not in ("any", ""):
        if current_role != allowed_role:
            logger.warning(
                "[V3.3.1 Scheduler] ROLE gate mismatch · allowed=%s current=%s · "
                "拒绝注册 V3.3.1 cron(防蓝绿双跑)",
                allowed_role, current_role or "(未设)",
            )
            return
        logger.info(
            "[V3.3.1 Scheduler] ROLE gate PASS · allowed=%s current=%s",
            allowed_role, current_role,
        )

    try:
        from apscheduler.triggers.cron import CronTrigger
        import pytz
        tz = pytz.timezone("Asia/Shanghai")
        scheduler.add_job(scheduler_sync_callable(service_fee_t3_settle),
                          trigger=CronTrigger(hour=4, minute=0, timezone=tz),
                          id="v3_3_1_service_fee_t3_settle", replace_existing=True)
        scheduler.add_job(scheduler_sync_callable(service_fee_t7_bonus_settle),
                          trigger=CronTrigger(hour=4, minute=15, timezone=tz),
                          id="v3_3_1_bonus_t7_settle", replace_existing=True)
        scheduler.add_job(scheduler_sync_callable(service_fee_anomaly_check),
                          trigger=CronTrigger(hour=5, minute=0, timezone=tz),
                          id="v3_3_1_anomaly_check", replace_existing=True)
        scheduler.add_job(scheduler_sync_callable(service_fee_quota_init_for_month),
                          trigger=CronTrigger(day=1, hour=6, minute=0, timezone=tz),
                          id="v3_3_1_quota_init_monthly", replace_existing=True)
        # Codex 三审 P1-4:每 15 分钟补扫一次 failed_service_fee_jobs
        # [FF5] async job 套 tick 级 claim(sched_claim 现支持 async · audit NEEDS_CLAIM:SELECT-then-retry
        # 非原子,两 leader 同 tick 可重复重试;claim 去重 + 各 job 内 record_v3_3_1_rewards 幂等叠加)
        from services.sched_claim import sched_claim as _sched_claim
        scheduler.add_job(scheduler_sync_callable(
                              _sched_claim("v3_3_1_failed_jobs_retry", 900)(service_fee_failed_jobs_retry)),
                          trigger=CronTrigger(minute="*/15", timezone=tz),
                          id="v3_3_1_failed_jobs_retry", replace_existing=True)
        logger.info("V3.3.1 5 cron jobs registered (Asia/Shanghai)")
    except Exception as exc:
        logger.exception("register_v3_3_1_jobs failed: %s", exc)
