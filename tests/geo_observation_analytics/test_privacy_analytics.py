"""Privacy gate unit tests: k-anonymity thresholds (raise-only), meets check,
and DTO leak guards."""

from __future__ import annotations

import pytest

from services.geo_observation_analytics import privacy
from services.geo_observation_analytics.privacy import KAnonThresholds


def _cell(valid, users, brands, sources):
    return {
        "valid_observations": valid,
        "independent_user_buckets": users,
        "independent_brand_buckets": brands,
        "independent_source_types": sources,
    }


def test_default_thresholds_match_numeric_gate():
    t = KAnonThresholds()
    assert t.min_valid_observations == 10
    assert t.min_independent_user_buckets == 3
    assert t.min_independent_brand_buckets == 3
    assert t.min_source_types == 2


def test_meets_k_anonymity_all_or_nothing():
    t = KAnonThresholds()
    assert privacy.meets_k_anonymity(_cell(10, 3, 3, 2), t) is True
    assert privacy.meets_k_anonymity(_cell(9, 3, 3, 2), t) is False   # too few obs
    assert privacy.meets_k_anonymity(_cell(10, 2, 3, 2), t) is False  # too few users
    assert privacy.meets_k_anonymity(_cell(10, 3, 2, 2), t) is False  # too few brands
    assert privacy.meets_k_anonymity(_cell(10, 3, 3, 1), t) is False  # too few sources
    assert privacy.meets_k_anonymity(None, t) is False


def test_thresholds_can_only_be_raised():
    base = KAnonThresholds()
    stricter = KAnonThresholds(min_valid_observations=20, min_source_types=2)
    merged = base.raised_to(stricter)
    assert merged.min_valid_observations == 20  # raised
    assert merged.min_source_types == 2         # unchanged
    # a policy that tries to LOWER cannot: raised_to keeps the max
    looser = KAnonThresholds(min_valid_observations=5)
    assert base.raised_to(looser).min_valid_observations == 10


def test_private_leak_guard_catches_owner_id():
    with pytest.raises(privacy.PrivacyLeakError):
        privacy.assert_no_private_leak({"summary": {"owner_user_id": 5}})
    # brand_id is allowed in a private DTO
    privacy.assert_no_private_leak({"brand": {"brand_id": 900001}})


def test_public_leak_guard_also_catches_brand_id():
    with pytest.raises(privacy.PrivacyLeakError):
        privacy.assert_no_public_leak({"brand": {"brand_id": 900001}})
    with pytest.raises(privacy.PrivacyLeakError):
        privacy.assert_no_public_leak({"x": {"aggregate_key": "abc"}})
    # clean public payload passes
    privacy.assert_no_public_leak({"industry_key": "x", "presence_rate_bps": 5000})


def test_leak_guard_walks_lists():
    with pytest.raises(privacy.PrivacyLeakError):
        privacy.assert_no_private_leak({"items": [{"ok": 1}, {"provider_trace_id": "t"}]})
