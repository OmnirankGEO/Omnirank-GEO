"""任务 A 判别锁:DeepSeek 必须打官方端点,并且真的声明了服务端检索工具。

背景(生产实证 2026-07-27):这一格改前打的是 DashScope,检索由**阿里**执行 ——
我们以为在测 DeepSeek 的引用行为,实际测的是阿里的检索行为。
引用产出率 DeepSeek 0.8% vs 通义 29.2%(同一套阿里检索),两家答案还高度相似。

本文件锁三件事:
  ① 请求真的打到 api.deepseek.com,且**不含 dashscope**;
  ② 请求体里真的带了 `web_search_20250305` 工具声明;
  ③ 三处血缘(执行层 / 监测血缘表 / 观测表面定义)一致 —— 这是本周踩过的坑。

变异验证(Review-CTO 复检会做,这里写清预期):
  · 把端点改回 dashscope → `test_request_hits_official_endpoint` 转红
  · 删掉 tools 声明   → `test_request_declares_web_search_tool` 转红
"""
from __future__ import annotations

import asyncio
import json

import pytest


# ---------------------------------------------------------------------------
# 真实响应形状 —— 逐字段照抄 2026-07-27 官方端点的实测响应
# (证据:docs/AI-CONTEXT/ENGINE_SEARCH_FIDELITY_EVIDENCE_2026-07-27.md)
# ---------------------------------------------------------------------------
REAL_SHAPE = {
    "id": "c2d4b975-8014-45a4-aaa6-ebb6749246d4",
    "type": "message",
    "role": "assistant",
    #: 🔴 [WO_221-c1] 夹具回显改成官方**现在真的回显**的那个名字。
    #:   旧名已退役:请求旧名仍返 200,但回显 `deepseek-flash` ——
    #:   夹具停在旧名上,就复现不出线上真实形态,回显锁也测不到
    #:   (本仓 an-unrealistic-fixture-hides-the-defect-the-poison-should-catch)。
    "model": "deepseek-flash",
    "stop_reason": "end_turn",
    "content": [
        {"type": "text", "text": "好的,我来帮你查找。"},
        {"type": "server_tool_use", "id": "call_00_x", "name": "web_search",
         "input": {"query": "2026年杭州装修公司推荐 排名"}, "caller": {"type": "direct"}},
        {"type": "web_search_tool_result", "tool_use_id": "call_00_x", "content": [
            {"type": "web_search_result", "title": "2026杭州家装综合实力十家正式发布",
             "url": "https://m.jiemian.com/article/14745524.html",
             "page_age": None, "encrypted_content": "x" * 3000},
            {"type": "web_search_result", "title": "2026杭州靠谱装修公司多维度观察_腾讯新闻",
             "url": "https://news.qq.com/rain/a/20260713A07M3F00",
             "page_age": None, "encrypted_content": "y" * 3000},
            # 同一轮里重复出现的 URL(实测两次检索结果高度重叠)
            {"type": "web_search_result", "title": "重复条目",
             "url": "https://m.jiemian.com/article/14745524.html",
             "page_age": None, "encrypted_content": "z" * 10},
        ]},
        {"type": "text", "text": "根据 2026 年多个第三方调研...(正文)"},
    ],
    "usage": {
        "input_tokens": 67461, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 512, "output_tokens": 1306,
        "service_tier": "standard",
        "server_tool_use": {"web_search_requests": 2},
    },
}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


@pytest.fixture
def captured_request(monkeypatch):
    """拦下真实 HTTP,记录 url / headers / body,返回实测形状的响应。"""
    from tools.ai_visibility import ai_tester

    seen: dict = {}

    async def _fake_tracked_post(client, url, *, platform, model, metadata=None, **kwargs):
        seen.update({"url": url, "platform": platform, "model": model,
                     "metadata": metadata or {}, **kwargs})
        return _FakeResponse(REAL_SHAPE)

    async def _fake_visibility(*_a, **_kw):
        return {"answer_summary": "ok", "mentioned_brands": [], "brand_detected": False}

    monkeypatch.setattr(ai_tester, "_tracked_post", _fake_tracked_post)
    monkeypatch.setattr(ai_tester, "_call_analyze_visibility", _fake_visibility)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-official-key-for-test")
    return seen


