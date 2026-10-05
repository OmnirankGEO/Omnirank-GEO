"""#150 §3.3 · 一次提交 N 条:幂等 / 逐条独立 / 失败即停。

## 三条语义各一个失败面

  · **幂等**:同一个 `request_id` 重放 ⇒ 原样返回上次结果,**不重复建单**;
  · **失败即停**:某条失败 ⇒ 该条与其后各条不建,**前面已建的照跑**;
  · **同一份实现**:批量走 `_create_and_dispatch_one`,四道闸不许写第二份 ——
    漂开的表现是「单条挡住了、批量放过去了」,两边各自看都正常。

🔴 幂等打**真库**:`ON CONFLICT DO NOTHING` 的裁决权在数据库,
   桩掉就等于没验(这是我在 §3.2 连栽两版的那个病)。
"""

from __future__ import annotations

import ast
import io
import os
import pathlib
import uuid
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = [ROOT / "db" / "migration_056_geo_douyin_post_batches_2026_09_08.sql"]
DSN = os.getenv("TEST_DATABASE_URL")

USER_A, USER_B = 70001, 70002


# ══════════════════════════════════ 幂等:打真库

@pytest.fixture(scope="module")
def batch_db():
    if not DSN:
        pytest.skip("需要 TEST_DATABASE_URL(缺则本组未验证)")
    name = "gdq_batch_%s" % uuid.uuid4().hex[:8]
    root = DSN.rsplit("/", 1)[0]
    admin = psycopg2.connect(root + "/postgres")
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "%s"' % name)
    admin.close()
    dsn = root + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    for m in MIGRATIONS:
        conn.cursor().execute(m.read_text(encoding="utf-8"))
    conn.close()
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(root + "/postgres")
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        admin.close()


@pytest.fixture
def wired_batch(batch_db, monkeypatch):
    """桩只打到连接 —— `ON CONFLICT` 那句 SQL 原样跑。"""
    import db.connection as dbc
    import db.geo_douyin_db as gddb

    def _conn(*a, **k):
        return psycopg2.connect(batch_db, cursor_factory=RealDictCursor)

    # 🔴 两处都要打:geo_douyin_db 在模块顶层绑了自己的引用
    #    (只打 db.connection 时「单跑绿、合跑红」——§3.2 栽过)。
    monkeypatch.setattr(dbc, "get_connection", _conn)
    monkeypatch.setattr(gddb, "get_connection", _conn, raising=False)
    return gddb


def test_the_first_claim_wins_and_the_replay_sees_the_stored_result(wired_batch):
    """🔴 幂等那一格:第一次认领返回 None(抢到),重放拿到既有行。"""
    gddb = wired_batch
    rid = "req-%s" % uuid.uuid4().hex[:10]

    assert gddb.claim_post_batch(created_by=USER_A, request_id=rid) is None, "第一次该抢到"
    again = gddb.claim_post_batch(created_by=USER_A, request_id=rid)
    assert again is not None, "第二次仍然抢到了 —— 双击会重复下单"
    assert again["status"] == "in_progress"

    gddb.finish_post_batch(created_by=USER_A, request_id=rid,
                           result=[{"index": 0, "post_id": 11}])
    replay = gddb.claim_post_batch(created_by=USER_A, request_id=rid)
    assert replay["status"] == "done"
    assert replay["result"] == [{"index": 0, "post_id": 11}], (
        "重放没拿回上次的逐条结果 —— 幂等不是「第二次返空」")


def test_the_same_request_id_from_another_user_is_a_different_batch(wired_batch):
    """🔴 `request_id` 由前端生成,必须**按人分域**。

    毒:主键改成单 `request_id` ⇒ A 的重放会读到 B 的结果 ——
    那是跨租户泄漏,不是幂等。
    """
    gddb = wired_batch
    rid = "shared-%s" % uuid.uuid4().hex[:8]
    assert gddb.claim_post_batch(created_by=USER_A, request_id=rid) is None
    assert gddb.claim_post_batch(created_by=USER_B, request_id=rid) is None, (
        "B 用同一个 request_id 被当成了 A 的重放 —— 跨租户")


