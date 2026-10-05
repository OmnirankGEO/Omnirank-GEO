"""证据检索供应商 · 连续「未送达」熔断 + 告警。

[WO 熔断单 2026-08-08 · 发车前置 · 升级自 G 单]

## 存在的理由(生产实证,不是设想)

一轮写作里 **454 笔调用、¥17.24、买到 0 条证据**。机制上完全解释得通,三段叠加:

1. **计费与成功无关** —— `tools/llm_call_tracker.llm_track()` 在 ``finally`` 里
   无条件写一行 `llm_call_log`,`flat_rate_per_call` 型(秘塔 ¥0.05 / 豆包 ¥0.02)
   的 `estimate_cost()` 不看 `success`。**打一枪就记一笔,不管有没有打中。**
2. **每次重试各记一笔** —— 秘塔 `max_retries=2`、豆包 `for attempt in (1,2)`,
   两边的 `llm_track` 都在**重试循环内部**,一次逻辑调用最多两笔。
3. **失败不阻断** —— 证据检索失败只让 `writing/article_length_contract` 走
   `verified == 0 → _compact("verified_evidence_absent_allow_shorter")`,
   文章照样出。**所以"确定性失败还在烧钱"这件事表面上看不出来**,
   要等有人去翻 `llm_call_log` 才发现。

三段单独都合理,叠在一起 = 供应商完全连不上时,并发 10 篇 × 每篇 4-8 query ×
每 query 2 次重试,几百笔就这么烧出来了。

## 这个模块只做一件事

**在真正发出 HTTP 之前**判断"这个供应商刚刚连续 N 次根本没送达",是就直接跳过 ——
跳过 = 不进 `llm_track` = **不产生那一笔成本**。

🔴 判据只认「确定未送达」那一类异常,**不认业务失败**:
HTTP 打通了但对方说"余额不足"、返回空结果 —— 那是**送达**的,该计的费就该计,
也不该因此停掉整个供应商(那是 `tools/metaso_health` 管的另一件事,见下)。

## 与既有两套设施的关系(不造第二套)

===============================================  ==========================================
既有                                             本模块与它的关系
===============================================  ==========================================
``services/research_monitor/circuit_breaker``    **直接复用**它的计数器(见 `_new_counter`),
  `CircuitBreaker`(纯连续/失败率计数器)         不另写一份 consecutive 累加
``services/marketing/image_client``              **直接复用**它的「没送出去」异常集合 ——
  `_connect_error_types()`                       那份 docstring 自称是**唯一**的一份,
                                                 而且明写"改这里 = 改计费安全边界"
``db/ai_ops_db``                                 **直接复用** `upsert_alert` /
  `upsert_alert` / `resolve_alerts`              `resolve_alerts`(同 `metaso_health`、
                                                 `flywheel_heartbeat` 的既有惯例)
``tools/metaso_health``                          **不动它,也不合并**。它管的是
  (秘塔连续 20 次**业务级**失败 → 只告警)       「HTTP 200 但 errCode≠0」这一类,
                                                 触发点在 `client.post()` **成功之后**,
                                                 结构上永远看不到 ConnectError;
                                                 而且它只告警**不拦调用**、只有秘塔没有豆包、
                                                 也没接进写作证据链实际走的
                                                 `metaso_mcp._metaso_web_search_direct`。
                                                 两者判据面不相交,合并只会让两边都说不清。
===============================================  ==========================================

## 状态是进程内的

与 `tools/metaso_health` 同一口径:生产 `WORKERS=1`,web 进程内计数即全量;
cron 是独立进程独立计数,但告警层按 `(rule_key, fingerprint)` 去重,
两个进程同时拉也只有一条 firing 行。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Tuple

logger = logging.getLogger("GEO-ProviderBreaker")

#: 供应商标识 —— 与 `llm_call_log.platform` 用同一串,查账时能直接对上。
PROVIDER_METASO = "metaso"
PROVIDER_DOUBAO = "doubao_search"

#: 连续多少次「未送达」就停掉该供应商。工单建议 5。
#: 🔴 改它 = 改"最多允许白烧几笔",走工单,不是随手调参
#:    (同 `tools/metaso_health.METASO_CONSECUTIVE_ALERT_THRESHOLD` 的口径)。
CONSECUTIVE_UNDELIVERED_THRESHOLD = 5

#: 熔断后的静默期(秒)。过了这段时间放**恰好一个**探针出去试水。
#: 300s 的依据:写作一批(并发 10、几十篇)通常跑十几分钟 ——
#: 一批之内最多探 2-3 次,既不会把"本批"拖成永久停用,也不会退化成不限速重试。
COOLDOWN_SECONDS = 300

#: 复用 `CircuitBreaker` 时把它的**失败率**那条臂设成不可达。
#: 理由:本模块的跳闸条件只认"连续未送达"。失败率那条臂会把业务级失败
#: (空结果 / 余额不足)也算进分母分子,那类失败**是送达的、该计费的**,
#: 让它参与跳闸 = 用错判据停供应商。锁 `test_rate_arm_is_unreachable` 钉死这一点。
_RATE_ARM_DISABLED_MIN_PROCESSED = 10 ** 9

_ALERT_RULE_KEY = "evidence_provider_undelivered"

_lock = threading.Lock()
#: provider -> {"counter": CircuitBreaker, "opened_at": float|None, "probing": bool}
_state: dict = {}


def _new_counter():
    """连续失败计数**复用**既有 `CircuitBreaker`,不另写一份。"""
    from services.research_monitor.circuit_breaker import CircuitBreaker

    return CircuitBreaker(
        consecutive_threshold=CONSECUTIVE_UNDELIVERED_THRESHOLD,
        min_processed=_RATE_ARM_DISABLED_MIN_PROCESSED,
    )


def _slot(provider: str) -> dict:
    st = _state.get(provider)
    if st is None:
        st = {"counter": _new_counter(), "opened_at": None, "probing": False}
        _state[provider] = st
    return st


def _aiohttp_no_send_types() -> tuple:
    """aiohttp 侧的「确定没送出去」族。

    只收 ``ClientConnectorError`` —— 它已经把 ``ClientProxyConnectionError`` /
    ``ClientConnectorCertificateError`` / ``ClientConnectorSSLError``
    作为子类全覆盖(实测 `issubclass` 三个都 True),`isinstance` 一条顶四条。

    🔴 **刻意不收**这三个(它们的父类更宽,收进来会把已送达的算成未送达):
      · ``ClientConnectionError`` —— 是 ``ClientConnectorError`` 与
        ``ServerDisconnectedError`` 的**共同父类**,后者是"连上了又断",可能已受理;
      · ``ClientOSError`` —— 同理,可能发生在流中途;
      · ``ServerTimeoutError`` / ``SocketTimeoutError`` —— 超时,与 httpx 侧
        排除 ``ReadTimeout`` 同一个理由:请求可能已经到了对方。
    """
    try:
        import aiohttp

        return (aiohttp.ClientConnectorError,)
    except Exception:  # pragma: no cover - 环境没装 aiohttp
        return ()


def undelivered_error_types() -> tuple:
    """「确定没送出去」的异常集合 —— **计费侧单点**。

    = `image_client._connect_error_types()`(httpx 传输族,那份自称唯一、
      明写「改这里 = 改计费安全边界」)
      + aiohttp 连接族(见 `_aiohttp_no_send_types`)
      + `ImageConnectFailed`(connect 重试**用尽**的标记)

    ## [WO R2 2026-08-09] 为什么是"扩这一份",不是"在 tracker 侧递归 __cause__"

    工单给了两个选型,选**扩定义**,理由是实测出来的、不是偏好:

    1. **递归 `__cause__` 抓不到 `ImageConnectFailed`。**
       它在 `image_client._submit_with_retry` 里是**裸 raise**、而且在 for 循环
       **之外**(不在 except 块里)—— `__cause__` 与 `__context__` **都是 None**。
       递归方案对这条链**完全无效**,而这条链正好是三条待覆盖链之一。
    2. **`__context__` 递归会过度匹配。** 它是**隐式**串起来的:任何"先接住一个
       ConnectError、然后在处理里抛了别的错"的写法都会被判成未送达 → 少记成本。
       而 `llm_call_log` 是成本 SSOT,方向必须是宁可多记。
    3. 扩定义是**白名单**,每加一个类型都要写清"为什么它确定没送出去",
       可审、可锁;递归是**行为**,加进来之后每条链都要单独证明它不会误判。

    ## 为什么不把这些直接塞进 `image_client._connect_error_types()`

    那一份同时是 image_client 的**重试判据**(`except _connect_error_types()`)。
      · `ImageConnectFailed` 塞进去 = 在重试用尽的标记上再套一层重试(自食);
      · aiohttp 族塞进去对它是死重量(image_client 全程 httpx)。
    → 分层:**它管"可不可以重试"(窄),这里管"要不要计费"(宽)**,
    而且宽的那份是**由窄的那份加出来的**,不存在两份各自维护的数字。

    取不到时**返回空元组**(fail-open,熔断退化为不触发)—— 熔断是省钱设施,
    它自己坏掉不该把主链带下水。计费侧的方向相反,见
    `llm_call_tracker._is_undelivered_exception` 的注释。
    """
    types: tuple = ()
    try:
        from services.marketing.image_client import ImageConnectFailed, _connect_error_types

        types = _connect_error_types() + (ImageConnectFailed,)
    except Exception:  # pragma: no cover - 依赖缺失兜底
        logger.warning("[ProviderBreaker] 取不到未送达异常集合,熔断本次退化为不触发")
        return ()
    return types + _aiohttp_no_send_types()


def is_undelivered(exc: BaseException) -> bool:
    """这个异常是不是「确定没送到对方服务器」。"""
    types = undelivered_error_types()
    return bool(types) and isinstance(exc, types)


def should_skip(provider: str) -> Tuple[bool, Optional[str]]:
    """发 HTTP **之前**问一句:这个供应商现在还能不能打。

    Returns:
        ``(True, reason)`` = 别打(直接返回失败,**不进 llm_track,不产生成本**);
        ``(False, None)``  = 可以打。

    半开只放**一个**探针:静默期一过,第一个问到的调用放行并占住探针位,
    其余并发仍被拦。探针成功 → 合闸;探针再次未送达 → 重新开闸 + 重新计时。
    没有这个探针位,并发 10 的写作批会在每个静默期一次性漏出 10 笔。
    """
    with _lock:
        st = _slot(provider)
        tripped, _reason = st["counter"].is_tripped()
        if not tripped:
            return False, None
        opened_at = st["opened_at"]
        if opened_at is None:
            # 计数到阈值但还没记开闸时刻(理论上不会走到,防御)
            st["opened_at"] = time.monotonic()
            return True, "open"
        if (time.monotonic() - opened_at) < COOLDOWN_SECONDS:
            return True, "open_cooldown"
        if st["probing"]:
            return True, "open_probe_in_flight"
        st["probing"] = True
        return False, None


def record_delivered(provider: str) -> None:
    """请求**送达**了(拿到任何 HTTP 响应,哪怕是 500 / 业务错)。

    送达 = 网络这一层是好的 → 连续计数清零、合闸、收告警。
    业务层好不好不归本模块管(那是 `tools/metaso_health`)。
    """
    resolve = False
    with _lock:
        st = _slot(provider)
        was_tripped, _ = st["counter"].is_tripped()
        st["counter"].record_success()
        st["opened_at"] = None
        st["probing"] = False
        if was_tripped:
            resolve = True
    if resolve:
        logger.warning("[ProviderBreaker] %s 已恢复送达,合闸", provider)
        _resolve_alert(provider)


def record_undelivered(provider: str, reason: str = "") -> bool:
    """请求**没送出去**(ConnectError 一类)。

    Returns:
        本次是否**刚刚**跳闸(用于日志/测试断言;告警在函数内已经发过了)。
    """
    just_tripped = False
    consecutive = 0
    with _lock:
        st = _slot(provider)
        was_tripped, _ = st["counter"].is_tripped()
        st["counter"].record_failure()
        consecutive = st["counter"].consecutive_failures
        now_tripped, _ = st["counter"].is_tripped()
        if now_tripped:
            # 首次跳闸,或半开探针又挂了 → 重新计时
            if not was_tripped or st["probing"]:
                st["opened_at"] = time.monotonic()
            st["probing"] = False
            just_tripped = not was_tripped
    if just_tripped:
        logger.error(
            "[ProviderBreaker] %s 连续 %d 次未送达 → 本批停止调用(静默 %ds)",
            provider, consecutive, COOLDOWN_SECONDS,
        )
        _fire_alert(provider, consecutive, reason)
    return just_tripped


def reset_provider_breakers() -> None:
    """重置全部供应商状态。**当前调用点只有测试** —— 不新增没人调的只读快照函数
    (死函数 = 建了没接线)。真要看状态查 `ai_ops_alerts` 表。"""
    with _lock:
        _state.clear()


# ============================================================
# 告警(复用 AIOps,不新造第二套通知面)
# ============================================================

def _fire_alert(provider: str, consecutive: int, reason: str) -> None:
    detail = (
        f"{provider} 连续 {consecutive} 次请求未送达(最近原因:{reason or '未知'})。"
        f"已停止本批对该供应商的证据检索调用,静默 {COOLDOWN_SECONDS}s 后放一个探针重试。"
        f"期间写作主链**不中断**,按「证据缺失允许写短」合同分支继续出稿。"
        f"请检查该供应商的网络出口/域名解析/代理。"
    )
    logger.error("[ProviderBreaker] %s", detail)
    try:
        from db import ai_ops_db

        ai_ops_db.upsert_alert(
            _ALERT_RULE_KEY,
            severity="critical",
            title=f"证据检索供应商不可达 · {provider} 已熔断",
            detail=detail,
            fingerprint=provider,
            payload={"provider": provider, "consecutive": consecutive, "reason": reason},
        )
    except Exception as exc:
        # 告警通道自身故障不能再吞 —— 至少留 error 日志(日志是最后一道人眼面)
        logger.error("[ProviderBreaker] 告警落库失败: %s", exc)


def _resolve_alert(provider: str) -> None:
    try:
        from db import ai_ops_db

        ai_ops_db.resolve_alerts(_ALERT_RULE_KEY, provider)
    except Exception as exc:
        logger.warning("[ProviderBreaker] 告警恢复失败: %s", exc)
