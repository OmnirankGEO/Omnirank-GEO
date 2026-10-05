"""复审 NO-GO 返工的判别测试(P1-1,P1-2,P1-4,P1-5,P2-8)。删/回退修复即转红。"""

from __future__ import annotations

import time

import pytest

from services.ai_surface_monitoring.adapters.base import BaseSurfaceAdapter, EngineResult
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.contracts import CollectionRequest
from services.ai_surface_monitoring.cost_policy import RpmLimiter
from services.ai_surface_monitoring.policy import (
    ObservationFeatureFlagsV1,
    PlatformPolicyV1,
    SamplingBudgetV1,
)
from services.ai_surface_monitoring.registry import AdapterRegistry, OrderPlatformEntitlement
from services.ai_surface_monitoring import service as service_mod
from services.ai_surface_monitoring.service import (
    CollectionService,
    IngestDisabledError,
    NotConfiguredError,
    PaidDeliveryContextError,
    PolicyNotReadyError,
    build_default_service,
)
from .conftest import build_service, ingest_policy, make_engine_fetch, make_http_post


def _req(surface, rid="r", source="research"):
    return CollectionRequest(request_id=rid, source_kind=source, source_ref="s",
                             question_text="q", query_kind="non_branded", surface_key=surface)


# ---- P1-1: policy 指定的 surface_key 精确路由(deepseek 配 legacy → 选 legacy,不选 native) ----
def _deepseek_legacy_platforms():
    # 契约 check④:全目录合计恰 10000(2 启用平台 7500+2500)
    return [
        PlatformPolicyV1(platform_key="doubao", enabled=True, base_weight_bps=7500,
                         surface_key="doubao_ark_api_search"),
        PlatformPolicyV1(platform_key="deepseek", enabled=True, base_weight_bps=2500,
                         surface_key="deepseek_dashscope_search_legacy"),  # 策略选 legacy
        PlatformPolicyV1(platform_key="kimi", enabled=False, base_weight_bps=0, legacy_read_only=True),
    ]


def test_policy_surface_key_drives_selection_not_code_default():
    policy = ingest_policy(platforms=_deepseek_legacy_platforms())
    reg = AdapterRegistry(policy)
    # 策略把 deepseek 配成 legacy → 默认采样选 legacy,不是代码默认的 native
    surfaces = reg.default_sampling_surfaces()
    assert "deepseek_dashscope_search_legacy" in surfaces
    assert "deepseek_native_no_search" not in surfaces
    # 权益闸:策略选定的 legacy 放行;未选定的 native 反而需权益
    assert reg.is_surface_allowed("deepseek_dashscope_search_legacy") is True
    assert reg.is_surface_allowed("deepseek_native_no_search") is False


def test_matrix_marks_only_policy_chosen_surface_enabled():
    policy = ingest_policy(platforms=_deepseek_legacy_platforms())
    reg = AdapterRegistry(policy)
    m = {r.surface_key: r.enabled for r in reg.get_surface_matrix().rows}
    # 只有策略选定的那个 deepseek 表面 enabled;其它 deepseek 表面 False(不再全标启用)
    assert m["deepseek_dashscope_search_legacy"] is True
    assert m["deepseek_native_no_search"] is False
    assert m["deepseek_native_with_search"] is False
    assert m["deepseek_metaso_proxy"] is False


# ---- P1-2: ingest 总闸 + 调用上限 + fail-closed 默认 ----
async def test_ingest_disabled_blocks_collection_zero_call():
    capture = {}
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture),
                           policy=ingest_policy(ingest=False))
    with pytest.raises(IngestDisabledError):
        await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    assert capture == {}  # 零 provider 调用


async def test_ingest_disabled_batch_returns_error_envelope():
    svc, _ = build_service(policy=ingest_policy(ingest=False))
    envs = [e async for e in svc.collect_batch([_req("doubao_ark_api_search", "r1")], round_id="rt")]
    assert len(envs) == 1 and envs[0].response_status == "error"


