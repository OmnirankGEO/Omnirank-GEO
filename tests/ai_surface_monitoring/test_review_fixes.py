"""Round-1 对抗审核确认项的判别测试(修复回归守卫)。

每个测试对应一条 CONFIRMED finding:删/回退修复即转红。
"""

from __future__ import annotations

import asyncio

import pytest

from services.ai_surface_monitoring.adapters.base import BaseSurfaceAdapter, EngineResult
from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.contracts import CollectionRequest
from services.ai_surface_monitoring.cost_policy import (
    ConcurrencyGovernor,
    InMemoryBudgetLedger,
    budget_charge_micros,
    estimate_usage_cost,
)
from services.ai_surface_monitoring.lineage import get_surface_spec
from services.ai_surface_monitoring.policy import FakeObservationPolicy, SamplingBudgetV1
from services.ai_surface_monitoring.registry import (
    AdapterRegistry,
    OrderPlatformEntitlement,
    _build_default_adapters,
)
from services.ai_surface_monitoring.sampling import detect_anomaly
from services.ai_surface_monitoring.service import CollectionService, SurfaceNotAllowedError
from .conftest import build_service, ingest_policy, make_engine_fetch, make_http_post


def _req(surface, rid="r", source="research"):
    return CollectionRequest(request_id=rid, source_kind=source, source_ref="s",
                             question_text="q", query_kind="non_branded", surface_key=surface)


# ---- Finding 1: hy3-prefixed distinct model must fail-closed ----
# 故被拒的样本相应上移一层:再加词尾的变体仍必须 fail-closed。
# [2026-08-03] expected 由 `hy3-preview` 改回 `hy3`(preview 8/31 下线 + 免费包耗尽 402),
#   于是 `hy3-preview` 从"好模型"变成**必须被拒的词尾变体** —— 这不是放宽,是收紧:
#   preview 与 hy3 是两个模型,回显 preview 却记成 hy3 会把不同模型混进同一时间序列。
#   反向对照仍在下一个用例:精确 `hy3` 与纯版本后缀必须通过。
@pytest.mark.parametrize("bad_model", ["hy3pro", "hy3x", "gpt-4",
                                       "hy3-preview", "hy3-preview-lite", "hy3-lite"])
async def test_hy3_prefixed_distinct_model_fails_closed(bad_model):
    http = make_http_post({"id": "t", "model": bad_model,
                           "choices": [{"message": {"content": "泄露答案"}}], "usage": {}})
    env = await OpenAICompatibleAdapter("yuanbao_hy3_tokenhub", http_post=http,
                                        api_key_getter=lambda n: "K", verify_model_echo=True).collect(
        _req("yuanbao_hy3_tokenhub"))
    assert env.response_status == "error"      # fail-closed,不记为元宝 answered
    assert env.answer_text == ""               # 答案被丢弃


@pytest.mark.parametrize("good_model", ["hy3", "hy3-20260716", "hy3-v2"])
async def test_hy3_exact_and_versioned_pass(good_model):
    http = make_http_post({"id": "t", "model": good_model,
                           "choices": [{"message": {"content": "答"}}], "usage": {}})
    env = await OpenAICompatibleAdapter("yuanbao_hy3_tokenhub", http_post=http,
                                        api_key_getter=lambda n: "K", verify_model_echo=True).collect(
        _req("yuanbao_hy3_tokenhub"))
    assert env.response_status == "answered" and env.model_key == "hy3"


# ---- Finding 3/4/11: non-default active surface gated without entitlement ----
def test_non_default_active_deepseek_legacy_gated_without_entitlement():
    reg = AdapterRegistry(ingest_policy())
    # deepseek 平台在默认矩阵启用,但 dashscope_legacy 是非默认表面 → 无权益不放行
    assert get_surface_spec("deepseek_dashscope_search_legacy").default_enabled is False
    assert reg.is_surface_allowed("deepseek_dashscope_search_legacy") is False
    # 有该 **surface** 订单权益才放行(P1-6:surface 级,非平台级)
    assert reg.is_surface_allowed(
        "deepseek_dashscope_search_legacy",
        OrderPlatformEntitlement({"deepseek_dashscope_search_legacy"})) is True
    # 默认表面无条件放行([2026-07-27] deepseek 的默认表面已换成官方原生搜索那一个)
    assert reg.is_surface_allowed("deepseek_native_with_search") is True


