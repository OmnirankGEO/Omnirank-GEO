"""Dedicated ADMIN governance and presentation-safe demo-customer APIs."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from db.connection import get_db
from schemas.admin_cross_tenant_governance import (
    BatchRevokeDemoCaseGrantsRequest,
    ChangeAccountStatusRequest,
    ConfirmProviderDowngradePlanRequest,
    CreateDemoCaseGrantRequest,
    CreateProviderDowngradePlanRequest,
    RevokeDemoCaseGrantRequest,
)
from services.admin_cross_tenant_governance import (
    CrossTenantConflict,
    CrossTenantNotFound,
    CrossTenantValidation,
    change_account_status,
    confirm_provider_downgrade_plan,
    create_demo_case_grants_batch,
    create_provider_downgrade_plan,
    get_authorized_demo_case,
    get_provider_downgrade_plan,
    get_provider_downgrade_readiness,
    has_authorized_demo_case,
    list_authorized_demo_cases,
    list_cross_tenant_audits,
    list_demo_case_catalog,
    list_demo_case_grants,
    record_admin_read,
    revoke_demo_case_grants_batch,
    revoke_demo_case_grant,
    safe_search_audit,
)


admin_router = APIRouter(
    prefix="/api/admin/cross-tenant-governance",
    tags=["ADMIN 跨租户治理"],
)
demo_router = APIRouter(prefix="/api/demo-cases", tags=["演示客户流程预览"])


def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _require_admin(request: Request) -> dict:
    user = _require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要平台管理员权限")
    return user


def _user_id(user: dict) -> int:
    value = user.get("user_id") or user.get("id")
    if not value:
        raise HTTPException(status_code=403, detail="登录身份缺少用户 ID")
    return int(value)


def _request_id(request: Request) -> str:
    return str(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )[:180]


def _ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )


def _read_reason(request: Request, fallback: str) -> str:
    value = (request.headers.get("X-Governance-Reason") or fallback).strip()
    return value[:500] if len(value) >= 2 else fallback


def _raise(exc: Exception) -> None:
    if isinstance(exc, CrossTenantNotFound):
        raise HTTPException(status_code=404, detail="资源不存在") from exc
    if isinstance(exc, CrossTenantConflict):
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        ) from exc
    if isinstance(exc, CrossTenantValidation):
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    raise exc


def _publish_permission_version(user_id: int, permission_version: int) -> None:
    try:
        from auth.perm_cache import publish_permission_version
        publish_permission_version(int(user_id), int(permission_version))
    except Exception:
        # PostgreSQL is checked on every protected request. Cache publication is
        # only an accelerator and cannot keep old authority alive.
        return


@admin_router.put("/users/{user_id:int}/account-status")
async def admin_change_account_status(
    user_id: int, body: ChangeAccountStatusRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        result = change_account_status(
            user_id, active=body.active, expected_version=body.expected_version,
            reason=body.reason, actor_user_id=_user_id(admin),
            actor_username=admin.get("username"), request_id=_request_id(request),
            ip_address=_ip(request),
        )
        permission_version = int(result.pop("permission_version"))
        _publish_permission_version(user_id, permission_version)
        return result
    except Exception as exc:
        _raise(exc)


@admin_router.get("/audits")
async def admin_cross_tenant_audits(
    request: Request, page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=10, le=100),
    subject_kind: Optional[str] = Query(default=None, max_length=80),
    subject_id: Optional[int] = Query(default=None, ge=1),
):
    admin = _require_admin(request)
    result = list_cross_tenant_audits(
        page=page, page_size=page_size, subject_kind=subject_kind,
        subject_id=subject_id,
    )
    record_admin_read(
        actor_user_id=_user_id(admin), actor_username=admin.get("username"),
        action="governance_audit.list", subject_kind=subject_kind or "governance_audit",
        subject_id=subject_id, request_id=_request_id(request),
        reason=_read_reason(request, "管理员读取跨租户治理审计"),
        before={"page": page, "page_size": page_size},
        after={"result_count": len(result["audits"]), "total": result["total"]},
        ip_address=_ip(request),
    )
    return result


@admin_router.get("/demo-grants")
async def admin_list_demo_grants(
    request: Request, page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=10, le=100),
    grantee_kind: Optional[Literal["user", "organization"]] = None,
    grantee_id: Optional[int] = Query(default=None, ge=1),
):
    admin = _require_admin(request)
    result = list_demo_case_grants(
        page=page, page_size=page_size, grantee_kind=grantee_kind,
        grantee_id=grantee_id,
    )
    record_admin_read(
        actor_user_id=_user_id(admin), actor_username=admin.get("username"),
        action="demo_grant.list", subject_kind=grantee_kind or "demo_grant",
        subject_id=grantee_id, request_id=_request_id(request),
        reason=_read_reason(request, "管理员读取演示案例授权列表"),
        before={"page": page, "page_size": page_size},
        after={"result_count": len(result["grants"]), "total": result["total"]},
        ip_address=_ip(request),
    )
    return result


@admin_router.get("/demo-cases/catalog")
async def admin_demo_case_catalog(
    request: Request, page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=10, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
):
    admin = _require_admin(request)
    result = list_demo_case_catalog(page=page, page_size=page_size, search=search)
    record_admin_read(
        actor_user_id=_user_id(admin), actor_username=admin.get("username"),
        action="demo_case.catalog", subject_kind="demo_case", subject_id=None,
        request_id=_request_id(request),
        reason=_read_reason(request, "管理员搜索可授权演示客户"),
        before={"page": page, "page_size": page_size, **safe_search_audit(search)},
        after={"result_count": len(result["cases"]), "total": result["total"]},
        ip_address=_ip(request),
    )
    return result


@admin_router.post("/demo-grants")
async def admin_create_demo_grant(body: CreateDemoCaseGrantRequest, request: Request):
    admin = _require_admin(request)
    try:
        return create_demo_case_grants_batch(
            grantee_kind=body.grantee_kind,
            grantee_user_id=body.grantee_user_id,
            grantee_organization_id=body.grantee_organization_id,
            selections=[item.model_dump(mode="json") for item in body.selections],
            expires_at=body.expires_at, reason=body.reason,
            note=body.note,
            actor_user_id=_user_id(admin), actor_username=admin.get("username"),
            request_id=_request_id(request), ip_address=_ip(request),
        )
    except Exception as exc:
        _raise(exc)


@admin_router.post("/demo-grants/batch-revoke")
async def admin_batch_revoke_demo_grants(
    body: BatchRevokeDemoCaseGrantsRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        return revoke_demo_case_grants_batch(
            grants=[item.model_dump() for item in body.grants], reason=body.reason,
            actor_user_id=_user_id(admin), actor_username=admin.get("username"),
            request_id=_request_id(request), ip_address=_ip(request),
        )
    except Exception as exc:
        _raise(exc)


@admin_router.post("/demo-grants/{grant_id:int}/revoke")
async def admin_revoke_demo_grant(
    grant_id: int, body: RevokeDemoCaseGrantRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        return revoke_demo_case_grant(
            grant_id, expected_version=body.expected_version, reason=body.reason,
            actor_user_id=_user_id(admin), actor_username=admin.get("username"),
            request_id=_request_id(request), ip_address=_ip(request),
        )
    except Exception as exc:
        _raise(exc)


@admin_router.get("/providers/{provider_user_id:int}/downgrade-readiness")
async def admin_provider_downgrade_readiness(provider_user_id: int, request: Request):
    admin = _require_admin(request)
    result = get_provider_downgrade_readiness(provider_user_id)
    record_admin_read(
        actor_user_id=_user_id(admin), actor_username=admin.get("username"),
        action="provider_downgrade.readiness", subject_kind="user",
        subject_id=provider_user_id, request_id=_request_id(request),
        reason=_read_reason(request, "管理员读取服务商降级依赖"), before={},
        after={"ready": result["ready"], "blocker_categories": result["blocker_categories"]},
        ip_address=_ip(request),
    )
    return {"success": True, "readiness": result}


@admin_router.post("/providers/{provider_user_id:int}/downgrade-plans")
async def admin_create_provider_downgrade_plan(
    provider_user_id: int, body: CreateProviderDowngradePlanRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        return create_provider_downgrade_plan(
            provider_user_id, strategy=body.strategy,
            target_provider_user_id=body.target_provider_user_id,
            expected_identity_version=body.expected_identity_version,
            reason=body.reason, actor_user_id=_user_id(admin),
            actor_username=admin.get("username"), request_id=_request_id(request),
            ip_address=_ip(request),
        )
    except Exception as exc:
        _raise(exc)


@admin_router.get("/providers/{provider_user_id:int}/downgrade-plans/{plan_id:int}")
async def admin_get_provider_downgrade_plan(
    provider_user_id: int, plan_id: int, request: Request,
):
    admin = _require_admin(request)
    try:
        result = get_provider_downgrade_plan(provider_user_id, plan_id)
        record_admin_read(
            actor_user_id=_user_id(admin), actor_username=admin.get("username"),
            action="provider_downgrade.plan_read", subject_kind="provider_downgrade_plan",
            subject_id=plan_id, request_id=_request_id(request),
            reason=_read_reason(request, "管理员读取服务商降级方案"), before={},
            after={"status": result["status"], "version": result["version"]},
            ip_address=_ip(request),
        )
        return {"success": True, "plan": result}
    except Exception as exc:
        _raise(exc)


@admin_router.post("/providers/{provider_user_id:int}/downgrade-plans/{plan_id:int}/confirm")
async def admin_confirm_provider_downgrade_plan(
    provider_user_id: int, plan_id: int,
    body: ConfirmProviderDowngradePlanRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        result = confirm_provider_downgrade_plan(
            provider_user_id, plan_id,
            expected_plan_version=body.expected_plan_version, reason=body.reason,
            actor_user_id=_user_id(admin), actor_username=admin.get("username"),
            request_id=_request_id(request), ip_address=_ip(request),
        )
        if result.get("blocked"):
            raise HTTPException(
                status_code=409,
                detail={"code": "PROVIDER_DOWNGRADE_BLOCKED",
                        "message": "仍有未处理依赖，禁止静默降级", "details": result["details"],
                        "plan": result["plan"]},
            )
        permission_version = result.pop("permission_version", None)
        if permission_version is not None:
            _publish_permission_version(provider_user_id, int(permission_version))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise(exc)


@demo_router.get("")
async def demo_case_list(
    request: Request, page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=10, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
):
    user = _require_user(request)
    return list_authorized_demo_cases(
        _user_id(user), page=page, page_size=page_size, search=search,
        request_id=_request_id(request), ip_address=_ip(request),
    )


@demo_router.get("/{case_id}")
async def demo_case_detail(case_id: uuid.UUID, request: Request):
    user = _require_user(request)
    try:
        return {"success": True, "case": get_authorized_demo_case(
            _user_id(user), str(case_id), action="demo_case.detail",
            request_id=_request_id(request), ip_address=_ip(request),
        )}
    except Exception as exc:
        _raise(exc)


@demo_router.get("/{case_id}/export")
async def demo_case_export(case_id: uuid.UUID, request: Request):
    user = _require_user(request)
    try:
        payload = get_authorized_demo_case(
            _user_id(user), str(case_id), action="demo_case.export",
            request_id=_request_id(request), ip_address=_ip(request),
        )
    except Exception as exc:
        _raise(exc)
    return Response(
        json.dumps(payload, ensure_ascii=False, default=str, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="demo-case-{case_id}.json"',
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


def _ws_token(websocket: WebSocket) -> Optional[str]:
    offered = [part.strip() for part in str(websocket.headers.get("sec-websocket-protocol") or "").split(",") if part.strip()]
    if len(offered) != 2 or offered[0] != "omnirank-auth" or len(offered[1]) < 20:
        return None
    return offered[1]


def _permission_current(user_id: int, expected: object) -> bool:
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT is_active,permission_version FROM users WHERE id=%s", (int(user_id),))
            row = cur.fetchone()
            return bool(row and _active_user(row["is_active"]) and int(row["permission_version"] or 1) == int(expected or 1))
    except Exception:
        return False


def _active_user(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "inactive", "disabled"}
    return bool(value)


@demo_router.websocket("/{case_id}/ws")
async def demo_case_ws(websocket: WebSocket, case_id: uuid.UUID):
    from auth.jwt_utils import decode_jwt
    from services.demo_access import record_demo_access_event, resolve_demo_case_access

    token = _ws_token(websocket)
    payload = decode_jwt(token or "")
    if not payload:
        await websocket.close(code=4401)
        return
    user_id = int(payload.get("user_id") or payload.get("id") or 0)
    if not user_id or not _permission_current(user_id, payload.get("perm_version")):
        await websocket.close(code=4401)
        return
    context = resolve_demo_case_access(user_id, str(case_id))
    if not context:
        # Missing and unauthorized resources share one close code.
        await websocket.close(code=4404)
        return
    record_demo_access_event(
        context, action="demo_case.ws_connect",
        request_id=f"demo-ws:{uuid.uuid4().hex}",
        ip_address=websocket.client.host if websocket.client else "unknown",
    )
    await websocket.accept(subprotocol="omnirank-auth")
    try:
        while True:
            if (
                not _permission_current(user_id, payload.get("perm_version"))
                or not has_authorized_demo_case(user_id, str(case_id))
            ):
                await websocket.send_json({"type": "permission-revoked"})
                await websocket.close(code=4403)
                return
            await websocket.send_json({
                "type": "heartbeat", "access_mode": "demo",
                "presentation_preview": True,
            })
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=1)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        return
