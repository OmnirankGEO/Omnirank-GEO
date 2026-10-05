"""Cost map for Social Studio agent-loop tools."""

from __future__ import annotations

from tools.agent_loop.tool_definitions import TOOL_SCHEMAS

TOOL_COST_POINTS: dict[str, int] = {
    schema["name"]: int(schema.get("cost_estimate_points") or 0)
    for schema in TOOL_SCHEMAS
}


def get_tool_cost(tool_name: str) -> int:
    return int(TOOL_COST_POINTS.get(tool_name, 0))


def estimate_tools_cost(tool_names: list[str]) -> int:
    return sum(get_tool_cost(name) for name in tool_names)
