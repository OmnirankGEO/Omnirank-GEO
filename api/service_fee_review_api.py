"""
V3.3.1 服务费 财务审核后台 API

7 个 endpoint:
    GET  /api/admin/service-fee/withdraw-requests              提现申请列表
    POST /api/admin/service-fee/withdraw-requests/:id/approve  批准提现(+凭证)
    POST /api/admin/service-fee/withdraw-requests/:id/reject   拒绝(+理由)
    POST /api/admin/service-fee/withdraw-requests/:id/partial  部分结算
    GET  /api/admin/service-fee/conversion-reviews             大额转换列表
    POST /api/admin/service-fee/conversion-reviews/:id/approve 批准大额转换
    POST /api/admin/service-fee/clawback/manual                手动 clawback(异常)

权限:
- 必须 admin role 'finance_reviewer'
- 操作全部 audit log

关联:
- 决策书 §3.3
- RED_LINES R6
- IDENTITY_DECISIONS_LOCK Q14-Q16
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from db.connection import get_db
from config.v3_3_1_flags import (
    is_v3_3_1_enabled,
    is_withdrawal_enabled,
    ServiceFeeStatus,
    is_anomalous_refund,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-ServiceFee-Review-API")

router = APIRouter(prefix="/api/admin/service-fee", tags=["服务费审核 (V3.3.1)"])


def _require_finance_reviewer(request: Request) -> dict:
    """要求 admin role 'finance_reviewer' 或超级管理员"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    roles = (user.get("roles") if isinstance(user, dict) else getattr(user, "roles", None)) or []
    role_names = {r["name"] if isinstance(r, dict) else r for r in roles}
    if "finance_reviewer" not in role_names and "admin" not in role_names:
        raise HTTPException(status_code=403, detail="需要财务审核员或管理员权限")
    return user


def _audit_log(action: str, **fields):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rbac_route_audit (user_id, user_role, method, path, response_code, blocked_reason)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fields.get("user_id"),
                        "finance_reviewer",
                        "POST",
                        action,
                        200,
                        None,
                    ),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("audit_log %s failed: %s", action, exc)


# ============================================
# Models
# ============================================

class ApproveRequest(BaseModel):
    note: Optional[str] = Field("", description="审核备注")
    settlement_evidence: Optional[str] = Field("", description="转账凭证 URL(可后传)")
    second_signer_id: Optional[int] = Field(None, description="双签场景:第二签字人 user_id")


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=2, description="拒绝理由")


class PartialApproveRequest(BaseModel):
    partial_amount_yuan: float = Field(..., gt=0, description="部分结算金额")
    note: Optional[str] = Field("", description="审核备注")
    settlement_evidence: Optional[str] = Field("", description="转账凭证 URL")


class ConversionReviewRequest(BaseModel):
    note: Optional[str] = Field("", description="审核备注")
    approve: bool = Field(..., description="是否批准")


class ManualClawbackRequest(BaseModel):
    user_id: int = Field(..., description="被追索 L2 user_id")
    source_order_id: str = Field(..., description="源订单 ID")
    refund_amount_yuan: float = Field(..., gt=0)
    refund_reason: str = Field(..., description="必须是 legal_dispute / platform_force / fraud / chargeback")


# ============================================
# 提现审核
# ============================================

@router.get("/withdraw-requests")
async def list_withdraw_requests(
    request: Request,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    _require_finance_reviewer(request)
    if not is_v3_3_1_enabled():
        return {"success": False, "error": "feature_disabled"}

    where = "settlement_type = 'withdrawal'"
    params: list = []
    if status:
        where += " AND status = %s"
        params.append(status)
    else:
        where += " AND status IN ('pending', 'approved', 'rejected', 'partial', 'completed')"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.id, s.user_id, s.amount_yuan, s.status,
                       s.bank_account_masked, s.invoice_required, s.invoice_uploaded,
                       s.requires_dual_sign, s.second_signer_id, s.second_signed_at,
                       s.reviewed_by, s.reviewed_at, s.review_note,
                       s.completed_at, s.settlement_evidence, s.partial_amount_yuan,
                       s.created_at,
                       uw.is_kyc_passed, uw.bank_account_verified, uw.agent_tier
                  FROM service_fee_settlements s
                  LEFT JOIN user_wallets uw ON uw.user_id = s.user_id
                 WHERE {where}
                 ORDER BY s.created_at DESC
                 LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) AS c FROM service_fee_settlements WHERE {where}", tuple(params))
            tr = cur.fetchone()
            total = int((tr["c"] if isinstance(tr, dict) else tr[0]) or 0)

    def _r(r):
        d = dict(r) if isinstance(r, dict) else {}
        for k in ("created_at", "reviewed_at", "completed_at", "second_signed_at"):
            if d.get(k) and isinstance(d[k], datetime):
                d[k] = d[k].isoformat()
        for k in ("amount_yuan", "partial_amount_yuan"):
            if d.get(k) is not None:
                d[k] = float(d[k])
        return d

    return {"success": True, "total": total, "data": [_r(r) for r in rows]}


