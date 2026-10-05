"""Replayable aggregate repository + refresh job for GEO observation analytics.

Reads ONLY ``processing_state='promoted'`` events joined to anonymized signals
(withdrawn/private_only/rejected/error never aggregate; signals are immutable —
a withdrawal flips the event and the next recompute drops it). Writes only the
AI-3-owned ``geo_observation_aggregates`` table, keyed idempotently by
``aggregate_key`` + (contract/aggregation/metric/policy) versions so the same
input watermark reruns to a byte-identical row.

All numbers here are computed by :mod:`.metrics`; no LLM ever computes a metric.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import json
from typing import Iterable, Optional, Sequence

from psycopg2.extras import Json, RealDictCursor

from . import metrics as M
from .contract import (
    AGGREGATION_VERSION,
    CONTRACT_VERSION,
    DEFAULT_ANOMALY_CONFIRMATION_DENOMINATOR,
    DEFAULT_ANOMALY_CONFIRMATION_NUMERATOR,
    DEFAULT_MAX_SINGLE_BRAND_SHARE_BPS,
    ELIGIBLE_PROCESSING_STATE,
    METRIC_VERSION,
    PRESENCE_OUTCOMES,
    PUBLIC_MIN_VALID_OBSERVATIONS,
    REFUSAL_OUTCOMES,
)
from db.xact_lock_guard import require_xact_scope

_WEIGHT_MICROS_PER_BPS = 100  # 1.0 unit weight == 10000 bps == 1_000_000 micros
# evidence-coverage derivation threshold (part of METRIC_VERSION): a valid
# observation counts as "verifiable" when its quality score is at least this, or
# it carries an explicit citation.
EVIDENCE_QUALITY_THRESHOLD_BPS = 5000
# uniform weight (micros) for private/admin scope so weighted rates == count rates
_UNIFORM_WEIGHT_MICROS = 1_000_000
DEFAULT_POLICY_BASIS_HASH = hashlib.sha256(b"policy-default").hexdigest()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AggregationConfig:
    """Version + policy-derived knobs for one refresh run.

    ``policy_version`` MUST be the live ``geo_observation_policy`` version at
    runtime (AI-2 owns the policy store); it is threaded into the aggregate_key
    so a policy change produces a new, separately-addressable aggregate version.
    """

    policy_version: str = "policy-default"
    policy_basis_hash: str = DEFAULT_POLICY_BASIS_HASH
    eligibility_epoch: int = 0
    promotion_sequence_watermark: int = 0
    contract_version: str = CONTRACT_VERSION
    aggregation_version: str = AGGREGATION_VERSION
    metric_version: str = METRIC_VERSION
    min_valid_observations_private: int = 1
    min_valid_observations_public: int = PUBLIC_MIN_VALID_OBSERVATIONS
    max_single_brand_share_bps: int = DEFAULT_MAX_SINGLE_BRAND_SHARE_BPS
    anomaly_numerator: int = DEFAULT_ANOMALY_CONFIRMATION_NUMERATOR
    anomaly_denominator: int = DEFAULT_ANOMALY_CONFIRMATION_DENOMINATOR


# ---------------------------------------------------------------------------
# One promoted observation (already anonymized at the signal layer)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Observation:
    event_id: int
    owner_user_id: Optional[int]
    brand_id: Optional[int]
    source_type: str
    industry_key: str
    prompt_family_key: Optional[str]
    prompt_intent: Optional[str]
    is_branded_prompt: Optional[bool]
    platform_key: Optional[str]
    surface_key: Optional[str]
    model_revision: Optional[str]
    search_enabled: Optional[bool]
    outcome: str
    position: Optional[int]
    competitor_count: int
    citation_count: int
    source_count: int
    quality_score_bps: int
    effective_weight_bps: int
    observed_at: datetime
    run_index: int
    user_bucket: Optional[str]
    brand_bucket: Optional[str]

    @property
    def is_valid(self) -> bool:
        return self.outcome not in ("entity_ambiguous", "engine_error")

    @property
    def is_present(self) -> bool:
        return self.outcome in PRESENCE_OUTCOMES

    # derived per-observation evidence flags (from REAL signal columns) --------
    @property
    def has_citation(self) -> bool:
        return self.citation_count > 0

    @property
    def source_visible(self) -> bool:
        # an accessed source exists (distinct from an explicit citation)
        return self.source_count > 0

    @property
    def evidence_verifiable(self) -> bool:
        # sufficient verifiable evidence: strong quality score OR an explicit citation
        return self.quality_score_bps >= EVIDENCE_QUALITY_THRESHOLD_BPS or self.citation_count > 0


# ---------------------------------------------------------------------------
# Bucket bounds
# ---------------------------------------------------------------------------
def bucket_bounds(granularity: str, day: date) -> tuple[date, date]:
    if granularity == "day":
        return day, day
    if granularity == "week":  # ISO week, Monday-start
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    if granularity == "month":
        start = day.replace(day=1)
        last = calendar.monthrange(day.year, day.month)[1]
        return start, day.replace(day=last)
    raise ValueError(f"unknown granularity: {granularity!r}")


def _window_utc(bucket_start: date, bucket_end: date) -> tuple[datetime, datetime]:
    start = datetime.combine(bucket_start, time.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(
        bucket_end + timedelta(days=1), time.min, tzinfo=timezone.utc
    )
    return start, end_exclusive


# ---------------------------------------------------------------------------
# Fetch promoted observations for a window
# ---------------------------------------------------------------------------
_FETCH_SQL = """
SELECT
    e.id                     AS event_id,
    e.owner_user_id          AS owner_user_id,
    e.brand_id               AS brand_id,
    e.source_type            AS source_type,
    COALESCE(s.industry_key, e.industry_key) AS industry_key,
    s.prompt_family_key      AS prompt_family_key,
    s.prompt_intent          AS prompt_intent,
    s.is_branded_prompt      AS is_branded_prompt,
    COALESCE(s.platform_key, e.platform_key)   AS platform_key,
    COALESCE(s.surface_key, e.surface_key)     AS surface_key,
    COALESCE(s.model_revision, e.model_revision) AS model_revision,
    e.search_enabled         AS search_enabled,
    s.target_outcome         AS outcome,
    s.target_position        AS position,
    s.competitor_count       AS competitor_count,
    s.citation_count         AS citation_count,
    s.source_count           AS source_count,
    s.quality_score_bps      AS quality_score_bps,
    s.effective_weight_bps   AS effective_weight_bps,
    e.observed_at            AS observed_at,
    e.run_index              AS run_index,
    b.contributor_user_bucket  AS user_bucket,
    b.contributor_brand_bucket AS brand_bucket
