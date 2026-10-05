from contextlib import contextmanager
from types import SimpleNamespace

from services import portal_token_authority


class _Cursor:
    def __init__(self, row):
        self._row = row

    def execute(self, _query, _params):
        return None

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)


def _request(*, user_id: int, is_admin: bool, request_id: str):
    return SimpleNamespace(
        state=SimpleNamespace(
            user={"user_id": user_id, "username": f"user-{user_id}", "is_admin": is_admin},
            request_id=request_id,
            organization_identity=None,
        ),
        headers={"X-Forwarded-For": "203.0.113.8"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def test_platform_admin_can_manage_cross_tenant_portal_with_audit(monkeypatch):
    row = {
        "quote_id": 366,
        "brand_id": 592,
        "brand_name": "测试客户",
        "owner_user_id": 28,
    }

    @contextmanager
    def fake_get_db():
        yield _Connection(row)

    audits = []
    monkeypatch.setattr(portal_token_authority, "get_db", fake_get_db)

    import services.admin_cross_tenant_governance as governance

    monkeypatch.setattr(
        governance,
        "record_admin_read",
        lambda **event: audits.append(event),
    )

    result = portal_token_authority.require_portal_token_authority(
        _request(user_id=102, is_admin=True, request_id="portal-p0-admin"),
        quote_id=366,
        action="portal.token.generate",
    )

    assert result["quote_id"] == 366
    assert audits == [{
        "actor_user_id": 102,
        "actor_username": "user-102",
        "action": "portal.token.generate",
        "subject_kind": "customer_portal_credential",
        "subject_id": 366,
        "request_id": "portal-p0-admin",
        "reason": "平台管理员处理客户门户凭证",
        "before": {"quote_id": 366, "brand_id": 592, "owner_user_id": 28},
        "after": {"authority": "platform_admin_governance"},
        "ip_address": "203.0.113.8",
    }]


def test_owner_path_does_not_create_admin_governance_audit(monkeypatch):
    row = {
        "quote_id": 366,
        "brand_id": 592,
        "brand_name": "测试客户",
        "owner_user_id": 28,
    }

    @contextmanager
    def fake_get_db():
        yield _Connection(row)

    monkeypatch.setattr(portal_token_authority, "get_db", fake_get_db)

    import services.admin_cross_tenant_governance as governance

    monkeypatch.setattr(
        governance,
        "record_admin_read",
        lambda **_event: (_ for _ in ()).throw(AssertionError("owner must not use admin audit")),
    )

    result = portal_token_authority.require_portal_token_authority(
        _request(user_id=28, is_admin=False, request_id="portal-p0-owner"),
        quote_id=366,
        action="portal.token.generate",
    )

    assert result["brand_id"] == 592
