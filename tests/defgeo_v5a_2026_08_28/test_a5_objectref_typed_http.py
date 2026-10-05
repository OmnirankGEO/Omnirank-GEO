"""【A-5 = Codex fix-of-fix2 P2-2】``objectRef.id`` 非法值变 500。

Codex 原文
----------
    ``object_ref`` 只是 ``dict[str, Any]``,validator 不校验 ``id``;
    ``int("not-an-integer")`` 抛未捕获 ``ValueError``。应改成 discriminated
    typed model,或在 validator 内完成转换并返回 422 / typed identity error。

我的证伪结果:坐实。底 ``5f5884893`` 上 ``ReissueRequest._object_ref_shape``
只检查「是 dict」「有 kind」「字段数 ≤ 4」——``id`` 是什么完全不看。
``{"kind":"quote","id":"abc"}`` 一路走到 ``reissue_link`` 的
``int(expected_object_ref["id"])``,``ValueError`` 由 route class 兜成
typed **500 INTERNAL_ERROR**。

500 的含义是「我们这边坏了,稍后再试」。而这是**她这次请求**的问题,
再试一百次都一样。类型不合法就在入口说清楚(422),别冒充我们的故障。

🔴 ``extra="forbid"`` 只加在**请求**模型上 —— 附一条我自己的证伪
--------------------------------------------------------------
工单提醒了本仓「response_model forbid 三炸」史。我据此先写了一条
「响应模型不许 forbid」的判据,**跑出来是红的**:本包的 ``_Envelope``
从 G-4 起就故意给响应也上了 forbid(逐字:「多返回一键 = 受控失败,不是裸 500」),
并配了 DTO 出口锁。那是存量的、有理由的选择,错的是我的判据。

所以本单的真实边界是:**只在请求侧新加 forbid**;响应侧那把不动,
而 test_06 钉的是「我确实没去动它」。

本文件不连库:FastAPI 的入参校验发生在 handler 之前,422 那几条根本走不到 DB。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from api import defensive_geo_assist_api as assist

    app = FastAPI()
    app.include_router(assist.router)

    @app.middleware("http")
    async def _inject_user(request: Request, call_next):   # noqa: ANN001
        request.state.user = {"user_id": 1, "username": "v5a", "is_admin": False}
        return await call_next(request)

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _post(client, object_ref, *, kind="customer_portal", brand_id=1):
    body = {"brandId": brand_id, "kind": kind}
    if object_ref is not _MISSING:
        body["objectRef"] = object_ref
    return client.post(REISSUE_PATH, json=body)


_MISSING = object()

#: 真实挂载路径(router 带 ``prefix="/api/defensive-geo"``)。
#: 写错路径的话每一条 422 断言都会因为 404 而"看起来对" —— test_00 先钉住这件事。
REISSUE_PATH = "/api/defensive-geo/customer-links/reissue"


# ══════════════════════════════════════════════════════════════════════════
# 00 · 路由活性 —— 先证明这条路由真的挂上了
# ══════════════════════════════════════════════════════════════════════════
def test_00_route_is_mounted(client) -> None:
    """零分母防线:如果路由没挂,下面每一条 422 断言都会因为 404 而"看起来对"。"""
    r = client.post(REISSUE_PATH, json={})
    assert r.status_code != 404, "路由没挂上 —— 后面的断言全是假绿"
    assert r.status_code == 422, "空 body 应当是入参校验未过,实得 %d" % r.status_code


# ══════════════════════════════════════════════════════════════════════════
# 01-03 · 工单点名的三例
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("object_ref,why", [
    ({"kind": "quote", "id": "not-an-integer"}, "id 不是整数"),
    ({"kind": "quote"},                          "缺 id"),
    ({"kind": "not_a_kind", "id": 1},            "未知 kind"),
])
def test_01_illegal_object_ref_is_422_not_500(client, object_ref, why) -> None:
    r = _post(client, object_ref)
    assert r.status_code == 422, (
        "%s ⇒ 期望 422(她这次请求的问题),实得 %d。"
        "500 的意思是「我们这边坏了,稍后再试」,而再试一百次都一样。"
        % (why, r.status_code))
    assert r.status_code != 500


def test_02_response_body_is_typed_not_bare(client) -> None:
    """422 的响应体必须是 typed 信封,不是 FastAPI 的 ``{detail:[…]}`` 原始形状。

    (``TypedErrorRoute`` 早就把 ``RequestValidationError`` 翻成
    ``VALIDATION_FAILED`` 了 —— 这条是**存量保护**:本单把 objectRef 收成
    typed model 之后,不许因此走回裸 detail 数组。)
    """
    r = _post(client, {"kind": "quote", "id": "not-an-integer"})
    body = r.json()
    assert isinstance(body, dict) and isinstance(body.get("detail"), dict), \
        "422 的 body 不是 typed 信封:%r" % body
    assert body["detail"].get("code"), "typed 信封里没有 code:%r" % body


def test_03_extra_field_in_object_ref_is_rejected(client) -> None:
    """``extra="forbid"``:只回传面板给的那几格。多出来的字段说明她回传的
    不是面板下发的那个对象引用,而是自己拼的 —— 那正是本条约束要挡的。"""
    r = _post(client, {"kind": "quote", "id": 1, "smuggled": "x"})
    assert r.status_code == 422, "objectRef 多带字段应当 422,实得 %d" % r.status_code


def test_04_missing_object_ref_is_still_required(client) -> None:
    """V3-A 定下的「必传」不许回退成可选。"""
    r = _post(client, _MISSING)
    assert r.status_code == 422, "objectRef 变成可选了 —— V3-A 的约束被削掉了"


# ══════════════════════════════════════════════════════════════════════════
# 05 · 配对的必须不命中 —— 合法 objectRef 必须**通过**校验
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("ok_ref", [
    {"kind": "quote", "id": 7},
    {"kind": "quote", "id": "7"},           # pydantic 非严格模式:数字串照收
    {"kind": "keyword_selection_session", "token": "tok-abc"},
])
def test_05_legal_object_ref_passes_validation(ok_ref) -> None:
    """直接打模型,不打 HTTP —— 合法 body 走到 handler 里会因为没有库而失败,
    那种失败证明不了"校验通过了"。打模型才是干净的正样本。"""
    from api.defensive_geo_assist_api import ReissueRequest

    m = ReissueRequest.model_validate(
        {"brandId": 1, "kind": "customer_portal", "objectRef": ok_ref})
    dumped = m.object_ref.model_dump()
    assert dumped["kind"] == ok_ref["kind"]
    if ok_ref["kind"] == "quote":
        assert dumped["id"] == 7 and isinstance(dumped["id"], int), \
            "合法 id 没有被规范成 int:%r" % dumped


def test_06_forbid_is_on_the_request_side_only() -> None:
    """本仓 response_model forbid **三炸**史 —— 这条把边界钉住。

    请求侧模型 forbid(挡住多塞的字段);响应侧模型**不许** forbid
    (出口一 forbid,任何多带一格的响应就整个 500)。
    """
    from api import defensive_geo_assist_api as assist

    req_models = [assist.ReissueRequest, assist._QuoteObjectRef,
                  assist._SelectionSessionObjectRef]
    for m in req_models:
        assert m.model_config.get("extra") == "forbid", \
            "%s 是请求模型,应当 forbid" % m.__name__

    # 🔴 证伪记录:我起初写的是「响应模型**不许** forbid」,跑出来是红的 ——
    #    本包的 `_Envelope` 从 G-4 起就**故意**给响应也上了 forbid
    #    (逐字:「多返回一键 = 受控失败,不是裸 500」),并配了 DTO 出口锁。
    #    错的是我的判据不是代码,所以改判据:本单只在**请求**侧加 forbid,
    #    响应侧那把是存量,本条钉的是「我没去动它」。
    from api.defensive_geo_assist_api import _Envelope

    assert _Envelope.model_config.get("extra") == "forbid", \
        "_Envelope 的响应侧 forbid 是 G-4 存量约定,本单不许改动它"
    assert issubclass(assist.ReissueResponse, _Envelope), \
        "ReissueResponse 不再继承 _Envelope —— 出口约定被绕过了"


def test_07_object_ref_is_a_discriminated_union() -> None:
    """形态锁:两类各自带自己那一格身份字段,``kind`` 是判别式。

    钉住"是个判别式 union"而不是"能挡住那三个坏例子":后者可以被一个
    宽松的 validator 假装满足,而判别式 union 让**未知 kind** 在入口就是 422,
    不是走到某个分支里才崩。
    """
    from api.defensive_geo_assist_api import _QuoteObjectRef, _SelectionSessionObjectRef

    assert set(_QuoteObjectRef.model_fields) == {"kind", "id"}
    assert set(_SelectionSessionObjectRef.model_fields) == {"kind", "token"}
    q = _QuoteObjectRef.model_validate({"kind": "quote", "id": 3})
    assert q.id == 3
    with pytest.raises(Exception):
        _QuoteObjectRef.model_validate({"kind": "quote", "id": 0})   # ge=1
    with pytest.raises(Exception):
        _SelectionSessionObjectRef.model_validate(
            {"kind": "keyword_selection_session", "token": ""})       # min_length=1
