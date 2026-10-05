"""【热修 P0 A】`GET /api/geo-douyin/quotes/{id}/delivery-plan` 真 HTTP + 真 PG16。

## 为什么必须是**真** HTTP + **不 mock** `fetch_brand_display_name`

这个 bug 的全部危险性都在"它躲过了除序列化以外的每一道检查":

* `asyncio.to_thread(fetch_brand_display_name, …)` 在线程里**调用**一个协程函数,
  拿到的是**协程对象**;
* `brand_dto = {"id": …, "name": <coroutine>}` —— 类型注解看不出、`if brand:` 看不出;
* 一路到 FastAPI 序列化响应才炸 ⇒ **该端点 100% 500**。

所以判据必须走到**序列化**那一步(真 HTTP,不是直接 await handler),
并且**不许 mock 那个 helper**(mock 掉它就把被测的那一行换掉了 ——
本仓管这叫「夹具替被测代码干活」)。
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")
OWNER_UID = 6601


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def live_db():
    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    if "test" not in DSN.rsplit("/", 1)[-1].lower():
        raise RuntimeError("安全栓:测试库名必须含 test,实得 " + DSN)

    name = "dplan_" + uuid.uuid4().hex[:8] + "_test"
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "' + name + '"')
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name

    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    cur.execute("SET search_path = public")
    conn.close()

    import db.connection as dbconn
    old_url, old_pool = dbconn.DATABASE_URL, dbconn._pool
    dbconn.DATABASE_URL = dsn
    dbconn._pool = None
    try:
        yield dsn
    finally:
        try:
            dbconn.close_pool()
        except Exception:  # noqa: BLE001
            pass
        dbconn.DATABASE_URL, dbconn._pool = old_url, old_pool
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def world(live_db):
    conn = psycopg2.connect(live_db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET search_path = public")
    cur.execute("INSERT INTO users (id, username, display_name, password_hash, email)"
                " VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
                (OWNER_UID, "dplan_owner", "dplan_owner", "dplan@example.com"))
    brand_name = "热修客户_" + uuid.uuid4().hex[:6]
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                (brand_name, OWNER_UID))
    brand_id = int(cur.fetchone()["id"])
    cur.execute("INSERT INTO quotes (brand_id, owner_user_id, status)"
                " VALUES (%s,%s,'confirmed') RETURNING id", (brand_id, OWNER_UID))
    quote_id = int(cur.fetchone()["id"])
    # 一张**没有客户**的报价:用来打「409 未关联客户」那一支
    cur.execute("INSERT INTO quotes (owner_user_id, status) VALUES (%s,'confirmed')"
                " RETURNING id", (OWNER_UID,))
    orphan_quote_id = int(cur.fetchone()["id"])
    conn.close()
    return {"dsn": live_db, "brand_id": brand_id, "brand_name": brand_name,
            "quote_id": quote_id, "orphan_quote_id": orphan_quote_id}


def _client(world, *, is_admin=True, brand_ids=None):
    """真 HTTP。**只**注入身份,`fetch_brand_display_name` 一个字不 mock。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.geo_douyin_api as mod

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {
            "user_id": OWNER_UID, "id": OWNER_UID, "username": "dplan_owner",
            "is_admin": is_admin,
            "client_brand_ids": list(brand_ids or []),
            "permissions": ["writing:read", "writing:write"],
        }
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(mod.router)
    return TestClient(app, raise_server_exceptions=False)


def _url(world, quote_id=None):
    return "/api/geo-douyin/quotes/{0}/delivery-plan".format(
        quote_id if quote_id is not None else world["quote_id"])


# ══════════════════════════════════════════════════════════════════════════
# 主判据:200 且 brand.name 是 str(不是协程对象)
# ══════════════════════════════════════════════════════════════════════════

def test_delivery_plan_returns_200_with_a_string_brand_name(world):
    """🔴 走到**序列化**那一步。协程对象在这里会 500,而不是"值有点怪"。"""
    with _client(world) as client:
        got = client.get(_url(world))
    assert got.status_code == 200, got.text[:600]
    body = got.json()
    brand = body.get("brand") or {}
    assert isinstance(brand.get("name"), str), brand
    # 🔴 不只是"是个 str":必须是**库里那个客户名**,
    #    否则一个恒空串的实现也能过上面那一条。
    assert brand["name"] == world["brand_name"], (brand, world["brand_name"])
    assert int(brand.get("id")) == world["brand_id"], brand
    # 整个响应体必须能被 json 序列化(协程对象在这一步炸)
    json.dumps(body)


def test_the_response_contains_no_coroutine_repr_anywhere(world):
    """🔁 收口:整个响应体里不许出现协程的字符串痕迹。

    有些实现会"顺手 str() 一下"把协程变成 `<coroutine object ...>` 塞进去 ——
    那样 200 也拿得到,但用户看到的是一串垃圾。上一条的 `== brand_name`
    已经拦住了这一格,这一条把它扩到**整个响应体**。
    """
    with _client(world) as client:
        got = client.get(_url(world))
    assert got.status_code == 200, got.text[:400]
    assert "coroutine" not in got.text, got.text[:400]


# ══════════════════════════════════════════════════════════════════════════
# 反向对照:报价不存在 404 / 无权限拒绝 —— 照旧
# ══════════════════════════════════════════════════════════════════════════

def test_missing_quote_is_still_404(world):
    with _client(world) as client:
        got = client.get(_url(world, quote_id=987654321))
    assert got.status_code == 404, got.text[:300]
    # 与"无权限"那条 404 用 detail 区分开(见下一条的说明)
    assert "报价不存在" in got.text, got.text[:300]


def test_a_user_without_access_to_the_brand_is_still_refused(world):
    """🔴 非 admin 且既不是 owner、也没被分配这个 brand → 拒绝(不是 200)。

    这条守的是「修 500 的时候没顺手把授权闸弄松」。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.geo_douyin_api as mod

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = {
            "user_id": OWNER_UID + 999, "id": OWNER_UID + 999, "username": "outsider",
            "is_admin": False, "client_brand_ids": [], "permissions": ["writing:read"],
        }
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(mod.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        got = client.get(_url(world))
    # 🔴 拒绝的形态是 **404 + "资源不存在"**,不是 403 —— `require_brand_access`
    #    刻意不回显存在性(403 会告诉外人"这张报价是存在的")。
    #    所以这里不能只断状态码:404 与"报价真的不存在"同码,必须按 **detail 区分**,
    #    否则一个"把报价删了"的实现也能让这条判据变绿。
    assert got.status_code in (401, 403, 404), (got.status_code, got.text[:300])
    if got.status_code == 404:
        assert "资源不存在" in got.text, got.text[:300]
        assert "报价不存在" not in got.text, got.text[:300]


def test_a_quote_without_a_brand_is_still_409(world):
    """🔁 边界对照:报价没关联客户那一支照旧 409(不是被这次改动带成 500)。"""
    with _client(world) as client:
        got = client.get(_url(world, quote_id=world["orphan_quote_id"]))
    assert got.status_code == 409, (got.status_code, got.text[:300])


def test_an_unsupported_channel_is_still_400(world):
    with _client(world) as client:
        got = client.get(_url(world) + "?channel=not_a_channel")
    assert got.status_code == 400, (got.status_code, got.text[:300])
