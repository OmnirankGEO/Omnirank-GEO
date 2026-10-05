def test_sse_event_builders_emit_step11_canonical_shapes():
    from tools.agent_loop.sse_events import (
        build_context_card_event,
        build_done_event,
        build_plan_state_event,
        build_thinking_event,
        build_tool_call_result_event,
        build_tool_call_start_event,
        build_confirmation_request_event,
    )

    events = [
        build_thinking_event("正在理解你的需求", round=1, model="fake-model"),
        build_plan_state_event({"plan_id": "plan-1", "current_step": 1}),
        build_tool_call_start_event("time_now", {"timezone": "Asia/Shanghai"}, call_id="c1", round=1),
        build_tool_call_result_event("time_now", {"ok": True, "result": {"now": "2026-05-15"}}, call_id="c1", round=1),
        build_confirmation_request_event("billing", [{"label": "继续", "value": "continue"}], "这次会用 80 积分，继续吗？"),
        build_context_card_event(used_tools=["time_now"], cost_points=1, model="fake-model", sources=["系统时间"]),
        build_done_event("stop", round=1),
    ]

    assert [event["type"] for event in events] == [
        "thinking",
        "plan_state",
        "tool_call_start",
        "tool_call_result",
        "confirmation_request",
        "context_card",
        "done",
    ]
    assert events[0]["text"] == "正在理解你的需求"
    assert events[2]["display"]["tool_label"] == "读取当前时间"
    assert "timezone" not in events[2]["display"]["summary"]
    assert events[3]["status"] == "ok"
    assert events[5]["used_tools"] == ["time_now"]
    assert events[6]["reason"] == "stop"
