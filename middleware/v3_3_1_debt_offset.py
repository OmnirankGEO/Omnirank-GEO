"""
V3.3.1 债务抵扣 + source 标记 helper

设计:
- 独立模块 · 不修改 middleware/billing.py 主体(CLAUDE.md 红线 R4 免签后可改)
- 由 billing.py 在扣费前调用 try_offset_debts(user_id) · 先把消费用于抵扣债务
- source 标记由各扣费入口在 INSERT point_transactions 时传(migration_007 加 source 字段)

债务抵扣顺序(§3.4.1 LAYER 2):
    1. bonus_clawback_pending
    2. service_fee_clawback_pending

关联:
- 决策书 §3.4.1
- RED_LINES R4(CLAUDE.md 红线免签项)
- IDENTITY_DECISIONS_LOCK Q34
"""

import logging
from decimal import Decimal
from typing import Dict, Any, Optional

from db.connection import get_db
from config.v3_3_1_flags import (
    is_v3_3_1_enabled,
    PaymentSource,
    VALID_PAYMENT_SOURCES,
)

logger = logging.getLogger("GEO-V3.3.1-DebtOffset")

POINTS_PER_YUAN = Decimal("130")


def validate_source(source: Optional[str]) -> str:
    """source 字段校验 · 默认 balance_deduction"""
    if not source:
        return PaymentSource.BALANCE_DEDUCTION
    if source not in VALID_PAYMENT_SOURCES:
        logger.warning("validate_source: invalid source=%s · fallback balance_deduction", source)
        return PaymentSource.BALANCE_DEDUCTION
    return source


