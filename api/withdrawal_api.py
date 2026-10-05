"""
代理端提现 API
- KYC 实名认证
- 银行卡管理（增删改默认）
- 提现申请 / 查询
"""

import logging
import uuid as _uuid
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field, field_validator

from db.connection import get_connection
from db.withdrawal_db import (
    luhn_check,
    get_withdrawal_kyc_status,
    save_withdrawal_kyc,
    list_bank_cards,
    add_bank_card,
    delete_bank_card,
    set_default_card,
    create_withdrawal,
    list_withdrawals,
)

logger = logging.getLogger("GEO-Withdrawal-API")

router = APIRouter(prefix="/api/wallet", tags=["提现"])


# ==================== 辅助函数 ====================

def _get_agent_user(request: Request) -> dict:
    """提取当前登录的代理用户，非代理返回 403。
    agent_level 不在 JWT payload 里，需从 user_wallets 查询。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    user_id = user.get("user_id") or user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="请先登录")

    from db.wallet_db import get_or_create_wallet
    wallet = get_or_create_wallet(user_id)
    agent_level = wallet.get("agent_level", 0) or 0
    if agent_level < 1:
        raise HTTPException(status_code=403, detail="仅服务方可使用提现功能")

    user["id"] = user_id
    user["agent_level"] = agent_level
    return user


# ==================== 请求模型 ====================

class KycRequest(BaseModel):
    real_name: str


class BankCardRequest(BaseModel):
    card_holder: str
    bank_name: str
    card_number: str
    phone: str


class WithdrawalRequest(BaseModel):
    amount_yuan: float = Field(..., ge=100)
    bank_card_id: int
    idempotency_key: str

    @field_validator("idempotency_key")
    @classmethod
    def _validate_uuid(cls, v):
        # [BUG-P3] DB 列 withdrawal_requests.idempotency_key 为 UUID NOT NULL · 非 UUID 入参在
        # 幂等 SELECT 触发 "invalid input syntax for type uuid" → 500(且客户端换 key 重试可能重复冻结)。
        # 入口校验拒非法 → 干净 422,幂等重放命中已有记录。
        try:
            _uuid.UUID(str(v))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("idempotency_key 必须是合法 UUID 格式")
        return v


# ==================== 冻结明细 ====================

@router.get("/freeze-detail")
async def get_freeze_detail(request: Request):
    """返回当前代理的冻结/待入账明细拆分"""
    user = _get_agent_user(request)
    user_id = user["id"]

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 1. T+3 待入账佣金
        cursor.execute("""
            SELECT pc.amount_yuan, pc.level, pc.commission_rate,
                   pc.available_at, pc.created_at, pc.frozen_reason,
                   u.username, u.phone
            FROM pending_commissions pc
            JOIN users u ON u.id = pc.source_user_id
            WHERE pc.user_id = %s AND pc.status = 'pending'
            ORDER BY pc.available_at ASC
        """, (user_id,))
        pending_commissions = []
        for row in cursor.fetchall():
            r = dict(row)
            phone = r.get("phone") or ""
            masked = phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else r.get("username", "")
            pending_commissions.append({
                "source_display": masked,
                "amount_yuan": float(r["amount_yuan"]),
                "points": int(float(r["amount_yuan"]) * 130),
                "level": r["level"],
                "available_at": r["available_at"].isoformat() if r["available_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })

        # 2. 处理中的提现（pending/approved）
        cursor.execute("""
            SELECT wr.id, wr.amount_yuan, wr.fee_yuan, wr.actual_yuan,
                   wr.points_deducted, wr.status, wr.created_at,
                   bc.card_number_mask, bc.bank_name
            FROM withdrawal_requests wr
            JOIN bank_cards bc ON bc.id = wr.bank_card_id
            WHERE wr.user_id = %s AND wr.status IN ('pending', 'approved')
            ORDER BY wr.created_at DESC
        """, (user_id,))
        pending_withdrawals = []
        for row in cursor.fetchall():
            r = dict(row)
            pending_withdrawals.append({
                "id": r["id"],
                "amount_yuan": float(r["amount_yuan"]),
                "fee_yuan": float(r["fee_yuan"]),
                "actual_yuan": float(r["actual_yuan"]),
                "points_deducted": r["points_deducted"],
                "status": r["status"],
                "bank_name": r["bank_name"],
                "card_mask": r["card_number_mask"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })

        # 3. 任务冻结 = 总冻结 - 提现冻结
        cursor.execute("SELECT frozen_points FROM user_wallets WHERE user_id = %s", (user_id,))
        wallet = cursor.fetchone()
        total_frozen = wallet["frozen_points"] if wallet else 0
        withdrawal_frozen = sum(w["points_deducted"] for w in pending_withdrawals)
        task_frozen = max(0, total_frozen - withdrawal_frozen)

        return {
            "pending_commissions": pending_commissions,
            "pending_withdrawals": pending_withdrawals,
            "task_frozen_points": task_frozen,
        }
    finally:
        conn.close()


# ==================== KYC 端点 ====================

@router.get("/withdrawal-kyc")
async def get_kyc(request: Request):
    """获取提现 KYC 状态"""
    user = _get_agent_user(request)
    result = get_withdrawal_kyc_status(user["id"])
    return result


@router.post("/withdrawal-kyc")
async def submit_kyc(req: KycRequest, request: Request):
    """提交提现 KYC 实名信息（简化版，生产环境应使用 OCR）"""
    user = _get_agent_user(request)
    save_withdrawal_kyc(user["id"], req.real_name)
    return {"success": True, "message": "实名信息已保存"}


# ==================== 银行卡端点 ====================

@router.get("/bank-cards")
async def get_bank_cards(request: Request):
    """获取用户银行卡列表"""
    user = _get_agent_user(request)
    cards = list_bank_cards(user["id"])
    return {"cards": cards}


@router.post("/bank-cards")
async def add_card(req: BankCardRequest, request: Request):
    """添加银行卡"""
    user = _get_agent_user(request)

    # Luhn 校验
    if not luhn_check(req.card_number):
        raise HTTPException(status_code=400, detail="银行卡号格式不正确")

    # KYC 校验
    kyc = get_withdrawal_kyc_status(user["id"])
    if not kyc.get("kyc_verified"):
        raise HTTPException(status_code=403, detail="请先完成实名认证")

    # 持卡人姓名必须与实名一致
    if req.card_holder != kyc["real_name"]:
        raise HTTPException(status_code=400, detail="持卡人姓名与实名信息不一致")

    # 添加银行卡（DB 层处理加密 + 唯一约束）
    try:
        card = add_bank_card(
            user_id=user["id"],
            card_holder=req.card_holder,
            bank_name=req.bank_name,
            card_number=req.card_number,
            phone=req.phone,
        )
    except HTTPException as e:
        if e.status_code == 409:
            raise HTTPException(status_code=409, detail="该银行卡已被绑定")
        raise

    return {"success": True, "card": card}


@router.delete("/bank-cards/{card_id}")
async def remove_card(card_id: int, request: Request):
    """删除银行卡"""
    user = _get_agent_user(request)
    result = delete_bank_card(user["id"], card_id)
    if not result:
        raise HTTPException(status_code=400, detail="删除银行卡失败")
    return {"success": True, "message": "银行卡已删除"}


@router.patch("/bank-cards/{card_id}/default")
async def make_default_card(card_id: int, request: Request):
    """设置默认银行卡"""
    user = _get_agent_user(request)
    result = set_default_card(user["id"], card_id)
    if not result:
        raise HTTPException(status_code=400, detail="设置默认卡失败")
    return {"success": True, "message": "已设为默认银行卡"}


# ==================== 提现端点 ====================

@router.post("/withdrawal")
async def submit_withdrawal(req: WithdrawalRequest, request: Request):
    """提交提现申请"""
    user = _get_agent_user(request)
    result = create_withdrawal(
        user_id=user["id"],
        amount_yuan=req.amount_yuan,
        bank_card_id=req.bank_card_id,
        idempotency_key=req.idempotency_key,
    )

    return {"success": True, "withdrawal": result}


@router.get("/withdrawals")
async def get_withdrawals(
    request: Request,
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
):
    """查询提现记录"""
    user = _get_agent_user(request)
    real_limit = min(limit, 100)
    offset = (max(page, 1) - 1) * real_limit
    items = list_withdrawals(
        user_id=user["id"],
        status_filter=status,
        limit=real_limit,
        offset=offset,
    )
    return {"items": items, "page": page, "limit": real_limit}
