"""V3.5 W3 · 代理协议签约 API · 3 endpoints"""
import logging
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from db.connection import get_db
from services.agent_agreement import (
    get_agreement_status,
    sign_agreement,
    reject_agreement,
    CURRENT_VERSION,
    CURRENT_CONTENT_HASH,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-V35-W3-Agreement-API")
router = APIRouter(prefix="/api/agent/agreement", tags=["V35-W3-代理协议"])


class SignRequest(BaseModel):
    version: Optional[str] = None
    content_hash: Optional[str] = None


class RejectRequest(BaseModel):
    version: Optional[str] = None
    reason: Optional[str] = None


def _require_agent(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    user_id = user.get("user_id") or current_user_id(user)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        level = (row.get("agent_level") if row and isinstance(row, dict) else (row[0] if row else 0)) or 0
    if level < 1 and not user.get("is_admin"):
        # 结构化 code · 前端 AgreementGate 据此区分"非代理"(not_agent)与"接口失败"(error)
        raise HTTPException(403, detail={"code": "NOT_AGENT", "message": "仅服务方可访问"})
    return {"user_id": user_id}


def _public_agreement_status(result: dict) -> dict:
    allowed = {
        "version", "status", "signed_at", "rejected_at", "rejected_reason",
        "can_show_dialog", "created_at",
    }
    return {key: result.get(key) for key in allowed if key in result}


@router.get("/v35-status")
async def get_status(request: Request, version: Optional[str] = None):
    a = _require_agent(request)
    v = version or CURRENT_VERSION
    if v != CURRENT_VERSION:
        raise HTTPException(status_code=400, detail="仅可查询当前服务商协议版本")
    with get_db() as conn:
        cur = conn.cursor()
        result = get_agreement_status(cur, a["user_id"], v)
    return _public_agreement_status(result)


@router.post("/v35-sign")
async def sign(req: SignRequest, request: Request):
    a = _require_agent(request)
    v = req.version or CURRENT_VERSION
    if v != CURRENT_VERSION:
        raise HTTPException(status_code=400, detail="仅可签署当前服务商协议版本")
    if req.content_hash != CURRENT_CONTENT_HASH:
        raise HTTPException(status_code=400, detail="协议正文版本已更新,请刷新页面后重新确认")
    signed_ip = request.headers.get("X-Real-IP") or (request.client.host if request.client else None)
    signed_ua = request.headers.get("User-Agent")
    try:
        with get_db() as conn:
            cur = conn.cursor()
            result = sign_agreement(
                cur, agent_user_id=a["user_id"], version=v,
                signed_ip=signed_ip, signed_ua=signed_ua, content_hash=req.content_hash,
            )
            conn.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _public_agreement_status(result)


@router.post("/v35-reject")
async def reject(req: RejectRequest, request: Request):
    a = _require_agent(request)
    v = req.version or CURRENT_VERSION
    if v != CURRENT_VERSION:
        raise HTTPException(status_code=400, detail="仅可拒绝当前服务商协议版本")
    with get_db() as conn:
        cur = conn.cursor()
        result = reject_agreement(cur, agent_user_id=a["user_id"], version=v, reason=req.reason)
        conn.commit()
    return _public_agreement_status(result)
