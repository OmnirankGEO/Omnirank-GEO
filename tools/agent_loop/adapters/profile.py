from __future__ import annotations

import asyncio

from db.profile_db import get_profile as get_client_profile


def _sync_get_selected(profile_id: str, fields: list[str]) -> dict:
    profile = get_client_profile(profile_id) or {}
    selected = {field: profile.get(field) for field in fields}
    social_fields = profile.get("social_fields") if isinstance(profile.get("social_fields"), dict) else {}
    for field in fields:
        if selected.get(field) is None and isinstance(social_fields, dict):
            selected[field] = social_fields.get(field)
    return {"source": "client_profiles", "profile_id": profile_id, "profile": selected}


async def get_selected(profile_id: str, fields: list[str]) -> dict:
    # H-P0-1 fix · sync DB call must go through to_thread to avoid blocking event loop
    # (重蹈 2026-05-07 event loop 血坑 · feedback_async_event_loop_block)
    return await asyncio.to_thread(_sync_get_selected, profile_id, fields)
