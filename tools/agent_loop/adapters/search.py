from __future__ import annotations

import json
from typing import Any

from tools.search.metaso_mcp import metaso_web_search


async def web_search(query: str, market: str = "", limit: int = 5) -> dict:
    raw = await metaso_web_search(query, size=max(1, min(int(limit or 5), 10)))
    data = _tool_response_to_data(raw)
    items = data.get("webpages") or data.get("results") or data.get("items") or []
    return {"source": "metaso", "query": query, "market": market, "items": items[:limit], "raw": data}


def _tool_response_to_data(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    content = getattr(raw, "content", None)
    if isinstance(content, list) and content:
        text = content[0].get("text") if isinstance(content[0], dict) else str(content[0])
        try:
            parsed = json.loads(text or "{}")
            return parsed if isinstance(parsed, dict) else {"items": parsed}
        except Exception:
            return {"text": text or ""}
    return {"raw": str(raw)}
