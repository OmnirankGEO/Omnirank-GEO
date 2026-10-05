"""Canonical SSE event builders for the Social Studio agent loop.

These helpers intentionally keep browser-facing payloads compact and safe:
tool arguments are summarized for display instead of streamed verbatim.
"""

from __future__ import annotations

from typing import Any

from tools.agent_loop.sse_alias import to_external_tool_name, to_external_tool_names


TOOL_LABELS: dict[str, str] = {
    "tikhub_search_topics": "搜索平台选题",
    "tikhub_get_account": "读取账号画像",
    "tikhub_parse_video": "解析视频素材",
    "metaso_web_search": "联网搜索资料",
    "keyword_explore": "拓展关键词",
    "internal_memory_query": "读取写稿资料库",
    "internal_profile_get": "读取客户资料",
    "internal_history_search": "搜索历史对话",
    "web_visit": "访问网页资料",
    "time_now": "读取当前时间",
    "industry_knowledge_query": "查询行业知识",
    "update_profile_taboo": "更新禁忌表达",
    "update_profile_memory": "沉淀用户资料",
    "confirm_memory_conflict": "确认资料冲突",
    "archive_memory_event": "归档资料记忆",
    "update_profile_field": "更新客户字段",
    "cost_estimate_and_confirm": "确认工具成本",
}


def build_thinking_event(text: str, **meta: Any) -> dict[str, Any]:
    return _compact({"type": "thinking", "text": str(text or "小榜正在思考..."), **meta})


def build_plan_state_event(plan_json: dict[str, Any] | None = None, **meta: Any) -> dict[str, Any]:
    payload = dict(plan_json or {})
    payload.update(meta)
    payload["type"] = "plan_state"
    return _compact(payload)


def build_tool_call_start_event(
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    call_id: str | None = None,
    round: int | None = None,
) -> dict[str, Any]:
    safe_args = args or {}
    # 2026-05-16 老板铁律 · `tool` 字段出 SSE 前 alias 化(不露 tikhub/metaso 等供应商名)
    # label / summary 仍按 internal 名 lookup(TOOL_LABELS 文案是中文中性词)
    return _compact(
        {
            "type": "tool_call_start",
            "tool": to_external_tool_name(tool),
            "call_id": call_id,
            "round": round,
            "display": {
                "tool_label": _tool_label(tool),
                "summary": _summarize_args(tool, safe_args),
                "args_keys": sorted(str(key) for key in safe_args.keys())[:12],
            },
        }
    )


def build_tool_call_result_event(
    tool: str,
    result: dict[str, Any] | None = None,
    *,
    call_id: str | None = None,
    round: int | None = None,
) -> dict[str, Any]:
    payload = result or {}
    ok = payload.get("ok") is True
    return _compact(
        {
            "type": "tool_call_result",
            # 2026-05-16 alias 化(同 start event)
            "tool": to_external_tool_name(tool),
            "call_id": call_id,
            "round": round,
            "status": "ok" if ok else "error",
            "ok": ok,
            "dry_run": payload.get("dry_run") is True,
            "fallback_policy": payload.get("fallback_policy"),
            "cost_points": int(payload.get("cost_points") or 0),
            "billing": payload.get("billing") if isinstance(payload.get("billing"), dict) else None,
            "display": {
                "tool_label": _tool_label(tool),
                "summary": _summarize_result(payload),
            },
        }
    )


def build_confirmation_request_event(
    request_type: str,
    options: list[dict[str, Any]] | None,
    prompt: str,
    **meta: Any,
) -> dict[str, Any]:
    return _compact(
        {
            "type": "confirmation_request",
            "request_type": request_type,
            "options": options or [],
            "prompt": str(prompt or "需要你确认一下。"),
            **meta,
        }
    )


def build_context_card_event(
    *,
    used_tools: list[str] | None = None,
    cost_points: int = 0,
    model: str | None = None,
    sources: list[str] | None = None,
    **meta: Any,
) -> dict[str, Any]:
    internal_tools = list(dict.fromkeys(used_tools or []))
    # 2026-05-16 老板铁律 · context_card.used_tools 数组 alias 化 · 不露供应商名
    # used_tool_labels 仍按 internal lookup TOOL_LABELS(已是中性中文)
    return _compact(
        {
            "type": "context_card",
            "used_tools": to_external_tool_names(internal_tools),
            "used_tool_labels": [_tool_label(tool) for tool in internal_tools],
            "cost_points": int(cost_points or 0),
            "model": model,
            "sources": (sources or [])[:8],
            **meta,
        }
    )


def build_done_event(reason: str, **meta: Any) -> dict[str, Any]:
    return _compact({"type": "done", "reason": str(reason or "stop"), **meta})


def to_sse_data(event: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe event payload ready for FastAPI SSE streaming."""
    return _compact(event)


def _tool_label(tool: str) -> str:
    return TOOL_LABELS.get(str(tool or ""), str(tool or "工具"))


def _summarize_args(tool: str, args: dict[str, Any]) -> str:
    if not args:
        return f"{_tool_label(tool)} · 无额外参数"
    if tool in {"internal_profile_get", "internal_memory_query", "internal_history_search"}:
        return f"{_tool_label(tool)} · 按当前客户资料检索"
    if tool in {"tikhub_search_topics", "keyword_explore", "industry_knowledge_query"}:
        return f"{_tool_label(tool)} · 按行业与平台检索"
    if tool in {"web_visit", "tikhub_parse_video"}:
        return f"{_tool_label(tool)} · 读取外部素材"
    if tool.startswith("update_") or tool in {"confirm_memory_conflict", "archive_memory_event"}:
        return f"{_tool_label(tool)} · 等待用户确认后写入"
    return f"{_tool_label(tool)} · {len(args)} 个参数"


def _summarize_result(result: dict[str, Any]) -> str:
    if result.get("dry_run") is True:
        return "已识别要用的工具，当前只预览不执行。"
    if result.get("ok") is True:
        cost = int(result.get("cost_points") or 0)
        suffix = f" · {cost} 积分" if cost else ""
        return f"工具执行完成{suffix}"
    error = str(result.get("error") or "工具失败")
    policy = str(result.get("fallback_policy") or "")
    policy_text = f" · {policy}" if policy else ""
    return f"{error[:80]}{policy_text}"


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
