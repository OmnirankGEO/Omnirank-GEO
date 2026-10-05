"""Admin refund work order API.

Submitting a work order only records the review request. The existing wallet
refund action is reused exclusively by the explicit execute endpoint.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path as FsPath
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from db.connection import get_db
from db.refund_work_order_db import (
    build_refund_order_preview,
    count_refund_work_order_attachments,
    create_refund_work_order,
    default_timeline,
    find_order_for_refund_query,
    get_refund_work_order,
    list_refund_work_orders,
    requires_payout_proof,
    save_action_error,
    save_execution_result,
    set_payout_proof_url,
    attach_refund_work_order_file,
    transition_refund_work_order,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("GEO-RefundWorkOrder-API")
router = APIRouter(prefix="/api/wallet/refund-work-orders", tags=["钱包-退款工单"])

REFUND_METHOD_LABELS = {
    "manual_wechat": "人工微信退款",
    "offline_transfer": "线下转账",
    "original_route_pending": "原路退款待接入",
    "system_only": "仅系统冲账不打款",
}

EVIDENCE_TYPES = {
    "chat_record", "payment_proof", "customer_request",
    "agent_confirmation", "payout_proof", "channel_refund_proof", "other",
}

ALLOWED_UPLOAD_TYPES = {
    "image/jpeg": ("image", "jpg"),
    "image/png": ("image", "png"),
    "image/webp": ("image", "webp"),
    "application/pdf": ("pdf", "pdf"),
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _require_admin(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    return {
        "user_id": user.get("user_id") or current_user_id(user),
        "username": user.get("username"),
        "is_admin": True,
    }


def _require_work_order(work_order_id: int) -> dict:
    work_order = get_refund_work_order(work_order_id)
    if not work_order:
        raise HTTPException(status_code=404, detail="退款工单不存在")
    return work_order


def _status_counts(items: list[dict]) -> dict:
    counts = {k: 0 for k in ["draft", "submitted", "approved", "payout_pending", "completed", "rejected", "cancelled"]}
    for item in items:
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _safe_name(name: str) -> str:
    base = FsPath(name or "evidence").name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return base[:80] or "evidence"


def _preview_for_refund_method(preview: dict, refund_method: str) -> dict:
    needs_manual_payout = refund_method != "system_only"
    impact = {
        **(preview.get("impact") or {}),
        "needs_manual_payout": needs_manual_payout,
    }
    checks = []
    for check in preview.get("checks") or []:
        if check.get("key") == "payout_proof":
            checks.append({
                **check,
                "label": "需要人工打款凭证" if needs_manual_payout else "无需人工打款凭证",
                "status": "warn" if needs_manual_payout else "pass",
            })
        else:
            checks.append(check)
    return {
        **preview,
        "impact": impact,
        "checks": checks,
        "refund_method_label": REFUND_METHOD_LABELS[refund_method],
    }


class RefundWorkOrderInput(BaseModel):
    source_order_id: str = Field(..., min_length=1)
    refund_reason_category: str = "other"
    refund_reason_detail: str = ""
    refund_method: str = "manual_wechat"
    requested_refund_cents: int = 0
    customer_requested_at: Optional[datetime] = None
    agent_confirmed_at: Optional[datetime] = None
    process_note: str = ""


class WorkOrderActionRequest(BaseModel):
    note: str = ""


class ExecuteRefundWorkOrderRequest(BaseModel):
    confirm_execute: bool = False
    note: str = ""


class RejectRefundWorkOrderRequest(BaseModel):
    reason: str


def _payload_from_input(req: RefundWorkOrderInput) -> dict:
    preview = build_refund_order_preview(req.source_order_id)
    if not preview.get("found"):
        raise HTTPException(status_code=404, detail=preview.get("message") or "订单不存在")
    order = preview["order"]
    impact = preview["impact"]
    if order.get("payment_status") != "paid":
        raise HTTPException(status_code=400, detail="订单未支付，不能创建退款工单")
    if req.refund_method not in REFUND_METHOD_LABELS:
        raise HTTPException(status_code=400, detail="退款方式不合法")
    requested = int(req.requested_refund_cents or impact["estimated_refund_cents"] or 0)
    original_amount = int(impact.get("original_amount_cents") or 0)
    if requested < 0:
        raise HTTPException(status_code=400, detail="退款金额不能为负数")
    if original_amount and requested > original_amount:
        raise HTTPException(status_code=400, detail="预计退款金额不能超过原订单金额")
    snapshot = _preview_for_refund_method(preview, req.refund_method)
    return {
        "source_order_id": order["id"],
        "customer_user_id": order.get("customer_user_id"),
        "agent_user_id": order.get("agent_user_id"),
        "refund_reason_category": req.refund_reason_category or "other",
        "refund_reason_detail": req.refund_reason_detail or req.process_note or "",
        "refund_method": req.refund_method,
        "requested_refund_cents": requested,
        "estimated_refund_cents": int(impact.get("estimated_refund_cents") or 0),
        "refundable_power": int(impact.get("refundable_power") or 0),
        "customer_requested_at": req.customer_requested_at,
        "agent_confirmed_at": req.agent_confirmed_at,
        "impact_snapshot": snapshot,
    }


@router.get("")
async def list_admin_refund_work_orders(
    request: Request,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    _require_admin(request)
    items = list_refund_work_orders(status=status, limit=limit, offset=offset)
    all_for_counts = list_refund_work_orders(status=None, limit=500, offset=0)
    return {"success": True, "items": items, "counts": _status_counts(all_for_counts)}


@router.get("/orders/preview")
async def preview_refund_order(request: Request, query: str = Query(..., min_length=1)):
    _require_admin(request)
    order_id = find_order_for_refund_query(query)
    if not order_id:
        raise HTTPException(status_code=404, detail="未找到匹配订单")
    preview = build_refund_order_preview(order_id)
    if not preview.get("found"):
        raise HTTPException(status_code=404, detail=preview.get("message") or "订单不存在")
    return {"success": True, **preview}


@router.get("/{work_order_id}")
async def get_admin_refund_work_order(work_order_id: int, request: Request):
    _require_admin(request)
    return {"success": True, "work_order": _require_work_order(work_order_id)}


@router.post("/drafts")
async def create_refund_work_order_draft(req: RefundWorkOrderInput, request: Request):
    admin = _require_admin(request)
    try:
        work_order = create_refund_work_order(_payload_from_input(req), admin["user_id"], status="draft")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"success": True, "work_order": get_refund_work_order(work_order["id"])}


@router.post("/{work_order_id}/submit")
async def submit_refund_work_order(work_order_id: int, req: WorkOrderActionRequest, request: Request):
    admin = _require_admin(request)
    work_order = _require_work_order(work_order_id)
    if work_order["status"] not in ("draft", "submitted"):
        raise HTTPException(status_code=400, detail="只有草稿工单可以提交审核")
    if count_refund_work_order_attachments(work_order_id) < 1:
        raise HTTPException(status_code=400, detail="请先上传至少 1 个退款凭证")
    updated = transition_refund_work_order(
        work_order_id, "submitted", admin["user_id"], note=req.note or "提交退款审核"
    )
    return {"success": True, "work_order": get_refund_work_order(updated["id"])}


@router.post("/{work_order_id}/approve")
async def approve_refund_work_order(work_order_id: int, req: WorkOrderActionRequest, request: Request):
    admin = _require_admin(request)
    work_order = _require_work_order(work_order_id)
    if work_order["status"] != "submitted":
        raise HTTPException(status_code=400, detail="只有待审核工单可以审核通过")
    updated = transition_refund_work_order(
        work_order_id, "approved", admin["user_id"], note=req.note or "审核通过"
    )
    return {"success": True, "work_order": get_refund_work_order(updated["id"])}


@router.post("/{work_order_id}/reject")
async def reject_refund_work_order(work_order_id: int, req: RejectRefundWorkOrderRequest, request: Request):
    admin = _require_admin(request)
    work_order = _require_work_order(work_order_id)
    if work_order["status"] not in ("draft", "submitted", "approved"):
        raise HTTPException(status_code=400, detail="已执行系统冲账或已结束的工单不能驳回")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE refund_work_orders
            SET status = 'rejected',
                rejected_reason = %s,
                reviewed_by = %s,
                reviewed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING id
        """, (req.reason, admin["user_id"], work_order_id))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="退款工单不存在")
        from db.refund_work_order_db import add_work_order_event
        add_work_order_event(cur, work_order_id, "rejected", admin["user_id"], req.reason)
    return {"success": True, "work_order": get_refund_work_order(work_order_id)}


