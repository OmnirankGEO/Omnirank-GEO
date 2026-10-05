"""
Channel tier incentive engine.

All behavior is guarded by system_settings.CHANNEL_TIER_ENABLED. When the flag
is off, mount points must preserve legacy purchase and consumption behavior.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

logger = logging.getLogger("GEO-ChannelTier")

TIERS = ("none", "certified", "preferred", "strategic")
TIER_RANK = {tier: idx for idx, tier in enumerate(TIERS)}


def is_channel_tier_enabled(cursor) -> bool:
    # 行级共享锁保证订单创建事务内 flag 不会被并发 UPDATE 切换成另一套奖励语义。
    cursor.execute(
        "SELECT value FROM system_settings WHERE key='CHANNEL_TIER_ENABLED' LIMIT 1 FOR SHARE"
    )
    row = cursor.fetchone()
    if not row:
        return False
    value = row["value"] if isinstance(row, dict) else row[0]
    return str(value or "").strip().lower() == "true"


def compute_rolling_12m_yuan(
    cursor,
    agent_user_id: int,
    exclude_order_id: Optional[str] = None,
) -> Decimal:
    """Rolling 12-month net paid procurement amount, in yuan.

    Effective refunds reduce progress by the persisted refunded amount. Legacy
    effective rows without an amount are conservatively treated as full refunds;
    pending/failed refunds do not change progress.
    """
    from services.channel_revenue_lifecycle import refund_effective_sql

    exclude_clause = ""
    effective_sql, effective_params = refund_effective_sql("ro")
    params = [*effective_params, agent_user_id]
    if exclude_order_id:
        exclude_clause = "AND ro.id <> %s"
        params.append(exclude_order_id)
    cursor.execute(
        f"""
        SELECT COALESCE(SUM(GREATEST(
                   0,
                   amount_cents - CASE
                        WHEN {effective_sql}
                       THEN CASE
                           WHEN COALESCE(refunded_amount_cents, 0) > 0
                           THEN LEAST(amount_cents, refunded_amount_cents)
                           ELSE amount_cents
                       END
                       ELSE 0
                   END
               )), 0) AS amount_cents
          FROM recharge_orders ro
         WHERE ro.user_id = %s
           AND ro.order_type = 'agent_inventory_purchase'
           AND ro.payment_status = 'paid'
           AND COALESCE(ro.paid_at, ro.created_at) >= NOW() - INTERVAL '12 months'
           {exclude_clause}
        """,
        tuple(params),
    )
    row = cursor.fetchone()
    cents = row["amount_cents"] if isinstance(row, dict) else row[0]
    return Decimal(int(cents or 0)) / Decimal(100)


def determine_tier(rolling_12m_yuan: Decimal | float | int, pricing_config: Optional[Dict[str, Any]] = None) -> str:
    from config.pricing_config import get_agent_tier_config
    value = Decimal(str(rolling_12m_yuan or 0))
    config = (pricing_config or {}).get("agent_tier_config") if pricing_config is not None else get_agent_tier_config()
    config = config or {}
    result = "none"
    for tier in ("certified", "preferred", "strategic"):
        rule = config.get(tier) or {}
        if not bool(rule.get("is_enabled", True)):
            continue
        threshold = Decimal(str(rule.get("min_yuan", 0)))
        if value >= threshold:
            result = tier
    return result


def _direction(from_tier: str, to_tier: str) -> str:
    if from_tier == to_tier:
        return "init"
    return "upgrade" if TIER_RANK[to_tier] > TIER_RANK[from_tier] else "downgrade"


def _tier_change_idempotency_key(
    agent_user_id: int,
    old_tier: str,
    new_tier: str,
    trigger_source: str,
    related_order_id: Optional[str],
) -> str:
    # Purchase/admin events are naturally scoped by order id. Cron/no-order
    # transitions need a time bucket so later downgrade/upgrade cycles remain auditable.
    event_scope = related_order_id or datetime.now(timezone.utc).strftime("%Y%m")
    return f"tier:{agent_user_id}:{old_tier}:{new_tier}:{trigger_source}:{event_scope}"


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if not row:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _active_override_tier(row: Any) -> Optional[str]:
    tier = str(_row_get(row, "tier_override", "") or "").strip()
    if tier not in ("certified", "preferred", "strategic"):
        return None
    until = _parse_datetime(_row_get(row, "tier_override_until"))
    if not until:
        return tier
    if until.tzinfo:
        return tier if until > datetime.now(timezone.utc) else None
    return tier if until > datetime.now(timezone.utc).replace(tzinfo=None) else None


def _override_expired(row: Any) -> bool:
    return bool(_row_get(row, "tier_override")) and _active_override_tier(row) is None


def _higher_tier(*tiers: Optional[str]) -> str:
    """Return the highest valid tier; an admin override is a floor, not a cap."""
    valid = [tier for tier in tiers if tier in TIER_RANK]
    return max(valid, key=lambda tier: TIER_RANK[tier]) if valid else "none"


def evaluate_and_apply_tier(
    cursor,
    agent_user_id: int,
    trigger_source: str = "cron",
    related_order_id: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    rolling = compute_rolling_12m_yuan(cursor, agent_user_id)
    natural_tier = determine_tier(rolling)
    cursor.execute(
        """
        SELECT * FROM agent_channel_tier_state
         WHERE agent_user_id = %s
         FOR UPDATE
        """,
        (agent_user_id,),
    )
    row = cursor.fetchone()
    old_tier = (row["channel_tier"] if isinstance(row, dict) else row[1]) if row else "none"
    override_tier = _active_override_tier(row)
    new_tier = _higher_tier(natural_tier, override_tier)
    changed = old_tier != new_tier
    clear_expired_override = _override_expired(row)
    cursor.execute(
        """
        INSERT INTO agent_channel_tier_state (
            agent_user_id, channel_tier, rolling_12m_yuan,
            last_evaluated_at, tier_effective_at, updated_at
        ) VALUES (%s, %s, %s, NOW(), NOW(), NOW())
        ON CONFLICT (agent_user_id) DO UPDATE SET
            channel_tier = EXCLUDED.channel_tier,
            rolling_12m_yuan = EXCLUDED.rolling_12m_yuan,
            last_evaluated_at = NOW(),
            tier_effective_at = CASE
                WHEN agent_channel_tier_state.channel_tier IS DISTINCT FROM EXCLUDED.channel_tier
                THEN NOW() ELSE agent_channel_tier_state.tier_effective_at END,
            tier_override = CASE WHEN %s THEN NULL ELSE agent_channel_tier_state.tier_override END,
            tier_override_until = CASE WHEN %s THEN NULL ELSE agent_channel_tier_state.tier_override_until END,
            tier_override_by = CASE WHEN %s THEN NULL ELSE agent_channel_tier_state.tier_override_by END,
            tier_override_note = CASE WHEN %s THEN NULL ELSE agent_channel_tier_state.tier_override_note END,
            updated_at = NOW()
        RETURNING *
        """,
        (
            agent_user_id,
            new_tier,
            rolling,
            clear_expired_override,
            clear_expired_override,
            clear_expired_override,
            clear_expired_override,
        ),
    )
    state = cursor.fetchone()
    state_dict = dict(state) if isinstance(state, dict) else {}
    if changed or not row:
        idem = _tier_change_idempotency_key(agent_user_id, old_tier, new_tier, trigger_source, related_order_id)
        cursor.execute(
            """
            INSERT INTO agent_tier_change_log (
                agent_user_id, from_tier, to_tier, direction, rolling_12m_yuan,
                trigger_source, related_order_id, idempotency_key, note
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                agent_user_id,
                old_tier,
                new_tier,
                _direction(old_tier, new_tier),
                rolling,
                trigger_source,
                related_order_id,
                idem,
                note,
            ),
        )
    return {
        **state_dict,
        "old_tier": old_tier,
        "new_tier": new_tier,
        "natural_tier": natural_tier,
        "effective_tier": new_tier,
        "override_active": bool(override_tier),
        "changed": changed,
    }