@router.post("/withdraw-requests/{settlement_id}/approve")
async def approve_withdraw(settlement_id: int, req: ApproveRequest, request: Request):
    reviewer = _require_finance_reviewer(request)
    reviewer_id = current_user_id(reviewer) if isinstance(reviewer, dict) else getattr(reviewer, "id", None)

    if not is_v3_3_1_enabled() or not is_withdrawal_enabled():
        raise HTTPException(status_code=503, detail={"error": "feature_disabled"})

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM service_fee_settlements WHERE id = %s FOR UPDATE",
                (settlement_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="提现申请不存在")
            s = dict(row) if isinstance(row, dict) else row

            if s["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"当前状态 {s['status']} · 不可批准")

            # 双签校验
            if s["requires_dual_sign"]:
                if not req.second_signer_id:
                    raise HTTPException(status_code=400, detail="此提现需双签 · 请传 second_signer_id")
                if req.second_signer_id == reviewer_id:
                    raise HTTPException(status_code=400, detail="双签人不可与第一签字人相同")
                cur.execute(
                    """
                    UPDATE service_fee_settlements
                       SET status = 'approved', reviewed_by = %s, reviewed_at = NOW(),
                           review_note = %s, settlement_evidence = %s,
                           second_signer_id = %s, second_signed_at = NOW(),
                           completed_at = NOW()
                     WHERE id = %s
                    """,
                    (reviewer_id, req.note, req.settlement_evidence,
                     req.second_signer_id, settlement_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE service_fee_settlements
                       SET status = 'approved', reviewed_by = %s, reviewed_at = NOW(),
                           review_note = %s, settlement_evidence = %s,
                           completed_at = NOW()
                     WHERE id = %s
                    """,
                    (reviewer_id, req.note, req.settlement_evidence, settlement_id),
                )

            # 推进 service_fee_records 状态 withdraw_requested → withdrawn
            related_ids = s.get("related_record_ids") or []
            if related_ids:
                cur.execute(
                    """
                    UPDATE service_fee_records
                       SET status = %s, withdrawn_at = NOW()
                     WHERE id = ANY(%s) AND status = %s
                    """,
                    (ServiceFeeStatus.WITHDRAWN, list(related_ids),
                     ServiceFeeStatus.WITHDRAW_REQUESTED),
                )

            conn.commit()
    _audit_log("approve_withdraw", user_id=reviewer_id, settlement_id=settlement_id)
    return {"success": True, "settlement_id": settlement_id, "status": "approved"}


@router.post("/withdraw-requests/{settlement_id}/reject")
async def reject_withdraw(settlement_id: int, req: RejectRequest, request: Request):
    reviewer = _require_finance_reviewer(request)
    reviewer_id = current_user_id(reviewer) if isinstance(reviewer, dict) else getattr(reviewer, "id", None)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM service_fee_settlements WHERE id = %s FOR UPDATE",
                (settlement_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="提现申请不存在")
            s = dict(row) if isinstance(row, dict) else row
            if s["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"当前状态 {s['status']} · 不可拒绝")

            cur.execute(
                """
                UPDATE service_fee_settlements
                   SET status = 'rejected', reviewed_by = %s, reviewed_at = NOW(),
                       review_note = %s
                 WHERE id = %s
                """,
                (reviewer_id, req.reason, settlement_id),
            )

            # 锁定的 service_fee_records 退回 settled
            related_ids = s.get("related_record_ids") or []
            if related_ids:
                cur.execute(
                    """
                    UPDATE service_fee_records
                       SET status = %s, withdraw_requested_at = NULL,
                           clawback_reason = %s
                     WHERE id = ANY(%s) AND status = %s
                    """,
                    (ServiceFeeStatus.SETTLED,
                     f"withdraw_rejected:{req.reason}",
                     list(related_ids),
                     ServiceFeeStatus.WITHDRAW_REQUESTED),
                )

            conn.commit()
    _audit_log("reject_withdraw", user_id=reviewer_id, settlement_id=settlement_id, reason=req.reason)
    return {"success": True, "settlement_id": settlement_id, "status": "rejected"}


@router.post("/withdraw-requests/{settlement_id}/partial")
async def partial_approve(settlement_id: int, req: PartialApproveRequest, request: Request):
    reviewer = _require_finance_reviewer(request)
    reviewer_id = current_user_id(reviewer) if isinstance(reviewer, dict) else getattr(reviewer, "id", None)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM service_fee_settlements WHERE id = %s FOR UPDATE",
                (settlement_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="提现申请不存在")
            s = dict(row) if isinstance(row, dict) else row

            if s["status"] != "pending":
                raise HTTPException(status_code=400, detail=f"当前状态 {s['status']} · 不可部分结算")

            if req.partial_amount_yuan >= float(s["amount_yuan"]):
                raise HTTPException(status_code=400, detail="部分金额需 < 申请金额 · 否则用 approve")

            cur.execute(
                """
                UPDATE service_fee_settlements
                   SET status = 'partial', reviewed_by = %s, reviewed_at = NOW(),
                       review_note = %s, settlement_evidence = %s,
                       partial_amount_yuan = %s, completed_at = NOW()
                 WHERE id = %s
                """,
                (reviewer_id, req.note, req.settlement_evidence,
                 req.partial_amount_yuan, settlement_id),
            )

            # Codex 二审 P1-7 修复:部分结算 · FIFO 把 partial_amount 实扣 withdrawn ·
            # 剩余金额返还 settled · 不再"全部 withdrawn"(账目不准)
            related_ids = s.get("related_record_ids") or []
            partial_amount = Decimal(str(req.partial_amount_yuan))
            if related_ids:
                # 锁定相关 records · FIFO 顺序消化 partial_amount
                cur.execute(
                    """
                    SELECT id, amount_yuan FROM service_fee_records
                     WHERE id = ANY(%s) AND status = %s
                     ORDER BY id ASC
                     FOR UPDATE
                    """,
                    (list(related_ids), ServiceFeeStatus.WITHDRAW_REQUESTED),
                )
                locked = cur.fetchall()
                remaining = partial_amount
                for rr in locked:
                    rid = rr["id"] if isinstance(rr, dict) else rr[0]
                    ramount = Decimal(str(rr["amount_yuan"] if isinstance(rr, dict) else rr[1]))
                    if remaining <= 0:
                        # 后续 records 已不需要提现 → 退回 settled
                        cur.execute(
                            """
                            UPDATE service_fee_records
                               SET status = %s,
                                   withdraw_requested_at = NULL,
                                   review_note = %s
                             WHERE id = %s
                            """,
                            (ServiceFeeStatus.SETTLED,
                             f"partial_settlement_rolled_back:s{settlement_id}",
                             rid),
                        )
                        continue
                    if ramount <= remaining:
                        # 全额 withdrawn
                        cur.execute(
                            """
                            UPDATE service_fee_records
                               SET status = %s, withdrawn_at = NOW(),
                                   review_note = %s
                             WHERE id = %s
                            """,
                            (ServiceFeeStatus.WITHDRAWN,
                             f"partial_settlement:s{settlement_id}",
                             rid),
                        )
                        remaining -= ramount
                    else:
                        # 拆分:remaining 部分 withdrawn · 剩余部分回 settled
                        cur.execute(
                            """
                            INSERT INTO service_fee_records (
                                user_id, source_user_id, source_order_id, source_order_type,
                                source_payment_method, source_payment_source,
                                net_cash_revenue_yuan,
                                amount_yuan, service_fee_rate, status,
                                available_at, settled_at, withdrawn_at,
                                review_note
                            )
                            SELECT user_id, source_user_id,
                                   source_order_id || '_partialwd_' || %s::text || '_' || EXTRACT(EPOCH FROM NOW())::bigint::text,
                                   source_order_type,
                                   source_payment_method, source_payment_source,
                                   0,
                                   %s, service_fee_rate, %s,
                                   available_at, settled_at, NOW(),
                                   'partial_split_from:' || %s::text
                              FROM service_fee_records WHERE id = %s
                            """,
                            (rid, float(remaining), ServiceFeeStatus.WITHDRAWN, rid, rid),
                        )
                        # 剩余部分回 settled
                        cur.execute(
                            """
                            UPDATE service_fee_records
                               SET status = %s,
                                   amount_yuan = amount_yuan - %s,
                                   withdraw_requested_at = NULL,
                                   review_note = %s
                             WHERE id = %s
                            """,
                            (ServiceFeeStatus.SETTLED, float(remaining),
                             f"partial_remainder_back_to_settled:s{settlement_id}",
                             rid),
                        )
                        remaining = Decimal("0")

            conn.commit()
    _audit_log("partial_approve_withdraw", user_id=reviewer_id, settlement_id=settlement_id)
    return {"success": True, "settlement_id": settlement_id, "status": "partial"}


# ============================================
# 大额转换审批
# ============================================

@router.get("/conversion-reviews")
async def list_conversion_reviews(request: Request, limit: int = 50, offset: int = 0):
    _require_finance_reviewer(request)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, amount_yuan, bonus_rate,
                       requires_review, review_status, reviewed_by, reviewed_at,
                       created_at
                  FROM service_fee_conversion_orders
                 WHERE requires_review = TRUE
                   AND review_status IN ('pending', 'approved', 'rejected')
                 ORDER BY created_at DESC
                 LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
    def _r(r):
        d = dict(r) if isinstance(r, dict) else {}
        for k in ("created_at", "reviewed_at"):
            if d.get(k) and isinstance(d[k], datetime):
                d[k] = d[k].isoformat()
        if d.get("amount_yuan") is not None:
            d["amount_yuan"] = float(d["amount_yuan"])
        return d
    return {"success": True, "data": [_r(r) for r in rows]}


@router.post("/conversion-reviews/{review_id}/decide")
async def decide_conversion_review(review_id: int, req: ConversionReviewRequest, request: Request):
    """Codex 三审 P1-3 单订单闭环:
    - approve → approve_large_conversion(同 record 填充 amount/points/records · 不产生第二条)
    - reject → reject_large_conversion(同 record status='cancelled')
    - 跳过 quota 检查(已审批通过)
    """
    reviewer = _require_finance_reviewer(request)
    reviewer_id = current_user_id(reviewer) if isinstance(reviewer, dict) else getattr(reviewer, "id", None)

    from services.service_fee_engine import (
        approve_large_conversion,
        reject_large_conversion,
        ServiceFeeError,
    )

    try:
        if req.approve:
            result = approve_large_conversion(review_id, reviewer_id)
            _audit_log("decide_conversion_review_approved",
                       user_id=reviewer_id, review_id=review_id)
            return {"success": True, "conversion_completed": True, **result}
        else:
            result = reject_large_conversion(review_id, reviewer_id,
                                             reason=req.note or "拒绝")
            _audit_log("decide_conversion_review_rejected",
                       user_id=reviewer_id, review_id=review_id)
            return {"success": True, **result}
    except ServiceFeeError as exc:
        raise HTTPException(
            status_code=getattr(exc, "http_status", 400),
            detail=exc.to_dict() if hasattr(exc, "to_dict") else {"error": str(exc)},
        )


# ============================================
# 手动 clawback(异常)
# ============================================

@router.post("/clawback/manual")
async def manual_clawback(req: ManualClawbackRequest, request: Request):
    reviewer = _require_finance_reviewer(request)
    reviewer_id = current_user_id(reviewer) if isinstance(reviewer, dict) else getattr(reviewer, "id", None)

    if not is_anomalous_refund(req.refund_reason):
        raise HTTPException(
            status_code=400,
            detail=f"refund_reason 必须是 legal_dispute / platform_force / fraud / chargeback · 实得 {req.refund_reason}",
        )

    from services.service_fee_engine import clawback_after_conversion
    result = clawback_after_conversion(
        req.user_id, req.source_order_id, req.refund_amount_yuan, req.refund_reason
    )
    _audit_log("manual_clawback", user_id=reviewer_id, target_user=req.user_id,
               order_id=req.source_order_id)
    return {"success": True, **result}
