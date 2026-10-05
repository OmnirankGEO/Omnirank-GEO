"""复审 NO-GO#2 的判别测试(P1-1..P1-8 + P2)。删/回退修复即转红。"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from services.ai_surface_monitoring import scheduler_wiring as sw
from services.ai_surface_monitoring.contracts import CollectionRequest
from services.ai_surface_monitoring.cost_policy import InMemoryBudgetLedger, RpmLimiter
from services.ai_surface_monitoring.lineage import get_surface_spec, resolve_runtime_surface
from services.ai_surface_monitoring.policy import (
    FakeObservationPolicy,
    ObservationFeatureFlagsV1,
    ObservationPolicySnapshot,
    PlatformPolicyV1,
    SamplingBudgetV1,
    SourceBaseWeightsV1,
)
from services.ai_surface_monitoring.registry import AdapterRegistry, OrderPlatformEntitlement
from services.ai_surface_monitoring.sampling import RepeatCondition, build_sample_specs, cap_round_calls
from .conftest import build_service, ingest_policy, make_engine_fetch


def _req(surface, rid="r", source="research"):
    return CollectionRequest(request_id=rid, source_kind=source, source_ref="s",
                             question_text="q", query_kind="non_branded", surface_key=surface)


# ---- P1-1: 同 request_id 20 并发 → 恰一 provider 调用、恰一账 ----
async def test_same_request_id_concurrent_exactly_one_call():
    calls = {"n": 0}

    async def slow(pid, prompt, max_retries=3):
        calls["n"] += 1
        await asyncio.sleep(0.02)  # 拉长在飞窗口,逼并发同 ID 竞态
        return {"answer": "a", "citations": [], "ok": True, "error": None, "raw": {"usage": {}}}

    svc, ledger = build_service(engine_fetch=slow)
    reqs = [_req("doubao_ark_api_search", "SAME") for _ in range(20)]
    envs = await asyncio.gather(*[svc.collect_observation(r, round_id="rt") for r in reqs])
    answered = [e for e in envs if e.response_status == "answered"]
    assert calls["n"] == 1, f"同 request_id 并发发了 {calls['n']} 次 provider 调用(应恰 1)"
    assert len(answered) == 1
    assert ledger.spent_micros_today("research") == 200_000  # 恰一笔账


# ---- P1-2: 请求正文的 max_retry_calls 被钳到 policy(不得绕过预算) ----
async def test_request_retry_clamped_to_policy():
    capture = {}
    # policy 禁止重试(0);请求却填 5 → 钳到 0 → max_retries=1 首次,成本=单次(不 ×6)
    svc, ledger = build_service(engine_fetch=make_engine_fetch(capture=capture),
                               sampling_budget=SamplingBudgetV1(max_retry_calls_per_request=0))
    req = _req("doubao_ark_api_search", "r1").model_copy(update={"max_retry_calls": 5})
    env = await svc.collect_observation(req, round_id="rt")
    assert capture["max_retries"] == 1                      # 1 首次 + 0 重试(policy 钳制)
    assert env.usage.estimated_cost_micros == 200_000       # 单次,不因请求填 5 而 ×6
    assert ledger.spent_micros_today("research") == 200_000


# ---- P1-3: 调用上限按真实 provider attempts 计 + max_calls_per_round 被消费 ----
async def test_calls_today_counts_provider_attempts():
    svc, ledger = build_service(engine_fetch=make_engine_fetch(attempts=3),
                               sampling_budget=SamplingBudgetV1(max_cost_micros_per_day=10_000_000_000))
    await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id="rt")
    assert ledger.calls_today("research") == 3   # 一次请求重试 3 次 = 3 次 provider 调用(非 1)


def test_max_calls_per_round_capped_not_silent(caplog):
    import logging
    specs = build_sample_specs(["doubao_ark_api_search"], condition=RepeatCondition.MODEL_VERSION_CHANGE)  # 3 条
    with caplog.at_level(logging.WARNING):
        capped = cap_round_calls(specs, max_calls_per_round=2)
    assert len(capped) == 2                       # 截断到 2
    assert "截断丢弃" in caplog.text               # 非静默:丢弃有日志
    # 不限时原样
    assert cap_round_calls(specs, None) == specs


# ---- 复审#2-R6 P1-1:max_calls_per_round 进入**真实执行链**,按真实 provider attempts 硬限单轮 ----
async def test_round_cap_bounds_provider_attempts_in_real_service():
    """两个请求,每个真实 attempts=3;单轮 cap=3。经真实 collect_batch(共享 round_id):
    只 1 个请求被准入(3 attempts),第二个 budget_blocked → 单轮 provider 调用总数 = 3(**不是 6**)。
    回退(不消费 round cap)→ 两个都跑 → provider 调用 2 次 / 单轮 6 → 断言翻红。"""
    capture: dict = {}
    svc, ledger = build_service(
        engine_fetch=make_engine_fetch(attempts=3, capture=capture),
        sampling_budget=SamplingBudgetV1(
            max_calls_per_round=3, max_retry_calls_per_request=2,   # worst-case=3/请求
            max_calls_per_day=10_000, max_cost_micros_per_day=10_000_000_000))
    reqs = [_req("doubao_ark_api_search", "r1"), _req("doubao_ark_api_search", "r2")]
    envs = [e async for e in svc.collect_batch(reqs, round_id="round-A")]
    answered = [e for e in envs if e.response_status == "answered"]
    blocked = [e for e in envs if e.response_status == "budget_blocked"]
    # 只 1 个请求真发 provider(fetch 只被调 1 次),另一个被单轮 cap 挡下
    assert len(capture.get("prompts", [])) == 1, \
        f"单轮 provider 调用应恰 1 个请求(fetch 1 次),实为 {len(capture.get('prompts', []))}"
    assert len(answered) == 1 and len(blocked) == 1
    # 单轮真实 attempts 入账 = 3(= cap),绝不 6
    assert ledger.calls_in_round("round-A") == 3, \
        f"单轮 attempts 应 = cap 3,实为 {ledger.calls_in_round('round-A')}(未消费 round cap 会 = 6)"


@pytest.mark.parametrize("bad", [0, -1])
def test_max_calls_per_round_must_be_positive_per_contract(bad):
    """复审#2-R6-2:机器契约 max_calls_per_round constraint '>0' → policy 构造期即拒 <=0
    (不接受 0/负=禁用;既然恒启用,采集必带稳定 round_id,见 service 层测试)。"""
    with pytest.raises(Exception):
        SamplingBudgetV1(max_calls_per_round=bad)


@pytest.mark.parametrize("kwargs", [
    {"max_calls_per_day": 0}, {"max_calls_per_day": -3},
    {"max_cost_micros_per_day": 0}, {"max_cost_micros_per_day": -5},
    {"max_retry_calls_per_request": -1},
])
def test_sampling_budget_enforces_all_contract_constraints(kwargs):
    """复审#2-R6-3-R2:机器契约 sampling_budget_fields **四字段全部**落约束(此前只查 round → 其余三个
    畸形 0/负被静默接受,daily 上限按字面比较使 try_reserve 恒 None → 全站静默停采)。构造期即拒。"""
    with pytest.raises(Exception):
        SamplingBudgetV1(**kwargs)
    # 合法边界:max_retry_calls_per_request=0(契约 >=0)可接受
    assert SamplingBudgetV1(max_retry_calls_per_request=0).max_retry_calls_per_request == 0


@pytest.mark.parametrize("kwargs", [{"research": -5}, {"research": 10001},
                                    {"paid_diagnosis": -1}, {"monitoring": 10001}])
def test_source_base_weights_enforce_0_10000_contract(kwargs):
    """复审#2-R6-3-R3:机器契约 source_base_weight_fields 各 '0..10000' → 构造期约束(与 sampling_budget 平齐)。"""
    from services.ai_surface_monitoring.policy import SourceBaseWeightsV1
    with pytest.raises(Exception):
        SourceBaseWeightsV1(**kwargs)
    # 边界合法(0 与 10000 都可)
    assert SourceBaseWeightsV1(research=0).research == 0
    assert SourceBaseWeightsV1(monitoring=10000).monitoring == 10000


