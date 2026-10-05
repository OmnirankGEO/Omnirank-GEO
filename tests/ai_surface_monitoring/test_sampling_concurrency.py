"""采样与并发(§10.3):20 并发不丢不重、429 独立退避、单平台断开、异常复测、预算零额外、WORKERS=4 一致。"""

from __future__ import annotations

import asyncio

import pytest

from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.contracts import CollectionRequest, ObservationEnvelopeV1
from services.ai_surface_monitoring.cost_policy import ConcurrencyGovernor, InMemoryBudgetLedger
from services.ai_surface_monitoring.policy import FakeObservationPolicy, SamplingBudgetV1
from services.ai_surface_monitoring.registry import AdapterRegistry, _build_default_adapters
from services.ai_surface_monitoring.sampling import (
    RepeatCondition,
    SampleSpec,
    build_sample_specs,
    detect_anomaly,
)
from services.ai_surface_monitoring.service import CollectionService
from .conftest import build_service, ingest_policy, make_engine_fetch, make_http_post


def _req(surface: str, rid: str) -> CollectionRequest:
    return CollectionRequest(request_id=rid, source_kind="research", source_ref="s",
                             question_text="q", query_kind="non_branded", surface_key=surface)


async def test_20_concurrent_no_lost_no_dup_request_id():
    adapters = _build_default_adapters()
    adapters["doubao_ark_api_search"] = WrappedResearchAdapter("doubao_ark_api_search", fetch=make_engine_fetch())
    reg = AdapterRegistry(ingest_policy(), adapters=adapters)
    svc = CollectionService(reg, governor=ConcurrencyGovernor(per_platform=8))
    reqs = [_req("doubao_ark_api_search", f"req-{i}") for i in range(20)]
    envs = [e async for e in svc.collect_batch(reqs, round_id="rt")]
    assert len(envs) == 20
    ids = [e.request_id for e in envs]
    assert sorted(ids) == sorted(f"req-{i}" for i in range(20))  # 不丢
    assert len(set(ids)) == 20                                   # 不重


async def test_per_platform_isolation_one_down_others_continue():
    # doubao 永远 429/5xx(引擎抛),qwen 正常 → doubao error,qwen answered,互不拖垮
    adapters = _build_default_adapters()
    adapters["doubao_ark_api_search"] = WrappedResearchAdapter(
        "doubao_ark_api_search", fetch=make_engine_fetch(raise_exc=RuntimeError("429 rate limit")))
    adapters["qwen_dashscope_search"] = WrappedResearchAdapter("qwen_dashscope_search", fetch=make_engine_fetch())
    reg = AdapterRegistry(ingest_policy(), adapters=adapters)
    svc = CollectionService(reg)
    reqs = [_req("doubao_ark_api_search", "d1"), _req("qwen_dashscope_search", "q1"),
            _req("doubao_ark_api_search", "d2"), _req("qwen_dashscope_search", "q2")]
    envs = {e.request_id: e async for e in svc.collect_batch(reqs, round_id="rt")}
    assert envs["d1"].response_status == "error" and envs["d2"].response_status == "error"
    assert envs["q1"].response_status == "answered" and envs["q2"].response_status == "answered"


async def test_per_platform_concurrency_ceiling():
    """每平台并发上限 N:同平台 20 并发,最大在飞 <= N(慢平台不拖垮 · 每平台独立闸)。"""
    in_flight = 0
    max_in_flight = 0

    async def tracking_fetch(pid, prompt):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            return {"answer": "a", "citations": [], "ok": True, "error": None, "raw": {"usage": {}}}
        finally:
            in_flight -= 1

    adapters = _build_default_adapters()
    adapters["doubao_ark_api_search"] = WrappedResearchAdapter("doubao_ark_api_search", fetch=tracking_fetch)
    reg = AdapterRegistry(ingest_policy(), adapters=adapters)
    svc = CollectionService(reg, governor=ConcurrencyGovernor(per_platform=4))
    reqs = [_req("doubao_ark_api_search", f"c-{i}") for i in range(20)]
    envs = [e async for e in svc.collect_batch(reqs, round_id="rt")]
    assert len(envs) == 20
    assert max_in_flight <= 4, f"per-platform concurrency exceeded: {max_in_flight}"
    assert max_in_flight >= 2  # 确有并发(证明闸不是把并发压成 1)


