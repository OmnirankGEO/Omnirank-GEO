from __future__ import annotations

import re

import bleach

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|iframe|form)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def clean_html(html: str, *, max_chars: int = 12000) -> str:
    without_blocks = _SCRIPT_STYLE_RE.sub("", str(html or ""))
    cleaned = bleach.clean(without_blocks, tags=[], attributes={}, strip=True)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[: max(1, int(max_chars))]
