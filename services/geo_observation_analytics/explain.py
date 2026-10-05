"""Semantic insight engine — the ONLY place an LLM is called, and it never
computes a metric.

Contract (00_MASTER_SPEC I12/§6, 03 §6.1, 04 §8.5):
  * Deterministic backend produces facts/evidence/allowed_action_types first.
  * The model receives an ANONYMIZED MINIMAL fact pack: aggregate metrics,
    owner-authorized opaque evidence refs, anonymized source themes, allowed
    actions. NEVER owner/brand/user/agent id, upstream/channel, cost, full
    question, full raw answer, other-customer evidence, or provider trace.
  * Official DeepSeek only: base_url=https://api.deepseek.com,
    model=deepseek-v4-flash, thinking=disabled, key=DEEPSEEK_API_KEY. NO
    DashScope / third-party fallback that would still be logged as official.
  * Idempotent by (input_hash, versions): browsing/filtering/pagination never
    calls the model; only an explicit generate does. 20 concurrent generates of
    the same input create exactly one job / one model call.
  * Output is a strongly-typed JSON schema; the model may only reference the
    provided evidence refs and allowed actions. On any failure the deterministic
    facts stay readable and the insight is marked unavailable — no hardcoded
    fallback, no retry loop.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Protocol

from psycopg2.extras import RealDictCursor

from .contract import (
    CONTRACT_VERSION,
    METRIC_VERSION,
    PRIVACY_FORBIDDEN_FIELDS,
    SEMANTIC_BASE_URL,
    SEMANTIC_MODEL,
    SEMANTIC_PROMPT_VERSION,
    SEMANTIC_PROVIDER,
    SEMANTIC_SCHEMA_VERSION,
    SEMANTIC_THINKING,
)
from db.xact_lock_guard import require_xact_scope

SYSTEM_PROMPT = (
    "你是 OmniRank 的 GEO 观测解读助手。只能基于用户消息里提供的结构化事实进行解释，"
    "严禁编造数字、排名、品牌或来源，严禁给出'保证上榜'类承诺。"
    "只能引用 evidence_refs 中给出的编号，只能建议 allowed_action_types 中列出的动作。"
    "无法给出有依据的结论时，state 返回 insufficient。"
    "只返回 JSON：{\"summary\": string, \"evidence_refs\": string[], "
    "\"allowed_actions\": string[], \"state\": \"ok\"|\"insufficient\"}。"
)

_INSIGHT_UNAVAILABLE_CODE = "SEMANTIC_INSIGHT_UNAVAILABLE"
# a claimed job holds a lease this long; if the owning worker dies mid-run the
# lease expires and the next explicit generate (or the sweeper) recovers it.
INSIGHT_LEASE_SECONDS = 120
INSIGHT_DAILY_BRAND_PAID_CALL_LIMIT = 20
INSIGHT_DAILY_OWNER_PAID_CALL_LIMIT = 50


# ---------------------------------------------------------------------------
# Anonymized minimal fact pack
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FactPack:
    contract_version: str
    metric_version: str
    industry: str
    window: dict
    sample_size: int
    metrics_bps: dict
    outcome_counts: dict
    stability_status: str
    model_shift: bool
    evidence_refs: list[str]
    allowed_action_types: list[str]

    def to_model_payload(self) -> dict:
        """The exact object sent to the model. Asserts zero forbidden fields."""
        payload = asdict(self)
        _assert_pack_is_anonymous(payload)
        return payload

    def input_hash(self) -> str:
        payload = asdict(self)
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        versioned = f"{SEMANTIC_MODEL}\x1f{SEMANTIC_PROMPT_VERSION}\x1f{SEMANTIC_SCHEMA_VERSION}\x1f{canonical}"
        return hashlib.sha256(versioned.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InsightSnapshot:
    policy_basis_hash: str
    scope_type: str
    bucket_granularity: str
    bucket_start: object
    bucket_epoch: int
    promotion_sequence_watermark: int
    aggregate_input_watermark: object
    aggregate_contract_version: str
    aggregate_aggregation_version: str
    aggregate_metric_version: str

    def snapshot_id(self) -> str:
        payload = "\x1f".join((
            self.policy_basis_hash, self.scope_type, self.bucket_granularity,
            _canonical_snapshot_value(self.bucket_start), str(self.bucket_epoch),
            str(self.promotion_sequence_watermark),
            _canonical_snapshot_value(self.aggregate_input_watermark),
            self.aggregate_contract_version,
            self.aggregate_aggregation_version, self.aggregate_metric_version,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_snapshot_value(value: object) -> str:
    """Stable cross-timezone serialization for immutable snapshot identity."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def snapshot_from_aggregate(row: dict) -> InsightSnapshot:
    return InsightSnapshot(
        policy_basis_hash=str(row["policy_basis_hash"]),
        scope_type=str(row["scope_type"]),
        bucket_granularity=str(row["bucket_granularity"]),
        bucket_start=row["bucket_start"],
        bucket_epoch=int(row["eligibility_epoch"]),
        promotion_sequence_watermark=int(row["promotion_sequence_watermark"]),
        aggregate_input_watermark=row["input_watermark"],
        aggregate_contract_version=str(row["contract_version"]),
        aggregate_aggregation_version=str(row["aggregation_version"]),
        aggregate_metric_version=str(row["metric_version"]),
    )