async def test_ingest_disabled_does_not_cancel_paid_diagnosis_delivery():
    capture = {}
    http = make_http_post({
        "id": "yuanbao-paid-delivery",
        "model": "hy3",
        "choices": [{"message": {"content": "有效回答"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }, capture=capture)
    svc, ledger = build_service(
        yuanbao_http=http,
        policy=ingest_policy(ingest=False),
    )
    request = _req("yuanbao_hy3_tokenhub", "paid-1", source="paid_diagnosis")

    # 普通观测入口仍被总闸阻断；只有显式已售诊断履约入口可继续。
    with pytest.raises(IngestDisabledError):
        await svc.collect_observation(request, round_id="diagnosis:1")
    env = await svc.collect_paid_delivery(request, round_id="diagnosis:1")

    assert env.response_status == "answered"
    assert env.answer_text == "有效回答"
    assert capture["body"]["model"] == "hy3"  # [2026-08-03] preview 8/31 下线,改回 hy3(实测 3/3 真检索)
    assert ledger.calls_today("paid_diagnosis") == 1


async def test_paid_delivery_bypass_rejects_research_and_monitoring():
    capture = {}
    svc, _ = build_service(
        engine_fetch=make_engine_fetch(capture=capture),
        policy=ingest_policy(ingest=False),
    )
    for source_kind in ("research", "monitoring"):
        with pytest.raises(PaidDeliveryContextError):
            await svc.collect_paid_delivery(
                _req("doubao_ark_api_search", source=source_kind),
                round_id=f"{source_kind}:1",
            )
    assert capture == {}


async def test_paid_delivery_keeps_policy_readiness_fail_closed(monkeypatch):
    capture = {}
    svc, _ = build_service(
        yuanbao_http=make_http_post({
            "model": "hy3",
            "choices": [{"message": {"content": "不应被调用"}}],
        }, capture=capture),
        policy=ingest_policy(ingest=False),
    )
    monkeypatch.setattr(
        svc._registry,
        "readiness",
        lambda _snapshot=None: {
            "ready": False,
            "status": "blocked",
            "problems": ["simulated unavailable surface"],
            "substitutions": [],
        },
    )

    with pytest.raises(PolicyNotReadyError):
        await svc.collect_paid_delivery(
            _req("yuanbao_hy3_tokenhub", source="paid_diagnosis"),
            round_id="diagnosis:blocked",
        )
    assert capture == {}


async def test_paid_delivery_keeps_shared_call_budget():
    capture = {}
    svc, ledger = build_service(
        yuanbao_http=make_http_post({
            "model": "hy3",
            "choices": [{"message": {"content": "有效回答"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        }, capture=capture),
        policy=ingest_policy(
            ingest=False,
            sampling_budget=SamplingBudgetV1(
                max_calls_per_day=1,
                max_cost_micros_per_day=10_000_000_000,
                max_calls_per_round=1,
                max_retry_calls_per_request=0,
            ),
        ),
    )
    first = await svc.collect_paid_delivery(
        _req("yuanbao_hy3_tokenhub", "paid-budget-1", source="paid_diagnosis"),
        round_id="diagnosis:budget-1",
    )
    second = await svc.collect_paid_delivery(
        _req("yuanbao_hy3_tokenhub", "paid-budget-2", source="paid_diagnosis"),
        round_id="diagnosis:budget-2",
    )

    assert first.response_status == "answered"
    assert second.response_status == "budget_blocked"
    assert len(capture["bodies"]) == 1
    assert ledger.calls_today("paid_diagnosis") == 1


async def test_max_calls_per_day_cap_enforced():
    # max_retry=0 → 每次预留 1 call;上限 2 → 2 成功,第 3 blocked
    svc, ledger = build_service(sampling_budget=SamplingBudgetV1(
        max_calls_per_day=2, max_cost_micros_per_day=10_000_000_000, max_retry_calls_per_request=0))
    e1 = await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    e2 = await svc.collect_observation(_req("doubao_ark_api_search", "r2"), round_id="rt")
    e3 = await svc.collect_observation(_req("doubao_ark_api_search", "r3"), round_id="rt")
    assert [e1.response_status, e2.response_status] == ["answered", "answered"]
    assert e3.response_status == "budget_blocked"  # 第 3 次超调用上限
    assert ledger.calls_today("research") == 2


def test_build_default_service_requires_real_deps():
    pol = ingest_policy()
    from services.ai_surface_monitoring.cost_policy import InMemoryBudgetLedger
    with pytest.raises(NotConfiguredError):
        build_default_service(pol, None)      # 缺账本
    with pytest.raises(NotConfiguredError):
        build_default_service(None, InMemoryBudgetLedger())  # 缺 policy
    # 齐全 → OK
    svc = build_default_service(pol, InMemoryBudgetLedger())
    assert isinstance(svc, CollectionService)


async def test_module_level_functions_fail_closed_until_configured():
    service_mod._default_service = None  # 复位:未接线
    with pytest.raises(NotConfiguredError):
        await service_mod.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")


# ---- P1-4: 真实重试计费 + policy 重试上限透传 ----
async def test_retry_attempts_are_billed():
    # 引擎报告 3 次真实调用(含重试)→ doubao flat 200k × 3 = 600k
    svc, ledger = build_service(engine_fetch=make_engine_fetch(attempts=3))
    env = await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    assert env.usage.estimated_cost_micros == 600_000
    assert ledger.spent_micros_today("research") == 600_000


async def test_policy_retry_cap_threaded_to_adapter():
    capture = {}
    svc, _ = build_service(
        engine_fetch=make_engine_fetch(capture=capture),
        sampling_budget=SamplingBudgetV1(max_retry_calls_per_request=5))
    await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    # max_retries = 1 首次 + 5 重试 = 6(policy 上限透传给包裹器)
    assert capture["max_retries"] == 6


# ---- P2-8: RPM 真实限速(不只是并发) ----
async def test_rpm_limiter_paces_calls():
    limiter = RpmLimiter(rpm_by_domain={"volcengine": 6000})  # interval = 10ms
    started = time.monotonic()
    for _ in range(4):
        await limiter.acquire("volcengine")
    elapsed = time.monotonic() - started
    assert elapsed >= 0.028, f"RPM 限速未生效(elapsed={elapsed:.3f}s,应 ≥ 3×10ms)"


async def test_rpm_unlimited_when_not_configured():
    limiter = RpmLimiter()  # 默认不限
    started = time.monotonic()
    for _ in range(50):
        await limiter.acquire("volcengine")
    assert time.monotonic() - started < 0.05  # 不限 → 几乎不耗时


# ---- P1-5: provider 畸形 citation 不让信封构建抛异常(付费调用不丢账) ----
async def test_malformed_citations_do_not_raise_and_cost_recorded():
    class _StubAdapter(BaseSurfaceAdapter):
        def __init__(self, result):
            super().__init__("doubao_ark_api_search")
            self._result = result

        async def _fetch(self, request, prompt_text):
            return self._result

    bad_citations = [
        {"url": 12345},                          # url 非 str
        {"title": "无 url"},                     # 缺 url
        None,                                     # 非 dict
        {"url": "http://ok.com", "title": "OK", "rank": "notint", "is_answer_cited": True},  # rank 非 int
    ]
    env = await _StubAdapter(EngineResult(answer="答", citations=bad_citations)).collect(
        _req("doubao_ark_api_search", "r1"))
    # 不抛异常;只保留可用的一条(rank 非法 → None 但仍收);成本正常算(doubao flat 200k)
    assert env.response_status == "answered"
    assert len(env.citations) == 1 and env.citations[0].url == "http://ok.com"
    assert env.citations[0].rank is None
    assert env.usage.estimated_cost_micros == 200_000


async def test_malformed_citations_via_service_still_charges():
    async def bad_fetch(pid, prompt, max_retries=3):
        return {"answer": "答", "citations": [{"url": 999}, {"url": "http://x.com", "is_answer_cited": True}],
                "ok": True, "error": None, "raw": {"usage": {}}}
    svc, ledger = build_service(engine_fetch=bad_fetch)
    env = await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    assert env.response_status == "answered"        # 畸形 citation 未致信封构建失败
    assert ledger.spent_micros_today("research") == 200_000  # 付费调用已入账(不丢账)


# ============================================================================
# 复审 rework-review 确认项(orphan/sentinel/hard-cap/failed-retry/validator/reaper)
# ============================================================================

# ---- rework #1/#4/#6: 孤儿预留超 TTL 不再计入 + reap 回收为 charge(自愈,防饿死) ----
def test_orphan_reservation_older_than_ttl_stops_counting():
    from services.ai_surface_monitoring.cost_policy import (
        InMemoryBudgetLedger, Reservation, RESERVATION_TTL_SECONDS)
    ledger = InMemoryBudgetLedger()
    # 注入一个巨额但超 TTL 的孤儿(模拟崩溃 worker 留下的预留)
    ledger._reservations["orphan"] = Reservation(
        "orphan", "research", "doubao_ark_api_search", 100_000_000, 1, "r0",
        time.monotonic() - RESERVATION_TTL_SECONDS - 1)
    assert ledger.spent_micros_today("research") == 0  # 真实花费为 0
    # 孤儿超窗不计入 → 新预留仍可准入(不被永久饿死)
    tok = ledger.try_reserve("research", 200_000, 1, surface_key="doubao_ark_api_search",
                             limit_micros=300_000, limit_calls=None, request_id="new")
    assert tok is not None and tok != "__already_charged__"


def test_reap_converts_orphan_to_conservative_charge():
    from services.ai_surface_monitoring.cost_policy import InMemoryBudgetLedger
    ledger = InMemoryBudgetLedger()
    tok = ledger.try_reserve("research", 200_000, 1, surface_key="doubao_ark_api_search",
                             limit_micros=None, limit_calls=None, request_id="r1")
    assert tok is not None
    reaped = ledger.reap_stale_reservations(ttl_seconds=0)  # 立即回收
    assert reaped == 1
    assert ledger.spent_micros_today("research") == 200_000  # 保守按预留额入账(不漏)
    assert ledger.calls_today("research") == 1


# ---- rework #2/#5: 重复 request_id 不再发第二次 provider 调用(哨兵短路) ----
async def test_duplicate_request_id_no_second_provider_call():
    capture = {}
    svc, ledger = build_service(engine_fetch=make_engine_fetch(capture=capture))
    e1 = await svc.collect_observation(_req("doubao_ark_api_search", "SAME"), round_id="rt")
    e2 = await svc.collect_observation(_req("doubao_ark_api_search", "SAME"), round_id="rt")  # 同 request_id 重放
    assert e1.response_status == "answered"
    assert e2.response_status == "budget_blocked"       # 去重:不重复采集/计费
    assert len(capture["prompts"]) == 1                  # provider 只被调用一次
    assert ledger.spent_micros_today("research") == 200_000  # 只计一次费


# ---- rework #3: worst-case 预留 → 硬上限,预留含重试余量 ----
async def test_hard_cap_reserves_retry_headroom():
    capture = {}
    # cap 500k;max_retry=2 → worst-case = 200k×3 = 600k > 500k → 单次 doubao 即被硬挡
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture),
                           sampling_budget=SamplingBudgetV1(max_cost_micros_per_day=500_000,
                                                            max_retry_calls_per_request=2))
    env = await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    assert env.response_status == "budget_blocked"  # 硬上限:重试余量放不下即拒
    assert capture == {}                             # 零 provider 调用


# ---- rework #7/#8: 失败(重试耗尽)按真实 attempts 计费,不当 1 次 ----
async def test_failed_all_retries_billed_by_attempts():
    # query_with_retry 重试耗尽 → 抛 "重试 N 次仍失败"(= N 次真实 provider 调用)
    svc, ledger = build_service(
        engine_fetch=make_engine_fetch(raise_exc=RuntimeError("重试 3 次仍失败: RuntimeError: boom")),
        sampling_budget=SamplingBudgetV1(max_cost_micros_per_day=10_000_000, max_retry_calls_per_request=2))
    env = await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    assert env.response_status == "error"
    assert env.usage.estimated_cost_micros == 600_000       # doubao 200k × 3(失败也按真实调用数计,不漏)
    assert ledger.spent_micros_today("research") == 600_000


# ---- convergence finding: 非重试类失败(ValueError 等)只 1 次调用,不按 worst-case 多算 ----
async def test_non_retryable_failure_billed_single_call():
    # query_with_retry 对 ValueError/JSONDecodeError 不重试,单次即抛 → attempts=1
    svc, ledger = build_service(
        engine_fetch=make_engine_fetch(raise_exc=ValueError("malformed 200 body - not retried")),
        sampling_budget=SamplingBudgetV1(max_cost_micros_per_day=10_000_000, max_retry_calls_per_request=2))
    env = await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    assert env.response_status == "error"
    assert env.usage.estimated_cost_micros == 200_000       # 单次,不当 3 次多算(不虚高预算)
    assert ledger.spent_micros_today("research") == 200_000


# ---- rework #9: enabled 平台必须指定 surface_key(否则策略加载即报错) ----
def test_enabled_platform_requires_surface_key():
    with pytest.raises(Exception):
        PlatformPolicyV1(platform_key="deepseek", enabled=True, base_weight_bps=2500)  # 缺 surface_key
    # 指定即 OK
    ok = PlatformPolicyV1(platform_key="deepseek", enabled=True, base_weight_bps=2500,
                          surface_key="deepseek_native_no_search")
    assert ok.surface_key == "deepseek_native_no_search"
    # legacy 只读行(Kimi)允许 surface_key=None
    legacy = PlatformPolicyV1(platform_key="kimi", enabled=False, base_weight_bps=0, legacy_read_only=True)
    assert legacy.surface_key is None


# ---- reaper 接线:reconciler tick 调用注入的回收器 ----
async def test_reconciler_calls_injected_reaper():
    from services.ai_surface_monitoring import scheduler_wiring as sw
    calls = {"n": 0}

    def reaper():
        calls["n"] += 1
        return 2
    sw.set_reservation_reaper(reaper)
    try:
        out = await sw._job_reconciler()
        assert calls["n"] == 1 and out.get("reservations_reaped") == 2
    finally:
        sw.set_reservation_reaper(None)
