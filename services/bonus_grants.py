"""
Bonus grant ledger for channel-tier incentive points.

The wallet tables remain the balance SSOT. This ledger records where bonus
points came from, their expiry, and FIFO consumption. Callers own the outer DB
transaction and pass an existing cursor.
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GEO-ChannelTier-BonusGrants")


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return dict(row) if isinstance(row, dict) else {}


def create_grant(
    cursor,
    owner_type: str,
    owner_id: int,
    granted_points: int,
    grant_type: str,
    grant_key: str,
    expires_months: int,
    tier_at_grant: Optional[str] = None,
    bonus_rate_used: Optional[float] = None,
    related_order_id: Optional[str] = None,
    parent_grant_id: Optional[int] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Create one idempotent bonus grant row and return it.

    This function intentionally does not mutate wallet balances. The caller
    adds inventory/credit through the existing wallet services after the grant
    row is created.
    """
    if owner_type not in ("agent", "customer"):
        raise ValueError("owner_type must be agent or customer")
    if granted_points <= 0:
        raise ValueError("granted_points must be positive")
    months = max(1, int(expires_months or 12))
    cursor.execute(
        """
        INSERT INTO bonus_grants (
            grant_key, owner_type, owner_id, granted_points, grant_type,
            tier_at_grant, bonus_rate_used, related_order_id, parent_grant_id,
            expires_at, note
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            NOW() + (%s || ' months')::interval, %s
        )
        ON CONFLICT (grant_key) DO NOTHING
        RETURNING *
        """,
        (
            grant_key,
            owner_type,
            owner_id,
            int(granted_points),
            grant_type,
            tier_at_grant,
            bonus_rate_used,
            related_order_id,
            parent_grant_id,
            months,
            note,
        ),
    )
    row = cursor.fetchone()
    if row:
        result = dict(row) if isinstance(row, dict) else _row_to_dict(row)
        result["_created"] = True
        return result

    cursor.execute("SELECT * FROM bonus_grants WHERE grant_key = %s FOR UPDATE", (grant_key,))
    existing = cursor.fetchone()
    result = dict(existing) if isinstance(existing, dict) else _row_to_dict(existing)
    result["_created"] = False
    return result