def _run(coro):
    return asyncio.run(coro)


def _query(**kwargs):
    from tools.ai_visibility.ai_tester import query_deepseek_official

    return _run(query_deepseek_official("杭州装修公司哪家好", "某品牌", **kwargs))


# ---------------------------------------------------------------------------
# ① 端点
# ---------------------------------------------------------------------------
def test_request_hits_official_endpoint(captured_request):
    """🔒 变异锁:端点改回 dashscope → 此测必须转红。"""
    _query()
    url = captured_request["url"]
    assert "api.deepseek.com" in url, f"DeepSeek 没打官方端点:{url}"
    assert "dashscope" not in url.lower(), (
        f"DeepSeek 又回到 DashScope 了 —— 那测的是阿里的检索行为,不是 DeepSeek:{url}"
    )
    assert url.endswith("/anthropic/v1/messages"), "官方走 Anthropic Messages 协议"


def test_request_uses_anthropic_auth_headers(captured_request):
    """Anthropic 协议:x-api-key + anthropic-version,不是 OpenAI 的 Bearer。"""
    _query()
    headers = captured_request["headers"]
    assert headers.get("x-api-key")
    assert headers.get("anthropic-version")
    assert "Authorization" not in headers


def test_tracker_platform_is_deepseek_official(captured_request):
    """成本落库要认得出这是官方通道,不能继续记成 dashscope。"""
    _query()
    assert captured_request["platform"] == "deepseek_official"
    assert captured_request["model"] == "deepseek-flash"


# ---------------------------------------------------------------------------
# ② 检索工具声明
# ---------------------------------------------------------------------------
def test_request_declares_web_search_tool(captured_request):
    """🔒 变异锁:删掉 tools 声明 → 此测必须转红。

    实测证据:不声明工具时响应里不会出现任何 server_tool_use 块 —— 就是完全不搜。
    """
    _query()
    body = captured_request["json"]
    tools = body.get("tools") or []
    assert tools, "没有声明任何工具 = 官方端点根本不会去检索"
    types = {t.get("type") for t in tools}
    assert "web_search_20250305" in types, f"检索工具声明不对:{types}"
    assert any(t.get("name") == "web_search" for t in tools)


def test_model_id_has_no_invented_search_suffix(captured_request):
    """网上流传的 `deepseek-flash-search` / `deepseek-v4-flash-search` 等 ID
    官方查无实据,实测也不需要 —— 检索由 `tools=[web_search]` 开,不靠模型名后缀。"""
    _query()
    assert captured_request["json"]["model"] == "deepseek-flash"
    assert "search" not in captured_request["json"]["model"]


# ---------------------------------------------------------------------------
# ③ 解析:引用只从 web_search_tool_result 取
# ---------------------------------------------------------------------------
def test_citations_come_from_tool_result_block(captured_request):
    result = json.loads(_query().content[0]["text"])
    citations = result["search_citations"]
    # 三条结果里有两条不同 URL,重复的那条要去重
    assert [c["url"] for c in citations] == [
        "https://m.jiemian.com/article/14745524.html",
        "https://news.qq.com/rain/a/20260713A07M3F00",
    ]
    assert citations[0]["title"].startswith("2026杭州家装")


def test_encrypted_content_never_leaks_into_citations(captured_request):
    """encrypted_content 是 DeepSeek 的不透明大 blob(单条数 KB),不落库不外传。"""
    result = json.loads(_query().content[0]["text"])
    payload = json.dumps(result, ensure_ascii=False)
    assert "encrypted_content" not in payload
    assert "x" * 100 not in payload


def test_search_request_count_is_read_from_usage(captured_request):
    """成本口径必须实测落库:服务端检索次数来自 usage.server_tool_use。"""
    result = json.loads(_query().content[0]["text"])
    assert result["web_search_request_count"] == 2
    assert result["web_search_enabled"] is True


def test_web_search_enabled_reflects_reality_not_intent(captured_request, monkeypatch):
    """没真的检索就不能标 enabled —— 不拿"我们声明了工具"当"已检索"。"""
    from tools.ai_visibility import ai_tester

    no_search = json.loads(json.dumps(REAL_SHAPE))
    no_search["content"] = [{"type": "text", "text": "纯模型回答"}]
    no_search["usage"].pop("server_tool_use")

    async def _fake_post(client, url, **kwargs):
        return _FakeResponse(no_search)

    monkeypatch.setattr(ai_tester, "_tracked_post", _fake_post)
    result = json.loads(_query().content[0]["text"])
    assert result["web_search_enabled"] is False
    assert result["web_search_request_count"] == 0
    assert result["search_citations"] == []