@router.post("/{work_order_id}/execute")
async def execute_refund_work_order(work_order_id: int, req: ExecuteRefundWorkOrderRequest, request: Request):
    admin = _require_admin(request)
    work_order = _require_work_order(work_order_id)
    if work_order["status"] != "approved":
        raise HTTPException(status_code=400, detail="只有已审核工单可以执行系统冲账")
    if not req.confirm_execute:
        raise HTTPException(status_code=400, detail="执行系统冲账前必须二次确认")

    mandatory_categories = {
        "statutory_seven_day", "duplicate_charge", "not_credited",
        "not_delivered", "system_failure", "legal_required",
    }
    if work_order.get("refund_reason_category") not in mandatory_categories:
        direct_service_evidence = any(
            item.get("evidence_type") == "agent_confirmation"
            for item in (work_order.get("attachments") or [])
        )
        if not (work_order.get("agent_confirmed_at") or direct_service_evidence):
            raise HTTPException(
                status_code=409,
                detail="协商退款须先取得原订单直属服务方的同意证据；平台审核不能代替该决定",
            )

    try:
        from api.wallet_api import RefundRequest, request_refund
        refund_req = RefundRequest(
            order_id=work_order["source_order_id"],
            reason=work_order.get("refund_reason_detail") or "退款工单审核通过",
            reason_category=work_order.get("refund_reason_category") or "negotiated_other",
            evidence={"refund_work_order_id": work_order_id, "review_note": req.note},
        )
        result = await request_refund(refund_req, request)
    except HTTPException as exc:
        save_action_error(work_order_id, str(exc.detail))
        raise
    except Exception as exc:
        logger.exception("[RefundWorkOrder] execute failed work_order=%s", work_order_id)
        save_action_error(work_order_id, str(exc))
        raise HTTPException(status_code=500, detail=f"系统冲账失败: {str(exc)[:160]}")

    next_status = "completed" if work_order.get("refund_method") == "system_only" else "payout_pending"
    updated = save_execution_result(work_order_id, admin["user_id"], result, next_status)
    return {"success": True, "work_order": get_refund_work_order(updated["id"]), "execution_result": result}


