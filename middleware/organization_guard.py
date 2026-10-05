"""Per-request organization authority resolution and member route firewall."""

from __future__ import annotations

import logging
import re
import uuid
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from db.organization_db import assigned_brand_ids, resolve_identity
from services.organization_contract import OrganizationError, feature_flags
from services.organization_route_contract import match_member_geo_route


logger = logging.getLogger("GEO-OrganizationGuard")


# Member traffic is fail-closed. Every legacy endpoint remains owner-only until
# its reads, writes, async tasks, and billing path are explicitly organization-
# aware. This avoids treating a frontend-hidden button as authorization.
MEMBER_ALLOWED_NAMESPACES = (
    "/api/organization",
    "/api/auth",
    "/api/notifications",
    "/api/public/organization",
)

MEMBER_ALLOWED_EXACT = {
    "/api/auth/me",
    "/api/auth/profile",
    "/api/organization",
}


class OrganizationGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.organization_request_id = request_id
        # [修复 2026-07-28] 下面的兜底 `except Exception` 原本把 `call_next` 也包在里面,
        #   于是**任何端点抛的未处理异常**都会被改写成「组织权限校验暂不可用」(503 retryable)。
        #   代价有三:①用户看到的是权限错,实际是业务 bug,方向完全带偏;
        #   ②`retryable: True` 让前端/客户反复重试一个确定性失败;
        #   ③真异常只进日志不进响应,只读账号看不到日志时就无从下手。
        #   真实案例:用户名邀请重发撞投递表 CHECK 约束,现象却是"权限校验暂不可用"。
        #   现在只有**守卫自身**失败才 fail-closed 503;端点异常照常上抛给全局错误处理。
        downstream = {"entered": False}

        async def _forward():
            downstream["entered"] = True
            return await call_next(request)

        try:
            flags = feature_flags()
            if not flags["ORGANIZATION_SEATS_ENABLED"]:
                return await _forward()
            user = getattr(request.state, "user", None)
            # Public portal tokens intentionally use a non-user pseudo
            # principal. They have their own token/quote boundary and must not
            # be interpreted as organization members.
            if isinstance(user, dict) and user.get("portal") is True:
                return await _forward()
            user_id = (user or {}).get("user_id") or (user or {}).get("id")
            if not user_id:
                return await _forward()
            identity = resolve_identity(int(user_id), request_id=request_id)
            request.state.organization_identity = identity
            if identity is None:
                return await _forward()
            brands = assigned_brand_ids(identity)
            request.state.organization_brand_ids = brands
            if isinstance(user, dict):
                # Compatibility projection only. The DB assignment table stays
                # authoritative and is re-read on every request.
                user["client_brand_ids"] = brands
                user["organization_id"] = identity.organization_id
                user["organization_actor_kind"] = identity.actor_kind
                user["organization_authority_version"] = identity.authority_version
            if identity.is_member:
                path = request.url.path
                governance_allowed = path in MEMBER_ALLOWED_EXACT or any(
                    path == namespace or path.startswith(namespace + "/")
                    for namespace in MEMBER_ALLOWED_NAMESPACES
                )
                route_policy = None if governance_allowed else match_member_geo_route(request.method, path)
                if not governance_allowed and route_policy is None:
                    raise OrganizationError(
                        "ORG_ROUTE_NOT_CLASSIFIED",
                        "这个功能暂时不对员工开放，请让老板来操作",
                        http_status=403,
                        safe_details={"path": path},
                    )
                if route_policy is not None:
                    # [R3-P7 ④] 通用路由(一条路径通向多个 operation)走 any-of:
                    # 外层守卫只判「这条路径对这个身份是否可达」,精确授权由 handler
                    # 的 _authorize() 按该 operation 的 required_capability 再问一次。
                    # 🔴 不这么做的话必然出现奇偶不一致:能力发现按
                    # writing.generate 把 writing_center 下发给员工,而外层守卫要
                    # publish.plan → 403。两边单看都"对",用户看到的是功能不存在。
                    # 追加谓词:any_of 为空时**一个字节不变**地走原来的 require()。
                    if route_policy.capability_any_of:
                        identity.require_any(route_policy.capability_any_of)
                    else:
                        identity.require(route_policy.capability)
                    request.state.organization_route_policy = route_policy
            elif identity.is_owner and identity.organization_status != "active":
                if not request.url.path.startswith("/api/organization"):
                    raise OrganizationError(
                        "ORG_INACTIVE",
                        "团队已停用，当前仅团队负责人可处理审批、账目等善后事项",
                        http_status=403,
                    )
            response = await _forward()
            response.headers["X-Organization-Authority-Version"] = identity.authority_version
            response.headers["X-Request-ID"] = request_id
            return response
        except OrganizationError as exc:
            return JSONResponse(status_code=exc.http_status, content={"detail": exc.as_detail(request_id)}, headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})
        except Exception as exc:
            if downstream["entered"]:
                # 端点自己抛的 —— 不是权限问题,别改写成权限文案,交给全局错误处理。
                raise
            logger.exception("organization guard failed closed: %s", type(exc).__name__)
            return JSONResponse(
                status_code=503,
                content={"detail": {"code": "ORG_GUARD_FAILED", "message": "服务暂时不可用，请稍后重试", "request_id": request_id, "retryable": True}},
                headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
            )


class OrganizationWebSocketGuardMiddleware:
    """Fail closed for every unclassified WebSocket used by a seat member."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _protocol_token(scope) -> str | None:
        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers") or []
        }
        protocols = [
            value.strip()
            for value in headers.get("sec-websocket-protocol", "").split(",")
            if value.strip()
        ]
        if len(protocols) == 2 and protocols[0] == "omnirank-auth":
            return protocols[1]
        return None

    @classmethod
    def _token(cls, scope) -> str | None:
        protocol_token = cls._protocol_token(scope)
        if protocol_token:
            return protocol_token
        query = parse_qs((scope.get("query_string") or b"").decode("utf-8", "ignore"))
        token = (query.get("token") or [None])[0]
        return str(token) if token else None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "websocket" or not feature_flags()["ORGANIZATION_SEATS_ENABLED"]:
            await self.app(scope, receive, send)
            return
        token = self._token(scope)
        if not token:
            await self.app(scope, receive, send)
            return
        from auth.jwt_utils import decode_jwt

        payload = decode_jwt(token)
        user_id = (payload or {}).get("user_id") or (payload or {}).get("id")
        if not user_id:
            await self.app(scope, receive, send)
            return
        try:
            identity = resolve_identity(int(user_id), request_id="websocket-guard")
        except Exception:
            await send({"type": "websocket.close", "code": 4503, "reason": "组织权限校验失败"})
            return
        if identity is not None and identity.is_member:
            if not self._protocol_token(scope):
                await send({"type": "websocket.close", "code": 4403, "reason": "ORG_WS_QUERY_TOKEN_FORBIDDEN"})
                return
            path = str(scope.get("path") or "")
            classified = path == "/api/organization/ws" or bool(
                re.fullmatch(r"/ws/progress/[A-Za-z0-9_-]{8,200}", path)
            )
            if not classified:
                await send({"type": "websocket.close", "code": 4403, "reason": "ORG_ROUTE_NOT_CLASSIFIED"})
                return
        await self.app(scope, receive, send)


def setup_organization_guard(app) -> None:
    app.add_middleware(OrganizationWebSocketGuardMiddleware)
    app.add_middleware(OrganizationGuardMiddleware)
