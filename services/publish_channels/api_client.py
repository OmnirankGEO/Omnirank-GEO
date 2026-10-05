"""发布渠道 API 客户端(PUBLISH_CHANNEL=api)。

按公开契约 v1 写的第三方客户端:只用契约里公开的接口与字段,服务地址与 Key 都从配置来,不提供默认值。
  · 目录:分页拉 ``GET /media?content_type=article``,把契约的媒体条目编号(字符串)映射成本地整数主键
    (表 ``publish_channel_offers``,迁移 066),写成应用目录同步认得的那种行;
  · 下单:**每个媒体单独** ``POST /quotes`` 锁价 → ``POST /orders`` 下单,所以一个本地单号对应渠道的一整单,
    撤单(契约按整单撤)只撤这一个媒体。本地单号是 ``<order_id>:<item_id>``;
  · 重试:每次报价、每次下单各生成一个 Idempotency-Key;碰到 5xx / 408 / 429 / 409 处理中 / 超时 / 连接错误,
    用**同一个**键有限次重试(契约保证同键重试不会重复下单)。其余 4xx = 渠道明确没受理;
  · 结果不明:下单重试完仍不明 ⇒ ``AmbiguousResponseError``,应用把条目放进「待同步」,不退款。
    每次下单都带一个本条目唯一的 ``client_reference``,下单前先记进表 ``publish_channel_submissions``,
    状态回流(status.py)按它在渠道订单列表里认单;24 小时仍认不到才判失败、原额退回;
  · 状态:``GET /orders/{order_id}``;撤单:``POST /orders/{order_id}/cancel``(只能撤还在排队的条目)。
只接软文(article)。自媒体图文与短视频的内容形态和应用现有的不一样,这一版不接。
错误文字只用本模块自己的话,不转述对方的原文。
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

import httpx

from services.meijiehezi.client import AmbiguousResponseError, PublishError, PublishResult

PAGE_SIZE = 100
TIMEOUT_SECONDS = 30.0
RETRY_DELAYS = (1.0, 3.0)        # 同一个键最多再试两次:先等 1 秒,再等 3 秒
_RETRY_STATUS = {408, 429}       # 另加所有 5xx,以及 409 处理中


def title_sha(title: str) -> str:
    return hashlib.sha256((title or "").strip().encode("utf-8")).hexdigest()


class _Unclear(Exception):
    """重试用完仍不知道对方有没有受理(超时 / 连接错误 / 5xx / 429 …)。"""

    def __init__(self, status: int, code: str):
        super().__init__(f"unclear {status} {code}")
        self.status, self.code = int(status or 0), code


def _yuan(amount: Any) -> float:
    try:
        return round(int((amount or {}).get("value") or 0) / 100.0, 2)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _fail(status: int, code: str) -> PublishError:
    return PublishError(int(status or 0), f"发布渠道没有接受这次请求(错误码 {code or status})")


# ---------------------------------------------------------------- 媒体编号 ↔ 本地主键

def local_ids_for(offer_ids: list[str]) -> dict[str, int]:
    """为每个契约媒体编号取(没有就分配)一个本地整数主键。"""
    from db.connection import get_connection

    out: dict[str, int] = {}
    if not offer_ids:
        return out
    conn = get_connection()
    try:
        cur = conn.cursor()
        for oid in offer_ids:
            cur.execute("INSERT INTO publish_channel_offers (offer_id) VALUES (%s) "
                        "ON CONFLICT (offer_id) DO UPDATE SET offer_id = EXCLUDED.offer_id RETURNING local_id", (oid,))
            row = cur.fetchone()
            out[oid] = int(row["local_id"] if isinstance(row, dict) else row[0])
        conn.commit()
    finally:
        conn.close()
    return out


def offer_ids_for(local_ids: list[int]) -> dict[int, str]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT local_id, offer_id FROM publish_channel_offers WHERE local_id = ANY(%s)",
                    ([int(x) for x in local_ids],))
        rows = cur.fetchall() or []
    finally:
        conn.close()
    pairs = [(r["local_id"], r["offer_id"]) if isinstance(r, dict) else (r[0], r[1]) for r in rows]
    return {int(a): str(b) for a, b in pairs}


def record_submission(client_reference: str, local_media_id: int, title: str) -> None:
    """下单前先记一笔:client_reference 是这次下单在渠道那边的唯一认单依据。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO publish_channel_submissions (client_reference, local_media_id, title_sha) "
                    "VALUES (%s, %s, %s)", (client_reference, int(local_media_id), title_sha(title)))
        conn.commit()
    finally:
        conn.close()


