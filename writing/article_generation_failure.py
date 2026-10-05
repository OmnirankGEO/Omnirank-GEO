"""Stable, customer-safe failure contract for article generation.

Raw provider, database and traceback text belongs in operator logs only.  This
module is deliberately dependency-light so the route, worker and tests all use
the same public error vocabulary without creating a second billing workflow.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ArticleGenerationFailure:
    code: str
    message: str
    retryable: bool
    phase: str

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArticleGenerationError(RuntimeError):
    """Typed internal error whose public representation is always bounded."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        phase: str,
    ) -> None:
        self.failure = ArticleGenerationFailure(code, message, retryable, phase)
        super().__init__(code)


class ArticleProviderUnsupported(ArticleGenerationError):
    def __init__(self) -> None:
        super().__init__(
            "ARTICLE_PROVIDER_UNSUPPORTED",
            "所选写作模型暂不受支持，请刷新模型列表后重试。",
            retryable=False,
            phase="provider_config",
        )


class ArticleProviderUnavailable(ArticleGenerationError):
    def __init__(self) -> None:
        super().__init__(
            "ARTICLE_PROVIDER_UNAVAILABLE",
            "写作模型暂时不可用，本次未交付内容，可稍后安全重试。",
            retryable=True,
            phase="provider",
        )


class ArticleOutputInvalid(ArticleGenerationError):
    def __init__(self) -> None:
        super().__init__(
            "ARTICLE_OUTPUT_INVALID",
            "模型未返回可用正文，本次未保存空白文章，可安全重试。",
            retryable=True,
            phase="provider_output",
        )


class ArticleSaveFailed(ArticleGenerationError):
    def __init__(self) -> None:
        super().__init__(
            "ARTICLE_SAVE_FAILED",
            "正文已生成但未能安全保存，本次没有覆盖原稿，可稍后重试。",
            retryable=True,
            phase="save",
        )


_MESSAGES = {
    # 法律禁止项是内容层唯一硬门。证据不足、排名依据、评分口径等问题
    # 必须随文保存为 advisory，不能再使用历史的全文证据拒绝码。
    "ARTICLE_LEGAL_PROHIBITION_BLOCKED": (
        "正文命中法律禁止的绝对化表述，本次未保存。"
        "请按提示定位修正该句后安全重试。"
    ),
    "ARTICLE_EVIDENCE_ADVISORY_FAILED": (
        "正文证据提示未能安全随文保存，原稿未被覆盖，可安全重试。"
    ),
    "ARTICLE_INTERNAL_ERROR": "文章任务暂时异常，本次未交付内容，可稍后安全重试。",
}


def classify_article_generation_failure(
    exc: BaseException | None,
    *,
    phase: str | None = None,
) -> ArticleGenerationFailure:
    """Map an arbitrary internal exception to the stable public contract."""
    if isinstance(exc, ArticleGenerationError):
        return exc.failure

    try:
        from writing.evidence_first_policy import EvidenceFirstViolation
    except Exception:  # pragma: no cover - import isolation for tiny utilities
        EvidenceFirstViolation = ()  # type: ignore[assignment,misc]
    if EvidenceFirstViolation and isinstance(exc, EvidenceFirstViolation):
        return ArticleGenerationFailure(
            "ARTICLE_LEGAL_PROHIBITION_BLOCKED",
            _MESSAGES["ARTICLE_LEGAL_PROHIBITION_BLOCKED"],
            False,
            "legal_gate",
        )
    try:
        from writing.evidence_precision_policy import EvidencePrecisionViolation
    except Exception:  # pragma: no cover - import isolation for tiny utilities
        EvidencePrecisionViolation = ()  # type: ignore[assignment,misc]
    if EvidencePrecisionViolation and isinstance(exc, EvidencePrecisionViolation):
        return ArticleGenerationFailure(
            "ARTICLE_EVIDENCE_ADVISORY_FAILED",
            _MESSAGES["ARTICLE_EVIDENCE_ADVISORY_FAILED"],
            True,
            "advisory_persistence",
        )

    exc_type = type(exc)
    exc_module = str(getattr(exc_type, "__module__", "")).lower()
    exc_name = str(getattr(exc_type, "__name__", "")).lower()
    if exc_module.startswith(("openai", "httpx")) or any(
        token in exc_name for token in ("timeout", "ratelimit", "connection", "apierror")
    ):
        return ArticleProviderUnavailable().failure

    normalized_phase = str(phase or "").strip().lower()
    if normalized_phase in {"save", "database", "persist"}:
        return ArticleSaveFailed().failure
    if normalized_phase in {"provider", "llm", "provider_call"}:
        return ArticleProviderUnavailable().failure
    if normalized_phase in {"output", "provider_output", "validation"}:
        return ArticleOutputInvalid().failure
    return ArticleGenerationFailure(
        "ARTICLE_INTERNAL_ERROR",
        _MESSAGES["ARTICLE_INTERNAL_ERROR"],
        True,
        normalized_phase or "worker",
    )
