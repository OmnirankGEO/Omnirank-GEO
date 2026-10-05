"""Admin-only API for user identity and relationship governance."""

import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from schemas.admin_user_governance import (
    AdjustUserWalletRequest,
    BackfillCommercialBindingAuditRequest,
    AdminUserDetailResponse,
    AgreementGateMetricsResponse,
    AdminUserListResponse,
    ChangeBusinessIdentityRequest,
    ChangeChannelRelationshipRequest,
    ChangeCommercialBindingRequest,
    ChangePlatformAccessRequest,
    GovernanceMutationResponse,
    PlatformDirectReadinessResponse,
    ResetUserPasswordRequest,
)
from services.admin_user_governance import (
    GovernanceNotFound,
    backfill_commercial_binding_audit,
    GovernanceValidationError,
    GovernanceVersionConflict,
    adjust_user_wallet,
    change_business_identity,
    change_channel_relationship,
    change_commercial_binding,
    change_platform_access,
    get_admin_user_detail,
    get_platform_direct_readiness,
    list_admin_users,
    reset_user_password,
)
from services.admin_cross_tenant_governance import record_admin_read, safe_search_audit


router = APIRouter(prefix="/api/admin/user-governance", tags=["管理员用户治理"])
from api.refusal_log import refuse

logger = logging.getLogger("GEO-Admin-User-Governance")


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _request_id(request: Request) -> str:
    return str(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )[:128]


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _actor_id(admin: dict) -> int:
    value = admin.get("user_id") or admin.get("id")
    if not value:
        raise HTTPException(status_code=403, detail="管理员身份缺少用户 ID")
    return int(value)


def _publish_permissions_after_commit(user_id: int, permission_version: int) -> None:
    """Best-effort cache publication; PostgreSQL remains authoritative per request."""
    try:
        from auth.perm_cache import publish_permission_version
        if not publish_permission_version(int(user_id), int(permission_version)):
            logger.warning(
                "权限版本已提交但 Redis 发布失败；各 worker 将从 PostgreSQL 即时核验 user=%s",
                user_id,
            )
    except Exception as exc:  # permission_version remains the cross-worker source of truth
        logger.warning(
            "治理写入已提交，但本地权限缓存失效失败 user=%s: %s", user_id, exc
        )


def _raise_governance_error(exc: Exception) -> None:
    if isinstance(exc, GovernanceNotFound):
        raise refuse(logger, status=404, code="USER_NOT_FOUND",
                     detail="用户不存在") from exc
    if isinstance(exc, GovernanceVersionConflict):
        raise refuse(
            logger, status=409, code="GOVERNANCE_VERSION_CONFLICT",
            detail={
                "code": "GOVERNANCE_VERSION_CONFLICT",
                "scope": exc.scope,
                "current_version": exc.current_version,
            },
            context={"scope": exc.scope, "current_version": exc.current_version},
        ) from exc
    if isinstance(exc, GovernanceValidationError):
        if exc.code == "COMMERCIAL_BINDING_SUBJECT_MUST_BE_ORDINARY":
            status = 422
        elif exc.code in {
            "NO_CHANGE", "ACTIVE_PROVIDER_DEPENDENCIES", "BINDING_DISPUTE_PENDING",
            "LAST_ADMIN", "SELF_ADMIN_REMOVAL", "BUSINESS_IDENTITY_SSOT_UNAVAILABLE",
            "BINDING_PENDING_ORDERS",
            "ACTIVE_COMMERCIAL_PROVIDER",
            "CHANNEL_SUBJECT_MUST_BE_SERVICE_PROVIDER",
            "CHANNEL_CONVERSION_INVALID",
        }:
            status = 409
        else:
            status = 400
        detail: dict = {"code": exc.code, "message": str(exc)}
        # Machine-readable obstacles let ADMIN see WHICH dependency is non-zero
        # and where it is handled, instead of a wall of counters that are mostly 0.
        detail.update(getattr(exc, "details", None) or {})
        raise refuse(logger, status=status, code=exc.code, detail=detail,
                     context={"message": str(exc)}) from exc
    raise exc


