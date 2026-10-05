import pytest


pytestmark = pytest.mark.asyncio


async def test_update_profile_memory_ai_inferred_is_pending(monkeypatch):
    from tools.agent_loop.write_tools import update_memory

    calls = []

    def fake_record(**kwargs):
        calls.append(kwargs)
        return 88

    monkeypatch.setattr(update_memory, "record_profile_memory_event", fake_record)

    result = await update_memory.update_profile_memory(
        profile_id="p1",
        concept="target_customer",
        text="目标客户是德国B端采购经理",
        source="ai_inferred",
        confidence=0.7,
        ctx_user_id=10,
    )

    assert result["status"] == "pending_confirm"
    assert result["event_id"] == 88
    assert calls[0]["review_status"] == "pending"


async def test_update_profile_field_ai_inferred_does_not_mutate_profile(monkeypatch):
    from tools.agent_loop.write_tools import update_field

    records = []

    def fake_record(**kwargs):
        records.append(kwargs)
        return 89

    def should_not_update(*args, **kwargs):
        raise AssertionError("ai_inferred must not directly update profile")

    monkeypatch.setattr(update_field, "record_profile_memory_event", fake_record)
    monkeypatch.setattr(update_field, "update_profile", should_not_update)

    result = await update_field.update_profile_field(
        profile_id="p1",
        field="industry",
        value="装修",
        source="ai_inferred",
        confidence=0.8,
        ctx_user_id=10,
    )

    assert result["status"] == "pending_confirm"
    assert records[0]["review_status"] == "pending"


async def test_update_profile_field_user_explicit_updates_profile(monkeypatch):
    from tools.agent_loop.write_tools import update_field

    calls = []

    def fake_update_profile(profile_id, **kwargs):
        calls.append((profile_id, kwargs))
        return True

    monkeypatch.setattr(update_field, "update_profile", fake_update_profile)

    result = await update_field.update_profile_field(
        profile_id="p1",
        field="industry",
        value="装修",
        source="user_explicit",
        ctx_user_id=10,
    )

    assert result["status"] == "auto"
    assert calls == [("p1", {"industry": "装修"})]


async def test_update_profile_taboo_appends_social_field_and_records_memory(monkeypatch):
    from tools.agent_loop.write_tools import update_taboo

    appended = []
    records = []

    monkeypatch.setattr(update_taboo, "append_profile_social_field_items", lambda *args, **kwargs: appended.append((args, kwargs)) or True)
    monkeypatch.setattr(update_taboo, "record_profile_memory_event", lambda **kwargs: records.append(kwargs) or 90)

    result = await update_taboo.update_profile_taboo(
        profile_id="p1",
        taboo="不要承诺低价第一",
        source="user_explicit",
        confidence=1.0,
        ctx_user_id=10,
    )

    assert result["status"] == "auto"
    assert appended[0][0][1] == "content_taboo"
    assert records[0]["canonical_concept"] == "guardrails"


async def test_archive_memory_event_uses_dismissed_status(monkeypatch):
    from tools.agent_loop.write_tools import archive_event

    calls = []
    monkeypatch.setattr(archive_event, "review_profile_memory_event", lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    result = await archive_event.archive_memory_event(event_id="123", reason="过时", ctx_user_id=10)

    assert result["status"] == "dismissed"
    assert calls[0][1]["review_status"] == "dismissed"
    assert calls[0][1]["is_active"] is False


# [开源 E3 · B2 · 2026-09-28] 局部重写工具 regenerate_part 随 E3 删除,守它的格退役。


async def test_cost_estimate_over_hard_limit_requires_confirmation():
    from tools.agent_loop.write_tools.cost_estimate import cost_estimate_and_confirm

    result = await cost_estimate_and_confirm(
        planned_tools=["tikhub_search_topics", "web_visit"],
        estimated_points=120,
        output_mode="full",
        profile_id="p1",
    )

    assert result["status"] == "requires_confirmation"
    assert result["limit"] == "hard"


async def test_router_dispatches_write_tool(monkeypatch):
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool
    from tools.agent_loop.write_tools import update_memory

    monkeypatch.setattr(update_memory, "record_profile_memory_event", lambda **kwargs: 91)
    result = await execute_tool(
        "update_profile_memory",
        {"profile_id": "p1", "concept": "target_customer", "text": "B端采购", "source": "ai_inferred"},
        AgentToolContext(user_id=10, profile_id="p1"),
    )

    assert result["ok"] is True
    assert result["result"]["status"] == "pending_confirm"
