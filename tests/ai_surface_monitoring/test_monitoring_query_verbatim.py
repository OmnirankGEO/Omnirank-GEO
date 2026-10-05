"""持续监测商业铁律:monitoring_query 逐字 SSOT(§6.3 / Codex #1)。

有值时 adapter 实际发给 provider 的 question 必须与之逐字一致(strip 后 content-verbatim);
禁止替换为"{keyword}哪家好？请推荐几家"或让 LLM 改写后替代。捕获真实发出的 question 断言。
"""

from __future__ import annotations

import pytest

from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.contracts import CollectionRequest
from .conftest import make_engine_fetch, make_http_post

MQ = "上海做知识产权的律师事务所有哪些靠谱的"  # 客户购买的自定义短句(非"哪家好"模板)


def _req(surface: str) -> CollectionRequest:
    return CollectionRequest(
        request_id="r-verbatim", source_kind="monitoring", source_ref="kw-77",
        question_text=MQ, query_kind="non_branded", surface_key=surface,
    )


@pytest.mark.parametrize("surface", [
    "doubao_ark_api_search", "qwen_dashscope_search",
    "deepseek_dashscope_search_legacy", "other_explicit",
])
async def test_wrapped_adapter_sends_monitoring_query_verbatim(surface):
    capture = {}
    adapter = WrappedResearchAdapter(surface, fetch=make_engine_fetch(capture=capture))
    env = await adapter.collect(_req(surface))
    # 引擎收到的 prompt == 客户短句(逐字);绝不含"哪家好"模板
    assert capture["prompt"] == MQ
    assert "哪家好" not in capture["prompt"]
    assert env.question_text == MQ
    assert env.prompt_text == MQ  # 无 framing override 时 prompt_text 逐字等于 question_text


async def test_yuanbao_sends_monitoring_query_verbatim():
    capture = {}
    http = make_http_post(
        {"id": "x", "model": "hy3", "choices": [{"message": {"content": "答"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        capture=capture,
    )
    adapter = OpenAICompatibleAdapter("yuanbao_hy3_tokenhub", http_post=http, api_key_getter=lambda n: "K")
    env = await adapter.collect(_req("yuanbao_hy3_tokenhub"))
    sent = capture["body"]["messages"][0]["content"]
    assert sent == MQ and "哪家好" not in sent
    assert env.question_text == MQ and env.prompt_text == MQ


async def test_deepseek_native_sends_monitoring_query_verbatim():
    capture = {}
    http = make_http_post(
        {"id": "x", "model": "deepseek-v4-flash", "choices": [{"message": {"content": "答"}}], "usage": {}},
        capture=capture,
    )
    adapter = OpenAICompatibleAdapter("deepseek_native_no_search", http_post=http,
                                      api_key_getter=lambda n: "K", verify_model_echo=False)
    env = await adapter.collect(_req("deepseek_native_no_search"))
    sent = capture["body"]["messages"][0]["content"]
    assert sent == MQ and "哪家好" not in sent


async def test_whitespace_is_stripped_but_content_preserved():
    """逐字 = strip 后 content-verbatim(与 resolve_monitoring_query 一致:去首尾空白,内部不改)。"""
    capture = {}
    adapter = WrappedResearchAdapter("doubao_ark_api_search", fetch=make_engine_fetch(capture=capture))
    padded = "  " + MQ + "  "
    req = _req("doubao_ark_api_search").model_copy(update={"question_text": padded})
    env = await adapter.collect(req)
    # question_text 原样保留(adapter 不改 question_text);只有 hash 规范化去空白
    assert env.question_text == padded
    # 发给引擎的 prompt 也是原样(adapter 不 strip 发送内容,保真;整合层的 resolve 已 strip 存库)
    assert capture["prompt"] == padded


async def test_framing_goes_to_prompt_text_not_question_text():
    """system framing 只进 prompt_text,question_text 保持客户短句不被覆盖(§5)。"""
    capture = {}
    adapter = WrappedResearchAdapter("doubao_ark_api_search", fetch=make_engine_fetch(capture=capture))
    framed = "请基于最新公开信息回答:" + MQ
    req = _req("doubao_ark_api_search").model_copy(update={"prompt_text_override": framed})
    env = await adapter.collect(req)
    assert env.question_text == MQ          # 客户短句不被 framing 覆盖
    assert env.prompt_text == framed        # framing 进 prompt_text
    assert capture["prompt"] == framed      # 实际发给 provider 的是 framed prompt
