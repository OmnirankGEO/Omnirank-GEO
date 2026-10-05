from __future__ import annotations

import asyncio

from db.profile_memory_db import list_profile_memory_events


def _sync_query(profile_id: str, concept: str, query: str, limit: int) -> dict:
    rows = list_profile_memory_events(
        profile_id,
        limit=limit,
        active_only=True,
        prompt_safe=True,
        review_statuses=("approved", "auto"),
        canonical_concept=concept or None,
        track_access=True,
    )
    items = [
        row for row in rows
        if not concept or row.get("canonical_concept") == concept
    ]
    if query:
        items = [
            row for row in items
            if query in (row.get("text") or "") or query in (row.get("title") or "")
        ] or items
    return {"source": "profile_memory_events", "profile_id": profile_id, "concept": concept, "items": items[:limit]}


async def query(profile_id: str, concept: str, query: str = "", limit: int = 10) -> dict:
    # H-P0-1 fix · sync DB call → to_thread (feedback_async_event_loop_block)
    return await asyncio.to_thread(_sync_query, profile_id, concept, query, limit)
