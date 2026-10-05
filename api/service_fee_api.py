"""
V3.3.1 服务费 用户侧 API

5 个 endpoint:
    GET  /api/service-fee/balance          余额 + 三段统计
    POST /api/service-fee/convert          转充值积分(+20% bonus · 月度额度内)
    POST /api/service-fee/convert-large    大额转换审批(超额走人工)
    POST /api/service-fee/withdraw-request 申请人工提现
    GET  /api/service-fee/history          8 态流水

关联:
- 决策书 §3.2 §3.3 §3.5
- RED_LINES R2
- IDENTITY_DECISIONS_LOCK Q12-Q22
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from db.connection import get_db
from config.v3_3_1_flags import (
    is_v3_3_1_enabled,
    is_service_fee_conversion_enabled,
    is_withdrawal_enabled,
    get_conversion_quota_yuan,
    get_conversion_bonus_rate,
    get_conversion_min_age_days,
    get_withdrawal_min_amount,
    ServiceFeeStatus,
)
from services.service_fee_engine import (
    convert_service_fee_to_points,
    request_large_conversion,
    request_withdrawal,
    get_settled_balance_yuan,
    get_pending_balance_yuan,
    get_monthly_used_yuan,
    is_l2_agent,
    get_user_wallet,
    ServiceFeeError,
)

logger = logging.getLogger("GEO-ServiceFee-API")

router = APIRouter(prefix="/api/service-fee", tags=["服务费 (V3.3.1)"])


def _get_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _user_id(user) -> int:
    if isinstance(user, dict):
        uid = user.get("user_id") or user.get("id")
    else:
        uid = getattr(user, "user_id", None) or getattr(user, "id", None)
    if uid is None:
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="此功能仅服务方可用")
    return uid_int


def _handle_service_fee_error(exc: ServiceFeeError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.to_dict())


# ============================================
# Models
# ============================================

class ConvertRequest(BaseModel):
    amount_yuan: float = Field(..., gt=0, description="转换金额(元)")


class ConvertLargeRequest(BaseModel):
    amount_yuan: float = Field(..., gt=0, description="申请转换金额(元)")
    reason: Optional[str] = Field("", description="申请理由")


class WithdrawalRequest(BaseModel):
    amount_yuan: float = Field(..., gt=0, description="提现金额(元)")
    bank_account_masked: str = Field(..., description="脱敏银行账号(如 招商银行 *** 1234)")


# ============================================
# Endpoints
# ============================================

@router.get("/balance")
async def get_balance(request: Request):
    """L2 服务费余额 + 三段统计 + 月度额度"""
    user = _get_user(request)
    user_id = _user_id(user)

    if not is_v3_3_1_enabled():
        return {
            "success": False,
            "error": "feature_disabled",
            "message": "V3.3.1 未启用",
        }

    if not is_l2_agent(user_id):
        # L1/L0 不显示服务费(决策书 §3.1.2 严禁字眼)
        return {
            "success": True,
            "is_l2": False,
            "data": {
                "pending_yuan": 0.0,
                "settled_yuan": 0.0,
                "converted_yuan": 0.0,
                "withdrawn_yuan": 0.0,
                "debt_yuan": 0.0,
            },
        }

    wallet = get_user_wallet(user_id)
    agent_tier = wallet.get("agent_tier", "standard") if wallet else "standard"

    pending = float(get_pending_balance_yuan(user_id))
    settled = float(get_settled_balance_yuan(user_id))
    used_this_month = float(get_monthly_used_yuan(user_id))
    quota_this_month = float(get_conversion_quota_yuan(agent_tier))

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status='converted' THEN amount_yuan ELSE 0 END), 0) AS converted_yuan,
                    COALESCE(SUM(CASE WHEN status='withdrawn' THEN amount_yuan ELSE 0 END), 0) AS withdrawn_yuan
                  FROM service_fee_records WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone() or {}
            converted_yuan = float(row["converted_yuan"] if isinstance(row, dict) else row[0])
            withdrawn_yuan = float(row["withdrawn_yuan"] if isinstance(row, dict) else row[1])

            cur.execute(
                """
                SELECT COALESCE(SUM(amount_due - amount_settled), 0) AS d
                  FROM service_fee_clawback_pending
                 WHERE user_id = %s AND status IN ('pending','partial_settled')
                """,
                (user_id,),
            )
            d = cur.fetchone()
            debt_yuan = float((d["d"] if isinstance(d, dict) else d[0]) or 0)

    return {
        "success": True,
        "is_l2": True,
        "agent_tier": agent_tier,
        "kyc_passed": bool(wallet.get("is_kyc_passed")) if wallet else False,
        "bank_account_verified": bool(wallet.get("bank_account_verified")) if wallet else False,
        "data": {
            "pending_yuan": pending,
            "settled_yuan": settled,
            "converted_yuan": converted_yuan,
            "withdrawn_yuan": withdrawn_yuan,
            "debt_yuan": debt_yuan,
        },
        "conversion": {
            "enabled": is_service_fee_conversion_enabled(),
            "bonus_rate": get_conversion_bonus_rate(),
            "min_age_days": get_conversion_min_age_days(),
            "monthly_quota_yuan": quota_this_month,
            "monthly_used_yuan": used_this_month,
            "monthly_available_yuan": max(0, quota_this_month - used_this_month),
        },
        "withdrawal": {
            "enabled": is_withdrawal_enabled(),
            "min_amount_yuan": get_withdrawal_min_amount(),
        },
    }


@router.post("/convert")
async def convert(req: ConvertRequest, request: Request):
    """服务费转充值积分(+赠送 bonus)"""
    user = _get_user(request)
    user_id = _user_id(user)
    try:
        result = convert_service_fee_to_points(user_id, req.amount_yuan)
        return {"success": True, **result}
    except ServiceFeeError as exc:
        raise _handle_service_fee_error(exc)


@router.post("/convert-large")
async def convert_large(req: ConvertLargeRequest, request: Request):
    """超月度额度的大额转换申请 · 走人工审批"""
    user = _get_user(request)
    user_id = _user_id(user)
    try:
        result = request_large_conversion(user_id, req.amount_yuan, reason=req.reason or "")
        return {"success": True, **result}
    except ServiceFeeError as exc:
        raise _handle_service_fee_error(exc)


@router.post("/withdraw-request")
async def withdraw_request(req: WithdrawalRequest, request: Request):
    """L2 申请人工提现 · 需 KYC + 实名账户"""
    user = _get_user(request)
    user_id = _user_id(user)
    try:
        result = request_withdrawal(
            user_id, req.amount_yuan,
            bank_account_masked=req.bank_account_masked,
        )
        return {"success": True, **result}
    except ServiceFeeError as exc:
        raise _handle_service_fee_error(exc)


@router.get("/history")
async def history(
    request: Request,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """8 态流水 · 支持 status 筛选"""
    user = _get_user(request)
    user_id = _user_id(user)

    if not is_v3_3_1_enabled():
        return {"success": False, "error": "feature_disabled"}

    if not is_l2_agent(user_id):
        return {"success": True, "data": [], "total": 0}

    where = "user_id = %s"
    params: list = [user_id]
    if status:
        where += " AND status = %s"
        params.append(status)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, source_user_id, source_order_id, source_order_type,
                       gross_amount_yuan, net_cash_revenue_yuan,
                       amount_yuan, service_fee_rate,
                       status, created_at, available_at, settled_at,
                       converted_at, withdraw_requested_at, withdrawn_at,
                       clawback_amount, clawback_reason
                  FROM service_fee_records
                 WHERE {where}
                 ORDER BY created_at DESC
                 LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = cur.fetchall()

            cur.execute(f"SELECT COUNT(*) AS c FROM service_fee_records WHERE {where}", tuple(params))
            total_row = cur.fetchone()
            total = int((total_row["c"] if isinstance(total_row, dict) else total_row[0]) or 0)

    def _row_to_dict(r):
        if isinstance(r, dict):
            d = dict(r)
        else:
            keys = [
                "id", "source_user_id", "source_order_id", "source_order_type",
                "gross_amount_yuan", "net_cash_revenue_yuan",
                "amount_yuan", "service_fee_rate",
                "status", "created_at", "available_at", "settled_at",
                "converted_at", "withdraw_requested_at", "withdrawn_at",
                "clawback_amount", "clawback_reason",
            ]
            d = dict(zip(keys, r))
        # iso datetime
        for k in ("created_at", "available_at", "settled_at", "converted_at",
                  "withdraw_requested_at", "withdrawn_at"):
            if d.get(k) and isinstance(d[k], datetime):
                d[k] = d[k].isoformat()
        # float
        for k in ("gross_amount_yuan", "net_cash_revenue_yuan", "amount_yuan",
                  "service_fee_rate", "clawback_amount"):
            if d.get(k) is not None:
                d[k] = float(d[k])
        return d

    return {
        "success": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [_row_to_dict(r) for r in rows],
    }
