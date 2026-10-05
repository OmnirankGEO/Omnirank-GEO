"""Cross-bucket trend logic: period-over-period comparison, model-shift markers,
and confirmed_change (single buckets never assert a direction — only a run of
same-direction buckets past the confirmation ratio does)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .metrics import MODEL_SHIFT_BPS_THRESHOLD, is_confirmed_change

# A period-over-period presence change beyond this is a candidate anomaly.
ANOMALY_PRESENCE_DELTA_BPS = 1500


def is_model_shift_marker(row: dict) -> bool:
    return (
        row.get("stability_status") == "shifted"
        or int(row.get("model_shift_index_bps") or 0) >= MODEL_SHIFT_BPS_THRESHOLD
    )


@dataclass(frozen=True)
class Comparison:
    presence_change_bps: Optional[int]
    recommendation_change_bps: Optional[int]
    comparison_allowed: bool
    reason: Optional[str]


def compare_periods(current: Optional[dict], previous: Optional[dict]) -> Comparison:
    """Compare two overall aggregates. Disallow the comparison when a model shift
    sits on either side (a caliber break is not a customer gain/loss)."""
    if current is None or previous is None:
        return Comparison(None, None, False, "insufficient_history")
    if is_model_shift_marker(current) or is_model_shift_marker(previous):
        return Comparison(
            presence_change_bps=int(current["presence_rate_bps"]) - int(previous["presence_rate_bps"]),
            recommendation_change_bps=int(current["explicit_recommendation_rate_bps"])
            - int(previous["explicit_recommendation_rate_bps"]),
            comparison_allowed=False,
            reason="model_shift",
        )
    return Comparison(
        presence_change_bps=int(current["presence_rate_bps"]) - int(previous["presence_rate_bps"]),
        recommendation_change_bps=int(current["explicit_recommendation_rate_bps"])
        - int(previous["explicit_recommendation_rate_bps"]),
        comparison_allowed=True,
        reason=None,
    )


@dataclass(frozen=True)
class TrendPoint:
    bucket_start: str
    bucket_end: str
    valid_observations: int
    presence_rate_bps: int
    explicit_recommendation_rate_bps: int
    stability_status: str
    model_shift_marker: bool
    confirmed_change: bool


def build_trend_points(
    series: Sequence[dict], *, numerator: int, denominator: int
) -> list[TrendPoint]:
    """``series`` is overall aggregates ordered ascending by bucket_start."""
    points: list[TrendPoint] = []
    n = len(series)
    for i, row in enumerate(series):
        confirmed = _confirmed_change_at(series, i, numerator, denominator)
        points.append(
            TrendPoint(
                bucket_start=str(row["bucket_start"]),
                bucket_end=str(row["bucket_end"]),
                valid_observations=int(row["valid_observations"]),
                presence_rate_bps=int(row["presence_rate_bps"]),
                explicit_recommendation_rate_bps=int(row["explicit_recommendation_rate_bps"]),
                stability_status=row["stability_status"],
                model_shift_marker=is_model_shift_marker(row),
                confirmed_change=confirmed,
            )
        )
    return points


def _confirmed_change_at(series: Sequence[dict], i: int, num: int, den: int) -> bool:
    """A change at bucket i (vs i-1) is confirmed only when at least num/den of
    the following buckets continue in the SAME direction, and neither the change
    bucket nor its confirmations sit on a model shift."""
    if i == 0:
        return False
    prev, cur = series[i - 1], series[i]
    if is_model_shift_marker(cur) or is_model_shift_marker(prev):
        return False  # a caliber break is never a confirmed customer change
    delta = int(cur["presence_rate_bps"]) - int(prev["presence_rate_bps"])
    if abs(delta) < ANOMALY_PRESENCE_DELTA_BPS:
        return False
    direction = 1 if delta > 0 else -1
    followers = series[i + 1 :]
    if not followers:
        return False
    same = 0
    total = 0
    ref = int(cur["presence_rate_bps"])
    for f in followers:
        if is_model_shift_marker(f):
            break
        total += 1
        fdelta = int(f["presence_rate_bps"]) - ref
        if (fdelta > 0 and direction > 0) or (fdelta < 0 and direction < 0) or (fdelta == 0):
            same += 1
        ref = int(f["presence_rate_bps"])
    return is_confirmed_change(True, same, total, num, den)