FROM public.geo_observation_signals s
JOIN public.geo_observation_events e ON e.id = s.event_id
LEFT JOIN public.geo_observation_contributor_buckets b ON b.event_id = e.id
WHERE e.processing_state = %s
  AND (e.retention_until IS NULL OR e.retention_until > NOW())
  AND e.observed_at >= %s
  AND e.observed_at <  %s
  AND (%s::bigint IS NULL OR e.promotion_seq <= %s)
  AND (%s::text IS NULL OR COALESCE(s.industry_key, e.industry_key) = %s)
  AND (
        -- public_industry aggregates ALL promoted sources (research + paid
        -- diagnosis + monitoring). Client events KEEP owner/brand privately;
        -- the public aggregate row/DTO hides them, the contributor buckets
        -- provide anti-abuse independence. It is a correctness bug to exclude
        -- owner/brand-bearing events from the public flywheel (I8 three sources).
        (%s = 'public_industry')
     OR (%s = 'private_brand'   AND e.owner_user_id IS NOT NULL AND e.brand_id IS NOT NULL)
     OR (%s = 'admin_shadow')
      )
  -- FAIL-CLOSED anti-abuse for customer sources: paid diagnosis/monitoring must
  -- carry the one-vote contributor bucket. Public research intentionally has no
  -- customer attribution bucket and contributes at its source weight; extra
  -- same-key customer stability samples remain excluded.
  AND (
        %s <> 'public_industry'
     OR e.source_type = 'research_round'
     OR b.event_id IS NOT NULL
      )
