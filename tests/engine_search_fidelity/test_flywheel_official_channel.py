"""飞轮 DeepSeek 换官方通道 · 判别锁(Owner 2026-07-27:"一定要验证是真的官方通道")。

改前 `services/research_monitor/platforms.py::query_deepseek` 走阿里百炼 + `enable_search` ——
飞轮的语料血缘一直记的是**阿里的检索口径**。复检 AI 的配对实证:近 30 天 138 对里
**91 对(66%)引用与通义字节级完全相同**,而 219 对正文无一相同 ——
两个不同模型生成的答案、检索结果却大面积同源,这就是"检索层被换掉"的直接证据。

「验证是真的官方通道」在本文件里落成两层:
  ① **静态**:端点必须是 api.deepseek.com,且不含 dashscope;鉴权走 x-api-key;
  ② **运行时**:`_assert_official_deepseek_shape` 按**响应形状**验 ——
     端点写对不等于打到了官方(可能被改回百炼、或中间加代理),
     而百炼返回 `{"output": ...}`、官方返回 `type=='message'` + `content` 块数组,一验即露。

实机验证已在生产容器跑过(2026-07-27),记录见证据文档 §八。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from services.research_monitor import platforms as fw


REAL_SHAPE = {
    #: 🔴 [WO_221-c1'] 夹具回显改成官方**现在真的回显**的名字。
    #:   两条端点行为不同(2026-09-15 实测):/v1/chat/completions 会把旧名归一成
    #:   deepseek-flash,而本文件测的 /anthropic/v1/messages **原样回显**。
    #:   夹具停在旧名上就复现不出线上形态,回显锁也测不到。
    "id": "fw-1", "type": "message", "role": "assistant", "model": "deepseek-flash",
    "stop_reason": "end_turn",
    "content": [
        {"type": "text", "text": "开场白"},
        {"type": "server_tool_use", "id": "c1", "name": "web_search", "input": {"query": "q"}},
        {"type": "web_search_tool_result", "tool_use_id": "c1", "content": [
            {"type": "web_search_result", "title": "来源A", "url": "https://a.example/1",
             "page_age": None, "encrypted_content": "x" * 40},
            {"type": "web_search_result", "title": "来源B", "url": "https://b.example/2",
             "page_age": None, "encrypted_content": "y" * 40},
        ]},
        {"type": "web_search_tool_result", "tool_use_id": "c2", "content": [
            # 实测:同一 URL 会跨轮重复出现 → 进 answer_ranks,不新增 citation
            {"type": "web_search_result", "title": "来源A 重复", "url": "https://a.example/1",
             "page_age": None, "encrypted_content": "z" * 10},
        ]},
        {"type": "text", "text": "正文结论"},
    ],
    "usage": {"input_tokens": 73648, "cache_read_input_tokens": 512, "output_tokens": 1490,
              "server_tool_use": {"web_search_requests": 2}},
}

DASHSCOPE_SHAPE = {"output": {"choices": [{"message": {"content": "百炼回答"}}],
                              "search_info": {"search_results": [{"url": "https://ali/1"}]}}}


# ---------------------------------------------------------------------------
# ① 静态:端点与鉴权
# ---------------------------------------------------------------------------
def test_endpoint_is_official_not_dashscope():
    """🔒 变异锁:端点改回 dashscope → 转红。"""
    assert "api.deepseek.com" in fw.DEEPSEEK_OFFICIAL_URL
    assert "dashscope" not in fw.DEEPSEEK_OFFICIAL_URL.lower()
    assert fw.DEEPSEEK_OFFICIAL_URL.endswith("/anthropic/v1/messages")


def test_web_search_tool_declared():
    assert fw.DEEPSEEK_OFFICIAL_WEB_SEARCH_TOOL["type"] == "web_search_20250305"
    assert fw.DEEPSEEK_OFFICIAL_WEB_SEARCH_TOOL["name"] == "web_search"


def test_source_uses_official_key_and_fails_closed():
    """缺官方 key 必须显式报错,**不许回落百炼**。"""
    import inspect

    src = inspect.getsource(fw.query_deepseek)
    assert "DEEPSEEK_API_KEY" in src
    assert "不回落百炼" in src or "fail-closed" in src
    assert "_research_dashscope_key" not in src, "还在读百炼 key = 没真换"


# ---------------------------------------------------------------------------
# ② 运行时形状自检(Owner 要求的"验证是真的官方通道")
# ---------------------------------------------------------------------------
def test_shape_check_accepts_official():
    fw._assert_official_deepseek_shape(REAL_SHAPE)  # 不抛即通过


def test_shape_check_rejects_dashscope_shape():
    """🔒 关键锁:哪天被改回百炼(或中间加代理),形状自检必须拦住,不静默接受。"""
    with pytest.raises(fw.NotOfficialDeepSeekError):
        fw._assert_official_deepseek_shape(DASHSCOPE_SHAPE)


@pytest.mark.parametrize("bad", [
    {}, {"type": "message"}, {"content": []}, {"type": "completion", "content": []},
    "not a dict", None,
])
def test_shape_check_rejects_malformed(bad):
    with pytest.raises(fw.NotOfficialDeepSeekError):
        fw._assert_official_deepseek_shape(bad)


# ---------------------------------------------------------------------------
# ③ 解析:引用只从 web_search_tool_result 取,血缘落官方
# ---------------------------------------------------------------------------
@pytest.fixture
def wired(monkeypatch):
    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return REAL_SHAPE

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            captured["url"] = url
            captured["headers"] = kw.get("headers") or {}
            captured["json"] = kw.get("json") or {}
            return _Resp()

    captured: dict = {}
    monkeypatch.setattr(fw.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-official")
    monkeypatch.setattr(fw, "_load_model_from_config", lambda *a, **k: "deepseek-v4-flash")
    monkeypatch.setattr(fw, "mark_answer_cited_sources", lambda answer, cits, raw=None: cits)

    class _T:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def record(self, **kw):
            captured.setdefault("tracker", []).append(kw)

    def _track(caller, platform, **kw):
        captured["track_platform"] = platform
        captured["track_metadata"] = kw.get("metadata") or {}
        return _T()

    monkeypatch.setattr(fw, "llm_track", _track)
    return captured


def _run():
    return asyncio.run(fw.query_deepseek(1, "杭州装修公司哪家好"))


def test_request_hits_official_with_anthropic_auth(wired):
    _run()
    assert "api.deepseek.com" in wired["url"] and "dashscope" not in wired["url"]
    assert wired["headers"].get("x-api-key")
    assert wired["headers"].get("anthropic-version")
    assert "Authorization" not in wired["headers"]
    assert wired["json"]["tools"][0]["type"] == "web_search_20250305"


def test_lineage_fields_report_official(wired):
    """🔒 飞轮血缘必须落 deepseek_official —— 改前是 dashscope。"""
    r = _run()
    assert r["provider"] == "deepseek_official"
    assert r["search_mode"] == "deepseek_native"
    assert r["surface"] == "ai_search"
    assert r["web_search_request_count"] == 2


def test_cost_tracking_platform_is_official(wired):
    """计费口径与百炼那条分开,否则成本对账会混。"""
    _run()
    assert wired["track_platform"] == "deepseek_official"
    assert wired["track_metadata"]["provider"] == "deepseek_official"


def test_citations_parsed_and_deduped_with_answer_ranks(wired):
    r = _run()
    urls = [c["url"] for c in r["citations"]]
    assert urls == ["https://a.example/1", "https://b.example/2"], "跨轮重复 URL 必须去重"
    a = next(c for c in r["citations"] if c["url"] == "https://a.example/1")
    assert a["answer_ranks"] == [1, 3], "重复出现要进 answer_ranks(与百炼口径一致)"


def test_answer_joins_text_blocks_only(wired):
    r = _run()
    assert "开场白" in r["answer"] and "正文结论" in r["answer"]


def test_official_platform_is_priced():
    """飞轮换了 platform 名,价目表必须有对应价,否则成本静默落默认值。"""
    from tools.llm_call_tracker import PRICING_TABLE

    #: 🔴 [WO_221-c1'] 钉**实际在发**的那个名字。原来钉的是退役名那一行 ——
    #:   那行还在表里,所以判据一直绿,但它守的是一条**没人走的路**。
    assert ("deepseek_official", "deepseek-flash") in PRICING_TABLE
