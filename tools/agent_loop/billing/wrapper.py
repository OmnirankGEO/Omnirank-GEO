"""Agent-loop billing wrapper.

This module deliberately wraps public middleware.billing APIs instead of
modifying middleware/billing.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

AGENT_TOOL_FEATURE_CODE = "agent_tool_call"

DeductFunc = Callable[..., Awaitable[dict[str, Any]]]
RefundFunc = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AgentBillingContext:
    user_id: int | None
    brand_id: int | None = None
    is_admin: bool = False
    grandfathered: bool = False


@dataclass(frozen=True)
class ToolCharge:
    tool_name: str
    user_id: int | None
    feature_code: str = AGENT_TOOL_FEATURE_CODE
    cost_points: int = 0
    deducted_points: int = 0
    refunded_points: int = 0
    charged: bool = False
    billing_status: str = "not_charged"
    reason: str = ""
    raw: dict[str, Any] | None = None


async def charge_tool_call(
    tool_name: str,
    cost_points: int,
    ctx: AgentBillingContext,
    *,
    deduct_func: DeductFunc | None = None,
) -> ToolCharge:
    safe_cost = max(0, int(cost_points or 0))
    if safe_cost <= 0:
        return ToolCharge(tool_name=tool_name, user_id=ctx.user_id, cost_points=0, billing_status="free")
    if not ctx.user_id:
        return ToolCharge(tool_name=tool_name, user_id=None, cost_points=safe_cost, billing_status="no_user")
    if ctx.is_admin:
        return ToolCharge(tool_name=tool_name, user_id=ctx.user_id, cost_points=safe_cost, billing_status="admin_exempt")
    if ctx.grandfathered:
        return ToolCharge(tool_name=tool_name, user_id=ctx.user_id, cost_points=safe_cost, billing_status="grandfathered")

    try:
        deduct = deduct_func or _default_deduct_points
        if deduct_func is None:
            _ensure_agent_tool_feature_pricing()
        result = await deduct(ctx.user_id, AGENT_TOOL_FEATURE_CODE, extra_cost=safe_cost, brand_id=ctx.brand_id)
        deducted = int(result.get("deducted") or safe_cost)
        return ToolCharge(
            tool_name=tool_name,
            user_id=ctx.user_id,
            cost_points=safe_cost,
            deducted_points=max(0, deducted),
            charged=deducted > 0,
            billing_status="charged" if deducted > 0 else "free",
            raw=result,
        )
    except Exception as exc:
        logger.warning("agent tool billing failed tool=%s user=%s: %s", tool_name, ctx.user_id, exc)
        return ToolCharge(
            tool_name=tool_name,
            user_id=ctx.user_id,
            cost_points=safe_cost,
            billing_status="billing_failed",
            reason=f"{type(exc).__name__}:{str(exc)[:200]}",
        )


async def refund_tool_call(
    charge: ToolCharge,
    reason: str,
    *,
    refund_func: RefundFunc | None = None,
) -> ToolCharge:
    if not charge.charged or not charge.user_id:
        return charge
    try:
        refund = refund_func or _default_refund_points
        result = await refund(charge.user_id, charge.feature_code, reason)
        refunded = int(result.get("refunded") or charge.deducted_points or 0)
        return replace(
            charge,
            refunded_points=max(0, refunded),
            billing_status="refunded" if refunded > 0 else "refund_noop",
            raw=result,
        )
    except Exception as exc:
        logger.warning("agent tool refund failed tool=%s user=%s: %s", charge.tool_name, charge.user_id, exc)
        return replace(charge, billing_status="refund_failed", reason=f"{type(exc).__name__}:{str(exc)[:200]}")


async def _default_deduct_points(user_id: int, feature_code: str, *, extra_cost: int = 0, brand_id: int | None = None):
    from middleware.billing import deduct_points

    return await deduct_points(user_id, feature_code, extra_cost=extra_cost, brand_id=brand_id)


async def _default_refund_points(user_id: int, feature_code: str, reason: str):
    from middleware.billing import refund_points

    return await refund_points(user_id, feature_code, reason)


def _ensure_agent_tool_feature_pricing() -> None:
    """H-P0-6 fix · seed only · 不再每次 tool call 都覆盖 cost_points=0.

    修前(2026-05-15 wire-up sprint 之前):DO UPDATE 强制重置 cost_points=0
    · 运营在 admin 改价 → 下次 tool call 自动覆盖 → 商业模式破坏。
    现在改 DO NOTHING:首次 seed 新 feature_code(默认 0)· 后续运营改价不被覆盖。
    """
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute, requires_paid_points)
            VALUES (%s, %s, 0, 0.0, FALSE)
            ON CONFLICT (feature_code) DO NOTHING
            """,
            (AGENT_TOOL_FEATURE_CODE, "社媒 Agent 工具调用"),
        )