@pytest.mark.parametrize("bad", [-1, 10001])
def test_platform_base_weight_field_constraint(bad):
    """复审#2-R6-3-R3:PlatformPolicyV1.base_weight_bps 字段级 '0..10000' 约束(与 budget/source_weights 平齐)。"""
    with pytest.raises(Exception):
        PlatformPolicyV1(platform_key="x", enabled=True, base_weight_bps=bad, surface_key="doubao_ark_api_search")


def test_renormalize_to_10000_deterministic_contract_check5():
    """机器契约 policy_schema.checks⑤:运行时权益过滤后**确定性重归一到 10000**。AI-1 提供确定性
    helper(最大余数补足,合计恰 10000,与 dict 顺序无关),供权重消费层(聚合)使用。"""
    from services.ai_surface_monitoring.policy import renormalize_to_10000
    r = renormalize_to_10000({"doubao": 3500, "qwen": 2500, "deepseek": 2500})  # 过滤到 3 平台
    assert sum(r.values()) == 10000
    assert renormalize_to_10000({"deepseek": 2500, "doubao": 3500, "qwen": 2500}) == r  # 顺序无关确定性
    assert renormalize_to_10000({}) == {} and renormalize_to_10000({"a": 0}) == {}      # 空/全 0 → 空


@pytest.mark.parametrize("bad", [0, -1])
def test_ledger_round_limit_nonpositive_is_unlimited_defensive(bad):
    """防御(复审#2-R6-R2 保留):即便 <=0 绕过 policy 直达账本,try_reserve 也把 <=0 当**不限**
    (与 cap_round_calls / RpmLimiter 的 <=0 约定一致),绝不把 0 当"cap=0 全阻断"。"""
    from services.ai_surface_monitoring.cost_policy import ALREADY_CHARGED_TOKEN, InMemoryBudgetLedger
    led = InMemoryBudgetLedger()
    tok = led.try_reserve("research", 200_000, 1, surface_key="doubao_ark_api_search",
                          limit_micros=None, limit_calls=None, request_id="x",
                          round_id="R", limit_round_calls=bad)
    assert tok is not None and tok != ALREADY_CHARGED_TOKEN   # <=0 → 不限 → 准入(非阻断)


async def test_round_cap_isolated_per_round_id():
    """不同 round_id 各自独立计 cap:同一 cap=1 下,两个不同轮各准入 1 个请求(互不挤占)。"""
    capture: dict = {}
    svc, ledger = build_service(
        engine_fetch=make_engine_fetch(attempts=1, capture=capture),
        sampling_budget=SamplingBudgetV1(
            max_calls_per_round=1, max_retry_calls_per_request=0,
            max_calls_per_day=10_000, max_cost_micros_per_day=10_000_000_000))
    e1 = await svc.collect_observation(_req("doubao_ark_api_search", "a1"), round_id="R1")
    e2 = await svc.collect_observation(_req("doubao_ark_api_search", "a2"), round_id="R2")
    assert e1.response_status == "answered" and e2.response_status == "answered"
    assert ledger.calls_in_round("R1") == 1 and ledger.calls_in_round("R2") == 1


# ---- 复审#2-R6-2 P1-2:单轮上限恒启用(契约>0)→ 稳定 round_id 强制,缺失 fail-closed,不自动生成 ----
async def test_round_id_mandatory_when_cap_enabled_fail_closed():
    from services.ai_surface_monitoring.service import RoundContextRequiredError
    capture: dict = {}
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture))  # 默认 cap 5000(启用)
    # collect_observation 缺 round_id → fail-closed
    with pytest.raises(RoundContextRequiredError):
        await svc.collect_observation(_req("doubao_ark_api_search", "r1"))
    # collect_batch 缺 round_id → fail-closed(**不自动生成随机 id**)
    with pytest.raises(RoundContextRequiredError):
        async for _ in svc.collect_batch([_req("doubao_ark_api_search", "r2")]):
            pass
    assert capture.get("prompts", []) == []  # 零 provider 调用


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_blank_round_id_is_fail_closed(blank):
    """复审#2-R6-2 收敛:**空白** round_id 视为缺失 → fail-closed(防空串既绕过强制、又把不同逻辑轮
    塌缩到同一 '' 账本桶)。此前闸只判 `round_id is None`,'' 放行。"""
    from services.ai_surface_monitoring.service import RoundContextRequiredError
    capture: dict = {}
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture))  # 默认 cap 启用
    with pytest.raises(RoundContextRequiredError):
        await svc.collect_observation(_req("doubao_ark_api_search", "r1"), round_id=blank)
    with pytest.raises(RoundContextRequiredError):
        async for _ in svc.collect_batch([_req("doubao_ark_api_search", "r2")], round_id=blank):
            pass
    assert capture.get("prompts", []) == []


async def test_round_cap_shared_across_workers_same_round_id():
    """四进程共享真实账本判别(内存账本模拟跨进程共享点,与每日预算跨 worker 测试同法):
    两个 service 实例(=两 worker)共享同一 ledger + 同一 round_id + cap=1 → 全局只 1 个请求真发 provider。"""
    from services.ai_surface_monitoring.cost_policy import InMemoryBudgetLedger
    shared = InMemoryBudgetLedger()
    ca: dict = {}
    cb: dict = {}
    budget = SamplingBudgetV1(max_calls_per_round=1, max_retry_calls_per_request=0,
                              max_calls_per_day=10_000, max_cost_micros_per_day=10_000_000_000)
    svc_a, _ = build_service(engine_fetch=make_engine_fetch(attempts=1, capture=ca),
                             ledger=shared, sampling_budget=budget)
    svc_b, _ = build_service(engine_fetch=make_engine_fetch(attempts=1, capture=cb),
                             ledger=shared, sampling_budget=budget)
    ea, eb = await asyncio.gather(
        svc_a.collect_observation(_req("doubao_ark_api_search", "wa"), round_id="ROUND-1"),
        svc_b.collect_observation(_req("doubao_ark_api_search", "wb"), round_id="ROUND-1"))
    total_fetches = len(ca.get("prompts", [])) + len(cb.get("prompts", []))
    assert total_fetches == 1, f"跨 worker 同 round_id cap=1 应恰 1 provider 调用,实为 {total_fetches}"
    assert {ea.response_status, eb.response_status} == {"answered", "budget_blocked"}
    assert shared.calls_in_round("ROUND-1") == 1


# ---- 复审#2-R6-2 P1-3:policy 非 ready(需替换)→ live 拒绝开闸(即便请求好平台;闸是整体) ----
async def test_live_collection_refused_when_policy_not_ready():
    from services.ai_surface_monitoring.service import PolicyNotReadyError
    capture: dict = {}
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture),
                           policy=ingest_policy(platforms=_ai2_legacy_native_with_search_platforms()))
    # readiness=attention_required → 请求替换目标 legacy 被拒
    with pytest.raises(PolicyNotReadyError):
        await svc.collect_observation(_req("deepseek_dashscope_search_legacy", "r1"), round_id="R")
    # 连好平台(doubao)也拒 —— 非 ready 时整轮开闸被拒(强制 AI-2 先修 policy)
    with pytest.raises(PolicyNotReadyError):
        await svc.collect_observation(_req("doubao_ark_api_search", "r2"), round_id="R")
    # collect_batch 同样上前置拒绝
    with pytest.raises(PolicyNotReadyError):
        async for _ in svc.collect_batch([_req("doubao_ark_api_search", "r3")], round_id="R"):
            pass
    assert capture.get("prompts", []) == []  # 零 provider 调用


