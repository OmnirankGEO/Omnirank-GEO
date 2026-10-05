from __future__ import annotations

import json
from typing import Any

from .pii_mask import mask_pii, mask_pii_recursive


def sanitize_tool_result(tool_name: str, result: Any, *, max_chars: int = 3000) -> Any:
    masked = mask_pii_recursive(result)
    try:
        rendered = json.dumps(masked, ensure_ascii=False, default=str)
    except Exception:
        rendered = mask_pii(str(masked))
    if len(rendered) <= max_chars:
        return masked
    return {
        "truncated": True,
        "tool": tool_name,
        "text": mask_pii(rendered[:max_chars]),
    }
