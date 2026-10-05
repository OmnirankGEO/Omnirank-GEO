"""#30 · `TypedErrorRoute` 漏接 starlette 基类的 HTTPException
(工单 post-train #30 · Review 2026-09-04 裁定「降为查证 + 加固」)。

## 🔴 工单原描述的缺陷**不存在**

原文:「`typed_error_route.py:64` 把 400 包成 500 INTERNAL_ERROR/retryable=true,修法:4xx 透传」。
实读:`:64` 是 `return await original(request)`(try 的函数体),而 `:65 except HTTPException: raise`
—— **4xx 本来就透传**。台账里也没有这条的原始读数(响应体/日志行/复现请求都没有)。
⇒ 前提不成立,不修想象中的缺陷。

## 但确实有一个漏,而且很隐蔽

```
from fastapi import HTTPException            # 用的是 fastapi 的
issubclass(fastapi.HTTPException, starlette.HTTPException)      == True
isinstance(starlette.HTTPException(400), fastapi.HTTPException) == False   ← 接不住
```
**父类实例不是子类实例。** 谁 raise starlette 那个基类,`except HTTPException`(子类)漏掉它,
掉进 `except Exception` ⇒ 被包成 **500 + INTERNAL_ERROR**,而它本来是个 4xx。

本仓目前没有代码直接 raise 基类(已 grep `api/ services/ auth/ middleware/`),
但**第三方中间件/依赖可能有**,而且这条一旦发生,症状与工单描述的一模一样 ——
这大概率就是那条工单的来处:**现象猜对了,坐标和机制都错了**。

## 判据口径

三条运行时判据打**真 ASGI 往返**(TestClient),不读源码串:
  1. 基类 400 —— 修前 500/INTERNAL_ERROR(红臂),修后 400 且信封非 INTERNAL_ERROR;
  2. 子类 403 —— 修前修后**逐字不变**(反臂:证明我没有顺手改掉正常透传);
  3. 真的未预期异常 —— 修前修后都必须仍是 500/INTERNAL_ERROR(反臂:证明网没撒太大)。

🔴 第 2、3 条在修前修后都绿,这是**故意**的 —— 它们的作用不是变绿,
   是在第 1 条变绿时证明「不是靠把所有异常都放行换来的」。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi import HTTPException as FastAPIHTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.defensive_geo.typed_error_route import TypedErrorRoute


def _client() -> TestClient:
    """一个只装了三条路由的最小应用,route_class = 被测对象。"""
    app = FastAPI()
    app.router.route_class = TypedErrorRoute

    @app.get("/raise-starlette-base")
    def _base():
        # 🔴 关键:raise 的是**基类**,不是 fastapi 那个子类。
        raise StarletteHTTPException(status_code=400, detail="来自基类的 400")

    @app.get("/raise-fastapi-subclass")
    def _sub():
        raise FastAPIHTTPException(status_code=403, detail={"code": "FORBIDDEN"})

    @app.get("/raise-unexpected")
    def _boom():
        raise ValueError("这是真的未预期异常")

    # raise_server_exceptions=False:让异常走完真实的 ASGI 错误路径,
    # 而不是被 TestClient 直接抛回测试进程 —— 否则测的是测试客户端,不是被测对象。
    return TestClient(app, raise_server_exceptions=False)


def test_subclass_asymmetry_is_real():
    """先把这条判据赖以成立的前提钉住:父类实例不是子类实例。

    **红了说明什么**:fastapi 改了异常继承关系 ⇒ 下面三条的推理前提没了,
    整个包要重写,而不是"顺手放宽"。
    """
    assert issubclass(FastAPIHTTPException, StarletteHTTPException)
    assert not isinstance(
        StarletteHTTPException(status_code=400, detail="x"), FastAPIHTTPException)


def test_starlette_base_400_is_not_swallowed_into_500():
    """主判据:基类 400 必须仍是 4xx,不许被包成 500 INTERNAL_ERROR。

    **红了说明什么**:`except` 子句只接住了 fastapi 子类 ⇒ 基类掉进 `except Exception`,
    一个 4xx 被返成 500 + retryable ⇒ 客户端会去重试一个**永远不会成功**的请求。
    """
    r = _client().get("/raise-starlette-base")
    body = r.json()
    assert r.status_code == 400, (
        f"基类 HTTPException(400) 被返成 {r.status_code};响应体={body}")
    detail = body.get("detail")
    code = detail.get("code") if isinstance(detail, dict) else None
    assert code != "INTERNAL_ERROR", (
        f"状态码对了但信封仍是 INTERNAL_ERROR ⇒ 只改了状态码没改分支;detail={detail}")


def test_fastapi_subclass_still_passes_through_unchanged():
    """反臂:子类的 403 透传口径**一个字不变**。

    **红了说明什么**:我为了接住基类,把原来"raise 不动"那条路也改掉了 ——
    正常的 typed 信封会被二次包装,前端拿到的 code/nextAction 就变了。
    """
    r = _client().get("/raise-fastapi-subclass")
    assert r.status_code == 403
    assert r.json().get("detail") == {"code": "FORBIDDEN"}


def test_unexpected_exception_still_becomes_typed_500():
    """反臂:真的未预期异常仍必须落 500 + INTERNAL_ERROR。

    **红了说明什么**:网撒太大 —— 把本该是 500 的东西也放行了,
    客户端会读到裸 500 文本(`Internal Server Error`),而那正是这个 route class 当初要消灭的。
    """
    r = _client().get("/raise-unexpected")
    assert r.status_code == 500
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("code") == "INTERNAL_ERROR", (
        f"未预期异常没有落 typed 信封;detail={detail}")


@pytest.mark.parametrize("path,expected", [
    ("/raise-starlette-base", 400),
    ("/raise-fastapi-subclass", 403),
    ("/raise-unexpected", 500),
])
def test_three_paths_are_three_distinct_outcomes(path, expected):
    """三条路必须给出**三种不同**的结果。

    **红了说明什么**:如果三条都返同一个码,说明上面任何一条"绿"都可能是
    "所有请求都长一样"造成的假象 —— 这条是防止整组判据一起失去区分力。
    """
    assert _client().get(path).status_code == expected
