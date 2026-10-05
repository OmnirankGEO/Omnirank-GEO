import pytest


def _cleanup_audit():
    from db.social_agent_tool_audit import ensure_tables, get_connection

    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_agent_tool_audit WHERE turn_id LIKE 'pytest_audit_%'")
        conn.commit()
    finally:
        conn.close()


def test_audit_db_stores_args_hash_without_raw_pii():
    from db.social_agent_tool_audit import get_audit_by_turn_id, record_tool_audit

    _cleanup_audit()
    row = record_tool_audit(
        turn_id="pytest_audit_direct",
        user_id=101,
        profile_id="pytest_profile_a",
        tool_name="internal_memory_query",
        args={"profile_id": "pytest_profile_a", "phone": "13800000000", "query": "目标客户"},
        result_chars=42,
        status="ok",
        latency_ms=7,
        cost_points=1,
    )
    rows = get_audit_by_turn_id("pytest_audit_direct")

    assert row["args_hash"]
    assert "args" not in row
    assert "13800000000" not in str(row)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "internal_memory_query"
    assert rows[0]["result_chars"] == 42
    assert rows[0]["status"] == "ok"
    assert "13800000000" not in str(rows[0])


@pytest.mark.asyncio
async def test_router_records_successful_tool_call_audit():
    from db.social_agent_tool_audit import get_audit_by_turn_id
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    _cleanup_audit()
    ctx = AgentToolContext(user_id=102, profile_id="pytest_profile_b", turn_id="pytest_audit_router_ok")
    result = await execute_tool("time_now", {"timezone": "Asia/Shanghai"}, ctx)
    rows = get_audit_by_turn_id("pytest_audit_router_ok")

    assert result["ok"] is True
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "time_now"
    assert rows[0]["status"] == "ok"
    assert rows[0]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_router_records_rejected_tool_call_audit():
    from db.social_agent_tool_audit import get_audit_by_turn_id
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    _cleanup_audit()
    ctx = AgentToolContext(user_id=103, profile_id="pytest_profile_owner", turn_id="pytest_audit_router_denied")
    result = await execute_tool(
        "internal_profile_get",
        {"profile_id": "pytest_profile_other", "fields": ["industry"]},
        ctx,
    )
    rows = get_audit_by_turn_id("pytest_audit_router_denied")

    assert result["ok"] is False
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "internal_profile_get"
    assert rows[0]["status"] == "error"
    assert rows[0]["error"]