def _tier_bonus_rate(tier: str, pricing_config: Optional[Dict[str, Any]] = None) -> Decimal:
    from config.pricing_config import get_agent_tier_config

    if tier not in ("certified", "preferred", "strategic"):
        return Decimal("0")
    cfg = (pricing_config or {}).get("agent_tier_config") if pricing_config is not None else get_agent_tier_config()
    rule = ((cfg or {}).get(tier) or {})
    if not bool(rule.get("is_enabled", True)):
        return Decimal("0")
    return Decimal(str(rule.get("bonus_rate", 0) or 0))


def get_agent_channel_tier_state(cursor, agent_user_id: int) -> Dict[str, Any]:
    enabled = is_channel_tier_enabled(cursor)
    rolling = compute_rolling_12m_yuan(cursor, agent_user_id)
    natural_tier = determine_tier(rolling)
    cursor.execute(
        """
        SELECT agent_user_id, channel_tier, rolling_12m_yuan, tier_override,
               tier_override_until, is_founder, founder_rank, first_order_done,
               tier_effective_at, last_evaluated_at, updated_at
          FROM agent_channel_tier_state
         WHERE agent_user_id = %s
         LIMIT 1
        """,
        (agent_user_id,),
    )
    row = cursor.fetchone()
    row_tier = str(_row_get(row, "channel_tier", "") or "").strip()
    stored_tier = row_tier if row_tier in TIERS else natural_tier
    override_tier = _active_override_tier(row)
    effective_tier = _higher_tier(natural_tier, override_tier)

    from config.pricing_config import get_agent_tier_config

    cfg = get_agent_tier_config()
    next_tier = None
    next_threshold = None
    for tier in ("certified", "preferred", "strategic"):
        rule = cfg.get(tier) or {}
        if not bool(rule.get("is_enabled", True)):
            continue
        if TIER_RANK[tier] <= TIER_RANK[effective_tier]:
            continue
        threshold = Decimal(str(rule.get("min_yuan", 0)))
        next_tier = tier
        next_threshold = threshold
        break
    gap = max(Decimal("0"), (next_threshold or rolling) - rolling)

    return {
        "enabled": bool(enabled),
        "agent_user_id": int(agent_user_id),
        "natural_tier": natural_tier,
        "channel_tier": stored_tier,
        "effective_tier": effective_tier,
        "override_active": bool(override_tier),
        "benefit_floor_tier": override_tier,
        "benefit_floor_rate": float(_tier_bonus_rate(override_tier or "none")),
        "natural_bonus_rate": float(_tier_bonus_rate(natural_tier)),
        "effective_bonus_rate": float(_tier_bonus_rate(effective_tier)),
        "tier_override": _row_get(row, "tier_override"),
        "tier_override_until": _row_get(row, "tier_override_until"),
        "rolling_12m_yuan": float(rolling),
        "next_tier": next_tier,
        "next_threshold_yuan": float(next_threshold) if next_threshold is not None else None,
        "gap_to_next_yuan": float(gap),
        "bonus_rate": float(_tier_bonus_rate(effective_tier)),
        "is_founder": bool(_row_get(row, "is_founder", False)),
        "founder_rank": _row_get(row, "founder_rank"),
        "first_order_done": bool(_row_get(row, "first_order_done", False)),
        "tier_effective_at": _row_get(row, "tier_effective_at"),
        "last_evaluated_at": _row_get(row, "last_evaluated_at"),
        "updated_at": _row_get(row, "updated_at"),
    }


