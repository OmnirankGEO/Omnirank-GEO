"""#150 §3.2 返修 · 归属守卫的**行为臂**。

## 上一版的洞

我只写了**结构臂**(校验调用排在建行之前)。Review 把
`if quote_id_for_post is None: raise 400` 改成**静默置 None** ⇒ **11 条全绿**。

那一改的后果不是"少一道校验":别人客户的 `confirmed_keyword_id` 会**原样写进**
本客户的成品行(`quote_id` 为 NULL、`confirmed_keyword_id` 串户),
记账跨租户污染 —— 而屏幕上一切正常。

🔴 结构臂只钉「校验在哪一行发生」,钉不住「拿到 None 之后做了什么」。
   位置对了、处置没了,读数完全相同。所以必须有行为臂打**真出口**。
"""

from __future__ import annotations

import pytest


def _client(monkeypatch, *, owned, seen):
    """真 router,把外部依赖换成桩;`seen` 记录有没有落库/派发。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.geo_douyin_api as gapi
    import db.geo_douyin_db as ddb
    import services.geo_douyin.topic_distiller as td

    async def _resolve(ck_id, *, brand_id):
        # owned=None ⇒ 这个词不属于该客户(或不存在)
        return owned

    async def _brand_name(_bid):
        return "测试客户"

    def _create_post(**kw):
        seen["create"] += 1
        seen["kwargs"] = kw
        return 4242

    monkeypatch.setattr(td, "resolve_quote_for_confirmed_keyword", _resolve)
    monkeypatch.setattr(gapi, "fetch_brand_display_name", _brand_name)
    monkeypatch.setattr(gapi, "require_brand_access", lambda *a, **k: None)
    monkeypatch.setattr(gapi, "is_pipeline_enabled", lambda: True)
    monkeypatch.setattr(ddb, "create_post", _create_post)
    # 🔴 `dispatch_production` 是端点里的**函数内局部导入**,不是 api 模块的属性 ——
    #    打在 gapi 上会 AttributeError(我第一版就是这么写的)。
    #    必须打在它的**源模块**上,局部 import 才会拿到桩。
    import services.geo_douyin.production_task as ptask
    monkeypatch.setattr(ptask, "dispatch_production",
                        lambda *a, **k: seen.__setitem__("dispatch",
                                                         seen["dispatch"] + 1))

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):        # noqa: ANN001
        request.state.user = {"user_id": 7, "username": "u", "is_admin": True}
        return await call_next(request)

    app.post("/posts")(gapi.api_create_and_produce)
    return TestClient(app, raise_server_exceptions=False)


def _body(**over):
    b = {"keyword": "深圳 GEO 优化", "brand_id": 9, "card_count": 4,
         "confirmed_keyword_id": 101}
    b.update(over)
    return b


def test_a_keyword_not_owned_by_this_brand_is_refused_with_zero_writes(monkeypatch):
    """🔴 本单那一格:词不属于该客户 ⇒ **400 且 posts 零新增**。

    毒:把 `raise` 静默成置 None ⇒ 本条红(会返 200 且落一行串户的账)。
    """
    seen = {"create": 0, "dispatch": 0}
    resp = _client(monkeypatch, owned=None, seen=seen).post("/posts", json=_body())

    assert resp.status_code == 400, resp.text[:300]
    assert resp.json()["detail"]["code"] == "CONFIRMED_KEYWORD_NOT_FOR_BRAND"
    assert seen["create"] == 0, "被拒了却仍建了成品行 —— 串户的账已经落库"
    assert seen["dispatch"] == 0, "被拒了却仍派发了生产 —— 算力会被冻"


def test_an_owned_keyword_is_accepted_and_carries_the_derived_quote(monkeypatch):
    """🔴 正样本臂:属于该客户 ⇒ 放行,且 `quote_id` 是**服务端派生**的那个。

    只证「不属于就拒」不够 —— 一个把所有请求都拒掉的实现同样能让上一条变绿,
    而那会让谁都下不了单。
    """
    seen = {"create": 0, "dispatch": 0}
    resp = _client(monkeypatch, owned=777, seen=seen).post("/posts", json=_body())

    assert resp.status_code == 200, resp.text[:300]
    assert seen["create"] == 1
    assert seen["kwargs"]["quote_id"] == 777, seen["kwargs"]
    assert seen["kwargs"]["confirmed_keyword_id"] == 101


def test_a_manual_keyword_without_identity_still_works(monkeypatch):
    """手填词(不传词身份)⇒ 照常下单,两列留 NULL。

    NULL 是诚实的「未知」,不是缺陷;老前端零改动也走这条路。
    """
    seen = {"create": 0, "dispatch": 0}
    resp = _client(monkeypatch, owned=None, seen=seen).post(
        "/posts", json=_body(confirmed_keyword_id=None))

    assert resp.status_code == 200, resp.text[:300]
    assert seen["create"] == 1
    assert seen["kwargs"]["quote_id"] is None
    assert seen["kwargs"]["confirmed_keyword_id"] is None
