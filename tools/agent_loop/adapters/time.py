from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


async def now(timezone: str = "Asia/Shanghai") -> dict:
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        timezone = "UTC"
        zone = ZoneInfo("UTC")
    current = datetime.now(zone)
    return {
        "timezone": timezone,
        "iso": current.isoformat(),
        "date": current.date().isoformat(),
        "hour": current.hour,
    }