def _fetch_active_grants(cursor, owner_type: str, owner_id: int) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT *
          FROM bonus_grants
         WHERE owner_type = %s
           AND owner_id = %s
           AND status = 'active'
           AND granted_points > consumed_points + frozen_points
         ORDER BY expires_at ASC, id ASC
         FOR UPDATE
        """,
        (owner_type, owner_id),
    )
    return [dict(r) if isinstance(r, dict) else _row_to_dict(r) for r in cursor.fetchall()]


def consume_bonus_fifo(cursor, owner_type: str, owner_id: int, points: int) -> Dict[str, Any]:
    """Consume grant rows FIFO.

    This is ledger-only. Wallet balances are consumed by the existing wallet
    path before/after this hook, depending on caller.
    """
    requested = int(points or 0)
    remaining = requested
    if remaining <= 0:
        return {"requested_points": 0, "consumed_points": 0, "missing_points": 0, "legacy_untracked": False, "items": []}

    items: List[Dict[str, Any]] = []
    for grant in _fetch_active_grants(cursor, owner_type, owner_id):
        if remaining <= 0:
            break
        available = int(grant["granted_points"]) - int(grant["consumed_points"]) - int(grant["frozen_points"])
        if available <= 0:
            continue
        take = min(available, remaining)
        new_consumed = int(grant["consumed_points"]) + take
        frozen = int(grant.get("frozen_points") or 0)
        granted = int(grant.get("granted_points") or (int(grant["consumed_points"]) + available))
        new_status = "fully_consumed" if new_consumed + frozen >= granted else "active"
        cursor.execute(
            """
            UPDATE bonus_grants
               SET consumed_points = %s,
                   status = %s
             WHERE id = %s
            """,
            (new_consumed, new_status, grant["id"]),
        )
        items.append({"grant_id": grant["id"], "points": take})
        remaining -= take

    if remaining > 0:
        logger.warning(
            "bonus grant ledger insufficient · owner=%s:%s requested=%s missing=%s · allow legacy wallet compatibility",
            owner_type,
            owner_id,
            requested,
            remaining,
        )
    return {
        "requested_points": requested,
        "consumed_points": requested - remaining,
        "missing_points": remaining,
        "legacy_untracked": remaining > 0,
        "items": items,
    }


def reserve_agent_order_bonus_for_refund(
    cursor,
    agent_user_id: int,
    related_order_id: str,
    *,
    untracked_bonus_points: int = 0,
) -> Dict[str, Any]:
    """Freeze one order's restricted bonus before external cash refund starts.

    Grant rows are locked before the wallet, matching ``expire_due_grants``. The
    returned JSON-safe snapshot is persisted on the refund row and is the only
    input accepted by release/finalize paths.
    """
    agent_id = int(agent_user_id)
    order_id = str(related_order_id)
    untracked = int(untracked_bonus_points or 0)
    if untracked < 0:
        raise ValueError("untracked bonus points must be non-negative")
    cursor.execute(
        """
        SELECT *, expires_at > NOW() AS not_expired
          FROM bonus_grants
         WHERE owner_type='agent' AND owner_id=%s AND related_order_id=%s
           AND grant_type = ANY(%s)
         ORDER BY id FOR UPDATE
        """,
        (agent_id, order_id, ["tier_purchase", "founder_first_order"]),
    )
    grants = [dict(row) if isinstance(row, dict) else _row_to_dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT paid_inventory_points,bonus_inventory_points,frozen_inventory_points
             FROM agent_inventory_wallets WHERE agent_user_id=%s FOR UPDATE""",
        (agent_id,),
    )
    wallet_row = cursor.fetchone()
    wallet = dict(wallet_row) if isinstance(wallet_row, dict) else _row_to_dict(wallet_row)
    items: List[Dict[str, Any]] = []
    newly_frozen = untracked
    already_frozen = 0
    for grant in grants:
        granted = int(grant.get("granted_points") or 0)
        consumed = int(grant.get("consumed_points") or 0)
        frozen = int(grant.get("frozen_points") or 0)
        status = str(grant.get("status") or "")
        if consumed > 0 or consumed + frozen > granted or status == "fully_consumed":
            raise ValueError("订单赠送算力已使用或账本异常，禁止发起现金退款")
        if status not in {"active", "renewed", "expired_frozen"}:
            raise ValueError("订单赠送算力状态异常，禁止发起现金退款")
        reserve_from_bonus = granted - consumed - frozen
        if status == "expired_frozen" and reserve_from_bonus:
            raise ValueError("已到期赠送算力冻结账本不守恒")
        newly_frozen += reserve_from_bonus
        already_frozen += frozen
        items.append({
            "grant_id": int(grant["id"]),
            "grant_type": str(grant.get("grant_type") or ""),
            "granted_points": granted,
            "prior_frozen_points": frozen,
            "prior_status": status,
            "reserved_from_bonus": reserve_from_bonus,
        })
    total = newly_frozen + already_frozen
    if total and not wallet:
        raise ValueError("订单赠送算力钱包不存在，禁止发起现金退款")
    if wallet and int(wallet.get("bonus_inventory_points") or 0) < newly_frozen:
        raise ValueError("订单赠送算力已划拨或使用，禁止发起现金退款")
    if wallet and int(wallet.get("frozen_inventory_points") or 0) < already_frozen:
        raise ValueError("订单既有冻结赠送算力不足，禁止发起现金退款")
    if newly_frozen:
        cursor.execute(
            """UPDATE agent_inventory_wallets
                  SET bonus_inventory_points=bonus_inventory_points-%s,
                      frozen_inventory_points=frozen_inventory_points+%s,updated_at=NOW()
                WHERE agent_user_id=%s AND bonus_inventory_points >= %s""",
            (newly_frozen, newly_frozen, agent_id, newly_frozen),
        )
        if cursor.rowcount != 1:
            raise ValueError("订单赠送算力并发变化，禁止发起现金退款")
    for item in items:
        if item["reserved_from_bonus"] <= 0:
            continue
        cursor.execute(
            """UPDATE bonus_grants SET frozen_points=frozen_points+%s
                 WHERE id=%s AND consumed_points=0 AND frozen_points=%s""",
            (item["reserved_from_bonus"], item["grant_id"], item["prior_frozen_points"]),
        )
        if cursor.rowcount != 1:
            raise ValueError("订单赠送算力账本并发变化，禁止发起现金退款")
    return {
        "version": 1,
        "agent_user_id": agent_id,
        "related_order_id": order_id,
        "untracked_bonus_points": untracked,
        "newly_frozen_points": newly_frozen,
        "already_frozen_points": already_frozen,
        "total_reserved_points": total,
        "grants": items,
    }