async def test_order_collection_independent_of_default_policy_readiness():
    """P1-1×P1-3:订单按**不可变快照独立执行**,不受当前默认 policy readiness 牵连。
    默认 policy attention_required(deepseek 写 native_with_search),但订单**显式买了** legacy →
    带权益采集正常 answered(显式购买非替换),不被默认轮 readiness 闸阻断(否则违反 P1-1 订单独立)。"""
    capture: dict = {}
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture),
                           policy=ingest_policy(platforms=_ai2_legacy_native_with_search_platforms()))
    ent = OrderPlatformEntitlement({"deepseek_dashscope_search_legacy"})
    env = await svc.collect_observation(
        _req("deepseek_dashscope_search_legacy", "ord1"), order_entitlement=ent, round_id="R")
    assert env.response_status == "answered"
    assert len(capture.get("prompts", [])) == 1


async def test_empty_or_unrelated_entitlement_cannot_collect_default_surface():
    """复审#2-R6-3 P1-1:传 entitlement = **订单模式** → 请求 surface 必须在**已验证快照集**内(严格快照,
    不落"默认 chosen"分支)。空/无关权益 → 快照集为空/不含该 surface → `SurfaceNotAllowedError` fail-closed
    (此前空权益能跑默认 chosen 的 doubao = 把缺失快照当默认权益)。"""
    from services.ai_surface_monitoring.registry import OrderSurfaceUnavailableError
    from services.ai_surface_monitoring.service import SurfaceNotAllowedError
    capture: dict = {}
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture))  # ready 默认 policy
    # 空权益 → 空快照 = 损坏订单 → OrderSurfaceUnavailableError(复审#2-R6-3-R5 P1-2:入口即拒空快照)
    with pytest.raises(OrderSurfaceUnavailableError):
        await svc.collect_observation(_req("doubao_ark_api_search", "e1"),
                                      order_entitlement=OrderPlatformEntitlement(set()), round_id="R")
    # 无关权益(买 Kimi)快照集 ['other_explicit'] 不含 doubao → SurfaceNotAllowedError(不落默认 chosen)
    with pytest.raises(SurfaceNotAllowedError):
        await svc.collect_observation(_req("doubao_ark_api_search", "e2"),
                                      order_entitlement=OrderPlatformEntitlement({"other_explicit"}), round_id="R")
    assert capture.get("prompts", []) == []


