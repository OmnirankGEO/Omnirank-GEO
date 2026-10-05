from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def mask_pii(value: str) -> str:
    text = str(value or "")
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _ID_RE.sub("[ID]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    return text


def mask_pii_recursive(value: Any) -> Any:
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, list):
        return [mask_pii_recursive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_pii_recursive(item) for item in value)
    if isinstance(value, dict):
        return {key: mask_pii_recursive(item) for key, item in value.items()}
    return value
