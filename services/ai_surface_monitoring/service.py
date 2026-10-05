"""采集服务:对 AI-2 暴露的稳定入口(§9.1)+ 全部功能/预算/限速 gate 真执行(复审 P1-2..P1-5,P2-8)。

    collect_observation(request) -> ObservationEnvelopeV1
    collect_batch(requests) -> AsyncIterator[ObservationEnvelopeV1]
    get_surface_matrix() -> SurfaceMatrix
    get_adapter_health() -> list[SurfaceHealth]

编排(每步都是真执行点,非"只定义未消费"):
  ingest_enabled 观测总闸(已发起的 paid_diagnosis 仅通过专用履约入口豁免此一项)
  → Kimi/非默认 surface 权益闸 → 每 source_kind 每日 micros+调用 **原子预留**
  (跨 worker 正确:预留存共享账本)→ RPM 限速 + 每 provider 域并发 → adapter.collect → **finalize 真实成本**
  (含重试;付费调用即使后续异常也 charge 不丢账)。预算数据不可读 → budget_blocked(fail-closed)。

模块级默认服务 **fail-closed**:未 configure_default_service 前调用便捷函数直接抛 NotConfiguredError;
fake policy/账本只能显式注入(不再自动启用 fake + 真 adapter + 无账本)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional, Sequence

from services.ai_surface_monitoring.contracts import (
    CollectionRequest,
    ObservationEnvelopeV1,
    SurfaceHealth,
    UsageEvidence,
    answer_text_hash,
    canonical_question_hash,
    utcnow,
)
from services.ai_surface_monitoring.cost_policy import (
    default_provider_rpm,
    ALREADY_CHARGED_TOKEN,
    ConcurrencyGovernor,
    RpmLimiter,
    actual_calls_from_usage,
    budget_charge_micros,
    estimate_usage_cost,
)
from services.ai_surface_monitoring.lineage import get_surface_spec
from services.ai_surface_monitoring.policy import budget_limits_from_policy
from services.ai_surface_monitoring.registry import (
    AdapterRegistry,
    OrderPlatformEntitlement,
    SurfaceMatrix,
)

logger = logging.getLogger("GEO-AISurface-Service")


class SurfaceNotAllowedError(RuntimeError):
    """请求的表面不是 policy 为其平台选定的表面,且订单权益未包含。"""


class IngestDisabledError(RuntimeError):
    """policy feature_flags.ingest_enabled=False:总闸关闭,拒绝采集(零 provider 调用)。"""


class PaidDeliveryContextError(RuntimeError):
    """只有 ``paid_diagnosis`` 可走已售诊断履约入口，其他来源不得借此绕过 ingest 闸。"""


class PolicyUnavailableError(RuntimeError):
    """策略不可读 → fail-closed,不采集。"""


class NotConfiguredError(RuntimeError):
    """进程默认服务未接线(未 configure_default_service);真实采集必须注入真实 policy + 账本。"""


class PolicyNotReadyError(RuntimeError):
    """复审#2-R6-2 P1-3:当前 policy readiness != ready(需替换或无可用运行表面)→ live 拒绝开闸。
    替换只用于只读回放;当前 policy 必须由 AI-2 改成真实运行表面后方可 live 采集。"""


class RoundContextRequiredError(RuntimeError):
    """复审#2-R6-2 P1-2:单轮上限(max_calls_per_round>0,契约恒真)启用时,采集必须携带**稳定 round_id**;
    缺失 → fail-closed(不自动生成随机 id,否则跨 batch/worker 不共享上限 = 绕过)。"""


def _round_cap_enabled(limits) -> bool:
    """单轮上限是否启用(>0)。契约 constraint '>0' 使其恒真;<=0(仅防御)视为未启用。"""
    return limits.max_calls_per_round is not None and int(limits.max_calls_per_round) > 0


def _round_id_missing(round_id) -> bool:
    """round_id 是否**缺失**(复审#2-R6-2 收敛):None **或空白串**都算缺失。
    防空串 '' 既绕过强制、又把不同逻辑轮塌缩到同一 '' 账本桶(driver 从可能为空的 DB 字段取值即触发)。"""
    return round_id is None or not str(round_id).strip()


def _build_error_envelope(request: CollectionRequest, reason: str) -> ObservationEnvelopeV1:
    """构造 error 信封(零 provider 调用/零成本),供 collect_batch 失败隔离用。地域字段保真。"""
    try:
        spec = get_surface_spec(request.surface_key)
        platform, provider, model = spec.platform_key, spec.provider_key, spec.default_model_key
    except Exception:
        platform, provider, model = "unknown", "unknown", "unknown"
    logger.info("[collect] error envelope surface=%s reason=%s", request.surface_key, reason)
    return ObservationEnvelopeV1(
        request_id=request.request_id, source_kind=request.source_kind, source_ref=request.source_ref,
        owner_user_id=request.owner_user_id, brand_id=request.brand_id, industry_key=request.industry_key,
        question_text=request.question_text, question_hash=canonical_question_hash(request.question_text),
        query_kind=request.query_kind, platform_key=platform, provider_key=provider, model_key=model,
        surface_key=request.surface_key, search_mode=request.search_mode,
        country_code=request.country_code, region_key=request.region_key,
        session_mode=request.session_mode, run_index=request.run_index,
        prompt_text=request.effective_prompt_text(), answer_text="", answer_hash=answer_text_hash(""),
        response_status="error", citations=[],
        usage=UsageEvidence(estimated_cost_micros=0, usage_is_estimated=False),
        latency_ms=0, retry_of_request_id=request.retry_of_request_id, observed_at=utcnow(),
    )


class CollectionService:
    def __init__(
        self,
        registry: AdapterRegistry,
        *,
        ledger=None,
        governor: Optional[ConcurrencyGovernor] = None,
        rpm_limiter: Optional[RpmLimiter] = None,
    ):
        self._registry = registry
        self._ledger = ledger  # 原子预留账本(cost_policy.BudgetLedger);None = 无预算强制(仅显式测试/dry-run)
        self._governor = governor or ConcurrencyGovernor()
        # [C · 2026-07-27] 默认注入各供应商已知的 RPM 硬限(目前只有腾讯 TokenHub 5 QPS)。
        #   改前是"默认不限" —— 元宝转联网后,监测侧并发 10 / 观测层每平台 8 都会直接压过 5 QPS。
        #   未登记的供应商仍不限,既有行为零变化。
        self._rpm = rpm_limiter or RpmLimiter(rpm_by_domain=default_provider_rpm())

    # ------------------------------------------------------------------
    def get_surface_matrix(self) -> SurfaceMatrix:
        return self._registry.get_surface_matrix()

    async def get_adapter_health(self, only_active: bool = True) -> list[SurfaceHealth]:
        return await self._registry.get_adapter_health(only_active=only_active)

    # ------------------------------------------------------------------
    def _projected_increment_micros(self, surface_key: str) -> int:
        """单次调用的预算预估增量(保守,永不 0)。"""
        micros, _ = budget_charge_micros(estimate_usage_cost(surface_key, None, None, None))
        return micros

    async def collect_observation(
        self,
        request: CollectionRequest,
        *,
        order_entitlement: Optional[OrderPlatformEntitlement] = None,
        round_id: Optional[str] = None,
    ) -> ObservationEnvelopeV1:
        """观测采集入口；``ingest_enabled`` 关闭时保持零 provider 调用。"""
        return await self._collect_observation(
            request,
            order_entitlement=order_entitlement,
            round_id=round_id,
            allow_paid_delivery=False,
        )

    async def collect_paid_delivery(
        self,
        request: CollectionRequest,
        *,
        round_id: Optional[str] = None,
    ) -> ObservationEnvelopeV1:
        """履行已发起的付费诊断，不把观测账本开关误当成产品停服开关。

        该入口只豁免 ``ingest_enabled``；policy 可读性、默认表面 readiness、预算、
        单轮上限、RPM、模型回显及失败记账全部继续执行。研究和监测不得调用。
        """
        if request.source_kind != "paid_diagnosis":
            raise PaidDeliveryContextError(
                f"collect_paid_delivery 仅接受 paid_diagnosis，收到 {request.source_kind!r};"
                "拒绝调用 provider"
            )
        return await self._collect_observation(
            request,
            order_entitlement=None,
            round_id=round_id,
            allow_paid_delivery=True,
        )

    async def _collect_observation(
        self,
        request: CollectionRequest,
        *,
        order_entitlement: Optional[OrderPlatformEntitlement] = None,
        round_id: Optional[str] = None,
        allow_paid_delivery: bool = False,
    ) -> ObservationEnvelopeV1:
        if allow_paid_delivery and request.source_kind != "paid_diagnosis":
            raise PaidDeliveryContextError(
                "观测内部入口拒绝为非 paid_diagnosis 绕过 ingest 闸；零 provider 调用"
            )
        # 0) 读策略(不可读 → fail-closed,不采集)
        try:
            snap = await asyncio.to_thread(self._registry.policy.get_policy)
        except Exception as exc:
            raise PolicyUnavailableError(f"策略不可读 → fail-closed: {exc}") from exc

        # 1) ingest 是观测采集/登记总闸，不是已发起付费诊断的停服开关。诊断履约只能通过
        # collect_paid_delivery 进入，且仅豁免此一项；其余 readiness/预算/限速/模型回显门不变。
        if not snap.feature_flags.ingest_enabled and not allow_paid_delivery:
            raise IngestDisabledError("ingest_enabled=False;拒绝采集(零 provider 调用)")

        # 1.5 + 2) 权益/快照闸(复审#2-R6-3 P1-1:**订单快照完整校验进真实采集链**)
        if order_entitlement is not None:
            # 订单模式:在真实入口**完整校验不可变快照**——sampling_surfaces_for_order 对冲突/未知/不可用/缺
            # adapter 直接 raise fail-closed(零 provider 调用;同平台双 surface 冲突在此被挡);且请求 surface
            # 必须**在已验证快照集内**(严格快照,不落"默认 chosen"分支;空/无关权益 → 空集 → 任何 surface 被拒)。
            validated = self._registry.sampling_surfaces_for_order(order_entitlement, snapshot=snap)
            if request.surface_key not in validated:
                raise SurfaceNotAllowedError(
                    f"surface {request.surface_key!r} 不在订单不可变快照 {validated} 内;拒绝采样(零 provider 调用)")
            # 订单显式购买表面按快照独立执行,不受当前默认 policy readiness 牵连(P1-1 订单独立)
        else:
            # 默认轮:readiness 闸(非 ready → 拒开闸,替换仅只读回放)+ policy **直接** chosen 放行(live 不替换)
            rd = self._registry.readiness(snap)
            if not rd.get("ready", False):
                raise PolicyNotReadyError(
                    f"policy readiness={rd.get('status')!r} 非 ready → 拒绝默认轮 live 采集(零 provider 调用);"
                    f"problems={rd.get('problems')} substitutions={rd.get('substitutions')}")
            if not self._registry.is_surface_allowed(request.surface_key, None, snapshot=snap):
                raise SurfaceNotAllowedError(
                    f"surface {request.surface_key!r} 非 policy 直接选定运行表面;拒绝采样(零 provider 调用)")

        adapter = self._registry.get_adapter(request.surface_key)
        limits = budget_limits_from_policy(snap)

        # 2.5) 单轮上限启用(契约 max_calls_per_round>0 恒真)→ 稳定 round_id 强制(复审#2-R6-2 P1-2):
        #      缺失 fail-closed,不自动生成随机 id(否则跨 batch/worker 不共享单轮上限 = 绕过)。
        if _round_cap_enabled(limits) and _round_id_missing(round_id):
            raise RoundContextRequiredError(
                f"max_calls_per_round={limits.max_calls_per_round}(启用)→ 采集必须携带稳定 round_id(非空);"
                f"缺失/空白 fail-closed(零 provider 调用)"
            )

        # 3) 重试上限**钳制**(复审 P1-2:请求正文不得超过 policy;取 min,防绕过预算硬限)
        policy_retry = limits.max_retry_calls_per_request
        effective_retry = min(int(request.max_retry_calls), policy_retry) \
            if request.max_retry_calls is not None else policy_retry
        if request.max_retry_calls != effective_retry:
            request = request.model_copy(update={"max_retry_calls": effective_retry})

        # 4) 原子预留(micros + 调用双上限;预留存共享账本 → 跨 worker 正确;数据不可读 → fail-closed)。
        #    预留 = **worst-case**(单次 × (1+最大重试)):硬上限,重试不越顶;finalize 释放预留并按
        #    真实 attempts 入账,故不永久压低日吞吐(仅在飞预留 worst-case)。
        token = None
        worst_case_calls = limits.max_calls_incl_retries
        projected = self._projected_increment_micros(request.surface_key) * worst_case_calls
        if self._ledger is not None:
            try:
                token = await asyncio.to_thread(
                    self._ledger.try_reserve, request.source_kind, projected, worst_case_calls,
                    surface_key=request.surface_key,
                    limit_micros=limits.max_cost_micros_per_day,
                    limit_calls=limits.max_calls_per_day,
                    request_id=request.request_id,
                    round_id=round_id,
                    limit_round_calls=limits.max_calls_per_round,   # P1-1:单轮 attempts 硬限进入真实执行链
                )
            except Exception as exc:  # 账本不可读 → fail-closed
                logger.error("[collect] 预算账本不可读 → budget_blocked(fail-closed): %s", exc)
                return adapter.budget_blocked_envelope(request, "budget data unavailable")
            if token == ALREADY_CHARGED_TOKEN:
                # 幂等:该 request_id 已采集/入账 → **绝不再发 provider 调用**(防未计费的重复调用)
                logger.info("[collect] duplicate request_id 已入账 → 不重复调用 provider: %s", request.request_id)
                return adapter.budget_blocked_envelope(request, "duplicate request_id already collected/charged")
            if token is None:
                logger.info("[collect] budget_blocked source=%s surface=%s round=%s(日/单轮 micros/调用上限)",
                            request.source_kind, request.surface_key, round_id)
                return adapter.budget_blocked_envelope(request, "daily/round budget or call cap reached")

        # 5) RPM 限速 + 每 provider 域并发 → 采集
        provider_domain = get_surface_spec(request.surface_key).provider_key
        try:
            await self._rpm.acquire(provider_domain)
            async with self._governor.slot(provider_domain):
                env = await adapter.collect(request)
        except BaseException:
            # provider 调用可能已发生 → finalize 保守 charge(P1-5:不丢账;calls 按 worst-case),再抛
            if token is not None:
                await asyncio.to_thread(
                    self._ledger.finalize, token, actual_micros=projected, actual_calls=worst_case_calls,
                    surface_key=request.surface_key, is_estimated=True)
            raise

        # 6) finalize 真实成本 + 真实 provider 调用数(含重试;耐久入账,request_id 幂等)。
        #    finalize 失败不崩业务结果,但**保留预留**(继续计入 effective,防低估);对账缺口告警。
        if token is not None:
            actual, _ = budget_charge_micros(env.usage)
            actual_calls = actual_calls_from_usage(request.surface_key, env.usage)
            try:
                await asyncio.to_thread(
                    self._ledger.finalize, token, actual_micros=actual, actual_calls=actual_calls,
                    surface_key=request.surface_key, is_estimated=env.usage.usage_is_estimated)
            except Exception as exc:
                logger.error("[collect] finalize 失败 → 预留保留(继续计入),对账缺口 source=%s surface=%s: %s",
                             request.source_kind, request.surface_key, exc)
        return env

    async def collect_batch(
        self,
        requests: Sequence[CollectionRequest],
        *,
        order_entitlement: Optional[OrderPlatformEntitlement] = None,
        round_id: Optional[str] = None,
    ) -> AsyncIterator[ObservationEnvelopeV1]:
        """并发采集;失败隔离(单表面错误不拖垮其他);按完成顺序 yield。重复采样是独立 observation。

        复审#2-R6-2 P1-2:一次 collect_batch = **一个采样轮**。单轮上限(契约 max_calls_per_round>0 恒真)
        启用时,**必须**由调用方(driver)传入**稳定的业务 round_id**(如订阅轮 / 研究轮 id);**绝不**自动
        生成随机 id —— 随机 id 会让同一逻辑轮跨 batch/worker 不共享账本 = 绕过上限。缺失 → fail-closed。
        四进程并发的同一逻辑轮传同一 round_id → 共享账本、总 attempts 不越 cap(与每日预算共享账本同理)。
        """
        # 上前置 fail-closed 闸(读一次 policy):
        try:
            snap = await asyncio.to_thread(self._registry.policy.get_policy)
        except Exception as exc:
            raise PolicyUnavailableError(f"策略不可读 → fail-closed: {exc}") from exc
        if order_entitlement is not None:
            # 订单批(复审#2-R6-3 P1-1):**完整校验不可变快照**(冲突/未知/不可用/缺 adapter → raise fail-closed,
            # 零 provider 调用),且请求 surface 集**严格等于**已验证快照集(无缺无多;重复采样按 surface 去重后比对)。
            validated = set(self._registry.sampling_surfaces_for_order(order_entitlement, snapshot=snap))
            requested = {r.surface_key for r in requests}
            if requested != validated:
                raise SurfaceNotAllowedError(
                    f"订单批请求 surface 集 {sorted(requested)} 与不可变快照 {sorted(validated)} 不一致;"
                    f"拒绝整批(零 provider 调用)")
        else:
            # 默认轮才受 policy readiness 牵连(P1-3);订单轮按不可变快照独立(P1-1)
            rd = self._registry.readiness(snap)
            if not rd.get("ready", False):
                raise PolicyNotReadyError(
                    f"policy readiness={rd.get('status')!r} 非 ready → 拒绝默认轮整轮采集(零 provider 调用)")
        if _round_cap_enabled(budget_limits_from_policy(snap)) and _round_id_missing(round_id):
            raise RoundContextRequiredError(
                "max_calls_per_round 启用 → collect_batch 必须由调用方传稳定 round_id(非空);缺失/空白 fail-closed(不自动生成)")

        async def _one(req: CollectionRequest) -> ObservationEnvelopeV1:
            try:
                return await self.collect_observation(
                    req, order_entitlement=order_entitlement, round_id=round_id)
            except (SurfaceNotAllowedError, IngestDisabledError, PolicyUnavailableError) as exc:
                return _build_error_envelope(req, str(exc))
            except Exception as exc:
                return _build_error_envelope(req, f"{type(exc).__name__}: {exc}")

        tasks = [asyncio.create_task(_one(r)) for r in requests]
        for coro in asyncio.as_completed(tasks):
            yield await coro


# ---------------------------------------------------------------------------
# 进程默认服务(集成者启动时注入真实 policy/账本;默认 fail-closed,不自动 fake)
# ---------------------------------------------------------------------------

_default_service: Optional[CollectionService] = None


def build_default_service(policy, ledger, *, governor=None, rpm_limiter=None) -> CollectionService:
    """构造服务。**必须**显式传入真实 policy(AI-2 reader)与真实 micros 账本 ledger。

    复审 P1-2:不再自动启用 fake policy + 真 adapter + 无账本。测试用 fake 只能显式注入。
    """
    if policy is None or ledger is None:
        raise NotConfiguredError("build_default_service 需要显式的 policy 与 ledger(真实采集不接受缺省 fake)")
    registry = AdapterRegistry(policy)
    return CollectionService(registry, ledger=ledger, governor=governor, rpm_limiter=rpm_limiter)


def configure_default_service(service: CollectionService) -> None:
    """集成者在启动时注入配置好的服务实例,供模块级便捷函数使用。"""
    global _default_service
    _default_service = service


def _svc() -> CollectionService:
    if _default_service is None:
        raise NotConfiguredError(
            "进程默认采集服务未接线;集成者须先 configure_default_service(build_default_service(真实 policy, 真实账本))"
        )
    return _default_service


# ---- §9.1 模块级便捷函数(委托给已接线的进程默认服务;未接线则 fail-closed) ----
async def collect_observation(request: CollectionRequest, *,
                              order_entitlement: Optional[OrderPlatformEntitlement] = None,
                              round_id: Optional[str] = None) -> ObservationEnvelopeV1:
    return await _svc().collect_observation(request, order_entitlement=order_entitlement, round_id=round_id)


async def collect_paid_delivery(request: CollectionRequest, *,
                                round_id: Optional[str] = None) -> ObservationEnvelopeV1:
    return await _svc().collect_paid_delivery(request, round_id=round_id)


async def collect_batch(requests: Sequence[CollectionRequest], *,
                        order_entitlement: Optional[OrderPlatformEntitlement] = None,
                        round_id: Optional[str] = None) -> AsyncIterator[ObservationEnvelopeV1]:
    async for env in _svc().collect_batch(requests, order_entitlement=order_entitlement, round_id=round_id):
        yield env


def get_surface_matrix() -> SurfaceMatrix:
    return _svc().get_surface_matrix()


async def get_adapter_health(only_active: bool = True) -> list[SurfaceHealth]:
    return await _svc().get_adapter_health(only_active=only_active)
