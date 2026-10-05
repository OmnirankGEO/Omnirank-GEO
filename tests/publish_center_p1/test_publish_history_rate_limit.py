from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from auth.global_rate_limiter import (
    PUBLISH_HISTORY_READ_LIMIT,
    PUBLISH_HISTORY_READ_PATH,
    _is_publish_history_read,
    setup_rate_limit_middleware,
)


def _app():
    app = FastAPI()
    setup_rate_limit_middleware(app)

    class InjectUser(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.user = {"user_id": 970001, "is_admin": False}
            return await call_next(request)

    app.add_middleware(InjectUser)

    @app.get(PUBLISH_HISTORY_READ_PATH)
    async def history():
        return {"status": "success"}

    @app.post(PUBLISH_HISTORY_READ_PATH)
    async def history_write():
        return {"status": "unexpected-write"}

    @app.get("/api/ordinary")
    async def ordinary():
        return {"status": "success"}

    return app


def test_publish_history_bucket_match_is_exact_get_only():
    assert _is_publish_history_read(PUBLISH_HISTORY_READ_PATH, "GET") is True
    assert _is_publish_history_read(PUBLISH_HISTORY_READ_PATH, "POST") is False
    assert _is_publish_history_read(PUBLISH_HISTORY_READ_PATH + "/withdraw", "GET") is False


def test_normal_bucket_exhaustion_does_not_starve_history_read():
    seen = []

    def check(key, limit, window):
        seen.append((key, limit, window))
        return ("publish_history_read" in key, 12)

    with patch("auth.global_rate_limiter._check_rate_limit", side_effect=check):
        client = TestClient(_app())
        assert client.get("/api/ordinary").status_code == 429
        response = client.get(PUBLISH_HISTORY_READ_PATH)
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == str(PUBLISH_HISTORY_READ_LIMIT)

    assert any("ratelimit:api:970001" in key for key, _, _ in seen)
    assert any("ratelimit:publish_history_read:970001" in key for key, _, _ in seen)


def test_history_bucket_abuse_returns_429_with_retry_after():
    def check(key, limit, window):
        return (False, 0) if "publish_history_read" in key else (True, limit - 1)

    with patch("auth.global_rate_limiter._check_rate_limit", side_effect=check):
        response = TestClient(_app()).get(PUBLISH_HISTORY_READ_PATH)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json()["code"] == "RATE_LIMITED"


def test_post_to_same_path_stays_in_normal_bucket():
    keys = []

    def check(key, limit, window):
        keys.append(key)
        return False, 0

    with patch("auth.global_rate_limiter._check_rate_limit", side_effect=check):
        response = TestClient(_app()).post(PUBLISH_HISTORY_READ_PATH)

    assert response.status_code == 429
    assert keys == ["ratelimit:api:970001"]


def test_cached_redis_outage_does_not_turn_business_requests_into_500():
    class BrokenRedis:
        def eval(self, *args, **kwargs):
            raise ConnectionError("redis restarted")

    with (
        patch("auth.global_rate_limiter.get_redis", return_value=BrokenRedis()),
        patch("auth.global_rate_limiter._last_redis_failure_log_at", 0.0),
        patch("auth.global_rate_limiter.logger.warning") as warning,
    ):
        client = TestClient(_app())
        response = client.get(PUBLISH_HISTORY_READ_PATH)
        repeated = client.get(PUBLISH_HISTORY_READ_PATH)

    assert response.status_code == 200
    assert response.json() == {"status": "success"}
    assert repeated.status_code == 200
    warning.assert_called_once()