def test_the_claim_is_decided_by_the_database_not_by_a_prior_select():
    """结构臂:认领必须靠 `ON CONFLICT DO NOTHING`,不许"先查再插"。

    先查再插在并发双击下**两次都查不到、两次都插** —— 而顺利路径上
    两种写法读数完全相同,行为臂分不出来。
    """
    src = io.open(ROOT / "db" / "geo_douyin_db.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "claim_post_batch")
    body = ast.unparse(fn)
    assert "ON CONFLICT" in body.upper(), "认领没走唯一插入 —— 并发双击会重复下单"


# ══════════════════════════════════ 批量语义:打真出口

def _client(monkeypatch, *, outcomes, seen):
    """真 router。`_create_and_dispatch_one` 按 `outcomes` 逐条成功/抛。"""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    import api.geo_douyin_api as gapi
    import db.geo_douyin_db as ddb

    calls = {"n": 0}

    async def _one(item, request):
        i = calls["n"]
        calls["n"] += 1
        out = outcomes[i] if i < len(outcomes) else "ok"
        if out != "ok":
            raise HTTPException(status_code=400,
                                detail={"code": out, "message": "造的失败"})
        seen["created"].append(item.keyword)
        return {"status": "accepted", "post_id": 1000 + i}

    monkeypatch.setattr(gapi, "_create_and_dispatch_one", _one)
    monkeypatch.setattr(gapi, "is_pipeline_enabled", lambda: True)
    monkeypatch.setattr(ddb, "claim_post_batch", lambda **k: None)
    monkeypatch.setattr(ddb, "finish_post_batch",
                        lambda **k: seen.__setitem__("finished", k["result"]))

    async def _quote(items):
        return {"total_points": 390, "lines": [], "price_fingerprint": "x", "rule": {}}

    import services.geo_douyin.production_quote as pq
    monkeypatch.setattr(pq, "quote_production", _quote)

    import db.wallet_db as wdb
    monkeypatch.setattr(wdb, "get_wallet_balance",
                        lambda uid: {"paid_points": 10_000, "bonus_points": 0})

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):        # noqa: ANN001
        request.state.user = {"user_id": USER_A, "username": "u", "is_admin": True}
        return await call_next(request)

    app.post("/posts/batch")(gapi.api_create_batch)
    return TestClient(app, raise_server_exceptions=False)


def _body(n=3):
    return {"request_id": "rid-%s" % uuid.uuid4().hex[:10],
            "items": [{"keyword": "词%d" % i, "brand_id": 9, "card_count": 4}
                      for i in range(n)]}


def test_a_failure_stops_the_rest_but_keeps_what_already_ran(monkeypatch):
    """🔴 本单那一格:第 2 条失败 ⇒ 第 3 条不建,**第 1 条照跑**。

    毒:失败后继续循环 ⇒ 第 3 条会被建出来 ⇒ 本条红。
    """
    seen = {"created": [], "finished": None}
    resp = _client(monkeypatch, outcomes=["ok", "INSUFFICIENT_BALANCE", "ok"],
                   seen=seen).post("/posts/batch", json=_body(3))
    assert resp.status_code == 200, resp.text[:300]
    items = resp.json()["items"]
    assert items[0]["post_id"] == 1000
    assert items[1]["post_id"] is None and items[1]["code"] == "INSUFFICIENT_BALANCE"
    assert items[2]["post_id"] is None, "失败之后仍然建了下一条"
    assert items[2]["code"] == "SKIPPED_AFTER_FAILURE"
    # 🔴 前面已建的**不回滚** —— 那是用户已经要到的东西
    assert seen["created"] == ["词0"], seen["created"]
    assert resp.json()["created"] == 1
    assert resp.json()["stopped_at"] == 1


def test_all_success_creates_every_item(monkeypatch):
    """正样本臂:全成功 ⇒ 每条都建。

    只证「失败会停」不够 —— 一个永远只建第一条的实现同样能让上一条变绿。
    """
    seen = {"created": [], "finished": None}
    resp = _client(monkeypatch, outcomes=["ok"] * 3, seen=seen).post(
        "/posts/batch", json=_body(3))
    body = resp.json()
    assert [i["post_id"] for i in body["items"]] == [1000, 1001, 1002]
    assert body["created"] == 3 and body["stopped_at"] is None
    assert seen["created"] == ["词0", "词1", "词2"]


def test_the_result_is_written_back_for_replay(monkeypatch):
    """逐条结果要落台账 —— 否则重放拿不回同一个答案。"""
    seen = {"created": [], "finished": None}
    _client(monkeypatch, outcomes=["ok", "ok"], seen=seen).post(
        "/posts/batch", json=_body(2))
    assert seen["finished"] and len(seen["finished"]) == 2