@router.get("/users", response_model=AdminUserListResponse)
async def admin_user_governance_list(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=10, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
    identity: Optional[Literal["ordinary_user", "service_provider"]] = None,
    attention_only: bool = False,
):
    admin = _require_admin(request)
    try:
        result = list_admin_users(
            page=page, page_size=page_size, search=search,
            identity=identity, attention_only=attention_only,
        )
        record_admin_read(
            actor_user_id=_actor_id(admin), actor_username=admin.get("username"),
            action="admin_user.list", subject_kind="user", subject_id=None,
            request_id=_request_id(request),
            reason=(request.headers.get("X-Governance-Reason") or "管理员读取用户治理列表")[:500],
            before={"page": page, "page_size": page_size, **safe_search_audit(search),
                    "identity": identity, "attention_only": attention_only},
            after={"result_count": len(result["users"]), "total": result["total"]},
            ip_address=_client_ip(request),
        )
        return result
    except Exception as exc:
        _raise_governance_error(exc)


@router.get("/users/{user_id:int}", response_model=AdminUserDetailResponse)
async def admin_user_governance_detail(user_id: int, request: Request):
    admin = _require_admin(request)
    try:
        result = get_admin_user_detail(user_id)
        record_admin_read(
            actor_user_id=_actor_id(admin), actor_username=admin.get("username"),
            action="admin_user.detail", subject_kind="user", subject_id=user_id,
            request_id=_request_id(request),
            reason=(request.headers.get("X-Governance-Reason") or "管理员读取用户治理详情")[:500],
            before={}, after={"found": True, "user_id": user_id},
            ip_address=_client_ip(request),
        )
        return result
    except Exception as exc:
        _raise_governance_error(exc)


@router.get("/platform-direct-readiness", response_model=PlatformDirectReadinessResponse)
async def admin_platform_direct_readiness(request: Request):
    admin = _require_admin(request)
    readiness = get_platform_direct_readiness()
    record_admin_read(
        actor_user_id=_actor_id(admin), actor_username=admin.get("username"),
        action="platform_direct.readiness", subject_kind="platform_direct", subject_id=None,
        request_id=_request_id(request), reason="管理员读取平台托管准备状态",
        before={}, after={"configured": readiness["configured"], "ready": readiness["ready"]},
        ip_address=_client_ip(request),
    )
    return {"success": True, "readiness": readiness}


@router.get("/agreement-gate-metrics", response_model=AgreementGateMetricsResponse)
async def admin_agreement_gate_metrics(request: Request, days: int = 14):
    """协议门禁触发计数(工单 2026-07-29 §4.1)。

    存在的理由很具体:2026-07-26~29 有账号被这道门挡了 3 天,系统**没有任何地方**
    能让人看出"我正在把人挡在门外"。这个端点就是那个地方。
    """
    admin = _require_admin(request)
    from db.connection import get_db
    from services.registration_agreement_gate_metrics import gate_trigger_summary

    window = max(1, min(int(days or 14), 90))
    try:
        with get_db() as conn:
            metrics = gate_trigger_summary(conn.cursor(), days=window)
    except Exception:  # noqa: BLE001 — 观测端点自身不得 500
        metrics = {
            "available": False, "days": window, "total": 0,
            "distinct_users": 0, "daily": [], "consecutive_days_with_triggers": 0,
        }
    record_admin_read(
        actor_user_id=_actor_id(admin), actor_username=admin.get("username"),
        action="agreement_gate.metrics", subject_kind="agreement_gate", subject_id=None,
        request_id=_request_id(request), reason="管理员读取协议门禁触发计数",
        before={}, after={"days": window, "total": metrics.get("total", 0)},
        ip_address=_client_ip(request),
    )
    return {"success": True, "metrics": metrics}


