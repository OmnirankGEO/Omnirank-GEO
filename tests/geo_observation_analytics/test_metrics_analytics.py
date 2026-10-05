"""Boundary tests for the deterministic metric SSOT."""

from __future__ import annotations

import pytest

from services.geo_observation_analytics import metrics as M
from services.geo_observation_analytics.metrics import OutcomeCounts


# ---------------------------------------------------------------------------
# rate_bps
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "num,den,expected",
    [
        (0, 0, 0),      # empty denominator -> 0 (not a rate)
        (0, 5, 0),
        (1, 2, 5000),
        (1, 4, 2500),
        (3, 4, 7500),
        (1, 3, 3333),   # round-half-up 3333.33 -> 3333
        (2, 3, 6667),   # 6666.66 -> 6667
        (5, 6, 8333),   # 8333.33 -> 8333
        (1, 1, 10000),
        (7, 7, 10000),
    ],
)
def test_rate_bps(num, den, expected):
    assert M.rate_bps(num, den) == expected


def test_rate_bps_never_exceeds_range():
    assert 0 <= M.rate_bps(999, 1000) <= 10000
    assert M.rate_bps(1000, 1000) == 10000


def test_rate_bps_or_none_distinguishes_no_denominator():
    assert M.rate_bps_or_none(0, 0) is None
    assert M.rate_bps_or_none(0, 5) == 0
    assert M.rate_bps_or_none(1, 2) == 5000


def test_rate_bps_rejects_negative_numerator():
    with pytest.raises(ValueError):
        M.rate_bps(-1, 10)


# ---------------------------------------------------------------------------
# OutcomeCounts + denominator discipline
# ---------------------------------------------------------------------------
def test_valid_observations_excludes_ambiguous_and_engine_error():
    c = OutcomeCounts(
        recommended=10, not_mentioned=5, entity_ambiguous=7, engine_error=3
    )
    # valid = 10 + 5 (ambiguous/engine_error excluded)
    assert c.valid_observations == 15
    assert c.excluded_count == 10
    assert c.presence_count == 10
    assert c.refusal_count == 0


def test_unknown_never_enters_denominator():
    # 100 ambiguous + 100 engine_error, only 2 valid -> denominator is 2, not 202
    c = OutcomeCounts(recommended=1, not_mentioned=1, entity_ambiguous=100, engine_error=100)
    rates = M.compute_outcome_rates(c)
    assert rates.valid_observations == 2
    assert rates.presence_rate_bps == M.rate_bps(1, 2) == 5000


def test_outcome_rates_partition_sums_to_full_range():
    c = OutcomeCounts(
        recommended=40,
        conditionally_recommended=23,
        candidate_only=11,
        mentioned_only=14,
        criteria_only=10,
        refused_no_evidence=12,
        refused_risk=3,
        not_mentioned=15,
    )  # from fixture brand_summary outcomes, valid = 128
    r = M.compute_outcome_rates(c)
    assert r.valid_observations == 128
    # presence = 40+23+11+14 = 88 -> 6875 bps (matches fixture presence_rate_bps)
    assert r.presence_rate_bps == 6875
    assert r.explicit_recommendation_rate_bps == M.rate_bps(40, 128) == 3125
    assert r.conditional_recommendation_rate_bps == M.rate_bps(23, 128) == 1797
    assert r.refusal_rate_bps == M.rate_bps(15, 128)  # 12 + 3


def test_criteria_and_refusals_not_folded_into_not_mentioned():
    c = OutcomeCounts(criteria_only=5, refused_no_evidence=5, refused_risk=5, not_mentioned=5)
    r = M.compute_outcome_rates(c)
    assert r.valid_observations == 20
    assert r.criteria_only_rate_bps == 2500
    assert r.refusal_no_evidence_rate_bps == 2500
    assert r.refusal_risk_rate_bps == 2500
    assert r.not_mentioned_rate_bps == 2500  # only the true not_mentioned


def test_outcome_counts_rejects_negative():
    with pytest.raises(ValueError):
        OutcomeCounts(recommended=-1)


def test_outcome_counts_from_mapping_rejects_unknown_key():
    with pytest.raises(ValueError):
        OutcomeCounts.from_mapping({"not_a_real_outcome": 1})


def test_outcome_counts_from_mapping_treats_none_as_zero():
    c = OutcomeCounts.from_mapping({"recommended": None, "not_mentioned": 3})
    assert c.recommended == 0
    assert c.not_mentioned == 3


