"""Organization shared-payer policy HTTP contracts (owner SSOT + ADMIN emergency off).

Registered beside ``api.organization_api`` by the integrator; the route guard in
``middleware.organization_guard`` already resolves ``request.state.user`` and
``request.state.organization_identity`` for ``/api/organization/*``.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from services.organization_contract import OrganizationError
from services.organization_billing import force_release_charge
from services.organization_payer_policy import get_payer_policy, put_payer_policy


router = APIRouter(prefix="/api/organization", tags=["组织共享钱包"])


class PayerPolicyPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shared_payer_enabled: bool
    overage_enabled: bool
    per_action_limit_points: Optional[int] = Field(default=None, ge=0)
    daily_limit_points: Optional[int] = Field(default=None, ge=0)
    monthly_limit_points: Optional[int] = Field(default=None, ge=0)
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(ge=0)
    request_id: str = Field(min_length=8, max_length=160)
    # Platform ADMIN emergency-disable target only; owners always act on their
    # own organization and may not pass a foreign organization id.
    organization_id: Optional[int] = Field(default=None, gt=0)


def _user(request: Request) -> dict:
    user = getattr(request.state, "user", None) or {}
    if not (user.get("user_id") or user.get("id")):
        raise OrganizationError("ORG_AUTH_REQUIRED", "未登录", http_status=401)
    return user


def _source_ip(request: Request) -> Optional[str]:
    client = request.client
    return str(client.host).strip() if client and str(client.host or "").strip() else None


class ForceReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=8, max_length=160)


@router.post("/charges/{charge_link_id}/force-release")
def force_release_quarantined_charge(
    req: ForceReleaseRequest,
    request: Request,
    charge_link_id: int = Path(gt=0),
):
    """人工解决 unknown 隔离 charge：仅老板或平台管理员，仅 unknown+租约过期。

    按不可变快照 release 冻结腿并写完整审计；force-settle 刻意不提供
    （无真实 outcome 不得确认消费）。
    """
    user = _user(request)
    actor_user_id = int(user.get("user_id") or user.get("id"))
    is_platform_admin = bool(user.get("is_admin"))
    identity = getattr(request.state, "organization_identity", None)
    if identity is None and not is_platform_admin:
        raise OrganizationError("ORG_MEMBERSHIP_REQUIRED", "当前账号尚未加入组织", http_status=404)
    return asyncio.run(
        force_release_charge(
            identity=identity,
            actor_user_id=actor_user_id,
            is_platform_admin=is_platform_admin,
            charge_link_id=int(charge_link_id),
            reason=req.reason,
            request_id=req.request_id,
            source_ip=_source_ip(request),
        )
    )


@router.get("/payer-policy")
def read_payer_policy(request: Request):
    _user(request)
    identity = getattr(request.state, "organization_identity", None)
    if identity is None:
        raise OrganizationError("ORG_MEMBERSHIP_REQUIRED", "当前账号尚未加入组织", http_status=404)
    return get_payer_policy(identity)


@router.put("/payer-policy")
def write_payer_policy(req: PayerPolicyPutRequest, request: Request):
    user = _user(request)
    actor_user_id = int(user.get("user_id") or user.get("id"))
    is_platform_admin = bool(user.get("is_admin"))
    identity = getattr(request.state, "organization_identity", None)
    if identity is None and not is_platform_admin:
        raise OrganizationError("ORG_MEMBERSHIP_REQUIRED", "当前账号尚未加入组织", http_status=404)
    return put_payer_policy(
        identity=identity,
        actor_user_id=actor_user_id,
        is_platform_admin=is_platform_admin,
        organization_id=req.organization_id,
        shared_payer_enabled=req.shared_payer_enabled,
        overage_enabled=req.overage_enabled,
        per_action_limit_points=req.per_action_limit_points,
        daily_limit_points=req.daily_limit_points,
        monthly_limit_points=req.monthly_limit_points,
        reason=req.reason,
        expected_version=req.expected_version,
        request_id=req.request_id,
        source_ip=_source_ip(request),
    )
