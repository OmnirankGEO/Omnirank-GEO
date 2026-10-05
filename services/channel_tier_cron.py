"""Cron helpers for channel-tier bonus expiry and tier refresh.

Role gate contract for blue/green deployments:
- Default CHANNEL_TIER_CRON_ROLE_GATE=any preserves current flag-off behavior.
- For production flag-on pilots, set the sole scheduler container ROLE to the
  same explicit value as CHANNEL_TIER_CRON_ROLE_GATE, and set standby containers
  to a different ROLE (or leave them stopped).
- Bonus grant idempotency and row locks still protect against accidental double
  runs; the role gate is an operator-facing guardrail, not the only safety net.
"""

import logging
import os

logger = logging.getLogger("GEO-ChannelTier-Cron")


def _log_bonus_grant_reconcile_drift(cursor, limit: int = 20) -> int:
    """Log bonus ledger drift without mutating balances."""
    cursor.execute(
        """
        SELECT owner_type, owner_id, pool_balance, grant_active_points, grant_frozen_points
          FROM v_bonus_grant_reconcile
         WHERE pool_balance <> grant_active_points
           AND (grant_active_points > 0 OR grant_frozen_points > 0)
         ORDER BY owner_type, owner_id
         LIMIT %s
        """,
        (max(1, int(limit or 20)),),
    )
    rows = cursor.fetchall() or []
    if rows:
        logger.warning(
            "bonus grant reconcile drift detected · sample_count=%s rows=%s",
            len(rows),
            [dict(r) if isinstance(r, dict) else r for r in rows],
        )
    return len(rows)


def run_channel_tier_hourly(limit: int = 500) -> dict:
    from db.connection import get_db
    from services.bonus_grants import expire_due_grants
    from services.channel_tier import is_channel_tier_enabled

    with get_db() as conn:
        cur = conn.cursor()
        if not is_channel_tier_enabled(cur):
            return {"enabled": False, "expired_count": 0, "frozen_points": 0}
        result = expire_due_grants(cur, limit=limit)
        reconcile_warning_count = _log_bonus_grant_reconcile_drift(cur)
        conn.commit()
        return {"enabled": True, **result, "reconcile_warning_count": reconcile_warning_count}


def register_channel_tier_jobs(scheduler) -> None:
    allowed_role = os.environ.get("CHANNEL_TIER_CRON_ROLE_GATE", "any").strip().lower() or "any"
    current_role = os.environ.get("ROLE", "").strip().lower()
    if allowed_role not in ("any", "*") and current_role != allowed_role:
        logger.info(
            "skip channel tier cron registration · ROLE=%s required=%s",
            current_role or "-",
            allowed_role,
        )
        return
    if scheduler.get_job("channel_tier_bonus_expiry_hourly"):
        return
    scheduler.add_job(
        run_channel_tier_hourly,
        "interval",
        hours=1,
        id="channel_tier_bonus_expiry_hourly",
        replace_existing=True,
        max_instances=1,
    )
    logger.info("registered channel tier hourly bonus-expiry job")
