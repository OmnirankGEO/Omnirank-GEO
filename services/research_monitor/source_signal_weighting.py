"""Source signal weighting for GEO research flywheel.

The important distinction is whether a source was actually adopted by the AI
answer, merely cited, merely returned by search, or only crawled as reference.
Raw crawled pages are kept for audit, but low-weight by default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


SIGNAL_WEIGHTS = {
    "answer_adopted": 1.00,
    "cited_source": 0.78,
    "search_result_only": 0.32,
    "crawled_reference_only": 0.12,
    "rejected_noise": 0.0,
}

# [T3 口径 SSOT 2026-07-03] "明确引用"(explicit citation)= AI 回答里真用了这个来源:
#   answer_adopted(答案直接采纳)+ cited_source(明确引用标记)。单点定义,禁止别处再独立写第二份。
#   metrics 的 explicit_cited(answer_adoption_metrics.metric_flags)与 source_signals rollup 的
#   explicit_cited_count 都从这里取,保证运营只见一个一致的数。
EXPLICIT_CITED_TIERS = frozenset({"answer_adopted", "cited_source"})

ENGINE_WEIGHTS = {
    "doubao": 1.0,
    "kimi": 1.0,
    "deepseek": 1.0,
    "qwen": 1.0,
    "dashscope": 1.0,
}


@dataclass(frozen=True)
class SourceSignal:
    source_url: str
    engine: str
    prompt_id: str
    signal_tier: str
    source_position: int = 0
    total_sources_in_answer: int = 1
    industry_key: str = "general"
    answer_mentioned_brand: bool = False
    metadata: dict[str, Any] | None = None


def normalize_signal_tier(value: str) -> str:
    tier = (value or "").strip().lower()
    aliases = {
        "adopted": "answer_adopted",
        "answer": "answer_adopted",
        "citation": "cited_source",
        "cited": "cited_source",
        "search": "search_result_only",
        "jina": "crawled_reference_only",
        "crawl": "crawled_reference_only",
        "reference": "crawled_reference_only",
        "noise": "rejected_noise",
    }
    return aliases.get(tier, tier if tier in SIGNAL_WEIGHTS else "crawled_reference_only")


def signal_weight(signal_tier: str) -> float:
    return SIGNAL_WEIGHTS[normalize_signal_tier(signal_tier)]


def balanced_source_credit(total_sources_in_answer: int) -> float:
    """Normalize per-source credit so long native-search lists remain fair."""
    count = max(1, int(total_sources_in_answer or 1))
    # sqrt keeps useful signal from rich answers while preventing 35-source
    # answers from overwhelming 7-source engines.
    return 1 / math.sqrt(count)


def position_decay(position: int) -> float:
    pos = max(1, int(position or 1))
    return max(0.70, 1 / math.sqrt(pos))


def weighted_signal_value(signal: SourceSignal) -> float:
    tier = normalize_signal_tier(signal.signal_tier)
    base = signal_weight(tier)
    engine = (signal.engine or "").strip().lower()
    engine_weight = ENGINE_WEIGHTS.get(engine, 1.0)
    adopted_bonus = 1.15 if signal.answer_mentioned_brand and tier == "answer_adopted" else 1.0
    return round(
        base
        * balanced_source_credit(signal.total_sources_in_answer)
        * position_decay(signal.source_position)
        * engine_weight
        * adopted_bonus,
        6,
    )


def aggregate_source_signals(signals: list[SourceSignal]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for signal in signals:
        key = (signal.source_url, signal.industry_key or "general")
        item = grouped.setdefault(
            key,
            {
                "source_url": signal.source_url,
                "industry_key": signal.industry_key or "general",
                "balanced_weight": 0.0,
                "answer_adopted_count": 0,
                "cited_count": 0,
                "search_only_count": 0,
                "reference_only_count": 0,
                "engines": set(),
                "prompts": set(),
                "signal_tiers": set(),
            },
        )
        value = weighted_signal_value(signal)
        item["balanced_weight"] += value
        item["engines"].add(signal.engine)
        item["prompts"].add(signal.prompt_id)
        tier = normalize_signal_tier(signal.signal_tier)
        item["signal_tiers"].add(tier)
        if tier == "answer_adopted":
            item["answer_adopted_count"] += 1
        elif tier == "cited_source":
            item["cited_count"] += 1
        elif tier == "search_result_only":
            item["search_only_count"] += 1
        elif tier == "crawled_reference_only":
            item["reference_only_count"] += 1

    rows: list[dict[str, Any]] = []
    for item in grouped.values():
        rows.append({
            **item,
            "balanced_weight": round(float(item["balanced_weight"]), 6),
            "engine_count": len(item["engines"]),
            "prompt_count": len(item["prompts"]),
            "engines": sorted(x for x in item["engines"] if x),
            "prompts": sorted(x for x in item["prompts"] if x),
            "signal_tiers": sorted(item["signal_tiers"]),
        })
    return sorted(rows, key=lambda r: r["balanced_weight"], reverse=True)
