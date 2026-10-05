import pytest


pytestmark = pytest.mark.asyncio


async def test_time_now_returns_iso_timestamp():
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    ctx = AgentToolContext(user_id=1, profile_id="p1")
    result = await execute_tool("time_now", {"timezone": "Asia/Shanghai"}, ctx)

    assert result["ok"] is True
    assert result["tool"] == "time_now"
    assert result["result"]["timezone"] == "Asia/Shanghai"
    assert "iso" in result["result"]


async def test_web_visit_fetches_after_ssrf_guard(monkeypatch):
    from tools.agent_loop.adapters import web
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    async def fake_fetch(url, timeout_seconds=12):
        return "<main>Example Content</main>"

    monkeypatch.setattr(web, "_fetch_text", fake_fetch)
    ctx = AgentToolContext(user_id=1, profile_id="p1")
    result = await execute_tool(
        "web_visit",
        {"url": "https://example.com", "purpose": "research"},
        ctx,
    )

    assert result["ok"] is True
    assert result["tool"] == "web_visit"
    assert result["result"]["text"] == "Example Content"


async def test_router_rejects_duplicate_tool_args():
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    ctx = AgentToolContext(user_id=1, profile_id="p1")
    args = {"timezone": "Asia/Shanghai"}
    first = await execute_tool("time_now", args, ctx)
    second = await execute_tool("time_now", args, ctx)

    assert first["ok"] is True
    assert second["ok"] is False
    assert "duplicate" in second["error"]


async def test_router_validates_required_args_without_crashing():
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    ctx = AgentToolContext(user_id=1, profile_id="p1")
    result = await execute_tool("metaso_web_search", {}, ctx)

    assert result["ok"] is False
    assert result["tool"] == "metaso_web_search"
    assert result["fallback_policy"] == "mark_uncertain"


