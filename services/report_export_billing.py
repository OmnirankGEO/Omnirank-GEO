"""Shared customer report export readiness and billing helpers."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


AsyncBillingFunc = Callable[..., Awaitable[Any]]
RecoveryOrderFunc = Callable[..., Any]


class ClientReportNotReadyError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ReportExportBillingContractError(RuntimeError):
    """The billing writer returned no immutable identity for a real charge."""


class ReportExportRefundError(RuntimeError):
    """An exact refund was not confirmed and was handed to durable recovery."""

    def __init__(self, result: Any, recovery_order_id: int | None):
        super().__init__("report export refund was not confirmed")
        self.result = result
        self.recovery_order_id = recovery_order_id


class ReportExportRecoveryPersistenceError(RuntimeError):
    """Both the exact refund and its durable recovery registration are unconfirmed."""

    def __init__(self, result: Any):
        super().__init__("report export refund recovery order was not persisted")
        self.result = result


CLIENT_REPORT_EXPORT_UNSUPPORTED_CODE = "CLIENT_REPORT_EXPORT_UNSUPPORTED"
CLIENT_REPORT_EXPORT_UNSUPPORTED_MESSAGE = (
    "PPTX 客户报告暂不支持安全导出，请先使用 PDF。"
)
REPORT_EXPORT_IDEMPOTENCY_HEADER = "X-Report-Export-Idempotency-Key"
_REPORT_EXPORT_IDEMPOTENCY_RE = re.compile(
    r"^report-export:(?P<diagnosis_id>[1-9][0-9]*):(?P<attempt>[A-Za-z0-9._-]{8,80})$"
)


def require_report_export_idempotency_key(
    request: Any,
    diagnosis_id: int,
) -> str | None:
    """Require a stable attempt identity only for users who can be charged."""
    user = getattr(getattr(request, "state", None), "user", None) or {}
    if not user or user.get("is_admin"):
        return None

    headers = getattr(request, "headers", None)
    raw_key = headers.get(REPORT_EXPORT_IDEMPOTENCY_HEADER) if headers else None
    match = (
        _REPORT_EXPORT_IDEMPOTENCY_RE.fullmatch(raw_key)
        if isinstance(raw_key, str)
        else None
    )
    if not match or int(match.group("diagnosis_id")) != int(diagnosis_id):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422,
            detail={
                "code": "REPORT_EXPORT_IDEMPOTENCY_KEY_REQUIRED",
                "message": "导出请求标识无效，请刷新后重试。",
            },
        )
    return raw_key


def require_client_report_ready(modules_jsonb: Any) -> None:
    """Block report export before any billable operation when artifact is absent."""
    from services.report_html_renderer import (
        CLIENT_REPORT_NOT_READY_CODE,
        CLIENT_REPORT_NOT_READY_MESSAGE,
        is_client_report_ready,
    )

    if not is_client_report_ready(modules_jsonb):
        raise ClientReportNotReadyError(
            CLIENT_REPORT_NOT_READY_CODE,
            CLIENT_REPORT_NOT_READY_MESSAGE,
        )


def require_client_v2_export_ready(record: Any) -> dict:
    """Return the explicit customer V2 artifact or fail before export work."""
    safe_record = record if isinstance(record, dict) else {}
    if safe_record.get("report_v2_version") != "v2":
        from services.report_html_renderer import (
            CLIENT_REPORT_NOT_READY_CODE,
            CLIENT_REPORT_NOT_READY_MESSAGE,
        )

        raise ClientReportNotReadyError(
            CLIENT_REPORT_NOT_READY_CODE,
            CLIENT_REPORT_NOT_READY_MESSAGE,
        )

    modules_jsonb = safe_record.get("report_v2_modules_jsonb")
    if isinstance(modules_jsonb, str):
        try:
            modules_jsonb = json.loads(modules_jsonb)
        except Exception:
            modules_jsonb = {}
    require_client_report_ready(modules_jsonb)
    return modules_jsonb


def _run_async(func: AsyncBillingFunc, *args, **kwargs):
    """Run an async billing function from sync FastAPI endpoints."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(func(*args, **kwargs), loop)
        return future.result(timeout=10)
    return asyncio.run(func(*args, **kwargs))


