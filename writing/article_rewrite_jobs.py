"""In-memory coordination for article rewrite jobs.

The batch rewrite API is intentionally async: the HTTP request only enqueues a
job and the frontend polls by batch_ref. A short-lived in-process idempotency
store prevents users from double-charging themselves by retrying after browser
or proxy timeouts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid
from typing import Callable, Dict, Iterable, List, Optional, Tuple


TERMINAL_STATUSES = {"completed", "failed", "billing_failed"}


def classify_batch_rewrite_error(error: object) -> str:
    text = str(error or "")
    if "EVIDENCE_FIRST_BLOCKED:" in text:
        return "content_trust_check_failed"
    if "not JSON serializable" in text:
        return "article_data_processing_failed"
    return "article_generation_failed"


def public_batch_rewrite_errors(errors: Iterable[object]) -> List[Dict]:
    public: List[Dict] = []
    for item in errors or []:
        row = item if isinstance(item, dict) else {}
        public.append({
            "topic_id": row.get("topic_id"),
            "reason": classify_batch_rewrite_error(row.get("error") if row else item),
        })
    return public


def batch_rewrite_failure_message(errors: Iterable[object]) -> str:
    reasons = {item["reason"] for item in public_batch_rewrite_errors(errors)}
    if reasons == {"content_trust_check_failed"}:
        return "所选文章未通过内容可信校验，原稿已保留，本次未收费"
    if reasons == {"article_data_processing_failed"}:
        return "文章资料处理异常，原稿已保留，本次未收费"
    return "所选文章暂未完成重写，原稿已保留，本次未收费"


def _normalise_topic_ids(topic_ids: Iterable[int]) -> Tuple[int, ...]:
    return tuple(sorted({int(tid) for tid in topic_ids}))


def make_batch_rewrite_key(
    *,
    actor_key: str,
    quote_id: int,
    topic_ids: Iterable[int],
    payload_fingerprint: str = "default",
) -> str:
    """Build a stable idempotency key for the same actor + quote + topic set."""
    ids = ",".join(str(tid) for tid in _normalise_topic_ids(topic_ids))
    return f"{actor_key}|quote:{int(quote_id)}|topics:{ids}|payload:{payload_fingerprint or 'default'}"


@dataclass
class BatchRewriteJob:
    batch_ref: str
    idempotency_key: str
    actor_key: str
    quote_id: int
    topic_ids: List[int]
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    message: str = "已提交，后台正在重写"
    result: Dict = field(default_factory=dict)
    error: Optional[str] = None
    charged_count: int = 0
    charged_points: int = 0

    def touch(self, now: Optional[float] = None) -> None:
        self.updated_at = time.time() if now is None else now

    def to_public_dict(self) -> Dict:
        return {
            "success": self.status not in {"failed", "billing_failed"},
            "batch_ref": self.batch_ref,
            "status": self.status,
            "message": self.message,
            "quote_id": self.quote_id,
            "topic_ids": list(self.topic_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "charged_count": self.charged_count,
            "charged_points": self.charged_points,
            **(self.result or {}),
            **({"error": self.error} if self.error else {}),
        }


class BatchRewriteJobStore:
    """Tiny TTL idempotency store.

    WORKERS=1 in production, so an in-process store is enough to prevent the
    repeated-click / timeout-retry double charge that triggered the incident.
    """

    def __init__(
        self,
        ttl_seconds: int = 600,
        ref_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self.ttl_seconds = int(ttl_seconds)
        self._ref_factory = ref_factory or (lambda: uuid.uuid4().hex)
        self._jobs_by_ref: Dict[str, BatchRewriteJob] = {}
        self._refs_by_key: Dict[str, str] = {}

    def _now(self, now: Optional[float] = None) -> float:
        return time.time() if now is None else float(now)

    def prune(self, now: Optional[float] = None) -> None:
        current = self._now(now)
        # [GEO-R6-CAN-007] Never evict a job that is still active (queued/running)
        # by wall-clock age. Evicting a running job drops its idempotency-key
        # mapping, so a timeout-retry mints a fresh job and re-dispatches +
        # double-charges. Only terminal jobs (completed/failed/billing_failed)
        # are aged out — their results just need to survive long enough to poll.
        expired_refs = [
            ref
            for ref, job in self._jobs_by_ref.items()
            if job.status in TERMINAL_STATUSES
            and current - job.created_at > self.ttl_seconds
        ]
        for ref in expired_refs:
            job = self._jobs_by_ref.pop(ref, None)
            if job:
                self._refs_by_key.pop(job.idempotency_key, None)

    def get(self, batch_ref: str, now: Optional[float] = None) -> Optional[BatchRewriteJob]:
        self.prune(now)
        return self._jobs_by_ref.get(str(batch_ref))

    def remove(self, batch_ref: str) -> None:
        job = self._jobs_by_ref.pop(str(batch_ref), None)
        if job:
            self._refs_by_key.pop(job.idempotency_key, None)

    def get_or_create(
        self,
        *,
        actor_key: str,
        quote_id: int,
        topic_ids: Iterable[int],
        payload_fingerprint: str = "default",
        now: Optional[float] = None,
    ) -> Tuple[BatchRewriteJob, bool]:
        current = self._now(now)
        self.prune(current)
        normalised_ids = list(_normalise_topic_ids(topic_ids))
        key = make_batch_rewrite_key(
            actor_key=actor_key,
            quote_id=quote_id,
            topic_ids=normalised_ids,
            payload_fingerprint=payload_fingerprint,
        )
        existing_ref = self._refs_by_key.get(key)
        if existing_ref:
            existing = self._jobs_by_ref.get(existing_ref)
            if existing:
                return existing, False

        ref = self._ref_factory()
        job = BatchRewriteJob(
            batch_ref=ref,
            idempotency_key=key,
            actor_key=actor_key,
            quote_id=int(quote_id),
            topic_ids=normalised_ids,
            created_at=current,
            updated_at=current,
        )
        self._jobs_by_ref[ref] = job
        self._refs_by_key[key] = ref
        return job, True

    def mark_running(self, batch_ref: str, now: Optional[float] = None) -> None:
        job = self._jobs_by_ref[batch_ref]
        job.status = "running"
        job.message = "后台正在重写，请稍候刷新查看结果"
        job.touch(self._now(now))

    def mark_completed(
        self,
        batch_ref: str,
        *,
        result: Dict,
        charged_count: int = 0,
        charged_points: int = 0,
        now: Optional[float] = None,
    ) -> None:
        job = self._jobs_by_ref[batch_ref]
        job.status = "completed"
        job.message = "批量重写完成"
        job.result = result or {}
        job.charged_count = int(charged_count or 0)
        job.charged_points = int(charged_points or 0)
        job.touch(self._now(now))

    def mark_failed(
        self,
        batch_ref: str,
        *,
        error: str,
        result: Optional[Dict] = None,
        status: str = "failed",
        message: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        job = self._jobs_by_ref[batch_ref]
        job.status = status
        job.message = message or "批量重写失败，本次未收费"
        job.error = str(error)[:500]
        job.result = result or {}
        job.touch(self._now(now))