# ---- P1-4:**历史** policy 仍写 deepseek_native_with_search(不可用)→ 读时替换实跑四路;但当前 policy
#          不该继续这样写(复审#2-R6 DeepSeek 裁定,见下面 readiness / corrected-default 测试) ----
def _ai2_legacy_native_with_search_platforms():
    """一份**历史/错误**策略:deepseek 写了一个**不可用**的表面。

    [2026-07-27] 原来用的 ``deepseek_native_with_search`` 已实测转 active,不再是"不可用表面",
    故换成仍然 unavailable 的 ``deepseek_metaso_proxy`` —— 测试要验的语义(policy 选了不可用表面
    → 整轮 fail-closed)一字未改,只是换了个仍然符合前提的样本。
    """
    return [
        PlatformPolicyV1(platform_key="doubao", enabled=True, base_weight_bps=2500, surface_key="doubao_ark_api_search"),
        PlatformPolicyV1(platform_key="qwen", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"),
        PlatformPolicyV1(platform_key="deepseek", enabled=True, base_weight_bps=2500,
                         surface_key="deepseek_metaso_proxy"),  # 不可用,且**无替身**
        PlatformPolicyV1(platform_key="yuanbao", enabled=True, base_weight_bps=2500, surface_key="yuanbao_hy3_tokenhub"),
        PlatformPolicyV1(platform_key="kimi", enabled=False, base_weight_bps=0, legacy_read_only=True),
    ]


def _ai2_corrected_default_platforms():
    """复审#2-R6 DeepSeek 裁定的**当前正确**默认:deepseek 直接写真实运行表面 deepseek_dashscope_search_legacy。"""
    return [
        PlatformPolicyV1(platform_key="doubao", enabled=True, base_weight_bps=2500, surface_key="doubao_ark_api_search"),
        PlatformPolicyV1(platform_key="qwen", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"),
        PlatformPolicyV1(platform_key="deepseek", enabled=True, base_weight_bps=2500,
                         surface_key="deepseek_dashscope_search_legacy"),  # 直接写运行表面,无需替换
        PlatformPolicyV1(platform_key="yuanbao", enabled=True, base_weight_bps=2500, surface_key="yuanbao_hy3_tokenhub"),
        PlatformPolicyV1(platform_key="kimi", enabled=False, base_weight_bps=0, legacy_read_only=True),
    ]


def test_unavailable_surface_policy_never_substitutes_and_fails_closed():
    """policy 选了不可用表面 → **绝不替换**,整轮 fail-closed。

    [2026-07-27 改名并强化] 原名 ``test_native_with_search_policy_live_no_substitution_but_matrix_shows_it``。
    当时 native_with_search 不可用,且登记了 → dashscope legacy 的替身,所以"只读矩阵显示替换"。
    现在两件事都变了:native_with_search 实测转 active;而**替换表已清空** ——
    因为工单红线是"官方通道不可用时绝不静默回落到别家供应商"
    (那正是"以为在测 DeepSeek 实际测阿里"的成因)。
    所以本测的契约收紧为:不可用表面既不进 live 轮,也**不在任何地方被替换**。
    """
    reg = AdapterRegistry(ingest_policy(platforms=_ai2_legacy_native_with_search_platforms()))
    # 不替换 → deepseek(选了不可用表面)不实跑 → 只 3 路
    surfaces = reg.default_sampling_surfaces()
    assert len(surfaces) == 3
    assert not any(get_surface_spec(sk).canonical_monitoring_platform == "deepseek" for sk in surfaces)
    # readiness = attention_required(非 ready)→ service 层据此拒绝 live 开闸
    rd = reg.readiness()
    # [2026-07-27] 状态由 attention_required 收紧为 **blocked**:
    #   替换表清空后,不可用表面**没有任何替身**,这比"有替身但不用"更严重,理应更响。
    assert rd["ready"] is False and rd["status"] == "blocked"
    # 🔴 红线:不可用表面**不解析成任何别家表面**(替换表已空)
    assert resolve_runtime_surface("deepseek_metaso_proxy") == ("deepseek_metaso_proxy", False)
    assert resolve_runtime_surface("deepseek_native_with_search") == ("deepseek_native_with_search", False)
    m = {r.surface_key: r for r in reg.get_surface_matrix().rows}
    assert m["deepseek_dashscope_search_legacy"].substituted_from is None, "禁止静默回落阿里"
    # live 放行闸:legacy 非默认表面,无订单权益不放行
    snap = reg.policy.get_policy()
    assert reg.is_surface_allowed("deepseek_dashscope_search_legacy", snapshot=snap) is False


def test_corrected_default_policy_ready_without_substitution():
    # 当前正确默认(deepseek 直接写 legacy)→ 实跑四路 + readiness fully ready(无替换)
    reg = AdapterRegistry(ingest_policy(platforms=_ai2_corrected_default_platforms()))
    surfaces = reg.default_sampling_surfaces()
    assert "deepseek_dashscope_search_legacy" in surfaces and len(surfaces) == 4
    rd = reg.readiness()
    assert rd["ready"] is True and rd["status"] == "ready" and rd["substitutions"] == []
    # [2026-07-27] native_with_search 已实测转 active(原判"官方无可验证原生搜索"被真实响应否定)
    assert get_surface_spec("deepseek_native_with_search").availability == "active"


# ---- P1-5: deepseek 官方回显严格校验(默认 registry 打开 verify) ----
async def test_deepseek_native_verify_enabled_by_default():
    from services.ai_surface_monitoring.registry import _build_default_adapters
    adapter = _build_default_adapters()["deepseek_native_no_search"]
    assert adapter._verify_model_echo is True  # 复审 P1-5:默认开启


# ---- P1-6: 权益是 surface 级 + 不可用表面绝不放行 ----
def test_entitlement_is_surface_level_and_unavailable_never_allowed():
    reg = AdapterRegistry(ingest_policy())
    # 订单含 deepseek 平台的另一表面权益,但只放行被授权的那个 surface,不放行未授权同平台表面
    ent_legacy = OrderPlatformEntitlement({"deepseek_dashscope_search_legacy"})
    assert reg.is_surface_allowed("deepseek_dashscope_search_legacy", ent_legacy) is True
    assert reg.is_surface_allowed("deepseek_metaso_proxy", ent_legacy) is False  # 未授权 + 不可用
    # 即使权益显式含 unavailable 表面,也不放行(active 门)
    ent_unavail = OrderPlatformEntitlement({"deepseek_metaso_proxy"})
    assert reg.is_surface_allowed("deepseek_metaso_proxy", ent_unavail) is False


# ---- P1-7: 迁移自愈(ADD CONSTRAINT / ADD COLUMN IF NOT EXISTS) ----
def test_migration_ddl_self_heals_partial_schema():
    from services.ai_surface_monitoring.migrations_stub import ALL_DDL
    assert "ADD COLUMN IF NOT EXISTS" in ALL_DDL
    assert "ADD CONSTRAINT" in ALL_DDL and "pg_constraint" in ALL_DDL  # 反查后补约束
    assert "geo_ai_surface_cost_ledger_request_uniq" in ALL_DDL       # ledger request_id UNIQUE
    assert "geo_ai_surface_cost_res_request_uniq" in ALL_DDL          # 在途去重 UNIQUE
    assert "call_count" in ALL_DDL                                    # 按 provider attempts 计
    assert "ADD COLUMN IF NOT EXISTS round_id" in ALL_DDL             # P1-1:单轮预算列自愈


# ---- P1-8: 未接 driver/reaper → readiness 失败;RpmLimiter 按 worker 数分摊 ----
def test_readiness_fails_when_unwired():
    sw.set_sampling_driver(sw._NoopDriver()); sw.set_reservation_reaper(None)
    try:
        r = sw.check_readiness()
        assert r["ready"] is False and len(r["problems"]) == 2
        # 接线后 ready
        class _D:
            async def run_tick(self, sk): return {"ok": True}
        sw.set_sampling_driver(_D()); sw.set_reservation_reaper(lambda: 0)
        assert sw.check_readiness()["ready"] is True
    finally:
        sw.set_sampling_driver(sw._NoopDriver()); sw.set_reservation_reaper(None)


async def test_unwired_monitoring_tick_fails_not_fake_success():
    sw.set_sampling_driver(sw._NoopDriver())
    out = await sw._job_monitoring_daily()
    assert out.get("ok") is False and out.get("wired") is False  # 非假成功


def test_rpm_limiter_divides_by_worker_count():
    lim = RpmLimiter(rpm_by_domain={"volcengine": 24000}, worker_count=4)
    assert lim._per_worker_rpm("volcengine") == 6000  # 24000 / 4(合计 ≈ 账户 24000)
    assert RpmLimiter(rpm_by_domain={"x": 1000}, worker_count=1)._per_worker_rpm("x") == 1000


# ---- P2: 策略 范围/权重总和/异常比例 校验 ----
def test_policy_validators_reject_bad_config():
    base = dict(policy_version="v", source_base_weights_bps=SourceBaseWeightsV1(),
               sampling_budget=SamplingBudgetV1(), feature_flags=ObservationFeatureFlagsV1())
    # 权重合计 > 10000
    with pytest.raises(Exception):
        ObservationPolicySnapshot(platforms=[
            PlatformPolicyV1(platform_key="a", enabled=True, base_weight_bps=6000, surface_key="doubao_ark_api_search"),
            PlatformPolicyV1(platform_key="b", enabled=True, base_weight_bps=6000, surface_key="qwen_dashscope_search"),
        ], **base)
    # 异常 denominator < numerator
    with pytest.raises(Exception):
        ObservationPolicySnapshot(platforms=[], anomaly_confirmation_numerator=3,
                                  anomaly_confirmation_denominator=2, **base)
    # public_min_independent_brands < 3
    with pytest.raises(Exception):
        ObservationPolicySnapshot(platforms=[], public_min_independent_brands=1, **base)


# ---- P2: 校准推荐集用 recommended_brands,不把全部提及品牌当推荐 ----
async def test_calibration_recommended_brands_specific():
    from services.ai_surface_monitoring.calibration import CalibrationHarness
    from services.ai_surface_monitoring.contracts import ObservationEnvelopeV1, utcnow

    async def collect(surface, q, run):
        return ObservationEnvelopeV1(
            request_id="r", source_kind="research", source_ref="s", question_text="q", question_hash="h",
            query_kind="non_branded", platform_key="p", provider_key="pr", model_key="m",
            surface_key=surface, session_mode="clean", prompt_text="q", answer_text="a", answer_hash="h",
            response_status="answered", observed_at=utcnow())

    # 复审#2 finding-4:判别性——旧式"提及全集"随重复**变化**({A,B} vs {A,C}),
    # 而**推荐集恒 {A}**。新码用 recommended_brands → jaccard=1.0;若回退成"提及即推荐"
    # → 用 {A,B}/{A,C} → jaccard=|{A}|/|{A,B,C}|=0.3333 → 断言翻红(真正锁死修复)。
    runs = {"n": 0}

    def judge(env):
        runs["n"] += 1
        extra = "B" if runs["n"] == 1 else "C"   # 提及全集逐次变化
        return {"mentioned": True, "recommended": True,
                "brands": ["A", extra], "recommended_brands": ["A"]}

    rep = await CalibrationHarness(collect, repeats=2, entity_judge=judge).run(["doubao_ark_api_search"], ["q1"])
    assert rep.per_surface["doubao_ark_api_search"]["recommendation_jaccard"] == 1.0
    assert rep.per_surface["doubao_ark_api_search"]["brand_mention_rate_bps"] == 10000

    # 反向锚:推荐集本身逐次变化({A} vs {A,B})→ jaccard 严格介于 0 与 1(钉住口径 =0.5)
    runs2 = {"n": 0}

    def judge_varies(env):
        runs2["n"] += 1
        rec = ["A"] if runs2["n"] == 1 else ["A", "B"]
        return {"mentioned": True, "recommended_brands": rec}

    rep2 = await CalibrationHarness(collect, repeats=2, entity_judge=judge_varies).run(
        ["doubao_ark_api_search"], ["q1"])
    assert rep2.per_surface["doubao_ark_api_search"]["recommendation_jaccard"] == 0.5  # |{A}|/|{A,B}|


# ==== 复审#2 R1 confirmed(4 项)判别测试 ====

# ---- finding-1 再订正(复审#2-R6-2 P1-1/P1-4):订单**严格按不可变快照执行**,绝不追加默认矩阵未购买平台 ----
def test_order_execution_is_strict_snapshot_no_default_backfill():
    from services.ai_surface_monitoring.registry import (
        OrderSurfaceConflictError,
        OrderSurfaceUnavailableError,
    )
    reg = AdapterRegistry(ingest_policy(platforms=_ai2_corrected_default_platforms()))
    # 只买 Kimi 的订单 → **恰跑 Kimi 一路**,绝不 backfill 默认四路(P1-1:不额外跑未购买平台 = 无额外 API 成本)
    assert reg.sampling_surfaces_for_order(OrderPlatformEntitlement({"other_explicit"})) == ["other_explicit"]
    # 只买 deepseek 官方 native 的订单 → 恰跑它一个(不 backfill doubao/qwen/yuanbao)
    assert reg.sampling_surfaces_for_order(
        OrderPlatformEntitlement({"deepseek_native_no_search"})) == ["deepseek_native_no_search"]
    # P1-4:未知 surface → fail-closed(不静默丢弃/回落默认)
    with pytest.raises(OrderSurfaceUnavailableError):
        reg.sampling_surfaces_for_order(OrderPlatformEntitlement({"future_surface_v99"}))
    # 同订单同平台多 surface → Conflict fail-closed
    with pytest.raises(OrderSurfaceConflictError):
        reg.sampling_surfaces_for_order(
            OrderPlatformEntitlement({"deepseek_native_no_search", "deepseek_dashscope_search_legacy"}))
    # 已不可用 surface → Unavailable fail-closed
    with pytest.raises(OrderSurfaceUnavailableError):
        reg.sampling_surfaces_for_order(OrderPlatformEntitlement({"deepseek_metaso_proxy"}))

    # 默认矩阵只在**创建新订单**时物化进快照(执行期不再引用默认):默认四路 ∪ 加购 Kimi
    created = reg.materialize_new_order_surfaces(extra_surface_keys={"other_explicit"})
    assert set(created) == {"doubao_ark_api_search", "qwen_dashscope_search",
                            "deepseek_dashscope_search_legacy", "yuanbao_hy3_tokenhub", "other_explicit"}
    # 加购未知/不可用 surface 创建期也 fail-closed
    with pytest.raises(OrderSurfaceUnavailableError):
        reg.materialize_new_order_surfaces(extra_surface_keys={"future_surface_v99"})


def test_materialize_fail_closed_when_policy_not_ready():
    """复审#2-R6-2 收敛:policy 非 ready(默认矩阵某已启用平台选了不可用表面)时创建新订单快照必
    **fail-closed**,绝不把静默降级的默认集(少 deepseek)烙进不可变订单快照(否则客户按四路付费长期少一路)。"""
    from services.ai_surface_monitoring.registry import OrderSurfaceUnavailableError
    reg = AdapterRegistry(ingest_policy(platforms=_ai2_legacy_native_with_search_platforms()))
    assert reg.readiness()["status"] == "blocked"  # [2026-07-27] 无替身 → 比 attention_required 更严重
    with pytest.raises(OrderSurfaceUnavailableError):
        reg.materialize_new_order_surfaces()  # 默认部分会静默丢 deepseek → 必 fail-closed(非降级快照)


async def test_service_entry_rejects_conflicting_order_entitlement_zero_calls():
    """复审#2-R6-3 P1-1:订单快照校验**进真实采集链**——同订单同平台双 surface(deepseek native+legacy)→
    service 入口 sampling_surfaces_for_order 抛冲突 fail-closed,**零 provider 调用**(此前两路都 answered)。"""
    from services.ai_surface_monitoring.registry import OrderSurfaceConflictError
    capture: dict = {}
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture))
    ent = OrderPlatformEntitlement({"deepseek_native_no_search", "deepseek_dashscope_search_legacy"})
    with pytest.raises(OrderSurfaceConflictError):   # collect_observation:整份权益冲突被拒
        await svc.collect_observation(_req("deepseek_native_no_search", "c1"), order_entitlement=ent, round_id="R")
    with pytest.raises(OrderSurfaceConflictError):   # collect_batch:上前置同样 fail-closed
        async for _ in svc.collect_batch(
                [_req("deepseek_native_no_search", "c2"), _req("deepseek_dashscope_search_legacy", "c3")],
                order_entitlement=ent, round_id="R"):
            pass
    assert capture.get("prompts", []) == []          # 零 provider 调用


