"""Persisted-amount refund execution for dealer and direct-service orders.

The request only identifies a durable refund intent.  Amount, payment route and
idempotency key are always reloaded under the order lock before a provider call.
Provider success becomes a cash terminal only after a signed callback or active
provider query is persisted with the canonical inventory/profit reversal.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional

from db.connection import get_db


logger = logging.getLogger("GEO-Refund-Cash-Execution")


def _enqueue_refund_terminal(cur, claim: Dict[str, Any], state: str, reason: str = "") -> None:
    from services.notification_events import NotificationEventType, RecipientKind
    from services.notification_outbox import (
        enqueue_admin_notification_events,
        enqueue_notification_event,
    )

    event_type = {
        "completed": NotificationEventType.REFUND_COMPLETED,
        "failed": NotificationEventType.REFUND_FAILED,
        "manual_required": NotificationEventType.REFUND_MANUAL_REQUIRED,
    }[state]
    facts = {
        "business_no": str(claim["order_id"]),
        "amount": f"{int(claim['amount_cents']) / 100:.2f} 元",
        "status": {
            "completed": "退款已完成",
            "failed": "退款处理未完成",
            "manual_required": "等待人工核验",
        }[state],
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if state == "failed":
        facts["reason"] = reason or "请在退款页面查看处理建议。"
    business_id = str(claim.get("cash_job_id") or claim.get("refund_id") or claim["order_id"])
    if claim["kind"] == "consumer":
        recipients = [(int(claim["consumer_user_id"]), RecipientKind.CUSTOMER)]
    else:
        recipients = [
            (int(claim["buyer_user_id"]), RecipientKind.TRADE_BUYER),
            (int(claim["seller_user_id"]), RecipientKind.TRADE_SELLER),
        ]
    for recipient_user_id, recipient_kind in recipients:
        enqueue_notification_event(
            cur,
            event_type=event_type,
            business_id=business_id,
            terminal_state=state,
            recipient_user_id=recipient_user_id,
            recipient_kind=recipient_kind,
            facts=facts,
        )
    if state in {"failed", "manual_required"}:
        enqueue_admin_notification_events(
            cur,
            event_type=event_type,
            business_id=business_id,
            terminal_state=state,
            facts=facts,
        )


class RefundExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _row(row) -> Dict[str, Any]:
    return dict(row) if row else {}


def _provider_for_route(route: str) -> str:
    normalized = str(route or "").strip().lower()
    if normalized.startswith("wechat"):
        return "wechat"
    if normalized == "xunhupay":
        return "xunhupay"
    raise RefundExecutionError(
        "REFUND_ROUTE_NOT_EXECUTABLE",
        "原支付路径不支持自动退款，已保留人工资金工单",
    )


def _yuan_to_cents(value: Any) -> int:
    try:
        raw_amount = Decimal(str(value))
        amount = raw_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not raw_amount.is_finite() or raw_amount != amount:
            raise ValueError("not an exact two-decimal amount")
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RefundExecutionError(
            "REFUND_PROVIDER_AMOUNT_INVALID",
            "支付渠道主动查询没有返回合法金额",
        ) from exc


def _integer_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError("not an integer-cent amount")
        return int(amount)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise RefundExecutionError(
            "REFUND_PROVIDER_AMOUNT_INVALID",
            "支付渠道主动查询没有返回合法整数分金额",
        ) from exc


def list_cash_jobs(
    cur, *, status: Optional[str] = None, limit: int = 100, offset: int = 0,
) -> Dict[str, Any]:
    allowed = {"queued", "provider_processing", "completed", "failed", "manual_review"}
    normalized = str(status or "").strip().lower()
    if normalized and normalized not in allowed:
        raise RefundExecutionError("REFUND_JOB_STATUS_INVALID", "退款工单状态筛选不合法", status_code=422)
    where = "WHERE j.status=%s" if normalized else ""
    params = [normalized] if normalized else []
    cur.execute(f"SELECT COUNT(*) AS c FROM service_refund_cash_jobs j {where}", tuple(params))
    total = int(_row(cur.fetchone()).get("c") or 0)
    cur.execute(
        f"""SELECT j.cash_job_id,j.case_id,j.source_order_id,j.consumer_user_id,
                    j.responsible_service_user_id,j.amount_cents,j.original_payment_route,
                    j.status,j.attempt_count,j.idempotency_key,j.provider_refund_id,
                    j.last_error,j.created_at,j.updated_at,j.completed_at,
                    CASE
                      WHEN j.status='queued' AND j.created_at < clock_timestamp()-INTERVAL '15 minutes'
                        THEN '排队超过 15 分钟，需执行或检查支付配置'
                      WHEN j.status='provider_processing' AND j.updated_at < clock_timestamp()-INTERVAL '10 minutes'
                        THEN '支付渠道处理中超过 10 分钟，需主动查询对账'
                      WHEN j.status='failed' THEN '上次执行失败，可按同一幂等键重试'
                      WHEN j.status='manual_review' THEN '需人工资金处理或外部成功补偿对账'
                      ELSE NULL END AS attention_reason
             FROM service_refund_cash_jobs j {where}
             ORDER BY CASE j.status
                        WHEN 'manual_review' THEN 0 WHEN 'failed' THEN 1
                        WHEN 'provider_processing' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END,
                      j.created_at
             LIMIT %s OFFSET %s""",
        tuple(params + [max(1, min(int(limit), 500)), max(0, int(offset))]),
    )
    items = [_row(item) for item in cur.fetchall()]
    return {
        "items": items,
        "total": total,
        "attention_count": sum(1 for item in items if item.get("attention_reason")),
    }


def _claim_refund(
    cur, *, order_id: str, cash_job_id: Optional[str] = None,
) -> Dict[str, Any]:
    from services import dealer_inventory_resale as resale

    resale._order_xact_lock(cur, str(order_id))
    cur.execute("SELECT * FROM recharge_orders WHERE id=%s FOR UPDATE", (str(order_id),))
    recharge = _row(cur.fetchone())
    if not recharge:
        raise RefundExecutionError("REFUND_ORDER_NOT_FOUND", "退款原订单不存在", status_code=404)
    if str(recharge.get("payment_status") or "") != "paid":
        raise RefundExecutionError("REFUND_ORDER_NOT_PAID", "原订单未支付，不能发起现金退款")
    route = resale._original_payment_route(recharge)
    provider = _provider_for_route(route)
    total_cents = int(recharge.get("amount_cents") or 0)

    cur.execute(
        "SELECT *,updated_at < clock_timestamp()-INTERVAL '10 minutes' AS processing_stale "
        "FROM service_refund_cash_jobs WHERE source_order_id=%s FOR UPDATE",
        (str(order_id),),
    )
    job = _row(cur.fetchone())
    if job:
        if cash_job_id and str(job["cash_job_id"]) != str(cash_job_id):
            raise RefundExecutionError("REFUND_JOB_ORDER_MISMATCH", "退款工单与原订单不一致")
        cur.execute(
            "SELECT refund_amount_cents,cash_status,internal_settled_at FROM consumer_refund_cases "
            "WHERE case_id=%s FOR UPDATE",
            (str(job["case_id"]),),
        )
        case = _row(cur.fetchone())
        amount_cents = int(job.get("amount_cents") or 0)
        if (
            not case.get("internal_settled_at")
            or amount_cents <= 0
            or amount_cents != int(case.get("refund_amount_cents") or 0)
            or amount_cents > total_cents
        ):
            raise RefundExecutionError(
                "REFUND_AMOUNT_SNAPSHOT_INVALID",
                "退款工单金额与已批准消费者退款快照不一致",
            )
        state = str(job.get("status") or "")
        if state == "completed":
            if (
                str(case.get("cash_status") or "") != "completed"
                or str(recharge.get("refund_status") or "") not in {"completed", "pending_review"}
                or recharge.get("refund_completed_at") is None
                or int(recharge.get("refunded_amount_cents") or 0) != amount_cents
            ):
                raise RefundExecutionError(
                    "REFUND_JOB_TERMINAL_SPLIT",
                    "现金工单、消费者退款与原订单终态不一致，必须先人工对账",
                )
            return {
                "kind": "consumer", "mode": "completed", "provider": provider,
                "order_id": str(order_id), "cash_job_id": str(job["cash_job_id"]),
                "case_id": str(job["case_id"]), "amount_cents": amount_cents,
                "total_cents": total_cents, "route": route,
                "out_refund_no": f"R{order_id}", "reason": "消费者退款",
                "consumer_user_id": int(job["consumer_user_id"]),
            }
        if state == "manual_review" and not job.get("provider_refund_id"):
            raise RefundExecutionError("REFUND_JOB_MANUAL_REVIEW", "该退款工单需要人工资金处理")
        mode = (
            "initiate"
            if state in {"queued", "failed"}
            or (
                state == "provider_processing"
                and not job.get("provider_refund_id")
                and bool(job.get("processing_stale"))
                and provider == "wechat"
            )
            else "reconcile"
        )
        if state not in {"queued", "failed", "provider_processing", "manual_review"}:
            raise RefundExecutionError("REFUND_JOB_STATE_INVALID", f"退款工单状态不可执行: {state}")
        if mode == "initiate":
            cur.execute(
                """UPDATE service_refund_cash_jobs
                   SET status='provider_processing',attempt_count=attempt_count+1,
                       last_error=NULL,updated_at=clock_timestamp()
                   WHERE cash_job_id=%s""",
                (str(job["cash_job_id"]),),
            )
        return {
            "kind": "consumer", "mode": mode, "provider": provider,
            "order_id": str(order_id), "cash_job_id": str(job["cash_job_id"]),
            "case_id": str(job["case_id"]), "amount_cents": amount_cents,
            "total_cents": total_cents, "route": route,
            "out_refund_no": f"R{order_id}", "reason": "消费者退款",
            "payment_transaction_id": str(recharge.get("payment_id") or ""),
            "persisted_provider_evidence": job.get("provider_evidence_jsonb") or {},
            "persisted_provider_refund_id": str(job.get("provider_refund_id") or ""),
            "persisted_status": state,
            "consumer_user_id": int(job["consumer_user_id"]),
        }

    cur.execute(
        """SELECT f.*,o.sale_amount_cents,o.buyer_user_id,o.seller_user_id,
                  o.state AS resale_order_state,
                  f.execution_updated_at < clock_timestamp()-INTERVAL '10 minutes' AS processing_stale
             FROM dealer_resale_refunds f
             JOIN dealer_resale_orders o ON o.order_id=f.order_id
             WHERE f.order_id=%s FOR UPDATE OF f,o""",
        (str(order_id),),
    )
    refund = _row(cur.fetchone())
    if not refund:
        raise RefundExecutionError(
            "PERSISTED_REFUND_REQUIRED",
            "该订单没有已批准并冻结库存的持久退款申请",
        )
    amount_cents = int(refund.get("refund_amount_cents") or 0)
    expected = int(refund.get("sale_amount_cents") or 0) - int(refund.get("processing_cost_cents") or 0)
    if amount_cents <= 0 or amount_cents != expected or total_cents != int(refund["sale_amount_cents"]):
        raise RefundExecutionError("REFUND_AMOUNT_SNAPSHOT_INVALID", "B2B 退款金额快照与订单不一致")
    state = str(refund.get("status") or "")
    if state == "completed":
        if (
            str(recharge.get("refund_status") or "") not in {"completed", "pending_review"}
            or recharge.get("refund_completed_at") is None
            or int(recharge.get("refunded_amount_cents") or 0) != amount_cents
            or str(refund.get("resale_order_state") or "") != "refunded"
        ):
            raise RefundExecutionError(
                "REFUND_JOB_TERMINAL_SPLIT",
                "B2B 退款、原订单与库存转售终态不一致，必须先人工对账",
            )
        mode = "completed"
    elif state == "manual_review" and refund.get("external_refund_id"):
        mode = "reconcile"
    elif state == "processing":
        mode = (
            "initiate"
            if (
                not refund.get("external_refund_id")
                and bool(refund.get("processing_stale"))
                and provider == "wechat"
            )
            else "reconcile"
        )
        if mode == "initiate":
            cur.execute(
                "UPDATE dealer_resale_refunds SET attempt_count=attempt_count+1,"
                "last_error=NULL,execution_updated_at=clock_timestamp() WHERE refund_id=%s",
                (str(refund["refund_id"]),),
            )
    elif state == "requested":
        mode = "initiate"
        cur.execute(
            """UPDATE dealer_resale_refunds
               SET status='processing',attempt_count=attempt_count+1,last_error=NULL,
                   execution_updated_at=clock_timestamp()
               WHERE refund_id=%s""",
            (str(refund["refund_id"]),),
        )
    else:
        raise RefundExecutionError("REFUND_JOB_STATE_INVALID", f"B2B 退款状态不可执行: {state}")
    return {
        "kind": "b2b", "mode": mode, "provider": provider,
        "order_id": str(order_id), "refund_id": str(refund["refund_id"]),
        "amount_cents": amount_cents, "total_cents": total_cents, "route": route,
        "out_refund_no": f"R{order_id}", "reason": str(refund.get("reason") or "B2B 整批退款"),
        "payment_transaction_id": str(recharge.get("payment_id") or ""),
        "persisted_provider_evidence": (
            (refund.get("audit_jsonb") or {}).get("provider_execution", {})
            if isinstance(refund.get("audit_jsonb"), dict)
            else {}
        ),
        "persisted_provider_refund_id": str(refund.get("external_refund_id") or ""),
        "persisted_status": state,
        "buyer_user_id": int(refund["buyer_user_id"]),
        "seller_user_id": int(refund["seller_user_id"]),
    }


def _mark_nonterminal(claim: Dict[str, Any], *, status: str, error: str = "", evidence: Optional[Dict[str, Any]] = None) -> None:
    evidence_json = json.dumps(evidence or {}, ensure_ascii=False, separators=(",", ":"))
    with get_db() as conn:
        cur = conn.cursor()
        from services import dealer_inventory_resale as resale

        resale._order_xact_lock(cur, claim["order_id"])
        if claim["kind"] == "consumer":
            target = "manual_review" if status == "manual_review" else ("failed" if status == "failed" else "provider_processing")
            cur.execute(
                """UPDATE service_refund_cash_jobs
                   SET status=%s,last_error=%s,
                       provider_refund_id=COALESCE(%s,provider_refund_id),
                       provider_evidence_jsonb=provider_evidence_jsonb || %s::jsonb,
                       updated_at=clock_timestamp()
                   WHERE cash_job_id=%s AND status<>'completed'""",
                (
                    target, str(error or "") or None,
                    str((evidence or {}).get("provider_refund_id") or "") or None,
                    evidence_json, claim["cash_job_id"],
                ),
            )
            updated = cur.rowcount
        else:
            target = "manual_review" if status == "manual_review" else ("requested" if status == "failed" else "processing")
            cur.execute(
                """UPDATE dealer_resale_refunds
                   SET status=%s,last_error=%s,execution_updated_at=clock_timestamp(),
                       external_channel=COALESCE(%s,external_channel),
                       external_refund_id=COALESCE(%s,external_refund_id),
                       audit_jsonb=audit_jsonb || %s::jsonb
                   WHERE refund_id=%s AND status<>'completed'""",
                (
                    target, str(error or "") or None,
                    str((evidence or {}).get("provider") or "") or None,
                    str((evidence or {}).get("provider_refund_id") or "") or None,
                    json.dumps({"provider_execution": evidence or {}}, ensure_ascii=False, separators=(",", ":")),
                    claim["refund_id"],
                ),
            )
            updated = cur.rowcount
        if updated == 1 and status == "manual_review":
            _enqueue_refund_terminal(cur, claim, "manual_required", error)
        elif updated == 1 and status == "failed":
            _enqueue_refund_terminal(cur, claim, "failed", error)
        conn.commit()


def _normalize_provider_result(claim: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    provider = claim["provider"]
    if provider != "wechat":
        raise RefundExecutionError(
            "XUNHUPAY_ORDER_QUERY_NOT_REFUND_PROOF",
            "虎皮椒通用订单查询只表示支付/取消状态，不能证明退款成功",
        )
    status = str(result.get("status") or "UNKNOWN").upper()
    amount = result.get("amount") or {}
    refund_cents = _integer_cents(amount.get("refund")) if status == "SUCCESS" else claim["amount_cents"]
    total_cents = _integer_cents(amount.get("total")) if status == "SUCCESS" else claim["total_cents"]
    provider_ref = str(result.get("refund_id") or claim["out_refund_no"])
    terminal = "success" if status == "SUCCESS" else ("processing" if status == "PROCESSING" else "failed")
    evidence_kind = "wechat_refund_query"
    if terminal in {"success", "processing"} and (
        str(result.get("out_refund_no") or "") != str(claim["out_refund_no"])
        or str(result.get("out_trade_no") or "") != str(claim["order_id"])
    ):
        raise RefundExecutionError(
            "REFUND_PROVIDER_ORDER_MISMATCH",
            "微信退款主动查询结果与持久订单/退款单号不一致",
        )
    if terminal == "success" and (
        refund_cents != int(claim["amount_cents"]) or total_cents != int(claim["total_cents"])
    ):
        raise RefundExecutionError(
            "REFUND_PROVIDER_AMOUNT_MISMATCH",
            "支付渠道查询结果与持久退款金额不一致",
        )
    return {
        "terminal": terminal,
        "provider": provider,
        "provider_refund_id": provider_ref,
        "refund_evidence_kind": evidence_kind,
        "refunded_amount_cents": refund_cents,
        "total_amount_cents": total_cents,
        "provider_status": status,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalize_xunhupay_order_query(
    claim: Dict[str, Any], result: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate a signed generic order query without manufacturing refund proof."""

    status = str(result.get("status") or "").upper()
    if status not in {"OD", "WP", "CD"}:
        raise RefundExecutionError(
            "XUNHUPAY_ORDER_QUERY_STATUS_INVALID",
            "虎皮椒通用订单查询返回未知订单状态",
        )
    response_order_id = str(
        result.get("trade_order_id") or result.get("out_trade_order") or ""
    )
    if response_order_id != str(claim["order_id"]):
        raise RefundExecutionError(
            "REFUND_PROVIDER_ORDER_MISMATCH",
            "虎皮椒通用订单查询与持久订单号不一致",
        )
    total_cents = _yuan_to_cents(result.get("total_fee"))
    if total_cents != int(claim["total_cents"]):
        raise RefundExecutionError(
            "REFUND_PROVIDER_AMOUNT_MISMATCH",
            "虎皮椒通用订单查询金额与原订单不一致",
        )
    return {
        "provider": "xunhupay",
        "provider_status": status,
        "order_state": {"OD": "paid", "WP": "pending", "CD": "cancelled"}[status],
        "total_amount_cents": total_cents,
        "refund_terminal": "unproven",
    }


