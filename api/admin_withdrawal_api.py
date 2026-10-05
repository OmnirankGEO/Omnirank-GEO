"""
管理员提现审核 API
- 提现列表（含统计）
- 审批通过 / 驳回 / 确认打款
"""

import logging
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from db.withdrawal_db import (
    admin_list_withdrawals,
    admin_approve,
    admin_reject,
    admin_mark_paid,
)
from db.auth_db import create_audit_log

logger = logging.getLogger("GEO-AdminWithdrawal-API")

router = APIRouter(prefix="/api/admin/withdrawals", tags=["管理-提现审核"])


# ==================== 辅助函数 ====================

def _get_admin_user(request: Request) -> dict:
    """提取管理员用户，非管理员返回 403"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    user["id"] = user.get("user_id") or user.get("id")
    return user


# ==================== 请求模型 ====================

class RejectRequest(BaseModel):
    reason: str


class PaidRequest(BaseModel):
    external_tx_ref: str


# ==================== 端点 ====================

@router.get("")
async def list_withdrawals(
    request: Request,
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
):
    """管理端提现列表"""
    _get_admin_user(request)
    real_limit = min(limit, 100)
    offset = (max(page, 1) - 1) * real_limit
    result = admin_list_withdrawals(
        status_filter=status,
        limit=real_limit,
        offset=offset,
    )
    return result


@router.get("/{withdrawal_id}/detail")
async def get_withdrawal_detail(withdrawal_id: int, request: Request):
    """管理端提现详情（含完整银行卡信息、代理实名、联系方式）"""
    _get_admin_user(request)
    from db.connection import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT wr.*,
                   u.username, u.phone as user_phone, u.display_name,
                   bc.card_holder, bc.bank_name, bc.card_number_encrypted, bc.card_number_mask, bc.phone as card_phone
            FROM withdrawal_requests wr
            JOIN users u ON u.id = wr.user_id
            JOIN bank_cards bc ON bc.id = wr.bank_card_id
            WHERE wr.id = %s
        """, (withdrawal_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="提现记录不存在")
        detail = dict(row)

        # 解密完整银行卡号（管理员有权限看）
        try:
            from services.kyc_crypto import decrypt_id_card
            detail["card_number_full"] = decrypt_id_card(detail.pop("card_number_encrypted", ""))
        except Exception:
            detail["card_number_full"] = detail.get("card_number_mask", "解密失败")
            detail.pop("card_number_encrypted", None)

        # 查实名信息
        real_name = None
        cursor.execute("""
            SELECT real_name FROM agent_applications
            WHERE user_id = %s AND status = 'approved'
            ORDER BY created_at DESC LIMIT 1
        """, (detail["user_id"],))
        app_row = cursor.fetchone()
        if app_row:
            real_name = app_row["real_name"]
        else:
            cursor.execute("""
                SELECT withdrawal_kyc_real_name FROM user_wallets WHERE user_id = %s
            """, (detail["user_id"],))
            w_row = cursor.fetchone()
            if w_row:
                real_name = w_row.get("withdrawal_kyc_real_name")
        detail["real_name"] = real_name

        # 佣金来源概要：最近的已结算佣金
        cursor.execute("""
            SELECT pc.amount_yuan, pc.level, pc.settled_at,
                   u2.username as source_username, u2.phone as source_phone
            FROM pending_commissions pc
            JOIN users u2 ON u2.id = pc.source_user_id
            WHERE pc.user_id = %s AND pc.status = 'settled'
            ORDER BY pc.settled_at DESC LIMIT 10
        """, (detail["user_id"],))
        detail["recent_commissions"] = [dict(r) for r in cursor.fetchall()]

        # 历史提现统计
        cursor.execute("""
            SELECT COUNT(*) as total_count,
                   COALESCE(SUM(actual_yuan), 0) as total_paid
            FROM withdrawal_requests
            WHERE user_id = %s AND status = 'paid'
        """, (detail["user_id"],))
        hist = cursor.fetchone()
        detail["history_paid_count"] = hist["total_count"]
        detail["history_paid_total"] = float(hist["total_paid"])

        # 序列化时间字段
        for key in ["created_at", "updated_at", "reviewed_at", "paid_at"]:
            if detail.get(key):
                detail[key] = detail[key].isoformat()

        return detail
    finally:
        conn.close()


@router.post("/{withdrawal_id}/approve")
async def approve_withdrawal(withdrawal_id: int, request: Request):
    """审批通过"""
    user = _get_admin_user(request)
    result = admin_approve(withdrawal_id, user["id"])

    create_audit_log(
        user_id=user["id"],
        username=user.get("username"),
        action="withdrawal_approve",
        module="withdrawal",
        entity_type="withdrawal_request",
        entity_id=withdrawal_id,
        summary=f"审批通过提现 ¥{result.get('amount_yuan', '')}",
        ip_address=request.client.host if request.client else None,
    )

    return {"success": True, "withdrawal": result}


@router.post("/{withdrawal_id}/reject")
async def reject_withdrawal(withdrawal_id: int, req: RejectRequest, request: Request):
    """驳回提现"""
    user = _get_admin_user(request)
    result = admin_reject(withdrawal_id, user["id"], req.reason)

    create_audit_log(
        user_id=user["id"],
        username=user.get("username"),
        action="withdrawal_reject",
        module="withdrawal",
        entity_type="withdrawal_request",
        entity_id=withdrawal_id,
        summary=f"驳回提现 ¥{result.get('amount_yuan', '')}，原因: {req.reason}",
        ip_address=request.client.host if request.client else None,
    )

    return {"success": True, "withdrawal": result}


@router.post("/{withdrawal_id}/paid")
async def mark_paid(withdrawal_id: int, req: PaidRequest, request: Request):
    """确认打款"""
    user = _get_admin_user(request)
    result = admin_mark_paid(withdrawal_id, user["id"], req.external_tx_ref)

    create_audit_log(
        user_id=user["id"],
        username=user.get("username"),
        action="withdrawal_paid",
        module="withdrawal",
        entity_type="withdrawal_request",
        entity_id=withdrawal_id,
        summary=f"确认打款 ¥{result.get('actual_yuan', '')}，流水号: {req.external_tx_ref}",
        ip_address=request.client.host if request.client else None,
    )

    return {"success": True, "withdrawal": result}