async def test_collect_batch_requires_request_set_equals_snapshot():
    """复审#2-R6-3 P1-1:订单批请求 surface 集必须**严格等于**已验证快照集——缺一路 / 多一路都 fail-closed
    (零调用);恰相等才放行。此前 collect_batch 未校验完整权益。"""
    from services.ai_surface_monitoring.service import SurfaceNotAllowedError
    capture: dict = {}
    svc, _ = build_service(engine_fetch=make_engine_fetch(capture=capture))
    ent = OrderPlatformEntitlement({"doubao_ark_api_search", "other_explicit"})  # 快照 = {doubao, kimi}
    with pytest.raises(SurfaceNotAllowedError):       # 缺一路(只请求 doubao)
        async for _ in svc.collect_batch([_req("doubao_ark_api_search", "b1")], order_entitlement=ent, round_id="R"):
            pass
    with pytest.raises(SurfaceNotAllowedError):       # 多一路(混入未购买 qwen)
        async for _ in svc.collect_batch(
                [_req("doubao_ark_api_search", "b2"), _req("other_explicit", "b3"),
                 _req("qwen_dashscope_search", "b4")], order_entitlement=ent, round_id="R"):
            pass
    assert capture.get("prompts", []) == []           # 上述两次零调用
    # 恰相等 → 放行(两路 answered)
    envs = [e async for e in svc.collect_batch(
        [_req("doubao_ark_api_search", "b5"), _req("other_explicit", "b6")], order_entitlement=ent, round_id="R")]
    assert all(e.response_status == "answered" for e in envs) and len(capture.get("prompts", [])) == 2


def _P(**kw):
    return PlatformPolicyV1(**kw)


def test_policy_validator_rejects_malformed_config_at_construction():
    """复审#2-R6-3 P1-2:**完整执行**机器契约 policy_schema.checks——畸形 policy **构造期即拒**
    (此前只查 <=10000,漏了唯一/至少一个启用/恰 10000/正权重/归属 → 畸形被签成 ready)。"""
    K = _P(platform_key="kimi", enabled=False, base_weight_bps=0, legacy_read_only=True)
    # ① legacy 表面 other_explicit(归属 kimi)授给真实平台 yuanbao → 归属不符 → 拒
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=2500, surface_key="doubao_ark_api_search"),
            _P(platform_key="qwen", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"),
            _P(platform_key="deepseek", enabled=True, base_weight_bps=2500, surface_key="deepseek_dashscope_search_legacy"),
            _P(platform_key="yuanbao", enabled=True, base_weight_bps=2500, surface_key="other_explicit"), K])
    # ② 全平台错误指向 qwen surface → 归属不符 → 拒(reviewer 复现:最终只物化 ['qwen'])
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"),
            _P(platform_key="qwen", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"),
            _P(platform_key="deepseek", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"),
            _P(platform_key="yuanbao", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"), K])
    # ③ 全 disabled(无启用平台)→ 拒(契约 check③;reviewer 复现:readiness=ready)
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=False, base_weight_bps=10000, legacy_read_only=False), K])
    # ④ 全目录合计 ≠ 10000 → 拒(契约 check④)
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=3500, surface_key="doubao_ark_api_search"), K])
    # ⑤ platform_key 非唯一 → 拒(契约 check①)
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=5000, surface_key="doubao_ark_api_search"),
            _P(platform_key="doubao", enabled=True, base_weight_bps=5000, surface_key="doubao_ark_api_search")])


def test_readiness_and_materialize_defense_against_validator_bypassing_snapshot():
    """复审#2-R6-3 P1-2 防御(钱安全闸不信 policy 校验是唯一防线):即便某快照 **model_construct 绕过校验器**
    含"启用平台选 legacy 表面",readiness 仍报 blocked、default_sampling 静默剔一路、materialize 仍双重 fail-closed。"""
    from services.ai_surface_monitoring.policy import (
        FakeObservationPolicy,
        ObservationFeatureFlagsV1,
        ObservationPolicySnapshot,
        SamplingBudgetV1,
        SourceBaseWeightsV1,
    )
    from services.ai_surface_monitoring.registry import OrderSurfaceUnavailableError
    bad = ObservationPolicySnapshot.model_construct(
        policy_version="bypass",
        platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=2500, surface_key="doubao_ark_api_search"),
            _P(platform_key="qwen", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"),
            _P(platform_key="deepseek", enabled=True, base_weight_bps=2500, surface_key="deepseek_dashscope_search_legacy"),
            _P(platform_key="yuanbao", enabled=True, base_weight_bps=2500, surface_key="other_explicit"),  # legacy 表面授真实平台
            _P(platform_key="kimi", enabled=False, base_weight_bps=0, legacy_read_only=True)],
        source_base_weights_bps=SourceBaseWeightsV1(), sampling_budget=SamplingBudgetV1(),
        max_single_brand_share_bps=1000, public_min_independent_brands=3, public_min_source_types=2,
        retention_days=180, anomaly_confirmation_numerator=2, anomaly_confirmation_denominator=3,
        feature_flags=ObservationFeatureFlagsV1(ingest_enabled=True))
    reg = AdapterRegistry(FakeObservationPolicy(snapshot=bad))
    rd = reg.readiness()
    assert rd["ready"] is False and rd["status"] == "blocked"   # readiness 防御(legacy 分支)
    assert len(reg.default_sampling_surfaces()) == 3            # 默认轮静默剔 legacy(证明不等价 → 需防御)
    with pytest.raises(OrderSurfaceUnavailableError):          # materialize 双重 fail-closed(readiness 闸 + 独立覆盖校验)
        reg.materialize_new_order_surfaces()