def _normalize_xunhupay_refund_response(
    claim: Dict[str, Any], result: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize only the signed refund-endpoint response contract."""

    if "refund_status" not in result:
        raise RefundExecutionError(
            "XUNHUPAY_REFUND_RESPONSE_CONTRACT_INVALID",
            "虎皮椒退款接口响应缺少 refund_status，不能形成退款终态",
        )
    status = str(result.get("refund_status") or "").upper()
    if status not in {"OD", "CD", "RD", "UD"}:
        raise RefundExecutionError(
            "XUNHUPAY_REFUND_STATUS_INVALID",
            "虎皮椒退款接口返回未知退款状态",
        )
    if str(result.get("trade_order_id") or "") != str(claim["order_id"]):
        raise RefundExecutionError(
            "REFUND_PROVIDER_ORDER_MISMATCH",
            "虎皮椒退款接口响应与持久订单号不一致",
        )
    payment_transaction_id = str(result.get("transaction_id") or "").strip()
    if not payment_transaction_id:
        raise RefundExecutionError(
            "REFUND_PAYMENT_REFERENCE_MISSING",
            "虎皮椒退款接口响应缺少原支付交易号",
        )
    expected_payment_transaction_id = str(
        claim.get("payment_transaction_id") or ""
    ).strip()
    if (
        expected_payment_transaction_id
        and payment_transaction_id != expected_payment_transaction_id
    ):
        raise RefundExecutionError(
            "REFUND_PAYMENT_REFERENCE_MISMATCH",
            "虎皮椒退款接口响应的支付交易号与原订单不一致",
        )
    provider_ref = str(result.get("out_refund_no") or "").strip()
    if status in {"CD", "RD"} and not provider_ref:
        raise RefundExecutionError(
            "REFUND_PROVIDER_REFERENCE_MISSING",
            "虎皮椒退款接口响应缺少退款单号",
        )
    refund_cents = (
        _yuan_to_cents(result.get("refund_fee"))
        if status in {"CD", "RD"}
        else int(claim["amount_cents"])
    )
    if status in {"CD", "RD"} and (
        refund_cents != int(claim["amount_cents"])
        or refund_cents != int(claim["total_cents"])
    ):
        raise RefundExecutionError(
            "REFUND_PROVIDER_AMOUNT_MISMATCH",
            "虎皮椒退款接口金额与持久退款金额不一致",
        )
    return {
        "terminal": (
            "success" if status == "CD"
            else "processing" if status == "RD"
            else "retryable" if status == "OD"
            else "failed"
        ),
        "provider": "xunhupay",
        "provider_refund_id": provider_ref,
        "payment_transaction_id": payment_transaction_id,
        "refund_evidence_kind": "signed_xunhupay_refund_response",
        "refunded_amount_cents": refund_cents,
        "total_amount_cents": int(claim["total_cents"]),
        "provider_status": status,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


def _validated_persisted_xunhupay_success(
    claim: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return durable signed success evidence usable for internal-only recovery.

    This path never calls Xunhupay.  It only lets a manual-review job finish the
    local atomic reversal after the signed refund response was already persisted
    and a previous local transaction failed.
    """

    if claim.get("provider") != "xunhupay" or claim.get("mode") != "reconcile":
        return None
    evidence = claim.get("persisted_provider_evidence")
    if not isinstance(evidence, dict):
        return None
    if evidence.get("refund_evidence_kind") != "signed_xunhupay_refund_response":
        return None
    if evidence.get("terminal") != "success":
        return None
    if (
        evidence.get("provider") != "xunhupay"
        or str(evidence.get("provider_status") or "").upper() != "CD"
    ):
        raise RefundExecutionError(
            "REFUND_PERSISTED_EVIDENCE_INVALID",
            "已持久化虎皮椒退款证据不是可补偿的成功终态",
        )
    provider_ref = str(evidence.get("provider_refund_id") or "").strip()
    persisted_ref = str(claim.get("persisted_provider_refund_id") or "").strip()
    if not provider_ref or provider_ref != persisted_ref:
        raise RefundExecutionError(
            "REFUND_PROVIDER_REFERENCE_MISMATCH",
            "已持久化虎皮椒退款号与现金工单不一致",
        )
    payment_transaction_id = str(
        evidence.get("payment_transaction_id") or ""
    ).strip()
    expected_payment_transaction_id = str(
        claim.get("payment_transaction_id") or ""
    ).strip()
    if (
        not payment_transaction_id
        or (
            expected_payment_transaction_id
            and payment_transaction_id != expected_payment_transaction_id
        )
    ):
        raise RefundExecutionError(
            "REFUND_PAYMENT_REFERENCE_MISMATCH",
            "已持久化虎皮椒原支付交易号与订单不一致",
        )
    if (
        int(evidence.get("refunded_amount_cents") or 0) != int(claim["amount_cents"])
        or int(evidence.get("total_amount_cents") or 0) != int(claim["total_cents"])
    ):
        raise RefundExecutionError(
            "REFUND_PROVIDER_AMOUNT_MISMATCH",
            "已持久化虎皮椒退款证据与退款金额快照不一致",
        )
    if not str(evidence.get("queried_at") or "").strip():
        raise RefundExecutionError(
            "REFUND_PERSISTED_EVIDENCE_INVALID",
            "已持久化虎皮椒退款证据缺少验签时间",
        )
    return dict(evidence)


def _persist_provider_result(claim: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    with get_db() as conn:
        cur = conn.cursor()
        from services import dealer_inventory_resale as resale

        resale._order_xact_lock(cur, claim["order_id"])
        if claim["kind"] == "consumer":
            cur.execute(
                "SELECT amount_cents,status FROM service_refund_cash_jobs WHERE cash_job_id=%s FOR UPDATE",
                (claim["cash_job_id"],),
            )
            persisted = _row(cur.fetchone())
            if int(persisted.get("amount_cents") or 0) != int(claim["amount_cents"]):
                raise RefundExecutionError("REFUND_AMOUNT_SNAPSHOT_CHANGED", "退款工单金额在执行期间发生变化")
        else:
            cur.execute(
                "SELECT refund_amount_cents,status FROM dealer_resale_refunds WHERE refund_id=%s FOR UPDATE",
                (claim["refund_id"],),
            )
            persisted = _row(cur.fetchone())
            if int(persisted.get("refund_amount_cents") or 0) != int(claim["amount_cents"]):
                raise RefundExecutionError("REFUND_AMOUNT_SNAPSHOT_CHANGED", "B2B 退款金额在执行期间发生变化")

        terminal = evidence["terminal"]
        if terminal == "success":
            if claim["kind"] == "b2b":
                cur.execute(
                    """UPDATE dealer_resale_refunds
                       SET status='processing',external_channel=%s,external_refund_id=%s,
                           audit_jsonb=audit_jsonb || %s::jsonb
                       WHERE refund_id=%s AND status<>'completed'""",
                    (
                        evidence["provider"],
                        evidence["provider_refund_id"],
                        json.dumps(
                            {"verified_cash_terminal": evidence},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        claim["refund_id"],
                    ),
                )
                if cur.rowcount != 1:
                    raise RefundExecutionError(
                        "REFUND_ORDER_STATE_CHANGED",
                        "B2B 退款状态在支付渠道返回成功后发生变化",
                    )
            snapshot = json.dumps(
                {
                    "refund_provider": evidence["provider"],
                    "provider_refund_id": evidence["provider_refund_id"],
                    "payment_transaction_id": evidence.get("payment_transaction_id", ""),
                    "refund_evidence_kind": evidence["refund_evidence_kind"],
                    "refund_provider_status": evidence["provider_status"],
                    "refund_query_at": evidence["queried_at"],
                    "refunded_amount_cents": evidence["refunded_amount_cents"],
                    "refund_total_amount_cents": evidence["total_amount_cents"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            cur.execute(
                """UPDATE recharge_orders
                   SET refund_status='completed',refund_completed_at=COALESCE(refund_completed_at,NOW()),
                       refund_requested_at=COALESCE(refund_requested_at,NOW()),
                       refunded_amount_cents=%s,
                       settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb) || %s::jsonb
                   WHERE id=%s AND payment_status='paid'""",
                (claim["amount_cents"], snapshot, claim["order_id"]),
            )
            if cur.rowcount != 1:
                raise RefundExecutionError("REFUND_ORDER_STATE_CHANGED", "原订单状态在退款对账时发生变化")
            from services.channel_revenue_lifecycle import reverse_channel_revenue_on_refund

            reverse_channel_revenue_on_refund(cur, claim["order_id"])
            _enqueue_refund_terminal(cur, claim, "completed")
        elif terminal == "processing":
            _evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
            if claim["kind"] == "consumer":
                cur.execute(
                    """UPDATE service_refund_cash_jobs
                       SET status='provider_processing',provider_refund_id=%s,
                           provider_evidence_jsonb=provider_evidence_jsonb || %s::jsonb,
                           updated_at=clock_timestamp()
                       WHERE cash_job_id=%s AND status<>'completed'""",
                    (evidence["provider_refund_id"], _evidence_json, claim["cash_job_id"]),
                )
            else:
                cur.execute(
                    """UPDATE dealer_resale_refunds
                       SET status='processing',external_channel=%s,external_refund_id=%s,
                           audit_jsonb=audit_jsonb || %s::jsonb
                       WHERE refund_id=%s AND status<>'completed'""",
                    (
                        evidence["provider"], evidence["provider_refund_id"],
                        json.dumps({"provider_execution": evidence}, ensure_ascii=False, separators=(",", ":")),
                        claim["refund_id"],
                    ),
                )
        else:
            if claim["kind"] == "consumer":
                cur.execute(
                    "UPDATE service_refund_cash_jobs SET status='failed',last_error=%s,"
                    "provider_evidence_jsonb=provider_evidence_jsonb || %s::jsonb,updated_at=NOW() "
                    "WHERE cash_job_id=%s AND status<>'completed'",
                    (
                        f"provider_status={evidence['provider_status']}",
                        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                        claim["cash_job_id"],
                    ),
                )
            else:
                cur.execute(
                    "UPDATE dealer_resale_refunds SET status='requested',last_error=%s,external_channel=%s,"
                    "audit_jsonb=audit_jsonb || %s::jsonb "
                    "WHERE refund_id=%s AND status<>'completed'",
                    (
                        f"provider_status={evidence['provider_status']}",
                        evidence["provider"],
                        json.dumps(
                            {"provider_execution": evidence},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        claim["refund_id"],
                    ),
                )
            failed_updated = cur.rowcount
            if failed_updated == 1:
                _enqueue_refund_terminal(
                    cur,
                    claim,
                    "failed",
                    "支付通道未确认退款完成，请稍后重试或等待平台处理。",
                )
        conn.commit()
        return {
            "order_id": claim["order_id"],
            "cash_job_id": claim.get("cash_job_id"),
            "refund_id": claim.get("refund_id"),
            "amount_cents": claim["amount_cents"],
            "provider": claim["provider"],
            "provider_status": evidence["provider_status"],
            "status": "completed" if terminal == "success" else (
                "provider_processing" if terminal == "processing" else "failed"
            ),
            "idempotent": False,
        }


async def execute_persisted_refund(
    *, order_id: Optional[str] = None, cash_job_id: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    if not order_id and not cash_job_id:
        raise RefundExecutionError("REFUND_TARGET_REQUIRED", "必须提供退款订单或现金工单", status_code=422)
    with get_db() as conn:
        cur = conn.cursor()
        if cash_job_id:
            cur.execute(
                "SELECT source_order_id FROM service_refund_cash_jobs WHERE cash_job_id=%s",
                (str(cash_job_id),),
            )
            job = _row(cur.fetchone())
            if not job:
                raise RefundExecutionError("REFUND_JOB_NOT_FOUND", "退款现金工单不存在", status_code=404)
            order_id = str(job["source_order_id"])
        claim = _claim_refund(cur, order_id=str(order_id), cash_job_id=cash_job_id)
        if claim["mode"] == "completed":
            _enqueue_refund_terminal(cur, claim, "completed")
        conn.commit()
    if claim["mode"] == "completed":
        return {
            "order_id": claim["order_id"], "cash_job_id": claim.get("cash_job_id"),
            "refund_id": claim.get("refund_id"), "amount_cents": claim["amount_cents"],
            "provider": claim["provider"], "status": "completed", "idempotent": True,
        }
    if claim["provider"] == "xunhupay" and claim["amount_cents"] != claim["total_cents"]:
        _mark_nonterminal(
            claim,
            status="manual_review",
            error="虎皮椒退款 API 仅支持整单全额；持久退款金额为部分退款，禁止调用渠道",
        )
        raise RefundExecutionError(
            "XUNHUPAY_PARTIAL_REFUND_UNSUPPORTED",
            "虎皮椒原路退款不支持部分金额，工单已转人工资金处理",
        )

    persisted_success = _validated_persisted_xunhupay_success(claim)
    if persisted_success is not None:
        try:
            return _persist_provider_result(claim, persisted_success)
        except Exception as exc:
            _mark_nonterminal(
                claim,
                status="manual_review",
                error=f"外部现金已成功，内部终态待补偿: {exc}",
                evidence=persisted_success,
            )
            raise RefundExecutionError(
                "REFUND_EXTERNAL_SUCCESS_RECONCILIATION_PENDING",
                "支付渠道已确认退款，但内部库存/收益终态待耐久补偿；禁止重复发起现金退款",
                status_code=503,
            ) from exc
    if claim["provider"] == "xunhupay" and claim.get("persisted_status") == "manual_review":
        raise RefundExecutionError(
            "REFUND_JOB_MANUAL_REVIEW",
            "该虎皮椒退款工单没有可重放的验签成功证据，必须人工渠道核证",
        )

    provider_accepted = False
    provider_attempted = False
    provider_ack: Dict[str, Any] = {}
    result: Dict[str, Any] = {}
    normalized_evidence: Optional[Dict[str, Any]] = None
    try:
        if claim["provider"] == "wechat":
            from services.wechat_pay import query_refund, refund_order

            # A retry must query first. Only an explicit NOT_FOUND/failed state may
            # reach the money-moving API; a query outage never authorizes another call.
            result = await query_refund(claim["out_refund_no"], strict=True)
            preflight = _normalize_provider_result(claim, dict(result or {}))
            if preflight["terminal"] not in {"success", "processing"}:
                if claim["mode"] != "initiate":
                    _mark_nonterminal(
                        claim,
                        status="processing",
                        error="主动查询尚未返回退款终态；当前执行权仍由既有工单持有",
                    )
                    return {
                        "order_id": claim["order_id"],
                        "cash_job_id": claim.get("cash_job_id"),
                        "refund_id": claim.get("refund_id"),
                        "amount_cents": claim["amount_cents"],
                        "provider": claim["provider"],
                        "provider_status": preflight["provider_status"],
                        "status": "provider_processing",
                        "idempotent": True,
                    }
                accepted = await refund_order(
                    out_trade_no=claim["order_id"],
                    out_refund_no=claim["out_refund_no"],
                    refund_yuan=claim["amount_cents"] / 100,
                    total_yuan=claim["total_cents"] / 100,
                    reason=(str(reason or claim["reason"]) or "用户申请退款")[:80],
                )
                provider_accepted = True
                provider_ack = {
                    "provider": "wechat",
                    "provider_refund_id": str((accepted or {}).get("refund_id") or claim["out_refund_no"]),
                    "provider_request_status": str((accepted or {}).get("status") or "ACCEPTED"),
                    "provider_request_accepted_at": datetime.now(timezone.utc).isoformat(),
                }
                _mark_nonterminal(claim, status="processing", evidence=provider_ack)
                result = await query_refund(claim["out_refund_no"], strict=True)
        else:
            from services.xunhupay import query_xunhupay_order, refund_xunhupay_order

            result = await query_xunhupay_order(claim["order_id"], strict=True)
            if not result:
                raise RuntimeError("虎皮椒通用订单查询未返回可验签订单状态")
            order_query = _normalize_xunhupay_order_query(claim, dict(result))
            if order_query["provider_status"] != "OD":
                raise RefundExecutionError(
                    "XUNHUPAY_ORDER_QUERY_NOT_REFUND_PROOF",
                    "虎皮椒通用订单查询不是退款查询；CD 仅表示订单已取消，禁止反向入账",
                )
            if claim["mode"] != "initiate":
                _mark_nonterminal(
                    claim,
                    status="processing",
                    error="虎皮椒通用订单查询不能证明退款终态；等待签名退款回调或人工渠道核证",
                    evidence={
                        "provider": "xunhupay",
                        "provider_status": order_query["provider_status"],
                        "order_query_state": order_query["order_state"],
                        "refund_terminal": "unproven",
                    },
                )
                return {
                    "order_id": claim["order_id"],
                    "cash_job_id": claim.get("cash_job_id"),
                    "refund_id": claim.get("refund_id"),
                    "amount_cents": claim["amount_cents"],
                    "provider": claim["provider"],
                    "provider_status": order_query["provider_status"],
                    "status": "provider_processing",
                    "idempotent": True,
                }
            provider_attempted = True
            accepted = await refund_xunhupay_order(
                claim["order_id"], reason=(str(reason or claim["reason"]) or "用户申请退款")[:80]
            )
            result = dict(accepted or {})
            normalized_evidence = _normalize_xunhupay_refund_response(claim, result)
            provider_accepted = normalized_evidence["terminal"] in {"success", "processing"}
            provider_ack = {
                "provider": "xunhupay",
                "provider_refund_id": normalized_evidence["provider_refund_id"],
                "provider_request_status": normalized_evidence["provider_status"],
                "provider_request_accepted_at": normalized_evidence["queried_at"],
                "refund_evidence_kind": normalized_evidence["refund_evidence_kind"],
            }
    except RefundExecutionError as exc:
        raw_result = dict(result or {}) if isinstance(result, dict) else {}
        raw_amount = raw_result.get("amount") if isinstance(raw_result.get("amount"), dict) else {}
        raw_provider_ref = str(
            raw_result.get("refund_id") or raw_result.get("out_refund_no") or ""
        )
        if not raw_provider_ref and claim["provider"] == "wechat":
            raw_provider_ref = str(claim["out_refund_no"])
        _mark_nonterminal(
            claim,
            status="manual_review",
            error=f"支付渠道证据与持久退款快照冲突: {exc.message}",
            evidence={
                "provider": claim["provider"],
                "provider_refund_id": raw_provider_ref,
                "payment_transaction_id": str(raw_result.get("transaction_id") or ""),
                "provider_status": str(
                    raw_result.get("status") or raw_result.get("refund_status") or "UNKNOWN"
                ),
                "reported_refund_cents": raw_amount.get("refund"),
                "reported_total_cents": raw_amount.get("total") or raw_result.get("total_fee"),
                "queried_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        raise
    except Exception as exc:  # provider/network failure before a trusted terminal
        ambiguous_xunhupay_attempt = (
            claim["provider"] == "xunhupay" and provider_attempted and not provider_accepted
        )
        _mark_nonterminal(
            claim,
            status=(
                "manual_review"
                if ambiguous_xunhupay_attempt
                else ("processing" if provider_accepted else "failed")
            ),
            error=str(exc),
            evidence=provider_ack,
        )
        raise RefundExecutionError(
            "REFUND_PROVIDER_REQUEST_FAILED",
            (
                "虎皮椒退款请求结果不确定，工单已转人工核证，禁止自动重试"
                if ambiguous_xunhupay_attempt
                else
                "支付渠道已受理退款但主动查询失败；工单保持处理中，后续只允许查询对账"
                if provider_accepted
                else "支付渠道主动查询或退款请求失败；工单已保留，可按同一幂等键重试"
            ),
            status_code=503,
        ) from exc

    try:
        evidence = normalized_evidence or _normalize_provider_result(claim, dict(result or {}))
        if provider_accepted and evidence["terminal"] == "failed":
            _mark_nonterminal(
                claim,
                status="processing",
                error="支付渠道已受理，主动查询尚未返回成功终态",
                evidence=provider_ack,
            )
            return {
                "order_id": claim["order_id"],
                "cash_job_id": claim.get("cash_job_id"),
                "refund_id": claim.get("refund_id"),
                "amount_cents": claim["amount_cents"],
                "provider": claim["provider"],
                "provider_status": evidence["provider_status"],
                "status": "provider_processing",
                "idempotent": False,
            }
    except RefundExecutionError as exc:
        raw_result = dict(result or {})
        raw_amount = raw_result.get("amount") if isinstance(raw_result.get("amount"), dict) else {}
        raw_provider_ref = str(
            raw_result.get("refund_id") or raw_result.get("out_refund_no") or ""
        )
        if not raw_provider_ref and claim["provider"] == "wechat":
            raw_provider_ref = str(claim["out_refund_no"])
        _mark_nonterminal(
            claim,
            status="manual_review",
            error=f"支付渠道终态与持久退款金额冲突: {exc.message}",
            evidence={
                "provider": claim["provider"],
                "provider_refund_id": raw_provider_ref,
                "payment_transaction_id": str(raw_result.get("transaction_id") or ""),
                "provider_status": str(
                    raw_result.get("status") or raw_result.get("refund_status") or "UNKNOWN"
                ),
                "reported_refund_cents": raw_amount.get("refund"),
                "reported_total_cents": raw_amount.get("total"),
                "queried_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        raise
    try:
        return _persist_provider_result(claim, evidence)
    except Exception as exc:  # external success must leave a durable compensation record
        if evidence["terminal"] == "success":
            _mark_nonterminal(
                claim,
                status="manual_review",
                error=f"外部现金已成功，内部终态待补偿: {exc}",
                evidence=evidence,
            )
            logger.critical(
                "external refund succeeded but internal reconciliation failed order=%s provider=%s ref=%s: %s",
                claim["order_id"], claim["provider"], evidence["provider_refund_id"], exc,
            )
            raise RefundExecutionError(
                "REFUND_EXTERNAL_SUCCESS_RECONCILIATION_PENDING",
                "支付渠道已确认退款，但内部库存/收益终态待耐久补偿；禁止重复发起现金退款",
                status_code=503,
            ) from exc
        raise