"""


def fetch_observations(
    conn,
    scope_type: str,
    bucket_start: date,
    bucket_end: date,
    industry_key: Optional[str] = None,
    promotion_sequence_watermark: Optional[int] = None,
) -> list[Observation]:
    start, end = _window_utc(bucket_start, bucket_end)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            _FETCH_SQL,
            (
                ELIGIBLE_PROCESSING_STATE,
                start,
                end,
                promotion_sequence_watermark,
                promotion_sequence_watermark,
                industry_key,
                industry_key,
                scope_type,
                scope_type,
                scope_type,
                scope_type,
            ),
        )
        rows = cur.fetchall()
    out: list[Observation] = []
    for d in rows:
        out.append(
            Observation(
                event_id=d["event_id"],
                owner_user_id=d["owner_user_id"],
                brand_id=d["brand_id"],
                source_type=d["source_type"],
                industry_key=d["industry_key"],
                prompt_family_key=d["prompt_family_key"],
                prompt_intent=d["prompt_intent"],
                is_branded_prompt=d["is_branded_prompt"],
                platform_key=d["platform_key"],
                surface_key=d["surface_key"],
                model_revision=d["model_revision"],
                search_enabled=d["search_enabled"],
                outcome=d["outcome"],
                position=d["position"],
                competitor_count=int(d["competitor_count"] or 0),
                citation_count=int(d["citation_count"] or 0),
                source_count=int(d["source_count"] or 0),
                quality_score_bps=int(d["quality_score_bps"] or 0),
                effective_weight_bps=int(d["effective_weight_bps"] or 0),
                observed_at=d["observed_at"],
                run_index=int(d["run_index"] or 1),
                user_bucket=d["user_bucket"],
                brand_bucket=d["brand_bucket"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Grouping-set dimension assignments (v1: overall + single-dimension breakdowns)
# ---------------------------------------------------------------------------
# Each grouping set names which dimensions are "set" (others are NULL in the row).
_GROUPING_SETS: tuple[tuple[str, ...], ...] = (
    (),                       # overall
    ("platform_key",),
    ("surface_key",),
    ("source_type",),
    ("is_branded_prompt",),
    ("prompt_intent",),
    ("search_enabled",),
)

_DIMENSION_ATTRS = (
    "prompt_family_key",
    "prompt_intent",
    "is_branded_prompt",
    "platform_key",
    "surface_key",
    "model_revision",
    "search_enabled",
    "source_type",
    "search_query_theme",
)


def _dim_value(obs: Observation, dim: str):
    if dim == "search_query_theme":
        return None  # not a v1 breakdown dimension
    return getattr(obs, dim)


# ---------------------------------------------------------------------------
# Per-cell computation
# ---------------------------------------------------------------------------
def _compute_cell(
    observations: Sequence[Observation],
    *,
    scope_type: str,
    owner_user_id: Optional[int],
    brand_id: Optional[int],
    industry_key: str,
    granularity: str,
    bucket_start: date,
    bucket_end: date,
    dims: dict[str, object],
    config: AggregationConfig,
    computed_at: datetime,
) -> Optional[dict]:
    """Compute one aggregate row from the observations already filtered to this
    exact (scope-cell, grouping-set) combination. Returns ``None`` when there is
    no observation at all (no empty rows written)."""
    if not observations:
        return None

    valid = [o for o in observations if o.is_valid]
    # raw outcome counts (count columns + valid_observations denominator)
    outcome_counts = M.OutcomeCounts.from_mapping(_count_outcomes(observations))
    valid_n = outcome_counts.valid_observations
    # A cell with ZERO valid observations (all entity_ambiguous/engine_error) has
    # no computable rate — do NOT emit a fabricated 0% row. No data => no row =>
    # the product API returns an honest insufficient/no-data state.
    if valid_n == 0:
        return None

    # per-observation weights (uniform for private => count rates; capped
    # source-weighted for public => cap + weights actually move the metrics).
    weights = _weight_map(valid, scope_type, config.max_single_brand_share_bps)
    total_w = sum(weights.values())

    def wrate(pred) -> int:
        return M.rate_bps(sum(weights[o.event_id] for o in valid if pred(o)), total_w)

    # ranks / avg position over valid observations (raw counts)
    positions = [o.position if o.is_present else None for o in valid]
    ranks = M.compute_rank_distribution(positions, valid_n)

    # weighted share of voice (target vs all-brand weighted appearances)
    presence_w = sum(weights[o.event_id] for o in valid if o.is_present)
    competitor_w = sum(weights[o.event_id] * o.competitor_count for o in valid)
    sov = M.rate_bps_or_none(presence_w, presence_w + competitor_w)

    # volatility: presence sequence ordered deterministically
    ordered = sorted(valid, key=lambda o: (o.observed_at, o.run_index, o.event_id))
    volatility = M.volatility_bps([o.is_present for o in ordered])

    # model shift: split valid by newest model_revision vs the rest
    model_shift = _model_shift(valid)

    # independence (k-anon inputs) from distinct HMAC buckets + source types
    independent_users = len({o.user_bucket for o in valid if o.user_bucket is not None})
    independent_brands = len({o.brand_bucket for o in valid if o.brand_bucket is not None})
    independent_sources = len({o.source_type for o in valid})

    # stored transparency column (effective-weight mass, capped for public)
    weighted_micros = _weighted_denominator_micros(
        valid, scope_type, config.max_single_brand_share_bps
    )

    # intrinsic vs retrieval split (weighted, same discipline as the rates)
    nosearch = [o for o in valid if o.search_enabled is False]
    search_on = [o for o in valid if o.search_enabled is True]
    intrinsic = M.rate_bps_or_none(
        sum(weights[o.event_id] for o in nosearch if o.is_present),
        sum(weights[o.event_id] for o in nosearch),
    )
    retrieval = M.rate_bps_or_none(
        sum(weights[o.event_id] for o in search_on if o.is_present),
        sum(weights[o.event_id] for o in search_on),
    )

    # trend confidence + stability
    span_days = (bucket_end - bucket_start).days + 1
    if scope_type == "public_industry":
        min_valid = config.min_valid_observations_public
        trend_conf = M.trend_confidence_bps(
            valid_n, independent_sources, span_days, volatility,
            independent_brand_buckets=independent_brands,
        )
    else:
        min_valid = config.min_valid_observations_private
        trend_conf = M.trend_confidence_bps(
            valid_n, independent_sources, span_days, volatility
        )
    stability = M.stability_status(valid_n, model_shift, volatility, min_valid)

    row = {
        "contract_version": config.contract_version,
        "aggregation_version": config.aggregation_version,
        "metric_version": config.metric_version,
        "policy_version": config.policy_version,
        "policy_basis_hash": config.policy_basis_hash,
        "eligibility_epoch": config.eligibility_epoch,
        "promotion_sequence_watermark": config.promotion_sequence_watermark,
        "scope_type": scope_type,
        "owner_user_id": owner_user_id,
        "brand_id": brand_id,
        "industry_key": industry_key,
        "bucket_granularity": granularity,
        "bucket_start": bucket_start,
        "bucket_end": bucket_end,
        "prompt_family_key": dims.get("prompt_family_key"),
        "prompt_intent": dims.get("prompt_intent"),
        "is_branded_prompt": dims.get("is_branded_prompt"),
        "platform_key": dims.get("platform_key"),
        "surface_key": dims.get("surface_key"),
        "model_revision": dims.get("model_revision"),
        "search_enabled": dims.get("search_enabled"),
        "source_type": dims.get("source_type"),
        "search_query_theme": dims.get("search_query_theme"),
        "valid_observations": valid_n,
        "independent_user_buckets": independent_users,
        "independent_brand_buckets": independent_brands,
        "independent_source_types": independent_sources,
        "weighted_denominator_micros": weighted_micros,
        "recommended_count": outcome_counts.recommended,
        "conditionally_recommended_count": outcome_counts.conditionally_recommended,
        "candidate_only_count": outcome_counts.candidate_only,
        "mentioned_only_count": outcome_counts.mentioned_only,
        "criteria_only_count": outcome_counts.criteria_only,
        "refused_no_evidence_count": outcome_counts.refused_no_evidence,
        "refused_risk_count": outcome_counts.refused_risk,
        "not_mentioned_count": outcome_counts.not_mentioned,
        "presence_rate_bps": wrate(lambda o: o.is_present),
        "explicit_recommendation_rate_bps": wrate(lambda o: o.outcome == "recommended"),
        "conditional_recommendation_rate_bps": wrate(lambda o: o.outcome == "conditionally_recommended"),
        "candidate_rate_bps": wrate(lambda o: o.outcome == "candidate_only"),
        "mentioned_only_rate_bps": wrate(lambda o: o.outcome == "mentioned_only"),
        "criteria_only_rate_bps": wrate(lambda o: o.outcome == "criteria_only"),
        "refusal_no_evidence_rate_bps": wrate(lambda o: o.outcome == "refused_no_evidence"),
        "refusal_risk_rate_bps": wrate(lambda o: o.outcome == "refused_risk"),
        "not_mentioned_rate_bps": wrate(lambda o: o.outcome == "not_mentioned"),
        "refusal_rate_bps": wrate(lambda o: o.outcome in REFUSAL_OUTCOMES),
        "citation_rate_bps": wrate(lambda o: o.has_citation),
        "source_visibility_rate_bps": wrate(lambda o: o.source_visible),
        "evidence_coverage_rate_bps": wrate(lambda o: o.evidence_verifiable),
        "rank_top1_count": ranks.rank_top1_count,
        "rank_top3_count": ranks.rank_top3_count,
        "rank_top5_count": ranks.rank_top5_count,
        "rank_not_listed_count": ranks.rank_not_listed_count,
        "avg_position_milli": ranks.avg_position_milli,
        "share_of_voice_bps": sov,
        "volatility_bps": volatility,
        # single-bucket aggregates never assert a confirmed direction (I "单日
        # 变化只能显示波动中"); trends.py computes cross-bucket confirmation.
        "confirmed_change": False,
        "trend_confidence_bps": trend_conf,
        "model_shift_index_bps": model_shift,
        "stability_status": stability,
        "intrinsic_memory_rate_bps": intrinsic,
        "retrieval_selection_rate_bps": retrieval,
        "input_watermark": _watermark(observations),
        "computed_at": computed_at,
    }
    row["aggregate_key"] = compute_aggregate_key(row)
    return row


def _count_outcomes(observations: Iterable[Observation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in observations:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    return counts


def _model_shift(valid: Sequence[Observation]) -> int:
    revisions = [o.model_revision for o in valid if o.model_revision is not None]
    # total ordering: earliest-seen first, ties broken by the revision string so
    # the "latest" revision is deterministic across processes (idempotency).
    distinct = sorted(
        set(revisions),
        key=lambda rev: (
            min(o.observed_at for o in valid if o.model_revision == rev),
            rev,
        ),
    )
    if len(distinct) < 2:
        return 0
    latest = distinct[-1]
    before = M.OutcomeCounts.from_mapping(
        _count_outcomes([o for o in valid if o.model_revision != latest])
    )
    after = M.OutcomeCounts.from_mapping(
        _count_outcomes([o for o in valid if o.model_revision == latest])
    )
    return M.model_shift_index_bps(before, after)


def _weight_map(
    valid: Sequence[Observation], scope_type: str, cap_bps: int
) -> dict[int, int]:
    """Per-observation weight (micros) used for RATE numerators/denominators.

    * private/admin scope -> uniform weight, so weighted rates == raw count
      rates (the owner sees their own true distribution).
    * public_industry scope -> source ``effective_weight_bps`` with a per-brand
      cap: each brand bucket's total contribution is capped at
      ``max_single_brand_share_bps`` of the raw total and re-distributed
      proportionally to that brand's observations. This is what makes the
      per-brand cap and source weights actually move the public metrics, so a
      high-volume / rich client cannot skew the public flywheel (I8).
    """
    if scope_type != "public_industry":
        return {o.event_id: _UNIFORM_WEIGHT_MICROS for o in valid}
    raw = {o.event_id: o.effective_weight_bps * _WEIGHT_MICROS_PER_BPS for o in valid}
    if sum(raw.values()) <= 0:
        return {o.event_id: 0 for o in valid}
    # Customer events carry a real brand bucket. Public research has no customer
    # attribution by design, so each research event remains outside customer
    # per-brand capping instead of inventing a shared pseudo-brand.
    brand_of = {o.event_id: (o.brand_bucket if o.brand_bucket is not None else f"__evt_{o.event_id}")
                for o in valid}
    brand_raw: dict[str, int] = {}
    for eid, w in raw.items():
        brand_raw[brand_of[eid]] = brand_raw.get(brand_of[eid], 0) + w
    capped_brand = _cap_brand_weights(brand_raw, cap_bps)
    # distribute each brand's capped total back to its events proportionally
    out: dict[int, int] = {}
    for o in valid:
        b = brand_of[o.event_id]
        bt = brand_raw[b]
        out[o.event_id] = (raw[o.event_id] * capped_brand[b]) // bt if bt > 0 else 0
    return out


def _cap_brand_weights(brand_raw: dict[str, int], cap_bps: int) -> dict[str, int]:
    """Water-filling per-brand cap: no single brand may exceed ``cap_bps`` of the
    FINAL (capped) denominator. Over-cap brands are set to ``cap*final_total`` and
    the total is recomputed until convergence; when the cap is infeasible (fewer
    than 1/cap brands, e.g. <10 brands for a 10% cap) it degrades to equal share
    (the tightest achievable). Deterministic integer arithmetic. The k-anonymity
    gate raises the minimum brand count so SHOWN public cells are always
    cap-feasible."""
    c = cap_bps
    n = len(brand_raw)
    if c <= 0 or c >= 10000 or n == 0:
        return dict(brand_raw)
    w = dict(brand_raw)
    # Iterate to CONVERGENCE. Each pass caps every over-ceiling brand to the
    # current ceiling; the total strictly decreases while any brand is over, so
    # this terminates (the safety bound only guards integer pathologies).
    # Convergence is geometric (contraction ~ |over|*c), not bounded by #brands,
    # so a fixed small iteration budget is NOT enough.
    for _ in range(10000):
        total = sum(w.values())
        if total <= 0:
            break
        ceil_w = (total * c) // 10000
        over = [b for b in w if w[b] > ceil_w]
        if not over:
            break  # converged: no brand exceeds cap of the FINAL denominator
        if len(over) == n:  # cap infeasible for this brand count -> equal share
            eq = total // n
            w = {b: eq for b in w}
            break
        for b in over:
            w[b] = ceil_w
    return w


def _weighted_denominator_micros(
    valid: Sequence[Observation], scope_type: str, cap_bps: int
) -> int:
    """Stored transparency column: the effective-weight mass, per-brand-capped
    for public scope; raw effective-weight sum for private/admin."""
    if not valid:
        return 0
    if scope_type != "public_industry":
        return sum(o.effective_weight_bps * _WEIGHT_MICROS_PER_BPS for o in valid)
    return sum(_weight_map(valid, scope_type, cap_bps).values())


def _watermark(observations: Iterable[Observation]) -> datetime:
    return max(o.observed_at for o in observations)


def _canonical_fingerprint(items: Sequence[object]) -> str:
    payload = json.dumps(
        list(items), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scope_cell_identity(
    observations: Sequence[Observation], scope_type: str
) -> tuple[int, str]:
    cells: set[tuple] = set()
    for observation in observations:
        if not observation.is_valid:
            continue
        if scope_type == "public_industry":
            cells.add((observation.industry_key,))
        else:
            cells.add(
                (
                    observation.owner_user_id,
                    observation.brand_id,
                    observation.industry_key,
                )
            )
    canonical = sorted(cells, key=lambda item: json.dumps(item, ensure_ascii=False))
    return len(canonical), _canonical_fingerprint(canonical)


def _aggregate_key_identity(rows: Sequence[dict]) -> tuple[int, str]:
    keys = sorted(str(row["aggregate_key"]) for row in rows)
    return len(keys), _canonical_fingerprint(keys)


# ---------------------------------------------------------------------------
# Aggregate key
# ---------------------------------------------------------------------------
_KEY_IDENTITY_FIELDS = (
    "contract_version",
    "aggregation_version",
    "metric_version",
    "policy_version",
    "policy_basis_hash",
    "eligibility_epoch",
    "promotion_sequence_watermark",
    "scope_type",
    "owner_user_id",
    "brand_id",
    "industry_key",
    "bucket_granularity",
    "bucket_start",
    "bucket_end",
    "prompt_family_key",
    "prompt_intent",
    "is_branded_prompt",
    "platform_key",
    "surface_key",
    "model_revision",
    "search_enabled",
    "source_type",
    "search_query_theme",
)


def compute_aggregate_key(row: dict) -> str:
    parts = []
    for f in _KEY_IDENTITY_FIELDS:
        v = row.get(f)
        parts.append("\x1e" if v is None else f"{f}={v!r}")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Build + upsert
# ---------------------------------------------------------------------------
def build_aggregate_rows(
    observations: Sequence[Observation],
    *,
    scope_type: str,
    granularity: str,
    bucket_start: date,
    bucket_end: date,
    config: AggregationConfig,
    computed_at: datetime,
) -> list[dict]:
    """Group the window's observations into scope-cells x grouping-sets and
    compute one aggregate row each. Pure (no DB)."""
    rows: list[dict] = []
    # partition by scope-cell first
    cells: dict[tuple, list[Observation]] = {}
    for o in observations:
        if scope_type == "public_industry":
            cell_key = (None, None, o.industry_key)
        elif scope_type == "private_brand":
            cell_key = (o.owner_user_id, o.brand_id, o.industry_key)
        else:  # admin_shadow
            cell_key = (o.owner_user_id, o.brand_id, o.industry_key)
        cells.setdefault(cell_key, []).append(o)

    for (owner, brand, industry), cell_obs in cells.items():
        for gset in _GROUPING_SETS:
            # bucket the cell's observations by the grouping-set dimension values
            groups: dict[tuple, list[Observation]] = {}
            for o in cell_obs:
                dim_key = tuple(_dim_value(o, d) for d in gset)
                groups.setdefault(dim_key, []).append(o)
            for dim_key, group_obs in groups.items():
                # A breakdown cohort with a NULL value for its grouping dimension
                # is indistinguishable (by columns AND by aggregate_key) from the
                # 'overall' all-dims-NULL row, so it would collide with and
                # corrupt the primary aggregate. Such "unknown-<dim>" observations
                # still count in the overall row; we simply do not emit a separate
                # breakdown row for them.
                if gset and any(v is None for v in dim_key):
                    continue
                dims = {d: None for d in _DIMENSION_ATTRS}
                for d, v in zip(gset, dim_key):
                    dims[d] = v
                row = _compute_cell(
                    group_obs,
                    scope_type=scope_type,
                    owner_user_id=owner,
                    brand_id=brand,
                    industry_key=industry,
                    granularity=granularity,
                    bucket_start=bucket_start,
                    bucket_end=bucket_end,
                    dims=dims,
                    config=config,
                    computed_at=computed_at,
                )
                if row is not None:
                    rows.append(row)
    return rows


_UPSERT_COLUMNS = (
    "aggregate_key", "contract_version", "aggregation_version", "metric_version",
    "policy_version", "policy_basis_hash", "eligibility_epoch", "promotion_sequence_watermark",
    "scope_type", "owner_user_id", "brand_id", "industry_key",
    "bucket_granularity", "bucket_start", "bucket_end", "prompt_family_key",
    "prompt_intent", "is_branded_prompt", "platform_key", "surface_key",
    "model_revision", "search_enabled", "source_type", "search_query_theme",
    "valid_observations", "independent_user_buckets", "independent_brand_buckets",
    "independent_source_types", "weighted_denominator_micros", "recommended_count",
    "conditionally_recommended_count", "candidate_only_count", "mentioned_only_count",
    "criteria_only_count", "refused_no_evidence_count", "refused_risk_count",
    "not_mentioned_count", "presence_rate_bps", "explicit_recommendation_rate_bps",
    "conditional_recommendation_rate_bps", "candidate_rate_bps",
    "mentioned_only_rate_bps", "criteria_only_rate_bps",
    "refusal_no_evidence_rate_bps", "refusal_risk_rate_bps", "not_mentioned_rate_bps",
    "refusal_rate_bps", "citation_rate_bps", "source_visibility_rate_bps",
    "evidence_coverage_rate_bps", "rank_top1_count", "rank_top3_count",
    "rank_top5_count", "rank_not_listed_count", "avg_position_milli",
    "share_of_voice_bps", "volatility_bps", "confirmed_change",
    "trend_confidence_bps", "model_shift_index_bps", "stability_status",
    "intrinsic_memory_rate_bps", "retrieval_selection_rate_bps", "input_watermark",
    "computed_at",
)

# Columns updated on conflict (everything except the identity/key/created_at).
_UPDATE_COLUMNS = tuple(
    c for c in _UPSERT_COLUMNS
    if c not in _KEY_IDENTITY_FIELDS and c != "aggregate_key"
)


def upsert_aggregates(conn, rows: Sequence[dict], commit: bool = True) -> int:
    """Idempotent upsert keyed by aggregate_key.

    Rerunning over the same promoted data writes byte-identical metric values
    (all inputs are deterministic). Concurrency-safe: the unique aggregate_key
    index makes 20 concurrent writers converge to exactly one row per cell.

    The no-regress guard is on ``computed_at`` (recompute recency), NOT on the
    data watermark: a withdrawal/correction legitimately LOWERS the data
    watermark (the removed observation may have carried the bucket's max
    observed_at), and a watermark guard would wrongly reject that corrected
    recompute and retain withdrawn data. ``computed_at`` reflects when the
    promoted set was read, so "latest read wins" both keeps a fresher concurrent
    recompute and lets withdrawals take effect.
    """
    if not rows:
        return 0
    placeholders = "(" + ",".join(["%s"] * len(_UPSERT_COLUMNS)) + ")"
    set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in _UPDATE_COLUMNS)
    sql = (
        f"INSERT INTO public.geo_observation_aggregates AS aggregates ({', '.join(_UPSERT_COLUMNS)}) "
        f"VALUES {placeholders} "
        f"ON CONFLICT (aggregate_key) DO UPDATE SET {set_clause}, updated_at=NOW() "
        f"WHERE EXCLUDED.computed_at >= aggregates.computed_at"
    )
    n = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(sql, tuple(row[c] for c in _UPSERT_COLUMNS))
            n += 1
    if commit:
        conn.commit()
    return n


def prune_stale_cells(
    conn,
    *,
    scope_type: str,
    granularity: str,
    bucket_start: date,
    config: AggregationConfig,
    fresh_keys: Sequence[str],
    industry_key: Optional[str] = None,
    commit: bool = True,
) -> int:
    """Delete aggregates for this (scope, granularity, bucket, versions) whose
    cell no longer has promoted data (aggregate_key not in the freshly computed
    set). This is what makes a fully-withdrawn cell drop to zero rows — the
    withdrawn-contribution=0 hard gate. Scoped to the current version tuple so
    other metric_version segments are preserved. Concurrency-safe: every worker
    computes the same fresh_keys, so no in-set row is ever deleted."""
    where = [
        "scope_type=%s", "bucket_granularity=%s", "bucket_start=%s",
        "contract_version=%s", "aggregation_version=%s",
        "metric_version=%s", "policy_basis_hash=%s",
    ]
    params: list = [
        scope_type, granularity, bucket_start, config.contract_version,
        config.aggregation_version, config.metric_version, config.policy_basis_hash,
    ]
    if industry_key is not None:
        where.append("industry_key=%s")
        params.append(industry_key)
    if fresh_keys:
        where.append("aggregate_key <> ALL(%s)")
        params.append(list(fresh_keys))
    sql = "DELETE FROM public.geo_observation_aggregates WHERE " + " AND ".join(where)
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        deleted = cur.rowcount
    if commit:
        conn.commit()
    return deleted


def write_refresh_manifest(
    conn,
    *,
    scope_type: str,
    granularity: str,
    bucket_start: date,
    bucket_end: date,
    config: AggregationConfig,
    observations: Sequence[Observation],
    rows: Sequence[dict],
    eligible_observation_count: int,
    completed_at: datetime,
) -> dict:
    """Persist the global refresh terminal in the aggregate transaction."""
    identity = "\x1f".join((
        config.policy_basis_hash,
        config.contract_version,
        config.aggregation_version,
        config.metric_version,
        scope_type,
        granularity,
        bucket_start.isoformat(),
    ))
    manifest_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    overall_rows = [
        row for row in rows
        if all(row.get(dim) is None for dim in _DIMENSION_ATTRS)
    ]
    expected_scope_cell_count, expected_scope_cell_fingerprint = _scope_cell_identity(
        observations, scope_type
    )
    aggregate_key_count, aggregate_key_fingerprint = _aggregate_key_identity(rows)
    if len(overall_rows) != expected_scope_cell_count:
        raise RuntimeError(
            "aggregate overall cell set 与 eligible scope-cell set 不一致"
        )
    if aggregate_key_count != len(rows):
        raise RuntimeError("aggregate key 集存在重复")
    watermarks = [row.get("input_watermark") for row in rows if row.get("input_watermark")]
    input_watermark = max(watermarks) if watermarks else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.geo_observation_aggregate_refresh_manifest AS manifests(
                manifest_key,policy_basis_hash,policy_version,contract_version,
                aggregation_version,metric_version,scope_type,bucket_granularity,
                bucket_start,bucket_end,input_watermark,eligibility_epoch,
                promotion_sequence_watermark,expected_scope_cell_count,
                expected_scope_cell_fingerprint,aggregate_key_fingerprint,
                overall_cell_count,eligible_observation_count,aggregate_row_count,completed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (policy_basis_hash,contract_version,aggregation_version,
                         metric_version,scope_type,bucket_granularity,bucket_start)
            DO UPDATE SET manifest_key=EXCLUDED.manifest_key,
                          policy_version=EXCLUDED.policy_version,
                          bucket_end=EXCLUDED.bucket_end,
                          input_watermark=EXCLUDED.input_watermark,
                          eligibility_epoch=EXCLUDED.eligibility_epoch,
                          promotion_sequence_watermark=EXCLUDED.promotion_sequence_watermark,
                          expected_scope_cell_count=EXCLUDED.expected_scope_cell_count,
                          expected_scope_cell_fingerprint=EXCLUDED.expected_scope_cell_fingerprint,
                          aggregate_key_fingerprint=EXCLUDED.aggregate_key_fingerprint,
                          overall_cell_count=EXCLUDED.overall_cell_count,
                          eligible_observation_count=EXCLUDED.eligible_observation_count,
                          aggregate_row_count=EXCLUDED.aggregate_row_count,
                          completed_at=EXCLUDED.completed_at,
                          updated_at=NOW()
            WHERE EXCLUDED.completed_at >= manifests.completed_at
            """,
            (
                manifest_key, config.policy_basis_hash, config.policy_version,
                config.contract_version, config.aggregation_version, config.metric_version,
                scope_type, granularity, bucket_start, bucket_end, input_watermark,
                config.eligibility_epoch, config.promotion_sequence_watermark,
                expected_scope_cell_count, expected_scope_cell_fingerprint,
                aggregate_key_fingerprint, len(overall_rows), eligible_observation_count,
                len(rows), completed_at,
            ),
        )

    cells_by_identity: dict[str, dict] = {}
    for row in rows:
        identity = {
            "owner_user_id": row.get("owner_user_id"),
            "brand_id": row.get("brand_id"),
            "industry_key": row.get("industry_key"),
        }
        identity_key = json.dumps(identity, ensure_ascii=False, sort_keys=True)
        cell = cells_by_identity.setdefault(
            identity_key, {**identity, "aggregate_keys": []},
        )
        cell["aggregate_keys"].append(str(row["aggregate_key"]))
    cells = sorted(cells_by_identity.values(), key=lambda item: json.dumps(
        {k: item[k] for k in ("owner_user_id", "brand_id", "industry_key")},
        ensure_ascii=False, sort_keys=True,
    ))
    for cell in cells:
        cell["aggregate_keys"].sort()
    watermark_iso = None
    if input_watermark is not None:
        normalized = input_watermark
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        watermark_iso = normalized.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
    return {
        "manifest_key": manifest_key,
        "policy_basis_hash": config.policy_basis_hash,
        "contract_version": config.contract_version,
        "aggregation_version": config.aggregation_version,
        "metric_version": config.metric_version,
        "scope_type": scope_type,
        "bucket_granularity": granularity,
        "bucket_start": bucket_start.isoformat(),
        "bucket_end": bucket_end.isoformat(),
        "input_watermark": watermark_iso,
        "eligibility_epoch": int(config.eligibility_epoch),
        "promotion_sequence_watermark": int(config.promotion_sequence_watermark),
        "cells": cells,
        "completed_at": completed_at.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
    }


