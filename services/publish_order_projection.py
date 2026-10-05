# -*- coding: utf-8 -*-
"""发布订单对外投影 —— 订单详情/批次详情响应体的**正列**出口。

[WO_255 2026-09-22] 病灶:`GET /api/publish/orders/{order_id}` 与
`GET /api/publish/batches/{batch_id}` 把 `publish_order_items` 的**整行**
(`db/publish_db.py:891 get_order_items` 是 `SELECT *`)原样放进响应,
其中 `mhz_cost_yuan` 是**供应商侧进货价**(写入点 `api/publish_api.py:1328`
把 `media["price_vip"]` 存了进去)⇒ 任何服务商拿自己的 token 打这两个端点
就能读到我方进货价。

🔴 **为什么是正列不是减法**:
   仓里已有两套减法名单(`services/media_price_projection.COST_SIDE_FIELDS`、
   `services/defensive_geo/publish/media_identity.PRIVATE_COLUMNS` + 前缀),
   而 `mhz_cost_yuan` **两套都漏**:它不在名单里,前缀是 `mhz_` 不是 `cost_`。
   减法名单永远在追赶列名;正列反过来 —— **没列出来的一律不出**,
   下一列叫什么都挡得住。

🔴 **一个命名与语义背离,必须写在这里**(不写清楚下一个人会据名字判断):
   · `cost_yuan`     = `our_price_yuan × discount` ⇒ **我方售价(元)**,名字叫 cost 但它是卖价。
   · `mhz_cost_yuan` = 供应商 VIP 价 ⇒ **真正的进货价**。
   本模块两个都不放出去(售价那个用 `cost_points` 表达已经够,多一份元值只是冗余),
   但**理由不同**:前者是冗余,后者是泄漏。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

#: `publish_order_items` 对外可见的列(正列)。
#: 口径:客户/服务商查自己的投放明细需要什么,就给什么。
ITEM_PUBLIC_FIELDS: tuple[str, ...] = (
    "id", "order_id", "media_id", "media_name",
    "cost_points",          # 用户自己被扣了多少算力 —— 他有权知道
    "status", "publish_url", "reject_reason",
    "submitted_at", "published_at", "refunded_at",
)

#: 明确**不给**的列,连同理由。放在这里是为了让下面那把枚举锁能说出
#: 「这一列是想清楚了不给」还是「这一列是新加的还没人看过」。
ITEM_WITHHELD_FIELDS: dict[str, str] = {
    "mhz_cost_yuan": "供应商侧进货价($ 类)—— 本单要堵的就是它",
    "cost_yuan": "我方售价的元值;`cost_points` 已表达同一事实,多一份只是冗余",
    "mhz_order_id": "供应商侧订单号(V 类)—— 能反查到我们从谁那里进的货",
}

#: `publish_orders` 对外可见的列。该表**没有**供应商侧列(2026-09-22 按
#: `information_schema` 现取 18 列核过),所以这里是"减噪"不是"堵漏":
#: 只去掉正文与内部归属字段,留下用户看得懂的那些。
ORDER_PUBLIC_FIELDS: tuple[str, ...] = (
    "id", "batch_id", "user_id", "article_id", "article_title",
    "status", "total_cost_points", "media_count", "created_at", "updated_at",
    "brand_id",
)

#: `publish_batches` 对外可见的列。
BATCH_PUBLIC_FIELDS: tuple[str, ...] = (
    "id", "user_id", "article_count", "total_publish_count",
    "total_cost_points", "status", "created_at",
)


def _project(row: Mapping[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    """只取白名单里**存在于该行**的键。

    不给缺失的键补 `None`:补了会让"这次没查这列"和"这列的值就是空"同形,
    而调用方分不出来(本仓 WO_251 §4 栽过同一个形状)。
    """
    return {k: row[k] for k in allowed if k in row}


def project_order_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return _project(row, ITEM_PUBLIC_FIELDS)


def project_order_items(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [project_order_item(r) for r in (rows or ())]


def project_order(row: Mapping[str, Any]) -> dict[str, Any]:
    return _project(row, ORDER_PUBLIC_FIELDS)


def project_orders(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [project_order(r) for r in (rows or ())]


def project_batch(row: Mapping[str, Any]) -> dict[str, Any]:
    return _project(row, BATCH_PUBLIC_FIELDS)


def project_batches(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [project_batch(r) for r in (rows or ())]
