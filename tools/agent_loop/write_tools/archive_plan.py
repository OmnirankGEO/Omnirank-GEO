"""W8 write tool · archive_plan · P0-8 fix · SSOT §2.4b 明文要求

用户中途说"算了"/"换话题" · LLM 调此 tool · plan 转 cancelled/completed/superseded。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("GEO-AgentLoop")

ALLOWED_STATUS = {"cancelled", "completed", "superseded"}


async def archive_plan(
    *,
    plan_id: str,
    reason: str,
    new_status: str,
    ctx_user_id: int | None = None,
) -> dict[str, Any]:
    if new_status not in ALLOWED_STATUS:
        return {
            "status": "error",
            "error": f"invalid new_status: {new_status}",
            "allowed": sorted(ALLOWED_STATUS),
        }

    def _sync_archive() -> dict[str, Any] | None:
        from db.social_agent_plans import update_plan_status

        try:
            return update_plan_status(plan_id, new_status)
        except ValueError as exc:
            logger.warning(f"[archive_plan] invalid status: {exc}")
            return None

    # H-P0-1 配套:sync DB 必走 to_thread 防 event loop block
    row = await asyncio.to_thread(_sync_archive)
    if row is None:
        return {
            "status": "error",
            "error": f"plan_id={plan_id} not found or update failed",
            "plan_id": plan_id,
        }
    return {
        "status": "archived",
        "plan_id": row.get("plan_id"),
        "new_status": row.get("status"),
        "reason": reason,
    }