async def test_service_blocks_non_default_surface_no_provider_call():
    capture = {}
    adapters = _build_default_adapters()
    adapters["deepseek_dashscope_search_legacy"] = WrappedResearchAdapter(
        "deepseek_dashscope_search_legacy", fetch=make_engine_fetch(capture=capture))
    reg = AdapterRegistry(ingest_policy(), adapters=adapters)
    svc = CollectionService(reg)
    with pytest.raises(SurfaceNotAllowedError):
        await svc.collect_observation(_req("deepseek_dashscope_search_legacy", "r1"), round_id="rt")
    assert capture == {}  # 零 provider 调用(不 fail-open 成隐性 billable)


# ---- Finding 5: hy3 partial usage -> conservative fallback (never under-book) ----
def test_hy3_partial_usage_is_unknown_and_conservative():
    only_in = estimate_usage_cost("yuanbao_hy3_tokenhub", input_tokens=1000, output_tokens=None)
    assert only_in.estimated_cost_micros is None            # 缺 output → 不精确 → null
    micros, is_fallback = budget_charge_micros(only_in)
    assert micros > 0 and is_fallback is True               # 预算保守回落,不低估
    only_out = estimate_usage_cost("yuanbao_hy3_tokenhub", input_tokens=None, output_tokens=500)
    assert only_out.estimated_cost_micros is None


# ---- Finding 6 (rework): finalize 失败 → 预留保留(继续计入)→ 后续 budget_blocked ----
class _FinalizeFailLedger(InMemoryBudgetLedger):
    def finalize(self, *a, **k):
        raise RuntimeError("simulated finalize failure")  # 不 pop 预留 → 预留继续计入


async def test_finalize_failure_keeps_reservation_counted():
    ledger = _FinalizeFailLedger()
    svc, _ = build_service(sampling_budget=SamplingBudgetV1(
        max_cost_micros_per_day=250_000, max_retry_calls_per_request=0), ledger=ledger)
    # 第一次:doubao 预留 200k → admit;finalize 失败(预留保留)
    e1 = await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    assert e1.response_status == "answered"
    # spent 仍 0(finalize 失败未入账),但预留 200k 仍在 → 第二次 effective=200k+200k>250k → budget_blocked
    e2 = await svc.collect_observation(_req("doubao_ark_api_search", "r2"), round_id="rt")
    assert e2.response_status == "budget_blocked", "finalize 失败未保留预留 → 上限被突破"


# ---- Finding 7 (rework): 原子预留;并发批不越上限(单进程) ----
async def test_concurrent_batch_respects_cap_via_reservation():
    async def slow_fetch(pid, prompt, max_retries=3):
        await asyncio.sleep(0.01)  # 拉长在飞窗口,逼出 check-then-act 竞态(若无原子预留)
        return {"answer": "a", "citations": [], "ok": True, "error": None, "raw": {"usage": {}}}

    # 上限只够 2 次 doubao(200k×2=400k);并发 10 次 → 至多 2 次成功(worst-case=单次 → max_retry=0)
    svc, ledger = build_service(engine_fetch=slow_fetch, per_platform=10,
                               sampling_budget=SamplingBudgetV1(max_cost_micros_per_day=400_000,
                                                                max_retry_calls_per_request=0))
    reqs = [_req("doubao_ark_api_search", f"r{i}") for i in range(10)]
    envs = [e async for e in svc.collect_batch(reqs, round_id="rt")]
    answered = [e for e in envs if e.response_status == "answered"]
    blocked = [e for e in envs if e.response_status == "budget_blocked"]
    assert len(answered) == 2, f"预留失效:成功 {len(answered)} 次(应恰 2),上限被并发突破"
    assert len(blocked) == 8
    assert ledger.spent_micros_today("research") == 400_000  # 恰好用满,未超


