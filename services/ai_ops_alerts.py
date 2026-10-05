"""ai_ops 告警产线的**共享入口**。

## 为什么这个文件存在(2026-08-20 · WO-A ②)

``raise_wiring_alert`` 原来长在 ``services/geo_douyin/contract_worker.py`` 里 ——
那是图文合同链的 worker,导入它会连带拉起 ``contract_seams`` / ``contract_states`` /
``durable_worker`` 一整串图文模块。小榜的索引重建要用同一条告警产线,
不该为了拉一条告警把图文链整个 import 进来。

所以把函数**搬**到这里,原处 ``from services.ai_ops_alerts import raise_wiring_alert``
再导出 —— 既有调用方(``api/geo_douyin_api.py`` 的函数内 import)逐字不变,
而**实现只有一份**:抄第二份的话,两份必然各自漂移。

## 它做什么

把"我们自己的链断了/崩过"拉成一条**可查询的告警**(``ai_ops_alerts``),
而不是只留一条没人查的日志。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("GEO-AiOpsAlerts")


def raise_wiring_alert(*, rule_key: str, title: str, detail: str,
                       fingerprint: str = "", payload: Optional[dict] = None,
                       severity: str = "critical") -> bool:
    """把"我们自己的链断了/崩过"拉成一条**可查询的告警**(`ai_ops_alerts`)。

    🔴 [第 4 棒 · P1-C] 这个函数存在的理由,是把**故障可见性**从**用户的钱**
       上拿下来。上一版靠"让算力继续冻着"来提醒我们自己 ——
       那等于**拿用户的算力当报警器**:用户看到钱没退、我们看到的也只是
       一条日志,而日志没有人查。告警面才是我们自己该承担的那一侧。

    🔴 告警失败**绝不**打断主链:拉不响是监控的问题,不该让一笔本该退的钱
       退不成。所以整段吞异常,只留一条 error 日志。
    """
    try:
        from db.ai_ops_db import upsert_alert

        upsert_alert(rule_key, severity=severity, title=title, detail=detail[:2000],
                     fingerprint=fingerprint, payload=payload or {})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[ai-ops-alert] 告警写入失败(不影响主链)%s: %s", rule_key, exc)
        return False


__all__ = ["raise_wiring_alert"]