async def test_budget_exhausted_zero_extra_provider_call():
    capture = {}
    # 每日 micros 上限 100k < 单次 doubao 200k → 预留即拒 → budget_blocked,零 provider 调用
    svc, ledger = build_service(
        engine_fetch=make_engine_fetch(capture=capture),
        sampling_budget=SamplingBudgetV1(max_cost_micros_per_day=100_000, max_retry_calls_per_request=0))
    env = await svc.collect_observation(_req("doubao_ark_api_search", "b1"), round_id="rt")
    assert env.response_status == "budget_blocked"
    assert capture == {}  # 零 provider 调用
    assert env.usage.estimated_cost_micros == 0


async def test_model_version_change_triggers_repeat():
    base = _make_env(model_revision="r1")
    changed = _make_env(model_revision="r2")
    decision = detect_anomaly(base, changed)
    assert decision.should_repeat is True
    assert decision.extra_runs == 2  # MODEL_VERSION_CHANGE=3 → 追加 2
    assert "version" in (decision.reason or "")


async def test_answered_refused_flip_triggers_repeat():
    ans = _make_env(response_status="answered")
    ref = _make_env(response_status="refused")
    assert detect_anomaly(ans, ref).should_repeat is True
    # 无变化 → 不复测
    assert detect_anomaly(ans, _make_env(response_status="answered")).should_repeat is False


async def test_search_surface_change_triggers_repeat_via_surface_key():
    # 复审#2 R6-3-R15:search_enabled 由 surface 决定(同 surface 内恒定,adapter 恒发 spec.default_search_enabled)。
    # "真实开搜→关搜"在生产中的表现 = 换到搜索状态不同的 surface,即 surface_key 变 → detect_anomaly 的
    # surface_key 比对触发复测。此处用**真实 adapter 路径**产出两条同属 DeepSeek 平台的记录(而非固定豆包身份/
    # 未知搜索状态的手搓 envelope),先断言其为真实生产形状,再验证复测触发。
    ds_search = "deepseek_dashscope_search_legacy"   # platform=deepseek · default_search_enabled=True
    ds_nosearch = "deepseek_native_no_search"        # platform=deepseek · default_search_enabled=False

    def _dsreq(sk, rid):
        return CollectionRequest(request_id=rid, source_kind="monitoring", source_ref="s",
                                 question_text="上海律师推荐", query_kind="non_branded", surface_key=sk)

    env_on = await WrappedResearchAdapter(ds_search, fetch=make_engine_fetch()).collect(_dsreq(ds_search, "on"))
    ds_http = make_http_post({"id": "d", "model": "deepseek-flash",
                              "choices": [{"message": {"content": "答"}}], "usage": {}})
    env_off = await OpenAICompatibleAdapter(ds_nosearch, http_post=ds_http, api_key_getter=lambda n: "K",
                                            verify_model_echo=False).collect(_dsreq(ds_nosearch, "off"))

    # 真实生产形状:同属 DeepSeek 平台 + 真实搜索状态(真/假),不是"豆包身份 + 未知搜索"的假形状
    assert env_on.platform_key == "deepseek" and env_off.platform_key == "deepseek"
    assert env_on.search_enabled is True                 # 开搜 surface
    assert env_off.search_enabled is False               # 关搜 surface
    assert env_on.surface_key != env_off.surface_key
    # 两者 model_revision 均 None(相等)且状态同为 answered → 唯一差异是 surface_key
    #   → 复测**只**能由 surface_key 比对触发,删该比对即翻红(判别性真实,非 model_revision 兜底)
    assert env_on.model_revision == env_off.model_revision
    assert env_on.response_status == "answered" and env_off.response_status == "answered"

    decision = detect_anomaly(env_on, env_off)           # 真实开搜→关搜
    assert decision.should_repeat is True                # ← 删 surface_key 比对即翻红
    assert decision.extra_runs == 2                      # MODEL_VERSION_CHANGE=3 → 追加 2
    assert "surface" in (decision.reason or "")
    # 同一记录自比(surface/状态一致)→ 不复测
    assert detect_anomaly(env_on, env_on).should_repeat is False


