"""发布渠道闸门(开源版)。

没有接入发布渠道时(PUBLISH_CHANNEL 不设,见 services/publish_channels),凡是会走到渠道的入口,一律在进入路由之前返回同一个
``503 ADMISSION_UNAVAILABLE`` 和同一句话(出处:services/publish_channel_notice.py)—— 不扣算力、不建订单 / 任务行、
不调任何外部地址。``retryable`` 为 false:没接渠道,重试没用。HEAD 按 GET 算。
响应形状与防御型发布的 ``ADMISSION_UNAVAILABLE`` 相同:``{"detail": {"code", "retryable", "publicExplanation"}}``。

``GATED_ROUTES`` 由导出工具按「路由函数的调用闭包会不会到达渠道客户端」机械算出,
导出闸核它与导出树里的路由一致(多一个、少一个都红)。接入自己的渠道后,删掉这里对应的条目即可。
"""
from __future__ import annotations

import re

from starlette.responses import JSONResponse

from services.publish_channel_notice import CHANNEL_NOT_CONFIGURED as MESSAGE

CODE = "ADMISSION_UNAVAILABLE"
#: 没接渠道,重试没用
RETRYABLE = False

GATED_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/api/meijiehezi/publish"),
    ("POST", "/api/meijiehezi/publish/batch"),
    ("POST", "/api/meijiehezi/short-video/publish"),
    ("POST", "/api/meijiehezi/short-video/upload-policy"),
    ("POST", "/api/meijiehezi/short-video/upload-video"),
    ("POST", "/api/meijiehezi/short-video/upload-cover"),
    ("POST", "/api/meijiehezi/orders/withdraw"),
    ("POST", "/api/meijiehezi/admin/config/session"),
    ("POST", "/api/meijiehezi/admin/sync/media"),
    ("POST", "/api/meijiehezi/admin/sync/wemedia"),
    ("POST", "/api/meijiehezi/admin/sync/short-video"),
    ("POST", "/api/meijiehezi/admin/sync/orders"),
    ("POST", "/api/meijiehezi/admin/sync/status"),
    ("POST", "/api/meijiehezi/confirm/{item_id}"),
    ("GET", "/api/publish/admin/session-status"),
    ("POST", "/api/publish/admin/session"),
    ("POST", "/api/publish/admin/sync-media"),
    ("POST", "/api/publish/admin/sync-orders"),
)


def _pattern(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(path)) + "/?$")


_COMPILED = tuple((method, _pattern(path)) for method, path in GATED_ROUTES)


def is_gated(method: str, path: str) -> bool:
    # Starlette 给每个 GET 路由自动接 HEAD:GET 条目连 HEAD 一起拦
    method = "GET" if method == "HEAD" else method
    return any(method == m and rx.match(path) for m, rx in _COMPILED)


def unavailable_response() -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": {"code": CODE, "retryable": RETRYABLE,
                                                              "publicExplanation": MESSAGE}})


def install(app) -> None:
    @app.middleware("http")
    async def _publish_channel_gate(request, call_next):
        # 只在「没接入发布渠道」时拦;配了模拟发布或发布渠道 API 就放行到真路由(services/publish_channels)
        if is_gated(request.method, request.url.path):
            from services.publish_channels import configured_mode

            if configured_mode() is None:
                return unavailable_response()
        return await call_next(request)
