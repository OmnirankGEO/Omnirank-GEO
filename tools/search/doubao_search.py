"""豆包搜索(火山融合信息搜索)客户端 —— 搜索 Provider 适配层的量供给侧。

工单 SEARCH_PROVIDER_LAYER 2026-07-28:
- 三件套满配:``Count=50 + NeedContent=true``(默认值即满配 · ¥0.02/次 flat,
  多要结果不加钱);
- 内置节流:令牌桶默认 5 QPS(env ``DOUBAO_SEARCH_QPS`` 可配),429/5xx 退避重试 1 次;
- 失败/空结果抛 ``DoubaoSearchError``,由调用侧(provider_router / metaso 入口 wrapper)
  自动回退秘塔 —— 本模块自己**不**吞错;
- 成本埋点走 llm_call_tracker 同口径(platform=doubao_search · ¥0.02/次)。

🔴 报价链(transparent_pricing / batch_pricing / keyword_value_scorer 等 7 文件)
禁止 import 本模块 —— 报价换搜索源 = 报价数值漂移 = 生产事故(工单 §1-A 底线锁)。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, Final

import httpx

from tools.llm_call_tracker import llm_track
from tools.search._err import describe_exc  # [2026-08-08] 见该模块抬头

DOUBAO_SEARCH_ENDPOINT: Final = "https://open.feedcoopapi.com/search_api/web_search"
DOUBAO_SEARCH_COST_PER_CALL_CNY: Final = 0.02
_DEFAULT_QPS: Final = 5.0
_RETRY_BACKOFF_S: Final = 1.5
_QUERY_MAX_CHARS: Final = 100
_COUNT_MAX: Final = 50


class DoubaoSearchError(RuntimeError):
    """豆包搜索失败(HTTP 错误 / 网络异常 / key 未配置)。调用侧据此回退秘塔。"""


class DoubaoSearchEmpty(DoubaoSearchError):
    """豆包返回空结果集 —— 也走回退链(工单 §2.4:空结果同样回退)。"""


def is_doubao_search_configured() -> bool:
    return bool(os.environ.get("DOUBAO_SEARCH_API_KEY", "").strip())


class TokenBucket:
    """严格间隔调度的异步令牌桶:任意 1 秒窗口内放行次数 ≤ qps。

    ``time_fn`` / ``sleep_fn`` 可注入,判别锁用虚拟时钟验证速率,不必真睡。
    """

    def __init__(
        self,
        qps: float,
        *,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.qps = max(0.1, float(qps))
        self._interval = 1.0 / self.qps
        self._time_fn = time_fn
        self._sleep_fn = sleep_fn
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def acquire(self) -> float:
        """预约下一个放行时隙并等到该时刻,返回时隙时间戳(测试用)。"""
        async with self._lock:
            now = self._time_fn()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        wait = slot - now
        if wait > 0:
            await self._sleep_fn(wait)
        return slot


_bucket_singleton: TokenBucket | None = None


def _bucket() -> TokenBucket:
    global _bucket_singleton
    if _bucket_singleton is None:
        try:
            qps = float(os.environ.get("DOUBAO_SEARCH_QPS", "") or _DEFAULT_QPS)
        except (TypeError, ValueError):
            qps = _DEFAULT_QPS
        _bucket_singleton = TokenBucket(qps)
    return _bucket_singleton


def _reset_bucket_for_tests() -> None:
    global _bucket_singleton
    _bucket_singleton = None


async def doubao_web_search_raw(
    query: str,
    *,
    count: int = _COUNT_MAX,
    need_content: bool = True,
    content_format: str = "markdown",
    timeout: float = 30.0,
) -> list[dict]:
    """调豆包搜索,返回原始 ``Result.WebResults[]`` 行(Url/Title/Content/Summary/Snippet)。

    默认即满配(Count=50 + NeedContent=true)。失败与空结果抛 DoubaoSearchError 族,
    不静默返回空 —— 回退决策属于调用侧。
    """
    api_key = os.environ.get("DOUBAO_SEARCH_API_KEY", "").strip()
    if not api_key:
        raise DoubaoSearchError("DOUBAO_SEARCH_API_KEY not configured")
    body = {
        "Query": str(query or "")[:_QUERY_MAX_CHARS],
        "SearchType": "web",
        "Count": max(1, min(int(count or _COUNT_MAX), _COUNT_MAX)),
        "Filter": {"NeedContent": bool(need_content), "NeedUrl": True},
        "ContentFormats": content_format,
    }
    from tools.search.provider_circuit_breaker import (
        PROVIDER_DOUBAO,
        is_undelivered,
        record_delivered,
        record_undelivered,
        should_skip,
    )

    last_error = "unknown"
    for attempt in (1, 2):  # 429/5xx/网络异常 退避重试 1 次,仍失败交回退链
        # [WO 熔断单 2026-08-08] 🔴 闸放在 **令牌桶与 llm_track 之前**:
        #   跳过 = 不占限速位、不进 llm_track = 不产生那一笔 ¥0.02。
        #   抛 DoubaoSearchError 与既有失败路径**同一个出口** ——
        #   provider_router:126 照旧回退秘塔,回退链一个字不用改。
        _skip, _skip_reason = should_skip(PROVIDER_DOUBAO)
        if _skip:
            raise DoubaoSearchError(f"doubao circuit open ({_skip_reason})")
        await _bucket().acquire()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with llm_track(
                    "doubao_web_search",
                    "doubao_search",
                    model="web_search",
                    metadata={"count": body["Count"], "attempt": attempt},
                ) as tracker:
                    response = await client.post(
                        DOUBAO_SEARCH_ENDPOINT,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=body,
                    )
                    # [WO 熔断单 2026-08-08] 拿到任何 HTTP 响应 = **送达** → 合闸。
                    #   429/5xx/空结果都算送达(业务层的事,不归本熔断管)。
                    record_delivered(PROVIDER_DOUBAO)

                    if response.status_code != 200:
                        last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                        tracker.record(success=False, error_msg=last_error)
                        if attempt == 1 and (response.status_code == 429 or response.status_code >= 500):
                            await asyncio.sleep(_RETRY_BACKOFF_S)
                            continue
                        raise DoubaoSearchError(last_error)
                    data = response.json()
                    results = ((data or {}).get("Result") or {}).get("WebResults") or []
                    tracker.record(
                        success=bool(results),
                        error_msg=None if results else "empty WebResults",
                    )
                    if not results:
                        raise DoubaoSearchEmpty(f"empty WebResults for query: {body['Query'][:60]}")
                    return [row for row in results if isinstance(row, dict)]
        except DoubaoSearchError:
            raise
        except Exception as exc:  # 网络/JSON 异常:重试一次后交回退链
            # [2026-08-08] 原来只取最外层 str(exc),而真实链条是
            #   ConnectError('') <- EndOfStream('') <- SSLEOFError('[SSL: UNEXPECTED_EOF...]')
            #   -- 外两层 str() 全空,日志只剩 "ConnectError: "。改走链式描述单点。
            last_error = describe_exc(exc)
            # [WO 熔断单 2026-08-08] 只把「确定未送达」记进熔断计数(同 metaso 侧口径)。
            if is_undelivered(exc):
                record_undelivered(PROVIDER_DOUBAO, last_error)
            if attempt == 1:
                await asyncio.sleep(_RETRY_BACKOFF_S)
                continue
            raise DoubaoSearchError(last_error) from exc
    raise DoubaoSearchError(last_error)


def shim_webpage_row(raw: dict, *, position: int = 1) -> dict:
    """豆包原始行 → 秘塔 webpages 行(消费字段逐一映射 · 工单 §2.2)。

    Url→link · Title→title · Content→content/rawContent · Summary→summary ·
    Snippet→snippet。上层消费方(extract_citations / _v36_metaso_pages / agent_loop
    adapter)读的就是这几个键,一个不能少。
    """
    content = str(raw.get("Content") or "")
    url = str(raw.get("Url") or "")
    return {
        "title": str(raw.get("Title") or ""),
        "link": url,
        "url": url,  # 部分消费方兜底读 url(extract_citations: link or url)
        "content": content,
        "rawContent": content,
        "summary": str(raw.get("Summary") or ""),
        "snippet": str(raw.get("Snippet") or raw.get("Summary") or ""),
        "date": str(raw.get("PublishTime") or raw.get("PublishDate") or ""),
        "position": position,
        "provider": "doubao_search",
    }


async def doubao_search_webpages(
    query: str,
    *,
    size: int = _COUNT_MAX,
    need_content: bool = True,
) -> list[dict]:
    """豆包搜索 → 秘塔结构行列表(D 级新场景直用入口)。失败抛 DoubaoSearchError。"""
    raw_rows = await doubao_web_search_raw(query, count=size, need_content=need_content)
    return [shim_webpage_row(row, position=index + 1) for index, row in enumerate(raw_rows)]


async def multi_angle_search(
    topic: str,
    angles: list[str] | tuple[str, ...],
    *,
    per_angle: int = _COUNT_MAX,
    need_content: bool = True,
    fallback_search: Callable[[str, int], Awaitable[list[dict]]] | None = None,
) -> dict:
    """多角度聚合(D 级通用):主题 × 角度列表 → 逐 query 检索 → URL 去重合并。

    实测近线性(10 query 去重 469 条独立 URL)。豆包不可用时逐 query 走
    ``fallback_search(query, size)``(秘塔结构行),没给回退就跳过该 query。
    供调研召回扩容与将来的深度报价信息底座复用 —— 深度报价属**新增信息层**,
    与现有报价 7 文件无关(那边永远秘塔原样)。
    """
    cleaned_angles = [str(a).strip() for a in (angles or []) if str(a or "").strip()]
    queries = [f"{topic} {angle}".strip() for angle in cleaned_angles] or [str(topic or "").strip()]
    rows: list[dict] = []
    seen: set[str] = set()
    fallback_used = 0
    for query in queries:
        try:
            batch = await doubao_search_webpages(query, size=per_angle, need_content=need_content)
        except DoubaoSearchError:
            if fallback_search is None:
                continue
            try:
                batch = list(await fallback_search(query, per_angle) or [])
                fallback_used += 1
            except Exception:
                continue
        for row in batch:
            url = str(row.get("link") or row.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                rows.append(row)
    return {
        "topic": topic,
        "query_count": len(queries),
        "unique_count": len(rows),
        "fallback_query_count": fallback_used,
        "rows": rows,
    }


__all__ = [
    "DOUBAO_SEARCH_ENDPOINT",
    "DOUBAO_SEARCH_COST_PER_CALL_CNY",
    "DoubaoSearchError",
    "DoubaoSearchEmpty",
    "TokenBucket",
    "is_doubao_search_configured",
    "doubao_web_search_raw",
    "doubao_search_webpages",
    "shim_webpage_row",
    "multi_angle_search",
]
