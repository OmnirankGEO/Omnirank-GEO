"""AI-1 平台采集包 测试共享 fake / fixture。

全部测试零真实网络、零真实 DB:provider 调用、http、账本、策略均以 fake 注入。
预算通过 policy.sampling_budget 设定,账本为线程安全 InMemoryBudgetLedger(原子预留)。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import pytest

from services.ai_surface_monitoring.adapters.openai_compat import OpenAICompatibleAdapter
from services.ai_surface_monitoring.adapters.wrapped import WrappedResearchAdapter
from services.ai_surface_monitoring.cost_policy import ConcurrencyGovernor, InMemoryBudgetLedger, RpmLimiter
from services.ai_surface_monitoring.policy import FakeObservationPolicy, SamplingBudgetV1
from services.ai_surface_monitoring.registry import AdapterRegistry, _build_default_adapters
from services.ai_surface_monitoring.service import CollectionService


def ingest_policy(*, sampling_budget: Optional[SamplingBudgetV1] = None, platforms=None,
                  ingest: bool = True) -> FakeObservationPolicy:
    """采集测试用 policy(ingest 默认开;可设预算/平台矩阵)。"""
    return FakeObservationPolicy(ingest_enabled=ingest, sampling_budget=sampling_budget, platforms=platforms)


def make_engine_fetch(
    answer: str = "回答",
    citations: Optional[List[dict]] = None,
    usage: Optional[dict] = None,
    capture: Optional[dict] = None,
    raise_exc: Optional[BaseException] = None,
    attempts: int = 1,
) -> Callable:
    """构造 platforms.query_* 风格的 async fetch。capture 记录收到的 prompt/max_retries。"""
    async def fetch(prompt_id: int, prompt: str, max_retries: int = 3) -> dict:
        if capture is not None:
            capture["prompt"] = prompt
            capture.setdefault("prompts", []).append(prompt)
            capture["prompt_id"] = prompt_id
            capture["max_retries"] = max_retries
        if raise_exc is not None:
            raise raise_exc
        return {
            "platform": "x", "prompt_id": prompt_id, "prompt": prompt, "answer": answer,
            "citations": list(citations or []), "ok": True, "error": None,
            "raw": {"usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}},
            "attempts": attempts,
        }

    return fetch


def make_http_post(payload: dict, status: int = 200, capture: Optional[dict] = None) -> Callable:
    """构造 OpenAI 兼容 http_post。capture 记录发出的 body。"""
    async def http_post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float):
        if capture is not None:
            capture["url"] = url
            capture["headers"] = headers
            capture["body"] = body
            capture.setdefault("bodies", []).append(body)
        return status, payload

    return http_post


def build_service(
    *,
    engine_fetch: Optional[Callable] = None,
    yuanbao_http: Optional[Callable] = None,
    deepseek_native_http: Optional[Callable] = None,
    sampling_budget: Optional[SamplingBudgetV1] = None,
    per_platform: int = 8,
    policy: Optional[FakeObservationPolicy] = None,
    with_budget: bool = True,
    ledger: Optional[InMemoryBudgetLedger] = None,
    rpm_limiter: Optional[RpmLimiter] = None,
    platforms=None,
) -> tuple[CollectionService, Optional[InMemoryBudgetLedger]]:
    """构造一个全 fake 注入的 CollectionService + 其 ledger(ingest 默认开)。"""
    policy = policy or ingest_policy(sampling_budget=sampling_budget, platforms=platforms)
    ef = engine_fetch or make_engine_fetch()

    adapters = _build_default_adapters()
    for sk in ("doubao_ark_api_search", "qwen_dashscope_search",
               "deepseek_dashscope_search_legacy", "other_explicit"):
        adapters[sk] = WrappedResearchAdapter(sk, fetch=ef)
    if yuanbao_http is not None:
        adapters["yuanbao_hy3_tokenhub"] = OpenAICompatibleAdapter(
            "yuanbao_hy3_tokenhub", http_post=yuanbao_http, api_key_getter=lambda n: "FAKEKEY", verify_model_echo=True)
    if deepseek_native_http is not None:
        adapters["deepseek_native_no_search"] = OpenAICompatibleAdapter(
            "deepseek_native_no_search", http_post=deepseek_native_http, api_key_getter=lambda n: "FAKEKEY",
            verify_model_echo=False)
    reg = AdapterRegistry(policy, adapters=adapters)

    if ledger is None and with_budget:
        ledger = InMemoryBudgetLedger()
    svc = CollectionService(reg, ledger=ledger, governor=ConcurrencyGovernor(per_platform=per_platform),
                            rpm_limiter=rpm_limiter)
    return svc, ledger


@pytest.fixture
def fake_service():
    return build_service()
