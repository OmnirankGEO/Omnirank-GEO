"""adapter 基类:统一的 envelope 映射、传输级状态判别、citation/usage 归一。

关键纪律:
  - **question_text 逐字 SSOT**:``request.question_text`` 原样作为发给 provider 的问题;
    system framing 只进 ``prompt_text``(默认二者相同)。adapter 绝不把 question 改写成
    "关键词哪家好",也不让 LLM 改写后替代。
  - **不做品牌判断**:response_status 只反映传输/响应形态(answered/timeout/error/unknown/
    budget_blocked/refused),不判定品牌是否被提及、不输出 mentioned=false;target_outcome 归 AI-2。
  - **不 silent fallback**:发生 fallback 时 provider/model/surface 必须记录真实执行者。
  - **UNKNOWN ≠ 未提到**:空答/降级归 unknown,不伪造空字符串成功、不改写成 not_mentioned。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.ai_surface_monitoring.contracts import (
    CitationEvidence,
    CollectionRequest,
    ModelLineage,
    ObservationEnvelopeV1,
    SearchQueryEvidence,
    SurfaceHealth,
    answer_text_hash,
    canonical_question_hash,
    utcnow,
)
from services.ai_surface_monitoring.cost_policy import estimate_usage_cost
from services.ai_surface_monitoring.lineage import get_surface_spec

logger = logging.getLogger("GEO-AISurface-Adapter")


@dataclass
class EngineResult:
    """provider 调用的中性结果(各 adapter 的 fetch 归一到本结构再映射 envelope)。"""

    answer: str = ""
    citations: List[dict] = field(default_factory=list)
    raw: Any = None
    # 真实执行身份(fallback 后必须反映真实执行者)
    provider_key: Optional[str] = None
    model_key: Optional[str] = None
    model_revision: Optional[str] = None
    search_provider: Optional[str] = None
    search_enabled: Optional[bool] = None
    search_queries: Optional[List[dict]] = None
    fallback_occurred: bool = False
    fallback_detail: Optional[str] = None
    provider_trace_id: Optional[str] = None
    # provider 明确的 content-filter / refusal 信号(传输级,非品牌判断)
    provider_refused: bool = False
    # 真实 provider 调用次数(含重试;P1-4 用于按次计费)
    attempts: int = 1
    # token 用量(provider 未返回则 None)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None


def extract_openai_usage(raw: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """从 OpenAI 兼容 / DashScope 响应提取 (input, output, cached) token。

    与 tools.llm_call_tracker.usage_from_response_payload 兼容,并**额外**处理 hy3 的嵌套
    ``prompt_tokens_details.cached_tokens``(既有 helper 不认)。provider 未返回 usage → None。
    """
    if not isinstance(raw, dict):
        return None, None, None
    usage = raw.get("usage")
    # dashscope generation 把 usage 放在 output 同级或顶层;OpenAI 放顶层
    if not isinstance(usage, dict):
        return None, None, None

    def _first_int(*names) -> Optional[int]:
        for n in names:
            v = usage.get(n)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
        return None

    inp = _first_int("prompt_tokens", "input_tokens", "total_input_tokens")
    out = _first_int("completion_tokens", "output_tokens", "total_output_tokens")
    total = _first_int("total_tokens")
    if (inp is None or inp == 0) and (out is None or out == 0) and total:
        inp = total
    # cached:先平铺字段,再 hy3 嵌套
    cached = _first_int("prompt_cache_hit_tokens", "cached_tokens")
    if cached is None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            try:
                cached = int(details["cached_tokens"])
            except (TypeError, ValueError):
                cached = None
    return inp, out, cached


class BaseSurfaceAdapter:
    """所有 adapter 的公共 envelope 映射。子类实现 ``_fetch`` 与 ``_probe_once``。"""

    def __init__(self, surface_key: str):
        self.spec = get_surface_spec(surface_key)
        self.surface_key = surface_key

    # ---- 子类必须实现 ----
    async def _fetch(self, request: CollectionRequest, prompt_text: str) -> EngineResult:
        raise NotImplementedError

    async def _probe_once(self) -> tuple[str, Optional[int], Optional[str], Optional[str]]:
        """返回 (status, latency_ms, model_revision, error_code)。默认:结构保留但无自动可用性。"""
        if self.spec.availability != "active":
            return "unavailable", None, None, f"surface_{self.spec.availability}"
        return "unknown", None, None, None

    # ---- 公共 ----
    def describe_lineage(self) -> ModelLineage:
        return ModelLineage(
            product_label=self.spec.product_label,
            platform_key=self.spec.platform_key,
            provider_key=self.spec.provider_key,
            surface_key=self.surface_key,  # type: ignore[arg-type]
            model_key=self.spec.default_model_key,
            model_revision=None,
            search_provider=self.spec.search_provider,
            search_enabled=self.spec.default_search_enabled,
            fallback_occurred=False,
        )

    async def probe(self) -> SurfaceHealth:
        status, latency_ms, model_revision, error_code = await self._probe_once()
        return SurfaceHealth(
            platform_key=self.spec.platform_key,
            provider_key=self.spec.provider_key,
            model_key=self.spec.default_model_key,
            surface_key=self.surface_key,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            checked_at=utcnow(),
            latency_ms=latency_ms,
            model_revision=model_revision,
            error_code=error_code,
        )

    async def collect(self, request: CollectionRequest) -> ObservationEnvelopeV1:
        if request.surface_key != self.surface_key:
            raise ValueError(
                f"adapter surface_key={self.surface_key!r} 与 request.surface_key={request.surface_key!r} 不符"
            )
        # 逐字 SSOT:question_text 原样;system framing 只进 prompt_text
        prompt_text = request.effective_prompt_text()
        started = _monotonic_ms()

        response_status = "answered"
        result: Optional[EngineResult] = None
        try:
            result = await self._fetch(request, prompt_text)
        except Exception as exc:  # 传输/供应商错误(保留结构化错误,不伪装空回答成功)
            # 超时与其他错误分开(§4 五态判别):httpx.TimeoutException / asyncio.TimeoutError /
            # 包裹层 RuntimeError("...TimeoutError...")均归 timeout,其余归 error。
            response_status = "timeout" if _is_timeout(exc) else "error"
            # P1-4:失败也按真实 provider 调用次数计费(重试耗尽 → attempts=N,由 adapter 挂在异常上)
            _att = getattr(exc, "attempts", 1)
            attempts = _att if isinstance(_att, int) and _att >= 1 else 1
            result = EngineResult(answer="", raw={"error": f"{type(exc).__name__}: {exc}"}, attempts=attempts)

        latency_ms = _monotonic_ms() - started
        if result is None:
            result = EngineResult(answer="", raw=None)

        # 传输级状态判别(不做品牌判断)
        if response_status == "answered":
            if result.provider_refused:
                response_status = "refused"
            elif (result.answer or "").strip() == "":
                # 空答/降级:UNKNOWN,不当 answered、不改写成 not_mentioned
                response_status = "unknown"

        return self._build_envelope(request, prompt_text, result, response_status, latency_ms)

    def _build_envelope(
        self,
        request: CollectionRequest,
        prompt_text: str,
        result: EngineResult,
        response_status: str,
        latency_ms: int,
    ) -> ObservationEnvelopeV1:
        # citations 映射:is_answer_cited → source_type=citation,否则 source。
        # P1-5:防御式解析 —— provider 返回的畸形 citation 绝不能让信封构建抛异常
        #       (否则付费调用已发生却因构建失败漏记成本);单条坏数据跳过,不整体失败。
        citations: List[CitationEvidence] = []
        for c in result.citations or []:
            try:
                if not isinstance(c, dict):
                    continue
                url = c.get("url")
                if not url or not isinstance(url, str):
                    continue
                cited = bool(c.get("is_answer_cited"))
                title = c.get("title")
                rank = c.get("rank")
                citations.append(CitationEvidence(
                    url=url,
                    title=title if isinstance(title, str) else None,
                    source_type="citation" if cited else "source",
                    rank=int(rank) if isinstance(rank, (int, float)) else None,
                ))
            except Exception as exc:
                logger.warning("[%s] 跳过畸形 citation: %s", self.surface_key, exc)
                continue

        # search_queries:只收 provider 明确返回的真实子查询;无则 None(禁止推断)。同样防御式。
        search_queries: Optional[List[SearchQueryEvidence]] = None
        if result.search_queries:
            sq: List[SearchQueryEvidence] = []
            for q in result.search_queries:
                try:
                    if isinstance(q, dict) and isinstance(q.get("text"), str) and q["text"]:
                        rank = q.get("rank")
                        kind = q.get("kind")
                        sq.append(SearchQueryEvidence(
                            text=q["text"],
                            kind=kind if isinstance(kind, str) else None,
                            rank=int(rank) if isinstance(rank, (int, float)) else None,
                        ))
                except Exception as exc:
                    logger.warning("[%s] 跳过畸形 search_query: %s", self.surface_key, exc)
                    continue
            search_queries = sq or None

        # P1-4:成本按真实 provider 调用次数(含重试)计
        usage = estimate_usage_cost(
            self.surface_key, result.input_tokens, result.output_tokens, result.cached_tokens,
            attempts=result.attempts,
        )

        # 真实执行身份(fallback 后反映真实执行者;否则用 spec 默认)
        provider_key = result.provider_key or self.spec.provider_key
        model_key = result.model_key or self.spec.default_model_key
        search_provider = result.search_provider if result.search_provider is not None else self.spec.search_provider
        search_enabled = (
            result.search_enabled if result.search_enabled is not None else self.spec.default_search_enabled
        )

        answer_text = result.answer or ""
        return ObservationEnvelopeV1(
            request_id=request.request_id,
            source_kind=request.source_kind,
            source_ref=request.source_ref,
            owner_user_id=request.owner_user_id,
            brand_id=request.brand_id,
            industry_key=request.industry_key,
            question_text=request.question_text,
            question_hash=canonical_question_hash(request.question_text),
            query_kind=request.query_kind,
            platform_key=self.spec.platform_key,
            provider_key=provider_key,
            model_key=model_key,
            model_revision=result.model_revision,
            search_provider=search_provider,
            surface_key=self.surface_key,  # type: ignore[arg-type]
            search_mode=request.search_mode,
            search_enabled=search_enabled,
            search_queries=search_queries,
            country_code=request.country_code,
            region_key=request.region_key,
            session_mode=request.session_mode,
            run_index=request.run_index,
            prompt_text=prompt_text,
            answer_text=answer_text,
            answer_hash=answer_text_hash(answer_text),
            response_status=response_status,  # type: ignore[arg-type]
            citations=citations,
            usage=usage,
            latency_ms=max(latency_ms, 0),
            retry_of_request_id=request.retry_of_request_id,
            provider_trace_id=result.provider_trace_id,
            observed_at=utcnow(),
        )

    def budget_blocked_envelope(self, request: CollectionRequest, reason: str) -> ObservationEnvelopeV1:
        """预算耗尽:返回 budget_blocked 信封,零 provider 调用、零成本。"""
        return ObservationEnvelopeV1(
            request_id=request.request_id,
            source_kind=request.source_kind,
            source_ref=request.source_ref,
            owner_user_id=request.owner_user_id,
            brand_id=request.brand_id,
            industry_key=request.industry_key,
            question_text=request.question_text,
            question_hash=canonical_question_hash(request.question_text),
            query_kind=request.query_kind,
            platform_key=self.spec.platform_key,
            provider_key=self.spec.provider_key,
            model_key=self.spec.default_model_key,
            surface_key=self.surface_key,  # type: ignore[arg-type]
            search_enabled=self.spec.default_search_enabled,
            search_provider=self.spec.search_provider,
            country_code=request.country_code,
            region_key=request.region_key,
            session_mode=request.session_mode,
            run_index=request.run_index,
            prompt_text=request.effective_prompt_text(),
            answer_text="",
            answer_hash=answer_text_hash(""),
            response_status="budget_blocked",
            citations=[],
            usage=estimate_usage_cost(self.surface_key, None, None, None).model_copy(
                update={"estimated_cost_micros": 0, "usage_is_estimated": False}
            ),
            latency_ms=0,
            retry_of_request_id=request.retry_of_request_id,
            observed_at=utcnow(),
        )


def _monotonic_ms() -> int:
    import time

    return int(time.monotonic() * 1000)


def _is_timeout(exc: BaseException) -> bool:
    """把各类超时归到 response_status='timeout',与传输 error 区分。

    覆盖:asyncio.TimeoutError(= TimeoutError);直接的 httpx.TimeoutException/ReadTimeout/
    ConnectTimeout/WriteTimeout/PoolTimeout(类名含 'timeout');以及包裹层
    RuntimeError('重试 N 次仍失败: <TimeoutClassName>: ...')(query_with_retry 把原异常类名嵌入消息)。

    第三层用**大小写敏感**的 ``"Timeout" in str(exc)``:超时异常类名都是 CamelCase 大写 T
    (ReadTimeout / ConnectTimeout / TimeoutError / APITimeoutError...),而 provider 错误体里
    泛化的 "timeout" 单词是小写(如 "retry after timeout window" / "invalid 'timeout' parameter"),
    大写 T 精确区分了两者 —— 既覆盖 httpx 子类的包裹消息,又不误标含小写 timeout 词的 429/400/500 body。
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if "timeout" in type(exc).__name__.lower():
        return True
    return "Timeout" in str(exc)  # 大小写敏感:匹配类名 CamelCase 大写 T,不匹配小写 prose
