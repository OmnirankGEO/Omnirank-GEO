"""监测计费通知链 —— N1 开启告知 / N3 余额不足暂停告知。

[WO_MONITORING_PLATFORM_COVERED_AND_NOTIFY 2026-08-16 · §4]

病史(生产实证 2026-08-16):
  岱林(brand 592)的两条监测订阅在 2026-08-11 因余额不足自动暂停,**静默至今 5 天**。
  暂停点(`scheduler.py`)当时只写了一行 `logger.warning` —— 服务商不知道、客户更不知道。
  这不是岱林一家,是**所有服务商都会踩**。

🔴 不新造通知体系:复用既有 `notification_outbox` + 既有事件类型
  `MONITORING_PAUSED`(它连模板都已经有了,只是**从来没有订阅级调用方** ——
  典型的"设施在、没人接线"死功能)。推送粒度沿用 `user_wallets.charge_notify_level`,
  不新造偏好字段。

🔴 平台承担的订阅通知**谁**:
  通知操作的管理员 + 落平台侧告警,**不要**通知那个服务商 ——
  他没付钱,收到"你的余额不足"是错的,而且会让他去充一笔本不该他出的钱。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger("GEO-MonitorBillingNotify")

# 与 §2 同口径:付款方只有这两种
BILLING_MODE_PLATFORM = "platform"
BILLING_MODE_BRAND_OWNER = "brand_owner"


def _charge_notify_level(user_id: int) -> str:
    """读该钱包主人的推送粒度偏好(既有字段,不新造)。

    取不到时返回 'all' —— **fail-open 到"多通知一次"**:
    这条链上"漏掉一次余额不足通知"的代价(客户监测静默停 5 天)
    远大于"多发一条通知"的代价。
    """
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT charge_notify_level FROM user_wallets WHERE user_id = %s", (int(user_id),))
            row = cur.fetchone()
            if row and row.get("charge_notify_level"):
                return str(row["charge_notify_level"])
        finally:
            try:
                conn.close()
            except Exception:      # noqa: BLE001
                pass
    except Exception as exc:       # noqa: BLE001
        logger.warning(f"[notify] 读 charge_notify_level 失败,按 all 处理: {exc}")
    return "all"


def notify_monitor_paused_low_balance(
    *,
    subscription_id: int,
    billing_user_id: int,
    billing_mode: str,
    brand_id: Optional[int],
    brand_name: str = "",
    keyword: str = "",
    daily_points: int = 130,
) -> Dict[str, Any]:
    """N3 · 余额不足暂停 → 通知**计费主体**(平台承担时通知管理员)。

    返回落库结果摘要,供判据断言"通知**真的发出**"而不是"函数被调用过"。

    🔴 整段 fail-open:通知发不出去**不许**影响暂停本身 ——
       暂停是保护性动作(余额不够就别再跑),它不能被一个通知故障拖住。
    """
    result: Dict[str, Any] = {"sent": 0, "recipient_kind": None, "recipient_user_id": None,
                              "service_provider_notified": False}
    try:
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import (
            enqueue_admin_notification_events_durable,
            enqueue_user_notification_event_durable,
        )

        is_platform = (billing_mode or BILLING_MODE_BRAND_OWNER).strip() == BILLING_MODE_PLATFORM
        facts = {
            "business_no": f"KMS-{int(subscription_id)}",
            "status": "效果监测已暂停 · 算力余额不足",
            "occurred_at": datetime.now().isoformat(timespec="seconds"),
            "summary": (
                f"品牌「{brand_name or brand_id}」的关键词「{keyword}」已于今日暂停监测。"
                f"每天需要 {int(daily_points)} 算力;充值后次日 09:00 CST 自动恢复,"
                f"词条与历史数据都不会丢。"
            ),
        }
        common = dict(
            event_type=NotificationEventType.MONITORING_PAUSED,
            # 🔴 [R6 · Codex 2026-08-17] outbox 的 dedup 键是
            #   (event_type, business_id, terminal_state, user_id) 且 ON CONFLICT DO NOTHING。
            #   固定成 `kms:<id>` ⇒ **同一条订阅第二次余额不足永远不会再通知** ——
            #   而"这个月又停了"恰恰是最需要告诉他的事(岱林那次静默 5 天就是这个形态的极端版)。
            #   ⇒ 键里加**当天日期**:同一天重复触发仍去重(不刷屏),换一天能重发。
            business_id=f"kms:{int(subscription_id)}:{datetime.now().strftime('%Y%m%d')}",
            terminal_state="paused",
            facts=facts,
        )

        if is_platform:
            # 平台承担:通知管理员,**不通知服务商**
            n = enqueue_admin_notification_events_durable(**common)
            result.update({"sent": int(n or 0), "recipient_kind": "admin",
                           "recipient_user_id": None, "service_provider_notified": False})
            logger.error(
                f"[notify] 平台承担订阅 sub#{subscription_id} 因平台账户余额不足暂停 · "
                f"已通知 {n} 位管理员(brand={brand_id})"
            )
        else:
            level = _charge_notify_level(billing_user_id)
            # 🔴 余额不足是**阻断性**事件,不是流水回执 —— 任何粒度下都要发。
            #    charge_notify_level 管的是 N2 的每日扣费回执,不是这条。
            nid = enqueue_user_notification_event_durable(
                recipient_user_id=int(billing_user_id),
                recipient_kind=RecipientKind.AGENT,
                **common,
            )
            result.update({"sent": 1 if nid else 0, "recipient_kind": "agent",
                           "recipient_user_id": int(billing_user_id),
                           "service_provider_notified": bool(nid), "notify_level": level})
            logger.warning(
                f"[notify] sub#{subscription_id} 余额不足暂停 · 已通知计费主体 user#{billing_user_id}"
            )
    except Exception as exc:       # noqa: BLE001
        # 🔴 fail-open:通知失败不许拖住暂停动作本身
        logger.error(f"[notify] 暂停通知发送失败 sub#{subscription_id}: {exc}")
        result["error"] = str(exc)
    return result


def notify_monitor_enabled_by_other(
    *,
    subscription_id: int,
    billing_user_id: int,
    operator_user_id: int,
    billing_mode: str,
    brand_id: Optional[int],
    brand_name: str = "",
    keyword_count: int = 1,
    daily_points: int = 130,
) -> Dict[str, Any]:
    """N1 · 计费主体 ≠ 操作者时,告知钱包主人:谁、给哪个品牌、开了几个词、每天多少。

    🔴 只在**主体 ≠ 操作者**时发:自己给自己开不需要通知自己(那是噪音,
       而 `alert_minimalism_business_first` 说过噪音会让真信号被忽略)。
    """
    result: Dict[str, Any] = {"sent": 0, "reason": None}
    if int(billing_user_id) == int(operator_user_id):
        result["reason"] = "same_subject"          # 主体=操作者 ⇒ 刻意不发
        return result
    try:
        from services.notification_events import NotificationEventType, RecipientKind
        from services.notification_outbox import (
            enqueue_admin_notification_events_durable,
            enqueue_user_notification_event_durable,
        )
        facts = {
            "business_no": f"KMS-{int(subscription_id)}",
            "status": "有人为你的账户开通了效果监测",
            "occurred_at": datetime.now().isoformat(timespec="seconds"),
            "summary": (
                f"用户 #{int(operator_user_id)} 为品牌「{brand_name or brand_id}」开通了 "
                f"{int(keyword_count)} 个关键词的自动监测,每个词每天 {int(daily_points)} 算力。"
            ),
        }
        common = dict(
            # 🔴 [R6-a · Codex 2026-08-17] 原来复用 MONITORING_COMPLETED,标题渲染成
            #   「效果监测已完成」—— 给钱包主人推一条"已完成",而实际发生的是
            #   "有人替你开通了、要开始扣钱了",**标题语义完全相反**。
            #   通知的价值几乎全在标题(用户多半只看那一行)⇒ 换独立事件类型。
            event_type=NotificationEventType.MONITORING_ENABLED_BY_OTHER,
            business_id=f"kms-enable:{int(subscription_id)}",
            terminal_state="enabled",
            facts=facts,
        )
        if (billing_mode or BILLING_MODE_BRAND_OWNER).strip() == BILLING_MODE_PLATFORM:
            n = enqueue_admin_notification_events_durable(**common)
            result.update({"sent": int(n or 0), "recipient_kind": "admin"})
        else:
            nid = enqueue_user_notification_event_durable(
                recipient_user_id=int(billing_user_id),
                recipient_kind=RecipientKind.AGENT,
                **common,
            )
            result.update({"sent": 1 if nid else 0, "recipient_kind": "agent",
                           "recipient_user_id": int(billing_user_id)})
    except Exception as exc:       # noqa: BLE001
        logger.error(f"[notify] 开通通知发送失败 sub#{subscription_id}: {exc}")
        result["error"] = str(exc)
    return result
