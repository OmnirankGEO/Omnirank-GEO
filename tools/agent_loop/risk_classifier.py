"""Risk helpers for Social Studio agent-loop tool fallback decisions."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "high_risk_industries.json"
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@lru_cache(maxsize=1)
def high_risk_industries() -> tuple[str, ...]:
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    values = data.get("industries", [])
    return tuple(str(value).strip() for value in values if str(value).strip())


def classify_tool_call(base_risk: str, args: dict[str, Any] | None = None) -> str:
    """Elevate risk when user-visible arguments mention regulated industries."""
    normalized_base = base_risk if base_risk in _RISK_ORDER else "high"
    if _mentions_high_risk(args or {}):
        return _max_risk(normalized_base, "high")
    return normalized_base


def _mentions_high_risk(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_mentions_high_risk(item) for item in value.values())
    if isinstance(value, list | tuple | set):
        return any(_mentions_high_risk(item) for item in value)
    text = str(value)
    return any(token in text for token in high_risk_industries())


def _max_risk(left: str, right: str) -> str:
    return left if _RISK_ORDER[left] >= _RISK_ORDER[right] else right
