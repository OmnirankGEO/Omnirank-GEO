"""Cost confirmation contract for agent tool plans."""

from __future__ import annotations

from typing import Any


async def cost_estimate_and_confirm(
    *,
    planned_tools: list[str],
    estimated_points: int,
    output_mode: str,
    profile_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    points = int(estimated_points or 0)
    if points > 100:
        status = "requires_confirmation"
        limit = "hard"
    elif points > 50:
        status = "soft_confirm"
        limit = "soft"
    else:
        status = "ok"
        limit = "normal"
    return {
        "status": status,
        "limit": limit,
        "estimated_points": points,
        "planned_tools": planned_tools,
        "output_mode": output_mode,
        "profile_id": profile_id,
        "reason": reason or "",
    }
