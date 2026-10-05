import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auth.jwt_utils import decode_jwt
from services import legal_agreements
from services.registration_agreement_supplement import (
    AGREEMENT_SESSION_TTL_SECONDS,
    AgreementSessionError,
    create_agreement_session,
    decode_agreement_session,
)


ROOT = Path(__file__).resolve().parents[2]


def test_supplement_token_is_not_a_business_jwt_and_is_tamper_evident():
    issued = create_agreement_session(user_id=81, auth_method="password", now=1000)
    token = issued["agreement_session_token"]
    assert decode_jwt(token) is None
    assert decode_agreement_session(token, now=1001)["user_id"] == 81

    prefix, payload, signature = token.split(":", 2)
    tampered = f"{prefix}:{payload[:-1]}A:{signature}"
    with pytest.raises(AgreementSessionError, match="无效"):
        decode_agreement_session(tampered, now=1001)
    with pytest.raises(AgreementSessionError, match="无效"):
        decode_agreement_session("eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoxfQ.fake", now=1001)


def test_supplement_token_expires_and_is_bound_to_current_document_versions(monkeypatch):
    issued = create_agreement_session(user_id=82, auth_method="sms", now=2000)
    token = issued["agreement_session_token"]
    with pytest.raises(AgreementSessionError, match="超时"):
        decode_agreement_session(token, now=2000 + AGREEMENT_SESSION_TTL_SECONDS)

    monkeypatch.setattr(legal_agreements, "USER_TERMS_VERSION", "user-v-next")
    with pytest.raises(AgreementSessionError, match="版本已更新"):
        decode_agreement_session(token, now=2001)


