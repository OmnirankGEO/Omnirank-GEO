"""
V3.3.1 身份 + 邀请码 API

Endpoints:
    GET  /api/identity/me                  当前用户身份(L0/L1/L2)
    POST /api/identity/invite-codes/issue  签发邀请码
    GET  /api/identity/invite-codes        我签发的邀请码列表
    POST /api/identity/invite-codes/verify 校验邀请码(注册前预校验)
    POST /api/identity/invite-codes/revoke 撤销邀请码

关联:
- 决策书 §1 §2 §5
- IDENTITY_DECISIONS_LOCK Q1-Q5
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from db.connection import get_db
from config.v3_3_1_flags import is_v3_3_1_enabled, is_invite_code_required
from services.identity_service import (
    classify_user,
    is_geo_eligible,
    issue_invite_code,
    verify_invite_code,
    InviteCodeError,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-V3.3.1-Identity-API")

router = APIRouter(prefix="/api/identity", tags=["身份 (V3.3.1)"])


def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


class IssueInviteCodeRequest(BaseModel):
    code_type: str = Field("user", description="user / agent")
    ttl_days: int = Field(30, ge=1, le=365)
    note: Optional[str] = None


class VerifyInviteCodeRequest(BaseModel):
    code: str


class RevokeInviteCodeRequest(BaseModel):
    code: str
    reason: Optional[str] = "manual_revoke"


@router.get("/me")
async def get_my_identity(request: Request):
    user = _get_user(request)
    user_id = current_user_id(user) if isinstance(user, dict) else user.id
    level = classify_user(user_id)
    return {
        "success": True,
        "user_id": user_id,
        "identity": level,
        "is_l0": level == "L0",
        "is_l1": level == "L1",
        "is_l2": level == "L2",
        "geo_eligible": is_geo_eligible(user_id),
        "v3_3_1_enabled": is_v3_3_1_enabled(),
        "invite_required": is_invite_code_required(),
    }


@router.post("/invite-codes/issue")
async def issue_code(req: IssueInviteCodeRequest, request: Request):
    user = _get_user(request)
    user_id = current_user_id(user) if isinstance(user, dict) else user.id
    try:
        result = issue_invite_code(
            user_id, code_type=req.code_type, ttl_days=req.ttl_days, note=req.note
        )
        return {"success": True, **result}
    except InviteCodeError as exc:
        raise HTTPException(status_code=getattr(exc, "http_status", 400),
                            detail={"error": getattr(exc, "code", "invite_code_error"),
                                    "message": str(exc)})


@router.get("/invite-codes")
async def list_my_codes(request: Request, limit: int = 50, offset: int = 0):
    user = _get_user(request)
    user_id = current_user_id(user) if isinstance(user, dict) else user.id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, code, code_type, expires_at, is_active,
                       revoked_at, revoked_reason, created_at
                  FROM invite_codes
                 WHERE user_id = %s
                 ORDER BY created_at DESC
                 LIMIT %s OFFSET %s
                """,
                (user_id, limit, offset),
            )
            rows = cur.fetchall()

    def _r(r):
        d = dict(r) if isinstance(r, dict) else {}
        for k in ("expires_at", "revoked_at", "created_at"):
            if d.get(k) and isinstance(d[k], datetime):
                d[k] = d[k].isoformat()
        return d

    return {"success": True, "data": [_r(r) for r in rows]}


@router.post("/invite-codes/verify")
async def verify_code(req: VerifyInviteCodeRequest):
    """注册前公开预校验 · 不需要登录"""
    try:
        info = verify_invite_code(req.code)
        return {"success": True, **info}
    except InviteCodeError as exc:
        raise HTTPException(status_code=getattr(exc, "http_status", 400),
                            detail={"error": getattr(exc, "code", "invite_code_error"),
                                    "message": str(exc)})


@router.post("/invite-codes/revoke")
async def revoke_code(req: RevokeInviteCodeRequest, request: Request):
    user = _get_user(request)
    user_id = current_user_id(user) if isinstance(user, dict) else user.id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE invite_codes
                   SET is_active = false, revoked_at = NOW(), revoked_reason = %s
                 WHERE code = %s AND user_id = %s AND is_active = true
                 RETURNING id
                """,
                (req.reason, req.code, user_id),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                raise HTTPException(status_code=404, detail="邀请码不存在或已撤销")
    return {"success": True, "code": req.code, "revoked": True}
