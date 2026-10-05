"""adapter 契约测试(§10.1):同一信封、缺字段不抛、fallback 留痕、retry 可识别、五态判别。"""

from __future__ import annotations

import asyncio

import pytest

from services.ai_surface_monitoring.adapters.base import BaseSurfaceAdapter, EngineResult
from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.contracts import CollectionRequest, ObservationEnvelopeV1
from .conftest import make_engine_fetch, make_http_post


def _req(surface: str, rid: str = "r") -> CollectionRequest:
    return CollectionRequest(request_id=rid, source_kind="monitoring", source_ref="s",
                             question_text="上海律师推荐", query_kind="non_branded", surface_key=surface)


async def test_five_adapters_return_same_envelope():
    hy3_http = make_http_post({"id": "y", "model": "hy3",
                               "choices": [{"message": {"content": "答"}}], "usage": {}})
    ds_http = make_http_post({"id": "d", "model": "deepseek-v4-flash",
                              "choices": [{"message": {"content": "答"}}], "usage": {}})
    adapters = {
        "doubao_ark_api_search": WrappedResearchAdapter("doubao_ark_api_search", fetch=make_engine_fetch()),
        "qwen_dashscope_search": WrappedResearchAdapter("qwen_dashscope_search", fetch=make_engine_fetch()),
        "other_explicit": WrappedResearchAdapter("other_explicit", fetch=make_engine_fetch()),
        "yuanbao_hy3_tokenhub": OpenAICompatibleAdapter("yuanbao_hy3_tokenhub", http_post=hy3_http,
                                                        api_key_getter=lambda n: "K"),
        "deepseek_native_no_search": OpenAICompatibleAdapter("deepseek_native_no_search", http_post=ds_http,
                                                             api_key_getter=lambda n: "K", verify_model_echo=False),
    }
    for sk, a in adapters.items():
        env = await a.collect(_req(sk))
        assert isinstance(env, ObservationEnvelopeV1)
        assert env.surface_key == sk
        assert env.schema_version == "geo-observation-v1"
        assert env.question_hash and env.answer_hash and env.observed_at


async def test_missing_provider_fields_no_uncaught_exception():
    # 引擎 dict 缺 answer/citations/raw
    async def sparse(pid, prompt):
        return {"ok": True}
    env = await WrappedResearchAdapter("qwen_dashscope_search", fetch=sparse).collect(_req("qwen_dashscope_search"))
    assert env.answer_text == "" and env.citations == [] and env.response_status == "unknown"

    # yuanbao 响应缺 choices
    http = make_http_post({"id": "x", "model": "hy3", "usage": {}})
    env2 = await OpenAICompatibleAdapter("yuanbao_hy3_tokenhub", http_post=http,
                                         api_key_getter=lambda n: "K").collect(_req("yuanbao_hy3_tokenhub"))
    assert env2.answer_text == "" and env2.response_status == "unknown"


async def test_fallback_records_real_executor():
    """发生 fallback → provider/model/surface 记录真实执行者(不 silent 沿用原名)。"""
    class _StubFallback(BaseSurfaceAdapter):
        async def _fetch(self, request, prompt_text):
            return EngineResult(answer="答", provider_key="volcengine_backup",
                                model_key="doubao-seed-fallback", fallback_occurred=True,
                                fallback_detail="primary 5xx → backup endpoint")
    env = await _StubFallback("doubao_ark_api_search").collect(_req("doubao_ark_api_search"))
    assert env.provider_key == "volcengine_backup"     # 真实执行者
    assert env.model_key == "doubao-seed-fallback"


async def test_same_request_id_retry_identifiable_not_new_event():
    r1 = _req("doubao_ark_api_search", "same-id")
    r2 = r1.model_copy(update={"retry_of_request_id": "same-id"})
    e1 = await WrappedResearchAdapter("doubao_ark_api_search", fetch=make_engine_fetch()).collect(r1)
    e2 = await WrappedResearchAdapter("doubao_ark_api_search", fetch=make_engine_fetch()).collect(r2)
    # 业务 request_id 不变(重试不造新业务事件);retry_of_request_id 可识别重试
    assert e1.request_id == e2.request_id == "same-id"
    assert e1.retry_of_request_id is None and e2.retry_of_request_id == "same-id"


async def test_five_response_states_discriminated():
    # answered
    e_ans = await WrappedResearchAdapter("qwen_dashscope_search",
                                         fetch=make_engine_fetch(answer="有内容")).collect(_req("qwen_dashscope_search"))
    assert e_ans.response_status == "answered"
    # unknown(空答,不当 answered、不改 not_mentioned)
    e_unk = await WrappedResearchAdapter("qwen_dashscope_search",
                                         fetch=make_engine_fetch(answer="")).collect(_req("qwen_dashscope_search"))
    assert e_unk.response_status == "unknown"
    # timeout
    e_to = await WrappedResearchAdapter("qwen_dashscope_search",
                                        fetch=make_engine_fetch(raise_exc=asyncio.TimeoutError())
                                        ).collect(_req("qwen_dashscope_search"))
    assert e_to.response_status == "timeout"
    # error
    e_err = await WrappedResearchAdapter("qwen_dashscope_search",
                                         fetch=make_engine_fetch(raise_exc=RuntimeError("boom"))
                                         ).collect(_req("qwen_dashscope_search"))
    assert e_err.response_status == "error"
    # refused(provider content_filter)
    http = make_http_post({"id": "x", "model": "hy3",
                           "choices": [{"message": {"content": "抱歉无法提供"}, "finish_reason": "content_filter"}],
                           "usage": {}})
    e_ref = await OpenAICompatibleAdapter("yuanbao_hy3_tokenhub", http_post=http,
                                          api_key_getter=lambda n: "K").collect(_req("yuanbao_hy3_tokenhub"))
    assert e_ref.response_status == "refused"


async def test_adapter_does_not_judge_brand():
    """adapter 不做品牌判断:envelope 无 mentioned 字段、response_status 只到传输级。"""
    env = await WrappedResearchAdapter("doubao_ark_api_search",
                                       fetch=make_engine_fetch(answer="某某公司很好")).collect(_req("doubao_ark_api_search"))
    assert not hasattr(env, "mentioned")
    # 有内容即 answered,不判定是否提及目标品牌(那是 AI-2 的 target_outcome)
    assert env.response_status == "answered"


async def test_empty_answer_is_unknown_not_error_when_no_exception():
    env = await WrappedResearchAdapter("qwen_dashscope_search",
                                       fetch=make_engine_fetch(answer="   ")).collect(_req("qwen_dashscope_search"))
    assert env.response_status == "unknown"  # 空白答案 → unknown(不伪造空串成功)