def job_matches_snapshot(job: dict, snapshot: InsightSnapshot) -> bool:
    return bool(job.get("snapshot_id")) and all((
        str(job["snapshot_id"]) == snapshot.snapshot_id(),
        str(job["policy_basis_hash"]) == snapshot.policy_basis_hash,
        str(job["scope_type"]) == snapshot.scope_type,
        str(job["bucket_granularity"]) == snapshot.bucket_granularity,
        job["bucket_start"] == snapshot.bucket_start,
        int(job["bucket_epoch"]) == snapshot.bucket_epoch,
        int(job["promotion_sequence_watermark"]) == snapshot.promotion_sequence_watermark,
        job["aggregate_input_watermark"] == snapshot.aggregate_input_watermark,
        str(job["aggregate_contract_version"]) == snapshot.aggregate_contract_version,
        str(job["aggregate_aggregation_version"]) == snapshot.aggregate_aggregation_version,
        str(job["aggregate_metric_version"]) == snapshot.aggregate_metric_version,
    ))


def _assert_pack_is_anonymous(payload: dict) -> None:
    """Defense in depth: refuse to build a request body carrying tenant/upstream
    /cost/trace keys, or obvious raw-text keys."""
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield str(k)
                yield from walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                yield from walk(v)

    keys = set(walk(payload))
    forbidden = keys & (PRIVACY_FORBIDDEN_FIELDS | {"brand_id"})
    # raw text keys are never allowed in the pack
    forbidden |= keys & {"question_text", "answer_text", "prompt_text", "full_response"}
    if forbidden:
        raise ValueError(f"fact pack is not anonymous, carries: {sorted(forbidden)}")


def build_fact_pack(
    *,
    industry_key: str,
    window: dict,
    outcome_counts: dict,
    metrics_bps: dict,
    sample_size: int,
    stability_status: str,
    model_shift: bool,
    evidence_refs: list[str],
    allowed_action_types: list[str],
) -> FactPack:
    return FactPack(
        contract_version=CONTRACT_VERSION,
        metric_version=METRIC_VERSION,
        industry=industry_key,
        window=dict(window),
        sample_size=int(sample_size),
        metrics_bps=dict(metrics_bps),
        outcome_counts=dict(outcome_counts),
        stability_status=stability_status,
        model_shift=bool(model_shift),
        evidence_refs=list(evidence_refs),
        allowed_action_types=list(allowed_action_types),
    )


# ---------------------------------------------------------------------------
# Validated model output
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InsightResult:
    summary: str
    evidence_refs: list[str]
    allowed_actions: list[str]
    state: str  # ok | insufficient


class InsightValidationError(ValueError):
    pass


class InsightUnavailableError(RuntimeError):
    pass


def parse_and_validate(raw_text: str, fact_pack: FactPack) -> InsightResult:
    """Parse the model's JSON and constrain it to the provided allowlists.

    The model may only reference evidence refs and actions that were supplied;
    anything else is rejected (the model cannot invent refs, actions, or leak).
    """
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InsightValidationError(f"non-JSON model output: {exc}") from exc
    if not isinstance(data, dict):
        raise InsightValidationError("model output is not a JSON object")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise InsightValidationError("missing summary")
    state = data.get("state", "ok")
    if state not in ("ok", "insufficient"):
        raise InsightValidationError(f"bad state: {state!r}")
    refs = data.get("evidence_refs", []) or []
    actions = data.get("allowed_actions", []) or []
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        raise InsightValidationError("evidence_refs must be a string list")
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        raise InsightValidationError("allowed_actions must be a string list")
    allowed_refs = set(fact_pack.evidence_refs)
    allowed_actions = set(fact_pack.allowed_action_types)
    bad_refs = [r for r in refs if r not in allowed_refs]
    bad_actions = [a for a in actions if a not in allowed_actions]
    if bad_refs:
        raise InsightValidationError(f"model referenced unknown evidence: {bad_refs}")
    if bad_actions:
        raise InsightValidationError(f"model proposed unknown actions: {bad_actions}")
    return InsightResult(summary=summary, evidence_refs=refs, allowed_actions=actions, state=state)


