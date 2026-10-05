from __future__ import annotations

import asyncio


def _sync_search(profile_id: str, query: str, limit: int) -> dict:
    from db.social_agent_sessions import search_agent_sessions

    items = search_agent_sessions(profile_id=profile_id, query=query, limit=limit)
    return {
        "source": "social_agent_sessions",
        "profile_id": profile_id,
        "query": query,
        "limit": limit,
        "status": "ok",
        "items": items,
    }


async def search(profile_id: str, query: str, limit: int = 5) -> dict:
    # H-P0-1 fix · sync DB call → to_thread (feedback_async_event_loop_block)
    return await asyncio.to_thread(_sync_search, profile_id, query, limit)
