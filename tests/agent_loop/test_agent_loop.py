import json
from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.asyncio


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, messages, tools, model):
        self.calls.append({"messages": list(messages), "tools": tools, "model": model})
        if not self.responses:
            return {"message": {"content": "done"}, "finish_reason": "stop"}
        return self.responses.pop(0)


def tool_response(name, args, *, call_id="call_1"):
    return {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            ],
        },
        "finish_reason": "tool_calls",
    }


async def test_agent_loop_exits_on_stop_without_tools():
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    model = ScriptedModel([{"message": {"content": "先聊清楚再写。"}, "finish_reason": "stop"}])
    result = await run_agent_loop(
        [{"role": "user", "content": "先别写，问我几个问题"}],
        ctx=AgentToolContext(user_id=1, profile_id="p1"),
        model_client=model,
    )

    assert result.final_response == "先聊清楚再写。"
    assert result.stopped_by == "stop"
    assert result.rounds == 1
    assert result.tool_results == []


async def test_agent_loop_executes_tool_then_feeds_result_back():
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    model = ScriptedModel(
        [
            tool_response("time_now", {"timezone": "Asia/Shanghai"}),
            {"message": {"content": "现在是北京时间，继续写。"}, "finish_reason": "stop"},
        ]
    )
    result = await run_agent_loop(
        [{"role": "user", "content": "现在几点？"}],
        ctx=AgentToolContext(user_id=1, profile_id="p1"),
        model_client=model,
    )

    assert result.stopped_by == "stop"
    assert result.rounds == 2
    assert result.tool_results[0]["tool"] == "time_now"
    assert result.tool_results[0]["ok"] is True
    assert any(message["role"] == "tool" for message in model.calls[1]["messages"])


async def test_agent_loop_caps_infinite_tool_loops():
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    model = ScriptedModel(
        [
            tool_response("internal_history_search", {"profile_id": "p1", "query": f"round-{idx}"}, call_id=f"c{idx}")
            for idx in range(5)
        ]
    )
    result = await run_agent_loop(
        [{"role": "user", "content": "一直查"}],
        ctx=AgentToolContext(user_id=1, profile_id="p1"),
        model_client=model,
        max_rounds=3,
    )

    assert result.stopped_by == "max_rounds"
    assert result.rounds == 3
    assert len(result.tool_results) == 3


async def test_agent_loop_rejects_duplicate_tool_args():
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    duplicate = tool_response("internal_history_search", {"profile_id": "p1", "query": "same"})
    model = ScriptedModel(
        [
            duplicate,
            duplicate,
            {"message": {"content": "已避免重复查同一件事。"}, "finish_reason": "stop"},
        ]
    )
    result = await run_agent_loop(
        [{"role": "user", "content": "查一下上次聊什么"}],
        ctx=AgentToolContext(user_id=1, profile_id="p1"),
        model_client=model,
    )

    assert result.stopped_by == "stop"
    assert len(result.tool_results) == 2
    assert result.tool_results[1]["ok"] is False
    assert "duplicate" in result.tool_results[1]["error"]


async def test_agent_loop_emits_step11_canonical_sse_events():
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    seen = []

    model = ScriptedModel(
        [
            tool_response("time_now", {"timezone": "UTC"}),
            {"message": {"content": "查完了。"}, "finish_reason": "stop"},
        ]
    )
    result = await run_agent_loop(
        [{"role": "user", "content": "查时间"}],
        ctx=AgentToolContext(user_id=1, profile_id="p1"),
        model_client=model,
        event_sink=seen.append,
    )

    event_types = [event["type"] for event in seen]
    assert result.events == seen
    assert "model_round" not in event_types
    assert "thinking" in event_types
    assert "tool_call_start" in event_types
    assert "tool_call_result" in event_types
    assert "context_card" in event_types
    assert event_types[-1] == "done"
    context_event = [event for event in seen if event["type"] == "context_card"][-1]
    assert context_event["used_tools"] == ["time_now"]
    assert context_event["cost_points"] >= 0


async def test_agent_loop_dry_run_lists_tool_call_without_executing(monkeypatch):
    import tools.agent_loop.agent_loop as agent_loop_module
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    async def forbidden_execute_tool(name, args, ctx):
        raise AssertionError("dry_run must not execute tools")

    monkeypatch.setattr(agent_loop_module, "execute_tool", forbidden_execute_tool)
    model = ScriptedModel([tool_response("time_now", {"timezone": "UTC"})])
    result = await run_agent_loop(
        [{"role": "user", "content": "查时间"}],
        ctx=AgentToolContext(user_id=1, profile_id="p1"),
        model_client=model,
        execute_tools=False,
    )

    assert result.stopped_by == "dry_run"
    assert result.tool_results == [{"ok": True, "tool": "time_now", "dry_run": True, "args": {"timezone": "UTC"}, "fallback_policy": None}]
    assert result.events[-1]["reason"] == "dry_run"


