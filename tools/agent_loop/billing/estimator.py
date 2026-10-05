"""Turn-level cost estimation for the Social Studio agent loop."""

from __future__ import annotations

from typing import Any

from .cost_map import estimate_tools_cost

SOFT_CONFIRM_POINTS = 50
HARD_CONFIRM_POINTS = 100


def estimate_turn_cost(
    tool_names: list[str],
    *,
    model_points: int = 0,
    user_is_admin: bool = False,
    grandfathered: bool = False,
) -> dict[str, Any]:
    raw_total = max(0, int(model_points or 0)) + estimate_tools_cost(tool_names)
    if user_is_admin:
        return _payload(raw_total=raw_total, total_points=0, decision="admin_exempt", requires_confirmation=False)
    if grandfathered:
        return _payload(raw_total=raw_total, total_points=0, decision="grandfathered", requires_confirmation=False)
    if raw_total > HARD_CONFIRM_POINTS:
        return _payload(raw_total=raw_total, total_points=raw_total, decision="hard_confirm", requires_confirmation=True)
    if raw_total > SOFT_CONFIRM_POINTS:
        return _payload(raw_total=raw_total, total_points=raw_total, decision="soft_confirm", requires_confirmation=True)
    return _payload(raw_total=raw_total, total_points=raw_total, decision="ok", requires_confirmation=False)


def _payload(*, raw_total: int, total_points: int, decision: str, requires_confirmation: bool) -> dict[str, Any]:
    return {
        "raw_total_points": raw_total,
        "total_points": total_points,
        "decision": decision,
        "requires_confirmation": requires_confirmation,
        "soft_line_points": SOFT_CONFIRM_POINTS,
        "hard_line_points": HARD_CONFIRM_POINTS,
    }
