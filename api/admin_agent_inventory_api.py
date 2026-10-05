"""管理员服务商库存治理 API(工单 WO_INVENTORY_POINTS_DEADLOCK_2026-08-12 §P0-1)。

补的是这个洞:`server.app.routes` 全量扫描下,admin 前缀里
只有 `/api/admin/inventory-audit/*`(只读)与 `/api/admin/pricing/inventory-purchase-catalog`
(价目表)—— **零个**库存调整或代划拨端点。服务商付了钱动不了,只能找人手工进库。
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from schemas.admin_agent_inventory import (
    AdminAdjustInventoryRequest,
    AdminAllocateInventoryRequest,
    AdminInventoryMutationResponse,
    LotDriftResponse,
)
from services.admin_agent_inventory import (
    InventoryAdminError,
    admin_adjust_inventory,
    admin_allocate_to_customer,
)
from services.admin_user_governance import (
    GovernanceNotFound,
    GovernanceValidationError,
    GovernanceVersionConflict,
)

router = APIRouter(prefix="/api/admin/agent-inventory", tags=["管理员服务商库存"])
from api.refusal_log import refuse

logger = logging.getLogger("GEO-Admin-AgentInventory")


# 语义映射:哪些拒绝是"当前状态不允许"(409),哪些是"你给的东西不对"(400/422)。
_CONFLICT_CODES = {
    "TARGET_IS_SERVICE_PROVIDER",
    "CUSTOMER_BOUND_TO_OTHER_PROVIDER",
    "PLATFORM_SELLER_USE_ISSUANCE",
    "TARGET_NOT_SERVICE_PROVIDER",
    "BUSINESS_IDENTITY_SSOT_UNAVAILABLE",
    "DUPLICATE_REQUEST",
    "SELF_ALLOCATION",
    "INVENTORY_LOT_DRIFT",
    "LOT_REJECTED",
}


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _actor_id(admin: dict) -> int:
    value = admin.get("user_id") or admin.get("id")
    if not value:
        raise HTTPException(status_code=403, detail="管理员身份缺少用户 ID")
    return int(value)


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


def _raise_inventory_error(exc: Exception) -> None:
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
        # 建绑定那一段是同事务复用治理服务函数,它的拒绝原样透出去,
        # 不在这里翻译成别的文案(两处文案漂移比多一层映射更坏)。
        detail: dict = {"code": exc.code, "message": str(exc)}
        detail.update(getattr(exc, "details", None) or {})
        raise refuse(logger, status=409, code=exc.code, detail=detail,
                     context={"message": str(exc)}) from exc
    if isinstance(exc, InventoryAdminError):
        status = 409 if exc.code in _CONFLICT_CODES else 400
        detail = {"code": exc.code, "message": str(exc)}
        detail.update(exc.details or {})
        raise refuse(logger, status=status, code=exc.code, detail=detail,
                     context={"message": str(exc)}) from exc
    raise exc


@router.post("/adjust", response_model=AdminInventoryMutationResponse)
async def admin_adjust_agent_inventory(
    body: AdminAdjustInventoryRequest, request: Request,
):
    """调整服务商库存(对公转账入账 / 录错更正)。

    反向对照(验收判据 1):不填原因 → DTO 层 422;非管理员 → 403。
    """
    admin = _require_admin(request)
    try:
        result = admin_adjust_inventory(
            body.agent_user_id,
            direction=body.direction,
            paid_points=body.paid_points,
            bonus_points=body.bonus_points,
            related_order_id=body.related_order_id,
            reason=body.reason,
            operator_user_id=_actor_id(admin),
            operator_username=admin.get("username"),
            request_id=_request_id(request),
            ip_address=_client_ip(request),
        )
        return result
    except Exception as exc:  # noqa: BLE001 — 统一映射后重抛
        _raise_inventory_error(exc)


@router.post("/allocate", response_model=AdminInventoryMutationResponse)
async def admin_allocate_agent_inventory(
    body: AdminAllocateInventoryRequest, request: Request,
):
    """管理员代服务商把库存划拨给客户。

    绑定缺失 → 同事务内建归属(带 `binding_expected_version`);
    归属他人 → 409 + 指向换绑端点。**不存在静默成功的路径**(验收判据 2)。
    """
    admin = _require_admin(request)
    try:
        result = admin_allocate_to_customer(
            body.agent_user_id, body.customer_user_id,
            paid_points=body.paid_points,
            bonus_points=body.bonus_points,
            binding_expected_version=body.binding_expected_version,
            reason=body.reason,
            operator_user_id=_actor_id(admin),
            operator_username=admin.get("username"),
            request_id=_request_id(request),
            ip_address=_client_ip(request),
        )
        for invalidation in result.pop("_permission_invalidations", []):
            try:
                from auth.perm_cache import publish_permission_version

                publish_permission_version(
                    int(invalidation["user_id"]), int(invalidation["permission_version"])
                )
            except Exception:  # noqa: BLE001 — PostgreSQL 仍是权限权威源
                logger.warning("划拨已提交,但权限缓存失效失败 user=%s", invalidation)
        return result
    except Exception as exc:  # noqa: BLE001
        _raise_inventory_error(exc)


@router.get("/lot-drift", response_model=LotDriftResponse)
async def admin_inventory_lot_drift(request: Request):
    """聚合钱包 vs 有限批次(lot)的漂移明细。

    存在的理由:`08_billing.md` §5.3 要求两者一致,而既有对账等式
    (`services/inventory_audit`)只比「钱包 vs 流水」——lot 这一维原本
    **没有任何地方看得见**。2026-08-12 实测已有两条存量漂移。
    """
    _require_admin(request)
    from db.connection import get_db
    from services.inventory_lot_ledger import lot_drift_summary

    with get_db() as conn:
        summary = lot_drift_summary(conn.cursor())
    return {"success": True, **summary}
