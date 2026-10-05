from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def test_public_portal_pseudo_principal_bypasses_only_organization_resolution(monkeypatch):
    import middleware.organization_guard as guard

    monkeypatch.setattr(guard, "feature_flags", lambda: {"ORGANIZATION_SEATS_ENABLED": True})
    monkeypatch.setattr(
        guard,
        "resolve_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("portal must not resolve as a user")),
    )
    app = FastAPI()
    app.add_middleware(guard.OrganizationGuardMiddleware)

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.user = {"id": "portal_366", "portal": True}
        return await call_next(request)

    @app.get("/api/portal/reports")
    async def portal_report():
        return {"success": True}

    response = TestClient(app).get("/api/portal/reports")
    assert response.status_code == 200
    assert response.json() == {"success": True}


def test_nonnumeric_nonportal_principal_still_fails_closed(monkeypatch):
    import middleware.organization_guard as guard

    monkeypatch.setattr(guard, "feature_flags", lambda: {"ORGANIZATION_SEATS_ENABLED": True})
    app = FastAPI()
    app.add_middleware(guard.OrganizationGuardMiddleware)

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        request.state.user = {"id": "malformed-principal", "portal": False}
        return await call_next(request)

    @app.get("/api/private")
    async def private_route():
        return {"success": True}

    response = TestClient(app).get("/api/private")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ORG_GUARD_FAILED"


def test_scheduler_status_accepts_tentative_jobs_without_next_run_time(monkeypatch):
    import api.scheduler as scheduler_api

    tentative_job = SimpleNamespace(
        id="keyword_subscription_daily_monitoring",
        name="监测任务",
        trigger="cron[hour='1']",
    )
    scheduler = SimpleNamespace(running=False, get_jobs=lambda: [tentative_job])
    monkeypatch.setattr(scheduler_api, "get_scheduler", lambda: scheduler)

    status = scheduler_api.get_status()
    assert status["monitoring_enabled"] is True
    assert status["jobs"][0]["next_run"] is None


def test_quote_list_imports_the_filter_it_calls():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    quote_list = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "api_list_quotes"
    )
    imports = {
        alias.name
        for node in ast.walk(quote_list)
        if isinstance(node, ast.ImportFrom) and node.module == "auth.brand_access"
        for alias in node.names
    }
    calls = {
        node.func.id
        for node in ast.walk(quote_list)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "filter_organization_artifact_rows" in calls
    assert "filter_organization_artifact_rows" in imports


def test_customer_only_whitelabel_never_brands_authenticated_backoffice(monkeypatch):
    from api.referral_api import _serialize_agent_whitelabel_config

    monkeypatch.setenv("WHITELABEL_BACKOFFICE_BRAND_ENABLED", "true")
    customer_only = _serialize_agent_whitelabel_config({
        "company_name": "客户自有品牌",
        "logo_url": "/uploads/whitelabel-logos/opaque/logo.png",
        "whitelabel_mode": "external_only",
        "whitelabel_status": "active",
        "unlocked_by_admin": True,
        "backoffice_brand_unlocked": False,
    })
    assert customer_only["configuration_status"] == "approved"
    assert customer_only["customer_branding_active"] is True
    assert customer_only["backoffice_branding_active"] is False
    assert customer_only["display_scope"] == "platform"

    full_oem = _serialize_agent_whitelabel_config({
        "company_name": "服务商后台品牌",
        "logo_url": "/uploads/whitelabel-logos/opaque/logo.png",
        "whitelabel_mode": "oem",
        "whitelabel_status": "active",
        "unlocked_by_admin": True,
        "backoffice_brand_unlocked": True,
    })
    assert full_oem["customer_branding_active"] is True
    assert full_oem["backoffice_branding_active"] is True
    assert full_oem["display_scope"] == "approved_whitelabel"