@dataclass
class ReportExportCharge:
    user_id: int | None
    diagnosis_id: int
    feature_code: str = "report_export"
    charged: bool = False
    charge_tx_id: int | None = None
    ledger_type: str | None = None
    deducted: int = 0
    recovery_order_id: int | None = None
    idempotency_key: str | None = None

    def _persist_refund_recovery(
        self,
        reason: str,
        result: Any,
        recovery_func: RecoveryOrderFunc | None,
    ) -> int | None:
        """Persist the exact charge identity before the request-local object dies."""
        if recovery_func is None:
            if self.idempotency_key is not None:
                from db.fund_recovery_db import (
                    create_report_export_refund_recovery as recovery_func,
                )
            else:
                # Compatibility for historical/non-HTTP callers.  The active
                # public export endpoint always requires an idempotency key.
                from db.fund_recovery_db import create_recovery_order as recovery_func

        if isinstance(result, dict):
            failure_type = str(
                result.get("error_type")
                or result.get("reason")
                or "unconfirmed_refund"
            )[:160]
        else:
            failure_type = type(result).__name__
        recovery_payload = {
            "diagnosis_id": int(self.diagnosis_id),
            "user_id": int(self.user_id) if self.user_id is not None else None,
            "charge_tx_id": (
                int(self.charge_tx_id) if self.charge_tx_id is not None else None
            ),
            "ledger_type": self.ledger_type,
            "deducted": int(self.deducted),
            "export_failure": str(reason)[:200],
        }
        if self.idempotency_key is not None:
            recovery_payload["idempotency_key"] = self.idempotency_key
        try:
            recovery_order_id = recovery_func(
                "report_export",
                "refund",
                ref_key=f"diagnosis:{int(self.diagnosis_id)}",
                user_id=int(self.user_id) if self.user_id is not None else None,
                feature_code=self.feature_code,
                charge_tx_id=(
                    int(self.charge_tx_id)
                    if self.charge_tx_id is not None
                    else None
                ),
                amount_points=int(self.deducted),
                reason="客户报告导出失败后的精确退款未确认",
                last_error=failure_type,
                payload=recovery_payload,
                ledger_type=self.ledger_type,
            )
        except Exception as exc:
            raise ReportExportRecoveryPersistenceError(result) from exc
        if recovery_order_id is None:
            raise ReportExportRecoveryPersistenceError(result)
        self.recovery_order_id = (
            int(recovery_order_id) if recovery_order_id is not None else None
        )
        return self.recovery_order_id

    def refund(
        self,
        reason: str,
        *,
        refund_func: AsyncBillingFunc | None = None,
        recovery_func: RecoveryOrderFunc | None = None,
    ) -> dict | None:
        if not self.charged or self.user_id is None:
            return None
        if self.charge_tx_id is None or self.ledger_type not in ("legacy", "v35"):
            raise ReportExportBillingContractError(
                "charged report export is missing its immutable billing identity"
            )
        if refund_func is None:
            from middleware.billing import refund_points as refund_func

        try:
            result = _run_async(
                refund_func,
                self.user_id,
                self.feature_code,
                reason=reason,
                charge_tx_id=self.charge_tx_id,
                ledger_type=self.ledger_type,
            )
        except Exception as exc:
            result = {"success": False, "error_type": type(exc).__name__}
            recovery_order_id = self._persist_refund_recovery(
                reason,
                result,
                recovery_func,
            )
            raise ReportExportRefundError(result, recovery_order_id) from exc
        if not isinstance(result, dict) or result.get("success") is not True:
            # Request-local state is not a recovery mechanism. Persist the
            # immutable identity before raising so scheduler retries survive
            # worker exit, blue/green rotation and lost refund responses.
            recovery_order_id = self._persist_refund_recovery(
                reason,
                result,
                recovery_func,
            )
            raise ReportExportRefundError(result, recovery_order_id)
        self.charged = False
        return result


def charge_report_export(
    request: Any,
    diagnosis_id: int,
    *,
    deduct_func: AsyncBillingFunc | None = None,
    idempotency_key: str | None = None,
) -> ReportExportCharge:
    """Deduct report_export points for non-admin users."""
    user = getattr(getattr(request, "state", None), "user", None) or {}
    if not user or user.get("is_admin"):
        return ReportExportCharge(user_id=None, diagnosis_id=diagnosis_id, charged=False)

    user_id = int(user["user_id"])
    if deduct_func is None:
        from middleware.billing import deduct_points as deduct_func
    deduct_kwargs = (
        {"idempotency_key": idempotency_key}
        if idempotency_key is not None
        else {}
    )
    result = _run_async(deduct_func, user_id, "report_export", **deduct_kwargs)
    if not isinstance(result, dict) or result.get("success") is not True:
        raise ReportExportBillingContractError("report export deduction was not confirmed")

    try:
        deducted = int(result.get("deducted") or 0)
    except (TypeError, ValueError) as exc:
        raise ReportExportBillingContractError(
            "report export deduction returned an invalid amount"
        ) from exc
    if deducted <= 0:
        return ReportExportCharge(
            user_id=user_id,
            diagnosis_id=diagnosis_id,
            charged=False,
            idempotency_key=idempotency_key,
        )

    charge_tx_id = result.get("charge_tx_id")
    try:
        charge_tx_id = int(charge_tx_id)
    except (TypeError, ValueError) as exc:
        raise ReportExportBillingContractError(
            "charged report export is missing charge_tx_id"
        ) from exc
    channel = result.get("channel") or "legacy"
    if channel not in ("legacy", "v35"):
        raise ReportExportBillingContractError(
            f"unsupported report export billing channel: {channel}"
        )
    return ReportExportCharge(
        user_id=user_id,
        diagnosis_id=diagnosis_id,
        charged=True,
        charge_tx_id=charge_tx_id,
        ledger_type=channel,
        deducted=deducted,
        idempotency_key=idempotency_key,
    )


def charge_client_report_export(
    request: Any,
    diagnosis_id: int,
    modules_jsonb: Any,
    *,
    deduct_func: AsyncBillingFunc | None = None,
    idempotency_key: str | None = None,
) -> ReportExportCharge:
    """Enforce readiness again at the final boundary before deduction."""
    require_client_report_ready(modules_jsonb)
    return charge_report_export(
        request,
        diagnosis_id,
        deduct_func=deduct_func,
        idempotency_key=idempotency_key,
    )
