"""Lightweight billing regression checks for Social Agent Loop.

This script intentionally does not mutate wallets. It verifies the agent loop
billing layer is isolated from middleware/billing.py and that tool costs remain
aligned with the declared tool schemas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))

    from tools.agent_loop.billing.cost_map import TOOL_COST_POINTS
    from tools.agent_loop.billing.estimator import estimate_turn_cost
    from tools.agent_loop.tool_definitions import TOOL_SCHEMAS

    billing_src = (root / "middleware" / "billing.py").read_text(encoding="utf-8")
    router_src = (root / "tools" / "agent_loop" / "tool_router.py").read_text(encoding="utf-8")
    schema_names = {schema["name"] for schema in TOOL_SCHEMAS}
    checks = {
        "cost_map_covers_all_tools": set(TOOL_COST_POINTS) == schema_names,
        "middleware_public_api_available": all(
            token in billing_src for token in ("async def deduct_points", "async def refund_points")
        ),
        "agent_router_uses_wrapper_not_middleware": "middleware.billing" not in router_src
        and "charge_tool_call" in router_src,
        "soft_line_triggers": estimate_turn_cost(["tikhub_parse_video"] * 8)["decision"] == "soft_confirm",
        "hard_line_triggers": estimate_turn_cost(["tikhub_parse_video"] * 13)["decision"] == "hard_confirm",
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
