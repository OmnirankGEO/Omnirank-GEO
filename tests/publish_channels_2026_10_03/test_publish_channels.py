# -*- coding: utf-8 -*-
"""开源版发布渠道(services/publish_channels)的锁。

① 模拟发布全流程(真 PG):目录 → 下单 → 状态回流 → 已发布;全程拦掉出站连接,外呼次数必须为 0。
② 发布渠道 API 客户端:用 httpx 的 MockTransport 接一个按契约行为写的假对端 ——
   每个媒体一张单、撤一个不影响另一个、5xx 同键重试只下一张单、重试完仍不明 ⇒ 待同步;
   待同步按 client_reference 认回来、明确拒绝的立即退、认不到的满 24 小时才退(真 PG);
   报错与回流原因文字都是本模块自己的话,不含成本 / 加价类词。
③ 配置:不设 / 认不出 / API 缺一项 ⇒ 没接入,get_client 抛那一句话;闸门只在没接入时拦。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest

BANNED = re.compile(r"成本|进货|加价|系数|倍率|涨价|价格变动|markup|multiplier|cost", re.I)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- ③ 配置

@pytest.mark.parametrize("env,expect", [
    ({}, None),
    ({"PUBLISH_CHANNEL": "dry_run"}, "dry_run"),
    ({"PUBLISH_CHANNEL": "DRY_RUN"}, "dry_run"),
    ({"PUBLISH_CHANNEL": "something"}, None),
    ({"PUBLISH_CHANNEL": "api"}, None),
    ({"PUBLISH_CHANNEL": "api", "PUBLISH_CHANNEL_API_BASE": "http://127.0.0.1:1"}, None),
    ({"PUBLISH_CHANNEL": "api", "PUBLISH_CHANNEL_API_KEY": "k"}, None),
    ({"PUBLISH_CHANNEL": "api", "PUBLISH_CHANNEL_API_BASE": "http://127.0.0.1:1", "PUBLISH_CHANNEL_API_KEY": "k"},
     "api"),
])
def test_configured_mode(monkeypatch, env, expect):
    from services import publish_channels as pc

    for k in (pc.MODE_ENV, pc.BASE_ENV, pc.KEY_ENV):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert pc.configured_mode() == expect


def test_unconfigured_client_raises_the_one_sentence(monkeypatch):
    from services import publish_channels as pc
    from services.meijiehezi.client import ChannelNotConfigured
    from services.publish_channel_notice import CHANNEL_NOT_CONFIGURED

    monkeypatch.delenv(pc.MODE_ENV, raising=False)
    with pytest.raises(ChannelNotConfigured) as e:
        pc.get_client()
    assert str(e.value) == CHANNEL_NOT_CONFIGURED


@pytest.mark.parametrize("mode,status", [(None, 503), ("dry_run", 200)])
def test_gate_blocks_only_when_unconfigured(monkeypatch, mode, status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.publish_channel_gate import GATED_ROUTES, install

    monkeypatch.delenv("PUBLISH_CHANNEL", raising=False)
    if mode:
        monkeypatch.setenv("PUBLISH_CHANNEL", mode)
    method, path = next((m, p) for m, p in GATED_ROUTES if "{" not in p)
    app = FastAPI()
    app.add_api_route(path, lambda: {"ok": True}, methods=[method])
    install(app)
    r = TestClient(app).request(method, path)
    assert r.status_code == status, r.text


# ---------------------------------------------------------------- ① 模拟发布全流程 · 0 外呼

@pytest.fixture
def no_outbound(monkeypatch):
    """拦掉一切非本机的出站连接并计数(本机的测试库连接照常放行)。"""
    attempts = []
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def _local(addr) -> bool:
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host in ("localhost",):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def connect(self, addr):
        if not _local(addr):
            attempts.append(addr)
            raise OSError("测试里禁止出站")
        return real_connect(self, addr)

    def create_connection(addr, *a, **k):
        if not _local(addr):
            attempts.append(addr)
            raise OSError("测试里禁止出站")
        return real_create(addr, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return attempts


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL") or ""
    if "test" not in url:
        pytest.skip("需要名字里带 test 的测试库")
    import psycopg2

    c = psycopg2.connect(url)
    made = {"orders": [], "sns": [], "titles": []}
    yield c, made
    c.rollback()
    with c.cursor() as cur:
        cur.execute("DELETE FROM mhz_publish_order_items WHERE order_id = ANY(%s)", (made["orders"],))
        cur.execute("DELETE FROM mhz_publish_orders WHERE id = ANY(%s)", (made["orders"],))
        cur.execute("DELETE FROM mhz_synced_orders WHERE order_sn = ANY(%s)", (made["sns"],))
        if made["titles"]:
            from services.publish_channels.api_client import title_sha

            cur.execute("DELETE FROM publish_channel_submissions WHERE title_sha = ANY(%s)",
                        ([title_sha(t) for t in made["titles"]],))
    c.commit()
    c.close()


def test_dry_run_full_flow_publishes_with_zero_outbound_calls(monkeypatch, db, no_outbound):
    from services import publish_channels as pc
    from services.publish_channels import dry_run, status

    conn, made = db
    monkeypatch.setenv(pc.MODE_ENV, "dry_run")
    monkeypatch.setenv(status.DELAY_ENV, "1")
    client = pc.get_client()
    catalog = _run(client.get_all_media_raw())
    assert catalog and all(dry_run.DEMO_TAG in r["media_name"] for r in catalog)

    media = catalog[0]
    result = _run(client.publish(title="模拟发布测试", content_md="正文", media_ids=[media["id"]]))
    assert result.success and result.order_sn.startswith(dry_run.ORDER_PREFIX)
    sn = result.order_sn_map[media["id"]]
    made["sns"].append(sn)

    with conn.cursor() as cur:
        cur.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status, total_items) "
                    "VALUES (4747, NULL, %s, 'pending', 2) RETURNING id", ("模拟发布测试-" + uuid.uuid4().hex[:6],))
        oid = cur.fetchone()[0]
        made["orders"].append(oid)
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        for submitted_at, order_sn in ((old, sn), (datetime.now(timezone.utc), "DRY-FRESH" + uuid.uuid4().hex[:8])):
            cur.execute("INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name, media_type, "
                        "status, mhz_order_id, cost_points, submitted_at) "
                        "VALUES (%s, 4747, %s, %s, 'mhz', 'submitted', %s, 0, %s)",
                        (oid, media["id"], media["media_name"], order_sn, submitted_at))
            made["sns"].append(order_sn)
    conn.commit()

    out = _run(status.sync_once())
    assert out["mode"] == "dry_run" and out["rows"] >= 2

    with conn.cursor() as cur:
        cur.execute("SELECT mhz_order_id, status, publish_url FROM mhz_publish_order_items WHERE order_id = %s "
                    "ORDER BY id", (oid,))
        done, fresh = cur.fetchall()
    conn.commit()
    assert done[1] == "published" and done[2] == dry_run.result_url(sn), done      # 满延时 ⇒ 已发布、链接一看就是模拟
    assert fresh[1] == "submitted", fresh                                            # 不满延时 ⇒ 仍在途
    assert _run(client.cancel_order(fresh[0])) is True                               # 在途的模拟单可以撤
    assert no_outbound == [], no_outbound                                            # 全程 0 次外呼


def test_dry_run_module_imports_no_network_library():
    import ast
    import inspect

    from services.publish_channels import dry_run

    tree = ast.parse(inspect.getsource(dry_run))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
    assert not names & {"httpx", "requests", "aiohttp", "urllib", "socket", "http"}, names


# ---------------------------------------------------------------- ② 发布渠道 API 客户端

OFFERS = {1: "off_AAAA1111", 2: "off_BBBB2222"}


class FakeChannel:
    """按公开契约 v1 行为写的假对端:同键同体重放首次结果;每单可整单撤;订单列表按 created_after 过滤。

    order_plan:按顺序消耗,每次 POST /orders 取一个动作 ——
      "ok"(默认)/ "500_after_create"(其实建了单,回 500)/ "timeout_after_create"(建了单,读超时)。
    reject_offers:这些媒体下单时回 422(渠道明确不受理,不建单)。
    """

    def __init__(self):
        self.calls, self.quotes, self.orders, self.by_key = [], {}, {}, {}
        self.order_plan: list[str] = []
        self.timeout_forever = False
        self.reject_offers: set[str] = set()

    def __call__(self, req):
        import httpx

        self.calls.append(req)
        path, key = req.url.path, req.headers.get("idempotency-key")
        body = json.loads(req.content or b"{}")
        if req.method == "POST" and path.endswith("/quotes"):
            if key in self.by_key:
                return httpx.Response(201, json=self.by_key[key], headers={"Idempotent-Replayed": "true"})
            qid = f"qt_{len(self.quotes) + 1:08d}"
            self.quotes[qid] = [it["offer_id"] for it in body["items"]]
            self.by_key[key] = {"quote_id": qid, "items": []}
            return httpx.Response(201, json=self.by_key[key])
        if req.method == "POST" and path.endswith("/orders"):
            action = self.order_plan.pop(0) if self.order_plan else "ok"
            offers = self.quotes[body["quote_id"]]
            if key not in self.by_key:
                if set(offers) & self.reject_offers:
                    return httpx.Response(422, json={"error": {"code": "CONTENT_REJECTED", "message": "成本不够"}})
                n = len(self.orders) + 1
                order = {"order_id": f"ord_{n:08d}", "quote_id": body["quote_id"], "status": "pending",
                         "client_reference": body.get("client_reference"),
                         "created_at": datetime.now(timezone.utc).isoformat(),
                         "items": [{"item_id": f"itm_{n:04d}{i:04d}", "offer_id": o, "status": "pending"}
                                   for i, o in enumerate(offers)]}
                self.orders[order["order_id"]] = order
                self.by_key[key] = order
            if self.timeout_forever or action == "timeout_after_create":
                raise httpx.ReadTimeout("timeout", request=req)
            if action == "500_after_create":
                return httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR", "message": "x"}})
            return httpx.Response(201, json=self.by_key[key])
        if req.method == "POST" and path.endswith("/cancel"):
            order = self.orders[path.split("/")[-2]]
            for it in order["items"]:
                if it["status"] == "pending":
                    it["status"] = "cancelled"
            return httpx.Response(200, json=order)
        if req.method == "GET" and path.endswith("/orders"):
            after = datetime.fromisoformat(req.url.params["created_after"])
            data = [o for o in self.orders.values() if datetime.fromisoformat(o["created_at"]) > after]
            return httpx.Response(200, json={"data": data[::-1], "page": 1, "page_size": 100, "total": len(data)})
        if req.method == "GET" and "/orders/" in path:
            return httpx.Response(200, json=self.orders[path.rsplit("/", 1)[-1]])
        raise AssertionError(req.url)

    def posts(self, suffix):
        return [r for r in self.calls if r.method == "POST" and r.url.path.endswith(suffix)]


def _mock_client(monkeypatch, handler, *, real_db=False):
    import httpx

    from services.publish_channels import api_client

    monkeypatch.setattr(api_client, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(api_client, "offer_ids_for", lambda ids: {i: OFFERS[i] for i in ids if i in OFFERS})
    monkeypatch.setattr(api_client, "local_ids_for", lambda oids: {o: i for i, o in OFFERS.items() if o in oids})
    if not real_db:
        subs = {}
        monkeypatch.setattr(api_client, "record_submission", lambda ref, m, t: subs.__setitem__(ref, [m, "pending"]))
        monkeypatch.setattr(api_client, "set_submission_outcome",
                            lambda ref, outcome, sn=None: subs[ref].__setitem__(1, outcome))
    return api_client.ApiChannelClient("http://channel.invalid/v1", "test-key", transport=httpx.MockTransport(handler))


def test_api_client_one_quote_and_one_order_per_media(monkeypatch):
    ch = FakeChannel()
    client = _mock_client(monkeypatch, ch)
    r = _run(client.publish(title="T", content_md="B", media_ids=[1, 2]))
    quotes, orders = ch.posts("/quotes"), ch.posts("/orders")
    assert [json.loads(q.content)["items"] for q in quotes] == [[{"offer_id": "off_AAAA1111"}],
                                                                 [{"offer_id": "off_BBBB2222"}]]
    assert len(orders) == 2 and len(ch.orders) == 2                                  # 两个媒体 ⇒ 两张单
    assert r.order_sn_map == {1: "ord_00000001:itm_00010000", 2: "ord_00000002:itm_00020000"}
    keys = [q.headers["idempotency-key"] for q in quotes + orders]
    assert all(keys) and len(set(keys)) == 4                                         # 每次报价、每次下单各一个键
    refs = [json.loads(o.content)["client_reference"] for o in orders]
    assert all(x.startswith("oss-") for x in refs) and len(set(refs)) == 2           # 每个条目一个唯一引用
    assert all(q.headers["authorization"] == "Bearer test-key" for q in ch.calls)


def test_api_client_cancel_one_leaves_the_other(monkeypatch):
    ch = FakeChannel()
    client = _mock_client(monkeypatch, ch)
    r = _run(client.publish(title="T", content_md="B", media_ids=[1, 2]))
    assert _run(client.cancel_order(r.order_sn_map[1])) is True
    assert ch.orders["ord_00000001"]["items"][0]["status"] == "cancelled"
    assert ch.orders["ord_00000002"]["items"][0]["status"] == "pending"              # 另一个媒体不受影响


def test_api_client_retries_5xx_with_the_same_key_and_orders_once(monkeypatch):
    ch = FakeChannel()
    ch.order_plan = ["500_after_create"]
    r = _run(_mock_client(monkeypatch, ch).publish(title="T", content_md="B", media_ids=[1]))
    orders = ch.posts("/orders")
    assert len(orders) == 2 and orders[0].headers["idempotency-key"] == orders[1].headers["idempotency-key"]
    assert len(ch.orders) == 1 and r.order_sn_map == {1: "ord_00000001:itm_00010000"}    # 只下了一张单


def test_api_client_unclear_after_retries_is_ambiguous(monkeypatch):
    from services.meijiehezi.client import AmbiguousResponseError

    ch = FakeChannel()
    ch.timeout_forever = True
    with pytest.raises(AmbiguousResponseError):
        _run(_mock_client(monkeypatch, ch).publish(title="T", content_md="B", media_ids=[1]))
    orders = ch.posts("/orders")
    assert len(orders) == 3 and len({o.headers["idempotency-key"] for o in orders}) == 1   # 同一个键,有限次
    assert len(ch.orders) == 1


def test_api_client_all_rejected_is_publish_error(monkeypatch):
    from services.meijiehezi.client import PublishError

    ch = FakeChannel()
    ch.reject_offers = {"off_AAAA1111", "off_BBBB2222"}
    with pytest.raises(PublishError) as e:
        _run(_mock_client(monkeypatch, ch).publish(title="T", content_md="B", media_ids=[1, 2]))
    assert "CONTENT_REJECTED" in str(e.value) and not BANNED.search(str(e.value))
    assert ch.orders == {}


def test_api_client_catalog_maps_price_in_fen_to_yuan(monkeypatch):
    import httpx

    def handler(req):
        return httpx.Response(200, json={"data": [
            {"offer_id": "off_AAAA1111", "name": "示例媒体", "content_type": "article", "platform": "p", "tier": "T1",
             "price": {"value": 1234, "currency": "CNY"}, "available": True},
            {"offer_id": "off_BBBB2222", "name": "不可下单", "content_type": "article", "platform": "p", "tier": "T1",
             "price": {"value": 99, "currency": "CNY"}, "available": False}], "page": 1, "page_size": 100, "total": 2})

    total, rows = _run(_mock_client(monkeypatch, handler).get_media_list_raw(page=1, limit=100))
    assert total == 2 and [(r["id"], r["media_name"], r["price"]) for r in rows] == [(1, "示例媒体", 12.34)]


@pytest.mark.parametrize("status,code", [(409, "QUOTE_STALE"), (402, "INSUFFICIENT_BALANCE"), (500, "INTERNAL_ERROR")])
def test_api_client_error_text_is_clean(monkeypatch, status, code):
    import httpx

    from services.meijiehezi.client import PublishError

    def handler(req):
        return httpx.Response(status, json={"error": {"code": code, "message": "进货价变了,成本上调", "request_id": "r"}})

    with pytest.raises(PublishError) as e:
        _run(_mock_client(monkeypatch, handler).publish(title="T", content_md="B", media_ids=[1]))
    assert code in str(e.value) and not BANNED.search(str(e.value)), str(e.value)   # 不转述对方原文


@pytest.mark.parametrize("remote,code,expect", [
    ({"status": "rejected", "failure": {"code": "CONTENT_REJECTED", "message": "进货价变了,成本上调"}}, -1,
     "发布渠道没有发出(原因码 CONTENT_REJECTED),算力已原额退回"),
    ({"status": "failed", "failure": {"code": "供应商 成本", "message": "markup"}}, -1,
     "发布渠道没有发出(原因码 未知),算力已原额退回"),
    ({"status": "cancelled"}, -2, "已撤单,算力已原额退回"),
])
def test_status_reason_is_our_own_words(remote, code, expect):
    from services.publish_channels import status

    class Client:
        async def get_order(self, order_id):
            return {"order_id": order_id, "items": [dict(remote, item_id="itm_X")]}

    rows = _run(status.api_rows([{"mhz_order_id": "ord_X:itm_X", "media_type": "mhz", "media_id": 1}], Client()))
    assert [(r["status"], r["reason"]) for r in rows] == [(code, expect)]
    assert not BANNED.search(rows[0]["reason"])


# ---------------------------------------------------------------- ② 待同步认单(真 PG)

def _awaiting_item(conn, made, *, title, media_id, cost, since_hours):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status, total_items) "
                    "VALUES (4747, NULL, %s, 'pending', 1) RETURNING id", (title,))
        oid = cur.fetchone()[0]
        made["orders"].append(oid)
        cur.execute("INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name, media_type, status, "
                    "cost_points, last_submit_at, awaiting_sync_since) VALUES (%s, 4747, %s, 'm', 'mhz', "
                    "'awaiting_sync', %s, NOW() - make_interval(hours => %s) - INTERVAL '1 minute', "
                    "NOW() - make_interval(hours => %s)) RETURNING id", (oid, media_id, cost, since_hours, since_hours))
        iid = cur.fetchone()[0]
    conn.commit()
    return iid


def _item(conn, iid):
    with conn.cursor() as cur:
        cur.execute("SELECT status, mhz_order_id, reject_reason FROM mhz_publish_order_items WHERE id = %s", (iid,))
        row = cur.fetchone()
    conn.commit()
    return row


def _only_mine(monkeypatch, status, ids):
    real = status._awaiting_items
    monkeypatch.setattr(status, "_awaiting_items", lambda: [r for r in real() if r["id"] in ids])


def test_awaiting_items_are_recovered_by_client_reference(monkeypatch, db):
    import db.meijiehezi_db as mdb
    from services.meijiehezi.client import AmbiguousResponseError
    from services.publish_channels import status

    conn, made = db
    refunds = []
    monkeypatch.setattr(mdb, "refund_for_publish_order", lambda **kw: refunds.append(kw) or {"success": True})
    title = "认单测试-" + uuid.uuid4().hex[:8]
    made["titles"].append(title)
    ch = FakeChannel()
    ch.order_plan = ["timeout_after_create"] * 3                   # 媒体 1:建了单但三次都读超时 ⇒ 不明
    ch.reject_offers = {"off_BBBB2222"}                             # 媒体 2:渠道明确不受理
    client = _mock_client(monkeypatch, ch, real_db=True)
    with pytest.raises(AmbiguousResponseError):                    # 受理不明与拒绝混在一起 ⇒ 整批进待同步
        _run(client.publish(title=title, content_md="B", media_ids=[1, 2]))

    unclear = _awaiting_item(conn, made, title=title, media_id=1, cost=7, since_hours=0)
    rejected = _awaiting_item(conn, made, title=title, media_id=2, cost=5, since_hours=0)
    _only_mine(monkeypatch, status, {unclear, rejected})
    out = _run(status.resolve_awaiting(client))
    assert out == {"linked": 1, "failed": 1, "waiting": 0}, out
    assert _item(conn, unclear)[:2] == ("submitted", "ord_00000001:itm_00010000")   # 按 client_reference 认回来
    assert _item(conn, rejected) == ("failed", None, status.REASON_NOT_ACCEPTED)
    assert refunds == [{"user_id": 4747, "amount": 5, "refund_key": f"item:{rejected}",
                        "reason": status.REASON_NOT_ACCEPTED}]


def test_awaiting_item_fails_and_refunds_only_after_24h(monkeypatch, db):
    import db.meijiehezi_db as mdb
    from services.publish_channels import status

    conn, made = db
    refunds = []
    monkeypatch.setattr(mdb, "refund_for_publish_order", lambda **kw: refunds.append(kw) or {"success": True})
    title = "超时测试-" + uuid.uuid4().hex[:8]
    made["titles"].append(title)
    client = _mock_client(monkeypatch, FakeChannel(), real_db=True)   # 渠道那边没有任何单
    fresh = _awaiting_item(conn, made, title=title, media_id=1, cost=3, since_hours=23)
    old = _awaiting_item(conn, made, title=title, media_id=2, cost=4, since_hours=25)
    _only_mine(monkeypatch, status, {fresh, old})
    out = _run(status.resolve_awaiting(client))
    assert out == {"linked": 0, "failed": 1, "waiting": 1}, out
    assert _item(conn, fresh)[0] == "awaiting_sync"                                  # 不满 24 小时:继续等
    assert _item(conn, old) == ("failed", None, status.REASON_NOT_FOUND)
    assert [(r["refund_key"], r["amount"]) for r in refunds] == [(f"item:{old}", 4)]


# ---------------------------------------------------------------- 时区(真 PG · 会话时区 Asia/Shanghai)
# 生产库会话时区是北京时间,提交时间列不带时区、由 NOW() 写入。时间先后必须在库里比;
# 交给渠道的 created_after 必须是真实时刻。旧写法(把不带时区的值当 UTC)在这里必红。

SHANGHAI = "Asia/Shanghai"


def _shanghai(monkeypatch):
    """让本包与测试自己的连接都用北京时间会话;返回一条测试用连接(行是元组)。
    交给应用的连接与生产 db.connection.get_connection 同形:行是字典(RealDictCursor)。"""
    import psycopg2
    import psycopg2.extras

    import db.connection as dbc

    url = os.environ["TEST_DATABASE_URL"]
    opts = f"-c timezone={SHANGHAI}"
    monkeypatch.setattr(dbc, "get_connection",
                        lambda: psycopg2.connect(url, options=opts, cursor_factory=psycopg2.extras.RealDictCursor))
    return psycopg2.connect(url, options=opts)


def test_dry_run_due_check_is_done_in_the_database_clock(monkeypatch, db):
    from services import publish_channels as pc
    from services.publish_channels import status

    _conn, made = db
    sh = _shanghai(monkeypatch)
    monkeypatch.setenv(pc.MODE_ENV, "dry_run")
    monkeypatch.setenv(status.DELAY_ENV, "1")
    try:
        with sh.cursor() as cur:
            cur.execute("SHOW timezone")
            assert cur.fetchone()[0] == SHANGHAI
            cur.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title, status, total_items) "
                        "VALUES (4747, NULL, %s, 'pending', 2) RETURNING id", ("时区测试-" + uuid.uuid4().hex[:6],))
            oid = cur.fetchone()[0]
            made["orders"].append(oid)
            sns = []
            for ago in (90, 30):                                    # 90 秒前 ⇒ 满 1 分钟;30 秒前 ⇒ 没满
                sn = "DRY-TZ" + uuid.uuid4().hex[:10]
                sns.append(sn)
                made["sns"].append(sn)
                cur.execute("INSERT INTO mhz_publish_order_items (order_id, user_id, media_id, media_name, media_type, "
                            "status, mhz_order_id, cost_points, submitted_at) VALUES (%s, 4747, 1, 'm', 'mhz', "
                            "'submitted', %s, 0, NOW() - make_interval(secs => %s))", (oid, sn, ago))
        sh.commit()
        _run(status.sync_once())
        # 第二轮:没满延时的那条已经同步过一次,这回走同步表「已存在」那条分支(E10 抓到的 NameError 就在那里)
        _run(status.sync_once())
        with sh.cursor() as cur:
            cur.execute("SELECT mhz_order_id, status FROM mhz_publish_order_items WHERE order_id = %s", (oid,))
            got = dict(cur.fetchall())
        sh.commit()
    finally:
        sh.close()
    assert got == {sns[0]: "published", sns[1]: "submitted"}, got


def test_created_after_sent_to_the_channel_is_the_real_moment(monkeypatch, db):
    import db.meijiehezi_db as mdb
    from services.meijiehezi.client import AmbiguousResponseError
    from services.publish_channels import status

    _conn, made = db
    sh = _shanghai(monkeypatch)
    monkeypatch.setattr(mdb, "refund_for_publish_order", lambda **kw: {"success": True})
    title = "时区认单-" + uuid.uuid4().hex[:8]
    made["titles"].append(title)
    ch = FakeChannel()
    ch.order_plan = ["timeout_after_create"] * 3
    client = _mock_client(monkeypatch, ch, real_db=True)
    with pytest.raises(AmbiguousResponseError):
        _run(client.publish(title=title, content_md="B", media_ids=[1]))
    try:
        iid = _awaiting_item(sh, made, title=title, media_id=1, cost=0, since_hours=0)
    finally:
        sh.close()
    _only_mine(monkeypatch, status, {iid})
    out = _run(status.resolve_awaiting(client))
    lists = [r for r in ch.calls if r.method == "GET" and r.url.path.endswith("/orders")]
    sent = datetime.fromisoformat(lists[0].url.params["created_after"])
    assert sent.tzinfo is not None
    assert timedelta(0) < datetime.now(timezone.utc) - sent < timedelta(minutes=15), sent   # 往前 10 分钟,不偏 8 小时
    assert out == {"linked": 1, "failed": 0, "waiting": 0}, out