def test_enabled_legacy_platform_must_have_surface_and_positive_weight():
    """复审#2-R6-3-R5 P1-1:契约"enabled 平台需 non-null surface + 正 base_weight"**不对 legacy 豁免**;
    仅 disabled legacy 目录行可 surface=null/weight=0。enabled legacy + surface=null(reviewer 复现:声称 N 路、
    订单实际少一路)→ 构造期拒;enabled legacy + weight=0 → 拒。"""
    # enabled legacy + surface=null → PlatformPolicyV1 层拒
    with pytest.raises(Exception):
        _P(platform_key="kimi", enabled=True, base_weight_bps=5000, legacy_read_only=True, surface_key=None)
    # reviewer 复现:qwen enabled + kimi enabled-legacy-surface-null → policy 构造期拒(不再声称两路只跑一路)
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="qwen", enabled=True, base_weight_bps=5000, surface_key="qwen_dashscope_search"),
            _P(platform_key="kimi", enabled=True, base_weight_bps=5000, legacy_read_only=True, surface_key=None)])
    # enabled legacy + weight=0 → snapshot 正权重校验拒(含 legacy)
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=10000, surface_key="doubao_ark_api_search"),
            _P(platform_key="kimi", enabled=True, base_weight_bps=0, legacy_read_only=True, surface_key="other_explicit")])
    # disabled legacy 目录行 surface=null / weight=0 仍合法(契约允许)
    assert _P(platform_key="kimi", enabled=False, base_weight_bps=0, legacy_read_only=True).surface_key is None


async def test_empty_snapshot_and_empty_batch_fail_closed_not_silent_success():
    """复审#2-R6-3-R5 P1-2:空订单快照(空权益)= 损坏订单,**不能表现成正常完成**。
    sampling_surfaces_for_order 入口即拒空快照;collect_batch 空权益+空批次 → 抛错(此前零异常零结果)。"""
    from services.ai_surface_monitoring.registry import OrderSurfaceUnavailableError
    reg = AdapterRegistry(ingest_policy())
    with pytest.raises(OrderSurfaceUnavailableError):
        reg.sampling_surfaces_for_order(OrderPlatformEntitlement(set()))
    svc, _ = build_service()
    with pytest.raises(OrderSurfaceUnavailableError):   # 空权益 + 空批次:上前置 raise,非零异常零结果
        async for _ in svc.collect_batch([], order_entitlement=OrderPlatformEntitlement(set()), round_id="R"):
            pass


def test_canonical_collision_bypass_blocked_and_materialize_fail_loud():
    """复审#2-R6-3-R7:两个启用平台条目映射**同一规范平台**(canonical 冲突;真实校验器由唯一 platform_key +
    归属挡下,此为 model_construct 绕过防御)→ readiness=blocked(默认轮会双采样)+ materialize **fail-loud**
    (与执行路径 sampling_surfaces_for_order 的 OrderSurfaceConflictError 对齐,不 canonical 去重静默丢一路)。"""
    from services.ai_surface_monitoring.policy import (
        FakeObservationPolicy,
        ObservationFeatureFlagsV1,
        ObservationPolicySnapshot,
        SamplingBudgetV1,
        SourceBaseWeightsV1,
    )
    from services.ai_surface_monitoring.registry import OrderSurfaceConflictError, OrderSurfaceUnavailableError
    bad = ObservationPolicySnapshot.model_construct(
        policy_version="canon-collide",
        platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=2500, surface_key="doubao_ark_api_search"),
            _P(platform_key="qwen", enabled=True, base_weight_bps=2500, surface_key="qwen_dashscope_search"),
            _P(platform_key="deepseek", enabled=True, base_weight_bps=2500, surface_key="deepseek_native_no_search"),
            # 同规范平台 deepseek 的第二条目(绕过唯一/归属校验)
            _P(platform_key="deepseek_alt", enabled=True, base_weight_bps=2500, surface_key="deepseek_dashscope_search_legacy")],
        source_base_weights_bps=SourceBaseWeightsV1(), sampling_budget=SamplingBudgetV1(),
        max_single_brand_share_bps=1000, public_min_independent_brands=3, public_min_source_types=2,
        retention_days=180, anomaly_confirmation_numerator=2, anomaly_confirmation_denominator=3,
        feature_flags=ObservationFeatureFlagsV1(ingest_enabled=True))
    reg = AdapterRegistry(FakeObservationPolicy(snapshot=bad))
    rd = reg.readiness()
    assert rd["ready"] is False and rd["status"] == "blocked"   # canonical 冲突 → blocked(不再假 ready)
    with pytest.raises((OrderSurfaceUnavailableError, OrderSurfaceConflictError)):  # materialize fail-closed(非静默丢一路)
        reg.materialize_new_order_surfaces()
    # 执行路径对同 canonical 双 surface 权益 raise(证明 materialize 现与之对齐 fail-loud)
    with pytest.raises(OrderSurfaceConflictError):
        reg.sampling_surfaces_for_order(
            OrderPlatformEntitlement({"deepseek_native_no_search", "deepseek_dashscope_search_legacy"}))


def test_materialize_addon_only_adds_never_replaces_default():
    """复审#2-R6-3-R9 P1-1:materialize 加购(extra_surface_keys)**只新增默认未覆盖平台**,规范平台与默认/其它
    加购冲突一律 fail-closed;**绝不删除/静默替换默认已购通道**(此前加购 deepseek_native 会把默认 legacy 静默换掉)。
    换通道需独立 replacement 契约。"""
    from services.ai_surface_monitoring.registry import OrderSurfaceConflictError
    reg = AdapterRegistry(ingest_policy(platforms=_ai2_corrected_default_platforms()))  # 默认含 deepseek_dashscope_search_legacy
    # reviewer 复现:加购 deepseek_native(同 canonical deepseek)不得删除默认 legacy → 冲突
    with pytest.raises(OrderSurfaceConflictError):
        reg.materialize_new_order_surfaces(extra_surface_keys={"deepseek_native_no_search"})
    # 两加购同 canonical → 冲突
    with pytest.raises(OrderSurfaceConflictError):
        reg.materialize_new_order_surfaces(
            extra_surface_keys={"deepseek_native_no_search", "deepseek_dashscope_search_legacy"})
    # 执行路径对同 canonical 双 surface 权益也 fail-loud(对齐)
    with pytest.raises(OrderSurfaceConflictError):
        reg.sampling_surfaces_for_order(
            OrderPlatformEntitlement({"deepseek_native_no_search", "deepseek_dashscope_search_legacy"}))
    # 加购**默认未覆盖平台**(Kimi)才允许 → 5 路,默认四路一个不丢
    out = reg.materialize_new_order_surfaces(extra_surface_keys={"other_explicit"})
    assert set(out) == {"doubao_ark_api_search", "qwen_dashscope_search",
                        "deepseek_dashscope_search_legacy", "yuanbao_hy3_tokenhub", "other_explicit"}


