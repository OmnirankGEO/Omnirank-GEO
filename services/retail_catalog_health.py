"""已发布零售目录的"零条目"体检 —— 断供是要被人看见的,不是等客户来报错。

背景(工单 2026-07-29 §2 · 生产实证):
    2026-07-27 14:39:48.346016  agent_sku_overrides id=228(平台直营「新客启动包」)软删
    2026-07-27 14:39:48.346016  pricing_catalog_versions id=124 published · items = 0
    → retail_catalog / retail_quote 全部 fail-closed
    → 47 个无归属(平台直营)客户约 2 天买不了额度包,**零告警**
    2026-07-29 14:19  Owner 手工重新上架发布(版本 131 · items=1)才解除

发布期守卫(`pricing_publication.EmptyRetailCatalogRejected`)拦的是"新产生"这一态;
本模块负责"已经处在这一态"的持续可见性 —— 两者缺一不可:守卫上线前就已经空掉的
scope,不会再经过守卫。

只读,永不写库,永不抛异常到调用方(体检本身不该成为新的故障源)。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-RetailCatalogHealth")


def empty_published_retail_scopes(cur) -> List[Dict[str, Any]]:
    """当前处于 published 且开放(effective_to IS NULL)但**零条目**的零售目录。

    返回按 scope_key 排序的列表;查不动就返回空列表 —— 体检失败不能把调用页面打死。
    """
    try:
        cur.execute(
            """
            SELECT v.id            AS version_id,
                   v.scope_key     AS scope_key,
                   v.version_code  AS version_code,
                   v.effective_from,
                   v.reason,
                   v.created_by
              FROM pricing_catalog_versions v
             WHERE v.catalog_type = 'retail'
               AND v.status = 'published'
               AND v.effective_to IS NULL
               AND NOT EXISTS (
                     SELECT 1 FROM pricing_catalog_entries e WHERE e.version_id = v.id
                   )
             ORDER BY v.scope_key
            """
        )
        rows = cur.fetchall() or []
    except Exception as exc:  # noqa: BLE001 — 体检不得把管理页打成 500
        logger.warning("[retail-catalog-health] 零条目体检查询失败: %s", type(exc).__name__)
        return []

    result: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        effective_from = item.get("effective_from")
        result.append({
            "version_id": int(item["version_id"]),
            "scope_key": str(item["scope_key"]),
            "version_code": str(item.get("version_code") or ""),
            "effective_from": effective_from.isoformat() if effective_from else None,
            "reason": str(item.get("reason") or ""),
        })
    return result


def empty_retail_catalog_alert(scopes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把零条目清单翻成管理端能直接看懂、且带下一步的 §13 告警合同。

    没有零条目就返回 None(不制造无意义的红点 —— `feedback_alert_minimalism_business_first`)。
    """
    if not scopes:
        return None

    from services.governance_contract import build_alert_safe

    names = "、".join(item["scope_key"] for item in scopes[:5])
    more = f" 等 {len(scopes)} 个" if len(scopes) > 5 else ""
    return build_alert_safe(
        "EMPTY_PUBLISHED_RETAIL_CATALOG",
        f"有 {len(scopes)} 个服务方的在售目录当前是空的，这些客户买不了任何东西。",
        reason=f"服务编号 {names}{more} 的已发布零售目录没有任何在售商品。",
        impact="这些服务方名下的客户在购买页会直接失败；平台直营目录为空时，所有无归属客户都无法购买。",
        repair_hint="到定价中心为这些服务方重新上架至少一个商品并重新发布；发布后本提示会自动消失。",
        actions=[
            {"id": "open_pricing_center", "label": "去定价中心处理", "type": "nav"},
        ],
        rule_version="empty-retail-catalog-v1",
    )
