"""Structured conflict-resolution action for profile memories."""

from __future__ import annotations

from typing import Any

from db.profile_memory_db import record_profile_memory_event, review_profile_memory_event


async def confirm_memory_conflict(
    *,
    profile_id: str,
    concept: str,
    old_event_id: str,
    new_text: str,
    options: list[str],
    resolution: str | None = None,
    ctx_user_id: int | None = None,
) -> dict[str, Any]:
    if not resolution:
        return {
            "status": "confirmation_required",
            "profile_id": profile_id,
            "concept": concept,
            "old_event_id": old_event_id,
            "new_text": new_text,
            "options": options,
        }
    if resolution == "use_latest":
        event_id = record_profile_memory_event(
            profile_id=profile_id,
            source="agent_loop",
            event_type="memory_conflict_resolution",
            title="冲突记忆已确认",
            text=new_text,
            dimension=concept,
            canonical_concept=concept,
            raw_payload={"tool": "confirm_memory_conflict", "old_event_id": old_event_id},
            confidence=1.0,
            user_id=ctx_user_id,
            review_status="auto",
        )
        archived = review_profile_memory_event(
            int(old_event_id),
            is_active=False,
            reviewed_by_user_id=ctx_user_id,
            review_status="dismissed",
            notes=f"conflict_use_latest:{event_id}",
        )
        return {"status": "resolved", "event_id": event_id, "archived_old": bool(archived)}
    return {"status": "confirmation_required", "options": options, "unsupported_resolution": resolution}