def test_replaying_the_same_request_id_creates_nothing_twice(batch_db, monkeypatch):
    """🔴🔴 端到端幂等:同一个 `request_id` 连发两次 ⇒ 第二次原样返回,**不重建**。

    ## 为什么这条必须存在

    上一版我把 `claim_post_batch` 打成 `lambda **k: None` —— 于是端点里那句
    `if existing is not None: return ...` **从没被跑过**:
    把它改成 `if False:`(重放不返回、继续处理)⇒ **57 条全绿**。
    此时同一个 request_id 双击会把整批**再建再扣一遍**。

    🔴 helper 打了真库、端点打了桩,**唯独两者的接缝没人验** ——
       这是我今天第四次在同一个形状上栽:把要验的东西桩掉,然后去锁它旁边那段。

    所以这里**不桩** `claim_post_batch` / `finish_post_batch`,只桩连接。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.geo_douyin_api as gapi
    import db.connection as dbc
    import db.geo_douyin_db as ddb
    import db.wallet_db as wdb
    import services.geo_douyin.production_quote as pq

    def _conn(*a, **k):
        return psycopg2.connect(batch_db, cursor_factory=RealDictCursor)

    # 两处都要打(§3.2 教训:geo_douyin_db 顶层绑了自己的引用)
    monkeypatch.setattr(dbc, "get_connection", _conn)
    monkeypatch.setattr(ddb, "get_connection", _conn, raising=False)

    calls = {"n": 0}

    async def _one(item, request):
        calls["n"] += 1
        return {"status": "accepted", "post_id": 5000 + calls["n"]}

    async def _quote(items):
        return {"total_points": 390, "lines": [], "price_fingerprint": "x", "rule": {}}

    monkeypatch.setattr(gapi, "_create_and_dispatch_one", _one)
    monkeypatch.setattr(gapi, "is_pipeline_enabled", lambda: True)
    monkeypatch.setattr(pq, "quote_production", _quote)
    monkeypatch.setattr(wdb, "get_wallet_balance",
                        lambda uid: {"paid_points": 10_000, "bonus_points": 0})

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):        # noqa: ANN001
        request.state.user = {"user_id": USER_A, "username": "u", "is_admin": True}
        return await call_next(request)

    app.post("/posts/batch")(gapi.api_create_batch)
    client = TestClient(app, raise_server_exceptions=False)

    body = {"request_id": "dbl-%s" % uuid.uuid4().hex[:10],
            "items": [{"keyword": "词A", "brand_id": 9, "card_count": 4},
                      {"keyword": "词B", "brand_id": 9, "card_count": 4}]}

    first = client.post("/posts/batch", json=body).json()
    assert first["idempotent_replay"] is False
    assert [i["post_id"] for i in first["items"]] == [5001, 5002]
    assert calls["n"] == 2

    second = client.post("/posts/batch", json=body).json()
    assert second["idempotent_replay"] is True, (
        "第二次没被认成重放 —— 双击会把整批再建再扣一遍")
    assert calls["n"] == 2, "重放又建了一遍:create 调用数 %d" % calls["n"]
    assert second["items"] == first["items"], (
        "重放没返回上次的逐条结果 —— 幂等不是「第二次返空」:%r" % second["items"])


def test_batch_reuses_the_single_item_implementation():
    """🔴 结构臂:批量必须调 `_create_and_dispatch_one`,不许自己写第二份校验。

    毒:把品牌/指纹/归属那几道闸在批量里重写一遍 ⇒
    本条红(它会看不到那个调用)。四道闸写两份就一定会漂,
    而漂开的表现是「单条挡住了、批量放过去了」。
    """
    src = io.open(ROOT / "api" / "geo_douyin_api.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "api_create_batch")
    body = ast.unparse(fn)
    assert "_create_and_dispatch_one" in body, "批量没走共用那份实现"
    for leaked in ("BRAND_NAME_MISSING", "PRICE_FINGERPRINT_MISMATCH",
                   "CONFIRMED_KEYWORD_NOT_FOR_BRAND"):
        assert leaked not in body, (
            "批量里重写了单条那道闸(%s)—— 两份实现一定会漂" % leaked)


def test_the_batch_endpoint_does_not_freeze_points():
    """🔴 批量**不冻结** —— 冻结仍在后台(工单 §3:freeze/commit 路径不动)。

    余额只做只读预检。写成"预检通过就一定能扣"是假承诺,
    而把冻结挪进来就是改资金路径。
    """
    src = io.open(ROOT / "api" / "geo_douyin_api.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "api_create_batch")
    body = ast.unparse(fn)
    for money in ("freeze_points", "commit_freeze", "release_freeze"):
        assert money not in body, "批量端点碰了资金路径:%s" % money
