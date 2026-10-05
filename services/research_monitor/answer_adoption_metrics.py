"""Helpers for real answer-adoption metric snapshots.

The metric layer reads already-classified source signals.  It must not promote
plain search exposure into answer adoption.
"""

from __future__ import annotations

from typing import Any

from services.research_monitor.source_signal_weighting import (
    EXPLICIT_CITED_TIERS,
    balanced_source_credit,
    normalize_signal_tier,
    position_decay,
    signal_weight,
)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def metric_flags(signal_tier: str) -> dict[str, bool]:
    """Return mutually readable flags for one classified signal tier."""
    tier = normalize_signal_tier(signal_tier)
    return {
        "answer_adopted": tier == "answer_adopted",
        "explicit_cited": tier in EXPLICIT_CITED_TIERS,  # [T3 SSOT]
        "search_exposed": tier == "search_result_only",
        "reference_only": tier == "crawled_reference_only",
        "rejected_noise": tier == "rejected_noise",
    }


def normalized_metric_credit(row: dict[str, Any]) -> float:
    """Prefer stored balanced_weight, otherwise compute the same conservative credit."""
    stored = _to_float(row.get("balanced_weight"), 0.0)
    if stored > 0:
        return round(stored, 6)
    total_sources = _to_int(row.get("total_sources_in_answer"), 1)
    source_position = _to_int(row.get("source_position"), 1)
    tier = normalize_signal_tier(str(row.get("signal_tier") or ""))
    credit = signal_weight(tier) * balanced_source_credit(total_sources) * position_decay(source_position)
    return round(float(credit), 6)


def build_metric_payload(row: dict[str, Any]) -> dict[str, Any]:
    tier = normalize_signal_tier(str(row.get("signal_tier") or ""))
    flags = metric_flags(tier)
    return {
        "source_url": row.get("source_url") or "",
        "domain": row.get("domain") or "",
        "industry_key": row.get("industry_key") or "general",
        "engine": row.get("engine") or "",
        "prompt_id": row.get("prompt_id") or "",
        "round_id": row.get("round_id") or "",
        "signal_tier": tier,
        "source_position": _to_int(row.get("source_position"), 0),
        "total_sources_in_answer": max(1, _to_int(row.get("total_sources_in_answer"), 1)),
        "normalized_credit": normalized_metric_credit(row),
        "metadata": {
            "source_signal_id": row.get("id"),
            "observed_at": str(row.get("observed_at") or ""),
        },
        "observed_at": row.get("observed_at"),
        **flags,
    }