async def test_two_service_instances_share_ledger_atomic():
    """复审 P1-3 精确复现(finding 10:并发跑,真正压 threading.Lock 竞态窗口):
    两 service 实例共享同一账本,上限 300k,两次 200k **并发** → 恰一成功一 budget_blocked,总花费 ≤300k。"""
    ledger = InMemoryBudgetLedger()
    policy = ingest_policy(sampling_budget=SamplingBudgetV1(
        max_cost_micros_per_day=300_000, max_retry_calls_per_request=0))

    async def slow_fetch(pid, prompt, max_retries=3):
        await asyncio.sleep(0.02)  # 拉长在飞窗口,使两实例的预留真正重叠
        return {"answer": "a", "citations": [], "ok": True, "error": None, "raw": {"usage": {}}}

    svc_a, _ = build_service(policy=policy, ledger=ledger, engine_fetch=slow_fetch)
    svc_b, _ = build_service(policy=policy, ledger=ledger, engine_fetch=slow_fetch)
    e1, e2 = await asyncio.gather(
        svc_a.collect_observation(_req("doubao_ark_api_search", "a1"), round_id="rt"),
        svc_b.collect_observation(_req("doubao_ark_api_search", "b1"), round_id="rt"))
    statuses = sorted([e1.response_status, e2.response_status])
    assert statuses == ["answered", "budget_blocked"], f"两实例并发各扣满上限被突破: {statuses}"
    assert ledger.spent_micros_today("research") <= 300_000


# ---- Finding 9: concurrency keyed by provider rate-limit domain ----
def test_governor_key_is_provider_domain():
    # 三个 DASHSCOPE 表面共享 provider domain(应共闸);doubao/yuanbao/deepseek-native 各自独立
    assert get_surface_spec("qwen_dashscope_search").provider_key == "dashscope"
    assert get_surface_spec("deepseek_dashscope_search_legacy").provider_key == "dashscope"
    assert get_surface_spec("other_explicit").provider_key == "dashscope"
    assert get_surface_spec("doubao_ark_api_search").provider_key == "volcengine"
    assert get_surface_spec("yuanbao_hy3_tokenhub").provider_key == "tencent_tokenhub"
    assert get_surface_spec("deepseek_native_no_search").provider_key == "deepseek"


# ---- Finding 10: real timeout classified as timeout not error ----
class _FakeTimeout(Exception):
    """模拟 httpx.ReadTimeout(类名含 Timeout)。"""


class _TimeoutNamedError(Exception):
    pass


_FakeTimeout.__name__ = "ReadTimeout"


async def test_httpx_like_timeout_classified_as_timeout():
    async def raise_httpx_timeout(pid, prompt):
        raise _FakeTimeout("read timed out")
    env = await WrappedResearchAdapter("qwen_dashscope_search",
                                       fetch=raise_httpx_timeout).collect(_req("qwen_dashscope_search"))
    assert env.response_status == "timeout"


async def test_wrapped_retry_timeout_message_classified_as_timeout():
    async def raise_wrapped(pid, prompt):
        raise RuntimeError("重试 3 次仍失败: TimeoutError: read timed out")
    env = await WrappedResearchAdapter("qwen_dashscope_search",
                                       fetch=raise_wrapped).collect(_req("qwen_dashscope_search"))
    assert env.response_status == "timeout"


# Round-3: wrapped httpx timeout subclasses(query_with_retry 包裹的最常见生产超时)必须 → timeout
@pytest.mark.parametrize("cls", ["ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"])
async def test_wrapped_httpx_timeout_subclass_classified_as_timeout(cls):
    async def raise_wrapped(pid, prompt):
        raise RuntimeError(f"重试 3 次仍失败: {cls}: The read operation timed out")
    env = await WrappedResearchAdapter("qwen_dashscope_search",
                                       fetch=raise_wrapped).collect(_req("qwen_dashscope_search"))
    assert env.response_status == "timeout", f"包裹 httpx {cls} 被误标(应 timeout)"


def test_is_timeout_case_sensitive_boundary():
    from services.ai_surface_monitoring.adapters.base import _is_timeout
    # 类名大写 Timeout → timeout
    assert _is_timeout(RuntimeError("重试 3 次仍失败: ReadTimeout: x")) is True
    assert _is_timeout(RuntimeError("... TimeoutError: x")) is True
    assert _is_timeout(RuntimeError("... APITimeoutError: x")) is True
    # provider body 小写 timeout 词 → 不误判
    assert _is_timeout(RuntimeError("HTTP 429: rate limited; retry after timeout window")) is False
    assert _is_timeout(RuntimeError("HTTP 400: invalid value for 'timeout' parameter")) is False
    assert _is_timeout(RuntimeError("500 internal server error")) is False


async def test_non_timeout_error_still_error():
    async def raise_other(pid, prompt):
        raise RuntimeError("500 internal server error")
    env = await WrappedResearchAdapter("qwen_dashscope_search",
                                       fetch=raise_other).collect(_req("qwen_dashscope_search"))
    assert env.response_status == "error"