@router.post("/{work_order_id}/attachments")
async def upload_refund_work_order_attachment(
    work_order_id: int,
    request: Request,
    evidence_type: str = Form("other"),
    file: UploadFile = File(...),
):
    admin = _require_admin(request)
    work_order = _require_work_order(work_order_id)
    if work_order["status"] == "completed":
        raise HTTPException(status_code=400, detail="已完成工单不能再上传凭证")
    if evidence_type not in EVIDENCE_TYPES:
        raise HTTPException(status_code=400, detail="凭证类型不合法")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="单个凭证不能超过 10MB")
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_UPLOAD_TYPES:
        suffix = FsPath(file.filename or "").suffix.lower().lstrip(".")
        if suffix == "pdf":
            mime = "application/pdf"
        elif suffix in ("jpg", "jpeg"):
            mime = "image/jpeg"
        elif suffix == "png":
            mime = "image/png"
        elif suffix == "webp":
            mime = "image/webp"
    if mime not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(status_code=400, detail="仅支持图片或 PDF 凭证")

    file_type, ext = ALLOWED_UPLOAD_TYPES[mime]
    base_name = _safe_name(file.filename or f"evidence.{ext}")
    if "." not in base_name:
        base_name = f"{base_name}.{ext}"
    upload_dir = FsPath("uploads") / "refund-work-orders" / str(work_order_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{int(datetime.utcnow().timestamp() * 1000)}_{base_name}"
    target = (upload_dir / stored_name).resolve()
    if upload_dir.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail="文件名不合法")
    target.write_bytes(raw)
    file_url = f"/uploads/refund-work-orders/{work_order_id}/{stored_name}"

    attachment = attach_refund_work_order_file(
        work_order_id,
        {
            "file_url": file_url,
            "file_name": file.filename or base_name,
            "file_type": file_type,
            "evidence_type": evidence_type,
            "mime_type": mime,
            "file_size_bytes": len(raw),
        },
        admin["user_id"],
    )
    if evidence_type == "payout_proof":
        set_payout_proof_url(work_order_id, file_url, admin["user_id"])
    return {"success": True, "attachment": attachment, "work_order": get_refund_work_order(work_order_id)}


