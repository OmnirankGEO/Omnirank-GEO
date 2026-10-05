"""Router for Social Studio agent-loop tools."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .billing.wrapper import AgentBillingContext, ToolCharge, charge_tool_call, refund_tool_call
from .risk_classifier import classify_tool_call
from .security.args_validator import validate_tool_args
from .security.owner_guard import verify_tool_access
from .security.result_sanitizer import sanitize_tool_result
from .tool_definitions import get_tool_schema

logger = logging.getLogger(__name__)


@dataclass
class AgentToolContext:
    user_id: int | None
    profile_id: str | None = None
    turn_id: str | None = None
    brand_id: int | None = None
    is_admin: bool = False
    grandfathered_billing: bool = False
    billing_enabled: bool = False
    billing_deduct_func: Callable[..., Awaitable[dict[str, Any]]] | None = None
    billing_refund_func: Callable[..., Awaitable[dict[str, Any]]] | None = None
    timeout_seconds: float = 30.0
    enforce_owner_guard: bool = True
    call_fingerprints: set[str] = field(default_factory=set)


async def execute_tool(name: str, args: dict[str, Any] | None, ctx: AgentToolContext) -> dict[str, Any]:
    args = args or {}
    started_at = time.perf_counter()
    cost_points = 0
    try:
        schema = get_tool_schema(name)
        cost_points = int(schema.get("cost_estimate_points") or 0)
    except KeyError:
        return _finish_tool_call(
            name,
            args,
            ctx,
            _error(name, "unknown_tool", "refuse_to_guess"),
            started_at,
            cost_points=cost_points,
            error="unknown_tool",
        )

    risk_level = classify_tool_call(schema["risk_level"], args)
    arg_error = validate_tool_args(schema, args)
    if arg_error:
        message = f"invalid_args:{arg_error}"
        return _finish_tool_call(
            name,
            args,
            ctx,
            _error(name, message, _fallback_policy(risk_level)),
            started_at,
            cost_points=cost_points,
            error=message,
        )
    access_error = verify_tool_access(name, args, ctx)
    if access_error:
        return _finish_tool_call(
            name,
            args,
            ctx,
            _error(name, access_error, "refuse_to_guess"),
            started_at,
            cost_points=cost_points,
            error=access_error,
        )

    fingerprint = _fingerprint(name, args)
    if fingerprint in ctx.call_fingerprints:
        message = "duplicate_tool_args_rejected"
        return _finish_tool_call(
            name,
            args,
            ctx,
            _error(name, message, _fallback_policy(risk_level)),
            started_at,
            cost_points=cost_points,
            error=message,
        )
    ctx.call_fingerprints.add(fingerprint)

    charge = await _charge_tool_call_safe(name, cost_points, ctx)
    if charge.billing_status == "billing_failed":
        message = f"billing_failed:{charge.reason}"
        return _finish_tool_call(
            name,
            args,
            ctx,
            _error(name, message, "refuse_to_guess"),
            started_at,
            cost_points=cost_points,
            error=message,
            charge=charge,
        )

    try:
        result = await asyncio.wait_for(_dispatch(name, args, ctx), timeout=ctx.timeout_seconds)
        result = sanitize_tool_result(name, result)
        if isinstance(result, dict) and result.get("error"):
            message = str(result["error"])
            charge = await _refund_tool_call_safe(charge, message, ctx)
            return _finish_tool_call(
                name,
                args,
                ctx,
                _error(name, message, _fallback_policy(risk_level), result=result),
                started_at,
                cost_points=cost_points,
                error=message,
                charge=charge,
            )
        return _finish_tool_call(
            name,
            args,
            ctx,
            {"ok": True, "tool": name, "result": result, "fallback_policy": None},
            started_at,
            cost_points=cost_points,
            charge=charge,
        )
    except asyncio.TimeoutError:
        message = "tool_timeout"
        charge = await _refund_tool_call_safe(charge, message, ctx)
        return _finish_tool_call(
            name,
            args,
            ctx,
            _error(name, message, _fallback_policy(risk_level)),
            started_at,
            cost_points=cost_points,
            error=message,
            charge=charge,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}:{str(exc)[:200]}"
        charge = await _refund_tool_call_safe(charge, message, ctx)
        return _finish_tool_call(
            name,
            args,
            ctx,
            _error(name, message, _fallback_policy(risk_level)),
            started_at,
            cost_points=cost_points,
            error=message,
            charge=charge,
        )


async def _dispatch(name: str, args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if not handler:
        raise KeyError(name)
    return await handler(args, ctx)


def _fingerprint(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"


def _fallback_policy(risk_level: str) -> str:
    if risk_level == "low":
        return "allow_local_guess"
    if risk_level == "medium":
        return "mark_uncertain"
    return "refuse_to_guess"


def _error(
    name: str,
    message: str,
    fallback_policy: str,
    *,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {"ok": False, "tool": name, "error": message, "fallback_policy": fallback_policy}
    if result is not None:
        payload["result"] = result
    return payload


def _finish_tool_call(
    name: str,
    args: dict[str, Any],
    ctx: AgentToolContext,
    payload: dict[str, Any],
    started_at: float,
    *,
    cost_points: int,
    error: str | None = None,
    charge: ToolCharge | None = None,
) -> dict[str, Any]:
    latency_ms = int(max(0.0, (time.perf_counter() - started_at) * 1000))
    payload.setdefault("cost_points", cost_points)
    if charge:
        payload.setdefault(
            "billing",
            {
                "status": charge.billing_status,
                "charged_points": charge.deducted_points,
                "refunded_points": charge.refunded_points,
            },
        )
    _record_tool_audit_safe(name, args, ctx, payload, latency_ms, cost_points=cost_points, error=error, charge=charge)
    return payload


def _record_tool_audit_safe(
    name: str,
    args: dict[str, Any],
    ctx: AgentToolContext,
    payload: dict[str, Any],
    latency_ms: int,
    *,
    cost_points: int,
    error: str | None,
    charge: ToolCharge | None,
) -> None:
    try:
        from db.social_agent_tool_audit import record_tool_audit

        result_chars = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        record_tool_audit(
            turn_id=ctx.turn_id,
            user_id=ctx.user_id,
            profile_id=str(args.get("profile_id") or ctx.profile_id or "") or None,
            tool_name=name,
            args=args,
            result_chars=result_chars,
            status="ok" if payload.get("ok") else "error",
            error=error,
            latency_ms=latency_ms,
            cost_points=cost_points,
            charged_points=charge.deducted_points if charge else 0,
            refunded_points=charge.refunded_points if charge else 0,
            billing_status=charge.billing_status if charge else None,
            billing_note=charge.reason if charge else None,
        )
    except Exception as exc:
        logger.warning("social agent tool audit failed for %s: %s", name, exc)


async def _charge_tool_call_safe(name: str, cost_points: int, ctx: AgentToolContext) -> ToolCharge | None:
    if not ctx.billing_enabled:
        return ToolCharge(
            tool_name=name,
            user_id=ctx.user_id,
            cost_points=max(0, int(cost_points or 0)),
            billing_status="billing_disabled",
        )
    return await charge_tool_call(
        name,
        cost_points,
        AgentBillingContext(
            user_id=ctx.user_id,
            brand_id=ctx.brand_id,
            is_admin=ctx.is_admin,
            grandfathered=ctx.grandfathered_billing,
        ),
        deduct_func=ctx.billing_deduct_func,
    )


async def _refund_tool_call_safe(
    charge: ToolCharge | None,
    reason: str,
    ctx: AgentToolContext,
) -> ToolCharge | None:
    if not charge:
        return None
    return await refund_tool_call(charge, reason, refund_func=ctx.billing_refund_func)


async def _tikhub_search_topics(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import tikhub

    return await tikhub.search_topics(**args)


async def _tikhub_get_account(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import tikhub

    return await tikhub.get_account(**args)


async def _tikhub_parse_video(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import video

    return await video.parse_video(**args)


async def _metaso_web_search(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import search

    return await search.web_search(**args)


async def _keyword_explore(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import keyword

    return await keyword.explore(**args)


async def _internal_memory_query(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import memory

    return await memory.query(**args)


async def _internal_profile_get(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import profile

    return await profile.get_selected(**args)


async def _internal_history_search(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import history

    return await history.search(**args)


async def _web_visit(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import web

    return await web.visit(**args)


async def _time_now(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import time

    return await time.now(**args)


async def _industry_knowledge_query(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .adapters import industry

    return await industry.query(**args)


async def _write_tool_not_implemented(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    return {"error": "write_tools_disabled_until_step_6"}


async def _update_profile_taboo(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .write_tools.update_taboo import update_profile_taboo

    return await update_profile_taboo(**args, ctx_user_id=ctx.user_id)


async def _update_profile_memory(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .write_tools.update_memory import update_profile_memory

    return await update_profile_memory(**args, ctx_user_id=ctx.user_id)


async def _confirm_memory_conflict(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .write_tools.confirm_conflict import confirm_memory_conflict

    return await confirm_memory_conflict(**args, ctx_user_id=ctx.user_id)


async def _archive_memory_event(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .write_tools.archive_event import archive_memory_event

    return await archive_memory_event(**args, ctx_user_id=ctx.user_id)


async def _update_profile_field(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .write_tools.update_field import update_profile_field

    return await update_profile_field(**args, ctx_user_id=ctx.user_id)


async def _cost_estimate_and_confirm(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    from .write_tools.cost_estimate import cost_estimate_and_confirm

    return await cost_estimate_and_confirm(**args)


async def _archive_plan(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    # P0-8 fix · SSOT §2.4b 明文要求的 archive_plan write tool
    from .write_tools.archive_plan import archive_plan

    return await archive_plan(**args, ctx_user_id=ctx.user_id)


async def _update_plan(args: dict[str, Any], ctx: AgentToolContext) -> dict[str, Any]:
    # P0-8 fix · SSOT §2.4b 明文要求的 update_plan write tool
    from .write_tools.update_plan import update_plan

    return await update_plan(**args, ctx_user_id=ctx.user_id)


_HANDLERS: dict[str, Callable[[dict[str, Any], AgentToolContext], Awaitable[dict[str, Any]]]] = {
    "tikhub_search_topics": _tikhub_search_topics,
    "tikhub_get_account": _tikhub_get_account,
    "tikhub_parse_video": _tikhub_parse_video,
    "metaso_web_search": _metaso_web_search,
    "keyword_explore": _keyword_explore,
    "internal_memory_query": _internal_memory_query,
    "internal_profile_get": _internal_profile_get,
    "internal_history_search": _internal_history_search,
    "web_visit": _web_visit,
    "time_now": _time_now,
    "industry_knowledge_query": _industry_knowledge_query,
    "update_profile_taboo": _update_profile_taboo,
    "update_profile_memory": _update_profile_memory,
    "confirm_memory_conflict": _confirm_memory_conflict,
    "archive_memory_event": _archive_memory_event,
    "update_profile_field": _update_profile_field,
    "cost_estimate_and_confirm": _cost_estimate_and_confirm,
    "archive_plan": _archive_plan,
    "update_plan": _update_plan,
}
