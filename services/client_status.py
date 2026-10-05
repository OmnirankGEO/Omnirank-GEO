"""Customer lifecycle status helpers.

The value is stored on brands.status and is intentionally independent from
quote/payment status. A paid quote is not automatically a won customer.
"""

from __future__ import annotations

from typing import Optional


CLIENT_STATUS_OPTIONS = [
    {"value": "active", "label": "跟进中"},
    {"value": "undecided", "label": "未确定"},
    {"value": "won", "label": "已成交"},
    {"value": "archived", "label": "已归档"},
]

_LABEL_BY_STATUS = {item["value"]: item["label"] for item in CLIENT_STATUS_OPTIONS}

_ALIASES = {
    "active": "active",
    "follow": "active",
    "following": "active",
    "open": "active",
    "draft": "active",
    "pending": "undecided",
    "undecided": "undecided",
    "uncertain": "undecided",
    "unknown": "undecided",
    "confirmed": "won",
    "closed": "won",
    "closed_won": "won",
    "deal": "won",
    "won": "won",
    "archive": "archived",
    "archived": "archived",
}


def normalize_client_status(value: Optional[str], allow_default: bool = True) -> Optional[str]:
    """Normalize stored/UI customer status to the canonical four-state workflow.

    When allow_default is False, unknown or blank values are rejected for write
    paths instead of silently falling back to active.
    """

    raw = "" if value is None else str(value).strip().lower()
    if not raw:
        return "active" if allow_default else None
    normalized = _ALIASES.get(raw)
    if normalized:
        return normalized
    return "active" if allow_default else None


def client_status_label(value: Optional[str]) -> str:
    normalized = normalize_client_status(value)
    return _LABEL_BY_STATUS.get(normalized or "active", _LABEL_BY_STATUS["active"])