def test_repeat_results_are_independent_specs_not_averaged():
    specs = build_sample_specs(["doubao_ark_api_search"], condition=RepeatCondition.MODEL_VERSION_CHANGE)
    # 3 次独立 run_index,不在采集层平均成一条
    assert specs == [SampleSpec("doubao_ark_api_search", 1),
                     SampleSpec("doubao_ark_api_search", 2),
                     SampleSpec("doubao_ark_api_search", 3)]


def test_new_platform_gold_set_exempts_yuanbao_hy3():
    # 元宝 Hy3 默认通道:new_platform 条件豁免金标准集(老板裁定)
    yb = build_sample_specs(["yuanbao_hy3_tokenhub"], condition=RepeatCondition.NEW_PLATFORM_RESEARCH)
    other = build_sample_specs(["deepseek_native_no_search"], condition=RepeatCondition.NEW_PLATFORM_RESEARCH)
    assert len(yb) == 1        # 豁免:1 次
    assert len(other) == 3     # 其他新平台:金标准集(>=3)


def test_workers4_config_consistency_and_env_clamp(monkeypatch):
    """WORKERS=4:多 worker 从同一发布快照读到一致 policy_version;并发数 env 覆盖 + clamp 生效。

    判别性:显式驱动 env 覆盖(RESEARCH-like)与上界 clamp 路径 —— 若 env 读取/clamp 被破坏则转红。
    """
    from services.ai_surface_monitoring.cost_policy import MAX_CONCURRENCY_PER_PLATFORM
    # 同一发布快照源(模拟 AI-2 发布的一份 policy)→ 多 worker 读到同一 policy_version
    published = ingest_policy()
    versions = {published.get_policy().policy_version for _ in range(4)}
    assert len(versions) == 1

    # env 覆盖:AI_SURFACE_CONCURRENCY_PER_PLATFORM 生效(判别:破坏 env 读取则不等于 12)
    monkeypatch.setenv("AI_SURFACE_CONCURRENCY_PER_PLATFORM", "12")
    assert ConcurrencyGovernor(per_platform=8).per_platform == 12
    # 上界 clamp:超 MAX 被压到 MAX
    monkeypatch.setenv("AI_SURFACE_CONCURRENCY_PER_PLATFORM", str(MAX_CONCURRENCY_PER_PLATFORM + 100))
    assert ConcurrencyGovernor(per_platform=8).per_platform == MAX_CONCURRENCY_PER_PLATFORM
    # 非法 env → 回落传入默认
    monkeypatch.setenv("AI_SURFACE_CONCURRENCY_PER_PLATFORM", "not-a-number")
    assert ConcurrencyGovernor(per_platform=7).per_platform == 7
    # 未设 env → 传入默认
    monkeypatch.delenv("AI_SURFACE_CONCURRENCY_PER_PLATFORM", raising=False)
    assert ConcurrencyGovernor(per_platform=5).per_platform == 5


# ---- helpers ----
def _make_env(*, model_revision=None, response_status="answered") -> ObservationEnvelopeV1:
    from services.ai_surface_monitoring.contracts import utcnow
    return ObservationEnvelopeV1(
        request_id="r", source_kind="research", source_ref="s", question_text="q",
        question_hash="h", query_kind="non_branded", platform_key="doubao", provider_key="volcengine",
        model_key="doubao-seed", model_revision=model_revision, surface_key="doubao_ark_api_search",
        session_mode="clean", prompt_text="q", answer_text="a", answer_hash="h",
        response_status=response_status, observed_at=utcnow(),
    )