async def test_agent_loop_accepts_step1_preflight_8_case_shapes(monkeypatch):
    import tools.agent_loop.agent_loop as agent_loop_module
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    async def fake_execute_tool(name, args, ctx):
        return {"ok": True, "tool": name, "result": {"args": args}, "fallback_policy": None}

    monkeypatch.setattr(agent_loop_module, "execute_tool", fake_execute_tool)

    spec_path = Path(__file__).resolve().parents[2] / "scripts" / "eval" / "preflight_8_cases.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    assert len(spec["cases"]) == 8

    for case in spec["cases"]:
        expect = case.get("expect") or {}
        required_names = list(expect.get("required_tool_names") or expect.get("required_tool_names_any_round") or [])
        responses = []
        if required_names:
            calls = [
                {
                    "id": f"{case['id']}_{idx}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(_default_args(name), ensure_ascii=False),
                    },
                }
                for idx, name in enumerate(required_names)
            ]
            responses.append({"message": {"role": "assistant", "tool_calls": calls}, "finish_reason": "tool_calls"})
        responses.append({"message": {"content": f"{case['id']} loop ok"}, "finish_reason": "stop"})

        result = await run_agent_loop(
            case["messages"],
            ctx=AgentToolContext(user_id=1, profile_id="profile_62"),
            model_client=ScriptedModel(responses),
            max_rounds=5,
        )
        assert result.stopped_by == "stop"
        assert result.final_response.endswith("loop ok")
        assert len(result.tool_results) >= len(required_names)


async def test_agent_loop_pauses_and_emits_confirmation_request_for_high_cost_tool():
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    result = await run_agent_loop(
        [{"role": "user", "content": "先调研再写，必要时多查资料"}],
        ctx=AgentToolContext(user_id=1, profile_id="profile_62"),
        model_client=ScriptedModel(
            [
                tool_response(
                    "cost_estimate_and_confirm",
                    {
                        "planned_tools": ["tikhub_search_topics", "metaso_web_search"],
                        "estimated_points": 120,
                        "output_mode": "full",
                        "reason": "deep research",
                    },
                    call_id="call_cost",
                )
            ]
        ),
    )

    confirmation = next(event for event in result.events if event["type"] == "confirmation_request")
    assert result.stopped_by == "confirmation_required"
    assert confirmation["request_type"] == "billing"
    assert confirmation["options"][0]["value"] == "continue"
    assert "120" in confirmation["prompt"]


async def test_agent_loop_pauses_for_memory_conflict_and_pending_profile_confirmation(monkeypatch):
    import tools.agent_loop.agent_loop as agent_loop_module
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    async def fake_execute_tool(name, args, ctx):
        if name == "confirm_memory_conflict":
            return {
                "ok": True,
                "tool": name,
                "result": {
                    "status": "confirmation_required",
                    "concept": "target_customer",
                    "old_event_id": "42",
                    "options": ["用最新说法", "保留原资料", "两个都保留"],
                },
                "fallback_policy": None,
                "cost_points": 0,
            }
        return {
            "ok": True,
            "tool": name,
            "result": {"status": "pending_confirm", "field": "target_users", "event_id": 88},
            "fallback_policy": None,
            "cost_points": 0,
        }

    monkeypatch.setattr(agent_loop_module, "execute_tool", fake_execute_tool)

    cases = [
        (
            "confirm_memory_conflict",
            {
                "profile_id": "profile_62",
                "concept": "target_customer",
                "old_event_id": "42",
                "new_text": "现在服务 B 端采购经理",
                "options": ["用最新说法", "保留原资料"],
            },
            "memory_conflict",
        ),
        (
            "update_profile_field",
            {
                "profile_id": "profile_62",
                "field": "target_users",
                "value": "海外 B 端采购经理",
                "source": "ai_inferred",
            },
            "profile_update",
        ),
    ]

    for tool_name, args, request_type in cases:
        result = await run_agent_loop(
            [{"role": "user", "content": "这句资料可能要确认一下"}],
            ctx=AgentToolContext(user_id=1, profile_id="profile_62"),
            model_client=ScriptedModel([tool_response(tool_name, args, call_id=f"call_{tool_name}")]),
        )

        confirmation = next(event for event in result.events if event["type"] == "confirmation_request")
        assert result.stopped_by == "confirmation_required"
        assert confirmation["request_type"] == request_type
        assert len(confirmation["options"]) >= 2


def _default_args(name):
    if name == "internal_profile_get":
        return {"profile_id": "profile_62", "fields": ["industry", "brand_name"]}
    if name == "internal_memory_query":
        return {"profile_id": "profile_62", "concept": "target_customer", "query": "目标客户"}
    if name == "tikhub_search_topics":
        return {"industry": "跨境洗护", "platform": "tiktok"}
    if name == "industry_knowledge_query":
        return {"industry": "教育咨询", "question": "痛点"}
    if name == "time_now":
        return {"timezone": "Asia/Shanghai"}
    if name == "cost_estimate_and_confirm":
        return {"estimated_points": 80, "reason": "tool budget"}
    if name == "update_profile_memory":
        return {"profile_id": "profile_62", "concept": "target_customer", "value": "B端采购经理", "source": "ai_inferred"}
    if name == "web_visit":
        return {"url": "https://example.com", "purpose": "research"}
    return {}
