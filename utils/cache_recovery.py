"""Pure policy helpers for the dedicated public HTTP-cache recovery endpoint."""

from collections.abc import Mapping


PUBLIC_CACHE_RECOVERY_PATH = "/api/public/cache-recovery"


def public_cache_recovery_allowed(
    path: str,
    method: str,
    headers: Mapping[str, str],
) -> bool:
    """Allow only a browser same-origin GET to the dedicated control endpoint."""
    if path != PUBLIC_CACHE_RECOVERY_PATH or method.upper() != "GET":
        return False
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    fetch_site = normalized.get("sec-fetch-site", "").strip().lower()
    return fetch_site == "same-origin"


def public_clear_site_data_value() -> str:
    """Public clients may clear HTTP cache only; never storage/cookies/IndexedDB."""
    return '"cache"'