def test_text_blocks_joined_thinking_excluded(captured_request):
    """thinking 块是思维链,不算回答正文。"""
    from tools.ai_visibility.ai_tester import _deepseek_official_text

    text = _deepseek_official_text({
        "content": [
            {"type": "thinking", "thinking": "内心独白不算回答"},
            {"type": "text", "text": "正文一"},
            {"type": "text", "text": "正文二"},
        ]
    })
    assert "内心独白" not in text
    assert text == "正文一\n\n正文二"


# ---------------------------------------------------------------------------
# ④ fail-closed:缺 key 绝不静默回落
# ---------------------------------------------------------------------------
def test_missing_official_key_fails_closed(monkeypatch):
    """🔒 缺官方 key 必须显式报错,**不许**悄悄退回 DashScope。

    静默回落正是"以为在测 DeepSeek 其实测的是阿里"这个坑的成因。
    """
    from tools.ai_visibility import ai_tester

    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    async def _must_not_call(*_a, **_kw):
        raise AssertionError("缺 key 时不该发起任何请求,更不该回落到别家")

    monkeypatch.setattr(ai_tester, "_tracked_post", _must_not_call)
    result = json.loads(_query().content[0]["text"])
    assert result["engine_error"] is True
    assert "不回落" in result["answer_summary"]


# ---------------------------------------------------------------------------
# ⑤ 三处血缘一致(本周踩过的坑)
# ---------------------------------------------------------------------------
def test_three_lineage_sites_agree_on_deepseek():
    """执行层 / 监测血缘表 / 观测表面定义 三处必须一致。"""
    from services.ai_surface_monitoring.lineage import SURFACE_SPECS
    from tools.ai_visibility.ai_tester import (
        DEEPSEEK_OFFICIAL_BASE_URL,
        query_deepseek_official,
    )
    from tools.monitoring.batch_monitor import PlatformAdapter, _resolve_runtime_lineage

    # 执行层
    assert PlatformAdapter.SUPPORTED_PLATFORMS["deepseek"] is query_deepseek_official

    # 监测血缘表
    provider, model, surface, search_mode = _resolve_runtime_lineage("deepseek", "standard")
    assert provider == "deepseek_official"
    #: 🔴 [WO_221-c1] 这是写进**观测账本**的血缘标签(batch_monitor 的 contract)。
    #:   它错了,整列 DeepSeek 监测数据的引擎名就是错的,而且不会报错。
    assert model == "deepseek-flash"
    assert search_mode == "deepseek_native"

    # 观测表面定义
    spec = SURFACE_SPECS["deepseek_native_with_search"]
    assert spec.availability == "active", "官方原生搜索已实测可用,不该还是 unavailable"
    assert spec.provider_key == "deepseek_official"
    assert spec.env_key_var == "DEEPSEEK_API_KEY"
    assert spec.default_search_enabled is True
    assert spec.search_provider == "deepseek_native"
    assert spec.base_url == DEEPSEEK_OFFICIAL_BASE_URL, "表面 base_url 必须与执行层同一个常量口径"


def test_dashscope_deepseek_demoted_to_legacy():
    """旧通道保留作对照,但不能再是默认表面 —— 否则等于没换。"""
    from services.ai_surface_monitoring.lineage import SURFACE_SPECS

    legacy = SURFACE_SPECS["deepseek_dashscope_search_legacy"]
    assert legacy.default_enabled is False
    assert legacy.provider_key == "dashscope"
    assert SURFACE_SPECS["deepseek_native_no_search"].default_enabled is False


def test_anthropic_cache_tokens_are_accounted():
    """Anthropic 的缓存命中字段名不同,不补就会把成本记高。"""
    from tools.llm_call_tracker import usage_from_response_payload

    _in, _out, cached = usage_from_response_payload(REAL_SHAPE)
    assert _in == 67461 and _out == 1306
    assert cached == 512, "cache_read_input_tokens 没被算进缓存命中"
