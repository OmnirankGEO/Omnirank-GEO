"""发布渠道状态回流(开源版)。

把本地在途的条目(已受理、还没终态)按渠道查一遍,写成应用状态同步一直认得的那种订单行,
交给现有的 ``db.meijiehezi_db.sync_mhz_orders`` —— 已发布 / 拒稿 / 撤回的落库、退款、通知都走它原来那条链,
本模块不碰计费。订单行里的状态码:1 在途、2 已发布、-1 拒稿(原额退回)、-2 撤回(原额退回)。
回流写给用户看的原因文字只用本模块自己的话,不转述渠道的原文。
时间先后一律在 SQL 里比(库里的提交时间是不带时区的本地时间,Python 不猜它的时区);
唯一交给渠道的时间是下单记录表里带时区的 created_at,原样带着偏移发出去。

发布渠道 API 模式下还处理「待同步」的条目(下单结果不明时应用放进去的,见 api_client.publish):
按下单前记下的 client_reference 认单 —— 认到了接上单号回到正常回流;渠道明确没受理的立即判失败、原额退回;
进待同步满 24 小时仍认不到的,判失败、原额退回(退款键与应用其他退款同为 ``item:<条目号>``,不会重复退)。
没接入渠道时什么都不做。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from services.publish_channels import API, DRY_RUN, configured_mode

logger = logging.getLogger("GEO-PublishChannel")

DELAY_ENV = "PUBLISH_CHANNEL_DRY_RUN_DELAY_MINUTES"
IN_FLIGHT = ("submitted",)
AWAITING_GIVE_UP = timedelta(hours=24)
LOOKBACK = timedelta(minutes=10)          # 按 client_reference 认单时,从记录时间往前多看这么久(两边时钟差)

REASON_REJECTED = "发布渠道没有发出(原因码 {code}),算力已原额退回"
REASON_CANCELLED = "已撤单,算力已原额退回"
REASON_NOT_ACCEPTED = "发布渠道没有受理这次下单,算力已原额退回"
REASON_NOT_FOUND = "发布渠道一直没有这次下单的记录,算力已原额退回"


def _reason(code: int, item: dict) -> str:
    if code == -2:
        return REASON_CANCELLED
    raw = str((item.get("failure") or {}).get("code") or item.get("status") or "").strip()
    safe = "".join(ch for ch in raw if ch.isascii() and (ch.isalnum() or ch == "_"))[:40] or "未知"
    return REASON_REJECTED.format(code=safe)

_API_ITEM_STATUS = {          # 契约条目状态 → 订单行状态码
    "pending": 1, "submitted": 1,
    "receipted": 2, "receipted_unverified": 2,
    "rejected": -1, "failed": -1,
    "cancelled": -2,
}


def _dry_run_delay() -> timedelta:
    raw = (os.environ.get("PUBLISH_CHANNEL_DRY_RUN_DELAY_MINUTES") or "").strip()
    try:
        return timedelta(minutes=max(0.0, float(raw))) if raw else timedelta(minutes=2)
    except ValueError:
        return timedelta(minutes=2)


def _in_flight(like: str, delay: timedelta | None = None) -> list[dict]:
    """在途条目。``due`` = 受理满 ``delay``(在库里用 NOW() 比,和写入提交时间的是同一个时钟)。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT i.id, i.mhz_order_id, i.media_id, i.media_name, i.media_type, i.cost_yuan,
                      i.submitted_at, o.article_title, o.created_at,
                      (%s::interval IS NOT NULL
                       AND NOW() - COALESCE(i.submitted_at, o.created_at) >= %s::interval) AS due
                 FROM mhz_publish_order_items i JOIN mhz_publish_orders o ON o.id = i.order_id
                WHERE i.status = ANY(%s) AND i.mhz_order_id LIKE %s""", (delay, delay, list(IN_FLIGHT), like))
        rows = cur.fetchall() or []
        cols = [d[0] for d in cur.description]
    finally:
        conn.close()
    return [r if isinstance(r, dict) else dict(zip(cols, r)) for r in rows]


def _row(item: dict, status: int, *, url: str = "", reason: str = "", remark: str = "") -> dict:
    now = datetime.now(timezone.utc)
    row = {"id": item["mhz_order_id"], "ordernum": item["mhz_order_id"], "status": status,
           "title": item.get("article_title") or "", "price": float(item.get("cost_yuan") or 0),
           "url": url, "reason": reason, "created_at": item.get("submitted_at") or item.get("created_at"),
           "updated_at": now, "published_at": now if status == 2 else None,
           "order_remark": remark, "customer_name": ""}
    if (item.get("media_type") or "") == "wemedia":
        row.update({"toutiao_name": item.get("media_name") or "", "toutiao_id": item.get("media_id") or 0})
    else:
        row.update({"media_name": item.get("media_name") or "", "resource_id": item.get("media_id") or 0})
    return row


def dry_run_rows(items: list[dict]) -> list[dict]:
    """items 来自 ``_in_flight(..., delay)``:满没满延时已经由库判好(``due``)。"""
    from services.publish_channels.dry_run import result_url

    out = []
    for it in items:
        done = bool(it.get("due"))
        out.append(_row(it, 2 if done else 1, url=result_url(it["mhz_order_id"]) if done else "", remark="模拟发布"))
    return out


async def api_rows(items: list[dict], client) -> list[dict]:
    by_order: dict[str, list[dict]] = {}
    for it in items:
        order_id = str(it["mhz_order_id"]).partition(":")[0]
        by_order.setdefault(order_id, []).append(it)
    out = []
    for order_id, its in by_order.items():
        try:
            order = await client.get_order(order_id)
        except Exception as exc:                          # 查不到这一单:这一轮跳过,下一轮再查
            logger.warning("[publish-channel] 查单 %s 失败:%s", order_id, type(exc).__name__)
            continue
        remote = {str(x.get("item_id")): x for x in order.get("items") or []}
        for it in its:
            item = remote.get(str(it["mhz_order_id"]).partition(":")[2])
            if item is None:
                continue
            code = _API_ITEM_STATUS.get(str(item.get("status")), 1)      # 未知新值按在途处理(契约要求)
            url = str((item.get("receipt") or {}).get("url") or "") if code == 2 else ""
            reason = _reason(code, item) if code in (-1, -2) else ""
            out.append(_row(it, code, url=url, reason=reason))
    return out


# ---------------------------------------------------------------- 待同步条目(只在 api 模式)

def _awaiting_items() -> list[dict]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT i.id, i.user_id, i.media_id, i.cost_points, i.last_submit_at,
                      (COALESCE(i.awaiting_sync_since, i.created_at) < NOW() - %s) AS overdue,
                      o.article_title
                 FROM mhz_publish_order_items i JOIN mhz_publish_orders o ON o.id = i.order_id
                WHERE i.status = 'awaiting_sync'
                ORDER BY i.id LIMIT 200""", (AWAITING_GIVE_UP,))
        rows = cur.fetchall() or []
        cols = [d[0] for d in cur.description]
    finally:
        conn.close()
    return [r if isinstance(r, dict) else dict(zip(cols, r)) for r in rows]


