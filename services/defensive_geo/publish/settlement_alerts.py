"""Z-1 · pending 超 7 天自动告警(规格 §0.5.6 Z-1 第 5 条)。

复用现役 AIOps 告警通道(``db/ai_ops_db.upsert_alert`` → ``ai_ops_alerts`` →
``/admin/ai-ops`` 面板),**不另造第三条告警链**。

🔴 为什么阈值是常量而不是 flag:Z-1 逐字给了「7 天」。写成 flag 意味着
   有人可以在不改判据的前提下把它调成 90 天,那样告警就永远不响 ——
   而判据仍然全绿。要改就改这个常量,判据会跟着看见。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("GEO-DefGeoSettlementAlerts")

ALERT_RULE_KEY = "defgeo_publish_settlement_pending"
#: 与 ``reconciler.PENDING_ALERT_SECONDS`` **同源**(那里是 SSOT),这里只引用。
from services.defensive_geo.publish.reconciler import (  # noqa: E402
    PENDING_ALERT_SECONDS,
    stale_pending,
)


def scan_and_alert() -> dict[str, Any]:
    """扫一遍队列,超 7 天的拉 critical 告警。scheduler 每小时调一次。

    🔴 全异常兜底:调度器忽略返回值,但**不能**让一个告警任务把调度线程带走。
       兜底的代价是「告警本身失败会静默」—— 所以失败也写一条 warn 级告警。
    """
    from db.connection import get_connection

    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        rows = stale_pending(cur, older_than_seconds=PENDING_ALERT_SECONDS)
        conn.rollback()
    except Exception as exc:                              # noqa: BLE001
        logger.error("[defgeo-settlement-alert] 扫描失败:%s", exc)
        _safe_alert(
            severity="warn", title="防御型 GEO 发布结算队列扫描失败",
            detail=f"{type(exc).__name__}", fingerprint="scan_failed",
        )
        return {"ok": False, "reason": type(exc).__name__}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                             # noqa: BLE001
                pass

    if not rows:
        return {"ok": True, "stale": 0}

    total_points = sum(int(r.get("exact_settlement_points") or 0) for r in rows)
    oldest_days = max(int(r.get("pending_seconds") or 0) for r in rows) // 86400
    _safe_alert(
        severity="critical",
        title=f"发布结算待核验超 {PENDING_ALERT_SECONDS // 86400} 天：{len(rows)} 笔",
        detail=(
            f"共 {len(rows)} 笔发布命令的费用停在冻结/隔离态超过 "
            f"{PENDING_ALERT_SECONDS // 86400} 天，合计 {total_points} 算力，"
            f"最久的一笔已 {oldest_days} 天。请到「资金核验队列」逐条处置。"
        ),
        fingerprint="stale_pending",
        payload={
            "count": len(rows), "totalPoints": total_points, "oldestDays": oldest_days,
            "commandIds": [str(r["publish_command_id"]) for r in rows[:20]],
        },
    )
    return {"ok": True, "stale": len(rows), "totalPoints": total_points}


def _safe_alert(*, severity: str, title: str, detail: str,
                fingerprint: str, payload: dict[str, Any] | None = None) -> None:
    try:
        from db.ai_ops_db import upsert_alert

        upsert_alert(
            ALERT_RULE_KEY, severity=severity, title=title, detail=detail,
            fingerprint=fingerprint, payload=payload or {},
        )
    except Exception as exc:                              # noqa: BLE001
        logger.error("[defgeo-settlement-alert] 告警写入失败:%s", exc)