async def test_high_risk_industry_elevates_fallback(monkeypatch):
    from tools.agent_loop.adapters import tikhub
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    async def boom(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(tikhub, "search_topics", boom)
    ctx = AgentToolContext(user_id=1, profile_id="p1")

    result = await execute_tool(
        "tikhub_search_topics",
        {"platform": "douyin", "industry": "医美诊所"},
        ctx,
    )

    assert result["ok"] is False
    assert result["fallback_policy"] == "refuse_to_guess"


async def test_search_adapter_uses_metaso_wrapper(monkeypatch):
    from tools.agent_loop.adapters import search
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    async def fake_metaso(query, **kwargs):
        return {"webpages": [{"title": "装修 2026", "url": "https://example.com"}]}

    monkeypatch.setattr(search, "metaso_web_search", fake_metaso)
    ctx = AgentToolContext(user_id=1, profile_id="p1")

    result = await execute_tool("metaso_web_search", {"query": "装修行业 2026"}, ctx)

    assert result["ok"] is True
    assert result["result"]["source"] == "metaso"
    assert result["result"]["items"][0]["title"] == "装修 2026"


async def test_keyword_adapter_can_be_stubbed(monkeypatch):
    from tools.agent_loop.adapters import keyword
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    async def fake_expand(**kwargs):
        return {"success": True, "keywords": [{"keyword": "装修避坑"}]}

    monkeypatch.setattr(keyword, "expand_keywords_for_client", fake_expand)
    ctx = AgentToolContext(user_id=1, profile_id="p1")

    result = await execute_tool("keyword_explore", {"seed": "装修", "industry": "装修"}, ctx)

    assert result["ok"] is True
    assert result["result"]["keywords"][0]["keyword"] == "装修避坑"


async def test_industry_adapter_reads_existing_knowledge(monkeypatch):
    from tools.agent_loop.adapters import industry
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    def fake_get_industry_knowledge(industry_name, category=None, level=None):
        return {"target_audience": {"items": ["宝妈"]}, "pain_points": ["怕踩坑"]}

    monkeypatch.setattr(industry, "get_industry_knowledge", fake_get_industry_knowledge)
    ctx = AgentToolContext(user_id=1, profile_id="p1")

    result = await execute_tool(
        "industry_knowledge_query",
        {"industry": "轻医美", "question": "目标客户"},
        ctx,
    )

    assert result["ok"] is True
    assert result["result"]["knowledge"]["pain_points"] == ["怕踩坑"]


async def test_memory_adapter_filters_by_concept(monkeypatch):
    from tools.agent_loop.adapters import memory
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    def fake_list_profile_memory_events(profile_id, **kwargs):
        return [{"id": 7, "canonical_concept": "target_customer", "text": "25-35岁女性"}]

    monkeypatch.setattr(memory, "list_profile_memory_events", fake_list_profile_memory_events)
    ctx = AgentToolContext(user_id=1, profile_id="p1")

    result = await execute_tool(
        "internal_memory_query",
        {"profile_id": "p1", "concept": "target_customer"},
        ctx,
    )

    assert result["ok"] is True
    assert result["result"]["items"][0]["id"] == 7


async def test_profile_adapter_reads_selected_fields(monkeypatch):
    from tools.agent_loop.adapters import profile
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    def fake_get_client_profile(profile_id):
        return {"id": profile_id, "industry": "教育咨询", "brand_name": "测试品牌"}

    monkeypatch.setattr(profile, "get_client_profile", fake_get_client_profile)
    ctx = AgentToolContext(user_id=1, profile_id="p1")

    result = await execute_tool(
        "internal_profile_get",
        {"profile_id": "p1", "fields": ["industry", "brand_name"]},
        ctx,
    )

    assert result["ok"] is True
    assert result["result"]["profile"]["industry"] == "教育咨询"


async def test_history_adapter_returns_empty_when_no_sessions():
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    ctx = AgentToolContext(user_id=1, profile_id="p1")
    result = await execute_tool("internal_history_search", {"profile_id": "p1", "query": "上次"}, ctx)

    assert result["ok"] is True
    assert result["result"]["items"] == []
    assert result["result"]["status"] == "ok"


async def test_tikhub_adapter_can_be_stubbed(monkeypatch):
    from tools.agent_loop.adapters import tikhub
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    async def fake_search_topics(**kwargs):
        return {"items": [{"title": "三秒讲清价格"}]}

    monkeypatch.setattr(tikhub, "search_topics", fake_search_topics)
    ctx = AgentToolContext(user_id=1, profile_id="p1")

    result = await execute_tool(
        "tikhub_search_topics",
        {"industry": "装修", "platform": "douyin"},
        ctx,
    )

    assert result["ok"] is True
    assert result["result"]["items"][0]["title"] == "三秒讲清价格"


async def test_tikhub_topic_search_parses_tool_response_into_research_brief(monkeypatch):
    import json
    from types import SimpleNamespace

    from tools.agent_loop.adapters import tikhub

    async def fake_search_douyin_videos(keyword, *, limit=10):
        payload = {
            "status": "success",
            "count": 2,
            "list": [
                {
                    "id": "v1",
                    "desc": "装修避坑：半包装修报价为什么差一倍",
                    "author": {"nickname": "设计师阿林"},
                    "stats": {"digg": 3200, "comment": 180, "share": 66},
                    "url": "https://www.douyin.com/video/v1",
                },
                {
                    "id": "v2",
                    "desc": "小户型改造前后对比，收纳翻倍",
                    "author": {"nickname": "空间改造师"},
                    "stats": {"digg": 2100, "comment": 95, "share": 40},
                    "url": "https://www.douyin.com/video/v2",
                },
            ],
        }
        return SimpleNamespace(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])

    monkeypatch.setattr(tikhub, "_search_douyin_videos", fake_search_douyin_videos)

    result = await tikhub.search_topics("装修设计", "douyin", market="中国", limit=2)

    assert result["status"] == "success"
    assert result["items"][0]["title"] == "装修避坑：半包装修报价为什么差一倍"
    assert result["research_table"][0]["作者"] == "设计师阿林"
    assert result["research_table"][0]["互动数据"].startswith("赞 3200")
    assert result["insights"]["top_sample_count"] == 2
    assert "装修避坑" in result["summary_markdown"]
    assert "raw" not in result