class _SignatureCursor:
    def __init__(self, *, active=True):
        self.active = active
        self.signatures = {}
        self.fetchone_value = None
        self.fetchall_value = []
        self.phone_verified = False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT id,is_active FROM users"):
            self.fetchone_value = {"id": int(params[0]), "is_active": self.active}
            return
        if normalized.startswith("INSERT INTO agreement_signatures"):
            key = (int(params[0]), str(params[1]), str(params[2]))
            existing = self.signatures.get(key)
            if existing is None or existing["content_hash"] != params[3]:
                self.signatures[key] = {
                    "content_hash": params[3],
                    "ip_address": params[4],
                    "user_agent": params[5],
                    "evidence_jsonb": params[6],
                }
            return
        if normalized.startswith("SELECT agreement_type,agreement_version,content_hash"):
            user_id = int(params[0])
            self.fetchall_value = [
                {"agreement_type": key[1], "agreement_version": key[2], "content_hash": value["content_hash"]}
                for key, value in self.signatures.items()
                if key[0] == user_id
                and ((key[1] == "user_terms" and key[2] == params[1] and value["content_hash"] == params[2])
                     or (key[1] == "privacy" and key[2] == params[3] and value["content_hash"] == params[4]))
            ]
            return
        if normalized.startswith("UPDATE users SET phone_verified=TRUE"):
            self.phone_verified = True
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self.fetchone_value

    def fetchall(self):
        return self.fetchall_value


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _request():
    return SimpleNamespace(
        headers={"X-Real-IP": "203.0.113.17", "User-Agent": "agreement-test"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def test_acceptance_writes_both_documents_idempotently_and_only_then_returns_jwt(monkeypatch):
    from api import auth_api
    import auth.jwt_utils
    import db.connection

    cursor = _SignatureCursor(active=True)
    conn = _Connection(cursor)
    monkeypatch.setattr(db.connection, "get_db", lambda: nullcontext(conn))
    monkeypatch.setattr(auth.jwt_utils, "create_jwt", lambda user_id: f"full-jwt-{user_id}")
    monkeypatch.setattr(auth_api, "get_user", lambda user_id: {"id": user_id, "must_change_password": 0})
    monkeypatch.setattr(auth_api, "update_last_login", lambda user_id: None)

    issued = create_agreement_session(user_id=83, auth_method="sms")
    request = auth_api.AgreementSupplementAcceptanceRequest(
        agreement_session_token=issued["agreement_session_token"],
        terms_accepted=True,
        privacy_accepted=True,
        terms_version=legal_agreements.USER_TERMS_VERSION,
        privacy_version=legal_agreements.PRIVACY_VERSION,
    )
    first = asyncio.run(auth_api.accept_registration_agreements(request, _request()))
    second = asyncio.run(auth_api.accept_registration_agreements(request, _request()))

    assert first == second == {
        "success": True,
        "token": "full-jwt-83",
        "must_change_password": False,
    }
    assert len(cursor.signatures) == 2
    assert cursor.phone_verified is True
    assert conn.commits == 2
    assert conn.rollbacks == 0


def test_acceptance_requires_both_explicit_checkbox_values():
    from api import auth_api

    issued = create_agreement_session(user_id=84, auth_method="password")
    request = auth_api.AgreementSupplementAcceptanceRequest(
        agreement_session_token=issued["agreement_session_token"],
        terms_accepted=True,
        privacy_accepted=False,
        terms_version=legal_agreements.USER_TERMS_VERSION,
        privacy_version=legal_agreements.PRIVACY_VERSION,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_api.accept_registration_agreements(request, _request()))
    assert exc.value.status_code == 400


def test_login_gate_returns_no_store_limited_session_without_full_jwt(monkeypatch):
    from api import auth_api
    import auth.jwt_utils
    import db.connection

    conn = _Connection(_SignatureCursor(active=True))
    monkeypatch.setattr(db.connection, "get_db", lambda: nullcontext(conn))
    monkeypatch.setattr(
        legal_agreements,
        "has_current_registration_agreements",
        lambda cur, user_id: False,
    )
    monkeypatch.setattr(
        auth.jwt_utils,
        "create_jwt",
        lambda user_id: pytest.fail("agreement gate must not issue a full JWT"),
    )

    with pytest.raises(HTTPException) as exc:
        auth_api._require_registration_agreements(user_id=840, auth_method="password")
    assert exc.value.status_code == 428
    assert exc.value.headers == {"Cache-Control": "no-store"}
    assert exc.value.detail["code"] == "REGISTRATION_AGREEMENTS_REQUIRED"
    assert exc.value.detail["requires_agreement"] is True
    assert exc.value.detail["agreement_session_token"].startswith("ags1:")
    assert len(exc.value.detail["agreements"]) == 2


def test_login_gate_allows_user_with_both_current_signatures(monkeypatch):
    from api import auth_api
    import db.connection

    conn = _Connection(_SignatureCursor(active=True))
    monkeypatch.setattr(db.connection, "get_db", lambda: nullcontext(conn))
    monkeypatch.setattr(
        legal_agreements,
        "has_current_registration_agreements",
        lambda cur, user_id: True,
    )
    assert auth_api._require_registration_agreements(user_id=841, auth_method="sms") is None


def test_inactive_account_cannot_be_reactivated_by_supplement(monkeypatch):
    from api import auth_api
    import db.connection

    cursor = _SignatureCursor(active=False)
    conn = _Connection(cursor)
    monkeypatch.setattr(db.connection, "get_db", lambda: nullcontext(conn))
    issued = create_agreement_session(user_id=85, auth_method="password")
    request = auth_api.AgreementSupplementAcceptanceRequest(
        agreement_session_token=issued["agreement_session_token"],
        terms_accepted=True,
        privacy_accepted=True,
        terms_version=legal_agreements.USER_TERMS_VERSION,
        privacy_version=legal_agreements.PRIVACY_VERSION,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_api.accept_registration_agreements(request, _request()))
    assert exc.value.status_code == 403
    assert cursor.signatures == {}
    assert conn.rollbacks == 1


def test_password_and_sms_login_share_the_same_agreement_gate():
    source = (ROOT / "api/auth_api.py").read_text(encoding="utf-8")
    password_block = source[source.index('async def login('):source.index('@router.get("/me")')]
    sms_block = source[source.index('async def login_sms('):]
    assert '_require_registration_agreements(' in password_block
    assert 'auth_method="password"' in password_block
    assert password_block.index('_require_registration_agreements(') < password_block.index('create_jwt(')
    assert '_require_registration_agreements(' in sms_block
    assert 'auth_method="sms"' in sms_block
    assert sms_block.index('_require_registration_agreements(') < sms_block.index('create_jwt(')
    assert "create_user" not in sms_block


def test_agreement_acceptance_is_the_only_supplement_endpoint_allowed_without_jwt():
    from auth.middleware import PUBLIC_PATHS, PUBLIC_PREFIXES

    acceptance_path = "/api/auth/registration-agreements/accept"
    assert acceptance_path in PUBLIC_PATHS
    assert "/api/auth/registration-agreements" not in PUBLIC_PREFIXES
    assert "/api/auth/registration-agreements/status" not in PUBLIC_PATHS


def test_acceptance_endpoint_cannot_mutate_business_identity_or_money_tables():
    source = (ROOT / "api/auth_api.py").read_text(encoding="utf-8")
    block = source[
        source.index('async def accept_registration_agreements('):
        source.index('# [GEO-R1-CAN-022]', source.index('async def accept_registration_agreements('))
    ]
    for forbidden in (
        "user_wallets",
        "customer_agent_bindings",
        "user_roles",
        "user_clients",
        "recharge_orders",
        "agent_inventory",
    ):
        assert forbidden not in block


def test_frontend_uses_session_storage_and_public_full_page_route():
    storage = (ROOT / "frontend/src/lib/agreementSupplement.ts").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/pages/Login/AgreementUpdatePage.tsx").read_text(encoding="utf-8")
    login = (ROOT / "frontend/src/pages/Login/LoginPage.tsx").read_text(encoding="utf-8")
    app = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    assert "sessionStorage" in storage
    assert "localStorage" not in storage
    assert 'path="/agreement-update"' in app
    assert "分别阅读并勾选两份协议" in page
    assert "不会改变账号、余额、客户、品牌或历史数据" in page
    assert "推荐关系" not in page
    assert "服务关系" not in page
    assert "服务商身份" not in page
    assert "updateToken(data.token)" in page
    assert page.index("updateToken(data.token)") < page.index("determineTargetRoute(session.return_to)")
    assert "pendingAgreementReturnTo()" in login