# ---------------------------------------------------------------------------
# Transport (injectable). Default = official DeepSeek, no fallback.
# ---------------------------------------------------------------------------
class Transport(Protocol):
    def chat(self, request_body: dict) -> dict:  # pragma: no cover - protocol
        ...


def build_request_body(fact_pack: FactPack) -> dict:
    """The exact HTTP request body sent to DeepSeek. Tests intercept this and
    assert zero forbidden fields / no raw tenant text."""
    return {
        "model": SEMANTIC_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    fact_pack.to_model_payload(), ensure_ascii=False, sort_keys=True
                ),
            },
        ],
        "temperature": 0.2,
        "thinking": {"type": SEMANTIC_THINKING},
        "response_format": {"type": "json_object"},
        "stream": False,
    }


class OfficialDeepSeekTransport:
    """Strictly official DeepSeek. Reuses the existing key pool; never falls back
    to DashScope (which would still be logged as 'deepseek')."""

    URL = SEMANTIC_BASE_URL + "/v1/chat/completions"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0):
        self._api_key = api_key
        self._timeout = timeout

    def _key(self) -> str:
        if self._api_key:
            return self._api_key
        # reuse the existing official key pool; do NOT create a new key system
        try:
            from services.llm.deepseek_key_pool import pick_deepseek_api_key
            key = pick_deepseek_api_key("realtime")
        except Exception:
            key = None
        if not key:
            import os
            key = os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("official DeepSeek API key unavailable")
        return key

    def chat(self, request_body: dict, tracking_context: Optional[dict] = None) -> dict:
        import httpx  # local import; not needed for unit tests
        from tools.llm_call_tracker import llm_track_sync, usage_from_response_payload

        headers = {"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"}
        tracking_context = dict(tracking_context or {})
        with llm_track_sync(
            caller="geo_observation_insight",
            platform="deepseek",
            model=str(request_body.get("model") or SEMANTIC_MODEL),
            metadata=tracking_context,
        ) as tracker:
            resp = httpx.post(self.URL, json=request_body, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            payload = resp.json()
            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(payload)
            tracker.record(
                input_tokens=input_tokens, output_tokens=output_tokens,
                cached_tokens=cached_tokens, success=True,
            )
            return payload


def _extract_text(response: dict) -> str:
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise InsightValidationError(f"unexpected model response shape: {exc}") from exc


# ---------------------------------------------------------------------------
# Job store (DB-backed idempotency + concurrency). Table geo_observation_insight_jobs
# ---------------------------------------------------------------------------
@dataclass
class InsightEngine:
    transport: Transport
    provider: str = SEMANTIC_PROVIDER
    model: str = SEMANTIC_MODEL
    prompt_version: str = SEMANTIC_PROMPT_VERSION
    schema_version: str = SEMANTIC_SCHEMA_VERSION
    clock: object = field(default=None)

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        return datetime.now(timezone.utc)

    def create_job(
        self, conn, fact_pack: FactPack, request_id: str,
        owner_user_id: int, brand_id: int, snapshot: InsightSnapshot,
    ) -> dict:
        """Claim-or-get an idempotent job scoped to (owner_user_id, brand_id,
        input_hash). Returns the job row.

        Claiming atomically INSERTs a new job OR reclaims an existing one that is
        ``failed`` or whose lease has expired (crashed worker) — so a failed or
        stuck insight is retryable on the next explicit generate, and a process
        death mid-run never leaves a permanent pending. A ``completed`` or an
        actively-leased ``running/pending`` job is returned as-is (cached; no new
        model call). Two tenants NEVER share a job. Only the claimer runs the
        model, exactly once."""
        input_hash = fact_pack.input_hash()
        job_id = f"insight-{uuid.uuid4().hex[:16]}"
        lease_token = str(uuid.uuid4())
        now = self._now()
        lease_until = now + timedelta(seconds=INSIGHT_LEASE_SECONDS)
        snapshot_id = snapshot.snapshot_id()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            require_xact_scope(cur, where="explain.create_job")  # §1 硬闸:autocommit 下取事务锁=没锁
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"geo_obs_insight_budget_owner::{owner_user_id}",),
            )
            self._recover_expired_jobs(
                cur, now=now, owner_user_id=owner_user_id, brand_id=brand_id,
                input_hash=input_hash, snapshot_id=snapshot_id,
            )
            cur.execute(
                """SELECT * FROM public.geo_observation_insight_jobs
                    WHERE owner_user_id=%s AND brand_id=%s AND input_hash=%s
                      AND snapshot_id=%s""",
                (owner_user_id, brand_id, input_hash, snapshot_id),
            )
            existing = cur.fetchone()
            reclaimable = bool(existing) and (
                existing["paid_call_started_at"] is None
                and (
                    existing["state"] == "failed"
                    or (
                        existing["state"] in ("pending", "running")
                        and existing["lease_until"] is not None
                        and existing["lease_until"] < now
                    )
                )
            )
            if existing and not reclaimable:
                conn.commit()
                return existing
            if existing is None:
                cur.execute(
                    """SELECT COUNT(*) FILTER (WHERE brand_id=%s) AS brand_calls,
                              COUNT(*) AS owner_calls
                         FROM public.geo_observation_insight_jobs
                        WHERE owner_user_id=%s
                          AND budget_reserved_at >= (
                              date_trunc('day', %s::timestamptz AT TIME ZONE 'UTC')
                              AT TIME ZONE 'UTC'
                          )""",
                    (brand_id, owner_user_id, now),
                )
                usage = cur.fetchone()
                if int(usage["brand_calls"] or 0) >= INSIGHT_DAILY_BRAND_PAID_CALL_LIMIT or int(
                    usage["owner_calls"] or 0
                ) >= INSIGHT_DAILY_OWNER_PAID_CALL_LIMIT:
                    conn.rollback()
                    raise InsightUnavailableError("semantic insight daily paid-call limit reached")
            cur.execute(
                """
                INSERT INTO public.geo_observation_insight_jobs AS insight_jobs
                    (job_id, input_hash, owner_user_id, brand_id, request_id, state,
                     model, provider, prompt_version, schema_version, lease_token, lease_until,
                     attempts, created_at, updated_at,snapshot_id,policy_basis_hash,scope_type,
                     bucket_granularity,bucket_start,bucket_epoch,promotion_sequence_watermark,
                     aggregate_input_watermark,aggregate_contract_version,
                     aggregate_aggregation_version,aggregate_metric_version)
                VALUES (%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,1,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (owner_user_id, brand_id, input_hash, snapshot_id) DO UPDATE
                    SET state='pending', request_id=EXCLUDED.request_id,
                        lease_token=EXCLUDED.lease_token, lease_until=EXCLUDED.lease_until,
                        attempts=insight_jobs.attempts + 1,
                        summary=NULL, evidence_refs='[]'::jsonb, allowed_actions='[]'::jsonb,
                        result_state=NULL, error_code=NULL, error_detail=NULL,
                        updated_at=EXCLUDED.updated_at
                    WHERE (insight_jobs.state = 'failed'
                           AND insight_jobs.paid_call_started_at IS NULL)
                       OR (insight_jobs.state IN ('pending','running')
                           AND insight_jobs.paid_call_started_at IS NULL
                           AND insight_jobs.lease_until < EXCLUDED.updated_at)
                RETURNING job_id, lease_token
                """,
                (job_id, input_hash, owner_user_id, brand_id, request_id, self.model,
                 self.provider, self.prompt_version, self.schema_version, lease_token, lease_until,
                 now, now, snapshot_id, snapshot.policy_basis_hash, snapshot.scope_type,
                 snapshot.bucket_granularity, snapshot.bucket_start, snapshot.bucket_epoch,
                 snapshot.promotion_sequence_watermark, snapshot.aggregate_input_watermark,
                 snapshot.aggregate_contract_version, snapshot.aggregate_aggregation_version,
                 snapshot.aggregate_metric_version),
            )
            claimed = cur.fetchone()
        conn.commit()
        if claimed is None:
            # existing job is completed or actively leased -> return it, no re-run
            return self._get_by_hash(conn, owner_user_id, brand_id, input_hash, snapshot_id)
        # we own the (fresh or reclaimed) job -> run the model exactly once, inline,
        # fencing every write on our lease_token so a revived prior worker whose
        # lease was superseded can never overwrite our result.
        self._run_job(conn, claimed["job_id"], claimed["lease_token"], fact_pack)
        return self._get_by_id(conn, claimed["job_id"])

    def recover_stuck_jobs(self, conn) -> int:
        """Sweeper (register under ROLE=cron): mark lease-expired pending/running
        unpaid jobs as failed. Any paid-call lease loss becomes result_unknown
        and can never be automatically reclaimed or paid again."""
        now = self._now()
        with conn.cursor() as cur:
            n = self._recover_expired_jobs(cur, now=now)
        conn.commit()
        return n

    def _recover_expired_jobs(
        self,
        cur,
        *,
        now: datetime,
        owner_user_id: Optional[int] = None,
        brand_id: Optional[int] = None,
        input_hash: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> int:
        """Expected-state CAS recovery used by create/get and the cron sweep.

        A provider call whose lease expired is never reclaimed. Clearing its
        lease token in the same CAS fences the old worker permanently.
        """
        where: list[str] = []
        params: list[object] = []
        for column, value in (
            ("owner_user_id", owner_user_id), ("brand_id", brand_id),
            ("input_hash", input_hash), ("snapshot_id", snapshot_id),
            ("job_id", job_id),
        ):
            if value is not None:
                where.append(f"{column}=%s")
                params.append(value)
        suffix = (" AND " + " AND ".join(where)) if where else ""
        cur.execute(
            """UPDATE public.geo_observation_insight_jobs
                   SET state='result_unknown', error_code=%s,
                       error_detail='paid call result unknown',paid_call_unknown_at=%s,
                       updated_at=%s,lease_token=NULL,lease_until=NULL
               WHERE state='paid_call_started'
                 AND paid_call_started_at IS NOT NULL
                 AND lease_until IS NOT NULL AND lease_until < %s""" + suffix,
            (_INSIGHT_UNAVAILABLE_CODE, now, now, now, *params),
        )
        n = cur.rowcount
        cur.execute(
            """UPDATE public.geo_observation_insight_jobs
                   SET state='failed', error_code=%s,
                       error_detail='lease expired before paid call',
                       updated_at=%s,lease_token=NULL,lease_until=NULL
               WHERE state IN ('pending','running') AND paid_call_started_at IS NULL
                 AND lease_until IS NOT NULL AND lease_until < %s""" + suffix,
            (_INSIGHT_UNAVAILABLE_CODE, now, now, *params),
        )
        return n + cur.rowcount

    def _run_job(self, conn, job_id: str, lease_token: str, fact_pack: FactPack) -> None:
        # Build/validate the anonymous body before crossing the paid-call point.
        if self._set_state(conn, job_id, lease_token, "pending", "running") == 0:
            return
        try:
            body = build_request_body(fact_pack)
        except Exception as exc:
            self._fail(conn, job_id, lease_token, "running", None, str(exc))
            return
        try:
            paid_call_started_at = self._mark_paid_call_started(conn, job_id, lease_token)
        except InsightUnavailableError as exc:
            self._fail(conn, job_id, lease_token, "running", None, str(exc))
            raise
        if paid_call_started_at is None:
            self._fail(
                conn, job_id, lease_token, "running", None,
                "aggregate snapshot is no longer eligible",
            )
            return
        try:
            tracking = {
                "job_id": job_id,
                "request_id": self._request_id(conn, job_id),
                "call_purpose": "geo_observation_semantic_insight",
            }
            if "tracking_context" in inspect.signature(self.transport.chat).parameters:
                response = self.transport.chat(body, tracking_context=tracking)
            else:
                response = self.transport.chat(body)
        except Exception as exc:
            self._mark_result_unknown(
                conn, job_id, lease_token, paid_call_started_at, str(exc)
            )
            return
        try:
            text = _extract_text(response)
            result = parse_and_validate(text, fact_pack)
            usage = response.get("usage", {}) if isinstance(response, dict) else {}
            self._complete(
                conn, job_id, lease_token, paid_call_started_at, result, usage
            )
        except Exception as exc:  # noqa: BLE001 - all failures => unavailable, no fallback
            self._fail(
                conn, job_id, lease_token, "paid_call_started",
                paid_call_started_at, str(exc),
            )

    def _mark_paid_call_started(
        self, conn, job_id: str, lease_token: str,
    ) -> Optional[datetime]:
        started_at = self._now()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT owner_user_id,brand_id,snapshot_id,policy_basis_hash,
                          scope_type,bucket_granularity,bucket_start,bucket_epoch,
                          promotion_sequence_watermark,aggregate_input_watermark,
                          aggregate_contract_version,aggregate_aggregation_version,
                          aggregate_metric_version
                     FROM public.geo_observation_insight_jobs
                    WHERE job_id=%s AND lease_token=%s AND state='running'
                      AND paid_call_started_at IS NULL""",
                (job_id, lease_token),
            )
            identity = cur.fetchone()
            if identity is None:
                conn.rollback()
                return None
            require_xact_scope(cur, where="explain._mark_paid_call_started")  # §1 硬闸:autocommit 下取事务锁=没锁
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"geo_obs_insight_budget_owner::{identity['owner_user_id']}",),
            )
            # Final paid-call fence, after the owner lock, in this fresh READ
            # COMMITTED transaction.  It prevents both stale-budget double spend
            # and provider calls against a withdrawn/deleted published snapshot.
            from . import repository as observation_repo
            receipt, receipt_problems = observation_repo.published_scope_snapshot(
                conn,
                policy_basis_hash=identity["policy_basis_hash"],
                scope_type=identity["scope_type"],
                granularity=identity["bucket_granularity"],
                owner_user_id=int(identity["owner_user_id"]),
                brand_id=int(identity["brand_id"]),
            )
            if receipt is None or receipt_problems:
                conn.rollback()
                return None
            receipt_snapshot = InsightSnapshot(
                policy_basis_hash=str(receipt["policy_basis_hash"]),
                scope_type=str(receipt["scope_type"]),
                bucket_granularity=str(receipt["bucket_granularity"]),
                bucket_start=receipt["bucket_start"],
                bucket_epoch=int(receipt["eligibility_epoch"]),
                promotion_sequence_watermark=int(receipt["promotion_sequence_watermark"]),
                aggregate_input_watermark=receipt["input_watermark"],
                aggregate_contract_version=str(receipt["contract_version"]),
                aggregate_aggregation_version=str(receipt["aggregation_version"]),
                aggregate_metric_version=str(receipt["metric_version"]),
            )
            if receipt_snapshot.snapshot_id() != str(identity["snapshot_id"]):
                conn.rollback()
                return None
            cur.execute(
                """SELECT COUNT(*) FILTER (WHERE brand_id=%s) AS brand_calls,
                          COUNT(*) AS owner_calls
                     FROM public.geo_observation_insight_jobs
                    WHERE owner_user_id=%s
                      AND budget_reserved_at >= (
                          date_trunc('day', %s::timestamptz AT TIME ZONE 'UTC')
                          AT TIME ZONE 'UTC'
                      )""",
                (identity["brand_id"], identity["owner_user_id"], started_at),
            )
            usage = cur.fetchone()
            if (
                int(usage["brand_calls"] or 0) >= INSIGHT_DAILY_BRAND_PAID_CALL_LIMIT
                or int(usage["owner_calls"] or 0) >= INSIGHT_DAILY_OWNER_PAID_CALL_LIMIT
            ):
                conn.rollback()
                raise InsightUnavailableError("semantic insight daily paid-call limit reached")
            cur.execute(
                """UPDATE public.geo_observation_insight_jobs AS j
                      SET state='paid_call_started',paid_call_started_at=%s,
                          budget_reserved_at=%s,updated_at=%s
                    WHERE j.job_id=%s AND j.lease_token=%s AND j.state='running'
                      AND j.paid_call_started_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM public.geo_observation_events overdue
                           WHERE overdue.processing_state='promoted'
                             AND overdue.retention_until IS NOT NULL
                             AND overdue.retention_until<=clock_timestamp()
                             AND overdue.owner_user_id=j.owner_user_id
                             AND overdue.brand_id=j.brand_id
                             AND overdue.promotion_seq<=j.promotion_sequence_watermark
                             AND overdue.observed_at >=
                                 (j.bucket_start::timestamp AT TIME ZONE 'UTC')
                             AND overdue.observed_at < CASE j.bucket_granularity
                                 WHEN 'day' THEN
                                     ((j.bucket_start + 1)::timestamp AT TIME ZONE 'UTC')
                                 WHEN 'week' THEN
                                     ((j.bucket_start + 7)::timestamp AT TIME ZONE 'UTC')
                                 WHEN 'month' THEN
                                     ((date_trunc('month',j.bucket_start::timestamp)
                                       + interval '1 month') AT TIME ZONE 'UTC')
                                 ELSE (j.bucket_start::timestamp AT TIME ZONE 'UTC')
                             END
                      )
                      AND EXISTS (
                          SELECT 1
                            FROM public.geo_observation_aggregates a
                            JOIN public.geo_observation_aggregate_bucket_revision r
                              ON r.scope_type=a.scope_type
                             AND r.bucket_granularity=a.bucket_granularity
                             AND r.bucket_start=a.bucket_start
                            JOIN public.geo_observation_aggregate_refresh_manifest m
                              ON m.policy_basis_hash=a.policy_basis_hash
                             AND m.contract_version=a.contract_version
                             AND m.aggregation_version=a.aggregation_version
                             AND m.metric_version=a.metric_version
                             AND m.scope_type=a.scope_type
                             AND m.bucket_granularity=a.bucket_granularity
                             AND m.bucket_start=a.bucket_start
                             AND m.eligibility_epoch=a.eligibility_epoch
                             AND m.promotion_sequence_watermark=a.promotion_sequence_watermark
                             AND m.input_watermark=a.input_watermark
                           WHERE a.policy_basis_hash=j.policy_basis_hash
                             AND a.scope_type=j.scope_type
                             AND a.bucket_granularity=j.bucket_granularity
                             AND a.bucket_start=j.bucket_start
                             AND a.eligibility_epoch=j.bucket_epoch
                             AND a.promotion_sequence_watermark=j.promotion_sequence_watermark
                             AND a.input_watermark=j.aggregate_input_watermark
                             AND a.contract_version=j.aggregate_contract_version
                             AND a.aggregation_version=j.aggregate_aggregation_version
                             AND a.metric_version=j.aggregate_metric_version
                             AND a.owner_user_id=j.owner_user_id
                             AND a.brand_id=j.brand_id
                             AND a.platform_key IS NULL AND a.surface_key IS NULL
                             AND a.source_type IS NULL AND a.is_branded_prompt IS NULL
                             AND a.prompt_intent IS NULL AND a.search_enabled IS NULL
                             AND a.prompt_family_key IS NULL AND a.model_revision IS NULL
                             AND a.search_query_theme IS NULL
                             AND r.epoch=a.eligibility_epoch AND r.dirty=FALSE
                             AND r.published_receipt->>'manifest_key'=m.manifest_key
                             AND r.published_receipt->>'policy_basis_hash'=j.policy_basis_hash
                             AND (r.published_receipt->>'eligibility_epoch')::bigint=j.bucket_epoch
                             AND (r.published_receipt->>'promotion_sequence_watermark')::bigint=
                                 j.promotion_sequence_watermark
                             AND (r.published_receipt->>'input_watermark')::timestamptz=
                                 j.aggregate_input_watermark
                      )
                  RETURNING j.paid_call_started_at""",
                (started_at, started_at, started_at, job_id, lease_token),
            )
            row = cur.fetchone()
        conn.commit()
        return row["paid_call_started_at"] if row else None

    def _mark_result_unknown(
        self, conn, job_id: str, lease_token: str,
        paid_call_started_at: datetime, error: str,
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.geo_observation_insight_jobs SET state='result_unknown',
                       paid_call_unknown_at=%s,error_code=%s,error_detail=%s,updated_at=%s,
                       lease_token=NULL,lease_until=NULL
                    WHERE job_id=%s AND lease_token=%s AND state='paid_call_started'
                      AND paid_call_started_at=%s""",
                (self._now(), _INSIGHT_UNAVAILABLE_CODE, error[:500], self._now(),
                 job_id, lease_token, paid_call_started_at),
            )
            n = cur.rowcount
        conn.commit()
        return n

    def _request_id(self, conn, job_id: str) -> Optional[str]:
        with conn.cursor() as cur:
            cur.execute("SELECT request_id FROM public.geo_observation_insight_jobs WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return row.get("request_id") if isinstance(row, dict) else row[0]

    def _set_state(
        self, conn, job_id: str, lease_token: str,
        expected_state: str, state: str,
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.geo_observation_insight_jobs SET state=%s, updated_at=%s "
                "WHERE job_id=%s AND lease_token=%s AND state=%s",
                (state, self._now(), job_id, lease_token, expected_state),
            )
            n = cur.rowcount
        conn.commit()
        return n

    def _complete(
        self, conn, job_id: str, lease_token: str,
        paid_call_started_at: datetime, result: InsightResult, usage: dict,
    ) -> int:
        """Persist provider output only while its exact published snapshot lives.

        The provider may have been paid while a withdrawal was committed.  A
        fresh READ COMMITTED validation locks the target bucket revision and
        either publishes the result before the withdrawal, or discards the
        summary into a durable non-retryable unavailable state after it.
        """
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT owner_user_id,brand_id,snapshot_id,policy_basis_hash,
                          scope_type,bucket_granularity
                     FROM public.geo_observation_insight_jobs
                    WHERE job_id=%s AND lease_token=%s AND state='paid_call_started'
                      AND paid_call_started_at=%s""",
                (job_id, lease_token, paid_call_started_at),
            )
            identity = cur.fetchone()
        if identity is None:
            conn.rollback()
            return 0

        from . import repository as observation_repo
        try:
            receipt, problems = observation_repo.published_scope_snapshot(
                conn,
                policy_basis_hash=identity["policy_basis_hash"],
                scope_type=identity["scope_type"],
                granularity=identity["bucket_granularity"],
                owner_user_id=int(identity["owner_user_id"]),
                brand_id=int(identity["brand_id"]),
            )
            receipt_snapshot = None if receipt is None else InsightSnapshot(
                policy_basis_hash=str(receipt["policy_basis_hash"]),
                scope_type=str(receipt["scope_type"]),
                bucket_granularity=str(receipt["bucket_granularity"]),
                bucket_start=receipt["bucket_start"],
                bucket_epoch=int(receipt["eligibility_epoch"]),
                promotion_sequence_watermark=int(receipt["promotion_sequence_watermark"]),
                aggregate_input_watermark=receipt["input_watermark"],
                aggregate_contract_version=str(receipt["contract_version"]),
                aggregate_aggregation_version=str(receipt["aggregation_version"]),
                aggregate_metric_version=str(receipt["metric_version"]),
            )
            snapshot_valid = (
                not problems
                and receipt_snapshot is not None
                and receipt_snapshot.snapshot_id() == str(identity["snapshot_id"])
            )
        except Exception:
            # A failed catalog/receipt query leaves psycopg in aborted state.
            # Roll back before persisting the non-retryable paid-result discard.
            conn.rollback()
            snapshot_valid = False

        if not snapshot_valid:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE public.geo_observation_insight_jobs SET
                           state='failed',summary=NULL,evidence_refs='[]'::jsonb,
                           allowed_actions='[]'::jsonb,result_state=NULL,
                           paid_call_unknown_at=NULL,error_code=%s,
                           error_detail='paid result discarded: aggregate snapshot revoked',
                           updated_at=%s,lease_token=NULL,lease_until=NULL
                       WHERE job_id=%s AND lease_token=%s AND state='paid_call_started'
                         AND paid_call_started_at=%s""",
                    (
                        "SNAPSHOT_REVOKED_RESULT_DISCARDED", self._now(),
                        job_id, lease_token, paid_call_started_at,
                    ),
                )
                n = cur.rowcount
            conn.commit()
            return n

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.geo_observation_insight_jobs SET
                       state='completed', summary=%s, evidence_refs=%s::jsonb,
                       allowed_actions=%s::jsonb, input_token=%s, output_token=%s,
                       result_state=%s, updated_at=%s, completed_at=%s,
                       lease_token=NULL,lease_until=NULL
                   WHERE job_id=%s AND lease_token=%s AND state='paid_call_started'
                     AND paid_call_started_at=%s""",
                (
                    result.summary, json.dumps(result.evidence_refs),
                    json.dumps(result.allowed_actions),
                    usage.get("prompt_tokens"), usage.get("completion_tokens"),
                    result.state, self._now(), self._now(), job_id, lease_token,
                    paid_call_started_at,
                ),
            )
            n = cur.rowcount
        conn.commit()
        return n
    def _fail(
        self, conn, job_id: str, lease_token: str, expected_state: str,
        paid_call_started_at: Optional[datetime], error: str,
    ) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE public.geo_observation_insight_jobs SET
                       state='failed', error_code=%s, error_detail=%s, updated_at=%s,
                       lease_token=NULL,lease_until=NULL
                   WHERE job_id=%s AND lease_token=%s AND state=%s
                     AND paid_call_started_at IS NOT DISTINCT FROM %s""",
                (_INSIGHT_UNAVAILABLE_CODE, error[:500], self._now(), job_id,
                 lease_token, expected_state, paid_call_started_at),
            )
            n = cur.rowcount
        conn.commit()
        return n

    def _get_by_id(self, conn, job_id: str) -> Optional[dict]:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM public.geo_observation_insight_jobs WHERE job_id=%s", (job_id,))
            return cur.fetchone()

    def _get_by_hash(
        self, conn, owner_user_id: int, brand_id: int, input_hash: str, snapshot_id: str,
    ) -> Optional[dict]:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM public.geo_observation_insight_jobs "
                "WHERE owner_user_id=%s AND brand_id=%s AND input_hash=%s AND snapshot_id=%s",
                (owner_user_id, brand_id, input_hash, snapshot_id),
            )
            return cur.fetchone()

    def get_job(self, conn, job_id: str, owner_user_id: int, brand_id: int) -> Optional[dict]:
        """Object-level authorization: only returns the job when it belongs to
        the given owner+brand, so job_id alone can never surface another tenant's
        insight (defends against IDOR on the /brands/{id}/insights/{job_id} path)."""
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            self._recover_expired_jobs(
                cur, now=self._now(), job_id=job_id,
                owner_user_id=owner_user_id, brand_id=brand_id,
            )
            cur.execute(
                "SELECT * FROM public.geo_observation_insight_jobs "
                "WHERE job_id=%s AND owner_user_id=%s AND brand_id=%s",
                (job_id, owner_user_id, brand_id),
            )
            row = cur.fetchone()
        conn.commit()
        return row