async def test_tikhub_topic_search_supports_tiktok_endpoint(monkeypatch):
    from tools.agent_loop.adapters import tikhub

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "data": {
                    "item_list": [
                        {
                            "id": "tt1",
                            "desc": "How German supermarkets choose laundry pods suppliers",
                            "author": {"nickname": "B2B Retail Notes"},
                            "statistics": {"digg_count": 4200, "comment_count": 310, "share_count": 90},
                            "share_url": "https://www.tiktok.com/@demo/video/tt1",
                        }
                    ]
                }
            }

    async def fake_tracked_get(client, url, **kwargs):
        assert url.endswith("/api/v1/tiktok/app/v3/fetch_video_search_result")
        assert kwargs["params"]["keyword"] == "洗衣凝珠"
        assert kwargs["params"]["count"] == 1
        assert kwargs["params"]["region"] == "DE"
        return FakeResponse()

    monkeypatch.setattr(tikhub, "_tikhub_api_key", lambda: "test-token")
    monkeypatch.setattr(tikhub, "tracked_tikhub_get", fake_tracked_get)

    result = await tikhub.search_topics("洗衣凝珠", "tiktok", market="德国", limit=1)

    assert result["status"] == "success"
    assert result["items"][0]["title"] == "How German supermarkets choose laundry pods suppliers"
    assert result["research_table"][0]["作者"] == "B2B Retail Notes"


async def test_tikhub_topic_search_supports_bilibili_endpoint(monkeypatch):
    from tools.agent_loop.adapters import tikhub

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "data": {
                    "result": [
                        {
                            "bvid": "BV1demo",
                            "title": "AI工具评测：三个国产模型实测",
                            "author": "科技区小明",
                            "play": 98000,
                            "review": 1200,
                            "favorites": 3000,
                            "arcurl": "https://www.bilibili.com/video/BV1demo",
                        }
                    ]
                }
            }

    async def fake_tracked_get(client, url, **kwargs):
        assert url.endswith("/api/v1/bilibili/app/fetch_search_by_type")
        assert kwargs["params"]["keyword"] == "AI工具评测"
        assert kwargs["params"]["search_type"] == "video"
        assert kwargs["params"]["page_size"] == 1
        return FakeResponse()

    monkeypatch.setattr(tikhub, "_tikhub_api_key", lambda: "test-token")
    monkeypatch.setattr(tikhub, "tracked_tikhub_get", fake_tracked_get)

    result = await tikhub.search_topics("AI工具评测", "bilibili", market="中国", limit=1)

    assert result["status"] == "success"
    assert result["items"][0]["title"] == "AI工具评测：三个国产模型实测"
    assert result["research_table"][0]["作者"] == "科技区小明"


async def test_tikhub_topic_search_supports_hot_topic_alias(monkeypatch):
    from tools.agent_loop.adapters import tikhub

    async def fake_hot():
        return {
            "trending": [
                {"word": "本周抖音热点一", "hot_value": 1000, "rank": 1},
                {"word": "本周抖音热点二", "hot_value": 800, "rank": 2},
            ]
        }

    monkeypatch.setattr(tikhub, "_search_douyin_hot", fake_hot)

    result = await tikhub.search_topics("热点", "热点", market="中国", limit=2)

    assert result["status"] == "success"
    assert result["items"][0]["title"] == "本周抖音热点一"
    assert result["research_table"][0]["作者"] == "抖音热榜"
    assert "平台热点" in result["research_table"][0]["可观察信号"]
