import pytest


def _cleanup_metrics():
    from db.social_agent_metrics import ensure_tables, get_connection

    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_agent_metrics WHERE profile_id LIKE 'pytest_metrics_%'")
        conn.commit()
    finally:
        conn.close()


def test_agent_metrics_summary_computes_step12_six_indicators():
    from db.social_agent_metrics import get_agent_metrics_summary, record_agent_turn_metrics

    _cleanup_metrics()
    record_agent_turn_metrics(
        turn_id="pytest-metrics-1",
        user_id=501,
        profile_id="pytest_metrics_profile",
        model="fake",
        used_tools=["time_now", "internal_profile_get"],
        tool_call_count=2,
        tool_error_count=0,
        cost_points=10,
        latency_ms=100,
        plan_completed=True,
        confirmation_requested=True,
        confirmation_confirmed=True,
    )
    record_agent_turn_metrics(
        turn_id="pytest-metrics-2",
        user_id=501,
        profile_id="pytest_metrics_profile",
        model="fake",
        used_tools=["time_now"],
        tool_call_count=1,
        tool_error_count=1,
        cost_points=20,
        latency_ms=300,
        plan_completed=False,
        confirmation_requested=True,
        confirmation_confirmed=False,
    )

    summary = get_agent_metrics_summary(hours=24, profile_id="pytest_metrics_profile")

    assert summary["turn_count"] == 2
    assert summary["tool_call_freq"]["time_now"] == 2
    assert summary["tool_call_freq"]["internal_profile_get"] == 1
    assert summary["tool_failure_rate"] == pytest.approx(1 / 3)
    assert summary["avg_cost_points"] == pytest.approx(15.0)
    assert summary["latency_p50_ms"] == 100
    assert summary["latency_p95_ms"] == 300
    assert summary["plan_completion_rate"] == pytest.approx(0.5)
    assert summary["abcd_confirm_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_agent_loop_records_turn_metrics(monkeypatch):
    import tools.agent_loop.agent_loop as agent_loop_module
    from db.social_agent_metrics import get_agent_metrics_summary
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    _cleanup_metrics()

    async def fake_execute_tool(name, args, ctx):
        return {"ok": True, "tool": name, "result": {"now": "ok"}, "cost_points": 1, "fallback_policy": None}

    async def model_client(messages, tools, model):
        if not any(message.get("role") == "tool" for message in messages):
            return {
                "message": {
                    "tool_calls": [
                        {
                            "id": "metric-call",
                            "type": "function",
                            "function": {"name": "time_now", "arguments": '{"timezone":"UTC"}'},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        return {"message": {"content": "done"}, "finish_reason": "stop"}

    monkeypatch.setattr(agent_loop_module, "execute_tool", fake_execute_tool)
    result = await run_agent_loop(
        [{"role": "user", "content": "查时间"}],
        ctx=AgentToolContext(user_id=502, profile_id="pytest_metrics_loop", turn_id="pytest-metrics-loop"),
        model_client=model_client,
    )

    summary = get_agent_metrics_summary(hours=24, profile_id="pytest_metrics_loop")

    assert result.stopped_by == "stop"
    assert summary["turn_count"] == 1
    assert summary["tool_call_freq"]["time_now"] == 1
    assert summary["tool_call_count"] == 1
