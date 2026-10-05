"""Privacy gates: public k-anonymity / cell suppression and DTO leak checks.

The deterministic thresholds default to the 04 §7.5 numeric gate and may only be
RAISED by ``geo_observation_policy`` (AI-2), never lowered silently. A public
cell that fails any threshold returns an explicit insufficient-samples state; it
must NOT fall back to a wider industry, a longer window, or a dropped dimension.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .contract import (
    PRIVACY_FORBIDDEN_FIELDS,
    PUBLIC_DTO_ALL_FORBIDDEN_FIELDS,
    PUBLIC_MIN_INDEPENDENT_BRAND_BUCKETS,
    PUBLIC_MIN_INDEPENDENT_USER_BUCKETS,
    PUBLIC_MIN_SOURCE_TYPES,
    PUBLIC_MIN_VALID_OBSERVATIONS,
)


@dataclass(frozen=True)
class KAnonThresholds:
    min_valid_observations: int = PUBLIC_MIN_VALID_OBSERVATIONS
    min_independent_user_buckets: int = PUBLIC_MIN_INDEPENDENT_USER_BUCKETS
    min_independent_brand_buckets: int = PUBLIC_MIN_INDEPENDENT_BRAND_BUCKETS
    min_source_types: int = PUBLIC_MIN_SOURCE_TYPES

    def raised_to(self, other: "KAnonThresholds") -> "KAnonThresholds":
        """Return the element-wise MAX (thresholds may only be raised)."""
        return KAnonThresholds(
            min_valid_observations=max(self.min_valid_observations, other.min_valid_observations),
            min_independent_user_buckets=max(
                self.min_independent_user_buckets, other.min_independent_user_buckets
            ),
            min_independent_brand_buckets=max(
                self.min_independent_brand_buckets, other.min_independent_brand_buckets
            ),
            min_source_types=max(self.min_source_types, other.min_source_types),
        )


def meets_k_anonymity(agg_row: Mapping[str, object], thresholds: KAnonThresholds) -> bool:
    """True only when ALL of valid_observations / independent user buckets /
    independent brand buckets / independent source types meet their minimum."""
    if agg_row is None:
        return False
    return (
        int(agg_row["valid_observations"]) >= thresholds.min_valid_observations
        and int(agg_row["independent_user_buckets"]) >= thresholds.min_independent_user_buckets
        and int(agg_row["independent_brand_buckets"]) >= thresholds.min_independent_brand_buckets
        and int(agg_row["independent_source_types"]) >= thresholds.min_source_types
    )


class PrivacyLeakError(AssertionError):
    """Raised when a DTO about to leave the service carries a forbidden field."""


def _walk_keys(obj) -> Iterable[str]:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _walk_keys(v)


def assert_no_private_leak(payload) -> None:
    """Guard any non-admin DTO: no owner/upstream/cost/trace/source-PK keys."""
    hits = {k for k in _walk_keys(payload) if k in PRIVACY_FORBIDDEN_FIELDS}
    if hits:
        raise PrivacyLeakError(f"private DTO leaked forbidden fields: {sorted(hits)}")


def assert_no_public_leak(payload) -> None:
    """Guard any public-industry DTO: also forbids brand_id."""
    hits = {k for k in _walk_keys(payload) if k in PUBLIC_DTO_ALL_FORBIDDEN_FIELDS}
    if hits:
        raise PrivacyLeakError(f"public DTO leaked forbidden fields: {sorted(hits)}")
