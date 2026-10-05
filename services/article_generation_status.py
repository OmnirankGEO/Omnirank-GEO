"""Durable topic projection helpers for the existing article worker.

Billing settlement and refund continue to use the established billing and fund
recovery primitives.  These helpers only project customer-safe task state.
"""
from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any

from writing.article_generation_failure import ArticleGenerationFailure
from writing.evidence_first_policy import LEGAL_PROHIBITION_CATALOG_VERSION


REFUND_STATES = {
    "not_charged",
    "charged",
    "not_required",
    "refunded",
    "released",
    "pending_recovery",
}
logger = logging.getLogger("ArticleGenerationStatus")


def _topic_ids(values: Iterable[int]) -> list[int]:
    return sorted({int(value) for value in values if int(value) > 0})


def refund_status_message(status: str | None) -> str:
    return {
        "not_charged": "本次未扣费",
        "charged": "已扣费；失败时系统会自动退款",
        "not_required": "内容已交付，无需退款",
        "refunded": "退款已完成",
        "released": "预留额度已释放",
        "pending_recovery": "退款处理中，请勿重复提交",
    }.get(str(status or ""), "旧任务暂无退款记录")


def mark_failure_on_cursor(
    cur,
    *,
    topic_id: int,
    failure: ArticleGenerationFailure,
    lease_started_at: Any = None,
) -> bool:
    # [统一 R3 · 2026-07-23 §五] 失败投影同步冻结本次尝试活跃的法律禁止
    # 清单版本(与生成期硬门/提示词同源),失败审计可追溯判定口径版本。
    params: list[Any] = [
        failure.code,
        failure.message,
        bool(failure.retryable),
        failure.phase,
        failure.message,
        LEGAL_PROHIBITION_CATALOG_VERSION,
        int(topic_id),
        "writing",
    ]
    lease_sql = ""
    if lease_started_at is not None:
        lease_sql = " AND writing_started_at=%s"
        params.append(lease_started_at)
    cur.execute(
        f"""
        UPDATE topics
           SET status='failed',
               generation_error_code=%s,
               generation_error_message=%s,
               generation_retryable=%s,
               generation_failure_phase=%s,
               fail_reason=%s,
               generation_legal_catalog_version=%s
         WHERE id=%s AND status=%s AND article_id IS NULL{lease_sql}
        """,
        params,
    )
    return cur.rowcount == 1


def set_refund_status(
    topic_ids: Iterable[int],
    status: str,
    *,
    request_id: str | None = None,
) -> int:
    if status not in REFUND_STATES:
        raise ValueError("unsupported article refund state")
    ids = _topic_ids(topic_ids)
    if not ids:
        return 0
    from db.diagnosis_db import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = [status, ids]
        request_sql = ""
        if request_id:
            request_sql = " AND generation_request_id=%s"
            params.append(str(request_id))
        cur.execute(
            "UPDATE topics SET generation_refund_status=%s "
            f"WHERE id=ANY(%s){request_sql}",
            params,
        )
        changed = cur.rowcount
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        # Projection failure must never turn a successful refund/settlement
        # into another billing action. The existing recovery ledger remains
        # authoritative and operators still receive this error in logs.
        logger.exception("article refund projection update failed")
        return 0
    finally:
        conn.close()