def refresh_scope(
    conn,
    *,
    scope_type: str,
    granularity: str,
    day: date,
    config: Optional[AggregationConfig] = None,
    industry_key: Optional[str] = None,
    computed_at: Optional[datetime] = None,
) -> dict:
    """Fetch promoted observations for the bucket, compute, upsert and prune.

    ``computed_at`` is injectable for deterministic tests; it does NOT enter the
    aggregate_key or any metric, so it never affects idempotency.

    Concurrency contract: this is idempotent under SAME-DATA concurrency (20
    concurrent refreshes converge to one version per cell). Two refreshes reading
    DIFFERENT in-flight data for the same bucket must be serialized by the
    ROLE=cron single-leader + ``sched_claim`` contract (04 §5.2) — that leader
    election is what prevents a late stale writer from re-inserting a cell that a
    fresher run pruned. The integrator MUST register this job under that contract
    (never under the root scheduler / never double-triggered).
    """
    config = config or AggregationConfig()
    if computed_at is None:
        computed_at = datetime.now(timezone.utc)
    bucket_start, bucket_end = bucket_bounds(granularity, day)

    # Product rows and their manifest form one published snapshot.  A partial
    # industry repair has no independent snapshot/version to publish into, so
    # even a brand-new UTC bucket could otherwise become the repository's
    # "latest" row while readiness still serves yesterday's complete manifest.
    # Keep the footgun closed until targeted repairs have a staging snapshot
    # and an atomic full-scope switch.
    if industry_key is not None:
        raise RuntimeError(
            "active aggregate basis 禁止定向 refresh；请执行完整 scope refresh"
        )

    # Serialize refreshes of the SAME (scope, granularity, bucket[, industry])
    # with a transaction-scoped advisory lock, then read + upsert + prune in ONE
    # transaction. This makes withdrawal correction atomic and prevents a late
    # stale writer from re-inserting a pruned cell WITHOUT relying on the cron
    # leader (privacy withdrawal must not depend on scheduler luck). The lock and
    # all writes are released/committed together.
    # Full refresh and targeted industry repair share one lock. A targeted
    # repair cannot race the full-scope prune/manifest transaction.
    lock_key = f"geo_obs_agg::{scope_type}::{granularity}::{bucket_start}"
    with conn.cursor() as cur:
        require_xact_scope(cur, where="aggregates.refresh_scope")  # §1 硬闸:autocommit 下取事务锁=没锁
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
        cur.execute(
            """INSERT INTO public.geo_observation_aggregate_bucket_revision(
                   scope_type,bucket_granularity,bucket_start,epoch,dirty
               ) VALUES (%s,%s,%s,0,FALSE)
               ON CONFLICT (scope_type,bucket_granularity,bucket_start) DO NOTHING""",
            (scope_type, granularity, bucket_start),
        )
        cur.execute(
            """SELECT epoch FROM public.geo_observation_aggregate_bucket_revision
                WHERE scope_type=%s AND bucket_granularity=%s AND bucket_start=%s
                FOR UPDATE""",
            (scope_type, granularity, bucket_start),
        )
        epoch_row = cur.fetchone()
        if epoch_row is None:
            raise RuntimeError(
                f"bucket eligibility revision 缺失: {scope_type}/{granularity}/{bucket_start}"
            )
        epoch_value = (
            epoch_row["epoch"] if isinstance(epoch_row, dict) else epoch_row[0]
        )
        cur.execute(
            "SELECT COALESCE(MAX(promotion_seq),0) FROM public.geo_observation_events "
            "WHERE processing_state='promoted'"
        )
        sequence_row = cur.fetchone()
        sequence_value = (
            sequence_row[0]
            if not isinstance(sequence_row, dict)
            else next(iter(sequence_row.values()))
        )
        config = replace(
            config,
            eligibility_epoch=int(epoch_value),
            promotion_sequence_watermark=int(sequence_value or 0),
        )

    observations = fetch_observations(
        conn, scope_type, bucket_start, bucket_end, industry_key=industry_key,
        promotion_sequence_watermark=config.promotion_sequence_watermark,
    )
    rows = build_aggregate_rows(
        observations,
        scope_type=scope_type,
        granularity=granularity,
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        config=config,
        computed_at=computed_at,
    )
    written = upsert_aggregates(conn, rows, commit=False)
    pruned = prune_stale_cells(
        conn, scope_type=scope_type, granularity=granularity, bucket_start=bucket_start,
        config=config, fresh_keys=[r["aggregate_key"] for r in rows], industry_key=industry_key,
        commit=False,
    )
    receipt = write_refresh_manifest(
        conn,
        scope_type=scope_type,
        granularity=granularity,
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        config=config,
        rows=rows,
        observations=observations,
        eligible_observation_count=len(observations),
        completed_at=computed_at,
    )
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE public.geo_observation_aggregate_bucket_revision
                  SET dirty=FALSE,published_receipt=%s,updated_at=NOW()
                WHERE scope_type=%s AND bucket_granularity=%s AND bucket_start=%s
                  AND epoch=%s""",
            (Json(receipt), scope_type, granularity, bucket_start, config.eligibility_epoch),
        )
        if cur.rowcount != 1:
            raise RuntimeError("bucket revision 在 refresh 事务中发生漂移")
    conn.commit()  # atomic: upsert + prune + manifest + advisory lock release
    return {
        "scope_type": scope_type,
        "granularity": granularity,
        "bucket_start": bucket_start,
        "bucket_end": bucket_end,
        "observations": len(observations),
        "rows_written": written,
        "rows_pruned": pruned,
    }