@pytest.mark.parametrize("legacy_flag,surface", [
    (True, "qwen_dashscope_search"),        # 非 legacy surface 标 legacy(reviewer 复现)→ 拒
    (False, "other_explicit"),              # legacy surface 标非 legacy(反向)→ 拒(但 kimi 平台)
])
def test_policy_legacy_read_only_must_match_surface_definition(legacy_flag, surface):
    """复审#2-R6-3-R9 P1-2:policy.legacy_read_only 与 lineage SURFACE_SPECS 的 legacy_read_only **两方向**
    不一致都在**策略加载(构造)时拒绝**(lineage 为唯一真相;防"标 legacy 只读却仍 live 采集"或反向)。"""
    plat = "qwen" if surface == "qwen_dashscope_search" else "kimi"
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=8000, surface_key="doubao_ark_api_search"),
            _P(platform_key=plat, enabled=True, base_weight_bps=2000, surface_key=surface, legacy_read_only=legacy_flag)])


def test_null_surface_legacy_read_only_validated_against_lineage():
    """复审#2-R6-3-R11 P2:surface_key=null 目录行的 legacy_read_only 也按 lineage 校验平台是否**确属 legacy**。
    disabled qwen + null + legacy=true(qwen 无 legacy surface)→ 构造期拒;Kimi disabled+null+legacy=true(有
    legacy surface other_explicit)→ 合法对照。"""
    # reviewer 复现:qwen 非 legacy 平台标 legacy=true(null surface)→ 拒
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=10000, surface_key="doubao_ark_api_search"),
            _P(platform_key="qwen", enabled=False, base_weight_bps=0, surface_key=None, legacy_read_only=True)])
    # Kimi(有 legacy surface)disabled + null + legacy=true → 合法(默认矩阵即如此)
    assert AdapterRegistry(ingest_policy()).readiness()["ready"] is True
    # 反向:Kimi disabled + null + legacy=false(kimi 确属 legacy)→ 拒
    with pytest.raises(Exception):
        ingest_policy(platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=10000, surface_key="doubao_ark_api_search"),
            _P(platform_key="kimi", enabled=False, base_weight_bps=0, surface_key=None, legacy_read_only=False)])


def test_matrix_legacy_read_only_from_lineage_only_not_policy_override():
    """复审#2-R6-3-R11 P2:get_surface_matrix 的 legacy_read_only **只读 lineage**(surface spec),不被 policy
    平台标记覆盖。model_construct 绕过校验器令 qwen policy 谎报 legacy=true → 矩阵仍按 lineage 报 qwen 非 legacy。"""
    from services.ai_surface_monitoring.policy import (
        FakeObservationPolicy,
        ObservationFeatureFlagsV1,
        ObservationPolicySnapshot,
        SamplingBudgetV1,
        SourceBaseWeightsV1,
    )
    bad = ObservationPolicySnapshot.model_construct(
        policy_version="qwen-legacy-lie",
        platforms=[
            _P(platform_key="doubao", enabled=True, base_weight_bps=5000, surface_key="doubao_ark_api_search"),
            _P(platform_key="qwen", enabled=True, base_weight_bps=5000, surface_key="qwen_dashscope_search", legacy_read_only=True)],
        source_base_weights_bps=SourceBaseWeightsV1(), sampling_budget=SamplingBudgetV1(),
        max_single_brand_share_bps=1000, public_min_independent_brands=3, public_min_source_types=2,
        retention_days=180, anomaly_confirmation_numerator=2, anomaly_confirmation_denominator=3,
        feature_flags=ObservationFeatureFlagsV1(ingest_enabled=True))
    m = {r.surface_key: r.legacy_read_only for r in AdapterRegistry(FakeObservationPolicy(snapshot=bad)).get_surface_matrix().rows}
    assert m["qwen_dashscope_search"] is False   # lineage 为准:qwen surface 非 legacy(不被 policy 谎报覆盖)
    assert m["other_explicit"] is True           # lineage:Kimi 表面确 legacy


def test_order_active_surface_without_adapter_is_fail_closed():
    """复审#2-R6-R3 加固:active 但本 registry 未注册 adapter(注册不一致)→ fail-closed,
    **绝不**静默跳过让默认矩阵回填该平台(=静默换通道)。回退成 continue → 无异常 → 翻红。"""
    from services.ai_surface_monitoring.registry import (
        AdapterRegistry,
        OrderPlatformEntitlement,
        OrderSurfaceUnavailableError,
        _build_default_adapters,
    )
    adapters = _build_default_adapters()
    del adapters["deepseek_native_no_search"]   # 制造注册不一致:active 表面缺 adapter
    reg = AdapterRegistry(ingest_policy(platforms=_ai2_corrected_default_platforms()), adapters=adapters)
    with pytest.raises(OrderSurfaceUnavailableError):
        reg.sampling_surfaces_for_order(OrderPlatformEntitlement({"deepseek_native_no_search"}))


# ---- finding-2:回显后缀数字打头但夹带词变体(hy3-2preview / hy3-0-lite)仍属不同模型 ----
@pytest.mark.parametrize("bad", ["hy3-2preview", "hy3-0-lite", "hy3-9x", "hy3-1beta", "hy3-2rc"])
def test_echo_rejects_digit_led_word_variants(bad):
    from services.ai_surface_monitoring.adapters.openai_compat import _model_echo_matches
    assert _model_echo_matches(bad, "hy3") is False


@pytest.mark.parametrize("good", ["hy3", "hy3-20260716", "hy3-v2", "hy3-2.5", "hy3-v2.5",
                                  "hy3-2026-07-16", "hy3:2026", "hy3_v3"])
def test_echo_accepts_pure_version_or_date_suffix(good):
    from services.ai_surface_monitoring.adapters.openai_compat import _model_echo_matches
    assert _model_echo_matches(good, "hy3") is True


# 复审#2-R2:为消除 ReDoS,分隔符类不再含点 → 点作**段分隔**的回显被收紧拒绝(点仅数字间版本点仍放行)
@pytest.mark.parametrize("dotted", ["hy3.v3", "hy3.2026.07.16", "hy3.2.5"])
def test_echo_rejects_dot_as_segment_separator(dotted):
    from services.ai_surface_monitoring.adapters.openai_compat import _model_echo_matches
    assert _model_echo_matches(dotted, "hy3") is False


def test_echo_matcher_is_linear_no_redos():
    """复审#2-R2/R3 finding:旧 (?:[-_.:@/]v?\\d[\\d.]*)+ 让 `.` 兼作分隔符与内容,对失败输入指数级
    回溯(ReDoS)。线性正则须**远低于**指数阈值返回。

    复审#2-R3:**不能**用线程 join(timeout) 抢占——CPython 的 re 匹配全程持 GIL、不被其它 Python
    线程打断,join 只会在匹配自身结束时返回(病态输入=数小时=真挂起)。故改用**有界 n=26**(旧病态正则
    约 2s 内**完成**、不永久挂)直接计时 + 绝对上界:回退到病态正则 → 本用例约 2s 后 assert dt<0.3
    **干净失败**(非挂起);当前线性正则亚毫秒通过。绝对上界两侧余量极大(线性 ~2e-5s vs 旧 ~2.2s),
    不受机器快慢影响。"""
    import time

    from services.ai_surface_monitoring.adapters.openai_compat import _model_echo_matches

    payload = "hy3" + ".9" * 26 + "?"   # 56 字符(<128 长度闸,真正压到正则);旧正则于此约 2.2s(有界)
    assert len(payload) < 128
    t0 = time.perf_counter()
    result = _model_echo_matches(payload, "hy3")
    dt = time.perf_counter() - t0
    assert result is False
    assert dt < 0.3, f"回显匹配疑似 ReDoS:{len(payload)} 字符耗时 {dt:.3f}s(线性应亚毫秒;旧重叠正则约 2.2s)"
    # 超长回显走 128 长度上界快速拒绝(输入成本上界)
    assert _model_echo_matches("hy3-" + "9" * 500, "hy3") is False


# ---- finding-3:PG try_reserve 账本幂等必须并入 INSERT(单快照);finalize 在两语句间提交不得签发新 token ----
def test_pg_try_reserve_insert_folds_ledger_dedup_single_snapshot():
    import inspect
    from services.ai_surface_monitoring.migrations_stub import COST_LEDGER_TABLE, PostgresBudgetLedger
    norm = " ".join(inspect.getsource(PostgresBudgetLedger.try_reserve).split())
    # 账本幂等检查须在 INSERT 的 WHERE 内(单条语句 = 单快照),不能仅靠 INSERT 前的独立 SELECT
    assert "NOT EXISTS (SELECT 1 FROM {COST_LEDGER_TABLE} WHERE request_id" in norm


