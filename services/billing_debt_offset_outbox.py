"""Durable, reversible debt-offset receipts for billing deductions.

The deduction, one per-charge outbox row, debt advancement, and any later
refund reconciliation are transaction-local.  Every worker uses the same lock
order: deduction lifecycle -> debt outbox -> wallet -> debt rows.  A refund can
therefore either cancel a pending advancement or reverse the exact immutable
receipt of an advancement that already committed.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Callable

from db.connection import get_db

logger = logging.getLogger("GEO-Billing-DebtOffset")

MAX_DEBT_OFFSET_ATTEMPTS = 12

_OUTBOX_COLUMNS = (
    "event_key",
    "user_id",
    "feature_code",
    "charge_tx_id",
    "ledger_type",
    "consumed_points",
    "status",
    "retry_count",
    "idempotency_key",
    "refunded_points",
    "reversed_points",
    "result_jsonb",
    "reversal_jsonb",
    "last_error",
)
_OUTBOX_SELECT = ", ".join(_OUTBOX_COLUMNS)


def debt_offset_event_key(ledger_type: str, charge_tx_id: int) -> str:
    if ledger_type not in ("legacy", "v35"):
        raise ValueError("unsupported billing ledger type")
    return f"billing_debt_offset:{ledger_type}:{int(charge_tx_id)}"


def _as_dict(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(columns, row or ()))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _lock_lifecycle_cursor(cursor, idempotency_key: str | None) -> dict | None:
    if not idempotency_key:
        return None
    cursor.execute(
        """
        SELECT idempotency_key,user_id,feature_code,charge_tx_id,ledger_type,
               deducted,status
          FROM billing_deduction_idempotency
         WHERE idempotency_key=%s
         FOR UPDATE
        """,
        (idempotency_key,),
    )
    row = cursor.fetchone()
    return _as_dict(
        row,
        (
            "idempotency_key",
            "user_id",
            "feature_code",
            "charge_tx_id",
            "ledger_type",
            "deducted",
            "status",
        ),
    ) if row is not None else None


def _lock_event_for_processing_cursor(cursor, event_key: str) -> tuple[dict | None, dict | None]:
    """Lock lifecycle then outbox, never the inverse."""
    cursor.execute(
        "SELECT idempotency_key FROM billing_debt_offset_outbox WHERE event_key=%s",
        (event_key,),
    )
    hint = cursor.fetchone()
    if hint is None:
        return None, None
    hinted_key = (
        hint.get("idempotency_key") if isinstance(hint, dict) else hint[0]
    )
    lifecycle = _lock_lifecycle_cursor(cursor, hinted_key)
    cursor.execute(
        f"SELECT {_OUTBOX_SELECT} FROM billing_debt_offset_outbox "
        "WHERE event_key=%s FOR UPDATE",
        (event_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None, lifecycle
    outbox = _as_dict(row, _OUTBOX_COLUMNS)
    if outbox.get("idempotency_key") != hinted_key:
        raise RuntimeError("debt offset lifecycle identity changed while locking")
    if hinted_key and lifecycle is None:
        raise RuntimeError("debt offset lifecycle row unavailable")
    return outbox, lifecycle


def _validate_lifecycle_identity(outbox: dict, lifecycle: dict | None) -> None:
    if lifecycle is None:
        if outbox.get("idempotency_key"):
            raise RuntimeError("debt offset lifecycle row unavailable")
        return
    immutable = (
        int(lifecycle["user_id"]) == int(outbox["user_id"])
        and str(lifecycle["feature_code"]) == str(outbox["feature_code"])
        and int(lifecycle["charge_tx_id"]) == int(outbox["charge_tx_id"])
        and str(lifecycle["ledger_type"]) == str(outbox["ledger_type"])
        and int(lifecycle["deducted"]) == int(outbox["consumed_points"])
        and str(lifecycle["idempotency_key"]) == str(outbox["idempotency_key"])
    )
    if not immutable:
        raise RuntimeError("debt offset lifecycle identity conflict")


def _defer_for_refund_cursor(cursor, event_key: str) -> dict:
    cursor.execute(
        """
        UPDATE billing_debt_offset_outbox
           SET next_retry_at=GREATEST(next_retry_at,NOW() + INTERVAL '60 seconds'),
               updated_at=NOW()
         WHERE event_key=%s AND status='pending'
        """,
        (event_key,),
    )
    return {"status": "deferred", "event_key": event_key, "refund_pending": True}


def _cancel_pending_cursor(cursor, event_key: str, refunded_points: int) -> dict:
    cursor.execute(
        """
        UPDATE billing_debt_offset_outbox
           SET status='cancelled', refunded_points=%s,
               cancelled_at=COALESCE(cancelled_at,NOW()), updated_at=NOW(),
               last_error=NULL
         WHERE event_key=%s AND status IN ('pending','manual')
        RETURNING status
        """,
        (int(refunded_points), event_key),
    )
    updated = cursor.fetchone()
    if updated is None:
        raise RuntimeError("debt offset cancellation CAS failed")
    return {"status": "cancelled", "event_key": event_key}


def _process_locked_row(cursor, outbox: dict, lifecycle: dict | None) -> dict:
    event_key = str(outbox["event_key"])
    status = str(outbox["status"])
    if status in ("completed", "manual", "cancelled", "reversed"):
        return {"status": status, "event_key": event_key, "replayed": True}
    if status != "pending":
        raise RuntimeError("unsupported debt offset status")

    _validate_lifecycle_identity(outbox, lifecycle)
    lifecycle_status = str(lifecycle.get("status")) if lifecycle else None
    if lifecycle_status == "refund_pending":
        return _defer_for_refund_cursor(cursor, event_key)
    if lifecycle_status == "refunded":
        refunded = int(outbox.get("refunded_points") or 0)
        if refunded >= int(outbox["consumed_points"]):
            return _cancel_pending_cursor(cursor, event_key, refunded)
        raise RuntimeError("refunded charge has unsettled debt offset receipt")
    if lifecycle_status not in (None, "charged"):
        raise RuntimeError("debt offset charge lifecycle is not processable")

    effective_points = max(
        0,
        int(outbox["consumed_points"]) - int(outbox.get("refunded_points") or 0),
    )
    if effective_points == 0:
        return _cancel_pending_cursor(
            cursor,
            event_key,
            int(outbox["consumed_points"]),
        )

    cursor.execute("SAVEPOINT billing_debt_offset_attempt")
    try:
        from middleware.v3_3_1_debt_offset import (
            offset_debts_after_consumption_cursor,
        )

        result = offset_debts_after_consumption_cursor(
            cursor,
            int(outbox["user_id"]),
            effective_points,
        )
        result["charge_consumed_points"] = int(outbox["consumed_points"])
        result["refunded_before_offset_points"] = int(
            outbox.get("refunded_points") or 0
        )
        cursor.execute(
            """
            UPDATE billing_debt_offset_outbox
               SET status='completed', result_jsonb=%s::jsonb,
                   completed_at=NOW(), updated_at=NOW(), last_error=NULL
             WHERE event_key=%s AND status='pending'
            RETURNING event_key
            """,
            (
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                event_key,
            ),
        )
        if cursor.fetchone() is None:
            raise RuntimeError("debt offset outbox completion CAS failed")
        cursor.execute("RELEASE SAVEPOINT billing_debt_offset_attempt")
        return {"status": "completed", "event_key": event_key, "result": result}
    except Exception as exc:
        cursor.execute("ROLLBACK TO SAVEPOINT billing_debt_offset_attempt")
        cursor.execute("RELEASE SAVEPOINT billing_debt_offset_attempt")
        cursor.execute(
            """
            UPDATE billing_debt_offset_outbox
               SET retry_count=retry_count + 1,
                   status=CASE WHEN retry_count + 1 >= %s THEN 'manual' ELSE 'pending' END,
                   next_retry_at=NOW() +
                       (LEAST(3600, 30 * (2 ^ LEAST(retry_count, 7))) || ' seconds')::interval,
                   last_error=%s, updated_at=NOW()
             WHERE event_key=%s AND status='pending'
            RETURNING status, retry_count
            """,
            (MAX_DEBT_OFFSET_ATTEMPTS, type(exc).__name__[:160], event_key),
        )
        updated = cursor.fetchone()
        updated_dict = _as_dict(updated, ("status", "retry_count")) if updated else {}
        next_status = str(updated_dict.get("status") or status)
        log = logger.critical if next_status == "manual" else logger.error
        log(
            "[BillingDebtOffset] event=%s advancement %s (%s)",
            event_key,
            "requires manual recovery" if next_status == "manual" else "deferred",
            type(exc).__name__,
        )
        return {"status": next_status, "event_key": event_key, "deferred": True}


def enqueue_and_process_debt_offset_cursor(
    cursor,
    *,
    user_id: int,
    feature_code: str,
    charge_tx_id: int,
    ledger_type: str,
    consumed_points: int,
    idempotency_key: str | None = None,
) -> dict:
    """Insert one immutable charge event and try to settle it in-place."""
    event_key = debt_offset_event_key(ledger_type, charge_tx_id)
    cursor.execute(
        """
        INSERT INTO billing_debt_offset_outbox
            (event_key, user_id, feature_code, charge_tx_id, ledger_type,
             consumed_points, idempotency_key, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')
        ON CONFLICT (event_key) DO NOTHING
        """,
        (
            event_key,
            int(user_id),
            str(feature_code),
            int(charge_tx_id),
            ledger_type,
            int(consumed_points),
            idempotency_key,
        ),
    )
    cursor.execute(
        f"SELECT {_OUTBOX_SELECT} FROM billing_debt_offset_outbox "
        "WHERE event_key=%s FOR UPDATE",
        (event_key,),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("debt offset outbox row unavailable")
    outbox = _as_dict(row, _OUTBOX_COLUMNS)
    immutable = (
        int(outbox["user_id"]) == int(user_id)
        and str(outbox["feature_code"]) == str(feature_code)
        and int(outbox["charge_tx_id"]) == int(charge_tx_id)
        and str(outbox["ledger_type"]) == ledger_type
        and int(outbox["consumed_points"]) == int(consumed_points)
        and outbox.get("idempotency_key") == idempotency_key
    )
    if not immutable:
        raise RuntimeError("debt offset event identity conflict")
    lifecycle = None
    if idempotency_key:
        # deduct_points owns this lifecycle row in the current transaction.
        lifecycle = {
            "idempotency_key": idempotency_key,
            "user_id": user_id,
            "feature_code": feature_code,
            "charge_tx_id": charge_tx_id,
            "ledger_type": ledger_type,
            "deducted": consumed_points,
            "status": "charged",
        }
    return _process_locked_row(cursor, outbox, lifecycle)


def lock_charge_refund_context_cursor(
    cursor,
    *,
    user_id: int,
    feature_code: str,
    charge_tx_id: int | None,
    ledger_type: str,
) -> dict | None:
    """Lock one exact charge before any wallet refund mutation."""
    if charge_tx_id is None or ledger_type not in ("legacy", "v35"):
        return None
    # Generic legacy refunds predate this additive migration.  If neither
    # tracking table exists there cannot be a tracked idempotent charge to
    # reconcile; preserve that historical path instead of aborting its wallet
    # transaction.  A partially installed schema is never accepted.
    cursor.execute(
        "SELECT to_regclass('billing_deduction_idempotency') AS lifecycle_table, "
        "to_regclass('billing_debt_offset_outbox') AS outbox_table"
    )
    table_row = cursor.fetchone()
    tables = _as_dict(table_row, ("lifecycle_table", "outbox_table"))
    if not tables.get("lifecycle_table") and not tables.get("outbox_table"):
        return None
    if not tables.get("lifecycle_table") or not tables.get("outbox_table"):
        raise RuntimeError("billing refund reconciliation schema is incomplete")
    cursor.execute(
        """
        SELECT idempotency_key,user_id,feature_code,charge_tx_id,ledger_type,
               deducted,status
          FROM billing_deduction_idempotency
         WHERE user_id=%s AND feature_code=%s AND charge_tx_id=%s
           AND ledger_type=%s
         FOR UPDATE
        """,
        (int(user_id), str(feature_code), int(charge_tx_id), ledger_type),
    )
    lifecycle_row = cursor.fetchone()
    lifecycle = _as_dict(
        lifecycle_row,
        (
            "idempotency_key",
            "user_id",
            "feature_code",
            "charge_tx_id",
            "ledger_type",
            "deducted",
            "status",
        ),
    ) if lifecycle_row is not None else None

    cursor.execute(
        f"SELECT {_OUTBOX_SELECT} FROM billing_debt_offset_outbox "
        "WHERE ledger_type=%s AND charge_tx_id=%s FOR UPDATE",
        (ledger_type, int(charge_tx_id)),
    )
    outbox_row = cursor.fetchone()
    if outbox_row is None:
        if lifecycle is not None:
            raise RuntimeError("deduction debt offset receipt is unavailable")
        return None
    outbox = _as_dict(outbox_row, _OUTBOX_COLUMNS)
    if (
        int(outbox["user_id"]) != int(user_id)
        or str(outbox["feature_code"]) != str(feature_code)
    ):
        raise RuntimeError("refund debt offset identity conflict")
    _validate_lifecycle_identity(outbox, lifecycle)
    return {"outbox": outbox, "lifecycle": lifecycle}


def _reverse_bonus_item_cursor(
    cursor,
    *,
    user_id: int,
    item: dict,
    reverse_points: int,
) -> None:
    cursor.execute(
        "SELECT amount_due,amount_settled,status FROM bonus_clawback_pending "
        "WHERE id=%s AND user_id=%s FOR UPDATE",
        (int(item["id"]), int(user_id)),
    )
    row = cursor.fetchone()
    values = _as_dict(row, ("amount_due", "amount_settled", "status")) if row else {}
    if values.get("status") == "written_off":
        raise RuntimeError("written-off bonus debt requires manual refund reconciliation")
    if not values or int(values["amount_settled"] or 0) < int(reverse_points):
        raise RuntimeError("bonus debt receipt can no longer be reversed exactly")
    due = int(values["amount_due"])
    settled = int(values["amount_settled"] or 0) - int(reverse_points)
    status = "pending" if settled <= 0 else "settled" if settled >= due else "partial_settled"
    cursor.execute(
        "UPDATE bonus_clawback_pending SET amount_settled=%s,status=%s,"
        "settled_at=CASE WHEN %s='settled' THEN settled_at ELSE NULL END WHERE id=%s",
        (settled, status, status, int(item["id"])),
    )


def _reverse_service_fee_item_cursor(
    cursor,
    *,
    user_id: int,
    item: dict,
    reverse_yuan: Decimal,
) -> None:
    cursor.execute(
        "SELECT amount_due,amount_settled,status FROM service_fee_clawback_pending "
        "WHERE id=%s AND user_id=%s FOR UPDATE",
        (int(item["id"]), int(user_id)),
    )
    row = cursor.fetchone()
    values = _as_dict(row, ("amount_due", "amount_settled", "status")) if row else {}
    if values.get("status") == "written_off":
        raise RuntimeError("written-off service fee debt requires manual refund reconciliation")
    settled_before = Decimal(str(values.get("amount_settled") or 0))
    if not values or settled_before < reverse_yuan:
        raise RuntimeError("service fee debt receipt can no longer be reversed exactly")
    due = Decimal(str(values["amount_due"]))
    settled = settled_before - reverse_yuan
    status = "pending" if settled <= 0 else "settled" if settled >= due else "partial_settled"
    cursor.execute(
        "UPDATE service_fee_clawback_pending SET amount_settled=%s,status=%s,"
        "settled_at=CASE WHEN %s='settled' THEN settled_at ELSE NULL END WHERE id=%s",
        (settled, status, status, int(item["id"])),
    )


def _lock_receipt_debt_rows_cursor(
    cursor,
    *,
    user_id: int,
    receipt_items: list[dict],
) -> None:
    """Match the forward primitive's bonus-before-service row lock order."""
    bonus_ids = sorted(
        {int(item["id"]) for item in receipt_items if item.get("kind") == "bonus"}
    )
    service_ids = sorted(
        {
            int(item["id"])
            for item in receipt_items
            if item.get("kind") == "service_fee"
        }
    )
    if bonus_ids:
        cursor.execute(
            "SELECT id FROM bonus_clawback_pending "
            "WHERE user_id=%s AND id=ANY(%s) ORDER BY created_at,id FOR UPDATE",
            (int(user_id), bonus_ids),
        )
        if len(cursor.fetchall()) != len(bonus_ids):
            raise RuntimeError("bonus debt receipt row is unavailable")
    if service_ids:
        cursor.execute(
            "SELECT id FROM service_fee_clawback_pending "
            "WHERE user_id=%s AND id=ANY(%s) ORDER BY created_at,id FOR UPDATE",
            (int(user_id), service_ids),
        )
        if len(cursor.fetchall()) != len(service_ids):
            raise RuntimeError("service fee debt receipt row is unavailable")


def _reverse_receipt_delta_cursor(
    cursor,
    *,
    outbox: dict,
    target_reversed_points: int,
) -> dict:
    result = _json_object(outbox.get("result_jsonb"))
    receipt_items = result.get("offsets")
    if not isinstance(receipt_items, list):
        raise RuntimeError("debt offset receipt items are unavailable")
    normalized_items = [item for item in receipt_items if isinstance(item, dict)]
    if len(normalized_items) != len(receipt_items):
        raise RuntimeError("debt offset receipt item is invalid")
    _lock_receipt_debt_rows_cursor(
        cursor,
        user_id=int(outbox["user_id"]),
        receipt_items=normalized_items,
    )
    reversal = _json_object(outbox.get("reversal_jsonb"))
    reversed_items = reversal.get("items")
    if not isinstance(reversed_items, dict):
        reversed_items = {}
    current_total = int(outbox.get("reversed_points") or 0)
    remaining = int(target_reversed_points) - current_total
    if remaining < 0:
        raise RuntimeError("debt offset reversal target regressed")

    for raw_item in reversed(receipt_items):
        if remaining <= 0:
            break
        if not isinstance(raw_item, dict) or raw_item.get("kind") not in ("bonus", "service_fee"):
            raise RuntimeError("debt offset receipt item is invalid")
        item = dict(raw_item)
        item_key = f"{item['kind']}:{int(item['id'])}"
        item_points = int(
            item.get("deducted")
            if item["kind"] == "bonus"
            else item.get("deducted_points")
            or 0
        )
        prior = reversed_items.get(item_key)
        prior = dict(prior) if isinstance(prior, dict) else {}
        prior_points = int(prior.get("reversed_points") or 0)
        available = item_points - prior_points
        if available <= 0:
            continue
        take = min(remaining, available)
        new_item_points = prior_points + take
        if item["kind"] == "bonus":
            _reverse_bonus_item_cursor(
                cursor,
                user_id=int(outbox["user_id"]),
                item=item,
                reverse_points=take,
            )
            reversed_items[item_key] = {"reversed_points": new_item_points}
        else:
            original_yuan = Decimal(str(item.get("deducted_yuan") or 0))
            if item_points <= 0 or original_yuan < 0:
                raise RuntimeError("service fee debt receipt amount is invalid")
            target_yuan = (
                original_yuan
                if new_item_points >= item_points
                else (original_yuan * Decimal(new_item_points) / Decimal(item_points)).quantize(
                    Decimal("0.01")
                )
            )
            prior_yuan = Decimal(str(prior.get("reversed_yuan") or 0))
            delta_yuan = target_yuan - prior_yuan
            if delta_yuan < 0:
                raise RuntimeError("service fee debt reversal target regressed")
            if delta_yuan > 0:
                _reverse_service_fee_item_cursor(
                    cursor,
                    user_id=int(outbox["user_id"]),
                    item=item,
                    reverse_yuan=delta_yuan,
                )
            reversed_items[item_key] = {
                "reversed_points": new_item_points,
                "reversed_yuan": str(target_yuan),
            }
        remaining -= take

    if remaining != 0:
        raise RuntimeError("debt offset receipt cannot satisfy exact reversal")
    return {
        "version": 1,
        "reversed_points": int(target_reversed_points),
        "items": reversed_items,
    }


def reconcile_charge_refund_cursor(
    cursor,
    *,
    context: dict | None,
    confirmed_refund_points: int,
) -> dict:
    """Cancel/reverse one locked receipt and synchronize its charge lifecycle."""
    if context is None:
        return {"status": "untracked", "full_refund": False}
    outbox = context["outbox"]
    lifecycle = context.get("lifecycle")
    consumed = int(outbox["consumed_points"])
    confirmed = max(
        int(outbox.get("refunded_points") or 0),
        min(consumed, max(0, int(confirmed_refund_points))),
    )
    full_refund = confirmed >= consumed
    status = str(outbox["status"])
    reversal_jsonb = outbox.get("reversal_jsonb")
    reversed_points = int(outbox.get("reversed_points") or 0)
    next_status = status

    if status == "pending":
        if full_refund:
            next_status = "cancelled"
    elif status == "manual":
        if str(outbox.get("last_error") or "") == "pre_migration_charge_requires_debt_audit":
            raise RuntimeError("pre-migration debt offset requires manual reconciliation")
        if full_refund:
            next_status = "cancelled"
    elif status == "completed":
        result = _json_object(outbox.get("result_jsonb"))
        offset_points = int(result.get("offset_points") or 0)
        net_consumption = max(0, consumed - confirmed)
        target_reversed = max(0, offset_points - net_consumption)
        if target_reversed > reversed_points:
            reversal = _reverse_receipt_delta_cursor(
                cursor,
                outbox=outbox,
                target_reversed_points=target_reversed,
            )
            reversal_jsonb = json.dumps(
                reversal,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            reversed_points = target_reversed
        if full_refund and reversed_points >= offset_points:
            if not reversal_jsonb:
                reversal_jsonb = json.dumps(
                    {"version": 1, "reversed_points": 0, "items": {}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            next_status = "reversed"
    elif status not in ("cancelled", "reversed"):
        raise RuntimeError("unsupported debt offset refund status")

    reversal_payload = (
        json.dumps(reversal_jsonb, ensure_ascii=False, separators=(",", ":"))
        if isinstance(reversal_jsonb, dict)
        else reversal_jsonb
    )
    cursor.execute(
        """
        UPDATE billing_debt_offset_outbox
           SET status=%s, refunded_points=%s, reversed_points=%s,
               reversal_jsonb=%s::jsonb,
               cancelled_at=CASE WHEN %s='cancelled' THEN COALESCE(cancelled_at,NOW()) ELSE cancelled_at END,
               reversed_at=CASE WHEN %s='reversed' THEN COALESCE(reversed_at,NOW()) ELSE reversed_at END,
               updated_at=NOW()
         WHERE event_key=%s AND status=%s
        RETURNING status
        """,
        (
            next_status,
            confirmed,
            reversed_points,
            reversal_payload,
            next_status,
            next_status,
            outbox["event_key"],
            status,
        ),
    )
    if cursor.fetchone() is None:
        raise RuntimeError("debt offset refund reconciliation CAS failed")

    lifecycle_status = None
    if lifecycle is not None:
        lifecycle_status = "refunded" if full_refund else "refund_pending"
        timestamp_column = (
            "refunded_at" if lifecycle_status == "refunded" else "refund_pending_at"
        )
        cursor.execute(
            f"""
            UPDATE billing_deduction_idempotency
               SET status=%s, {timestamp_column}=COALESCE({timestamp_column},NOW()),
                   updated_at=NOW()
             WHERE idempotency_key=%s
               AND status IN ('charged','refund_pending','refunded')
            RETURNING idempotency_key
            """,
            (lifecycle_status, lifecycle["idempotency_key"]),
        )
        if cursor.fetchone() is None:
            raise RuntimeError("deduction refund lifecycle CAS failed")
    return {
        "status": next_status,
        "lifecycle_status": lifecycle_status,
        "full_refund": full_refund,
        "refunded_points": confirmed,
        "reversed_points": reversed_points,
    }


def process_debt_offset_for_charge(
    *,
    ledger_type: str,
    charge_tx_id: int,
    db_factory: Callable = get_db,
) -> dict:
    """Retry one event, used by idempotent billing replay before returning."""
    event_key = debt_offset_event_key(ledger_type, charge_tx_id)
    with db_factory() as conn:
        cursor = conn.cursor()
        outbox, lifecycle = _lock_event_for_processing_cursor(cursor, event_key)
        if outbox is None:
            return {"status": "deferred", "event_key": event_key}
        if (
            str(outbox["status"]) == "pending"
            and int(outbox["retry_count"] or 0) > 1
        ):
            cursor.execute(
                "SELECT next_retry_at <= NOW() AS due FROM billing_debt_offset_outbox WHERE event_key=%s",
                (event_key,),
            )
            due_row = cursor.fetchone()
            due = due_row.get("due") if isinstance(due_row, dict) else due_row[0]
            if not due:
                return {"status": "deferred", "event_key": event_key}
        return _process_locked_row(cursor, outbox, lifecycle)


def process_pending_debt_offsets(limit: int = 100) -> dict:
    """Process due rows with the global lifecycle-before-outbox lock order."""
    stats = {
        "claimed": 0,
        "completed": 0,
        "cancelled": 0,
        "deferred": 0,
        "manual": 0,
    }
    seen: set[str] = set()
    for _ in range(max(1, int(limit))):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT event_key
                  FROM billing_debt_offset_outbox
                 WHERE status='pending' AND next_retry_at <= NOW()
                   AND NOT (event_key = ANY(%s))
                 ORDER BY next_retry_at, created_at, event_key
                 LIMIT 1
                """,
                (list(seen),),
            )
            candidate = cursor.fetchone()
            if candidate is None:
                break
            event_key = str(
                candidate.get("event_key") if isinstance(candidate, dict) else candidate[0]
            )
            seen.add(event_key)
            outbox, lifecycle = _lock_event_for_processing_cursor(cursor, event_key)
            if outbox is None or str(outbox["status"]) != "pending":
                continue
            stats["claimed"] += 1
            outcome = _process_locked_row(cursor, outbox, lifecycle)
            outcome_status = str(outcome.get("status") or "deferred")
            if outcome_status in stats:
                stats[outcome_status] += 1
            else:
                stats["deferred"] += 1
    return stats
