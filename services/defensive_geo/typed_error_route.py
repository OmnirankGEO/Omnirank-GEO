"""防御型 GEO 的**统一错误出口**(P1-5 返修③)。

被测缺陷:同一个 router 家族里并存三种信封
------------------------------------------
门四实证,同一批端点能返回三种形状:

  ① full typed   ``{code, retryable, publicExplanation, nextAction}``  —— ``_safe_error``
  ② minimal typed``{code, retryable}``                                 —— 手写的 HTTPException
  ③ framework bare ``Internal Server Error``(裸文本)/ ``{detail:[…]}`` —— 谁都没接住的异常

②③ 对前端是两种**没法渲染**的东西:``publicExplanation`` 没有 ⇒ 弹窗空白;
``nextAction`` 没有 ⇒ 她读完不知道该干什么。§0.5.6 逐字:任何阻塞必须自带解决方案。

为什么做成 route class,不是在每个 handler 里包 try
--------------------------------------------------
每个 handler 各包一次 = 同一个谓词写十几遍,必有一处漏(而漏掉的那一处不会有
任何判据变红 —— 判据只会打它跑到的那几条路)。route class 是**一处**,
且对**将来新加的端点**自动生效:新端点不需要记得做任何事就已经被接住。

三条边界(刻意不一刀切)
------------------------
· ``HTTPException`` 原样放行 —— 它已经是 ``_safe_error`` 造好的信封,
  再包一层只会把 404 变成 500。
· ``RequestValidationError``(入参 schema 不过)翻成 typed ``VALIDATION_FAILED``,
  **状态码仍是 422** —— 既有判据断言的是 422 这个码,行为不回归;变的只是
  响应体从 framework ``{detail:[…]}`` 变成她能读的那句话。
· 其余一切异常 ⇒ typed ``INTERNAL_ERROR``(500),并且**照常 logger.exception**。
  这里不是把错误藏起来:栈还在日志里,变的只是**客户看到的那一面**。
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
# 🔴 [#30 2026-09-04] 接 **starlette 的基类**,不接 fastapi 那个子类。
#    `fastapi.HTTPException` 是它的子类;**父类实例不是子类实例**:
#        isinstance(starlette.HTTPException(400), fastapi.HTTPException) == False
#    所以原来的 `except fastapi.HTTPException` 漏掉基类 ⇒ 一个 4xx 掉进
#    下面的 `except Exception`,被包成 **500 + retryable=true** ⇒
#    客户端会去重试一个永远不会成功的请求。
#    接基类**同时覆盖两者**(子类是它的实例),所以正常透传口径一个字不变。
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

logger = logging.getLogger("GEO-DefGeoTypedError")


def _envelope(code: str) -> tuple[int, dict]:
    """借 ``_safe_error`` 造信封 —— 信封形状只此一处定义。

    🔴 惰性 import:``api.defensive_geo_api`` 在模块顶层就会 import 本模块
       (它要拿 route_class),顶层反向 import 会成环。
    """
    from api.defensive_geo_api import _safe_error

    exc = _safe_error(code)
    return exc.status_code, exc.detail


class TypedErrorRoute(APIRoute):
    """把本包所有拒绝出口收敛成一种信封。"""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def typed_handler(request: Request) -> Response:
            try:
                return await original(request)
            except StarletteHTTPException:
                # 已经是 typed 信封(或 FastAPI 自己的 404/405)—— 不动。
                # 🔴 接的是**基类**:fastapi 的子类是它的实例,一并覆盖;
                #    而第三方中间件/依赖 raise 的基类以前会漏到 `except Exception`。
                raise
            except RequestValidationError as exc:
                status, detail = _envelope("VALIDATION_FAILED")
                logger.info("[defgeo] 入参校验未过 %s: %s", request.url.path, exc.errors()[:3])
                return JSONResponse(status_code=status, content={"detail": detail})
            except Exception:
                # 🔴 栈照常进日志。客户那一面不许出现裸 500 文本:
                #    她读到 "Internal Server Error" 只会反复重试,
                #    而 typed 信封里写的是「没有扣除任何算力,稍后再试一次」。
                logger.exception("[defgeo] 未预期异常 %s", request.url.path)
                status, detail = _envelope("INTERNAL_ERROR")
                return JSONResponse(status_code=status, content={"detail": detail})

        return typed_handler
