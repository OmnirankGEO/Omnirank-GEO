"""多源限流轮询池(并发-2 · 2026-06-10 打广告高并发准备)

把外部数据 API(metaso / 5118 / 未来 DataForSEO 等)的「多 key / 多账号 / 多供应商」
统一抽象成「源池」:
  - round-robin 轮询多个 key(一个 key/账号 = 一个"源")
  - 每源独立 QPM 令牌桶限速(防打爆上游·metaso 200/QPM 这类硬限)
  - 超速【非阻塞降级】:无配额时 pick_key() 返回 None,调用方走经验兜底,**不排队**(老板要求)

env 配置(逗号/分号分隔多 key 即自动轮询·向后兼容单 key):
  - METASO_API_KEYS=k1,k2,k3   (回落 METASO_API_KEY)        每源 QPM 默认 200
  - API_5118_KEYS=k1,k2        (回落 API_5118_SEARCH_VOLUME_KEY)  每源 QPM 默认 600
  - METASO_QPM_PER_KEY / API_5118_QPM_PER_KEY 可覆盖每源 QPM

老板收集到多账号/多 API 的 key 后,填进对应 *_KEYS 即自动分流,**代码零改动**。
多账号总配额 = 单源 QPM × 账号数(如 metaso 3 账号 = 600/QPM)。
"""
from __future__ import annotations

import os
import time
import threading
from typing import List, Optional


class _TokenBucket:
    """QPM 令牌桶 · 线程安全 · 非阻塞(无 token 返 False,调用方降级不排队)。"""

    def __init__(self, qpm: int):
        self.capacity = float(max(1, qpm))
        self.tokens = self.capacity
        self.rate = self.capacity / 60.0  # tokens / 秒
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
            self._last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class _AsyncRateGate:
    """每账号 QPS 平滑限速闸 · async 等待排队(不丢请求·不降级)。[round-robin+限速 2026-06-11]

    用于 5118 这类「不返 429·超速则报错(每秒调用量超限)」的源:把高并发请求平滑到 qps 以内。
    令牌桶:速率 = qps tokens/秒 · 突发容量 = burst(默认 max(1,qps))。
    无 token 时 async sleep 轮询等待至 max_wait 秒 · 超时返 False(调用方仍可尽力发·限速只平滑不硬阻断·防限速器异常卡死请求)。
    与 _TokenBucket(QPM·非阻塞降级·metaso 算价用)互不影响。
    """

    def __init__(self, qps: float, burst: Optional[float] = None):
        self.rate = float(max(0.1, qps))                              # tokens / 秒
        self.capacity = float(burst) if burst else float(max(1.0, qps))
        self.tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _try(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
            self._last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    async def acquire(self, max_wait: float = 30.0) -> bool:
        import asyncio
        if self._try():
            return True
        deadline = time.monotonic() + max(0.0, max_wait)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.03)
            if self._try():
                return True
        return False


def _parse_keys(*env_names: str) -> List[str]:
    """从一组 env 变量解析 key 列表(逗号/分号分隔 · 去重 · 保序 · 第一个非空变量优先)。"""
    keys: List[str] = []
    seen = set()
    for name in env_names:
        val = os.getenv(name, "")
        if not val:
            continue
        for part in val.replace(";", ",").split(","):
            k = part.strip()
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
        if keys:
            break  # 用第一个有值的变量(多 key 复数变量优先于单 key)
    return keys


