"""SSE 供应商名 alias 回归(2026-05-16 老板铁律).

用户可见层禁露 tikhub / metaso / 5118 等供应商品牌名(商业机密)。
本测试保 SSE event 出口处把内部 tool 名翻译成 alias · 防再有未来 commit 把供应商名漏给前端。
"""

from __future__ import annotations



# ─── alias 表本身 ─────────────────────────────────────────────────────────────


def test_alias_map_covers_known_supplier_named_tools():
    from tools.agent_loop.sse_alias import SUPPLIER_TOOL_ALIAS_FOR_FE

    # tikhub_* 系列必须 alias
    assert "tikhub_search_topics" in SUPPLIER_TOOL_ALIAS_FOR_FE
    assert "tikhub_get_account" in SUPPLIER_TOOL_ALIAS_FOR_FE
    assert "tikhub_parse_video" in SUPPLIER_TOOL_ALIAS_FOR_FE
    # metaso 必须 alias
    assert "metaso_web_search" in SUPPLIER_TOOL_ALIAS_FOR_FE

    # alias 自身不能含供应商名
    for internal, alias in SUPPLIER_TOOL_ALIAS_FOR_FE.items():
        for forbidden in ("tikhub", "metaso", "5118", "moonshot", "dashscope", "doubao", "volcengine"):
            assert forbidden not in alias.lower(), (
                f"alias {alias!r} (from {internal!r}) 仍含供应商名 · 必须改"
            )


def test_to_external_tool_name_aliases_supplier_names():
    from tools.agent_loop.sse_alias import to_external_tool_name

    # 含供应商名 → 翻译
    assert "tikhub" not in to_external_tool_name("tikhub_search_topics").lower()
    assert "tikhub" not in to_external_tool_name("tikhub_get_account").lower()
    assert "tikhub" not in to_external_tool_name("tikhub_parse_video").lower()
    assert "metaso" not in to_external_tool_name("metaso_web_search").lower()


def test_to_external_tool_name_passthrough_for_neutral_tools():
    from tools.agent_loop.sse_alias import to_external_tool_name

    # 不含供应商名的工具 → passthrough(不变)
    for tool in (
        "internal_profile_get",
        "internal_memory_query",
        "internal_history_search",
        "industry_knowledge_query",
        "keyword_explore",
        "web_visit",
        "time_now",
        "update_profile_taboo",
        "update_profile_memory",
        "confirm_memory_conflict",
        "archive_memory_event",
        "update_profile_field",
        "cost_estimate_and_confirm",
    ):
        assert to_external_tool_name(tool) == tool


def test_to_external_tool_names_handles_empty():
    from tools.agent_loop.sse_alias import to_external_tool_names

    assert to_external_tool_names(None) == []
    assert to_external_tool_names([]) == []


# ─── SSE event builder 行为 ───────────────────────────────────────────────────


def test_tool_call_start_event_aliases_tool_field():
    from tools.agent_loop.sse_events import build_tool_call_start_event

    event = build_tool_call_start_event("tikhub_search_topics", {"keyword": "出海"})
    assert "tikhub" not in event["tool"].lower(), f"tool 字段仍露 tikhub · 实际 {event['tool']!r}"
    # display.tool_label 仍是中文中性词 OK
    assert event["display"]["tool_label"] == "搜索平台选题"


def test_tool_call_result_event_aliases_tool_field():
    from tools.agent_loop.sse_events import build_tool_call_result_event

    event = build_tool_call_result_event("metaso_web_search", {"ok": True})
    assert "metaso" not in event["tool"].lower(), f"tool 字段仍露 metaso · 实际 {event['tool']!r}"


def test_context_card_event_aliases_used_tools_array():
    from tools.agent_loop.sse_events import build_context_card_event

    event = build_context_card_event(
        used_tools=["tikhub_search_topics", "metaso_web_search", "internal_profile_get"],
        cost_points=10,
        model="test",
    )
    flat = " ".join(event["used_tools"]).lower()
    assert "tikhub" not in flat, f"context_card.used_tools 仍含 tikhub · 实际 {event['used_tools']}"
    assert "metaso" not in flat, f"context_card.used_tools 仍含 metaso · 实际 {event['used_tools']}"
    # 中性工具 passthrough
    assert "internal_profile_get" in event["used_tools"]