# ---------------------------------------------------------------------------
# Rank distribution
# ---------------------------------------------------------------------------
def test_rank_distribution_cumulative_and_avg():
    # 6 valid: positions 1, 2, 3, 5, None, None
    positions = [1, 2, 3, 5, None, None]
    d = M.compute_rank_distribution(positions, valid_observations=6)
    assert d.rank_top1_count == 1
    assert d.rank_top3_count == 3   # 1,2,3
    assert d.rank_top5_count == 4   # 1,2,3,5
    assert d.rank_not_listed_count == 2
    # avg position over ranked = (1+2+3+5)/4 = 2.75 -> 2750 milli
    assert d.avg_position_milli == 2750


def test_rank_distribution_none_avg_when_no_positions():
    d = M.compute_rank_distribution([None, None], valid_observations=5)
    assert d.avg_position_milli is None
    assert d.rank_not_listed_count == 5


def test_rank_distribution_rejects_more_positions_than_valid():
    with pytest.raises(ValueError):
        M.compute_rank_distribution([1, 2, 3], valid_observations=2)


# ---------------------------------------------------------------------------
# Share of voice
# ---------------------------------------------------------------------------
def test_share_of_voice():
    assert M.share_of_voice_bps(30, 70) == 3000
    assert M.share_of_voice_bps(0, 0) is None  # no appearances at all
    assert M.share_of_voice_bps(10, 0) == 10000


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------
def test_volatility_flip_rate():
    assert M.volatility_bps([True, True, True]) == 0
    assert M.volatility_bps([True, False, True, False]) == 10000  # 3 flips / 3
    assert M.volatility_bps([True, True, False, False]) == M.rate_bps(1, 3)
    assert M.volatility_bps([True]) == 0  # not measurable


# ---------------------------------------------------------------------------
# Model shift index
# ---------------------------------------------------------------------------
def test_model_shift_zero_when_identical():
    a = OutcomeCounts(recommended=5, not_mentioned=5)
    assert M.model_shift_index_bps(a, a) == 0


def test_model_shift_max_when_disjoint():
    before = OutcomeCounts(recommended=10)          # all recommended
    after = OutcomeCounts(not_mentioned=10)          # all not_mentioned
    assert M.model_shift_index_bps(before, after) == 10000


def test_model_shift_zero_when_one_side_empty():
    before = OutcomeCounts(recommended=10)
    after = OutcomeCounts(entity_ambiguous=10)       # 0 valid
    assert M.model_shift_index_bps(before, after) == 0


# ---------------------------------------------------------------------------
# Trend confidence
# ---------------------------------------------------------------------------
def test_trend_confidence_zero_without_samples():
    assert M.trend_confidence_bps(0, 2, 14, 0) == 0


def test_trend_confidence_full_when_saturated():
    v = M.trend_confidence_bps(
        valid_observations=30, independent_source_types=2, span_days=14,
        volatility_value_bps=0,
    )
    assert v == 10000


def test_trend_confidence_penalized_by_volatility():
    stable = M.trend_confidence_bps(30, 2, 14, 0)
    volatile = M.trend_confidence_bps(30, 2, 14, 5000)
    assert volatile < stable
    assert volatile == 5000  # 1.0 * (1 - 0.5)


def test_trend_confidence_public_uses_brand_independence():
    private = M.trend_confidence_bps(30, 2, 14, 0)
    public_low = M.trend_confidence_bps(30, 2, 14, 0, independent_brand_buckets=0)
    assert public_low == 0
    assert private == 10000


# ---------------------------------------------------------------------------
# Confirmed change
# ---------------------------------------------------------------------------
def test_confirmed_change_requires_anomaly():
    assert M.is_confirmed_change(False, 3, 3, 2, 3) is False


def test_confirmed_change_threshold():
    assert M.is_confirmed_change(True, 2, 3, 2, 3) is True   # 2/3 >= 2/3
    assert M.is_confirmed_change(True, 1, 3, 2, 3) is False  # 1/3 < 2/3
    assert M.is_confirmed_change(True, 0, 0, 2, 3) is False  # no confirmations


# ---------------------------------------------------------------------------
# Stability status priority
# ---------------------------------------------------------------------------
def test_stability_priority():
    # insufficient wins even if shifted/volatile
    assert M.stability_status(5, 9000, 9000, min_valid_observations=10) == "insufficient"
    # shifted wins over watch
    assert M.stability_status(50, 9000, 9000, min_valid_observations=10) == "shifted"
    # watch when volatile but not shifted
    assert M.stability_status(50, 0, 5000, min_valid_observations=10) == "watch"
    # stable otherwise
    assert M.stability_status(50, 0, 0, min_valid_observations=10) == "stable"


def test_intrinsic_and_retrieval_rates_none_without_samples():
    assert M.intrinsic_memory_rate_bps(0, 0) is None
    assert M.retrieval_selection_rate_bps(3, 4) == 7500
