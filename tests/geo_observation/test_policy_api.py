"""Admin governance API:RBAC 403、CAS 409、env 覆盖 423、extra=forbid 422、readiness。"""
from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.geo_observation_admin_api import router


def make_client(is_admin=True, logged_in=True):
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = ({"is_admin": is_admin, "username": "admin1"} if logged_in else None)
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def test_non_admin_403():
    c = make_client(is_admin=False)
    assert c.get("/api/admin/geo-observation/policy").status_code == 403
    assert c.get("/api/admin/geo-observation/events").status_code == 403


def test_not_logged_in_401():
    c = make_client(logged_in=False)
    assert c.get("/api/admin/geo-observation/policy").status_code == 401


def test_readiness_unavailable_before_reconciler_is_healthy():
    c = make_client()
    r = c.get("/api/admin/geo-observation/readiness")
    assert r.status_code == 200 and r.json()["ready"] is False
    collection = r.json()["collection_readiness"]
    assert collection["mode"] == "existing_collectors_reconciled"
    assert collection["status"] == "unavailable"


def test_readiness_ready_only_with_complete_collection_contract(monkeypatch):
    from services.geo_observation import integration

    monkeypatch.setattr(integration, "collection_readiness_provider", lambda: {
        "mode": "existing_collectors_reconciled",
        "ready": True,
        "status": "ready",
        "problems": [],
        "reconciler_last_success_at": "2026-07-20T00:00:00+00:00",
        "reconciler_backlog": 0,
        "source_watermarks": {
            "paid_diagnosis": "2026-07-20T00:00:00+00:00",
            "recurring_monitoring": "2026-07-20T00:00:00+00:00",
            "research_round": "2026-07-20T00:00:00+00:00",
        },
        "duplicate_collection_jobs": [],
        "policy_version": 1,
    })
    r = make_client().get("/api/admin/geo-observation/readiness")
    assert r.status_code == 200 and r.json()["ready"] is True


def test_policy_get_and_cas_put():
    c = make_client()
    cur = c.get("/api/admin/geo-observation/policy").json()
    assert cur["policy_version"] == 1
    new_policy = copy.deepcopy(cur["policy"])
    new_policy["policy_version"] = "v2"
    body = {"expected_policy_version": 1, "reason": "启用采集", "request_id": "rq-1", "policy": new_policy}
    r = c.put("/api/admin/geo-observation/policy", json=body)
    assert r.status_code == 200 and r.json()["policy_version"] == 2
    # 陈旧版本 → 409
    r2 = c.put("/api/admin/geo-observation/policy", json=body)
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "POLICY_VERSION_CONFLICT"


def test_policy_put_env_override_423(monkeypatch):
    c = make_client()
    cur = c.get("/api/admin/geo-observation/policy").json()
    monkeypatch.setenv("GEO_OBSERVATION_PROMOTION_ENABLED", "false")
    new_policy = copy.deepcopy(cur["policy"])
    new_policy["feature_flags"]["promotion_enabled"] = True   # 与 env 不一致
    body = {"expected_policy_version": cur["policy_version"], "reason": "试图开晋升", "request_id": "rq-2", "policy": new_policy}
    r = c.put("/api/admin/geo-observation/policy", json=body)
    assert r.status_code == 423 and r.json()["detail"]["code"] == "POLICY_ENV_OVERRIDE"


def test_policy_put_extra_forbid_422():
    c = make_client()
    cur = c.get("/api/admin/geo-observation/policy").json()
    body = {"expected_policy_version": 1, "reason": "x1", "request_id": "rq-3", "policy": cur["policy"], "evil": "x"}
    assert c.put("/api/admin/geo-observation/policy", json=body).status_code == 422


def test_policy_put_bad_weights_422():
    c = make_client()
    cur = c.get("/api/admin/geo-observation/policy").json()
    bad = copy.deepcopy(cur["policy"])
    bad["platforms"][0]["base_weight_bps"] = 9999   # 破坏合计=10000
    body = {"expected_policy_version": 1, "reason": "bad weights", "request_id": "rq-4", "policy": bad}
    assert c.put("/api/admin/geo-observation/policy", json=body).status_code == 422
