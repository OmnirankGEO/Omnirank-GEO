import pytest


def _cleanup_plans():
    from db.social_agent_plans import ensure_tables, get_connection

    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_agent_plans WHERE profile_id LIKE 'pytest_plan_%'")
        conn.commit()
    finally:
        conn.close()


def test_plan_db_upserts_one_active_plan_per_user_profile():
    # P0-10 fix · 新设计 archive→INSERT(旧 plan superseded · 新 plan INSERT)
    # 不再覆盖旧 row · 保留历史 user_intent_summary
    from db.social_agent_plans import get_active_plan, get_plan_by_id, upsert_active_plan

    _cleanup_plans()
    first = upsert_active_plan(
        user_id=1001,
        profile_id="pytest_plan_profile_a",
        state_json={"steps": ["先梳理", "再写稿"], "current_step": 1},
        user_intent_summary="先梳理再写稿",
    )
    second = upsert_active_plan(
        user_id=1001,
        profile_id="pytest_plan_profile_a",
        state_json={"steps": ["先梳理", "再写稿"], "current_step": 2},
        user_intent_summary="继续第二步",
    )
    active = get_active_plan(user_id=1001, profile_id="pytest_plan_profile_a")
    archived = get_plan_by_id(first["plan_id"])

    # 新 plan 拿到新 plan_id · 唯一 active partial index 仍生效
    assert second["plan_id"] != first["plan_id"]
    assert active["plan_id"] == second["plan_id"]
    assert active["current_step"] == 2
    assert active["state_json"]["current_step"] == 2
    assert active["status"] == "active"
    # 旧 plan 仍存在 · 状态 superseded · user_intent_summary 保留
    assert archived["status"] == "superseded"
    assert archived["user_intent_summary"] == "先梳理再写稿"


def test_mark_stale_plans_moves_old_active_plan_to_stale():
    from db.social_agent_plans import get_plan_by_id, get_connection, mark_stale_plans, upsert_active_plan

    _cleanup_plans()
    plan = upsert_active_plan(
        user_id=1002,
        profile_id="pytest_plan_profile_stale",
        state_json={"steps": ["旧计划"], "current_step": 1},
        user_intent_summary="旧计划",
    )
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE social_agent_plans SET updated_at = NOW() - INTERVAL '25 hours' WHERE plan_id=%s",
                (plan["plan_id"],),
            )
        conn.commit()
    finally:
        conn.close()

    changed = mark_stale_plans(stale_after_hours=24)
    stale = get_plan_by_id(plan["plan_id"])

    assert changed >= 1
    assert stale["status"] == "stale"


@pytest.mark.asyncio
async def test_agent_loop_persists_model_plan_state():
    from db.social_agent_plans import get_active_plan
    from tools.agent_loop.agent_loop import run_agent_loop
    from tools.agent_loop.tool_router import AgentToolContext

    _cleanup_plans()

    async def model_client(messages, tools, model):
        return {
            "message": {
                "content": "我先拆成两步，先梳理目标客户，再写正文。",
                "plan_state": {
                    "steps": ["梳理目标客户", "写正文"],
                    "current_step": 1,
                    "status": "active",
                },
            },
            "finish_reason": "stop",
        }

    result = await run_agent_loop(
        [{"role": "user", "content": "先帮我梳理三国B端人群，再写TikTok脚本"}],
        ctx=AgentToolContext(user_id=1003, profile_id="pytest_plan_profile_loop"),
        model_client=model_client,
    )
    active = get_active_plan(user_id=1003, profile_id="pytest_plan_profile_loop")

    assert result.stopped_by == "stop"
    assert active["state_json"]["steps"] == ["梳理目标客户", "写正文"]
    assert active["current_step"] == 1
    plan_event = next(event for event in result.events if event["type"] == "plan_state")
    assert plan_event["steps"] == ["梳理目标客户", "写正文"]
    assert plan_event["user_intent_summary"] == "我先拆成两步，先梳理目标客户，再写正文。"
