import pytest


def test_url_check_rejects_private_targets():
    from tools.agent_loop.security.url_check import is_safe_url

    assert is_safe_url("https://example.com/page")[0] is True
    assert is_safe_url("http://localhost:8000/admin")[0] is False
    assert is_safe_url("http://127.0.0.1:8000/admin")[0] is False
    assert is_safe_url("ftp://example.com/file")[0] is False


def test_html_clean_removes_scripts_and_handlers():
    from tools.agent_loop.security.html_clean import clean_html

    cleaned = clean_html('<h1 onclick="x()">标题</h1><script>alert(1)</script><p>正文</p>')

    assert "script" not in cleaned.lower()
    assert "onclick" not in cleaned.lower()
    assert "标题" in cleaned
    assert "正文" in cleaned


def test_pii_mask_masks_phone_email_and_id():
    from tools.agent_loop.security.pii_mask import mask_pii

    masked = mask_pii("手机号 13800000000 邮箱 a@b.com 身份证 110101199003074512")

    assert "13800000000" not in masked
    assert "a@b.com" not in masked
    assert "110101199003074512" not in masked
    assert "[PHONE]" in masked
    assert "[EMAIL]" in masked
    assert "[ID]" in masked


def test_args_validator_rejects_extra_args():
    from tools.agent_loop.security.args_validator import validate_tool_args
    from tools.agent_loop.tool_definitions import get_tool_schema

    error = validate_tool_args(get_tool_schema("time_now"), {"timezone": "UTC", "extra": "nope"})

    assert error
    assert "extra" in error


@pytest.mark.asyncio
async def test_router_blocks_cross_profile_tool_call():
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    result = await execute_tool(
        "internal_profile_get",
        {"profile_id": "victim_profile", "fields": ["industry"]},
        AgentToolContext(user_id=7, profile_id="my_profile"),
    )

    assert result["ok"] is False
    assert result["error"] == "permission_denied"


@pytest.mark.asyncio
async def test_router_masks_pii_from_profile_memory(monkeypatch):
    from tools.agent_loop.adapters import memory
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    monkeypatch.setattr(
        memory,
        "list_profile_memory_events",
        lambda *args, **kwargs: [{"text": "客户手机号 13800000000", "canonical_concept": "target_customer"}],
    )

    result = await execute_tool(
        "internal_memory_query",
        {"profile_id": "p1", "concept": "target_customer"},
        AgentToolContext(user_id=7, profile_id="p1"),
    )

    assert result["ok"] is True
    assert "13800000000" not in str(result["result"])
    assert "[PHONE]" in str(result["result"])


@pytest.mark.asyncio
async def test_web_visit_rejects_localhost():
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    result = await execute_tool(
        "web_visit",
        {"url": "http://localhost:8000/admin", "purpose": "research"},
        AgentToolContext(user_id=7, profile_id="p1"),
    )

    assert result["ok"] is False
    assert result["error"].startswith("unsafe_url")


@pytest.mark.asyncio
async def test_web_visit_cleans_html(monkeypatch):
    from tools.agent_loop.adapters import web
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    async def fake_fetch(url, timeout_seconds=12):
        return "<html><script>alert(1)</script><body>真实内容 13800000000</body></html>"

    monkeypatch.setattr(web, "_fetch_text", fake_fetch)

    result = await execute_tool(
        "web_visit",
        {"url": "https://example.com/page", "purpose": "research"},
        AgentToolContext(user_id=7, profile_id="p1"),
    )

    assert result["ok"] is True
    text = result["result"]["text"]
    assert "script" not in text.lower()
    assert "13800000000" not in text
    assert "[PHONE]" in text
