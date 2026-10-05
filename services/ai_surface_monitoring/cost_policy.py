"""成本护栏(§6.3 / Codex #5,#6 / 复审 P1-3,P1-4,P1-5,P2-8)。

原则:
  - **整数 micros only**(1 元 = 1_000_000 micros);全程 int,只在显示/对账边界 ÷1e6。
  - **成本版本化**(Codex #5):Hy3 按官方 2026-07-16 价格 token 计费,不用固定每次成本。
  - **未知成本写 null**(Codex #6):估算未知时 estimated_cost_micros=None,绝不假 0;预算 fail-closed。
  - **真实重试计费**(P1-4):attempts>1 时按真实 provider 调用次数计费,不低估。
  - **跨 worker 原子预算**(P1-3/P1-5):预留(reservation)存在**共享账本**里,不在进程内字典。
    ``try_reserve`` 原子准入(micros + call 双上限);``finalize`` 耐久入账(request_id 幂等);
    ``release`` 取消预留。付费调用即使后续信封构建异常,也 finalize(charge)不丢账。
  - **每平台每分钟真实限速**(P2-8):RpmLimiter 令牌节流(不只是并发 Semaphore)。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Protocol

from services.ai_surface_monitoring.contracts import UsageEvidence

logger = logging.getLogger("GEO-AISurface-Cost")

MICROS_PER_YUAN = 1_000_000
PRICING_VERSION = "2026-07-16"


# ---------------------------------------------------------------------------
# 价目规格(整数 micros)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenPricing:
    input_micros_per_mtok: int
    output_micros_per_mtok: int
    cache_micros_per_mtok: int


@dataclass(frozen=True)
class FlatPricing:
    micros_per_call: int
    exact: bool


PRICING_TABLE: Dict[str, object] = {
    "yuanbao_hy3_tokenhub": TokenPricing(1_000_000, 4_000_000, 250_000),  # 官方 2026-07-16
    "doubao_ark_api_search": FlatPricing(micros_per_call=200_000, exact=True),  # ai_search ¥0.2 flat
    "qwen_dashscope_search": FlatPricing(micros_per_call=50_000, exact=False),
    "deepseek_native_no_search": FlatPricing(micros_per_call=50_000, exact=False),
    "deepseek_dashscope_search_legacy": FlatPricing(micros_per_call=50_000, exact=False),
    "other_explicit": FlatPricing(micros_per_call=80_000, exact=False),
}

_CONSERVATIVE_FALLBACK_MICROS = 200_000


def estimate_usage_cost(
    surface_key: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cached_tokens: Optional[int] = 0,
    attempts: int = 1,
) -> UsageEvidence:
    """按版本化价目计算 UsageEvidence。attempts 为真实 provider 调用次数(含重试;P1-4)。

    estimated_cost_micros = 单次成本 × attempts(总 provider 花费);未知 → None(Codex #6)。
    """
    attempts = max(int(attempts or 1), 1)
    cached = int(cached_tokens or 0)
    pricing = PRICING_TABLE.get(surface_key)

    single_micros: Optional[int]
    is_estimated: bool
    if isinstance(pricing, TokenPricing):
        if input_tokens is None or output_tokens is None:
            single_micros, is_estimated = None, True  # 缺 in 或 out → 不可知
        else:
            inp = int(input_tokens or 0)
            out = int(output_tokens or 0)
            cached = min(max(cached, 0), max(inp, 0))
            uncached_input = max(inp - cached, 0)
            single_micros = (
                uncached_input * pricing.input_micros_per_mtok
                + cached * pricing.cache_micros_per_mtok
                + out * pricing.output_micros_per_mtok
            ) // 1_000_000
            is_estimated = True  # token 计税对真实账单仍是估算
    elif isinstance(pricing, FlatPricing):
        single_micros, is_estimated = pricing.micros_per_call, not pricing.exact
    else:
        single_micros, is_estimated = None, True

    total = None if single_micros is None else int(single_micros) * attempts
    return UsageEvidence(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_micros=total,
        usage_is_estimated=is_estimated,
    )


def budget_charge_micros(usage: UsageEvidence) -> tuple[int, bool]:
    """把 UsageEvidence 折算成预算应计 micros。未知(None)→ 保守回落(绝不 0),防低估(Codex #6)。"""
    if usage.estimated_cost_micros is None:
        return _CONSERVATIVE_FALLBACK_MICROS, True
    return int(usage.estimated_cost_micros), False


def actual_calls_from_usage(surface_key: str, usage: UsageEvidence) -> int:
    """从 UsageEvidence 反推真实 provider 调用次数(复审 P1-3:调用上限按 attempts 计,非业务行数)。

    flat 表面(doubao/qwen/deepseek-legacy/kimi):成本 = 单价 × attempts → attempts = 成本 // 单价;
    token 表面(hy3/deepseek-native,openai adapter 单次不内部重试):恒 1。
    """
    pricing = PRICING_TABLE.get(surface_key)
    if isinstance(pricing, FlatPricing) and pricing.micros_per_call > 0 and usage.estimated_cost_micros:
        return max(1, int(usage.estimated_cost_micros) // pricing.micros_per_call)
    return 1


# ---------------------------------------------------------------------------
# 预算上限(来自 policy sampling_budget)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLimits:
    max_cost_micros_per_day: Optional[int] = None
    max_calls_per_day: Optional[int] = None
    max_retry_calls_per_request: int = 2
    max_calls_per_round: Optional[int] = None   # 复审#2-R6 P1-1:单轮 provider 调用(attempts)硬上限

    @property
    def max_calls_incl_retries(self) -> int:
        return 1 + max(int(self.max_retry_calls_per_request or 0), 0)


# ---------------------------------------------------------------------------
# 每平台并发上限 + 每分钟真实限速
# ---------------------------------------------------------------------------

DEFAULT_CONCURRENCY_PER_PLATFORM = 8


MAX_CONCURRENCY_PER_PLATFORM = 64


def _safe_int_env(name: str, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            logger.warning("[cost_policy] env %s=%r 非法整数,回落默认 %d", name, raw, default)
            value = default
    if minimum is not None:
        value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


class ConcurrencyGovernor:
    """按 rate-limit 域(provider)维度的并发闸(每域各一 Semaphore)。"""

    def __init__(self, per_platform: Optional[int] = None):
        base = per_platform if per_platform is not None else DEFAULT_CONCURRENCY_PER_PLATFORM
        self._n = _safe_int_env(
            "AI_SURFACE_CONCURRENCY_PER_PLATFORM", default=base, minimum=1, maximum=MAX_CONCURRENCY_PER_PLATFORM)
        self._sems: Dict[str, asyncio.Semaphore] = {}

    @property
    def per_platform(self) -> int:
        return self._n

    def slot(self, domain: str) -> asyncio.Semaphore:
        sem = self._sems.get(domain)
        if sem is None:
            sem = asyncio.Semaphore(self._n)
            self._sems[domain] = sem
        return sem


def default_provider_rpm() -> Dict[str, int]:
    """各供应商的默认 RPM 上限(env 可覆盖)。未登记的供应商 = 不限(保持既有行为)。

    [C · 2026-07-27] QPS 限的是**每秒发起数**,不是在飞数量,所以对它生效的工具是
    RpmLimiter(平滑令牌节流),不是并发 Semaphore。
    腾讯 TokenHub 联网搜索官方限制 **5 QPS**(文档《使用限制》)= 300 RPM;
    默认取 240 RPM(4 QPS)留 20% 余量,避免与重试/多进程叠加后正好压线。
    👉 提额渠道:腾讯云控制台提工单(大模型服务平台 TokenHub)申请调高 QPS;
       调高后改 env AI_SURFACE_RPM_TENCENT_TOKENHUB 即可,不必改代码。

    [复检返修 · 2026-07-27] 原来这段注释挂在一个空的 DEFAULT_PROVIDER_RPM = {} 死常量上,
    容易让人以为默认值写在那里。常量已删,说明并入本函数。
    """
    return {
        "tencent_tokenhub": _safe_int_env(
            "AI_SURFACE_RPM_TENCENT_TOKENHUB", default=240, minimum=1, maximum=100000),
    }


class RpmLimiter:
    """每 rate-limit 域(provider)每分钟真实限速(平滑令牌节流,不只是并发上限;P2-8)。

    最小调用间隔 = 60/(rpm/worker_count) 秒;rpm=None/<=0 表示不限。并发下 per-domain 锁排期,sleep 在锁外。

    **WORKERS=4 共享 RPM**(复审 P1-8):账户级 RPM 是**跨进程**上限,而本限速器是进程内状态,
    N 个 worker 各限 rpm 会累计成 N×rpm。故用 ``worker_count``(默认取 env WORKERS)把每进程配额降为
    rpm/worker_count,合计 ≈ rpm(近似,负载均衡假设下)。**严格**跨进程 RPM 需共享 store(Redis/DB)
    令牌桶 —— 属集成层,已在回包列为残留;本限速器把每进程配额确定性收敛,避免 N 倍膨胀。
    """

    def __init__(self, rpm_by_domain: Optional[Dict[str, int]] = None, default_rpm: Optional[int] = None,
                 worker_count: Optional[int] = None):
        self._rpm = dict(rpm_by_domain or {})
        self._default = default_rpm
        self._workers = worker_count if worker_count is not None else _safe_int_env("WORKERS", default=1, minimum=1)
        self._next: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _rpm_for(self, domain: str) -> Optional[int]:
        return self._rpm.get(domain, self._default)

    def _per_worker_rpm(self, domain: str) -> Optional[float]:
        rpm = self._rpm_for(domain)
        if not rpm or rpm <= 0:
            return None
        return rpm / max(self._workers, 1)  # 每进程配额 = 账户 RPM / worker 数(合计 ≈ 账户 RPM)

    async def acquire(self, domain: str) -> None:
        per_worker = self._per_worker_rpm(domain)
        if per_worker is None:
            return
        interval = 60.0 / per_worker
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            nxt = self._next.get(domain, now)
            wait = max(0.0, nxt - now)
            self._next[domain] = max(now, nxt) + interval
        if wait > 0:
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# 原子预留账本(跨 worker 正确性核心;P1-3/P1-5)
# ---------------------------------------------------------------------------


class BudgetDataUnavailableError(Exception):
    """账本读/写不可用 → 调用方必须 fail-closed,绝不以 0 成本继续。"""


# 预留生命周期上界:单次采集(含重试)是分钟级;超过即视为崩溃 worker 的孤儿,停止计入并回收。
RESERVATION_TTL_SECONDS = 900  # 15 min


@dataclass(frozen=True)
class Reservation:
    token: str
    source_kind: str
    surface_key: str
    micros: int
    calls: int          # 预留的**调用**数(worst-case = 1+最大重试;按真实 provider attempts 消耗)
    request_id: str
    created_at: float  # time.monotonic()
    round_id: Optional[str] = None   # 复审#2-R6 P1-1:所属采样轮(用于 max_calls_per_round 跨 worker 硬限)


class BudgetLedger(Protocol):
    """跨 worker 原子预算账本。预留状态存在账本内(共享),非进程内字典。

    - try_reserve:原子准入。effective(micros/calls)= 已花 + **TTL 内**在途预留,放得下才占位返回 token;
      否则 None(budget_blocked)。数据不可读 → raise(fail-closed)。request_id 幂等:已 finalize 过的
      request_id 直接返回哨兵 ALREADY_CHARGED_TOKEN(调用方须据此**不再发 provider 调用**)。
    - finalize:把预留转为真实入账(actual_micros),幂等(request_id 唯一)。
    - release:取消预留(未发生 provider 调用时)。
    - reap_stale_reservations:回收超过 TTL 的孤儿预留 —— 保守按预留额 finalize(charge)后删除,
      避免 worker 崩溃/ finalize 失败留下永久幽灵额度把预算饿死(自愈)。
    """

    def try_reserve(self, source_kind: str, projected_micros: int, projected_calls: int, *, surface_key: str,
                    limit_micros: Optional[int], limit_calls: Optional[int], request_id: str,
                    round_id: Optional[str] = None, limit_round_calls: Optional[int] = None) -> Optional[str]:
        ...

    def finalize(self, token: str, *, actual_micros: int, actual_calls: int, surface_key: str,
                 is_estimated: bool) -> None:
        ...

    def release(self, token: str) -> None:
        ...

    def spent_micros_today(self, source_kind: str) -> int:
        ...

    def calls_today(self, source_kind: str) -> int:
        ...

    def reap_stale_reservations(self, ttl_seconds: int = RESERVATION_TTL_SECONDS) -> int:
        ...


# 幂等命中哨兵:该 request_id 已入账 → 调用方**不得**再发 provider 调用,finalize/release 对它 no-op。
ALREADY_CHARGED_TOKEN = "__already_charged__"


class InMemoryBudgetLedger:
    """线程安全内存账本(测试 + 多 service 实例共享以验证跨实例原子性)。

    ``fail_reads=True`` 模拟读失败以验证 fail-closed。用 threading.Lock(经 asyncio.to_thread
    在真实线程中调用),两个 service 实例共享同一 ledger 时序列化,真实复现并修复"两实例各扣满上限"。
    预留带 TTL:超期不再计入准入,并可被 reap 回收为保守 charge(自愈孤儿)。
    """

    def __init__(self, fail_reads: bool = False):
        self._lock = threading.Lock()
        self._spent_micros: Dict[str, int] = {}
        self._calls: Dict[str, int] = {}
        self._round_calls: Dict[str, int] = {}   # round_id -> 已入账 provider attempts(P1-1 单轮硬限)
        self._reservations: Dict[str, Reservation] = {}
        self._finalized_request_ids: set = set()
        self.records: list = []
        self.fail_reads = fail_reads

    def _live(self, r: Reservation, now: float, ttl: int = RESERVATION_TTL_SECONDS) -> bool:
        return (now - r.created_at) < ttl

    def _reserved_micros(self, source_kind: str, now: float) -> int:
        return sum(r.micros for r in self._reservations.values()
                   if r.source_kind == source_kind and self._live(r, now))

    def _reserved_calls(self, source_kind: str, now: float) -> int:
        return sum(r.calls for r in self._reservations.values()
                   if r.source_kind == source_kind and self._live(r, now))

    def _has_live_reservation_for_request(self, request_id: str, now: float) -> bool:
        return any(r.request_id == request_id and self._live(r, now)
                   for r in self._reservations.values())

    def _reserved_round_calls(self, round_id: str, now: float) -> int:
        return sum(r.calls for r in self._reservations.values()
                   if r.round_id == round_id and self._live(r, now))

    def try_reserve(self, source_kind: str, projected_micros: int, projected_calls: int, *, surface_key: str,
                    limit_micros: Optional[int], limit_calls: Optional[int], request_id: str,
                    round_id: Optional[str] = None, limit_round_calls: Optional[int] = None) -> Optional[str]:
        if self.fail_reads:
            raise BudgetDataUnavailableError("simulated ledger read failure")
        with self._lock:
            if request_id in self._finalized_request_ids:
                return ALREADY_CHARGED_TOKEN  # 幂等:该业务请求已扣过 → 调用方不得再发 provider 调用
            now = time.monotonic()
            # 复审 P1-1:同 request_id **在途**预留已存在 → 拒绝(防并发下同 ID 双发 provider、只记一笔)
            if self._has_live_reservation_for_request(request_id, now):
                return None
            eff_micros = self._spent_micros.get(source_kind, 0) + self._reserved_micros(source_kind, now)
            eff_calls = self._calls.get(source_kind, 0) + self._reserved_calls(source_kind, now)
            pc = max(int(projected_calls), 1)
            if limit_micros is not None and eff_micros + int(projected_micros) > limit_micros:
                return None
            if limit_calls is not None and eff_calls + pc > limit_calls:
                return None
            # 复审#2-R6 P1-1:单轮 provider 调用(attempts)硬限 —— 已入账 + 在途预留(worst-case)+ 本次 ≤ cap。
            # 预留存共享账本 → 四进程同 round_id 也不越顶;finalize 按真实 attempts 结算释放。
            # 复审#2-R6-R2:``limit_round_calls <= 0`` 视为**不限**(与 cap_round_calls / RpmLimiter 的 <=0 约定一致,
            # 避免 policy 用 0 表达"禁用单轮限"时反而整轮全阻断)。
            if round_id is not None and limit_round_calls is not None and int(limit_round_calls) > 0:
                eff_round = self._round_calls.get(round_id, 0) + self._reserved_round_calls(round_id, now)
                if eff_round + pc > limit_round_calls:
                    return None
            token = uuid.uuid4().hex
            self._reservations[token] = Reservation(
                token, source_kind, surface_key, int(projected_micros), pc, request_id, now, round_id)
            return token

    def _charge_locked(self, res: Reservation, actual_micros: int, actual_calls: int,
                       surface_key: str, is_estimated: bool) -> None:
        if res.request_id in self._finalized_request_ids:
            return
        self._finalized_request_ids.add(res.request_id)
        calls = max(int(actual_calls), 1)
        self._spent_micros[res.source_kind] = self._spent_micros.get(res.source_kind, 0) + int(actual_micros)
        self._calls[res.source_kind] = self._calls.get(res.source_kind, 0) + calls
        if res.round_id is not None:  # P1-1:真实 attempts 计入单轮账,释放 worst-case 预留头寸
            self._round_calls[res.round_id] = self._round_calls.get(res.round_id, 0) + calls
        self.records.append({"source_kind": res.source_kind, "surface_key": surface_key,
                             "micros": int(actual_micros), "calls": calls,
                             "round_id": res.round_id, "is_estimated": is_estimated})

    def finalize(self, token: str, *, actual_micros: int, actual_calls: int, surface_key: str,
                 is_estimated: bool) -> None:
        if token == ALREADY_CHARGED_TOKEN:
            return
        with self._lock:
            res = self._reservations.pop(token, None)
            if res is None:
                return  # 已 finalize/release(幂等)
            self._charge_locked(res, actual_micros, actual_calls, surface_key, is_estimated)

    def release(self, token: str) -> None:
        if token == ALREADY_CHARGED_TOKEN:
            return
        with self._lock:
            self._reservations.pop(token, None)

    def reap_stale_reservations(self, ttl_seconds: int = RESERVATION_TTL_SECONDS) -> int:
        """回收超期孤儿预留:保守按预留额(micros + calls)charge 后删除(provider 可能已调用,宁可多算不漏)。"""
        with self._lock:
            now = time.monotonic()
            stale = [r for r in self._reservations.values() if (now - r.created_at) >= ttl_seconds]
            for r in stale:
                self._reservations.pop(r.token, None)
                self._charge_locked(r, r.micros, r.calls, r.surface_key, is_estimated=True)
            return len(stale)

    def spent_micros_today(self, source_kind: str) -> int:
        if self.fail_reads:
            raise BudgetDataUnavailableError("simulated ledger read failure")
        with self._lock:
            return self._spent_micros.get(source_kind, 0)

    def calls_today(self, source_kind: str) -> int:
        if self.fail_reads:
            raise BudgetDataUnavailableError("simulated ledger read failure")
        with self._lock:
            return self._calls.get(source_kind, 0)

    def calls_in_round(self, round_id: str) -> int:
        if self.fail_reads:
            raise BudgetDataUnavailableError("simulated ledger read failure")
        with self._lock:
            return self._round_calls.get(round_id, 0)