def _claim_submission(item: dict) -> dict | None:
    """给这个条目认领它那一次下单记录(同媒体、同标题、在这次提交之后记的;已被它认领过的优先)。"""
    from db.connection import get_connection
    from services.publish_channels.api_client import title_sha

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE publish_channel_submissions SET claimed_item_id = %(item)s
                WHERE client_reference = (
                      SELECT client_reference FROM publish_channel_submissions
                       WHERE local_media_id = %(media)s AND title_sha = %(sha)s
                         AND (%(since)s::timestamp IS NULL OR created_at >= %(since)s::timestamp - INTERVAL '5 seconds')
                         AND (claimed_item_id IS NULL OR claimed_item_id = %(item)s)
                       ORDER BY (claimed_item_id IS NOT DISTINCT FROM %(item)s) DESC, created_at DESC
                       LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING client_reference, outcome, order_sn, created_at""",
            {"item": int(item["id"]), "media": int(item.get("media_id") or 0),
             "sha": title_sha(item.get("article_title") or ""), "since": item.get("last_submit_at")})
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if row is not None else []
        conn.commit()
    finally:
        conn.close()
    if row is None:
        return None
    return row if isinstance(row, dict) else dict(zip(cols, row))


def _fail_and_refund(item: dict, reason: str) -> bool:
    """待同步 → 失败,原额退回。只在条目仍是待同步时动(并发安全);退款键与应用其他退款同一个。"""
    from db.connection import get_connection
    from db.meijiehezi_db import refund_for_publish_order

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE mhz_publish_order_items SET status = 'failed', reject_reason = %s
                        WHERE id = %s AND status = 'awaiting_sync' RETURNING user_id, cost_points""",
                    (reason, int(item["id"])))
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if row is None:
        return False
    user_id, cost = (row["user_id"], row["cost_points"]) if isinstance(row, dict) else (row[0], row[1])
    if int(cost or 0) > 0:
        refund_for_publish_order(user_id=int(user_id), amount=int(cost), refund_key=f"item:{int(item['id'])}",
                                 reason=reason)
    return True