class SourcePool:
    """一组源(key/账号)的 round-robin 轮询 + 每源 QPM 限速。

    pick_key(): 返回一个当前有配额的 key;全部超速/无源 → None(调用方降级·不排队)。
    """

    def __init__(self, name: str, keys: List[str], qpm_per_source: int,
                 primary_key: Optional[str] = None, primary_qpm: Optional[int] = None,
                 qps_per_source: Optional[float] = None, qps_burst: Optional[float] = None):
        self.name = name
        keys = [k for k in (keys or []) if k]
        # [primary-first 2026-06-11] 主 key(老板给原 key 加过 QPS)永远排 keys[0]·去重保序
        primary_key = (primary_key or "").strip() or None
        if primary_key:
            keys = [primary_key] + [k for k in keys if k != primary_key]
        self.keys = keys
        self.primary_key = self.keys[0] if (primary_key and self.keys) else None
        # 主 key 可单独配更大 QPM 桶(如 METASO_PRIMARY_QPM)反映加过 QPS 的高配额;其余每源默认桶
        self._buckets = {}
        for k in self.keys:
            q = primary_qpm if (self.primary_key and k == self.primary_key and primary_qpm) else qpm_per_source
            self._buckets[k] = _TokenBucket(q)
        self._rr = 0
        self._lock = threading.Lock()
        # [round-robin+限速 2026-06-11] per-key QPS 平滑限速闸(懒建)·仅配 qps_per_source 的源(5118)启用·metaso 不受影响
        self._qps_per_source = qps_per_source
        self._qps_burst = qps_burst
        self._rate_gates: dict = {}
        self._rate_lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self.keys)

    def pick_key(self, throttle: bool = True, primary_first: bool = False, attempt: int = 0) -> Optional[str]:
        """取一个 key。
        primary_first=False(默认): round-robin 轮询(全源均摊·deepseek/原有行为不变)。
        primary_first=True: 主 key 优先 —— attempt 当尝试序号按 [主, 备用1, 备用2 ...] 顺序取;
          throttle 时该 key 超速则返 None(调用方 attempt+1 试下一个·见 acall_with_failover)。
          老板诉求:原 key(加过 QPS)优先吃满·满/失败才 fallback 到备用。"""
        if not self.keys:
            return None
        n = len(self.keys)
        if primary_first:
            if attempt < 0 or attempt >= n:
                return None
            k = self.keys[attempt]
            if not throttle:
                return k  # 5118 类「不拒绝只慢」:主 key 一直优先·失败靠 failover 换下一个
            return k if self._buckets[k].try_acquire() else None  # 超速 → 调用方试下一个备用
        with self._lock:
            start = self._rr
            self._rr = (self._rr + 1) % n
        if not throttle:
            # 纯 round-robin·不限速:用于「不拒绝只慢」的源(5118)·并发由调用侧全局 Semaphore 控
            return self.keys[start]
        for i in range(n):
            k = self.keys[(start + i) % n]
            if self._buckets[k].try_acquire():
                return k
        return None  # 全部源超速 → 调用方降级

    async def acquire_round_robin_throttled(self, max_wait: float = 30.0):
        """round-robin 取一个账号 + async 平滑限速等待该账号 QPS 配额(限速排队·不丢请求)。[2026-06-11]
        返回 (key, ok):
          - round-robin(_rr 自增)→ 高并发分流到各账号均摊(压测证 5118 线性叠加);
          - 每账号独立 QPS 闸 → 平滑到 qps 以内·不撞 5118「每秒调用量超限」;
          - ok=False = 限速等待超时(max_wait)·调用方仍可用该 key 尽力发(限速只平滑不硬阻断·防限速器异常卡死请求);
          - 未配 qps_per_source → 不限速·直接返回 round-robin key(向后兼容)。
        配合上层 failover 循环(每次换下一个 round-robin 账号)实现 分流+限速+容错 三合一。"""
        if not self.keys:
            return None, True
        with self._lock:
            start = self._rr
            self._rr = (self._rr + 1) % len(self.keys)
        key = self.keys[start]
        if not self._qps_per_source:
            return key, True
        gate = self._rate_gates.get(key)
        if gate is None:
            with self._rate_lock:
                gate = self._rate_gates.get(key)
                if gate is None:
                    gate = _AsyncRateGate(self._qps_per_source, self._qps_burst)
                    self._rate_gates[key] = gate
        ok = await gate.acquire(max_wait=max_wait)
        return key, ok


# ========== 各数据源全局池(懒加载单例 · 进程内共享) ==========
_pools: dict = {}
_pools_lock = threading.Lock()


def _get_pool(name: str, factory) -> SourcePool:
    pool = _pools.get(name)
    if pool is None:
        with _pools_lock:
            pool = _pools.get(name)
            if pool is None:
                pool = factory()
                _pools[name] = pool
    return pool


def get_metaso_pool() -> SourcePool:
    """metaso 源池:METASO_API_KEYS(多 key)回落 METASO_API_KEY · 每源默认 200 QPM。"""
    return _get_pool(
        "metaso",
        lambda: SourcePool(
            "metaso",
            _parse_keys("METASO_API_KEYS", "METASO_API_KEY"),
            qpm_per_source=int(os.getenv("METASO_QPM_PER_KEY", "200")),
            # [primary-first] 原 key(加过 QPS)作主·优先吃满;METASO_PRIMARY_QPM 配主 key 高配额桶
            primary_key=os.getenv("METASO_API_KEY", ""),
            primary_qpm=int(os.getenv("METASO_PRIMARY_QPM", "0") or "0") or None,
        ),
    )