def compute_tier_bonus_points(cursor, agent_user_id: int, amount_cents: int, tier: str) -> int:
    """Use the live purchase base helper so display and grant paths share pricing math."""
    rate = _tier_bonus_rate(tier)
    if rate <= 0 or amount_cents <= 0:
        return 0
    from services.agent_pricing import calc_prepay_points

    base_points = calc_prepay_points(int(amount_cents), agent_user_id)
    return int(round(base_points * rate))


def compute_purchase_bonus_projection(
    cursor,
    agent_user_id: int,
    amount_cents: int,
    *,
    rolling_before_yuan: Optional[Decimal | float | int] = None,
    exclude_order_id: Optional[str] = None,
    pricing_config: Optional[Dict[str, Any]] = None,
    base_points: Optional[int] = None,
) -> Dict[str, Any]:
    """Project the post-purchase channel tier and bonus for one inventory purchase.

    This is the single source used by both purchase-option display and the paid
    order settlement path. When settlement calls this after the order has already
    been marked paid, pass exclude_order_id so the current order is not counted
    twice before adding this purchase back into the projection.
    """
    amount = int(amount_cents or 0)
    if rolling_before_yuan is None:
        rolling_before = compute_rolling_12m_yuan(
            cursor,
            int(agent_user_id),
            exclude_order_id=exclude_order_id,
        )
    else:
        rolling_before = Decimal(str(rolling_before_yuan or 0))

    purchase_yuan = Decimal(amount) / Decimal(100)
    projected_rolling = rolling_before + purchase_yuan
    natural_before_tier = determine_tier(rolling_before, pricing_config)
    natural_projected_tier = determine_tier(projected_rolling, pricing_config)
    cursor.execute(
        "SELECT tier_override, tier_override_until FROM agent_channel_tier_state WHERE agent_user_id=%s",
        (int(agent_user_id),),
    )
    override_row = cursor.fetchone()
    override_tier = _active_override_tier(override_row)
    effective_before_tier = _higher_tier(natural_before_tier, override_tier)
    projected_tier = _higher_tier(natural_projected_tier, override_tier)

    if base_points is None:
        from services.agent_pricing import calc_prepay_points
        base_points = calc_prepay_points(amount, agent_user_id)
    rate = _tier_bonus_rate(projected_tier, pricing_config)
    from services.agent_inventory_pricing import decimal_rate_to_bps, points_at_rate
    rate_bps = decimal_rate_to_bps(rate)
    bonus_points = points_at_rate(int(base_points or 0), rate_bps) if amount > 0 else 0
    return {
        "rolling_before_yuan": rolling_before,
        "purchase_yuan": purchase_yuan,
        "projected_rolling_12m_yuan": projected_rolling,
        "natural_projected_tier": natural_projected_tier,
        "natural_before_tier": natural_before_tier,
        "effective_before_tier": effective_before_tier,
        "projected_tier": projected_tier,
        "benefit_floor_tier": override_tier,
        "tier_source": "benefit_floor" if override_tier and projected_tier == override_tier else "automatic",
        "crosses_tier_threshold": TIER_RANK[projected_tier] > TIER_RANK[effective_before_tier],
        "tier_override_until": _row_get(override_row, "tier_override_until"),
        "base_points": int(base_points or 0),
        "bonus_points": int(bonus_points or 0),
        "bonus_rate_bps": rate_bps,
    }


