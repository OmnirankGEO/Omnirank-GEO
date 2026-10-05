"""Dealer inventory resale APIs.

Dealer reads are constrained to orders where the actor is the direct seller or
buyer.  Admin endpoints are separate and are the only place that exposes a full
chain, internal user ids, lot cost, or opening-balance governance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from db.connection import get_db
from services import dealer_inventory_resale as resale
from auth.user_ctx import current_user_id


router = APIRouter(tags=["Dealer inventory resale"])
RefundProviderInput = Literal[
    "wechat", "wechat_pay", "wechat_jsapi", "wechat_native",
    "xunhupay", "xunhupay_manual", "xunhupay_callback",
]


class DealerPolicyPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    downstream_markup_bps: int = Field(ge=10000, le=1_000_000)
    expected_row_version: int = Field(ge=1)
    policy_version: str = Field(min_length=1, max_length=128)


class AdminPolicyPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    downstream_markup_bps: int = Field(ge=10000, le=1_000_000)
    authorized_min_markup_bps: int = Field(ge=10000, le=1_000_000)
    authorized_max_markup_bps: int = Field(ge=10000, le=1_000_000)
    policy_version: str = Field(min_length=1, max_length=128)
    expected_row_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)


class AdminSettingsPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_seller_user_id: int = Field(gt=0)
    default_downstream_markup_bps: int = Field(ge=10000, le=1_000_000)
    settings_version: str = Field(min_length=1, max_length=128)
    expected_row_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)


class OpeningLotPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_agent_user_id: int = Field(gt=0)
    points: int = Field(gt=0, le=9_000_000_000)
    acquisition_cost_cents: int = Field(gt=0, le=2_000_000_000)
    pricing_version: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    evidence: Dict[str, Any]


class ManufacturerLotIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points: int = Field(gt=0, le=9_000_000_000)
    acquisition_cost_cents: int = Field(gt=0, le=2_000_000_000)
    pricing_version: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    evidence: Dict[str, Any]
    promotion_campaign_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class AdminPromotionPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    funding_scope: str = Field(pattern="^(PLATFORM|SELLER)$")
    seller_user_id: Optional[int] = Field(default=None, gt=0)
    product_code: str = Field(min_length=1, max_length=128)
    root_catalog_version: str = Field(min_length=1, max_length=128)
    promotion_discount_bps: int = Field(ge=1, le=10000)
    eligibility: Dict[str, Any] = Field(default_factory=dict)
    status: str = Field(pattern="^(draft|active)$")
    campaign_version: str = Field(min_length=1, max_length=128)
    started_at: datetime
    expires_at: datetime
    expected_row_version: Optional[int] = Field(default=None, ge=0)


class AdminPromotionEndRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_row_version: int = Field(ge=1)


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=500)
    acknowledge_whole_lot_and_72h: bool
    reason_category: str = Field(default="voluntary_unused_inventory", max_length=64)
    processing_cost_cents: int = Field(default=0, ge=0, le=2_000_000_000)
    processing_cost_evidence: Dict[str, Any] = Field(default_factory=dict)
    mandatory_evidence: Dict[str, Any] = Field(default_factory=dict)


class ManualRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)
    evidence: Dict[str, Any]


class ExternalRefundMaterialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: int = Field(gt=0, le=2_000_000_000)
    provider: RefundProviderInput
    external_refund_id: str = Field(min_length=1, max_length=128)
    external_completed_at: datetime
    evidence: Dict[str, Any]


class ConsumerRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)
    reason_category: str = Field(default="negotiated_other", max_length=64)
    evidence: Dict[str, Any] = Field(default_factory=dict)


class ConsumerRefundReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str = Field(default="", max_length=1000)


class ConsumerRefundCashCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: int = Field(gt=0, le=2_000_000_000)
    provider: RefundProviderInput
    external_refund_id: str = Field(min_length=1, max_length=128)
    evidence: Dict[str, Any]


class RefundCashExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=80)


def _user(request: Request) -> Dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    user_id = user.get("user_id") or current_user_id(user)
    if not user_id:
        raise HTTPException(401, "登录信息不完整")
    return {**user, "user_id": int(user_id), "is_admin": bool(user.get("is_admin"))}


def _dealer(request: Request) -> Dict[str, Any]:
    user = _user(request)
    if user["is_admin"]:
        return user
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent_level FROM user_wallets WHERE user_id=%s", (user["user_id"],))
        row = cur.fetchone()
        level = int((row.get("agent_level") if isinstance(row, dict) else row[0]) or 0) if row else 0
    if level < 1:
        raise HTTPException(403, "仅经销商或服务方可访问逐级库存转售")
    return user


def _admin(request: Request) -> Dict[str, Any]:
    user = _user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "仅管理员可操作")
    return user


def _raise(exc: resale.ResaleError, *, expose_internal: bool = False) -> None:
    if exc.code == "FORBIDDEN":
        status = 403
    elif exc.code.endswith("NOT_FOUND"):
        status = 404
    elif "VERSION_CONFLICT" in exc.code or exc.code in {
        "REFUND_ALREADY_EXISTS", "REFUND_STATE_INVALID", "RESALE_ORDER_CONFLICT",
    }:
        status = 409
    elif exc.code in {"RESALE_SCHEMA_NOT_READY", "RESALE_SETTINGS_MISSING"}:
        status = 503
    else:
        status = 422
    if expose_internal:
        detail = {"code": exc.code, "message": exc.message}
    elif status == 403:
        detail = {"code": "FORBIDDEN", "message": "无权执行该操作"}
    elif status == 404:
        detail = {"code": "NOT_FOUND", "message": "记录不存在或不可访问"}
    elif status == 503:
        detail = {"code": "PLATFORM_SERVICE_UNAVAILABLE", "message": "平台服务暂不可用，请稍后重试"}
    elif status == 409:
        detail = {"code": "REQUEST_CONFLICT", "message": "订单状态已变化，请刷新后重试"}
    else:
        detail = {"code": "REQUEST_NOT_AVAILABLE", "message": "当前请求暂不能处理，请联系平台服务"}
    raise HTTPException(status, detail=detail) from exc


@router.get("/api/dealer-resale/orders")
def list_my_resale_orders(
    request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    actor = _dealer(request)
    with get_db() as conn:
        return resale.list_orders(
            conn.cursor(), actor_user_id=actor["user_id"],
            is_admin=actor["is_admin"], limit=limit, offset=offset,
        )


@router.get("/api/dealer-resale/orders/{order_id}")
def get_my_resale_order(order_id: str, request: Request):
    actor = _dealer(request)
    try:
        with get_db() as conn:
            return resale.get_order_visible(
                conn.cursor(), order_id=order_id, actor_user_id=actor["user_id"],
                is_admin=actor["is_admin"],
            )
    except resale.ResaleError as exc:
        _raise(exc)


@router.get("/api/dealer-resale/orders/{order_id}/export")
def export_my_resale_order(order_id: str, request: Request):
    """A one-order JSON export with the exact same ownership filter as detail."""
    actor = _dealer(request)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            item = resale.get_order_visible(
                cur, order_id=order_id, actor_user_id=actor["user_id"],
                is_admin=actor["is_admin"],
            )
            cur.execute("SELECT clock_timestamp() AS exported_at")
            exported_row = cur.fetchone()
            exported_at = exported_row.get("exported_at") if isinstance(exported_row, dict) else exported_row[0]
        return jsonable_encoder({"format": "dealer-resale-order-v1", "item": item, "exported_at": exported_at})
    except resale.ResaleError as exc:
        _raise(exc)


@router.get("/api/dealer-resale/policy")
def get_my_resale_policy(request: Request):
    actor = _dealer(request)
    with get_db() as conn:
        return resale.get_effective_policy(conn.cursor(), actor["user_id"])


@router.put("/api/dealer-resale/policy")
def update_my_resale_policy(body: DealerPolicyPutRequest, request: Request):
    actor = _dealer(request)
    if actor["is_admin"]:
        raise HTTPException(403, "管理员请使用 admin policy 端点并显式指定 seller")
    try:
        with get_db() as conn:
            cur = conn.cursor()
            result = resale.save_policy_dealer(
                cur, seller_user_id=actor["user_id"],
                downstream_markup_bps=body.downstream_markup_bps,
                expected_row_version=body.expected_row_version,
                policy_version=body.policy_version,
            )
            conn.commit()
            return result
    except resale.ResaleError as exc:
        _raise(exc)


@router.post("/api/dealer-resale/orders/{order_id}/refund")
def request_b2b_resale_refund(order_id: str, body: RefundRequest, request: Request):
    actor = _dealer(request)
    if not body.acknowledge_whole_lot_and_72h:
        raise HTTPException(
            422,
            detail={
                "code": "B2B_REFUND_ACK_REQUIRED",
                "message": "请确认：仅支付后 72 小时内、整批未使用且未向下转售才可自动退款",
            },
        )
    try:
        with get_db() as conn:
            cur = conn.cursor()
            result = resale.request_refund(
                cur, order_id=order_id, requested_by_user_id=actor["user_id"],
                reason=body.reason, is_admin=actor["is_admin"],
                reason_category=body.reason_category,
                processing_cost_cents=body.processing_cost_cents,
                processing_cost_evidence=body.processing_cost_evidence,
                mandatory_evidence=body.mandatory_evidence,
            )
            conn.commit()
            public_refund = {
                "refund_id": str(result["refund_id"]),
                "order_id": str(result["order_id"]),
                "status": str(result["status"]),
                "reason": str(result.get("reason") or ""),
                "requested_at": result.get("requested_at"),
                "deadline_at": result.get("deadline_at"),
                "refund_handling": "PLATFORM_MANAGED",
            }
            if actor["is_admin"]:
                public_refund.update({
                    "requested_by_user_id": int(result["requested_by_user_id"]),
                    "responsible_seller_user_id": int(result["responsible_seller_user_id"]),
                })
            return {
                "refund": public_refund,
                "cash_execution_required": True,
                "message": "本跳库存已冻结，等待平台按原支付通道执行现金退款",
            }
    except resale.ResaleError as exc:
        _raise(exc)


@router.get("/api/dealer-resale/profits/summary")
def get_my_resale_profit_summary(request: Request):
    actor = _dealer(request)
    with get_db() as conn:
        return resale.profit_summary(conn.cursor(), seller_user_id=actor["user_id"])


@router.post("/api/dealer-resale/consumer/orders/{order_id}/refunds")
def request_consumer_refund(
    order_id: str, body: ConsumerRefundRequest, request: Request,
):
    actor = _user(request)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            result = resale.request_consumer_refund_case(
                cur, order_id=order_id, consumer_user_id=actor["user_id"],
                reason=body.reason, reason_category=body.reason_category,
                evidence=body.evidence,
            )
            if result.get("status") == "platform_execution":
                result = resale.settle_consumer_refund_case(cur, case_id=str(result["case_id"]))
            public_result = resale.get_consumer_refund_visible(
                cur, case_id=str(result["case_id"]), actor_user_id=actor["user_id"],
                is_admin=actor["is_admin"],
            )
            conn.commit()
            return {
                "refund": public_result,
                "refund_handling": "PLATFORM_MANAGED",
                "b2b_72h_rule_applied": False,
                "internal_ledger_settled": result.get("status") == "platform_execution",
                "cash_completed": result.get("cash_status") == "completed",
                "message": (
                    "强制退款已进入平台核验和原支付路径执行，任何销售方均无权拒绝"
                    if result.get("decision_kind") == "mandatory"
                    else "协商退款已进入本单售后审核；批准前不改变现金、算力、库存或收益"
                ),
            }
    except resale.ResaleError as exc:
        _raise(exc)


@router.get("/api/dealer-resale/consumer/refunds/{case_id}")
def get_consumer_refund(case_id: str, request: Request):
    actor = _user(request)
    try:
        with get_db() as conn:
            return resale.get_consumer_refund_visible(
                conn.cursor(), case_id=case_id, actor_user_id=actor["user_id"],
                is_admin=actor["is_admin"],
            )
    except resale.ResaleError as exc:
        _raise(exc)


@router.post("/api/dealer-resale/consumer/refunds/{case_id}/review")
def review_consumer_refund(
    case_id: str, body: ConsumerRefundReviewRequest, request: Request,
):
    actor = _dealer(request)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            result = resale.review_consumer_refund_case(
                cur, case_id=case_id, actor_user_id=actor["user_id"],
                approve=body.approve, note=body.note, is_admin=actor["is_admin"],
            )
            if body.approve:
                result = resale.settle_consumer_refund_case(cur, case_id=str(result["case_id"]))
            public_result = resale.get_consumer_refund_visible(
                cur, case_id=str(result["case_id"]), actor_user_id=actor["user_id"],
                is_admin=actor["is_admin"],
            )
            conn.commit()
            return {
                "refund": public_result,
                "internal_ledger_settled": bool(body.approve),
                "cash_completed": result.get("cash_status") == "completed",
                "next_step": "PLATFORM_EXECUTION" if body.approve else "CLOSED_REJECTED",
            }
    except resale.ResaleError as exc:
        _raise(exc)


@router.post("/api/admin/dealer-resale/consumer/refunds/{case_id}/cash-complete")
def admin_complete_consumer_refund_cash(
    case_id: str, body: ConsumerRefundCashCompleteRequest, request: Request,
):
    _admin(request)
    if not body.evidence:
        raise HTTPException(422, detail="必须提交原支付渠道的退款证据")
    try:
        with get_db() as conn:
            cur = conn.cursor()
            result = resale.complete_consumer_refund_cash(
                cur, case_id=case_id, amount_cents=body.amount_cents,
                provider=body.provider, external_refund_id=body.external_refund_id,
                evidence=body.evidence,
            )
            public_result = resale.get_consumer_refund_visible(
                cur, case_id=case_id, actor_user_id=0, is_admin=True,
            )
            conn.commit()
            return {"refund": public_result, "cash_completed": True, "idempotent": result.get("cash_status") == "completed"}
    except resale.ResaleError as exc:
        _raise(exc, expose_internal=True)


@router.get("/api/admin/dealer-resale/refund-cash-jobs")
def admin_list_refund_cash_jobs(
    request: Request,
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    _admin(request)
    from services.refund_cash_execution import RefundExecutionError, list_cash_jobs

    try:
        with get_db() as conn:
            return list_cash_jobs(conn.cursor(), status=status, limit=limit, offset=offset)
    except RefundExecutionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.post("/api/admin/dealer-resale/refund-cash-jobs/{cash_job_id}/execute")
async def admin_execute_refund_cash_job(
    cash_job_id: str, body: RefundCashExecuteRequest, request: Request,
):
    admin = _admin(request)
    from services.refund_cash_execution import RefundExecutionError, execute_persisted_refund

    try:
        result = await execute_persisted_refund(
            cash_job_id=str(cash_job_id), reason=body.reason,
        )
        return {"refund_execution": result, "executed_by": int(admin["user_id"])}
    except RefundExecutionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.get("/api/admin/dealer-resale/readiness")
def admin_resale_readiness(request: Request):
    _admin(request)
    return resale.readiness()


@router.get("/api/admin/dealer-resale/settings")
def admin_get_resale_settings(request: Request):
    _admin(request)
    with get_db() as conn:
        return resale._global_settings(conn.cursor())


@router.put("/api/admin/dealer-resale/settings")
def admin_put_resale_settings(body: AdminSettingsPutRequest, request: Request):
    admin = _admin(request)
    try:
        with get_db() as conn:
            result = resale.save_global_settings(
                conn.cursor(), platform_seller_user_id=body.platform_seller_user_id,
                default_downstream_markup_bps=body.default_downstream_markup_bps,
                settings_version=body.settings_version,
                expected_row_version=body.expected_row_version, updated_by=admin["user_id"],
                reason=body.reason,
                request_id=(
                    getattr(request.state, "request_id", None)
                    or request.headers.get("X-Request-ID")
                    or str(uuid4())
                ),
                updated_by_username=admin.get("username"),
                ip_address=request.client.host if request.client else None,
            )
            conn.commit()
            return result
    except resale.ResaleError as exc:
        _raise(exc, expose_internal=True)


@router.put("/api/admin/dealer-resale/policies/{seller_user_id}")
def admin_put_resale_policy(
    seller_user_id: int, body: AdminPolicyPutRequest, request: Request,
):
    admin = _admin(request)
    try:
        with get_db() as conn:
            result = resale.save_policy_admin(
                conn.cursor(), seller_user_id=seller_user_id,
                downstream_markup_bps=body.downstream_markup_bps,
                min_markup_bps=body.authorized_min_markup_bps,
                max_markup_bps=body.authorized_max_markup_bps,
                policy_version=body.policy_version,
                expected_row_version=body.expected_row_version, updated_by=admin["user_id"],
                reason=body.reason,
                request_id=(
                    getattr(request.state, "request_id", None)
                    or request.headers.get("X-Request-ID")
                    or str(uuid4())
                ),
                updated_by_username=admin.get("username"),
                ip_address=request.client.host if request.client else None,
            )
            conn.commit()
            return result
    except resale.ResaleError as exc:
        _raise(exc, expose_internal=True)


@router.get("/api/admin/dealer-resale/opening-lots/dry-run")
def admin_opening_lot_dry_run(request: Request):
    _admin(request)
    with get_db() as conn:
        return resale.opening_lot_dry_run(conn.cursor())


@router.post("/api/admin/dealer-resale/opening-lots/prepare")
def admin_prepare_opening_lot(body: OpeningLotPrepareRequest, request: Request):
    _admin(request)
    try:
        with get_db() as conn:
            result = resale.prepare_opening_lot(conn.cursor(), **body.model_dump())
            conn.commit()
            return result
    except resale.ResaleError as exc:
        _raise(exc, expose_internal=True)


@router.post("/api/admin/dealer-resale/manufacturer-lots/issue")
def admin_issue_manufacturer_lot(body: ManufacturerLotIssueRequest, request: Request):
    """Explicit audited origin issuance; never automatic and never a production action here."""
    admin = _admin(request)
    try:
        with get_db() as conn:
            result = resale.issue_manufacturer_lot(
                conn.cursor(), **body.model_dump(), issued_by=admin["user_id"],
            )
            conn.commit()
            return result
    except resale.ResaleError as exc:
        _raise(exc, expose_internal=True)


@router.get("/api/admin/dealer-resale/promotions")
def admin_list_promotions(
    request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    _admin(request)
    with get_db() as conn:
        return resale.list_promotions_admin(conn.cursor(), limit=limit, offset=offset)


@router.put("/api/admin/dealer-resale/promotions/{campaign_id}")
def admin_put_promotion(campaign_id: str, body: AdminPromotionPutRequest, request: Request):
    admin = _admin(request)
    try:
        with get_db() as conn:
            result = resale.save_promotion_admin(
                conn.cursor(), campaign_id=campaign_id, **body.model_dump(),
                updated_by=admin["user_id"],
            )
            conn.commit()
            return result
    except resale.ResaleError as exc:
        _raise(exc)


@router.post("/api/admin/dealer-resale/promotions/{campaign_id}/end")
def admin_end_promotion(
    campaign_id: str, body: AdminPromotionEndRequest, request: Request,
):
    admin = _admin(request)
    try:
        with get_db() as conn:
            result = resale.end_promotion_admin(
                conn.cursor(), campaign_id=campaign_id,
                expected_row_version=body.expected_row_version, updated_by=admin["user_id"],
            )
            conn.commit()
            return result
    except resale.ResaleError as exc:
        _raise(exc)


@router.get("/api/admin/dealer-resale/orders")
def admin_list_resale_orders(
    request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    admin = _admin(request)
    with get_db() as conn:
        return resale.list_orders(
            conn.cursor(), actor_user_id=admin["user_id"], is_admin=True,
            limit=limit, offset=offset,
        )


@router.get("/api/admin/dealer-resale/orders/{order_id}/chain")
def admin_get_resale_chain(order_id: str, request: Request):
    _admin(request)
    with get_db() as conn:
        result = resale.admin_order_chain(conn.cursor(), order_id)
    if not result["count"]:
        raise HTTPException(404, "逐级转售订单不存在")
    return result


@router.post("/api/admin/dealer-resale/orders/{order_id}/manual-refund")
def admin_create_manual_refund(
    order_id: str, body: ManualRefundRequest, request: Request,
):
    admin = _admin(request)
    try:
        with get_db() as conn:
            result = resale.create_manual_refund_case(
                conn.cursor(), order_id=order_id, requested_by_user_id=admin["user_id"],
                reason=body.reason, evidence=body.evidence,
            )
            conn.commit()
            return {
                "refund": result,
                "automatic_inventory_or_cash_movement": False,
                "message": "已登记人工异常；未自动退款、未移动库存、未递归上游",
            }
    except resale.ResaleError as exc:
        _raise(exc, expose_internal=True)


@router.post("/api/admin/dealer-resale/orders/{order_id}/external-refund-complete")
def admin_record_external_resale_refund_material(
    order_id: str, body: ExternalRefundMaterialRequest, request: Request,
):
    """Register unverified material; never create a cash or inventory terminal."""
    admin = _admin(request)
    try:
        with get_db() as conn:
            result = resale.record_b2b_external_refund_material(
                conn.cursor(), order_id=order_id, amount_cents=body.amount_cents,
                provider=body.provider, external_refund_id=body.external_refund_id,
                external_completed_at=body.external_completed_at.isoformat(),
                evidence=body.evidence, recorded_by_user_id=admin["user_id"],
            )
            conn.commit()
            return {
                "refund": result,
                "cash_terminal_recorded": False,
                "automatic_inventory_or_profit_movement": False,
                "ancestor_orders_touched": 0,
            }
    except resale.ResaleError as exc:
        _raise(exc, expose_internal=True)