def get_5118_pool() -> SourcePool:
    """[兼容保留] 5118 搜索量源池别名 · = get_5118_search_pool()。"""
    return get_5118_search_pool()


def get_5118_search_pool() -> SourcePool:
    """5118 搜索量信息源池:API_5118_KEYS(多 key)回落 API_5118_SEARCH_VOLUME_KEY。
    5118 不返 429·只慢 → 调用方用 pick_key(throttle=False) 纯轮询·并发由 _5118_semaphore 控。"""
    return _get_pool(
        "5118_search",
        lambda: SourcePool(
            "5118_search",
            _parse_keys("API_5118_KEYS", "API_5118_SEARCH_VOLUME_KEY"),
            qpm_per_source=int(os.getenv("API_5118_QPM_PER_KEY", "600")),
            # [round-robin+限速 2026-06-11] 5118 多账号 round-robin 均摊(压测证线性叠加)·原 key 仍排 keys[0] 进轮询序列
            primary_key=os.getenv("API_5118_SEARCH_VOLUME_KEY", ""),
            qps_per_source=float(os.getenv("API_5118_QPS_PER_KEY", "3")),   # 每账号 QPS 平滑限速·防撞「每秒调用量超限」
            qps_burst=float(os.getenv("API_5118_QPS_BURST", "3")),
        ),
    )


def get_5118_longtail_pool() -> SourcePool:
    """5118 海量长尾词挖掘源池(多账号):API_5118_LONGTAIL_KEYS(逗号分隔多 key)回落 API_5118_LONGTAIL_KEY。
    多账号自动轮询分流 · 老板填多账号 longtail key 进 API_5118_LONGTAIL_KEYS 即生效·代码零改。"""
    return _get_pool(
        "5118_longtail",
        lambda: SourcePool(
            "5118_longtail",
            _parse_keys("API_5118_LONGTAIL_KEYS", "API_5118_LONGTAIL_KEY"),
            qpm_per_source=int(os.getenv("API_5118_QPM_PER_KEY", "600")),
            # [round-robin+限速 2026-06-11] 5118 长尾多账号 round-robin 均摊·原 key 仍排 keys[0] 进轮询序列
            primary_key=os.getenv("API_5118_LONGTAIL_KEY", ""),
            qps_per_source=float(os.getenv("API_5118_QPS_PER_KEY", "3")),   # 每账号 QPS 平滑限速·防撞「每秒调用量超限」
            qps_burst=float(os.getenv("API_5118_QPS_BURST", "3")),
        ),
    )


# ========== failover 助手:某账号请求失败 → 自动换下一个账号重试(双保险:分流 + 容错) ==========
async def acall_with_failover(pool: "SourcePool", do_request, throttle: bool = False,
                              fallback_key: Optional[str] = None, primary_first: bool = False):
    """对 pool 内 key 依次 failover · 异步。
      do_request(key) -> result:成功返回结果;失败必须 raise(本助手据此换下一个 key 重试)。
      逐个取 key 调 do_request·返回首个成功结果;某 key raise → 换下一个;
      试遍所有账号仍失败 → raise 最后一次异常(调用方 except 走降级)。
    throttle: 5118 类「不拒绝只慢」用 False(纯轮询);metaso 类带 QPM 闸用 True(跳过超速账号)。
    primary_first: True=主 key(原 key·加过 QPS)优先·满/失败才轮备用(老板诉求);False=round-robin 均摊(原行为)。
    fallback_key: pool 为空时用的兜底单 key(向后兼容未配多 key 环境)。"""
    n = pool.size
    if n == 0:
        if fallback_key:
            return await do_request(fallback_key)
        raise RuntimeError("%s pool empty · 无可用 key" % pool.name)
    last_exc = None
    attempts = 0
    for attempt in range(n):
        key = pool.pick_key(throttle=throttle, primary_first=primary_first, attempt=attempt)
        if key is None:
            if primary_first:
                continue  # primary-first:该 attempt 的 key 超速 → 试下一个备用(不提前退出)
            break  # round-robin:pick_key 已内部遍历·None = 全部账号超速 → 无可用
        attempts += 1
        try:
            return await do_request(key)
        except Exception as e:
            last_exc = e
            continue  # 该账号失败 → 自动换下一个账号
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("%s 全部 %d 账号超速·无可用 key" % (pool.name, n))
