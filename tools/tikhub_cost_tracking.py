"""Cost tracking helpers for direct TikHub HTTP calls."""
from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

from tools.llm_call_tracker import llm_track


def _safe_endpoint_label(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or url[:120]


async def tracked_tikhub_request(
    client: Any,
    method: str,
    url: str,
    *,
    caller: str,
    model: str = "rest",
    metadata: Optional[Dict[str, Any]] = None,
    **request_kwargs: Any,
) -> Any:
    """Wrap a TikHub HTTP request so every paid call lands in llm_call_log."""
    merged_metadata = {
        "method": method.upper(),
        "endpoint": _safe_endpoint_label(url),
    }
    if metadata:
        merged_metadata.update(metadata)

    async with llm_track(
        caller,
        "tikhub",
        model=model,
        metadata=merged_metadata,
    ) as tracker:
        response = await client.request(method, url, **request_kwargs)
        status_code = getattr(response, "status_code", 0)
        ok = 200 <= int(status_code or 0) < 400
        tracker.record(
            success=ok,
            error_msg=None if ok else f"HTTP {status_code}: {getattr(response, 'text', '')[:200]}",
        )
        return response


async def tracked_tikhub_get(
    client: Any,
    url: str,
    *,
    caller: str,
    model: str = "rest",
    metadata: Optional[Dict[str, Any]] = None,
    **request_kwargs: Any,
) -> Any:
    return await tracked_tikhub_request(
        client,
        "GET",
        url,
        caller=caller,
        model=model,
        metadata=metadata,
        **request_kwargs,
    )


async def tracked_tikhub_post(
    client: Any,
    url: str,
    *,
    caller: str,
    model: str = "rest",
    metadata: Optional[Dict[str, Any]] = None,
    **request_kwargs: Any,
) -> Any:
    return await tracked_tikhub_request(
        client,
        "POST",
        url,
        caller=caller,
        model=model,
        metadata=metadata,
        **request_kwargs,
    )