def _validate_bonus_refund_reservation(
    reservation: Dict[str, Any], agent_user_id: int, related_order_id: str,
) -> Dict[str, Any]:
    snap = dict(reservation or {})
    if int(snap.get("version") or 0) != 1:
        raise ValueError("订单赠送算力退款快照版本非法")
    if int(snap.get("agent_user_id") or 0) != int(agent_user_id):
        raise ValueError("订单赠送算力退款快照归属不一致")
    if str(snap.get("related_order_id") or "") != str(related_order_id):
        raise ValueError("订单赠送算力退款快照订单不一致")
    return snap


def _lock_bonus_refund_grants(cursor, items: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Lock persisted grant rows in the same deterministic order as expiry."""
    locked: Dict[int, Dict[str, Any]] = {}
    for item in sorted(items, key=lambda value: int(value["grant_id"])):
        grant_id = int(item["grant_id"])
        cursor.execute("SELECT * FROM bonus_grants WHERE id=%s FOR UPDATE", (grant_id,))
        row = cursor.fetchone()
        grant = dict(row) if isinstance(row, dict) else _row_to_dict(row)
        if not grant:
            raise ValueError("订单赠送算力退款账本不存在")
        locked[grant_id] = grant
    return locked


def release_agent_order_bonus_refund(
    cursor, agent_user_id: int, related_order_id: str, reservation: Dict[str, Any],
) -> Dict[str, Any]:
    """Release a rejected refund reservation without reviving expired grants."""
    snap = _validate_bonus_refund_reservation(reservation, agent_user_id, related_order_id)
    agent_id = int(agent_user_id)
    items = list(snap.get("grants") or [])
    locked_grants = _lock_bonus_refund_grants(cursor, items)
    cursor.execute(
        """SELECT paid_inventory_points,bonus_inventory_points,frozen_inventory_points
             FROM agent_inventory_wallets WHERE agent_user_id=%s FOR UPDATE""",
        (agent_id,),
    )
    wallet = cursor.fetchone()
    wallet = dict(wallet) if isinstance(wallet, dict) else _row_to_dict(wallet)
    releasable = int(snap.get("untracked_bonus_points") or 0)
    for item in items:
        grant = locked_grants[int(item["grant_id"])]
        expected_frozen = int(item.get("prior_frozen_points") or 0) + int(item.get("reserved_from_bonus") or 0)
        if not grant or int(grant.get("frozen_points") or 0) != expected_frozen:
            raise ValueError("订单赠送算力退款释放账本不一致")
        expires_at = grant.get("expires_at")
        cursor.execute("SELECT %s::timestamptz > NOW() AS not_expired", (expires_at,))
        expiry_row = cursor.fetchone()
        not_expired = bool(
            expiry_row.get("not_expired") if isinstance(expiry_row, dict) else expiry_row[0]
        )
        if not_expired:
            release_points = int(item.get("reserved_from_bonus") or 0)
            releasable += release_points
            cursor.execute(
                """UPDATE bonus_grants SET frozen_points=%s,status=%s
                     WHERE id=%s AND frozen_points=%s""",
                (int(item.get("prior_frozen_points") or 0), str(item.get("prior_status") or "active"),
                 int(item["grant_id"]), expected_frozen),
            )
        else:
            cursor.execute(
                """UPDATE bonus_grants SET status='expired_frozen',frozen_at=COALESCE(frozen_at,NOW())
                     WHERE id=%s AND frozen_points=%s""",
                (int(item["grant_id"]), expected_frozen),
            )
        if cursor.rowcount != 1:
            raise ValueError("订单赠送算力退款释放发生并发变化")
    if releasable:
        if not wallet or int(wallet.get("frozen_inventory_points") or 0) < releasable:
            raise ValueError("订单赠送算力退款释放余额不足")
        cursor.execute(
            """UPDATE agent_inventory_wallets
                  SET frozen_inventory_points=frozen_inventory_points-%s,
                      bonus_inventory_points=bonus_inventory_points+%s,updated_at=NOW()
                WHERE agent_user_id=%s AND frozen_inventory_points >= %s""",
            (releasable, releasable, agent_id, releasable),
        )
        if cursor.rowcount != 1:
            raise ValueError("订单赠送算力退款释放发生并发变化")
    return {"released_points": releasable, "grant_count": len(items)}


def revoke_agent_order_bonus_for_refund(
    cursor,
    agent_user_id: int,
    related_order_id: str,
    *,
    reservation: Dict[str, Any],
) -> Dict[str, Any]:
    """Finalize a persisted bonus reservation after cash refund proof succeeds."""
    snap = _validate_bonus_refund_reservation(reservation, agent_user_id, related_order_id)
    agent_id = int(agent_user_id)
    order_id = str(related_order_id)
    marker = f"refund_revoked:{order_id}"
    items = list(snap.get("grants") or [])
    total_remove = int(snap.get("total_reserved_points") or 0)
    locked_grants = _lock_bonus_refund_grants(cursor, items)
    cursor.execute(
        """SELECT paid_inventory_points,bonus_inventory_points,frozen_inventory_points
             FROM agent_inventory_wallets WHERE agent_user_id=%s FOR UPDATE""",
        (agent_id,),
    )
    wallet = cursor.fetchone()
    wallet = dict(wallet) if isinstance(wallet, dict) else _row_to_dict(wallet)
    already_revoked = 0
    for item in items:
        grant = locked_grants[int(item["grant_id"])]
        if marker in str(grant.get("note") or ""):
            already_revoked += 1
            continue
        expected_frozen = int(item.get("prior_frozen_points") or 0) + int(item.get("reserved_from_bonus") or 0)
        if (
            not grant or int(grant.get("consumed_points") or 0) != 0
            or int(grant.get("frozen_points") or 0) != expected_frozen
        ):
            raise ValueError("订单赠送算力退款终态账本不一致")
    if items and already_revoked == len(items):
        return {"revoked_points": total_remove, "grant_count": len(items), "idempotent": True}
    if already_revoked:
        raise ValueError("订单赠送算力退款终态只完成了一部分")
    if total_remove and (not wallet or int(wallet.get("frozen_inventory_points") or 0) < total_remove):
        raise ValueError("订单冻结赠送算力不足，禁止自动完成现金退款")
    if total_remove:
        cursor.execute(
            """UPDATE agent_inventory_wallets
                  SET frozen_inventory_points=frozen_inventory_points-%s,
                      total_purchased_points=GREATEST(0,total_purchased_points-%s),updated_at=NOW()
                WHERE agent_user_id=%s AND frozen_inventory_points >= %s
                RETURNING paid_inventory_points,bonus_inventory_points""",
            (total_remove, total_remove, agent_id, total_remove),
        )
        updated = cursor.fetchone()
        if not updated:
            raise ValueError("订单赠送算力并发变化，禁止自动完成现金退款")
        updated = dict(updated) if isinstance(updated, dict) else _row_to_dict(updated)
    else:
        updated = wallet or {"paid_inventory_points": 0, "bonus_inventory_points": 0}
    for item in items:
        cursor.execute(
            """UPDATE bonus_grants
                  SET consumed_points=granted_points,frozen_points=0,status='fully_consumed',
                      note=CONCAT_WS(' | ',NULLIF(note,''),%s)
                WHERE id=%s AND consumed_points=0 AND note IS DISTINCT FROM %s""",
            (marker, int(item["grant_id"]), marker),
        )
        if cursor.rowcount != 1:
            raise ValueError("订单赠送算力账本并发变化，禁止自动完成现金退款")
    if total_remove:
        cursor.execute(
            """INSERT INTO agent_inventory_transactions (
                   agent_user_id,type,pool,points,balance_paid_after,balance_bonus_after,
                   related_order_id,description)
                 VALUES (%s,'refund_clawback','bonus',%s,%s,%s,%s,%s)""",
            (agent_id, -total_remove, int(updated.get("paid_inventory_points") or 0),
             int(updated.get("bonus_inventory_points") or 0), order_id,
             "逐级转售退款扣回未使用赠送算力"),
        )
    return {"revoked_points": total_remove, "grant_count": len(items), "idempotent": False}


def _freeze_agent_bonus(cursor, owner_id: int, points: int) -> None:
    if points <= 0:
        return
    cursor.execute(
        """
        UPDATE agent_inventory_wallets
           SET bonus_inventory_points = GREATEST(0, bonus_inventory_points - %s),
               frozen_inventory_points = frozen_inventory_points + %s,
               updated_at = NOW()
         WHERE agent_user_id = %s
         RETURNING paid_inventory_points, bonus_inventory_points
        """,
        (points, points, owner_id),
    )
    row = cursor.fetchone()
    if row:
        paid_after = row["paid_inventory_points"] if isinstance(row, dict) else row[0]
        bonus_after = row["bonus_inventory_points"] if isinstance(row, dict) else row[1]
        cursor.execute(
            """
            INSERT INTO agent_inventory_transactions (
                agent_user_id, type, pool, points,
                balance_paid_after, balance_bonus_after, description
            ) VALUES (%s, 'admin_adjust', 'bonus', %s, %s, %s, %s)
            """,
            (owner_id, -points, paid_after, bonus_after, "赠送算力到期冻结"),
        )


def _freeze_customer_bonus(cursor, owner_id: int, points: int) -> None:
    """[单账本收敛 2026-07-27] 落点从 customer_agent_credit_wallets.bonus_credit_points
    改为 user_wallets.bonus_points —— 客户的赠送算力只有这一处。"""
    if points <= 0:
        return
    cursor.execute(
        """
        UPDATE user_wallets
           SET bonus_points = GREATEST(0, bonus_points - %s),
               updated_at = NOW()
         WHERE user_id = %s
        """,
        (points, owner_id),
    )


def expire_due_grants(cursor, limit: int = 500) -> Dict[str, Any]:
    """Freeze due active grants and remove their remaining wallet balance."""
    cursor.execute(
        """
        SELECT *
          FROM bonus_grants
         WHERE status = 'active'
           AND expires_at <= NOW()
           AND granted_points > consumed_points + frozen_points
         ORDER BY expires_at ASC, id ASC
         LIMIT %s
         FOR UPDATE
        """,
        (int(limit or 500),),
    )
    grants = [dict(r) if isinstance(r, dict) else _row_to_dict(r) for r in cursor.fetchall()]
    frozen_total = 0
    for grant in grants:
        points = int(grant["granted_points"]) - int(grant["consumed_points"]) - int(grant["frozen_points"])
        if points <= 0:
            continue
        if grant["owner_type"] == "agent":
            _freeze_agent_bonus(cursor, int(grant["owner_id"]), points)
        else:
            _freeze_customer_bonus(cursor, int(grant["owner_id"]), points)
        cursor.execute(
            """
            UPDATE bonus_grants
               SET frozen_points = frozen_points + %s,
                   status = 'expired_frozen',
                   frozen_at = NOW()
             WHERE id = %s
            """,
            (points, grant["id"]),
        )
        frozen_total += points
    return {"expired_count": len(grants), "frozen_points": frozen_total}


def _restore_agent_bonus(cursor, owner_id: int, points: int) -> None:
    if points <= 0:
        return
    cursor.execute(
        """
        UPDATE agent_inventory_wallets
           SET bonus_inventory_points = bonus_inventory_points + %s,
               frozen_inventory_points = GREATEST(0, frozen_inventory_points - %s),
               updated_at = NOW()
         WHERE agent_user_id = %s
         RETURNING paid_inventory_points, bonus_inventory_points
        """,
        (points, points, owner_id),
    )
    row = cursor.fetchone()
    if row:
        paid_after = row["paid_inventory_points"] if isinstance(row, dict) else row[0]
        bonus_after = row["bonus_inventory_points"] if isinstance(row, dict) else row[1]
        cursor.execute(
            """
            INSERT INTO agent_inventory_transactions (
                agent_user_id, type, pool, points,
                balance_paid_after, balance_bonus_after, description
            ) VALUES (%s, 'admin_adjust', 'bonus', %s, %s, %s, %s)
            """,
            (owner_id, points, paid_after, bonus_after, "赠送算力续期解冻"),
        )


def _restore_customer_bonus(cursor, owner_id: int, points: int) -> None:
    """[单账本收敛 2026-07-27] 落点同 _freeze_customer_bonus。"""
    if points <= 0:
        return
    cursor.execute(
        """
        UPDATE user_wallets
           SET bonus_points = bonus_points + %s,
               updated_at = NOW()
         WHERE user_id = %s
        """,
        (points, owner_id),
    )


def renew_grant(
    cursor,
    grant_id: int,
    extra_months: int = 12,
    owner_type: Optional[str] = None,
    owner_id: Optional[int] = None,
) -> Dict[str, Any]:
    cursor.execute("SELECT * FROM bonus_grants WHERE id = %s FOR UPDATE", (grant_id,))
    grant = cursor.fetchone()
    if not grant:
        raise ValueError("bonus grant not found")
    grant = dict(grant) if isinstance(grant, dict) else _row_to_dict(grant)
    if owner_type is not None and str(grant.get("owner_type")) != str(owner_type):
        raise ValueError("bonus grant owner mismatch")
    if owner_id is not None and int(grant.get("owner_id") or 0) != int(owner_id):
        raise ValueError("bonus grant owner mismatch")
    if grant.get("status") == "fully_consumed":
        raise ValueError("fully consumed grant cannot be renewed")
    frozen = int(grant.get("frozen_points") or 0)
    if frozen > 0:
        if grant["owner_type"] == "agent":
            _restore_agent_bonus(cursor, int(grant["owner_id"]), frozen)
        else:
            _restore_customer_bonus(cursor, int(grant["owner_id"]), frozen)
    cursor.execute(
        """
        UPDATE bonus_grants
           SET status = 'active',
               frozen_points = 0,
               expires_at = GREATEST(expires_at, NOW()) + (%s || ' months')::interval,
               renewed_at = NOW()
         WHERE id = %s
         RETURNING *
        """,
        (max(1, int(extra_months or 12)), grant_id),
    )
    row = cursor.fetchone()
    return dict(row) if isinstance(row, dict) else _row_to_dict(row)


def record_customer_bonus_grant_for_allocation(
    cursor,
    agent_user_id: int,
    customer_user_id: int,
    points: int,
    related_order_id: Optional[str] = None,
    source: str = "online_payment",
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Record customer-side bonus credit provenance.

    Existing wallet rows remain the balance SSOT. This helper makes new bonus
    allocations traceable without blocking legacy rows that predate grants.
    """
    from config.pricing_config import get_bonus_validity_months

    points = int(points or 0)
    if points <= 0:
        return {"grant": None, "source_consumed": {"requested_points": 0, "items": []}}

    source_key = str(source or "allocation").strip().lower() or "allocation"
    ref = related_order_id or f"{source_key}_{uuid.uuid4().hex[:16]}"
    consumed = {"requested_points": 0, "consumed_points": 0, "missing_points": 0, "legacy_untracked": False, "items": []}
    parent_grant_id = None
    grant_type = "customer_order_bonus"

    if source_key in {"offline_allocation", "grant_transfer", "agent_rebate"}:
        consumed = consume_bonus_fifo(cursor, "agent", agent_user_id, points)
        parent_grant_id = consumed["items"][0]["grant_id"] if consumed.get("items") else None
        grant_type = "allocate_from_grant"
    elif source_key == "admin_adjust":
        grant_type = "admin_adjust"

    grant_prefix = "alloc" if grant_type == "allocate_from_grant" else "customer_bonus"
    child = create_grant(
        cursor,
        owner_type="customer",
        owner_id=customer_user_id,
        granted_points=points,
        grant_type=grant_type,
        grant_key=f"{grant_prefix}:{source_key}:{agent_user_id}:{customer_user_id}:{ref}:{points}",
        expires_months=get_bonus_validity_months(),
        parent_grant_id=parent_grant_id,
        related_order_id=ref,
        note=note,
    )
    return {"grant": child, "source_consumed": consumed}


def reconcile_owner(cursor, owner_type: str, owner_id: int) -> Dict[str, Any]:
    cursor.execute(
        """
        SELECT pool_balance, grant_active_points, grant_frozen_points
          FROM v_bonus_grant_reconcile
         WHERE owner_type = %s AND owner_id = %s
        """,
        (owner_type, owner_id),
    )
    row = cursor.fetchone()
    if not row:
        return {"owner_type": owner_type, "owner_id": owner_id, "pool_balance": 0, "grant_active_points": 0, "grant_frozen_points": 0}
    return dict(row) if isinstance(row, dict) else _row_to_dict(row)
