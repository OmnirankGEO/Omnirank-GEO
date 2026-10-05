"""Core Social Studio agent tool loop.

This module is intentionally UI-agnostic. API/SSE integration happens in
later steps; Step 4 gives the product one tested loop primitive.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from .model_config import DEFAULT_MODEL, get_model_config, resolve_api_key
from .plan_state import build_plan_state_prompt, maybe_persist_plan_state
from .risk_classifier import classify_tool_call
from .sse_events import (
    build_confirmation_request_event,
    build_context_card_event,
    build_done_event,
    build_plan_state_event,
    build_thinking_event,
    build_tool_call_result_event,
    build_tool_call_start_event,
)
from .tool_definitions import openai_tools
from .tool_router import AgentToolContext, execute_tool


# 路由提示(老板 2026-05-17 拍"全面修改" · 修 12 场景压测 S1/S2/S10 路由跑偏)
# 凡是构建 system prompt 给本 agent loop 的调用方都 include 一下这块
ROUTING_HINTS = (
    "## 路由判断必读\n"
    "1. 用户问 \"什么火 / 最近热门 / 爆款 / 有啥新的 / 大家在聊啥\" 这类**模糊行业不明的趋势探索** → "
    "首选 tikhub_search_topics(platform=\"douyin_hot\") 拿抖音热搜榜;"
    "不要调 time_now / web_visit / metaso_web_search 等无关工具。\n"
    "2. 用户主诉是 **\"找视频 / 搜内容 / 找对标 / 抓爆款 / 看老师们在讲什么\"** → "
    "第一步直接调 tikhub_search_topics 或 tikhub_get_account 拿数据;"
    "不要先调 internal_history_search 绕远。"
    "只有用户明确说 \"我之前问过 X 吗 / 上次那个 / 历史对话里 / 之前我们聊过\" 这类承接才走 internal_history_search。\n"
    "3. 与短视频 / 选题 / 写稿 / 对标 / 品牌资料 / 平台数据 **完全无关的事**"
    "(天气 / 闲聊 / 数学题 / 笑话 / 时事新闻 / 哲学问题)→ "
    "礼貌说明 \"我是 OmniRank 社媒写稿助手 · 服务范围是选题/对标/写稿/品牌资料,这个问题帮不上,"
    "不过你可以让我聊聊你的内容方向\";一个工具都别调。\n"
    "4. 用户给了视频链接(http/https + douyin/xhs/bilibili/wechat 域名)说 \"帮我拆 / 仿写\" → "
    "tikhub_parse_video(url=..., need_asr=true)。\n"
    "5. 用户只说 \"我看了一个爆款\" 没给链接 → 先问对方要链接,不要凭空猜。"
)


ModelClient = Callable[[list[dict[str, Any]], list[dict[str, Any]], str], Awaitable[dict[str, Any]]]
EventSink = Callable[[dict[str, Any]], Any]


@dataclass
class AgentLoopResult:
    final_response: str
    messages: list[dict[str, Any]]
    rounds: int
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    stopped_by: str = "stop"
    model: str = DEFAULT_MODEL


async def run_agent_loop(
    messages: list[dict[str, Any]],
    *,
    tools: list[str] | None = None,
    model: str | None = None,
    ctx: AgentToolContext | None = None,
    model_client: ModelClient | None = None,
    max_rounds: int = 5,
    event_sink: EventSink | None = None,
    execute_tools: bool = True,
) -> AgentLoopResult:
    selected_model = model or DEFAULT_MODEL
    working_messages = [dict(message) for message in messages]
    tool_payload = openai_tools(tools)
    tool_results: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    context = ctx or AgentToolContext(user_id=None)
    client = model_client or _call_openai_compatible_model
    last_content = ""
    started_at = time.perf_counter()

    # P0-1 fix · Plan State Machine 主路径接通:LLM round 1 之前先看到 active plan
    # 防 LLM 不知道有未完成多步 plan · 防"跳步"重大 bug(Audit G A1-A8 6 场景全跑通的关键)
    if context.user_id is not None and context.profile_id:
        try:
            from db.social_agent_plans import get_active_plan
            active_plan = await asyncio.to_thread(
                get_active_plan,
                user_id=int(context.user_id),
                profile_id=str(context.profile_id),
            )
        except Exception as exc:
            active_plan = None
            import logging
            logging.getLogger("GEO-AgentLoop").warning(f"get_active_plan failed: {exc}")
        plan_prompt = build_plan_state_prompt(active_plan)
        if plan_prompt:
            working_messages.insert(0, {"role": "system", "content": plan_prompt})

    # P0-11 fix · 高风险行业禁猜元指令注入(医美/教育/金融/法律/医疗/房产投资/保险)
    # 防 risk_classifier 只在 tool 失败时生效的死角 · 改成正常路径 LLM 也看到 risk warning
    if context.profile_id:
        try:
            from tools.agent_loop.adapters.profile import get_selected
            brand_profile = await get_selected(str(context.profile_id), ["industry"])
            industry = str((brand_profile.get("profile") or {}).get("industry") or "").strip()
            if industry:
                risk_level = classify_tool_call("low", {"industry": industry})
                if risk_level == "high":
                    working_messages.insert(0, {
                        "role": "system",
                        "content": (
                            f"⚠️ 高风险行业:{industry}。"
                            "禁止凭训练知识编造数据 / 案例 / 价格 / 疗效 / 资质。"
                            "tool 失败或资料不足时必须明示「我没拿到数据 · 此类内容必须基于真实资料 · 请你先补充或上传」· 不允许猜。"
                        ),
                    })
        except Exception as exc:
            import logging
            logging.getLogger("GEO-AgentLoop").warning(f"risk classify failed: {exc}")

    for round_index in range(1, max_rounds + 1):
        await _emit(
            events,
            event_sink,
            build_thinking_event("小榜正在理解你的任务，并判断是否需要查资料。", round=round_index, model=selected_model),
        )
        response = await client(working_messages, tool_payload, selected_model)
        message, finish_reason = _normalize_response(response)
        content = _message_content(message)
        if content:
            last_content = content

        plan_event = await maybe_persist_plan_state(message, context)
        if plan_event:
            await _emit(events, event_sink, build_plan_state_event(plan_event))

        calls = _tool_calls(message)
        if not calls or finish_reason == "stop":
            await _emit(events, event_sink, _context_card(tool_results, selected_model))
            await _emit(events, event_sink, build_done_event("stop", round=round_index))
            await _record_agent_session_turns_safe(
                context,
                messages,
                last_content,
                tool_results,
                stopped_by="stop",
            )
            await _record_turn_metrics_safe(context, selected_model, tool_results, events, started_at, stopped_by="stop")
            return AgentLoopResult(
                final_response=last_content,
                messages=working_messages + [_assistant_message(message, content, calls)],
                rounds=round_index,
                tool_results=tool_results,
                events=events,
                stopped_by="stop",
                model=selected_model,
            )

        assistant_message = _assistant_message(message, content, calls)
        working_messages.append(assistant_message)

        # P0-4 fix · cost estimator 主动调(原 estimator 0 处生产代码调用 · LLM 忘了调 W7 = 用户 surprise 扣费)
        # 软线 50:加 system warning 让下轮 LLM 精简 · 硬线 100:强 break + 弹 confirmation_request
        from .billing.cost_map import get_tool_cost
        planned_tool_names_for_cost = [_tool_name(c) for c in calls if _tool_name(c)]
        prior_tool_cost = sum(
            get_tool_cost(str(r.get("tool") or ""))
            for r in tool_results
            if isinstance(r, dict)
        )
        this_round_estimate = sum(get_tool_cost(n) for n in planned_tool_names_for_cost)
        total_projected_points = prior_tool_cost + this_round_estimate
        is_admin_user = bool(getattr(context, "is_admin", False))
        if total_projected_points > 100 and not is_admin_user:
            await _emit(events, event_sink, build_confirmation_request_event(
                request_type="cost_hard_break",
                options=[
                    {"label": "继续 · 我接受", "value": "continue"},
                    {"label": "取消 · 改方案", "value": "cancel"},
                ],
                prompt=f"本轮预估累计消耗 {total_projected_points} 积分(已 {prior_tool_cost} + 本轮预估 {this_round_estimate})· 超 100 硬线 · 是否继续?",
                round=round_index,
                soft_line=50,
                hard_line=100,
                total_projected_points=total_projected_points,
            ))
            await _emit(events, event_sink, _context_card(tool_results, selected_model))
            await _emit(events, event_sink, build_done_event("hard_cost_break", round=round_index))
            await _record_agent_session_turns_safe(context, messages, last_content, tool_results, stopped_by="hard_cost_break")
            await _record_turn_metrics_safe(context, selected_model, tool_results, events, started_at, stopped_by="hard_cost_break")
            return AgentLoopResult(
                final_response=last_content or f"本轮预估 {total_projected_points} 积分超 100 硬线 · 等用户确认",
                messages=working_messages,
                rounds=round_index,
                tool_results=tool_results,
                events=events,
                stopped_by="hard_cost_break",
                model=selected_model,
            )
        elif total_projected_points > 50 and not is_admin_user:
            working_messages.append({
                "role": "system",
                "content": f"⚠️ 本 turn 累计预估 {total_projected_points} 积分(超软线 50)· 后续 tool call 请精简 · 别堆调用。",
            })

        for call_index, call in enumerate(calls):
            name = _tool_name(call)
            args = _parse_tool_args(call)
            call_id = str(call.get("id") or f"call_{round_index}_{call_index}")
            await _emit(
                events,
                event_sink,
                build_tool_call_start_event(name, args, call_id=call_id, round=round_index),
            )
            if not execute_tools:
                result = {"ok": True, "tool": name, "dry_run": True, "args": args, "fallback_policy": None}
                tool_results.append(result)
                await _emit(
                    events,
                    event_sink,
                    build_tool_call_result_event(name, result, call_id=call_id, round=round_index),
                )
                await _emit(events, event_sink, _context_card(tool_results, selected_model))
                await _emit(events, event_sink, build_done_event("dry_run", round=round_index))
                await _record_agent_session_turns_safe(
                    context,
                    messages,
                    last_content,
                    tool_results,
                    stopped_by="dry_run",
                )
                await _record_turn_metrics_safe(context, selected_model, tool_results, events, started_at, stopped_by="dry_run")
                return AgentLoopResult(
                    final_response=last_content,
                    messages=working_messages + [assistant_message],
                    rounds=round_index,
                    tool_results=tool_results,
                    events=events,
                    stopped_by="dry_run",
                    model=selected_model,
                )
            result = await execute_tool(name, args, context)
            tool_results.append(result)
            await _emit(
                events,
                event_sink,
                build_tool_call_result_event(name, result, call_id=call_id, round=round_index),
            )
            confirmation_event = _confirmation_request_from_tool_result(name, result)
            if confirmation_event:
                await _emit(events, event_sink, confirmation_event)
                await _emit(events, event_sink, _context_card(tool_results, selected_model))
                await _emit(events, event_sink, build_done_event("confirmation_required", round=round_index))
                confirmation_prompt = str(confirmation_event.get("prompt") or "需要你确认一下。")
                await _record_agent_session_turns_safe(
                    context,
                    messages,
                    confirmation_prompt,
                    tool_results,
                    stopped_by="confirmation_required",
                )
                await _record_turn_metrics_safe(context, selected_model, tool_results, events, started_at, stopped_by="confirmation_required")
                return AgentLoopResult(
                    final_response=confirmation_prompt,
                    messages=working_messages + [assistant_message],
                    rounds=round_index,
                    tool_results=tool_results,
                    events=events,
                    stopped_by="confirmation_required",
                    model=selected_model,
                )
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    await _emit(events, event_sink, _context_card(tool_results, selected_model))
    await _emit(events, event_sink, build_done_event("max_rounds", round=max_rounds))
    await _record_agent_session_turns_safe(
        context,
        messages,
        last_content,
        tool_results,
        stopped_by="max_rounds",
    )
    await _record_turn_metrics_safe(context, selected_model, tool_results, events, started_at, stopped_by="max_rounds")
    return AgentLoopResult(
        final_response=last_content,
        messages=working_messages,
        rounds=max_rounds,
        tool_results=tool_results,
        events=events,
        stopped_by="max_rounds",
        model=selected_model,
    )


async def _call_openai_compatible_model(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
) -> dict[str, Any]:
    config = get_model_config(model)
    api_key = resolve_api_key(config)
    if not api_key:
        raise RuntimeError(f"missing API key for {config.name}")
    payload: dict[str, Any] = {
        "model": config.name,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if config.thinking_disabled:
        payload["thinking"] = {"type": "disabled"}
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            f"{config.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"model HTTP {resp.status_code}:{resp.text[:300]}")
        return resp.json()


def _normalize_response(response: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if response.get("choices"):
        choice = (response.get("choices") or [{}])[0]
        return choice.get("message") or {}, str(choice.get("finish_reason") or "")
    if response.get("message"):
        return response.get("message") or {}, str(response.get("finish_reason") or "")
    return {
        "role": "assistant",
        "content": response.get("content") or "",
        "tool_calls": response.get("tool_calls") or [],
    }, str(response.get("finish_reason") or "")


def _assistant_message(message: dict[str, Any], content: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": "assistant", "content": content or None}
    if calls:
        payload["tool_calls"] = calls
    if message.get("reasoning_content"):
        payload["reasoning_content"] = message["reasoning_content"]
    return payload


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls") or []
    return calls if isinstance(calls, list) else []


def _tool_name(call: dict[str, Any]) -> str:
    return str((call.get("function") or {}).get("name") or "")


def _parse_tool_args(call: dict[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _emit(events: list[dict[str, Any]], sink: EventSink | None, event: dict[str, Any]) -> None:
    events.append(event)
    if sink is None:
        return
    result = sink(event)
    if inspect.isawaitable(result):
        await result


def _context_card(tool_results: list[dict[str, Any]], model: str) -> dict[str, Any]:
    used_tools = [str(result.get("tool") or "") for result in tool_results if result.get("tool")]
    cost_points = sum(int(result.get("cost_points") or 0) for result in tool_results)
    sources = [str(result.get("tool") or "") for result in tool_results if result.get("ok") is True and result.get("tool")]
    return build_context_card_event(
        used_tools=used_tools,
        cost_points=cost_points,
        model=model,
        sources=sources,
    )


def _confirmation_request_from_tool_result(tool: str, result: dict[str, Any]) -> dict[str, Any] | None:
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    status = str(payload.get("status") or result.get("status") or "")
    if tool == "cost_estimate_and_confirm" and status in {"requires_confirmation", "soft_confirm"}:
        points = int(payload.get("estimated_points") or payload.get("cost_points") or result.get("cost_points") or 0)
        prompt = f"这次预计消耗 {points} 积分。要继续吗？"
        return build_confirmation_request_event(
            "billing",
            [
                {"label": "继续执行", "value": "continue"},
                {"label": "先省积分", "value": "reduce_scope"},
                {"label": "取消", "value": "cancel"},
            ],
            prompt,
            tool=tool,
            estimated_points=points,
            limit=payload.get("limit") or result.get("limit"),
        )
    if tool == "confirm_memory_conflict" and status == "confirmation_required":
        options = payload.get("options") if isinstance(payload.get("options"), list) else []
        normalized_options = [
            {"label": str(option), "value": str(option)}
            for option in options[:4]
            if str(option).strip()
        ] or [
            {"label": "用最新说法", "value": "use_latest"},
            {"label": "保留原资料", "value": "keep_existing"},
            {"label": "两个都保留", "value": "keep_both"},
        ]
        return build_confirmation_request_event(
            "memory_conflict",
            normalized_options,
            "这里和之前记下的资料有冲突，你想以哪个为准？",
            tool=tool,
            concept=payload.get("concept"),
            old_event_id=payload.get("old_event_id"),
        )
    if status == "pending_confirm":
        return build_confirmation_request_event(
            "profile_update",
            [
                {"label": "确认写入", "value": "confirm"},
                {"label": "先不写入", "value": "skip"},
                {"label": "我来改一句", "value": "edit"},
            ],
            "这条资料还不够确定，要写进资料库吗？",
            tool=tool,
        )
    return None


async def _record_agent_session_turns_safe(
    ctx: AgentToolContext,
    messages: list[dict[str, Any]],
    assistant_content: str,
    tool_results: list[dict[str, Any]],
    *,
    stopped_by: str,
) -> None:
    if not ctx.turn_id or not ctx.profile_id:
        return
    user_content = _last_user_content(messages)
    if not user_content and not assistant_content:
        return
    try:
        session_id = str(ctx.turn_id)
        profile_id = str(ctx.profile_id)
        cost_points = sum(int(result.get("cost_points") or 0) for result in tool_results)
        summary = _session_summary(user_content, assistant_content, stopped_by=stopped_by)
        topic = user_content[:120]

        def _record() -> None:
            from db.social_agent_sessions import upsert_agent_session
            from db.social_agent_turns import record_agent_turn_pair

            upsert_agent_session(
                session_id=session_id,
                user_id=ctx.user_id,
                profile_id=profile_id,
                summary=summary,
                topic=topic,
            )
            record_agent_turn_pair(
                session_id=session_id,
                turn_id=ctx.turn_id,
                user_id=ctx.user_id,
                profile_id=profile_id,
                user_content=user_content,
                assistant_content=assistant_content,
                tool_results=tool_results,
                cost_points=cost_points,
            )

        await asyncio.to_thread(_record)
    except Exception:
        return


def _last_user_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(part.get("text") or "").strip()
                for part in content
                if isinstance(part, dict) and part.get("text")
            ]
            return " ".join(part for part in parts if part).strip()
    return ""


def _session_summary(user_content: str, assistant_content: str, *, stopped_by: str) -> str:
    user_part = str(user_content or "").replace("\n", " ").strip()[:220]
    assistant_part = str(assistant_content or "").replace("\n", " ").strip()[:220]
    return f"用户:{user_part} / 小榜:{assistant_part} / 状态:{stopped_by}"[:700]


async def _record_turn_metrics_safe(
    ctx: AgentToolContext,
    model: str,
    tool_results: list[dict[str, Any]],
    events: list[dict[str, Any]],
    started_at: float,
    *,
    stopped_by: str,
) -> None:
    try:
        used_tools = [str(result.get("tool") or "") for result in tool_results if result.get("tool")]
        tool_error_count = sum(1 for result in tool_results if result.get("ok") is not True)
        cost_points = sum(int(result.get("cost_points") or 0) for result in tool_results)
        latency_ms = int(max(0.0, (time.perf_counter() - started_at) * 1000))
        plan_completed = stopped_by == "stop" and any(event.get("type") == "plan_state" for event in events)
        confirmation_requested = any(event.get("type") == "confirmation_request" for event in events)
        confirmation_confirmed = any(
            event.get("type") == "tool_call_result"
            and event.get("tool") in {"confirm_memory_conflict", "cost_estimate_and_confirm"}
            and event.get("ok") is True
            for event in events
        )

        def _record() -> None:
            from db.social_agent_metrics import record_agent_turn_metrics

            record_agent_turn_metrics(
                turn_id=ctx.turn_id,
                user_id=ctx.user_id,
                profile_id=ctx.profile_id,
                model=model,
                used_tools=used_tools,
                tool_call_count=len(tool_results),
                tool_error_count=tool_error_count,
                cost_points=cost_points,
                latency_ms=latency_ms,
                plan_completed=plan_completed,
                confirmation_requested=confirmation_requested,
                confirmation_confirmed=confirmation_confirmed,
            )

        await asyncio.to_thread(_record)
    except Exception:
        return