@router.put("/users/{user_id:int}/business-identity", response_model=GovernanceMutationResponse)
async def admin_change_business_identity(
    user_id: int, body: ChangeBusinessIdentityRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        result = change_business_identity(
            user_id, body.business_identity,
            expected_version=body.expected_version, reason=body.reason,
            operator_user_id=_actor_id(admin), operator_username=admin.get("username"),
            request_id=_request_id(request), ip_address=_client_ip(request),
        )
        permission_version = int(result.pop("permission_version"))
        _publish_permissions_after_commit(user_id, permission_version)
        return result
    except Exception as exc:
        _raise_governance_error(exc)


@router.put(
    "/users/{user_id:int}/commercial-service-binding",
    response_model=GovernanceMutationResponse,
)
async def admin_change_commercial_service_binding(
    user_id: int, body: ChangeCommercialBindingRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        result = change_commercial_binding(
            user_id, body.provider_user_id,
            expected_version=body.expected_version, reason=body.reason,
            operator_user_id=_actor_id(admin), operator_username=admin.get("username"),
            request_id=_request_id(request), ip_address=_client_ip(request),
        )
        for invalidation in result.pop("_permission_invalidations", []):
            _publish_permissions_after_commit(
                int(invalidation["user_id"]),
                int(invalidation["permission_version"]),
            )
        return result
    except Exception as exc:
        _raise_governance_error(exc)


@router.post(
    "/users/{user_id:int}/commercial-service-binding/audit-backfill",
    response_model=GovernanceMutationResponse,
)
async def admin_backfill_commercial_binding_audit(
    user_id: int, body: BackfillCommercialBindingAuditRequest, request: Request,
):
    """给一条历史 admin_manual 归属补录审计凭证(工单 §P1-5 方案 a)。

    🔴 **不是第二个建绑定端点**:本端点对 `customer_agent_bindings` 只读,
       一个字段都不写。它补的是"当时这条归属为什么这么定"的凭证。
    🔴 补录带 `backfill=true` + `original_bound_at`,`created_at` 照实落今天 ——
       与原生审计**机械可分**;裸插的无审计绑定**仍然亮灯**。
    """
    admin = _require_admin(request)
    try:
        return backfill_commercial_binding_audit(
            user_id,
            expected_version=body.expected_version, reason=body.reason,
            operator_user_id=_actor_id(admin), operator_username=admin.get("username"),
            request_id=_request_id(request), ip_address=_client_ip(request),
        )
    except Exception as exc:
        _raise_governance_error(exc)


@router.put(
    "/users/{user_id:int}/channel-relationship",
    response_model=GovernanceMutationResponse,
)
async def admin_change_channel_relationship(
    user_id: int, body: ChangeChannelRelationshipRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        return change_channel_relationship(
            user_id,
            body.upstream_user_id,
            body.cost_multiplier_bps,
            expected_version=body.expected_version,
            reason=body.reason,
            operator_user_id=_actor_id(admin),
            operator_username=admin.get("username"),
            request_id=_request_id(request),
            ip_address=_client_ip(request),
        )
    except Exception as exc:
        _raise_governance_error(exc)


@router.put("/users/{user_id:int}/platform-access", response_model=GovernanceMutationResponse)
async def admin_change_platform_access(
    user_id: int, body: ChangePlatformAccessRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        result = change_platform_access(
            user_id, body.administrator,
            expected_version=body.expected_version, reason=body.reason,
            operator_user_id=_actor_id(admin), operator_username=admin.get("username"),
            request_id=_request_id(request), ip_address=_client_ip(request),
        )
        permission_version = int(result.pop("permission_version"))
        _publish_permissions_after_commit(user_id, permission_version)
        return result
    except Exception as exc:
        _raise_governance_error(exc)


@router.put(
    "/users/{user_id:int}/password",
    response_model=GovernanceMutationResponse,
)
async def admin_reset_user_password(
    user_id: int, body: ResetUserPasswordRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        result = reset_user_password(
            user_id,
            body.new_password,
            expected_version=body.expected_version,
            reason=body.reason,
            operator_user_id=_actor_id(admin),
            operator_username=admin.get("username"),
            request_id=_request_id(request),
            ip_address=_client_ip(request),
        )
        permission_version = int(result.pop("permission_version"))
        _publish_permissions_after_commit(user_id, permission_version)
        return result
    except Exception as exc:
        _raise_governance_error(exc)


@router.put(
    "/users/{user_id:int}/wallet-adjustment",
    response_model=GovernanceMutationResponse,
)
async def admin_adjust_user_wallet(
    user_id: int, body: AdjustUserWalletRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        return adjust_user_wallet(
            user_id,
            point_type=body.point_type,
            operation=body.operation,
            amount=body.amount,
            expected_version=body.expected_version,
            reason=body.reason,
            operator_user_id=_actor_id(admin),
            operator_username=admin.get("username"),
            request_id=_request_id(request),
            ip_address=_client_ip(request),
        )
    except Exception as exc:
        _raise_governance_error(exc)
