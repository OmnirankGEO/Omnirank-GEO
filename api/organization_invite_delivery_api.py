"""Organization invite delivery status HTTP contracts (W1).

独立 router 文件，避免与 W2 在 api/organization_api.py 上的并行改动冲突。
集成者需在 server.py 注册（片段见交付报告）：

    from api.organization_invite_delivery_api import (
        router as organization_invite_delivery_router,
        public_router as organization_invite_delivery_public_router,
    )
    app.include_router(organization_invite_delivery_router)
    app.include_router(organization_invite_delivery_public_router)
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from api.organization_api import _identity, _set_no_store, _source_ip
from services.organization_onboarding import (
    get_challenge_delivery_status,
    list_invite_delivery_states,
)


router = APIRouter(prefix="/api/organization", tags=["组织邀请送达"])
public_router = APIRouter(prefix="/api/public/organization", tags=["组织公开链接"])


class PublicChallengeDeliveryStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=512)
    request_id: str = Field(min_length=8, max_length=160)


class InviteDeliveryStatesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invite_ids: List[int] = Field(min_length=1, max_length=200)


@public_router.post("/invites/verification-challenges/{challenge_id}/delivery-status")
def challenge_delivery_status(
    challenge_id: int,
    req: PublicChallengeDeliveryStatusRequest,
    request: Request,
    response: Response,
):
    _set_no_store(response)
    return get_challenge_delivery_status(
        challenge_id=challenge_id,
        token=req.token,
        request_id=req.request_id,
        source_ip=_source_ip(request),
    )


@router.post("/invites/delivery-states")
def invite_delivery_states(req: InviteDeliveryStatesRequest, request: Request):
    return {
        "items": list_invite_delivery_states(
            _identity(request),
            invite_ids=req.invite_ids,
        )
    }
