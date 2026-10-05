"""渠道合作(发展下级服务商)申请—审批 API(工单 §P0-2)。

服务商侧 `/api/agent/channel-partners/*` + 管理员侧 `/api/admin/channel-partner-requests/*`。
业务规则与「为什么需要审批」的结论见 `services/channel_partner_requests.py` 模块头。
"""

import logging
import uuid
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from db.connection import get_db
from services import channel_partner_requests as svc
from services.relationship_privacy import strip_private_relationship_fields
from services.admin_user_governance import (
    GovernanceNotFound,
    GovernanceValidationError,
    GovernanceVersionConflict,
)
from auth.user_ctx import current_user_id

from api.refusal_log import refuse

logger = logging.getLogger("GEO-ChannelPartnerAPI")

agent_router = APIRouter(prefix="/api/agent/channel-partners", tags=["服务商渠道合作"])
admin_router = APIRouter(prefix="/api/admin/channel-partner-requests", tags=["管理员渠道合作审批"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateChannelPartnerRequest(StrictModel):
    target_user_id: int
    proposed_cost_multiplier_bps: int = Field(..., ge=10000, le=100000)
    reason: str = Field(..., min_length=2, max_length=500)


class ApproveChannelPartnerRequest(StrictModel):
    # None = 采用申请方提议值。管理员可覆写 —— 最终以审批确认值为准。
    cost_multiplier_bps: Optional[int] = Field(None, ge=10000, le=100000)
    channel_expected_version: int = Field(..., ge=1)
    decision_note: str = Field(..., min_length=2, max_length=500)


class RejectChannelPartnerRequest(StrictModel):
    decision_note: str = Field(..., min_length=2, max_length=500)


def _require_agent(request: Request) -> dict:
    """服务商身份解析 —— 直接复用工作台那一份,**不再写第二套**。

    🔴 不能读 `request.state.user["agent_level"]`:JWT 里没有这个字段,
       它的 SSOT 是 `user_wallets.agent_level`(本仓踩过 JWT vs DB 列混淆)。
       自己再实现一遍还会漏掉管理员操作平台直营账号那条分支。
    """
    from api.agent_workbench_api import _require_agent as _canonical_require_agent

    return _canonical_require_agent(request)


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


_CONFLICT_CODES = {
    "ALREADY_DOWNSTREAM", "TARGET_HAS_UPSTREAM", "PENDING_REQUEST_EXISTS",
    "REQUEST_NOT_PENDING", "TARGET_IS_ORDINARY_USER", "REQUESTER_NOT_SERVICE_PROVIDER",
    # v3:三种"不能申请"合并后的统一码(见 services 侧注释:三个码 = 三个探测位)
    "TARGET_NOT_ELIGIBLE",
}


def _raise(exc: Exception) -> None:
    if isinstance(exc, svc.ChannelPartnerRequestError):
        if exc.code in {"TARGET_NOT_FOUND", "REQUEST_NOT_FOUND"}:
            status = 404
        elif exc.code in _CONFLICT_CODES:
            status = 409
        else:
            status = 400
        detail = {"code": exc.code, "message": str(exc)}
        detail.update(exc.details or {})
        raise refuse(logger, status=status, code=exc.code, detail=detail,
                     context={"message": str(exc)}) from exc
    if isinstance(exc, GovernanceNotFound):
        raise refuse(logger, status=404, code="USER_NOT_FOUND",
                     detail="用户不存在") from exc
    if isinstance(exc, GovernanceVersionConflict):
        raise refuse(
            logger, status=409, code="GOVERNANCE_VERSION_CONFLICT",
            detail={"code": "GOVERNANCE_VERSION_CONFLICT", "scope": exc.scope,
                    "current_version": exc.current_version},
            context={"scope": exc.scope, "current_version": exc.current_version},
        ) from exc
    if isinstance(exc, GovernanceValidationError):
        detail = {"code": exc.code, "message": str(exc)}
        detail.update(getattr(exc, "details", None) or {})
        raise refuse(logger, status=409, code=exc.code, detail=detail,
                     context={"message": str(exc)}) from exc
    raise exc


# ============================================================
# 服务商侧
# ============================================================

@agent_router.get("/preflight")
async def channel_partner_preflight(
    request: Request, target_user_id: int = Query(..., ge=1),
):
    """回答「这个人跟我是什么关系、我能做什么」(工单 v3 §P0-1)。

    🔴 v3 前后的差别就是这个端点从**枚举面**变回**关系查询**:
       改造前它对任意 `target_user_id` 都回展示名 + 身份,等于开放了
       "此号注册没有 / 是不是服务商" 的探测接口(工单 §1.3b 第 2 条)。
       现在没有**有向**下级关系一律 404 `TARGET_NOT_FOUND`,
       与"账号不存在"和"查自己的上游"逐字节同构。
    """
    agent = _require_agent(request)
    try:
        with get_db() as conn:
            rel = svc.resolve_relationship(
                conn.cursor(), int(agent["user_id"]), int(target_user_id)
            )
        # 🔴 R5 机械脱敏:即便上游解析结果里将来混进关系私有字段,也在出口被剥掉。
        #    用的是全系统唯一那份清单(services/relationship_privacy.py),不另起一套。
        return strip_private_relationship_fields({"success": True, **rel})
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@agent_router.post("/requests")
async def create_channel_partner_request(
    body: CreateChannelPartnerRequest, request: Request,
):
    """申请把某个服务商账号设为自己的下级(需管理员审批)。"""
    agent = _require_agent(request)
    try:
        return svc.create_request(
            requester_user_id=int(agent["user_id"]),
            target_user_id=body.target_user_id,
            proposed_cost_multiplier_bps=body.proposed_cost_multiplier_bps,
            reason=body.reason,
            request_id=_request_id(request),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@agent_router.get("/requests")
async def list_my_channel_partner_requests(request: Request):
    agent = _require_agent(request)
    items = svc.list_requests_for_requester(int(agent["user_id"]))
    return {"success": True, "items": items, "total": len(items)}


@agent_router.post("/requests/{request_row_id:int}/cancel")
async def cancel_channel_partner_request(request_row_id: int, request: Request):
    agent = _require_agent(request)
    try:
        return svc.cancel_request(
            request_row_id=request_row_id, requester_user_id=int(agent["user_id"]),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


# ============================================================
# 管理员侧
# ============================================================

@admin_router.get("")
async def admin_list_channel_partner_requests(
    request: Request,
    status: Optional[Literal["pending", "approved", "rejected", "cancelled"]] = None,
):
    _require_admin(request)
    items = svc.list_requests_admin(status=status)
    return {"success": True, "items": items, "total": len(items)}


@admin_router.post("/{request_row_id:int}/approve")
async def admin_approve_channel_partner_request(
    request_row_id: int, body: ApproveChannelPartnerRequest, request: Request,
):
    """批准 · 同事务落渠道关系(复用治理服务函数,继承 CAS/审计/环检测)。"""
    admin = _require_admin(request)
    try:
        return svc.approve_request(
            request_row_id=request_row_id,
            cost_multiplier_bps=body.cost_multiplier_bps,
            channel_expected_version=body.channel_expected_version,
            decision_note=body.decision_note,
            operator_user_id=int(admin.get("user_id") or current_user_id(admin)),
            operator_username=admin.get("username"),
            request_id=_request_id(request),
            ip_address=_client_ip(request),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)


@admin_router.post("/{request_row_id:int}/reject")
async def admin_reject_channel_partner_request(
    request_row_id: int, body: RejectChannelPartnerRequest, request: Request,
):
    admin = _require_admin(request)
    try:
        return svc.reject_request(
            request_row_id=request_row_id,
            decision_note=body.decision_note,
            operator_user_id=int(admin.get("user_id") or current_user_id(admin)),
        )
    except Exception as exc:  # noqa: BLE001
        _raise(exc)
