import uuid

import pytest


pytestmark = pytest.mark.asyncio


def _ids():
    suffix = uuid.uuid4().hex[:10]
    return f"session-{suffix}", f"turn-{suffix}", f"profile-{suffix}"


async def test_step21_session_turns_roundtrip_and_history_search():
    from db.social_agent_sessions import search_agent_sessions, upsert_agent_session
    from db.social_agent_turns import record_agent_turn, list_turns_by_session

    session_id, turn_id, profile_id = _ids()
    upsert_agent_session(
        session_id=session_id,
        user_id=7,
        profile_id=profile_id,
        summary="上次聊过德国 TikTok 洗衣凝珠 B 端获客",
        topic="海外社媒获客",
    )
    record_agent_turn(
        session_id=session_id,
        turn_id=turn_id,
        user_id=7,
        profile_id=profile_id,
        role="user",
        content="德国超市采购最关心环保认证和到岸成本。",
    )
    record_agent_turn(
        session_id=session_id,
        turn_id=turn_id,
        user_id=7,
        profile_id=profile_id,
        role="assistant",
        content="下次可以顺着德国市场的认证和成本继续写。",
        tool_calls_json=[{"tool": "industry_knowledge_query", "ok": True}],
        cost_points=3,
    )

    turns = list_turns_by_session(session_id)
    assert [row["role"] for row in turns] == ["user", "assistant"]

    results = search_agent_sessions(profile_id=profile_id, query="德国 环保认证", limit=5)
    assert results[0]["session_id"] == session_id
    assert "德国" in results[0]["summary"]
    assert results[0]["matched_turns"]


async def test_step21_agent_loop_records_user_and_assistant_turns():
    from db.social_agent_turns import list_turns_by_session
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    session_id, _, profile_id = _ids()

    async def model(messages, tools, model_name):
        return {"message": {"content": "先按上次的德国市场线索继续。"}, "finish_reason": "stop"}

    result = await run_agent_loop(
        [{"role": "user", "content": "按上次那个德国市场方向继续"}],
        ctx=AgentToolContext(user_id=7, profile_id=profile_id, turn_id=session_id),
        model_client=model,
    )

    assert result.stopped_by == "stop"
    turns = list_turns_by_session(session_id)
    assert [row["role"] for row in turns] == ["user", "assistant"]
    assert "德国市场" in turns[0]["content"]
    assert "德国市场" in turns[1]["content"]


async def test_step21_history_adapter_reads_session_turns():
    from db.social_agent_sessions import upsert_agent_session
    from db.social_agent_turns import record_agent_turn
    from tools.agent_loop.adapters import history

    session_id, turn_id, profile_id = _ids()
    upsert_agent_session(
        session_id=session_id,
        user_id=7,
        profile_id=profile_id,
        summary="用户上次要求少用感叹号，多用经营者视角。",
        topic="表达偏好",
    )
    record_agent_turn(
        session_id=session_id,
        turn_id=turn_id,
        user_id=7,
        profile_id=profile_id,
        role="user",
        content="我不喜欢感叹号，也不想要鸡血口播。",
    )

    result = await history.search(profile_id=profile_id, query="感叹号 鸡血", limit=5)

    assert result["source"] == "social_agent_sessions"
    assert result["items"][0]["session_id"] == session_id
    assert "感叹号" in result["items"][0]["summary"]


# [开源 E3 · B2 · 2026-09-28] 局部重写工具与社媒内容工坊随 E3 删除,守它们的 2 格退役。


