"""Deterministic metric formulas — the ONLY place GEO observation rates,
ranks, share-of-voice, volatility, trend confidence, model-shift and stability
are computed.

Invariants enforced here (00_MASTER_SPEC §8, 03 §3, 04 §7.5):
  * ``valid_observations`` excludes ``entity_ambiguous`` and ``engine_error``
    (UNKNOWN never enters a denominator).
  * ``criteria_only`` / ``refused_no_evidence`` / ``refused_risk`` are valid
    product behaviors, counted separately, never folded into ``not_mentioned``.
  * All ``*_bps`` are integers in ``[0, 10000]`` computed by round-half-up
    integer arithmetic (platform-independent, replayable, drift = 0).
  * ``None`` is never coerced to ``0``; a metric with no denominator returns
    ``None`` where the contract allows it, otherwise ``0`` only when the count
    itself is genuinely zero.

Nothing here touches the database, the network, or an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .contract import (
    OUTCOMES_EXCLUDED_FROM_VALID,
    PRESENCE_OUTCOMES,
    REFUSAL_OUTCOMES,
    TARGET_OUTCOMES,
)

# Tunable metric constants (part of METRIC_VERSION; changing any of these is a
# metric-version bump, never a silent edit).
WATCH_VOLATILITY_BPS = 3000          # volatility above this => "watch"
MODEL_SHIFT_BPS_THRESHOLD = 2000     # model_shift_index above this => "shifted"
TREND_TARGET_SAMPLE = 30             # samples for full sample-confidence
TREND_TARGET_SPAN_DAYS = 14          # span for full span-confidence


# ---------------------------------------------------------------------------
# Integer round-half-up rate in basis points, clamped to [0, 10000].
# ---------------------------------------------------------------------------
def rate_bps(numerator: int, denominator: int) -> int:
    """Return round-half-up ``numerator/denominator`` in basis points.

    Returns 0 when the denominator is 0 (an empty denominator is not a rate;
    callers that must distinguish "no data" from "0%" use the *_or_none form or
    check ``denominator`` themselves).
    """
    if denominator <= 0:
        return 0
    if numerator < 0:
        raise ValueError("rate numerator must be non-negative")
    value = (2 * numerator * 10000 + denominator) // (2 * denominator)
    if value < 0:
        return 0
    if value > 10000:
        return 10000
    return value


def rate_bps_or_none(numerator: int, denominator: int) -> Optional[int]:
    """Like :func:`rate_bps` but returns ``None`` when there is no denominator."""
    if denominator <= 0:
        return None
    return rate_bps(numerator, denominator)


# ---------------------------------------------------------------------------
# Outcome counts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OutcomeCounts:
    """Counts of the ten target outcomes for one aggregation cell."""

    recommended: int = 0
    conditionally_recommended: int = 0
    candidate_only: int = 0
    mentioned_only: int = 0
    criteria_only: int = 0
    refused_no_evidence: int = 0
    refused_risk: int = 0
    not_mentioned: int = 0
    entity_ambiguous: int = 0
    engine_error: int = 0

    @classmethod
    def from_mapping(cls, mapping: dict[str, int]) -> "OutcomeCounts":
        unknown = set(mapping) - set(TARGET_OUTCOMES)
        if unknown:
            raise ValueError(f"unknown outcome keys: {sorted(unknown)}")
        return cls(**{o: int(mapping.get(o, 0) or 0) for o in TARGET_OUTCOMES})

    def __post_init__(self) -> None:
        for name in TARGET_OUTCOMES:
            if getattr(self, name) < 0:
                raise ValueError(f"outcome count {name} must be non-negative")

    @property
    def valid_observations(self) -> int:
        """Sum of the 8 valid outcomes (excludes entity_ambiguous/engine_error)."""
        return sum(
            getattr(self, o)
            for o in TARGET_OUTCOMES
            if o not in OUTCOMES_EXCLUDED_FROM_VALID
        )

    @property
    def presence_count(self) -> int:
        return sum(getattr(self, o) for o in TARGET_OUTCOMES if o in PRESENCE_OUTCOMES)

    @property
    def refusal_count(self) -> int:
        return sum(getattr(self, o) for o in TARGET_OUTCOMES if o in REFUSAL_OUTCOMES)

    @property
    def excluded_count(self) -> int:
        return self.entity_ambiguous + self.engine_error

    def as_dict(self) -> dict[str, int]:
        return {o: getattr(self, o) for o in TARGET_OUTCOMES}


# ---------------------------------------------------------------------------
# Outcome-derived rate bundle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OutcomeRates:
    valid_observations: int
    presence_rate_bps: int
    explicit_recommendation_rate_bps: int
    conditional_recommendation_rate_bps: int
    candidate_rate_bps: int
    mentioned_only_rate_bps: int
    criteria_only_rate_bps: int
    refusal_no_evidence_rate_bps: int
    refusal_risk_rate_bps: int
    not_mentioned_rate_bps: int
    refusal_rate_bps: int


def compute_outcome_rates(counts: OutcomeCounts) -> OutcomeRates:
    """All outcome-distribution rates over the valid-observation denominator."""
    valid = counts.valid_observations
    return OutcomeRates(
        valid_observations=valid,
        presence_rate_bps=rate_bps(counts.presence_count, valid),
        explicit_recommendation_rate_bps=rate_bps(counts.recommended, valid),
        conditional_recommendation_rate_bps=rate_bps(
            counts.conditionally_recommended, valid
        ),
        candidate_rate_bps=rate_bps(counts.candidate_only, valid),
        mentioned_only_rate_bps=rate_bps(counts.mentioned_only, valid),
        criteria_only_rate_bps=rate_bps(counts.criteria_only, valid),
        refusal_no_evidence_rate_bps=rate_bps(counts.refused_no_evidence, valid),
        refusal_risk_rate_bps=rate_bps(counts.refused_risk, valid),
        not_mentioned_rate_bps=rate_bps(counts.not_mentioned, valid),
        refusal_rate_bps=rate_bps(counts.refusal_count, valid),
    )


# ---------------------------------------------------------------------------
# Rank distribution (cumulative Top1 ⊆ Top3 ⊆ Top5) + average position
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RankDistribution:
    rank_top1_count: int
    rank_top3_count: int
    rank_top5_count: int
    rank_not_listed_count: int
    avg_position_milli: Optional[int]


def compute_rank_distribution(
    positions: Sequence[Optional[int]], valid_observations: int
) -> RankDistribution:
    """Cumulative rank buckets over valid observations.

    ``positions`` is one entry per *valid* observation: a positive integer rank
    when the brand was present with a known position, else ``None`` (present
    without a ranked position, or not present). Top3 includes Top1; Top5
    includes Top3. ``rank_not_listed_count`` = valid observations without a
    known ranked position. ``avg_position_milli`` = mean position * 1000 over
    observations that DO have a position (``None`` if none).
    """
    if len(positions) > valid_observations:
        raise ValueError("positions cannot exceed valid_observations")
    top1 = top3 = top5 = 0
    ranked: list[int] = []
    for p in positions:
        if p is None:
            continue
        if p <= 0:
            raise ValueError("position must be a positive integer or None")
        ranked.append(p)
        if p <= 1:
            top1 += 1
        if p <= 3:
            top3 += 1
        if p <= 5:
            top5 += 1
    not_listed = valid_observations - len(ranked)
    avg_milli: Optional[int] = None
    if ranked:
        # round-half-up mean * 1000
        avg_milli = (2 * sum(ranked) * 1000 + len(ranked)) // (2 * len(ranked))
    return RankDistribution(
        rank_top1_count=top1,
        rank_top3_count=top3,
        rank_top5_count=top5,
        rank_not_listed_count=not_listed,
        avg_position_milli=avg_milli,
    )


# ---------------------------------------------------------------------------
# Share of voice
# ---------------------------------------------------------------------------
def share_of_voice_bps(presence_count: int, competitor_total: int) -> Optional[int]:
    """target appearances / all brand appearances. ``None`` if no appearances.

    ``competitor_total`` is the summed competitor mention COUNT across valid
    observations (never a private competitor name list).
    """
    denom = presence_count + competitor_total
    return rate_bps_or_none(presence_count, denom)


# ---------------------------------------------------------------------------
# Volatility — flip rate of the presence sequence within a cell
# ---------------------------------------------------------------------------
def volatility_bps(presence_sequence: Sequence[bool]) -> int:
    """Deterministic disagreement of a time/run-ordered presence sequence.

    volatility = adjacent flips / (n-1). n<2 => 0 (not measurable; stability
    reports 'insufficient' separately). The caller supplies presence booleans
    already ordered by (observed_at, run_index, id).
    """
    n = len(presence_sequence)
    if n < 2:
        return 0
    flips = sum(
        1 for a, b in zip(presence_sequence, presence_sequence[1:]) if a != b
    )
    return rate_bps(flips, n - 1)


# ---------------------------------------------------------------------------
# Model shift index — total-variation distance of outcome distributions
# ---------------------------------------------------------------------------
def model_shift_index_bps(
    before: OutcomeCounts, after: OutcomeCounts
) -> int:
    """Structural distance between two outcome distributions (TVD * 10000).

    Only valid outcomes are compared. If either side has zero valid
    observations, there is no comparable distribution => 0 (no shift asserted).
    Interpreted as a caliber break marker, NOT a customer gain/loss.
    """
    vb, va = before.valid_observations, after.valid_observations
    if vb <= 0 or va <= 0:
        return 0
    valid_outcomes = [o for o in TARGET_OUTCOMES if o not in OUTCOMES_EXCLUDED_FROM_VALID]
    # TVD = 0.5 * sum |p_i - q_i|, computed in integer bps to stay deterministic.
    total_abs_bps = 0
    for o in valid_outcomes:
        p = rate_bps(getattr(before, o), vb)  # bps of before
        q = rate_bps(getattr(after, o), va)   # bps of after
        total_abs_bps += abs(p - q)
    tvd_bps = total_abs_bps // 2
    return min(10000, max(0, tvd_bps))


# ---------------------------------------------------------------------------
# Trend confidence — combines sample, sources, span, stability (+ brand indep.)
# ---------------------------------------------------------------------------
def trend_confidence_bps(
    valid_observations: int,
    independent_source_types: int,
    span_days: int,
    volatility_value_bps: int,
    independent_brand_buckets: Optional[int] = None,
) -> int:
    """Product of bounded confidence factors, in basis points.

    ``independent_brand_buckets`` applies to public-scope aggregates (cross-
    customer independence); pass ``None`` for private-brand scope where the
    subject is the owner's own single brand.
    """
    if valid_observations <= 0:
        return 0
    sample_factor = min(valid_observations, TREND_TARGET_SAMPLE) / TREND_TARGET_SAMPLE
    source_factor = min(max(independent_source_types, 0), 2) / 2
    span_factor = min(max(span_days, 0), TREND_TARGET_SPAN_DAYS) / TREND_TARGET_SPAN_DAYS
    stability_factor = max(0.0, 1.0 - volatility_value_bps / 10000)
    confidence = sample_factor * source_factor * span_factor * stability_factor
    if independent_brand_buckets is not None:
        brand_factor = min(max(independent_brand_buckets, 0), 3) / 3
        confidence *= brand_factor
    bps = int(confidence * 10000 + 0.5)
    return min(10000, max(0, bps))


# ---------------------------------------------------------------------------
# Confirmed change (anomaly then >= num/den same-direction confirmation)
# ---------------------------------------------------------------------------
def is_confirmed_change(
    anomaly_detected: bool,
    same_direction_confirmations: int,
    total_confirmations: int,
    numerator: int,
    denominator: int,
) -> bool:
    """True only when an anomaly was detected AND the confirmation samples meet
    the ``numerator/denominator`` same-direction threshold (default 2/3)."""
    if not anomaly_detected:
        return False
    if total_confirmations <= 0 or denominator <= 0:
        return False
    # same/total >= numerator/denominator, integer-safe
    return same_direction_confirmations * denominator >= numerator * total_confirmations


# ---------------------------------------------------------------------------
# Stability status (priority: insufficient > shifted > watch > stable)
# ---------------------------------------------------------------------------
def stability_status(
    valid_observations: int,
    model_shift_value_bps: int,
    volatility_value_bps: int,
    min_valid_observations: int,
) -> str:
    if valid_observations < min_valid_observations:
        return "insufficient"
    if model_shift_value_bps >= MODEL_SHIFT_BPS_THRESHOLD:
        return "shifted"
    if volatility_value_bps > WATCH_VOLATILITY_BPS:
        return "watch"
    return "stable"


# ---------------------------------------------------------------------------
# Intrinsic-memory vs retrieval-selection split (no-search vs search samples)
# ---------------------------------------------------------------------------
def intrinsic_memory_rate_bps(
    presence_in_nosearch: int, valid_nosearch: int
) -> Optional[int]:
    """Presence among search-disabled valid observations ('model knows it')."""
    return rate_bps_or_none(presence_in_nosearch, valid_nosearch)


def retrieval_selection_rate_bps(
    presence_in_search: int, valid_search: int
) -> Optional[int]:
    """Presence among search-enabled valid observations ('retrieved and chose')."""
    return rate_bps_or_none(presence_in_search, valid_search)
