"""Archive a memory event without deleting its audit trail."""

from __future__ import annotations

from typing import Any

from db.profile_memory_db import review_profile_memory_event


async def archive_memory_event(
    *,
    event_id: str,
    reason: str,
    profile_id: str | None = None,
    ctx_user_id: int | None = None,
) -> dict[str, Any]:
    changed = review_profile_memory_event(
        int(event_id),
        is_active=False,
        reviewed_by_user_id=ctx_user_id,
        review_status="dismissed",
        notes=reason,
    )
    return {"status": "dismissed" if changed else "not_found", "event_id": event_id, "changed": bool(changed)}