def get_pending_debts(user_id: int) -> Dict[str, Any]:
    """查询用户全部 pending / partial_settled 债务"""
    if not is_v3_3_1_enabled():
        return {"bonus_debt_points": 0, "service_fee_debt_yuan": 0.0, "rows": []}

    rows: list = []
    bonus_debt_points = 0
    service_fee_debt_yuan = Decimal("0")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 'bonus' AS kind, id, amount_due, amount_settled, status
                  FROM bonus_clawback_pending
                 WHERE user_id = %s AND status IN ('pending', 'partial_settled')
                 ORDER BY created_at
                """,
                (user_id,),
            )
            for r in cur.fetchall():
                d = dict(r) if isinstance(r, dict) else {
                    "kind": r[0], "id": r[1], "amount_due": r[2],
                    "amount_settled": r[3], "status": r[4]
                }
                remaining = int(d["amount_due"]) - int(d.get("amount_settled") or 0)
                bonus_debt_points += max(0, remaining)
                rows.append({**d, "remaining": remaining})

            cur.execute(
                """
                SELECT 'service_fee' AS kind, id, amount_due, amount_settled, status
                  FROM service_fee_clawback_pending
                 WHERE user_id = %s AND status IN ('pending', 'partial_settled')
                 ORDER BY created_at
                """,
                (user_id,),
            )
            for r in cur.fetchall():
                d = dict(r) if isinstance(r, dict) else {
                    "kind": r[0], "id": r[1], "amount_due": r[2],
                    "amount_settled": r[3], "status": r[4]
                }
                remaining = Decimal(str(d["amount_due"])) - Decimal(str(d.get("amount_settled") or 0))
                if remaining > 0:
                    service_fee_debt_yuan += remaining
                rows.append({**d, "remaining": float(remaining)})

    return {
        "bonus_debt_points": bonus_debt_points,
        "service_fee_debt_yuan": float(service_fee_debt_yuan),
        "rows": rows,
    }


def offset_debts_after_consumption(user_id: int, consumed_points: int) -> Dict[str, Any]:
    """V3.3.1 消费**成功后**推进债务表进度(§3.4.1 LAYER 2)

    设计调整(Codex 反馈 + 商业模型修正):
    - 不影响 billing 扣费总额(避免"用户付 1000 只得 800 服务"差体验)
    - 仅把已发生的消费"贴标签"到债务表 · 推进 amount_settled
    - 用户钱包正常扣 consumed_points · 平台内部记录"这 X 积分中 Y 用于抵债"

    输入:用户已成功消费的 consumed_points(由 deduct_points 已扣完后调用)
    输出:debt 推进明细 + offset_points(本次推进的债务金额)

    幂等保证:同一笔消费多次调用不会重复推进(由 amount_settled 上限自动夹)
    """
    if not is_v3_3_1_enabled():
        return {
            "skipped": True, "reason": "v3_3_1_disabled",
            "consumed_points": consumed_points, "offset_points": 0,
            "offsets": [],
        }

    if consumed_points <= 0:
        return {
            "skipped": True, "reason": "no_consumption",
            "consumed_points": consumed_points, "offset_points": 0,
            "offsets": [],
        }

    with get_db() as conn:
        return offset_debts_after_consumption_cursor(
            conn.cursor(), user_id, consumed_points
        )


# 向后兼容(老 try_offset_debts 名)· 不改扣费总额 · 仅推进债务
def try_offset_debts(user_id: int, available_points: int) -> Dict[str, Any]:
    """[DEPRECATED · 保留兼容] · 用 offset_debts_after_consumption · 不再扣减 wallet"""
    result = offset_debts_after_consumption(user_id, available_points)
    result["remaining_points"] = available_points  # 兼容老调用方:返不扣减
    return result


def _do_offset(user_id: int, available_points: int) -> Dict[str, Any]:
    """Backward-compatible wrapper around the transaction-local implementation."""
    with get_db() as conn:
        return offset_debts_after_consumption_cursor(
            conn.cursor(), user_id, available_points
        )


def offset_debts_after_consumption_cursor(
    cursor,
    user_id: int,
    consumed_points: int,
) -> Dict[str, Any]:
    """Advance debt rows using the caller's transaction.

    This is the atomic primitive used by the billing debt outbox.  It never
    opens or commits a second connection, so the debt mutation and the
    per-charge outbox receipt can commit (or roll back) together.
    """
    available_points = int(consumed_points)
    if not is_v3_3_1_enabled():
        return {
            "skipped": True,
            "reason": "v3_3_1_disabled",
            "consumed_points": available_points,
            "offset_points": 0,
            "offsets": [],
        }
    if available_points <= 0:
        return {
            "skipped": True,
            "reason": "no_consumption",
            "consumed_points": available_points,
            "offset_points": 0,
            "offsets": [],
        }

    remaining = available_points
    offsets: list = []

    cursor.execute(
        """
        SELECT id, amount_due, amount_settled
          FROM bonus_clawback_pending
         WHERE user_id = %s AND status IN ('pending', 'partial_settled')
         ORDER BY created_at, id
         FOR UPDATE
        """,
        (user_id,),
    )
    for row in cursor.fetchall():
        if remaining <= 0:
            break
        rid = row["id"] if isinstance(row, dict) else row[0]
        due = int(row["amount_due"] if isinstance(row, dict) else row[1])
        settled = int(
            (row["amount_settled"] if isinstance(row, dict) else row[2]) or 0
        )
        debt_left = max(0, due - settled)
        if debt_left == 0:
            continue
        deducted = min(remaining, debt_left)
        new_settled = settled + deducted
        new_status = "settled" if new_settled >= due else "partial_settled"
        cursor.execute(
            """
            UPDATE bonus_clawback_pending
               SET amount_settled = %s, status = %s,
                   settled_at = CASE WHEN %s = 'settled' THEN NOW() ELSE settled_at END
             WHERE id = %s
            """,
            (new_settled, new_status, new_status, rid),
        )
        offsets.append(
            {
                "kind": "bonus",
                "id": rid,
                "deducted": deducted,
                "status_after": new_status,
            }
        )
        remaining -= deducted

    cursor.execute(
        """
        SELECT id, amount_due, amount_settled
          FROM service_fee_clawback_pending
         WHERE user_id = %s AND status IN ('pending', 'partial_settled')
         ORDER BY created_at, id
         FOR UPDATE
        """,
        (user_id,),
    )
    for row in cursor.fetchall():
        if remaining <= 0:
            break
        rid = row["id"] if isinstance(row, dict) else row[0]
        due_yuan = Decimal(
            str(row["amount_due"] if isinstance(row, dict) else row[1])
        )
        settled_yuan = Decimal(
            str((row["amount_settled"] if isinstance(row, dict) else row[2]) or 0)
        )
        debt_yuan = max(Decimal("0"), due_yuan - settled_yuan)
        debt_points = int((debt_yuan * POINTS_PER_YUAN).quantize(Decimal("1")))
        if debt_points <= 0:
            continue
        deducted_points = min(remaining, debt_points)
        deducted_yuan = (
            Decimal(deducted_points) / POINTS_PER_YUAN
        ).quantize(Decimal("0.01"))
        new_settled_yuan = settled_yuan + deducted_yuan
        new_status = (
            "settled" if new_settled_yuan >= due_yuan else "partial_settled"
        )
        cursor.execute(
            """
            UPDATE service_fee_clawback_pending
               SET amount_settled = %s, status = %s,
                   settled_at = CASE WHEN %s = 'settled' THEN NOW() ELSE settled_at END
             WHERE id = %s
            """,
            (new_settled_yuan, new_status, new_status, rid),
        )
        offsets.append(
            {
                "kind": "service_fee",
                "id": rid,
                "deducted_points": deducted_points,
                # Keep the immutable reversal receipt decimal-exact.  JSON
                # numbers would round-trip through binary float and can make a
                # later refund reverse a different amount from the one settled.
                "deducted_yuan": str(deducted_yuan),
                "status_after": new_status,
            }
        )
        remaining -= deducted_points

    offset_points = available_points - remaining
    return {
        "skipped": False,
        "consumed_points": available_points,
        "offset_points": offset_points,
        "offsets": offsets,
    }
