"""
services/marketing/reconcile.py — 每小时发放对账补发 + 营销异常告警(§C.2 / §C.4)

reconcile_grants(force):
  · 首充双倍补发:首笔付费充值的用户若 hook 漏发(无 mktg_firstcharge grant)且活动 active → 补发。
    (幂等靠 grant_key;dry_run 时补的也是 dry_run 台账,彩排账自洽。)
  · 营销异常告警(§C.4 "AI Ops 巡逻加一条营销规则,复用现有告警管道"):
    日发放量异常(> per_day 的 80%)/ 活动超期(active 但过 ends_at)→ 写 marketing_events
    并**复用 AI Ops 告警管道**(ai_ops_db.upsert_alert,规则键 marketing_anomaly),
    在 AI Ops 控制塔告警面板可见(去重靠 (rule_key,fingerprint) 部分唯一索引)。

reconcile_grants_job():scheduler 入口,吞异常永不抛。
"""
import logging
from typing import Optional

from db import marketing_db
from services.marketing.executors import grants

logger = logging.getLogger("GEO-Marketing-Reconcile")


def _emit_alert(fingerprint: str, severity: str, title: str, detail: str, payload: dict) -> None:
    """复用 AI Ops 告警管道(不改 ai_ops patrol · 只调 upsert_alert)。fail-soft。"""
    try:
        from db import ai_ops_db
        ai_ops_db.upsert_alert("marketing_anomaly", severity=severity, title=title,
                               detail=detail, fingerprint=fingerprint, payload=payload)
    except Exception as e:  # noqa: BLE001
        logger.warning("[reconcile] 写 AI Ops 告警失败(降级): %s", e)


def _backfill_first_charge(dry_run: bool) -> int:
    """首充双倍漏发补发。返回补发条数。"""
    camp = marketing_db.get_campaign_by_code("firstcharge_double")
    if not camp or camp.get("status") != "active":
        return 0
    # [返工 R6-1] 补发时间窗:只补活动上线(starts_at)之后的漏发,历史存量单充用户不追溯
    #(activate 端点已保证 status→active 时 starts_at 必有值;无值 = 异常配置,保守不补)
    starts_at = camp.get("starts_at")
    if not starts_at:
        logger.warning("[reconcile] firstcharge_double 无 starts_at,补发跳过(保守)")
        return 0
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 恰好 1 笔付费充值(且付于活动上线后)、无真实 mktg_firstcharge grant 的用户
        # [返工 R6-1] NOT EXISTS 只查真实键:dryrun: 彩排行不再永久挡住真实补发
        #(彩排期重复轮询由 dryrun: 键自身 UNIQUE + DO NOTHING 保证不膨胀)
        cur.execute(
            """
            SELECT ro.user_id, MIN(ro.id) AS order_id, MAX(ro.base_points) AS base_points,
                   COUNT(*) AS paid_cnt
            FROM recharge_orders ro
            WHERE ro.payment_status = 'paid'
            GROUP BY ro.user_id
            HAVING COUNT(*) = 1
               AND MIN(ro.paid_at) >= %s
               AND NOT EXISTS (SELECT 1 FROM marketing_grants g
                               WHERE g.grant_key = 'mktg_firstcharge:' || ro.user_id)
            LIMIT 200
            """,
            (starts_at,)
        )
        rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("[reconcile] 首充补发查询失败: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()

    params = camp.get("params_jsonb") or {}
    rate = float(params.get("bonus_rate", 1.0) or 1.0)
    max_grant = int(params.get("max_grant_points", 65000) or 65000)  # [返工 R1] 与钩子同口径封顶
    n = 0
    for r in rows:
        pts = min(int(round(int(r["base_points"] or 0) * rate)), max_grant)
        if pts <= 0:
            continue
        res = grants.apply_grant(user_id=r["user_id"], points=pts,
                                 grant_key=f"mktg_firstcharge:{r['user_id']}", campaign_id=camp["id"],
                                 related_order_id=str(r["order_id"]), grant_type="first_charge_double",
                                 reason="首充双倍补发(对账)", force_dry_run=dry_run)
        if res["status"] in ("granted", "dry_run"):
            n += 1
    return n


def _anomaly_scan() -> None:
    # 日发放量异常
    per_day = marketing_db.get_config_int("marketing.grant.cap_per_day")
    today = marketing_db.sum_grants_today()
    if per_day and today > per_day * 0.8:
        _emit_alert("daily_grant_volume", "warn", "营销日发放量接近上限",
                    f"今日已发 {today} / 上限 {per_day}(>80%)", {"today": today, "cap": per_day})
    # 活动超期(active 但过 ends_at)
    for c in marketing_db.list_campaigns(status="active"):
        ends_at = c.get("ends_at")
        if ends_at:
            from datetime import datetime
            try:
                if isinstance(ends_at, str):
                    ends_at = datetime.fromisoformat(ends_at)
                if ends_at < datetime.now(ends_at.tzinfo):
                    _emit_alert(f"campaign_overdue:{c['id']}", "warn", "营销活动已超期未下线",
                                f"活动 {c.get('name')} ends_at={c.get('ends_at')} 仍 active",
                                {"campaign_id": c["id"]})
            except Exception:  # noqa: BLE001
                pass


def reconcile_grants(force: bool = False) -> Optional[dict]:
    """跑一轮对账。总闸关且非 force → 跳过(但异常扫描照跑,保安不下岗)。"""
    # 异常扫描不依赖发放闸(观测优先)
    _anomaly_scan()

    if not force and not marketing_db.is_flag_enabled("marketing_agent.enabled", default=False):
        return {"backfilled": 0, "note": "master_off_scan_only"}
    if marketing_db.is_kill_switch_enabled():
        return {"backfilled": 0, "note": "kill_switch"}

    dry_run = not (marketing_db.is_flag_enabled("marketing_agent.execute.enabled", default=False)
                   and marketing_db.is_flag_enabled("marketing_agent.grant.enabled", default=False))
    backfilled = _backfill_first_charge(dry_run=dry_run)
    if backfilled:
        marketing_db.add_event(event_type="grant_reconciled",
                               message=f"对账补发首充双倍 {backfilled} 笔(dry_run={dry_run})",
                               payload={"backfilled": backfilled, "dry_run": dry_run})
    return {"backfilled": backfilled, "dry_run": dry_run}


def reconcile_grants_job() -> None:
    """Scheduler 入口(每小时)。永不抛异常。"""
    try:
        result = reconcile_grants(force=False)
        if result and result.get("backfilled"):
            logger.info("[reconcile] %s", result)
    except Exception as e:  # noqa: BLE001
        logger.warning("[reconcile] 对账轮异常: %s", e)
