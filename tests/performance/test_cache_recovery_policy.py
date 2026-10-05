from pathlib import Path

from utils.cache_recovery import (
    PUBLIC_CACHE_RECOVERY_PATH,
    public_cache_recovery_allowed,
    public_clear_site_data_value,
)


def test_normal_api_can_never_request_clear_site_data() -> None:
    assert public_cache_recovery_allowed(
        "/api/wallet",
        "GET",
        {"Sec-Fetch-Site": "same-origin", "X-Omnirank-Cache-Recovery": "1"},
    ) is False


def test_dedicated_same_origin_get_is_allowed() -> None:
    assert public_cache_recovery_allowed(
        PUBLIC_CACHE_RECOVERY_PATH,
        "GET",
        {"Sec-Fetch-Site": "same-origin"},
    ) is True


def test_missing_same_site_cross_site_and_non_get_are_rejected() -> None:
    for fetch_site in ("", "none", "same-site", "cross-site"):
        assert public_cache_recovery_allowed(
            PUBLIC_CACHE_RECOVERY_PATH,
            "GET",
            {"Sec-Fetch-Site": fetch_site},
        ) is False
    assert public_cache_recovery_allowed(
        PUBLIC_CACHE_RECOVERY_PATH,
        "POST",
        {"Sec-Fetch-Site": "same-origin"},
    ) is False


def test_public_recovery_is_cache_only_without_storage_mode() -> None:
    value = public_clear_site_data_value()
    assert value == '"cache"'
    assert "storage" not in value
    assert "cookies" not in value


def test_http_control_surface_is_dedicated_and_has_no_legacy_magic_header() -> None:
    root = Path(__file__).resolve().parents[2]
    server_source = (root / "server.py").read_text(encoding="utf-8")
    client_source = (root / "frontend/src/lib/cacheRecovery.ts").read_text(encoding="utf-8")
    entry_source = (root / "frontend/index.html").read_text(encoding="utf-8")

    route_declaration = '@app.get("/api/public/cache-recovery", include_in_schema=False)'
    assert route_declaration in server_source
    assert '"Clear-Site-Data": public_clear_site_data_value()' in server_source
    route_offset = server_source.index(route_declaration)
    assert "@app.middleware" not in server_source[route_offset:route_offset + 1200]
    assert "X-Omnirank-Cache-Recovery" not in server_source
    assert "X-Omnirank-Cache-Recovery" not in client_source
    assert "fetch('/api/public/cache-recovery'" in client_source
    # The entry chunk cannot import cacheRecovery.ts when that very chunk failed.
    # Its inline recovery branch must therefore implement the same narrow contract.
    assert "fetch('/api/public/cache-recovery'" in entry_source
    assert "method: 'GET'" in entry_source
    assert "credentials: 'same-origin'" in entry_source
    assert "X-Omnirank-Cache-Recovery" not in entry_source
    assert "fetch('/api/public/whitelabel'" not in entry_source
