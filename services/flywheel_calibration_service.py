"""Calibration helpers for placement and writing flywheel.

Calibration output is shadow-only.  It can suggest adjustments, but it never
silently changes production recommendation or writing prompts.
"""

from __future__ import annotations

from typing import Any


CALIBRATION_VERSION = "geo_flywheel_calibration_v1_2026-06-12"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_prediction_error(prediction: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    predicted = _num(prediction.get("predicted_citation_lift_30d"), 0)
    actual = _num(outcome.get("ai_citations_delta_30d") or outcome.get("citation_lift_30d"), 0)
    error = actual - predicted
    denominator = max(1.0, abs(predicted))
    return {
        "predicted": predicted,
        "actual": actual,
        "absolute_error": round(abs(error), 4),
        "relative_error": round(error / denominator, 4),
        "direction": "under_predicted" if error > 0 else ("over_predicted" if error < 0 else "matched"),
    }


def build_calibration_suggestion(
    *,
    scope_key: str,
    predictions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    paired = zip(predictions, outcomes)
    errors = [compute_prediction_error(p, o) for p, o in paired]
    if not errors:
        return {
            "calibration_version": CALIBRATION_VERSION,
            "scope_key": scope_key,
            "status": "insufficient_data",
            "requires_admin_review": True,
            "suggested_adjustment": 0.0,
            "errors": [],
        }
    avg_relative_error = sum(e["relative_error"] for e in errors) / len(errors)
    suggested_adjustment = max(-0.15, min(0.15, avg_relative_error * 0.25))
    return {
        "calibration_version": CALIBRATION_VERSION,
        "scope_key": scope_key,
        "status": "shadow_suggestion",
        "requires_admin_review": True,
        "suggested_adjustment": round(suggested_adjustment, 4),
        "sample_count": len(errors),
        "avg_relative_error": round(avg_relative_error, 4),
        "errors": errors,
        "guardrail": "只写入校准建议，不自动修改线上推荐权重或写作策略",
    }


def _coerce_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            import json as _json
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def build_calibration_from_db(scope_key: str, *, limit: int = 200) -> dict[str, Any]:
    """[W6.3] 从库读 predictions/outcomes(按 prediction_id 配对)算校准建议。

    此前 calibration-preview 只从 request body 读(两张 geo_recommendation_* 表零读取);W6 回写
    落库后,这里改为读库配对,校准建议进看板回环面板。仍 shadow-only,不自动改线上权重。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.predicted_payload AS pred, o.outcome_payload AS outc
            FROM geo_recommendation_outcomes o
            JOIN geo_recommendation_predictions p ON p.id = o.prediction_id
            WHERE o.scope_key = %s
            ORDER BY o.observed_at DESC
            LIMIT %s
            """,
            (scope_key, int(limit)),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    predictions = [_coerce_payload(r.get("pred")) for r in rows]
    outcomes = [_coerce_payload(r.get("outc")) for r in rows]
    result = build_calibration_suggestion(scope_key=scope_key, predictions=predictions, outcomes=outcomes)
    result["source"] = "db"
    return result
