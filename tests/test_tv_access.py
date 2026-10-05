"""
板块 D · TV 大屏二次门禁后端契约测试（不连数据库）。

覆盖：
- 密码门：授权/错密码/非法输入/非管理员/畸形 env fail-closed/会话签发失败 503
- P1-1 legacy 兜底哈希显式 opt-in：默认 fail-closed 503 · flag 开 → legacy 可用 · env 配置 → 正常
- P2-2 失败限流：10 次失败 → 60s 冷却 429 TV_ACCESS_RATE_LIMITED + Retry-After · 成功清零
- 会话：刷新恢复/篡改拒绝+清 cookie/数据加载失败 503 无泄漏
- D4 一次性交换码：管理员签发/单次使用/过期/未知码/指纹日志无明文
- D4 legacy URL token：只读兼容 + 迁移引导头/交换会话/flag 关闭 fail-closed
- 前端 bundle 不再携带硬编码明文密码
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


TEST_PASSWORD = "correct-tv-password"
TEST_PASSWORD_HASH = hashlib.pbkdf2_hmac(
    "sha256",
    TEST_PASSWORD.encode("utf-8"),
    b"omnirank-tv-access-v1",
    210_000,
).hex()

LEGACY_TOKEN = "legacy-tv-token-value-0123456789abcdef"


def _app(monkeypatch, *, user=None, dashboard_error: Exception | None = None,
         legacy_token: str | None = None) -> FastAPI:
    import api.dashboard_api as dashboard_api

    monkeypatch.setenv("WORKERS", "1")
    monkeypatch.setenv("TV_ACCESS_PASSWORD_HASH", TEST_PASSWORD_HASH)
    monkeypatch.setenv("JWT_SECRET", "tv-dashboard-test-signing-secret-32-bytes")
    dashboard_api._tv_cache["data"] = None
    dashboard_api._tv_cache["expires_at"] = 0
    dashboard_api._TV_EXCHANGE_CODES.clear()
    dashboard_api._TV_ACCESS_FAILURES.clear()  # 失败限流计数器隔离（P2-2）
    # 默认无 legacy token（fail-closed）；需要时由具体用例传入。绝不触碰真实 DB。
    monkeypatch.setattr(dashboard_api, "_read_legacy_tv_token", lambda: legacy_token)
    if dashboard_error is None:
        monkeypatch.setattr(
            dashboard_api,
            "_compute_tv_dashboard",
            lambda: {"status": "success", "funnel": {"total_users": 7}},
        )
    else:
        def fail_dashboard():
            raise dashboard_error

        monkeypatch.setattr(dashboard_api, "_compute_tv_dashboard", fail_dashboard)

    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.user = user if user is not None else {
            "user_id": 101,
            "username": "qa-admin",
            "is_admin": True,
        }
        return await call_next(request)

    app.include_router(dashboard_api.router)
    return app


class _FakeSharedRedis:
    """TV 共享状态所需的最小 Redis 语义；锁内模拟 Redis 单线程原子执行。"""

    def __init__(self):
        self._values: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()

    def _purge(self, key: str) -> None:
        item = self._values.get(key)
        if item and item[1] is not None and item[1] <= time.time():
            self._values.pop(key, None)

    def set(self, key, value, *, nx=False, ex=None):
        with self._lock:
            self._purge(key)
            if nx and key in self._values:
                return False
            expires_at = time.time() + int(ex) if ex is not None else None
            self._values[str(key)] = (str(value), expires_at)
            return True

    def getdel(self, key):
        with self._lock:
            self._purge(key)
            item = self._values.pop(str(key), None)
            return item[0] if item else None

    def pttl(self, key):
        with self._lock:
            self._purge(key)
            item = self._values.get(str(key))
            if not item:
                return -2
            if item[1] is None:
                return -1
            return max(0, int((item[1] - time.time()) * 1000))

    def delete(self, *keys):
        with self._lock:
            deleted = 0
            for key in keys:
                deleted += int(self._values.pop(str(key), None) is not None)
            return deleted

    def eval(self, _script, numkeys, *parts):
        assert numkeys == 2
        failure_key, cooldown_key = map(str, parts[:2])
        max_failures, cooldown_seconds, counter_ttl = map(int, parts[2:])
        with self._lock:
            self._purge(failure_key)
            self._purge(cooldown_key)
            cooldown = self._values.get(cooldown_key)
            if cooldown:
                ttl_ms = max(1, int((cooldown[1] - time.time()) * 1000))
                return [0, ttl_ms]
            current = int(self._values.get(failure_key, ("0", None))[0]) + 1
            if current >= max_failures:
                self._values.pop(failure_key, None)
                self._values[cooldown_key] = (
                    "1",
                    time.time() + cooldown_seconds,
                )
                return [current, cooldown_seconds * 1000]
            self._values[failure_key] = (
                str(current),
                time.time() + counter_ttl,
            )
            return [current, 0]


def _grant_access(client: TestClient, request_id: str = "tv-access-success-001"):
    return client.post(
        "/api/tv/access",
        json={"password": TEST_PASSWORD},
        headers={"X-Request-ID": request_id},
    )


# ========== 密码门（候选资产 port） ==========

def test_tv_access_grants_http_only_session_and_survives_refresh(monkeypatch):
    client = TestClient(_app(monkeypatch), base_url="https://testserver")

    granted = _grant_access(client)

    assert granted.status_code == 200
    assert granted.json() == {
        "success": True,
        "status": "success",
        "code": "TV_ACCESS_GRANTED",
        "message": "大屏访问已授权",
        "request_id": "tv-access-success-001",
    }
    assert TEST_PASSWORD not in granted.text
    cookie_header = granted.headers["set-cookie"]
    assert "omnirank_tv_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header
    assert "Secure" in cookie_header
    assert TEST_PASSWORD not in cookie_header

    restored = client.get(
        "/api/tv/access/session",
        headers={"X-Request-ID": "tv-session-restore-001"},
    )
    assert restored.status_code == 200
    assert restored.json() == {
        "success": True,
        "status": "success",
        "code": "TV_ACCESS_SESSION_ACTIVE",
        "message": "大屏访问会话有效",
        "request_id": "tv-session-restore-001",
        "authenticated": True,
    }

    dashboard = client.get(
        "/api/tv/dashboard/auth",
        headers={"X-Request-ID": "tv-dashboard-load-001"},
    )
    assert dashboard.status_code == 200
    assert dashboard.json()["funnel"]["total_users"] == 7
    assert dashboard.json()["code"] == "TV_DASHBOARD_READY"
    assert dashboard.json()["message"] == "大屏数据已加载"
    assert dashboard.json()["request_id"] == "tv-dashboard-load-001"


def test_tv_access_wrong_password_has_stable_retryable_contract(monkeypatch):
    client = TestClient(_app(monkeypatch), base_url="https://testserver")

    response = client.post(
        "/api/tv/access",
        json={"password": "wrong-password"},
        headers={"X-Request-ID": "tv-access-denied-001"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "success": False,
        "status": "error",
        "code": "TV_ACCESS_DENIED",
        "message": "访问密码不正确",
        "request_id": "tv-access-denied-001",
    }
    assert TEST_PASSWORD not in response.text
    assert "traceback" not in response.text.lower()
    assert "omnirank_tv_session" not in response.headers.get("set-cookie", "")


def test_tv_access_requires_injected_legacy_hash_for_explicit_opt_in(monkeypatch):
    """legacy 轮换只允许 secret-store 哈希；源码无明文或内置 verifier。"""
    import api.dashboard_api as dashboard_api

    monkeypatch.delenv("TV_ACCESS_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("TV_ACCESS_PASSWORD_LEGACY_HASH", raising=False)
    # 默认（flag 未开）：未配置 env hash → 校验器 fail-closed 抛错（→ 端点 503）
    monkeypatch.delenv("TV_ACCESS_PASSWORD_LEGACY_ENABLED", raising=False)
    with pytest.raises(ValueError):
        dashboard_api._verify_tv_password(TEST_PASSWORD)
    # 仅开 flag 仍 fail-closed；必须同时从 secret store 注入 legacy hash。
    monkeypatch.setenv("TV_ACCESS_PASSWORD_LEGACY_ENABLED", "true")
    with pytest.raises(ValueError):
        dashboard_api._verify_tv_password(TEST_PASSWORD)
    monkeypatch.setenv("TV_ACCESS_PASSWORD_LEGACY_HASH", TEST_PASSWORD_HASH)
    assert dashboard_api._verify_tv_password(TEST_PASSWORD) is True
    repo_root = Path(__file__).resolve().parents[1]
    frontend_source = (
        repo_root / "frontend" / "src" / "pages" / "Home" / "TVDashboard.tsx"
    ).read_text(encoding="utf-8")
    assert TEST_PASSWORD not in frontend_source
    legacy_static = (repo_root / "frontend" / "public" / "tv.html").read_text(encoding="utf-8")
    assert TEST_PASSWORD not in legacy_static
    assert "71faa74b1886bd61" not in legacy_static  # 旧硬编码默认 token（后门常量）一并清除


def test_tv_access_unconfigured_env_fails_closed_503_by_default(monkeypatch):
    """P1-1：未配置 TV_ACCESS_PASSWORD_HASH 且 flag 默认关 → /api/tv/access 503 fail-closed（旧公开密码不再可用）。"""
    client = TestClient(_app(monkeypatch), base_url="https://testserver")
    monkeypatch.delenv("TV_ACCESS_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("TV_ACCESS_PASSWORD_LEGACY_ENABLED", raising=False)

    response = client.post(
        "/api/tv/access",
        json={"password": TEST_PASSWORD},
        headers={"X-Request-ID": "tv-access-no-env-001"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "success": False,
        "status": "error",
        "code": "TV_ACCESS_UNAVAILABLE",
        "message": "大屏访问服务暂时不可用，请稍后重试",
        "request_id": "tv-access-no-env-001",
    }
    assert "omnirank_tv_session" not in response.headers.get("set-cookie", "")


def test_tv_access_legacy_password_endpoint_uses_injected_hash(monkeypatch):
    """P1-1：轮换窗口必须显式开关并注入 legacy 哈希。"""
    client = TestClient(_app(monkeypatch), base_url="https://testserver")
    monkeypatch.delenv("TV_ACCESS_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("TV_ACCESS_PASSWORD_LEGACY_ENABLED", "true")
    monkeypatch.setenv("TV_ACCESS_PASSWORD_LEGACY_HASH", TEST_PASSWORD_HASH)

    granted = client.post(
        "/api/tv/access",
        json={"password": TEST_PASSWORD},
        headers={"X-Request-ID": "tv-access-legacy-opt-in-001"},
    )

    assert granted.status_code == 200
    assert granted.json()["code"] == "TV_ACCESS_GRANTED"
    assert "omnirank_tv_session=" in granted.headers["set-cookie"]


def test_tv_access_rejects_invalid_input_and_non_admin_without_stack(monkeypatch):
    admin_client = TestClient(_app(monkeypatch), base_url="https://testserver")
    invalid = admin_client.post(
        "/api/tv/access",
        content=b"{not-json",
        headers={
            "Content-Type": "application/json",
            "X-Request-ID": "tv-invalid-input-001",
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "TV_ACCESS_INPUT_INVALID"
    assert invalid.json()["message"] == "请输入有效的大屏访问密码"
    assert invalid.json()["request_id"] == "tv-invalid-input-001"

    non_admin_client = TestClient(
        _app(
            monkeypatch,
            user={"user_id": 103, "username": "qa-user", "is_admin": False},
        ),
        base_url="https://testserver",
    )
    denied = non_admin_client.post(
        "/api/tv/access",
        json={"password": TEST_PASSWORD},
        headers={"X-Request-ID": "tv-admin-required-001"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "TV_ADMIN_REQUIRED"
    assert denied.json()["message"] == "仅管理员可访问大屏"
    assert denied.json()["request_id"] == "tv-admin-required-001"
    assert TEST_PASSWORD not in denied.text


def test_tv_access_session_rejects_tampering_and_dashboard_failure_is_safe(monkeypatch):
    app = _app(monkeypatch, dashboard_error=RuntimeError("db-secret traceback marker"))
    client = TestClient(app, base_url="https://testserver")

    missing = client.get(
        "/api/tv/access/session",
        headers={"X-Request-ID": "tv-session-missing-001"},
    )
    assert missing.status_code == 401
    assert missing.json()["code"] == "TV_ACCESS_SESSION_REQUIRED"

    client.cookies.set("omnirank_tv_session", "cookie-secret-marker", path="/api/tv")
    tampered = client.get(
        "/api/tv/access/session",
        headers={"X-Request-ID": "tv-session-tampered-001"},
    )
    assert tampered.status_code == 401
    assert tampered.json()["code"] == "TV_ACCESS_SESSION_INVALID"
    assert "cookie-secret-marker" not in tampered.text
    # 会话被判无效时必须同时下发清 cookie 指令
    cleared = tampered.headers.get("set-cookie", "")
    assert "omnirank_tv_session=" in cleared

    client.cookies.set("omnirank_tv_session", "not-base64!.signature", path="/api/tv")
    malformed = client.get(
        "/api/tv/access/session",
        headers={"X-Request-ID": "tv-session-malformed-001"},
    )
    assert malformed.status_code == 401
    assert malformed.json()["code"] == "TV_ACCESS_SESSION_INVALID"

    granted = _grant_access(client, "tv-access-before-failure-001")
    assert granted.status_code == 200
    failed = client.get(
        "/api/tv/dashboard/auth",
        headers={"X-Request-ID": "tv-dashboard-failure-001"},
    )
    assert failed.status_code == 503
    assert failed.json() == {
        "success": False,
        "status": "error",
        "code": "TV_DASHBOARD_UNAVAILABLE",
        "message": "大屏数据暂时不可用，请稍后重试",
        "request_id": "tv-dashboard-failure-001",
    }
    assert "db-secret" not in failed.text
    assert "traceback" not in failed.text.lower()


def test_tv_access_session_creation_failure_keeps_stable_contract(monkeypatch):
    import api.dashboard_api as dashboard_api

    client = TestClient(_app(monkeypatch), base_url="https://testserver")
    monkeypatch.setattr(
        dashboard_api,
        "_create_tv_session",
        lambda _user_id: (_ for _ in ()).throw(RuntimeError("signing secret marker")),
    )

    response = _grant_access(client, "tv-session-create-failure-001")

    assert response.status_code == 503
    assert response.json() == {
        "success": False,
        "status": "error",
        "code": "TV_ACCESS_UNAVAILABLE",
        "message": "大屏访问服务暂时不可用，请稍后重试",
        "request_id": "tv-session-create-failure-001",
    }
    assert "signing secret" not in response.text


def test_tv_access_malformed_password_env_fails_closed_503(monkeypatch):
    """TV_ACCESS_PASSWORD_HASH 配置畸形（非 64 位 hex）→ 校验器不可用 → 503 fail-closed，不泄漏配置内容。"""
    client = TestClient(_app(monkeypatch), base_url="https://testserver")
    monkeypatch.setenv("TV_ACCESS_PASSWORD_HASH", "not-a-valid-hex-digest")

    response = client.post(
        "/api/tv/access",
        json={"password": TEST_PASSWORD},
        headers={"X-Request-ID": "tv-access-bad-env-001"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "TV_ACCESS_UNAVAILABLE"
    assert response.json()["request_id"] == "tv-access-bad-env-001"
    assert "not-a-valid-hex-digest" not in response.text
    assert "traceback" not in response.text.lower()
    assert "ValueError" not in response.text


# ========== P2-2 · /api/tv/access 失败限流 ==========

def test_tv_access_rate_limited_after_ten_failures(monkeypatch):
    """连续 10 次错密码 → 第 11 次起 429 TV_ACCESS_RATE_LIMITED + Retry-After（统一契约）。"""
    client = TestClient(_app(monkeypatch), base_url="https://testserver")

    for attempt in range(10):
        denied = client.post("/api/tv/access", json={"password": "wrong-password"})
        assert denied.status_code == 403, f"第 {attempt + 1} 次失败应仍为 403"
        assert denied.json()["code"] == "TV_ACCESS_DENIED"

    limited = client.post(
        "/api/tv/access",
        json={"password": "wrong-password"},
        headers={"X-Request-ID": "tv-access-rate-limited-001"},
    )
    assert limited.status_code == 429
    assert limited.json() == {
        "success": False,
        "status": "error",
        "code": "TV_ACCESS_RATE_LIMITED",
        "message": "尝试次数过多，请稍后重试",
        "request_id": "tv-access-rate-limited-001",
    }
    retry_after = int(limited.headers["Retry-After"])
    assert 1 <= retry_after <= 60

    # 冷却期内连正确密码也不放行（不给密码猜测任何反馈）
    still_limited = client.post("/api/tv/access", json={"password": TEST_PASSWORD})
    assert still_limited.status_code == 429
    assert still_limited.json()["code"] == "TV_ACCESS_RATE_LIMITED"
    assert "omnirank_tv_session" not in still_limited.headers.get("set-cookie", "")


def test_tv_access_success_clears_failure_count(monkeypatch):
    """成功登录清零失败计数：9 次失败 → 成功 → 再失败需重新计满 10 次才 429。"""
    client = TestClient(_app(monkeypatch), base_url="https://testserver")

    for _ in range(9):
        denied = client.post("/api/tv/access", json={"password": "wrong-password"})
        assert denied.status_code == 403

    granted = _grant_access(client)
    assert granted.status_code == 200

    # 清零后重新计窗口：第 1-10 次失败仍 403，第 11 次才 429
    for attempt in range(10):
        denied = client.post("/api/tv/access", json={"password": "wrong-password"})
        assert denied.status_code == 403, f"清零后第 {attempt + 1} 次失败应仍为 403"
    limited = client.post("/api/tv/access", json={"password": "wrong-password"})
    assert limited.status_code == 429
    assert limited.json()["code"] == "TV_ACCESS_RATE_LIMITED"
    assert "Retry-After" in limited.headers


# ========== D4 · 一次性交换码 ==========

def test_admin_mints_one_time_exchange_code_and_it_is_single_use(monkeypatch):
    client = TestClient(_app(monkeypatch), base_url="https://testserver")

    mint = client.post("/api/admin/tv-token")
    assert mint.status_code == 200
    payload = mint.json()
    assert payload["status"] == "success"
    code = payload["exchange_code"]
    assert isinstance(code, str) and len(code) >= 32
    assert payload["url"] == f"/tv?token={code}"
    assert payload["expires_in"] == 600

    first = client.post(
        "/api/tv/access/exchange",
        json={"token": code},
        headers={"X-Request-ID": "tv-exchange-first-001"},
    )
    assert first.status_code == 200
    assert first.json()["code"] == "TV_ACCESS_GRANTED"
    assert "omnirank_tv_session=" in first.headers["set-cookie"]
    assert code not in first.text  # 交换码明文不出现在响应体

    # 单次使用：第二次交换同一个码必须拒绝
    second = client.post(
        "/api/tv/access/exchange",
        json={"token": code},
        headers={"X-Request-ID": "tv-exchange-second-001"},
    )
    assert second.status_code == 403
    assert second.json()["code"] == "TV_ACCESS_DENIED"
    assert "omnirank_tv_session" not in second.headers.get("set-cookie", "")


def test_admin_tv_token_requires_admin(monkeypatch):
    non_admin_client = TestClient(
        _app(monkeypatch, user={"user_id": 103, "username": "qa-user", "is_admin": False}),
        base_url="https://testserver",
    )
    denied = non_admin_client.post("/api/admin/tv-token")
    assert denied.status_code == 403


def test_exchange_rejects_unknown_expired_and_blank_codes(monkeypatch):
    import api.dashboard_api as dashboard_api

    client = TestClient(_app(monkeypatch), base_url="https://testserver")

    blank = client.post("/api/tv/access/exchange", json={})
    assert blank.status_code == 400
    assert blank.json()["code"] == "TV_ACCESS_INPUT_INVALID"

    unknown = client.post(
        "/api/tv/access/exchange",
        json={"token": "never-minted-code"},
        headers={"X-Request-ID": "tv-exchange-unknown-001"},
    )
    assert unknown.status_code == 403
    assert unknown.json()["code"] == "TV_ACCESS_DENIED"
    assert unknown.json()["message"] == "安全链接无效或已被使用，请让管理员重新生成或改用访问密码"

    # 20 分钟前签发的码（TTL 10 分钟）→ 已过期
    expired_code = dashboard_api._mint_tv_exchange_code(101, now=time.time() - 1200)
    expired = client.post(
        "/api/tv/access/exchange",
        json={"token": expired_code},
        headers={"X-Request-ID": "tv-exchange-expired-001"},
    )
    assert expired.status_code == 403
    assert expired.json()["code"] == "TV_ACCESS_DENIED"
    assert expired.json()["message"] == "安全链接已过期，请让管理员重新生成"


def test_exchange_denial_logs_fingerprint_never_plaintext(monkeypatch, caplog):
    import api.dashboard_api as dashboard_api

    client = TestClient(_app(monkeypatch), base_url="https://testserver")
    secret_token = "super-secret-exchange-token-value"
    with caplog.at_level(logging.INFO, logger="GEO-Dashboard"):
        response = client.post(
            "/api/tv/access/exchange",
            json={"token": secret_token},
            headers={"X-Request-ID": "tv-exchange-fp-001"},
        )
    assert response.status_code == 403
    assert secret_token not in response.text
    assert secret_token not in caplog.text
    assert dashboard_api._tv_fingerprint(secret_token) in caplog.text


def test_exchange_non_admin_gets_admin_required(monkeypatch):
    import api.dashboard_api as dashboard_api

    non_admin_client = TestClient(
        _app(monkeypatch, user={"user_id": 103, "username": "qa-user", "is_admin": False}),
        base_url="https://testserver",
    )
    code = dashboard_api._mint_tv_exchange_code(101)
    denied = non_admin_client.post("/api/tv/access/exchange", json={"token": code})
    assert denied.status_code == 403
    assert denied.json()["code"] == "TV_ADMIN_REQUIRED"


# ========== WORKERS>1 · Redis 共享安全状态 ==========

def test_multiworker_exchange_code_is_shared_atomic_and_never_stores_plaintext(monkeypatch):
    import api.dashboard_api as dashboard_api

    _app(monkeypatch)
    monkeypatch.setenv("WORKERS", "4")
    shared = _FakeSharedRedis()
    monkeypatch.setattr(dashboard_api, "_tv_shared_state_client", lambda: shared)

    code = dashboard_api._mint_tv_exchange_code(101)
    stored_snapshot = repr(shared._values)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(dashboard_api._consume_tv_exchange_code, [code] * 8))

    assert outcomes.count("ok") == 1
    assert outcomes.count("unknown") == 7
    assert code not in stored_snapshot
    assert code not in repr(shared._values)
    assert all(code not in key for key in shared._values)


def test_multiworker_password_failures_share_one_atomic_limit(monkeypatch):
    import api.dashboard_api as dashboard_api

    app = _app(monkeypatch)
    monkeypatch.setenv("WORKERS", "4")
    shared = _FakeSharedRedis()
    monkeypatch.setattr(dashboard_api, "_tv_shared_state_client", lambda: shared)
    client = TestClient(app, base_url="https://testserver")

    for attempt in range(10):
        denied = client.post("/api/tv/access", json={"password": "wrong-password"})
        assert denied.status_code == 403, attempt
    limited = client.post(
        "/api/tv/access",
        json={"password": TEST_PASSWORD},
        headers={"X-Request-ID": "tv-shared-limit-001"},
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == "TV_ACCESS_RATE_LIMITED"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60


def test_multiworker_redis_unavailable_fails_closed_for_all_stateful_entrypoints(monkeypatch):
    import api.dashboard_api as dashboard_api
    import cache.redis_client as redis_client

    app = _app(monkeypatch)
    monkeypatch.setenv("WORKERS", "4")
    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    client = TestClient(app, base_url="https://testserver")

    password = client.post(
        "/api/tv/access",
        json={"password": TEST_PASSWORD},
        headers={"X-Request-ID": "tv-shared-down-password-001"},
    )
    exchange = client.post(
        "/api/tv/access/exchange",
        json={"token": "unknown-shared-token"},
        headers={"X-Request-ID": "tv-shared-down-exchange-001"},
    )
    mint = client.post(
        "/api/admin/tv-token",
        headers={"X-Request-ID": "tv-shared-down-mint-001"},
    )

    for response in (password, exchange, mint):
        assert response.status_code == 503
        assert response.json()["code"] == "TV_ACCESS_UNAVAILABLE"
        assert "traceback" not in response.text.lower()


# ========== D4 · legacy URL token 只读兼容 ==========

def test_legacy_url_token_serves_data_readonly_with_deprecation_header(monkeypatch):
    client = TestClient(_app(monkeypatch, legacy_token=LEGACY_TOKEN), base_url="https://testserver")

    ok = client.get("/api/tv/dashboard", params={"token": LEGACY_TOKEN})
    assert ok.status_code == 200
    assert ok.headers.get("X-TV-Legacy-Token") == "deprecated"
    assert ok.json()["funnel"]["total_users"] == 7
    assert LEGACY_TOKEN not in ok.text

    denied = client.get("/api/tv/dashboard", params={"token": "wrong-token"})
    assert denied.status_code == 403
    assert denied.headers.get("X-TV-Legacy-Token") == "deprecated"

    missing = client.get("/api/tv/dashboard")
    assert missing.status_code == 403


def test_legacy_url_token_fail_closed_when_unconfigured(monkeypatch):
    """system_config 未配置 legacy token 时 403 fail-closed（无硬编码默认 token 回落）。"""
    client = TestClient(_app(monkeypatch, legacy_token=None), base_url="https://testserver")
    response = client.get("/api/tv/dashboard", params={"token": LEGACY_TOKEN})
    assert response.status_code == 403
    assert "未配置" in response.text


def test_legacy_token_exchanges_to_session(monkeypatch):
    """D4 兼容：已存在的 legacy token 可一次性交换为 HttpOnly 会话（全程只读，不改写 system_config）。"""
    client = TestClient(_app(monkeypatch, legacy_token=LEGACY_TOKEN), base_url="https://testserver")

    exchanged = client.post(
        "/api/tv/access/exchange",
        json={"token": LEGACY_TOKEN},
        headers={"X-Request-ID": "tv-exchange-legacy-001"},
    )
    assert exchanged.status_code == 200
    assert exchanged.json()["code"] == "TV_ACCESS_GRANTED"
    assert "omnirank_tv_session=" in exchanged.headers["set-cookie"]

    dashboard = client.get("/api/tv/dashboard/auth")
    assert dashboard.status_code == 200
    assert dashboard.json()["code"] == "TV_DASHBOARD_READY"


def test_legacy_flag_disabled_fails_closed_but_exchange_codes_still_work(monkeypatch):
    import api.dashboard_api as dashboard_api

    monkeypatch.setenv("TV_LEGACY_TOKEN_ENABLED", "false")
    client = TestClient(_app(monkeypatch, legacy_token=LEGACY_TOKEN), base_url="https://testserver")

    legacy_get = client.get("/api/tv/dashboard", params={"token": LEGACY_TOKEN})
    assert legacy_get.status_code == 403

    legacy_exchange = client.post("/api/tv/access/exchange", json={"token": LEGACY_TOKEN})
    assert legacy_exchange.status_code == 403
    assert legacy_exchange.json()["code"] == "TV_ACCESS_DENIED"

    # 一次性交换码链路不受 legacy flag 影响
    code = dashboard_api._mint_tv_exchange_code(101)
    exchanged = client.post("/api/tv/access/exchange", json={"token": code})
    assert exchanged.status_code == 200