# ---- 复审#2-R6 P1-1:PG 单轮 attempts 硬限须在 INSERT 单快照内(跨进程同 round_id 不越顶) ----
def test_pg_try_reserve_enforces_round_cap_single_snapshot():
    import inspect
    from services.ai_surface_monitoring.migrations_stub import ALL_DDL, PostgresBudgetLedger
    norm = " ".join(inspect.getsource(PostgresBudgetLedger.try_reserve).split())
    # 单轮上限在 INSERT 的 WHERE 内(ledger round 已入账 + 在途预留 + 本次 ≤ cap);<=0 短路为不限
    assert "%(lim_round)s IS NULL OR %(lim_round)s <= 0 OR %(round)s IS NULL OR" in norm
    assert "WHERE round_id=%(round)s" in norm
    # finalize 把 round_id 从预留带进账本(单轮聚合可读)
    fnorm = " ".join(inspect.getsource(PostgresBudgetLedger.finalize).split())
    assert "round_id" in fnorm
    # DDL 自愈 round_id 列(ledger + reservations 各一)
    assert ALL_DDL.count("ADD COLUMN IF NOT EXISTS round_id") == 2


def test_pg_try_reserve_race_finalize_between_precheck_and_insert():
    """模拟 finalize(#1) 在 try_reserve(#2) 的账本快路径 SELECT 与条件 INSERT 之间提交:
    修复(账本幂等并入 INSERT 单快照 + 消歧复读)→ 返回 ALREADY_CHARGED(不签发新 token=不发第二次
    provider 调用);回退成两快照 → INSERT 只查预留 → 签发新 token → 断言翻红。"""
    from services.ai_surface_monitoring.migrations_stub import (
        ALREADY_CHARGED_TOKEN,
        COST_LEDGER_TABLE,
        COST_RESERVATIONS_TABLE,
        PostgresBudgetLedger,
    )

    RID = "X"
    state = {"ledger": set(), "res": {}, "ledger_selects": 0}

    def _finalize_commit():  # 另一进程 finalize(#1):入账 X + 删其在途预留(两语句之间提交)
        state["ledger"].add(RID)
        state["res"].pop(RID, None)

    class _Cur:
        def __init__(self):
            self._row = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            low = s.lower()
            if "pg_advisory_xact_lock" in low:
                self._row = None
                return
            # 账本按 request_id 单行查(快路径 / 消歧);排除按 source_kind 的预算聚合子查询
            if low.startswith("select 1 from") and COST_LEDGER_TABLE in s and "where request_id" in low:
                rid = params[0] if isinstance(params, (tuple, list)) else params
                present = rid in state["ledger"]
                self._row = (1,) if present else None
                state["ledger_selects"] += 1
                if state["ledger_selects"] == 1 and not present:  # 竞态注入:快路径查空后 finalize 提交
                    _finalize_commit()
                return
            if low.startswith("insert into") and COST_RESERVATIONS_TABLE in s and "select" in low:
                rid, tok = params["rid"], params["token"]
                res_ok = rid not in state["res"]
                # 判别:INSERT 的 WHERE 是否在**单语句内**也查账本幂等(修复=是)
                checks_ledger = f"NOT EXISTS (SELECT 1 FROM {COST_LEDGER_TABLE} WHERE request_id" in s
                ledger_ok = (rid not in state["ledger"]) if checks_ledger else True
                if res_ok and ledger_ok:
                    state["res"][rid] = tok
                    self._row = (tok,)
                else:
                    self._row = None
                return
            self._row = None

        def fetchone(self):
            return self._row

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

    ledger = PostgresBudgetLedger(get_connection=lambda: _Conn())
    tok = ledger.try_reserve("research", 200_000, 1, surface_key="doubao_ark_api_search",
                             limit_micros=None, limit_calls=None, request_id=RID)
    assert tok in (ALREADY_CHARGED_TOKEN, None), \
        f"竞态下签发新 token {tok!r} → 会触发第二次 provider 调用(应 ALREADY_CHARGED/None)"
    assert tok == ALREADY_CHARGED_TOKEN  # 消歧复读发现已入账 → 精确短路


# ---- 复审#2-R6-3 P1-3:真实 PostgreSQL 并发——单轮上限**跨来源(source_kind)不得穿透** ----
@pytest.mark.skipif(not os.getenv("AI_SURFACE_PG_TEST_URL"),
                    reason="真实 PostgreSQL 并发判别;设 AI_SURFACE_PG_TEST_URL 指向隔离测试库后运行")
def test_pg_round_cap_holds_across_source_kinds_real_concurrency():
    """真实 PG + 多线程真并发:同一 round_id 下 research 与 monitoring 交替预留,cap=5。
    单轮上限是**全局按 round**;若 advisory 锁只按 source_kind(旧),跨来源同 round 不串行 → 穿透(合计 > cap)。
    修复(取 round_id 锁,按锁键排序)→ 跨来源同 round 严格串行 → 预留合计**恰 == cap**。"""
    import psycopg2  # 项目依赖 psycopg2-binary
    from concurrent.futures import ThreadPoolExecutor

    from services.ai_surface_monitoring.migrations_stub import (
        ALL_DDL,
        COST_LEDGER_TABLE,
        COST_RESERVATIONS_TABLE,
        PostgresBudgetLedger,
    )
    url = os.environ["AI_SURFACE_PG_TEST_URL"]
    setup = psycopg2.connect(url)
    setup.autocommit = True
    with setup.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {COST_RESERVATIONS_TABLE}, {COST_LEDGER_TABLE} CASCADE")
        cur.execute(ALL_DDL)
    setup.close()

    CAP, ROUND, N = 5, "R-cross-source", 30

    def worker(i: int):
        conn = psycopg2.connect(url)  # 每线程独立连接(真并发)
        try:
            led = PostgresBudgetLedger(get_connection=lambda: conn)
            sk = "research" if i % 2 == 0 else "monitoring"   # 跨来源交替
            return led.try_reserve(sk, 50_000, 1, surface_key="doubao_ark_api_search",
                                   limit_micros=None, limit_calls=None, request_id=f"req-{i}",
                                   round_id=ROUND, limit_round_calls=CAP)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=16) as ex:
        toks = list(ex.map(worker, range(N)))
    accepted = [t for t in toks if t and t != "__already_charged__"]

    check = psycopg2.connect(url)
    try:
        with check.cursor() as cur:
            cur.execute(f"SELECT COALESCE(SUM(call_count),0) FROM {COST_RESERVATIONS_TABLE} WHERE round_id=%s",
                        (ROUND,))
            total = int(cur.fetchone()[0])
    finally:
        check.close()
    assert total == CAP, f"跨来源同 round 预留合计 {total} 应恰 = cap {CAP}(旧锁按 source_kind → 穿透 > cap)"
    assert len(accepted) == CAP


def test_pg_ledger_returns_connection_to_pool_explicitly():
    """复审#2-R6-3-R4:PostgresBudgetLedger 每方法用完**显式归还连接**(_maybe_close 调 conn.close→putconn),
    不依赖 __del__ refcount 安全网。用可追踪 close 的假连接验证归还被调用(回退成 no-op → closed==0 翻红)。"""
    from services.ai_surface_monitoring.migrations_stub import PostgresBudgetLedger
    closed = {"n": 0}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return (0,)

    class _TrackConn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            closed["n"] += 1

    led = PostgresBudgetLedger(get_connection=lambda: _TrackConn())
    led.calls_today("research")        # 只读方法用完也归还
    assert closed["n"] == 1, "PG ledger 方法用完必须显式归还连接(conn.close 被调 1 次)"
    led.calls_in_round("R")
    assert closed["n"] == 2            # 每方法各归还一次