@router.post("/{work_order_id}/complete")
async def complete_refund_work_order(work_order_id: int, req: WorkOrderActionRequest, request: Request):
    admin = _require_admin(request)
    work_order = _require_work_order(work_order_id)
    method_label = REFUND_METHOD_LABELS.get(work_order.get("refund_method"), "")
    if work_order["status"] not in ("payout_pending", "completed"):
        raise HTTPException(status_code=400, detail="只有待打款工单可以标记完成")
    if work_order["status"] == "completed":
        return {"success": True, "work_order": work_order}

    if requires_payout_proof(work_order.get("refund_method", "")):
        has_payout_proof = bool(work_order.get("payout_proof_url")) or count_refund_work_order_attachments(work_order_id, "payout_proof") > 0
        if not has_payout_proof:
            raise HTTPException(status_code=400, detail="请先上传打款凭证，再标记完成")
    elif method_label == "仅系统冲账不打款":
        pass

    # [v11 F3] 单事务原子完成:锁工单 + 订单,UPDATE ... RETURNING 命中校验,canonical 冲销,
    #   工单终态 + 事件一并提交。任一步未命中(订单不在可完成态 / 工单并发变更)→ raise → 整事务回滚 → 不显示完成。
    #   消除旧版"UPDATE recharge_orders 不查 rowcount + 另一事务标工单 completed" 的假完成缺口。
    from db.refund_work_order_db import _transition_refund_work_order_cur, assert_refund_completion_allowed_cur
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, status FROM refund_work_orders WHERE id = %s FOR UPDATE", (work_order_id,))
        _wo = cur.fetchone()
        if not _wo:
            raise HTTPException(status_code=404, detail="退款工单不存在")
        _wo_status = _wo["status"] if isinstance(_wo, dict) else _wo[1]
        if _wo_status == "completed":
            return {"success": True, "work_order": get_refund_work_order(work_order_id)}
        if _wo_status != "payout_pending":
            raise HTTPException(status_code=400, detail="只有待打款工单可以标记完成")
        try:
            order = assert_refund_completion_allowed_cur(
                cur, work_order_id, work_order["source_order_id"], work_order,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cur.execute("""
            UPDATE recharge_orders
            SET refund_status = 'completed'
            WHERE id = %s
                  AND refund_status IS NOT DISTINCT FROM %s
            RETURNING id
        """, (work_order["source_order_id"], order.get("refund_status")))
        _upd = cur.fetchone()
        if _upd is None:
            raise HTTPException(status_code=409, detail="退款订单状态并发变化·请刷新后重试")
        # [v10 item6] canonical 冲销渠道收益(幂等·仅生效态真冲)· 同事务。补齐进货订单退款漏冲 pre-existing gap。
        from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund
        reverse_channel_revenue_on_refund(cur, work_order["source_order_id"])
        # [v11 F3] 工单终态与事件在【同一事务】提交(不再另开事务)
        _transition_refund_work_order_cur(
            cur, work_order_id, "completed", admin["user_id"], note=req.note or "退款工单完成"
        )
    return {
        "success": True,
        "work_order": {
            **get_refund_work_order(work_order_id),
            "timeline": default_timeline("completed"),
        },
    }