def grab_founder_seat(cursor, agent_user_id: int, *, cap_snapshot: Optional[int] = None) -> Dict[str, Any]:
    from config.pricing_config import get_founding_config

    cap = (
        int(cap_snapshot)
        if cap_snapshot is not None
        else int((get_founding_config() or {}).get("cap", 10) or 10)
    )
    if cap < 0:
        raise ValueError("创始席位上限非法")
    if cap_snapshot is None:
        cursor.execute(
            """
            INSERT INTO founder_seats (id, used, cap)
            VALUES (1, 0, %s)
            ON CONFLICT (id) DO UPDATE SET
                cap = GREATEST(founder_seats.used, EXCLUDED.cap),
                updated_at = NOW()
            RETURNING used, cap
            """,
            (cap,),
        )
    else:
        # 待支付订单可保留创建时的活动上限，但不得用较旧/较小快照把当前容量调低。
        cursor.execute(
            """
            INSERT INTO founder_seats (id, used, cap)
            VALUES (1, 0, %s)
            ON CONFLICT (id) DO UPDATE SET
                cap = GREATEST(founder_seats.cap, EXCLUDED.cap),
                updated_at = NOW()
            RETURNING used, cap
            """,
            (cap,),
        )
    cursor.execute(
        "SELECT is_founder, founder_rank FROM agent_channel_tier_state WHERE agent_user_id=%s FOR UPDATE",
        (agent_user_id,),
    )
    state = cursor.fetchone()
    if state and (state["is_founder"] if isinstance(state, dict) else state[0]):
        rank = state["founder_rank"] if isinstance(state, dict) else state[1]
        return {"is_founder": True, "founder_rank": rank, "grabbed": False}

    cursor.execute("SELECT used, cap FROM founder_seats WHERE id=1 FOR UPDATE")
    seat = cursor.fetchone()
    used = int(seat["used"] if isinstance(seat, dict) else seat[0])
    stored_cap = int(seat["cap"] if isinstance(seat, dict) else seat[1])
    seat_cap = cap if cap_snapshot is not None else stored_cap
    if used >= seat_cap:
        return {"is_founder": False, "founder_rank": None, "grabbed": False}
    rank = used + 1
    cursor.execute("UPDATE founder_seats SET used=%s, updated_at=NOW() WHERE id=1", (rank,))
    cursor.execute(
        """
        INSERT INTO agent_channel_tier_state (agent_user_id, is_founder, founder_rank, updated_at)
        VALUES (%s, TRUE, %s, NOW())
        ON CONFLICT (agent_user_id) DO UPDATE SET
            is_founder = TRUE,
            founder_rank = COALESCE(agent_channel_tier_state.founder_rank, EXCLUDED.founder_rank),
            updated_at = NOW()
        """,
        (agent_user_id, rank),
    )
    return {"is_founder": True, "founder_rank": rank, "grabbed": True}


