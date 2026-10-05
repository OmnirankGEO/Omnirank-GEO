"""W9 write tool · update_plan · P0-8 fix · SSOT §2.4b 明文要求

用户改 plan(中途加 step / 改 step 顺序)· LLM 调此 tool · upsert_active_plan 更新。
requires_confirmation=true · 走 ABCD popover 让用户确认改动。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("GEO-AgentLoop")


async def update_plan(
    *,
    plan_id: str,
    new_state_json: str,
    reason: str,
    ctx_user_id: int | None = None,
) -> dict[str, Any]:
    # 解析 LLM 传的 new_state JSON
    try:
        parsed_state = json.loads(new_state_json)
        if not isinstance(parsed_state, dict):
            raise ValueError("new_state_json must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"invalid new_state_json: {exc}",
            "plan_id": plan_id,
        }

    if not isinstance(parsed_state.get("steps"), list) or not parsed_state["steps"]:
        return {
            "status": "error",
            "error": "new_state_json must contain non-empty 'steps' array",
            "plan_id": plan_id,
        }

    def _sync_update() -> dict[str, Any] | None:
        from db.social_agent_plans import get_plan_by_id, upsert_active_plan

        existing = get_plan_by_id(plan_id)
        if existing is None:
            return None
        # 注:upsert 走 active 状态(覆盖现有 active plan · 维持单 active 唯一性)
        # 阶段 2 会改 upsert 为 "archive old → INSERT new"(P0-10)
        return upsert_active_plan(
            user_id=int(existing["user_id"]),
            profile_id=str(existing["profile_id"]),
            state_json=parsed_state,
            user_intent_summary=str(parsed_state.get("user_intent_summary") or existing.get("user_intent_summary") or ""),
        )

    # H-P0-1 配套:sync DB 必走 to_thread
    row = await asyncio.to_thread(_sync_update)
    if row is None:
        return {
            "status": "error",
            "error": f"plan_id={plan_id} not found",
            "plan_id": plan_id,
        }
    return {
        "status": "updated",
        "plan_id": row.get("plan_id"),
        "current_step": row.get("current_step"),
        "user_intent_summary": row.get("user_intent_summary"),
        "reason": reason,
        "requires_confirmation": True,
        "requires_user_ack": True,
    }
