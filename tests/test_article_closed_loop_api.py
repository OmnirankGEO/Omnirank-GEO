from types import SimpleNamespace

from fastapi import HTTPException
import pytest

import api.article_closed_loop_api as api


def _request(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


def test_project_summary_preserves_tenant_404_boundary(monkeypatch):
    import auth.brand_access as access

    def denied(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(access, "require_quote_access", denied)
    with pytest.raises(HTTPException) as exc:
        api.project_summary(42, _request({"id": 7}))
    assert exc.value.status_code == 404
    assert exc.value.detail == "项目不存在"


def test_project_summary_is_additive_and_calls_existing_access_guard(monkeypatch):
    import auth.brand_access as access

    calls = []
    monkeypatch.setattr(access, "require_quote_access", lambda request, quote_id, allow_null: calls.append((quote_id, allow_null)))
    monkeypatch.setattr(api, "get_project_summary", lambda quote_id: {"available": True, "quote_id": quote_id})
    result = api.project_summary(9, _request({"id": 7}))
    assert result == {"available": True, "quote_id": 9}
    assert calls == [(9, False)]


def test_admin_endpoints_are_hidden_from_non_admins():
    with pytest.raises(HTTPException) as state_exc:
        api.admin_state(_request({"id": 7, "is_admin": False}))
    assert state_exc.value.status_code == 404
    with pytest.raises(HTTPException) as health_exc:
        api.admin_data_health(_request({"id": 7, "is_admin": False}))
    assert health_exc.value.status_code == 404


def test_admin_diff_endpoint_is_hidden_until_its_own_flag_is_enabled(monkeypatch):
    monkeypatch.delenv("ARTICLE_PLAN_ADMIN_DIFF_UI_ENABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        api.admin_state(_request({"id": 7, "is_admin": True}))
    assert exc.value.status_code == 404


def test_admin_mutation_requires_a_stable_actor_before_db_access(monkeypatch):
    import db.diagnosis_db as diagnosis

    monkeypatch.setattr(diagnosis, "get_connection", lambda: (_ for _ in ()).throw(AssertionError("db must not open")))
    body = api.ResolveSlotRequest(resolution_evidence="已核验")
    with pytest.raises(HTTPException) as exc:
        api.admin_unblock_slot("00000000-0000-0000-0000-000000000001", body, _request({"is_admin": True}))
    assert exc.value.status_code == 409