def is_first_order(
    cursor,
    agent_user_id: int,
    related_order_id: Optional[str] = None,
    amount_yuan: Optional[Decimal | float | int] = None,
    min_first_order_yuan: Optional[Decimal | float | int] = None,
) -> bool:
    from config.pricing_config import get_founding_config

    min_yuan = Decimal(str(
        min_first_order_yuan
        if min_first_order_yuan is not None
        else ((get_founding_config() or {}).get("min_first_order_yuan", 500) or 500)
    ))
    params = [agent_user_id, related_order_id, related_order_id]
    cursor.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS prior_amount_cents
          FROM recharge_orders
         WHERE user_id = %s
           AND order_type = 'agent_inventory_purchase'
           AND payment_status = 'paid'
           AND (%s IS NULL OR id <> %s)
        """,
        params,
    )
    row = cursor.fetchone()
    prior_amount_cents = int((row["prior_amount_cents"] if isinstance(row, dict) else row[0]) or 0)
    prior_yuan = Decimal(prior_amount_cents) / Decimal(100)
    if amount_yuan is None:
        return prior_yuan < min_yuan
    current_yuan = Decimal(str(amount_yuan or 0))
    return prior_yuan < min_yuan <= (prior_yuan + current_yuan)


def grant_tier_bonus(
    cursor,
    agent_user_id: int,
    amount_cents: int,
    tier: str,
    related_order_id: str,
    bonus_points: Optional[int] = None,
    bonus_rate_bps: Optional[int] = None,
    expires_months: Optional[int] = None,
) -> Dict[str, Any]:
    from config.pricing_config import get_bonus_validity_months
    from services.agent_inventory import purchase_inventory_prepay
    from services.bonus_grants import create_grant

    points = int(bonus_points) if bonus_points is not None else compute_tier_bonus_points(
        cursor,
        agent_user_id,
        amount_cents,
        tier,
    )
    if points <= 0:
        return {"granted_points": 0, "grant": None}
    grant = create_grant(
        cursor,
        owner_type="agent",
        owner_id=agent_user_id,
        granted_points=points,
        grant_type="tier_purchase",
        grant_key=f"tier_purchase:{agent_user_id}:{related_order_id}",
        expires_months=int(expires_months) if expires_months is not None else get_bonus_validity_months(),
        tier_at_grant=tier,
        bonus_rate_used=(Decimal(int(bonus_rate_bps)) / Decimal(10000)) if bonus_rate_bps is not None else _tier_bonus_rate(tier),
        related_order_id=related_order_id,
    )
    created = bool(grant.get("_created"))
    if created:
        purchase_inventory_prepay(
            cursor,
            agent_user_id=agent_user_id,
            paid_points=0,
            bonus_points=points,
            related_order_id=related_order_id,
            description=f"渠道等级奖励({tier})",
        )
    return {"granted_points": points if created else 0, "grant": grant}


def grant_founder_first_order_bonus(
    cursor,
    agent_user_id: int,
    base_points: int,
    related_order_id: str,
    bonus_points: Optional[int] = None,
    bonus_rate_bps: Optional[int] = None,
    expires_months: Optional[int] = None,
) -> Dict[str, Any]:
    from config.pricing_config import get_bonus_validity_months, get_founding_config
    from services.agent_inventory import purchase_inventory_prepay
    from services.bonus_grants import create_grant

    cursor.execute(
        "SELECT is_founder, first_order_done FROM agent_channel_tier_state WHERE agent_user_id=%s FOR UPDATE",
        (agent_user_id,),
    )
    state = cursor.fetchone()
    if not state:
        return {"granted_points": 0, "reason": "no_state"}
    is_founder = state["is_founder"] if isinstance(state, dict) else state[0]
    first_done = state["first_order_done"] if isinstance(state, dict) else state[1]
    if not is_founder or first_done:
        return {"granted_points": 0, "reason": "not_founder_or_done"}
    rate = ((Decimal(int(bonus_rate_bps)) / Decimal(10000)) if bonus_rate_bps is not None
            else Decimal(str((get_founding_config() or {}).get("first_order_extra_bonus", 0.10) or 0)))
    points = int(bonus_points) if bonus_points is not None else int((Decimal(int(base_points or 0)) * rate).quantize(Decimal("1")))
    if points <= 0:
        return {"granted_points": 0, "reason": "zero"}
    grant = create_grant(
        cursor,
        owner_type="agent",
        owner_id=agent_user_id,
        granted_points=points,
        grant_type="founder_first_order",
        grant_key=f"founder_first_order:{agent_user_id}:{related_order_id}",
        expires_months=int(expires_months) if expires_months is not None else get_bonus_validity_months(),
        bonus_rate_used=rate,
        related_order_id=related_order_id,
    )
    created = bool(grant.get("_created"))
    if created:
        purchase_inventory_prepay(
            cursor,
            agent_user_id=agent_user_id,
            paid_points=0,
            bonus_points=points,
            related_order_id=related_order_id,
            description="创始席首单额外奖励",
        )
        cursor.execute(
            "UPDATE agent_channel_tier_state SET first_order_done=TRUE, updated_at=NOW() WHERE agent_user_id=%s",
            (agent_user_id,),
        )
    return {"granted_points": points if created else 0, "grant": grant}
