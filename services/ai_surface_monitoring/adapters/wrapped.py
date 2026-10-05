"""包裹既有 research 平台调用(services.research_monitor.platforms.query_*)的 adapter。

用于:doubao_ark_api_search / qwen_dashscope_search / deepseek_dashscope_search_legacy /
other_explicit(Kimi)。这些引擎已在 base 存在且被 live 研究/监测链使用;本 adapter
**只 import 调用、不改写**它们,把统一返回 dict 映射成 ObservationEnvelopeV1。

依赖注入:``fetch`` 为 ``async (prompt_id, prompt) -> dict``;默认惰性包裹真实
``query_with_retry(query_X, ...)``(惰性 import 保证本模块 import 干净、测试零 DB/网络)。
"""

from __future__ import annotations

import inspect
import re
import zlib
from typing import Any, Awaitable, Callable, Dict, Optional

# query_with_retry 的终态包裹异常:"重试 N 次仍失败: ..."(仅这种代表 N 次真实 provider 调用)
_RETRY_EXHAUSTED_RE = re.compile(r"重试\s*(\d+)\s*次仍失败")

from services.ai_surface_monitoring.adapters.base import BaseSurfaceAdapter, EngineResult, extract_openai_usage
from services.ai_surface_monitoring.contracts import CollectionRequest

# surface_key → research 引擎函数名(惰性解析)
_SURFACE_TO_ENGINE: Dict[str, str] = {
    "doubao_ark_api_search": "query_doubao",
    "qwen_dashscope_search": "query_qwen",
    "deepseek_dashscope_search_legacy": "query_deepseek",
    "other_explicit": "query_kimi",  # Kimi(legacy)
}

FetchFn = Callable[[int, str], Awaitable[dict]]


def _default_research_fetch(surface_key: str) -> FetchFn:
    """惰性包裹真实引擎:import 只在首次调用时发生(测试注入 fake 时永不 import platforms)。"""
    engine_name = _SURFACE_TO_ENGINE.get(surface_key)
    if engine_name is None:
        raise KeyError(f"surface_key {surface_key!r} 无对应 research 引擎")

    async def fetch(prompt_id: int, prompt: str, max_retries: int = 3) -> dict:
        from services.research_monitor.platforms import (  # 惰性
            query_with_retry,
            query_doubao,
            query_deepseek,
            query_qwen,
            query_kimi,
        )

        engine = {
            "query_doubao": query_doubao,
            "query_deepseek": query_deepseek,
            "query_qwen": query_qwen,
            "query_kimi": query_kimi,
        }[engine_name]
        return await query_with_retry(engine, prompt_id, prompt, max_retries=max_retries)

    return fetch


class WrappedResearchAdapter(BaseSurfaceAdapter):
    """把 platforms.query_* 的返回 dict 映射成 ObservationEnvelopeV1。"""

    def __init__(self, surface_key: str, fetch: Optional[FetchFn] = None):
        super().__init__(surface_key)
        self._fetch_fn: FetchFn = fetch if fetch is not None else _default_research_fetch(surface_key)

    @staticmethod
    def _stable_prompt_id(request: CollectionRequest) -> int:
        # 稳定 int(仅用于引擎 tracking metadata);crc32 稳定,不受 PYTHONHASHSEED 影响
        return zlib.crc32(request.request_id.encode("utf-8")) & 0x7FFFFFFF

    async def _fetch(self, request: CollectionRequest, prompt_text: str) -> EngineResult:
        prompt_id = self._stable_prompt_id(request)
        # 逐字 SSOT:把 prompt_text 原样交给引擎(引擎内部若加 provider marker,属其口径,不改 question 意图)
        # P1-4:按 policy 注入的重试上限调用(默认走 fetch 内部默认)。
        data: dict = await self._invoke(prompt_id, prompt_text, request.max_retry_calls)
        if not isinstance(data, dict):
            raise RuntimeError(f"引擎返回非 dict: {type(data).__name__}")

        raw = data.get("raw")
        inp, out, cached = extract_openai_usage(raw)
        # model:优先响应回显,否则 spec 默认(引擎内部 llm_track 已记真实 model 供对账)
        model_key = None
        if isinstance(raw, dict):
            model_key = raw.get("model") or _dashscope_model_echo(raw)
        # P1-4:attempts = 真实 provider 调用次数(query_with_retry 通过 setdefault 挂 'attempts';失败重试也计入)
        attempts = int(data.get("attempts") or 1)
        return EngineResult(
            answer=data.get("answer") or "",
            citations=list(data.get("citations") or []),
            raw=raw,
            provider_key=self.spec.provider_key,
            model_key=model_key or self.spec.default_model_key,
            model_revision=None,
            search_provider=self.spec.search_provider,
            search_enabled=self.spec.default_search_enabled,
            attempts=max(attempts, 1),
            input_tokens=inp,
            output_tokens=out,
            cached_tokens=cached,
        )

    async def _invoke(self, prompt_id: int, prompt_text: str, max_retry_calls: Optional[int]) -> dict:
        """调用注入的 fetch;按 policy 上限传 max_retries。失败时把**真实**尝试次数挂到异常
        (P1-4)。只有 query_with_retry 的终态包裹异常("重试 N 次仍失败")才是 N 次真实调用;
        非重试类(ValueError/TypeError/JSONDecodeError 等)在包裹器里只跑 1 次即抛,故 attempts=1
        (不把单次失败当 N 次多算;复审 convergence finding)。"""
        max_retries = None if max_retry_calls is None else 1 + max(int(max_retry_calls), 0)  # 1 首次 + N 重试
        try:
            return await self._call_fetch(prompt_id, prompt_text, max_retries)
        except Exception as exc:
            if not hasattr(exc, "attempts"):
                m = _RETRY_EXHAUSTED_RE.search(str(exc))
                attempts = int(m.group(1)) if m else 1  # 有"重试 N 次仍失败"才 N 次;否则单次
                try:
                    exc.attempts = attempts  # type: ignore[attr-defined]
                except Exception:
                    pass
            raise

    async def _call_fetch(self, prompt_id: int, prompt_text: str, max_retries: Optional[int]) -> dict:
        # 用签名探测决定是否传 max_retries,避免 except TypeError 兜底在**真实** TypeError 时重复调用 provider。
        if max_retries is None or not _fetch_accepts_max_retries(self._fetch_fn):
            return await self._fetch_fn(prompt_id, prompt_text)
        return await self._fetch_fn(prompt_id, prompt_text, max_retries=max_retries)


def _fetch_accepts_max_retries(fn: Callable) -> bool:
    """探测 fetch 是否接受 max_retries kw(显式参数或 **kwargs)。"""
    try:
        params = inspect.signature(fn).parameters.values()
    except (ValueError, TypeError):
        return False
    return any(p.name == "max_retries" or p.kind == inspect.Parameter.VAR_KEYWORD for p in params)


def _dashscope_model_echo(raw: dict) -> Optional[Any]:
    # dashscope generation 有时把 model 放在顶层或 output 里;尽力而为
    for key in ("model", "model_name"):
        if raw.get(key):
            return raw.get(key)
    return None
