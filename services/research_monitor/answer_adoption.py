"""Infer whether a research source was adopted by the final AI answer."""

from __future__ import annotations

import re
from typing import Any


ANSWER_CITATION_RE = re.compile(r"(?<![A-Za-z0-9_])[\[【](\d{1,3})[\]】]")


def extract_answer_citation_indices(answer_text: str) -> set[int]:
    """Extract explicit answer citation markers such as 来源[1] or 参考【2】."""
    indices: set[int] = set()
    for match in ANSWER_CITATION_RE.finditer(answer_text or ""):
        try:
            indices.add(int(match.group(1)))
        except (TypeError, ValueError):
            continue
    return indices


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def infer_answer_adoption(row: dict[str, Any]) -> dict[str, Any]:
    """Infer row-level answer adoption from answer markers and cite position.

    A URL appearing in a search/source list is not enough. We only mark
    adoption when answer text contains a marker whose number equals this row's
    cite/search position.
    """
    answer_text = str(row.get("answer_text") or "")
    cite_position = _int_or_none(row.get("cite_position") or row.get("search_rank"))
    markers = extract_answer_citation_indices(answer_text)

    if not markers:
        return {
            "is_answer_cited": False,
            "adoption_rank": None,
            "reason": "no_answer_marker",
            "answer_marker_count": 0,
        }
    if cite_position is None:
        return {
            "is_answer_cited": False,
            "adoption_rank": None,
            "reason": "missing_cite_position",
            "answer_marker_count": len(markers),
        }
    if cite_position in markers:
        return {
            "is_answer_cited": True,
            "adoption_rank": cite_position,
            "reason": "answer_marker",
            "answer_marker_count": len(markers),
        }
    return {
        "is_answer_cited": False,
        "adoption_rank": None,
        "reason": "marker_mismatch",
        "answer_marker_count": len(markers),
    }
