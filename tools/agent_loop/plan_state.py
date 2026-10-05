"""Plan-state extraction and persistence for multi-step agent tasks."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .tool_router import AgentToolContext


def extract_plan_state(message: dict[str, Any]) -> dict[str, Any] | None:
    raw = message.get("plan_state")
    if isinstance(raw, dict):
        return _normalize_plan_state(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return _normalize_plan_state(parsed) if isinstance(parsed, dict) else None
    content = message.get("content")
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and isinstance(parsed.get("plan_state"), dict):
            return _normalize_plan_state(parsed["plan_state"])
    return None


async def maybe_persist_plan_state(message: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any] | None:
    state = extract_plan_state(message)
    if not state or ctx.user_id is None or not ctx.profile_id:
        return None
    summary = str(state.get("user_intent_summary") or state.get("summary") or message.get("content") or "")[:500]

    def _save() -> dict[str, Any]:
        from db.social_agent_plans import upsert_active_plan

        return upsert_active_plan(
            user_id=int(ctx.user_id),
            profile_id=str(ctx.profile_id),
            state_json=state,
            user_intent_summary=summary,
        )

    row = await asyncio.to_thread(_save)
    return {
        "type": "plan_state",
        "plan_id": row["plan_id"],
        "profile_id": row["profile_id"],
        "current_step": row["current_step"],
        "status": row["status"],
        "steps": state.get("steps") or [],
        "user_intent_summary": summary,
    }


def build_plan_state_prompt(active_plan: dict[str, Any] | None) -> str:
    if not active_plan:
        return "当前没有未完成的多步计划。若用户提出先后顺序，请输出 plan_state 并按步骤执行。"
    state = active_plan.get("state_json") or {}
    steps = state.get("steps") or []
    current_step = active_plan.get("current_step", 0)
    return (
        "当前有未完成的多步计划。不要跳步；先确认当前步骤是否继续。\n"
        f"计划摘要:{active_plan.get('user_intent_summary') or ''}\n"
        f"当前步骤:{current_step}\n"
        f"步骤列表:{json.dumps(steps, ensure_ascii=False)}"
    )


def _normalize_plan_state(raw: dict[str, Any]) -> dict[str, Any] | None:
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    state = dict(raw)
    state["status"] = str(state.get("status") or "active")
    try:
        state["current_step"] = int(state.get("current_step", 0))
    except Exception:
        state["current_step"] = 0
    return state
