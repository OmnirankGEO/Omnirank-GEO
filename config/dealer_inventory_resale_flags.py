"""Authoritative dealer-inventory-resale gate.

The flag is independent from CHANNEL_PRICING_ENABLED, defaults false in migration,
and is read directly from PostgreSQL for every financial write. Redis/cache failure
cannot turn it on or hide an already-created resale order's settlement obligation.
"""

from __future__ import annotations

from db.connection import get_db


FLAG_KEY = "DEALER_INVENTORY_RESALE_ENABLED"


class DealerResaleFlagUnavailable(RuntimeError):
    pass


def enabled(cur=None) -> bool:
    try:
        if cur is not None:
            cur.execute("SELECT value FROM system_settings WHERE key=%s", (FLAG_KEY,))
            row = cur.fetchone()
        else:
            with get_db() as conn:
                db_cur = conn.cursor()
                db_cur.execute("SELECT value FROM system_settings WHERE key=%s", (FLAG_KEY,))
                row = db_cur.fetchone()
        if not row:
            return False
        value = row.get("value") if isinstance(row, dict) else row[0]
        return str(value).strip().lower() in {"true", "1", "yes", "on"}
    except Exception as exc:  # noqa: BLE001
        raise DealerResaleFlagUnavailable("逐级库存转售开关状态暂不可用") from exc
