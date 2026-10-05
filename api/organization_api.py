"""Organization internal-seat HTTP, realtime, and public-token contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from db.connection import get_db
from db.organization_db import readiness, resolve_identity
from services.organization_approvals import (
    configure_policy,
    decide_approval,
    list_approvals,
    list_policies,
    revoke_approval,
    submit_approval,
)
from services.organization_artifacts import (
    issue_public_token,
    list_artifacts,
    revoke_share,
    share_artifact_internal,
    validate_public_token,
)
from services.organization_billing import refund_charge
from services.organization_contract import IdentityContext, OrganizationError
from services.organization_limits import configure_limit, list_limits
from services.organization_onboarding import (
    create_verification_challenge,
    get_product_config,
    inspect_public_invite,
    onboard_operator,
    onboard_operator_via_credential,
    publish_product_config,
    verify_challenge,
)
from services.organization_plans import create_plan, list_plans, set_plan_status
from services.organization_service import (
    accept_invite,
    assign_brands,
    create_invite,
    create_organization,
    create_role,
    get_invite_link,
    get_overview,
    handoff_brands,
    inspect_invite,
    leave_organization,
    list_audit_events,
    list_invites,
    list_members,
    list_roles,
    resend_invite,
    revoke_invite,
    set_member_overrides,
    set_member_status,
    update_role,
)


router = APIRouter(prefix="/api/organization", tags=["组织员工席位"])
public_router = APIRouter(prefix="/api/public/organization", tags=["组织公开链接"])
admin_router = APIRouter(prefix="/api/admin/organization-product-config", tags=["组织席位商品治理"])


def _identity(request: Request) -> IdentityContext:
    identity = getattr(request.state, "organization_identity", None)
    if identity is None:
        raise OrganizationError("ORG_MEMBERSHIP_REQUIRED", "当前账号尚未加入组织", http_status=404)
    return identity


def _user_id(request: Request) -> int:
    user = getattr(request.state, "user", None) or {}
    value = user.get("user_id") or user.get("id")
    if not value:
        raise HTTPException(status_code=401, detail="未登录")
    return int(value)


def _source_ip(request: Request) -> str:
    """Use the authenticated transport peer; never trust client-supplied XFF."""
    client = request.client
    if client is None or not str(client.host or "").strip():
        raise OrganizationError(
            "ORG_RATE_SUBJECT_MISSING",
            "无法确认请求来源，组织安全限流已关闭该操作",
            http_status=503,
        )
    return str(client.host).strip()


def _permission_version_is_current(user_id: int, expected_version: Any) -> bool:
    if expected_version is None:
        return False
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT permission_version,is_active FROM users WHERE id=%s", (int(user_id),))
        row = cursor.fetchone()
        return bool(
            row
            and bool(row["is_active"])
            and int(row["permission_version"] or 0) == int(expected_version)
        )


class CreateOrganizationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=8, max_length=160)


class InviteFeatureLimitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily: Optional[int] = Field(default=None, ge=0)
    monthly: Optional[int] = Field(default=None, ge=0)


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # [WP6] username = owner 设定的登录用户名式邀请(被邀请人凭 token 直接自设密码开户)。
    target_kind: Literal["phone", "email", "username"] = "phone"
    target: str = Field(min_length=3, max_length=320)
    role_id: int = Field(gt=0)
    brand_ids: List[int] = Field(default_factory=list, max_length=1000)
    capability_overrides: Dict[str, Literal["allow", "deny"]] = Field(default_factory=dict)
    artifact_scope: Literal["own", "assigned_team"] = "own"
    daily_limit_points: Optional[int] = Field(default=None, ge=0)
    monthly_limit_points: Optional[int] = Field(default=None, ge=0)
    feature_limits: Dict[str, InviteFeatureLimitRequest] = Field(default_factory=dict)
    high_risk_confirmed: bool = False
    high_risk_reason: Optional[str] = Field(default=None, min_length=3, max_length=500)
    request_id: str = Field(min_length=8, max_length=160)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ResendRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=160)


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    request_id: str = Field(min_length=8, max_length=160)


class ProductConfigPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=160)
    expected_version: int = Field(ge=1)
    included_seats: int = Field(ge=1, le=10000)
    invite_ttl_hours: int = Field(ge=1, le=24 * 30)
    verification_ttl_minutes: int = Field(ge=5, le=60)
    verification_max_attempts: int = Field(ge=3, le=10)
    reason: str = Field(min_length=1, max_length=500)


class PublicInviteTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=512)
    request_id: str = Field(min_length=8, max_length=160)


class PublicInviteVerifyRequest(PublicInviteTokenRequest):
    code: str = Field(pattern=r"^[0-9]{6}$")


# [F-3] 阈值不在此重复定义:从服务层唯一权威常量导入,杜绝前后端/层间漂移
from services.organization_onboarding import MIN_OPERATOR_PASSWORD_LENGTH


class PublicInviteOnboardRequest(PublicInviteTokenRequest):
    challenge_id: int = Field(gt=0)
    verification_receipt: str = Field(min_length=20, max_length=512)
    # [F-3 · Owner 裁决]团队内部操作员账号走一次性邀请凭证开户,12 位属过度设防
    # (§1.6:合规姿态不得牺牲可用性)。最少 6 位,上限维持 128;前后端阈值同源。
    password: str = Field(min_length=MIN_OPERATOR_PASSWORD_LENGTH, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    terms_accepted: bool
    privacy_accepted: bool
    terms_version: str = Field(min_length=1, max_length=40)
    privacy_version: str = Field(min_length=1, max_length=40)


class PublicInviteCredentialOnboardRequest(PublicInviteTokenRequest):
    # [WP6] 用户名式邀请:owner 已设登录名,被邀请人凭 token 直接自设初始密码开户,
    # 无 challenge_id/verification_receipt。
    # [F-3 · Owner 裁决]团队内部操作员账号走一次性邀请凭证开户,12 位属过度设防
    # (§1.6:合规姿态不得牺牲可用性)。最少 6 位,上限维持 128;前后端阈值同源。
    password: str = Field(min_length=MIN_OPERATOR_PASSWORD_LENGTH, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    terms_accepted: bool
    privacy_accepted: bool
    terms_version: str = Field(min_length=1, max_length=40)
    privacy_version: str = Field(min_length=1, max_length=40)


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    capabilities: List[str] = Field(default_factory=list, max_length=100)
    high_risk_confirmed: bool = False
    high_risk_reason: Optional[str] = Field(default=None, min_length=3, max_length=500)


class RoleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    capabilities: List[str] = Field(default_factory=list, max_length=100)
    high_risk_confirmed: bool = False
    high_risk_reason: Optional[str] = Field(default=None, min_length=3, max_length=500)


class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_membership_version: int = Field(ge=1)
    overrides: Dict[str, Literal["allow", "deny"]]
    reason: str = Field(min_length=1, max_length=500)
    high_risk_confirmed: bool = False
    high_risk_reason: Optional[str] = Field(default=None, min_length=3, max_length=500)


class MemberStatusRequest(BaseModel):
    action: Literal["suspend", "resume", "remove"]
    reason: str = Field(min_length=1, max_length=500)


class AssignmentRequest(BaseModel):
    brand_ids: List[int] = Field(default_factory=list, max_length=1000)
    request_id: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=1, max_length=500)


class HandoffRequest(BaseModel):
    from_membership_id: int = Field(gt=0)
    to_membership_id: int = Field(gt=0)
    brand_ids: List[int] = Field(min_length=1, max_length=1000)
    request_id: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=1, max_length=500)


class LimitRequest(BaseModel):
    membership_id: int = Field(gt=0)
    limit_kind: Literal["daily_total", "monthly_total", "daily_feature", "monthly_feature", "custom"]
    limit_points: int = Field(ge=0)
    feature_code: Optional[str] = Field(default=None, max_length=100)
    custom_start: Optional[datetime] = None
    custom_end: Optional[datetime] = None
    reason: str = Field(min_length=1, max_length=500)


class ApprovalSubmitRequest(BaseModel):
    action_type: str = Field(min_length=3, max_length=100)
    payload: Dict[str, Any]
    request_id: str = Field(min_length=8, max_length=160)
    brand_id: Optional[int] = Field(default=None, gt=0)
    artifact_type: Optional[str] = Field(default=None, max_length=100)
    artifact_id: Optional[str] = Field(default=None, max_length=200)
    estimated_points: int = Field(default=0, ge=0)
    feature_code: Optional[str] = Field(default=None, max_length=100)
    public_scope: Optional[str] = Field(default=None, max_length=100)


class ApprovalPolicyRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=160)
    action_type: str = Field(min_length=3, max_length=100)
    always_require_approval: bool
    membership_id: Optional[int] = Field(default=None, gt=0)
    role_id: Optional[int] = Field(default=None, gt=0)
    feature_code: Optional[str] = Field(default=None, max_length=100)
    public_scope: Optional[str] = Field(default=None, max_length=100)
    min_points: Optional[int] = Field(default=None, ge=0)
    max_points: Optional[int] = Field(default=None, ge=0)
    threshold_points: Optional[int] = Field(default=None, ge=0)
    expected_version: Optional[int] = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=500)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=500)


class ReserveWorkRequest(BaseModel):
    execution_id: str = Field(min_length=8, max_length=160)
    feature_code: str = Field(min_length=2, max_length=100)
    work_kind: str = Field(min_length=2, max_length=100)
    payload: Dict[str, Any]
    brand_id: Optional[int] = Field(default=None, gt=0)
    task_ref: Optional[str] = Field(default=None, max_length=200)
    approval_request_id: Optional[int] = Field(default=None, gt=0)
    external_action: Optional[Literal[
        "quote.send_external",
        "diagnosis_report.share_external",
        "monitoring_report.share_external",
        "portal.issue_external_token",
        "publish.execute",
    ]] = None


class RefundRequest(BaseModel):
    cumulative_refund_target: int = Field(ge=0)
    refund_request_id: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=1, max_length=500)


class InternalShareRequest(BaseModel):
    artifact_type: str = Field(min_length=2, max_length=100)
    artifact_id: str = Field(min_length=1, max_length=200)
    permission: Literal["read", "review", "edit", "handoff"]
    subject_membership_id: Optional[int] = Field(default=None, gt=0)
    subject_role_id: Optional[int] = Field(default=None, gt=0)
    request_id: str = Field(min_length=8, max_length=160)


class PublicTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_type: str = Field(min_length=2, max_length=100)
    artifact_id: str = Field(min_length=1, max_length=200)
    purpose: Literal["quote", "diagnosis_report", "monitoring_report", "portal"]
    request_id: str = Field(min_length=8, max_length=160)
    approval_request_id: Optional[int] = Field(default=None, gt=0)
    expires_in_seconds: int = Field(ge=60, le=30 * 86400)


class PublicTokenValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: Literal["quote", "diagnosis_report", "monitoring_report", "portal"]
    token: str = Field(min_length=20, max_length=512)


class AutomaticPlanRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=160)
    feature_code: str = Field(min_length=2, max_length=100)
    work_kind: str = Field(min_length=2, max_length=100)
    payload: Dict[str, Any]
    cadence_seconds: int = Field(ge=60, le=365 * 86400)
    max_occurrences: int = Field(gt=0, le=10000)
    total_budget_points: int = Field(gt=0)
    max_occurrence_points: int = Field(gt=0)
    starts_at: datetime
    ends_at: datetime
    brand_id: Optional[int] = Field(default=None, gt=0)


class AutomaticPlanStatusRequest(BaseModel):
    status: Literal["active", "paused", "cancelled"]
    expected_version: int = Field(ge=1)


@router.post("")
def create_org(req: CreateOrganizationRequest, request: Request):
    return create_organization(owner_user_id=_user_id(request), name=req.name, request_id=req.request_id)


@router.get("/overview")
def overview(request: Request):
    """团队总览。**「还没有团队」是正常状态,不是错误。**

    [P0 修复 2026-08-17] 原来这里走 `_identity()`,没有团队的账号一律 404。
    但 `OrganizationProvider` 挂在 App 顶层(frontend/src/App.tsx),
    **每一个登录账号的每一次页面加载**都会打这个端点 —— 绝大多数账号没有团队,
    于是浏览器 console 每次都印一条红色 404。前端本来就把 404 当「没团队」处理
    (OrganizationContext.tsx),所以这个 404 不携带任何信息量,纯粹是噪音,
    还会掩盖真正的 404。

    改法:没团队 → 200 + `{"has_organization": false}`。这是**加字段**,
    有团队时的响应形状一字未变(仍然没有 `has_organization` 以外的改动),
    前端两种形态都认(旧前端遇到 200 也不会崩:它只读 `identity`/`entitlement`,
    读不到就当没团队)。404 分支保留在前端,兼容灰度期间的旧构建。
    """
    identity = getattr(request.state, "organization_identity", None)
    if identity is None:
        return {"has_organization": False}
    result = get_overview(identity)
    result["has_organization"] = True
    return result


@router.get("/readiness")
def organization_readiness(request: Request):
    identity = _identity(request)
    if not identity.is_owner:
        raise OrganizationError("ORG_OWNER_REQUIRED", "该页面仅团队负责人可查看", http_status=403)
    return readiness()


@router.get("/roles")
def roles(request: Request):
    return list_roles(_identity(request))


@router.post("/roles")
def add_role(req: RoleRequest, request: Request):
    return create_role(
        _identity(request),
        code=req.code,
        name=req.name,
        capabilities=req.capabilities,
        high_risk_confirmed=req.high_risk_confirmed,
        high_risk_reason=req.high_risk_reason,
    )


@router.put("/roles/{role_id}")
def edit_role(role_id: int, req: RoleUpdateRequest, request: Request):
    return update_role(
        _identity(request),
        role_id=role_id,
        expected_version=req.expected_version,
        name=req.name,
        capabilities=req.capabilities,
        high_risk_confirmed=req.high_risk_confirmed,
        high_risk_reason=req.high_risk_reason,
    )


@router.get("/invites")
def invites(request: Request):
    return list_invites(_identity(request))


@router.get("/short-code")
def get_short_code(request: Request):
    """[P0-B ②] 取本团队短代码(没有就按公司名自动分配一个候选,不锁定)。"""
    identity = _identity(request)
    from db.connection import get_db
    from services.organization_short_code import (
        SHORT_CODE_MAX, SHORT_CODE_MIN, get_or_assign_short_code,
    )

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM organizations WHERE id = %s", (identity.organization_id,))
        row = cursor.fetchone()
        code = get_or_assign_short_code(
            cursor,
            organization_id=identity.organization_id,
            organization_name=(row or {}).get("name") or "",
        )
        cursor.execute(
            "SELECT short_code_locked FROM organizations WHERE id = %s", (identity.organization_id,)
        )
        locked = bool((cursor.fetchone() or {}).get("short_code_locked"))
        conn.commit()

    return {
        "success": True,
        "short_code": code,
        "can_change": (not locked) and identity.is_owner,
        "min_length": SHORT_CODE_MIN,
        "max_length": SHORT_CODE_MAX,
        "hint": "员工登录名会是「团队代码-员工名」,例如 " + f"{code}-zhangwei",
    }


class ShortCodeRequest(BaseModel):
    short_code: str = Field(..., min_length=4, max_length=12)


@router.put("/short-code")
def update_short_code(req: ShortCodeRequest, request: Request):
    """[P0-B ②] 团队长改短代码。只能改一次 —— 它是登录名的一部分。"""
    identity = _identity(request)
    if not identity.is_owner:
        raise OrganizationError("ORG_OWNER_REQUIRED", "仅团队长可修改团队代码", http_status=403)

    from db.connection import get_db
    from services.organization_short_code import ShortCodeError, set_short_code

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            code = set_short_code(
                cursor, organization_id=identity.organization_id, short_code=req.short_code
            )
            conn.commit()
    except ShortCodeError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": exc.message,
                "reason": "团队代码是员工登录名的一部分,必须全局唯一且稳定。",
                "impact": "本次修改没有保存。",
                "repair_hint": "换一个 4-12 位的小写字母或数字组合再试。",
                "actions": [{"id": "retry", "label": "换一个再试", "type": "dismiss"}],
                "rule_version": "org-short-code-v1",
            },
        ) from exc

    return {"success": True, "short_code": code, "can_change": False}


@router.get("/invites/check-login-name")
def check_invite_login_name(request: Request, member_name: str = ""):
    """[P0-B ①④] 边输边查:返回**最终登录名全貌**与三态判定。

    团队长在输入框里打 zhangwei,这里就告诉他最终是 sjkj-zhangwei、现在能不能用。
    冲突在这一刻就说清楚,不留到员工设密码那一刻。
    """
    identity = _identity(request)
    from db.connection import get_db
    from services.organization_short_code import (
        ShortCodeError, check_login_name, compose_login_name, get_or_assign_short_code,
    )

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM organizations WHERE id = %s", (identity.organization_id,))
        row = cursor.fetchone()
        code = get_or_assign_short_code(
            cursor,
            organization_id=identity.organization_id,
            organization_name=(row or {}).get("name") or "",
        )
        try:
            login_name = compose_login_name(code, member_name)
        except ShortCodeError as exc:
            conn.commit()
            return {
                "success": True, "short_code": code, "login_name": None,
                "status": "invalid", "message": exc.message, "code": exc.code,
            }
        verdict = check_login_name(
            cursor, login_name=login_name, organization_id=identity.organization_id
        )
        conn.commit()

    return {"success": True, "short_code": code, **verdict}


@router.post("/invites")
def invite(req: InviteRequest, request: Request):
    return create_invite(
        _identity(request),
        target_kind=req.target_kind,
        target=req.target,
        role_id=req.role_id,
        request_id=req.request_id,
        source_ip=_source_ip(request),
        brand_ids=req.brand_ids,
        capability_overrides=req.capability_overrides,
        artifact_scope=req.artifact_scope,
        daily_limit_points=req.daily_limit_points,
        monthly_limit_points=req.monthly_limit_points,
        feature_limits={code: value.model_dump(exclude_none=True) for code, value in req.feature_limits.items()},
        high_risk_confirmed=req.high_risk_confirmed,
        high_risk_reason=req.high_risk_reason,
    )


@router.get("/invites/{invite_id}/link")
def invite_link(invite_id: int, request: Request):
    """[P0 修复 2026-08-17] 老板重新取回邀请链接口令。只读,不重签、不作废旧链接。"""
    return get_invite_link(_identity(request), invite_id=invite_id)


@router.post("/invites/{invite_id}/resend")
def resend(invite_id: int, req: ResendRequest, request: Request):
    return resend_invite(
        _identity(request), invite_id=invite_id, request_id=req.request_id,
        source_ip=_source_ip(request),
    )


@router.post("/invites/{invite_id}/revoke")
def revoke(invite_id: int, req: ReasonRequest, request: Request):
    return revoke_invite(_identity(request), invite_id=invite_id, reason=req.reason)


@router.post("/invites/accept")
def accept(req: AcceptInviteRequest, request: Request):
    return accept_invite(
        authenticated_user_id=_user_id(request), token=req.token,
        request_id=req.request_id, source_ip=_source_ip(request),
    )


@router.post("/invites/inspect")
def inspect(req: AcceptInviteRequest, request: Request):
    return inspect_invite(
        authenticated_user_id=_user_id(request), token=req.token,
        request_id=req.request_id, source_ip=_source_ip(request),
    )


@router.get("/members")
def members(request: Request):
    return list_members(_identity(request))


@router.put("/members/{membership_id}/overrides")
def overrides(membership_id: int, req: OverrideRequest, request: Request):
    return set_member_overrides(
        _identity(request),
        membership_id=membership_id,
        expected_membership_version=req.expected_membership_version,
        overrides=req.overrides,
        reason=req.reason,
        high_risk_confirmed=req.high_risk_confirmed,
        high_risk_reason=req.high_risk_reason,
    )


@router.post("/members/{membership_id}/status")
def member_status(membership_id: int, req: MemberStatusRequest, request: Request):
    return set_member_status(_identity(request), membership_id=membership_id, action=req.action, reason=req.reason)


@router.post("/leave")
def leave(req: ReasonRequest, request: Request):
    return leave_organization(_identity(request), reason=req.reason)


@router.put("/members/{membership_id}/assignments")
def assignments(membership_id: int, req: AssignmentRequest, request: Request):
    return assign_brands(_identity(request), membership_id=membership_id, brand_ids=req.brand_ids, request_id=req.request_id, reason=req.reason)


@router.post("/assignments/handoff")
def handoff(req: HandoffRequest, request: Request):
    return handoff_brands(_identity(request), from_membership_id=req.from_membership_id, to_membership_id=req.to_membership_id, brand_ids=req.brand_ids, request_id=req.request_id, reason=req.reason)


@router.get("/limits")
def limits(request: Request, membership_id: Optional[int] = Query(default=None, gt=0)):
    return list_limits(_identity(request), membership_id=membership_id)


@router.post("/limits")
def set_limit(req: LimitRequest, request: Request):
    return configure_limit(_identity(request), membership_id=req.membership_id, limit_kind=req.limit_kind, limit_points=req.limit_points, feature_code=req.feature_code, custom_start=req.custom_start, custom_end=req.custom_end, reason=req.reason)


@router.get("/approvals")
def approvals(request: Request, status: Optional[str] = None, limit: int = Query(default=100, ge=1, le=200)):
    return list_approvals(_identity(request), status=status, limit=limit)


@router.post("/approvals")
def submit(req: ApprovalSubmitRequest, request: Request):
    return submit_approval(_identity(request), action_type=req.action_type, payload=req.payload, request_id=req.request_id, brand_id=req.brand_id, artifact_type=req.artifact_type, artifact_id=req.artifact_id, estimated_points=req.estimated_points, feature_code=req.feature_code, public_scope=req.public_scope)


@router.get("/approval-policies")
def approval_policies(request: Request):
    return list_policies(_identity(request))


@router.post("/approval-policies")
def set_approval_policy(req: ApprovalPolicyRequest, request: Request):
    return configure_policy(
        _identity(request),
        request_id=req.request_id,
        action_type=req.action_type,
        always_require_approval=req.always_require_approval,
        membership_id=req.membership_id,
        role_id=req.role_id,
        feature_code=req.feature_code,
        public_scope=req.public_scope,
        min_points=req.min_points,
        max_points=req.max_points,
        threshold_points=req.threshold_points,
        expected_version=req.expected_version,
        reason=req.reason,
    )


@router.post("/approvals/{approval_id}/decision")
def decide(approval_id: int, req: ApprovalDecisionRequest, request: Request):
    return decide_approval(_identity(request), approval_id=approval_id, decision=req.decision, expected_version=req.expected_version, reason=req.reason)


@router.post("/approvals/{approval_id}/revoke")
def revoke_approval_endpoint(approval_id: int, req: ReasonRequest, request: Request):
    return revoke_approval(_identity(request), approval_id=approval_id, reason=req.reason)


@router.post("/work/reservations")
async def reserve(req: ReserveWorkRequest, request: Request):
    _identity(request)
    raise OrganizationError(
        "ORG_DIRECT_RESERVATION_FORBIDDEN",
        "付费任务只能由已接线的现役业务入口创建并立即执行",
        http_status=403,
    )


@router.post("/charges/{charge_link_id}/refund")
async def refund(charge_link_id: int, req: RefundRequest, request: Request):
    identity = _identity(request)
    if not identity.is_owner:
        raise OrganizationError("ORG_OWNER_REQUIRED", "仅老板可发起退款", http_status=403)
    return await refund_charge(charge_link_id=charge_link_id, cumulative_refund_target=req.cumulative_refund_target, refund_request_id=req.refund_request_id, reason=req.reason, expected_organization_id=identity.organization_id)


@router.get("/artifacts/{artifact_type}")
def artifacts(artifact_type: str, request: Request, brand_id: Optional[int] = Query(default=None, gt=0), search: Optional[str] = Query(default=None, max_length=100), limit: int = Query(default=50, ge=1, le=200)):
    return list_artifacts(_identity(request), artifact_type=artifact_type, brand_id=brand_id, search=search, limit=limit)


@router.post("/artifacts/shares/internal")
def internal_share(req: InternalShareRequest, request: Request):
    return share_artifact_internal(_identity(request), artifact_type=req.artifact_type, artifact_id=req.artifact_id, permission=req.permission, subject_membership_id=req.subject_membership_id, subject_role_id=req.subject_role_id, request_id=req.request_id)


@router.post("/artifacts/shares/public")
def public_share(req: PublicTokenRequest, request: Request):
    return issue_public_token(_identity(request), artifact_type=req.artifact_type, artifact_id=req.artifact_id, purpose=req.purpose, request_id=req.request_id, approval_request_id=req.approval_request_id, expires_in_seconds=req.expires_in_seconds)


@router.post("/artifacts/shares/{share_id}/revoke")
def revoke_artifact_share(share_id: int, req: ReasonRequest, request: Request):
    return revoke_share(_identity(request), share_id=share_id, reason=req.reason)


@router.get("/audit")
def audit(request: Request, limit: int = Query(default=100, ge=1, le=200), before_id: Optional[int] = Query(default=None, gt=0)):
    return list_audit_events(_identity(request), limit=limit, before_id=before_id)


@router.get("/automatic-plans")
def automatic_plans(request: Request):
    return list_plans(_identity(request))


@router.post("/automatic-plans")
def add_automatic_plan(req: AutomaticPlanRequest, request: Request):
    return create_plan(
        _identity(request), request_id=req.request_id, feature_code=req.feature_code,
        work_kind=req.work_kind, payload=req.payload, cadence_seconds=req.cadence_seconds,
        max_occurrences=req.max_occurrences, total_budget_points=req.total_budget_points,
        max_occurrence_points=req.max_occurrence_points, starts_at=req.starts_at,
        ends_at=req.ends_at, brand_id=req.brand_id,
    )


@router.post("/automatic-plans/{plan_id}/status")
async def automatic_plan_status(plan_id: int, req: AutomaticPlanStatusRequest, request: Request):
    return await set_plan_status(_identity(request), plan_id=plan_id, status=req.status, expected_version=req.expected_version)


@router.get("/events")
async def events(request: Request):
    identity = _identity(request)
    initial_authority = identity.authority_version
    initial_permission_version = (getattr(request.state, "user", None) or {}).get("perm_version")

    async def stream():
        last_id: Optional[int] = None
        while True:
            if await request.is_disconnected():
                return
            try:
                live = resolve_identity(identity.authenticated_user_id, request_id=identity.request_id)
            except OrganizationError:
                live = None
            if (
                live is None
                or live.authority_version != initial_authority
                or not _permission_version_is_current(identity.authenticated_user_id, initial_permission_version)
            ):
                yield {"event": "permission-revoked", "data": json.dumps({"code": "ORG_AUTHORITY_CHANGED"})}
                return
            rows = list_audit_events(live, limit=20, before_id=None)
            fresh = [row for row in reversed(rows) if last_id is None or int(row["id"]) > last_id]
            for row in fresh:
                last_id = int(row["id"])
                yield {"id": str(last_id), "event": "organization-event", "data": json.dumps(row, ensure_ascii=False, default=str)}
            yield {"event": "heartbeat", "data": json.dumps({"authority_version": live.authority_version})}
            await asyncio.sleep(1)

    return EventSourceResponse(stream(), headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _websocket_protocol_token(websocket: WebSocket) -> Optional[str]:
    offered = [
        value.strip()
        for value in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if value.strip()
    ]
    if len(offered) != 2 or offered[0] != "omnirank-auth" or len(offered[1]) < 20:
        return None
    return offered[1]


@router.websocket("/ws")
async def organization_ws(websocket: WebSocket):
    # WebSocket bypasses HTTP middleware. Reuse JWT decode read-only, then
    # re-resolve organization authority on every heartbeat/message.
    from auth.jwt_utils import decode_jwt
    token = _websocket_protocol_token(websocket)
    if not token:
        await websocket.close(code=4401)
        return
    payload = decode_jwt(token)
    if not payload or not _permission_version_is_current(payload.get("user_id", 0), payload.get("perm_version")):
        await websocket.close(code=4401)
        return
    try:
        identity = resolve_identity(int(payload["user_id"]))
    except Exception:
        await websocket.close(code=4403)
        return
    if identity is None:
        await websocket.close(code=4404)
        return
    initial_authority = identity.authority_version
    await websocket.accept(subprotocol="omnirank-auth")
    try:
        while True:
            try:
                live = resolve_identity(identity.authenticated_user_id)
            except OrganizationError:
                live = None
            if (
                live is None
                or live.authority_version != initial_authority
                or not _permission_version_is_current(identity.authenticated_user_id, payload.get("perm_version"))
            ):
                await websocket.send_json({"type": "permission-revoked", "code": "ORG_AUTHORITY_CHANGED"})
                await websocket.close(code=4403)
                return
            await websocket.send_json({"type": "heartbeat", "authority_version": live.authority_version})
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=1)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        return


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"


@admin_router.get("")
def product_config(request: Request):
    return get_product_config(actor_user_id=_user_id(request))


@admin_router.post("/publish")
def publish_product(req: ProductConfigPublishRequest, request: Request):
    return publish_product_config(
        actor_user_id=_user_id(request),
        request_id=req.request_id,
        expected_version=req.expected_version,
        included_seats=req.included_seats,
        invite_ttl_hours=req.invite_ttl_hours,
        verification_ttl_minutes=req.verification_ttl_minutes,
        verification_max_attempts=req.verification_max_attempts,
        reason=req.reason,
    )


@public_router.post("/invites/inspect")
def inspect_public(req: PublicInviteTokenRequest, request: Request, response: Response):
    _set_no_store(response)
    return inspect_public_invite(
        token=req.token,
        request_id=req.request_id,
        source_ip=_source_ip(request),
    )


@public_router.post("/invites/verification-challenges")
def create_public_challenge(req: PublicInviteTokenRequest, request: Request, response: Response):
    _set_no_store(response)
    return create_verification_challenge(
        token=req.token,
        request_id=req.request_id,
        source_ip=_source_ip(request),
    )


@public_router.post("/invites/verification-challenges/{challenge_id}/verify")
def verify_public_challenge(
    challenge_id: int,
    req: PublicInviteVerifyRequest,
    request: Request,
    response: Response,
):
    _set_no_store(response)
    return verify_challenge(
        challenge_id=challenge_id,
        token=req.token,
        code=req.code,
        request_id=req.request_id,
        source_ip=_source_ip(request),
    )


@public_router.post("/invites/onboard")
def onboard_public_operator(
    req: PublicInviteOnboardRequest,
    request: Request,
    response: Response,
):
    _set_no_store(response)
    return onboard_operator(
        token=req.token,
        challenge_id=req.challenge_id,
        verification_receipt=req.verification_receipt,
        password=req.password,
        display_name=req.display_name,
        request_id=req.request_id,
        terms_accepted=req.terms_accepted,
        privacy_accepted=req.privacy_accepted,
        terms_version=req.terms_version,
        privacy_version=req.privacy_version,
        source_ip=_source_ip(request),
        user_agent=str(request.headers.get("user-agent") or "")[:2000],
    )

@public_router.post("/invites/onboard-credential")
def onboard_public_operator_credential(
    req: PublicInviteCredentialOnboardRequest,
    request: Request,
    response: Response,
):
    # [WP6] 用户名式邀请开户:凭 token 直接自设初始密码(无短信验证码)。仅对
    # target_kind='username' 的邀请生效(服务层 fail-closed 校验)。
    _set_no_store(response)
    return onboard_operator_via_credential(
        token=req.token,
        password=req.password,
        display_name=req.display_name,
        request_id=req.request_id,
        terms_accepted=req.terms_accepted,
        privacy_accepted=req.privacy_accepted,
        terms_version=req.terms_version,
        privacy_version=req.privacy_version,
        source_ip=_source_ip(request),
        user_agent=str(request.headers.get("user-agent") or "")[:2000],
    )


@public_router.post("/links/validate")
def public_link(req: PublicTokenValidationRequest, response: Response):
    # Capability tokens never enter the request URI or default access logs.
    # Successful validation is also explicitly non-cacheable so revocation is
    # enforced by a fresh server check rather than a browser/CDN copy.
    _set_no_store(response)
    return validate_public_token(purpose=req.purpose, token=req.token)