# ---- Round-2 #1: provider error body containing the word 'timeout' must NOT be mislabeled ----
@pytest.mark.parametrize("msg", [
    'HTTP 429: {"error":{"message":"rate limited; retry after timeout window 60s"}}',
    'HTTP 400: {"error":{"message":"invalid value for \'timeout\' parameter"}}',
    'HTTP 500: worker timeout config exceeded',
])
async def test_provider_error_mentioning_timeout_is_error_not_timeout(msg):
    http = make_http_post({}, status=429)  # status>=400 → openai_compat raises RuntimeError(HTTP ...)

    async def raise_msg(pid, prompt):
        raise RuntimeError(msg)
    env = await WrappedResearchAdapter("qwen_dashscope_search",
                                       fetch=raise_msg).collect(_req("qwen_dashscope_search"))
    assert env.response_status == "error", f"provider body 含 'timeout' 词被误标: {msg}"


async def test_openai_http_error_body_with_timeout_word_is_error():
    # openai_compat 把 provider HTTP body 嵌入 RuntimeError 消息;含 'timeout' 词不应误判 timeout
    http = make_http_post({"error": {"message": "rate limited; retry after timeout window"}}, status=429)
    env = await OpenAICompatibleAdapter("deepseek_native_no_search", http_post=http,
                                        api_key_getter=lambda n: "K", verify_model_echo=False).collect(
        _req("deepseek_native_no_search"))
    assert env.response_status == "error"


# ---- Round-2 #2: _build_error_envelope preserves country_code / region_key ----
def test_error_envelope_preserves_geo_attribution():
    from services.ai_surface_monitoring.service import _build_error_envelope
    req = CollectionRequest(request_id="r", source_kind="research", source_ref="s", question_text="q",
                            query_kind="non_branded", surface_key="qwen_dashscope_search",
                            country_code="US", region_key="US-CA")
    env = _build_error_envelope(req, "surface not allowed")
    assert env.country_code == "US" and env.region_key == "US-CA"
    assert env.response_status == "error" and env.usage.estimated_cost_micros == 0


async def test_collect_batch_error_envelope_keeps_geo():
    reg = AdapterRegistry(ingest_policy())
    svc = CollectionService(reg)
    # 未授权 Kimi + 带地域 → 批内返回 error 信封,地域不丢
    req = CollectionRequest(request_id="rk", source_kind="research", source_ref="s", question_text="q",
                            query_kind="non_branded", surface_key="other_explicit",
                            country_code="US", region_key="US-CA")
    envs = [e async for e in svc.collect_batch([req], round_id="rt")]
    assert len(envs) == 1 and envs[0].response_status == "error"
    assert envs[0].country_code == "US" and envs[0].region_key == "US-CA"


# ---- Finding 12: answered<->unknown flip triggers repeat ----
def _env(status, model_rev=None):
    from services.ai_surface_monitoring.contracts import ObservationEnvelopeV1, utcnow
    return ObservationEnvelopeV1(
        request_id="r", source_kind="research", source_ref="s", question_text="q", question_hash="h",
        query_kind="non_branded", platform_key="doubao", provider_key="volcengine", model_key="m",
        model_revision=model_rev, surface_key="doubao_ark_api_search", session_mode="clean",
        prompt_text="q", answer_text="a", answer_hash="h", response_status=status, observed_at=utcnow())


def test_answered_unknown_flip_triggers_repeat():
    assert detect_anomaly(_env("answered"), _env("unknown")).should_repeat is True
    assert detect_anomaly(_env("unknown"), _env("answered")).should_repeat is True
    # 稳定 answered → 不复测
    assert detect_anomaly(_env("answered"), _env("answered")).should_repeat is False


# ---- Finding 2: model-echo mismatch logs trace_id (auditable), no key/answer leaked ----
async def test_model_echo_mismatch_logged_with_trace_id(caplog):
    import logging
    http = make_http_post({"id": "trace-xyz", "model": "gpt-4",
                           "choices": [{"message": {"content": "leaked"}}], "usage": {}})
    with caplog.at_level(logging.WARNING):
        env = await OpenAICompatibleAdapter("yuanbao_hy3_tokenhub", http_post=http,
                                            api_key_getter=lambda n: "SECRETKEY", verify_model_echo=True).collect(
            _req("yuanbao_hy3_tokenhub"))
    assert env.response_status == "error"
    text = caplog.text
    assert "trace-xyz" in text and "mismatch" in text.lower()
    assert "SECRETKEY" not in text and "leaked" not in text  # 绝不泄露 key / answer 正文