async def resolve_awaiting(client) -> dict:
    """推进一轮待同步条目。返回 {"linked", "failed", "waiting"}。"""
    from db.meijiehezi_db import link_awaiting_sync_to_order_sn
    from services.publish_channels.api_client import set_submission_outcome

    out = {"linked": 0, "failed": 0, "waiting": 0}
    for it in await asyncio.to_thread(_awaiting_items):
        sub = await asyncio.to_thread(_claim_submission, it)
        overdue = bool(it.get("overdue"))
        sn = None
        if sub is not None and sub["outcome"] == "ordered":
            sn = sub["order_sn"]
        elif sub is not None and sub["outcome"] == "rejected":
            out["failed"] += await asyncio.to_thread(_fail_and_refund, it, REASON_NOT_ACCEPTED)
            continue
        elif sub is not None:
            after = (sub["created_at"] - LOOKBACK).isoformat()     # created_at 是带时区的
            try:
                sn = await client.find_order_by_reference(sub["client_reference"], after)
            except Exception as exc:                  # 这一轮查不到列表:下一轮再认,不判失败
                logger.warning("[publish-channel] 认单 %s 失败:%s", it["id"], type(exc).__name__)
                out["waiting"] += 1
                continue
            if sn:
                await asyncio.to_thread(set_submission_outcome, sub["client_reference"], "ordered", sn)
        if sn:
            out["linked"] += await asyncio.to_thread(link_awaiting_sync_to_order_sn, int(it["id"]), sn)
        elif overdue:
            out["failed"] += await asyncio.to_thread(_fail_and_refund, it, REASON_NOT_FOUND)
        else:
            out["waiting"] += 1
    return out


async def sync_once() -> dict:
    """推进一轮。返回 {"mode", "checked", "rows"};没接入 ⇒ {"mode": None}。"""
    mode = configured_mode()
    if mode is None:
        return {"mode": None}
    from db.meijiehezi_db import sync_mhz_orders

    if mode == DRY_RUN:
        from services.publish_channels.dry_run import ORDER_PREFIX

        items = await asyncio.to_thread(_in_flight, ORDER_PREFIX + "%", _dry_run_delay())
        rows = dry_run_rows(items)
    else:
        from services.publish_channels import get_client

        client = get_client()
        try:
            awaiting = await resolve_awaiting(client)
            items = await asyncio.to_thread(_in_flight, "ord\\_%:itm\\_%")
            rows = await api_rows(items, client)
        finally:
            await client.close()
        if rows:
            await asyncio.to_thread(sync_mhz_orders, 1, rows)
        return {"mode": mode, "checked": len(items), "rows": len(rows), "awaiting": awaiting}
    if rows:
        await asyncio.to_thread(sync_mhz_orders, 1, rows)
    return {"mode": mode, "checked": len(items), "rows": len(rows)}