def set_submission_outcome(client_reference: str, outcome: str, order_sn: str | None = None) -> None:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE publish_channel_submissions SET outcome = %s, order_sn = COALESCE(%s, order_sn) "
                    "WHERE client_reference = %s", (outcome, order_sn, client_reference))
        conn.commit()
    finally:
        conn.close()


def _offer_row(offer: dict, local_id: int) -> dict:
    """契约媒体条目 → 应用目录同步认得的行(字段名与应用的目录映射一致)。"""
    price = _yuan(offer.get("price"))
    return {
        "id": local_id,
        "media_name": str(offer.get("name") or ""),
        "price": price, "price1": price, "price2": price,
        "area": "、".join(str(x) for x in offer.get("regions") or []),
        "portal_media": str(offer.get("platform") or ""),
        "resource_type_name": "、".join(str(x) for x in offer.get("industries") or []),
        "avg_time": int(offer.get("turnaround_hours") or 0),
        "remark": "",
        "case_link": "",
    }


class ApiChannelClient:
    """与应用调用的那组方法同名同签名。"""

    def __init__(self, base_url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not base_url or not api_key:
            raise ValueError("发布渠道 API 的服务地址与 Key 都必须配置")
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=TIMEOUT_SECONDS, transport=transport,
                                       headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def check_session(self) -> bool:
        r = await self._http.get("/media", params={"content_type": "article", "page": 1, "page_size": 1})
        return r.status_code == 200

    async def refresh_token(self) -> None:
        return None

    async def _send(self, method: str, path: str, *, key: str | None = None, **kw) -> dict:
        """发一个请求;可重试的情况用同一个键有限次重试。明确拒绝 ⇒ PublishError;重试完仍不明 ⇒ _Unclear。"""
        headers = {"Idempotency-Key": key} if key else None
        last = (0, "")
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                await asyncio.sleep(RETRY_DELAYS[attempt - 1])
            try:
                r = await self._http.request(method, path, headers=headers, **kw)
            except httpx.TransportError as exc:          # 超时、连不上、读到一半断开
                last = (0, type(exc).__name__)
                continue
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code < 400:
                return body or {}
            code = str(((body or {}).get("error") or {}).get("code") or "") if isinstance(body, dict) else ""
            if (r.status_code in _RETRY_STATUS or r.status_code >= 500
                    or (r.status_code == 409 and code == "IDEMPOTENCY_IN_PROGRESS")):
                last = (r.status_code, code)
                continue
            raise _fail(r.status_code, code)
        raise _Unclear(*last)

    async def _json(self, method: str, path: str, **kw) -> dict:
        """不下单的请求(目录、查单、撤单):重试完仍不明也按失败处理。"""
        try:
            return await self._send(method, path, **kw)
        except _Unclear as exc:
            raise _fail(exc.status, exc.code) from None

    # ---------------------------------------------------------------- 目录

    async def get_media_list_raw(self, page: int = 1, limit: int = 50, **_filters) -> tuple[int, list]:
        body = await self._json("GET", "/media", params={"content_type": "article", "page": max(1, int(page or 1)),
                                                         "page_size": min(PAGE_SIZE, max(1, int(limit or 50))),
                                                         "sort": "price_asc"})
        offers = [o for o in body.get("data") or [] if o.get("available")]
        ids = local_ids_for([str(o["offer_id"]) for o in offers])
        return int(body.get("total") or 0), [_offer_row(o, ids[str(o["offer_id"])]) for o in offers]

    async def get_all_media_raw(self, limit: int = PAGE_SIZE) -> list:
        rows, page = [], 1
        while True:
            total, items = await self.get_media_list_raw(page=page, limit=limit)
            rows.extend(items)
            if not items or page * min(PAGE_SIZE, limit) >= total:
                return rows
            page += 1

    async def get_wemedia_list_raw(self, page: int = 1, limit: int = 50, **_filters) -> tuple[int, list]:
        return 0, []

    async def get_all_wemedia_raw(self, limit: int = 50) -> list:
        return []

    async def get_short_video_list_raw(self, page: int = 1, limit: int = 50, **_filters) -> tuple[int, list]:
        return 0, []

    async def get_all_short_video_raw(self, limit: int = 50) -> list:
        return []

    # ---------------------------------------------------------------- 下单 / 查单 / 撤单

    async def publish(self, title: str, content_md: str, media_ids: list, **_kwargs) -> PublishResult:
        """每个媒体单独报价、单独下单。

        全部受理 ⇒ 返回逐媒体单号;全部被明确拒绝 ⇒ PublishError(没下成单,应用按它原来的失败链处理);
        其余情况(有不明的,或受理与拒绝混在一起)⇒ AmbiguousResponseError:应用把这批条目都放进待同步,
        由状态回流逐条认单 —— 受理了的接上单号,明确拒绝的判失败退款,不明的按 client_reference 去认。
        """
        ids = [int(m) for m in media_ids]
        offers = offer_ids_for(ids)
        missing = [m for m in ids if m not in offers]
        if not ids or missing:
            raise PublishError(404, "所选媒体不在发布渠道目录里,请先同步目录")
        content = {"type": "article", "title": title, "body": content_md}
        sn_map: dict[int, str] = {}
        rejected: dict[int, PublishError] = {}
        unclear: list[int] = []
        for m in ids:
            ref = "oss-" + uuid.uuid4().hex
            record_submission(ref, m, title)
            try:
                sn_map[m] = await self._order_one(offers[m], content, ref)
            except PublishError as exc:
                rejected[m] = exc
                set_submission_outcome(ref, "rejected")
                continue
            except _Unclear:
                unclear.append(m)
                continue
            set_submission_outcome(ref, "ordered", sn_map[m])
        if len(sn_map) == len(ids):
            return PublishResult(success=True, code=200, msg="发布渠道已受理", selected_num=len(ids),
                                 success_count=len(ids), order_sn=sn_map[ids[0]],
                                 raw_data={"orders": len(ids)}, order_sn_map=sn_map)
        if len(rejected) == len(ids):
            raise next(iter(rejected.values()))
        raise AmbiguousResponseError(0, "发布渠道有条目下单结果不明,等状态同步认单",
                                     {"ordered": len(sn_map), "rejected": len(rejected), "unclear": len(unclear)})

    async def _order_one(self, offer_id: str, content: dict, ref: str) -> str:
        """一个媒体:报价 → 下单,返回本地单号。报价阶段的任何失败都意味着没下单 ⇒ PublishError。"""
        try:
            quote = await self._send("POST", "/quotes", key=uuid.uuid4().hex,
                                     json={"items": [{"offer_id": offer_id}]})
        except _Unclear as exc:
            raise _fail(exc.status, exc.code) from None
        order = await self._send("POST", "/orders", key=uuid.uuid4().hex,
                                 json={"quote_id": quote["quote_id"], "content": content, "client_reference": ref})
        item = next((it for it in order.get("items") or [] if str(it.get("offer_id")) == offer_id), None)
        if not order.get("order_id") or item is None:
            raise _Unclear(200, "NO_ITEM")
        return f"{order['order_id']}:{item['item_id']}"

    async def publish_wemedia(self, *args, **kwargs) -> PublishResult:
        raise PublishError(400, "发布渠道这一版只接软文")

    async def publish_short_video(self, *args, **kwargs) -> PublishResult:
        raise PublishError(400, "发布渠道这一版只接软文")

    async def get_order(self, order_id: str) -> dict:
        return await self._json("GET", f"/orders/{order_id}")

    async def find_order_by_reference(self, client_reference: str, created_after: str) -> str | None:
        """在 created_after 之后的订单里按 client_reference 认单,返回本地单号;没有 ⇒ None。"""
        page = 1
        while True:
            body = await self._json("GET", "/orders", params={"created_after": created_after,
                                                              "page": page, "page_size": PAGE_SIZE})
            for order in body.get("data") or []:
                if order.get("client_reference") == client_reference and order.get("items"):
                    return f"{order['order_id']}:{order['items'][0]['item_id']}"
            if not body.get("data") or page * PAGE_SIZE >= int(body.get("total") or 0):
                return None
            page += 1

    async def cancel_order(self, order_sn: str) -> bool:
        order_id, _, item_id = str(order_sn or "").partition(":")
        if not order_id or not item_id:
            return False
        try:
            order = await self._json("POST", f"/orders/{order_id}/cancel", key=uuid.uuid4().hex)
        except PublishError:
            return False
        item = next((it for it in order.get("items") or [] if it.get("item_id") == item_id), None)
        return bool(item and item.get("status") == "cancelled")
