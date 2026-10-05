from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_app(*, is_admin: bool = True, logged_in: bool = True) -> FastAPI:
    app = FastAPI()

    class _InjectUserMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if logged_in:
                request.state.user = {
                    "id": 88,
                    "username": "admin_test" if is_admin else "operator_test",
                    "is_admin": is_admin,
                }
            return await call_next(request)

    app.add_middleware(_InjectUserMW)
    from api.writing_style_control_api import router

    app.include_router(router)
    return app


def _client(*, is_admin: bool = True, logged_in: bool = True) -> TestClient:
    return TestClient(_build_app(is_admin=is_admin, logged_in=logged_in))


def _temp_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WRITING_STYLE_CONTROL_FILE", str(tmp_path / "writing_style_versions.json"))
    monkeypatch.setenv("WRITING_STYLE_AUDIT_LOG_FILE", str(tmp_path / "writing_style_audit_log.jsonl"))
    monkeypatch.setenv("WRITING_STYLE_FLYWHEEL_SOURCE_ROOT", str(tmp_path / "flywheel"))
    import writing.style_registry as style_registry

    monkeypatch.setattr(style_registry, "WRITING_CONFIG_FILE", str(tmp_path / "writing_config.json"))


def test_style_control_versions_requires_admin(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    assert _client(logged_in=False).get("/api/writing/style-control/versions").status_code == 401
    assert _client(is_admin=False).get("/api/writing/style-control/versions").status_code == 403


def test_style_control_lists_versions_with_safe_projection(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    response = _client().get("/api/writing/style-control/versions")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["config_version"] >= 1
    assert payload["data"]["versions"]
    row = payload["data"]["versions"][0]
    assert "version_id" in row
    assert "style_code" in row
    assert "prompt_text" not in row
    assert "prompt_sha256" in row


def test_style_control_align_draft_never_activates(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    config_version = _client().get("/api/writing/style-control/versions").json()["data"]["config_version"]

    response = _client().post(
        "/api/writing/style-control/align-draft",
        json={
            "style_code": "buying_guide",
            "industry_key": "education",
            "evidence_mode": "with_evidence",
            "expected_config_version": config_version,
            "source_summary": {
                "sample_count": 128,
                "control_sample_count": 44,
                "shadow_pass_rate": 0.98,
                "semantic_pass_rate": 0.93,
            },
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    version = payload["data"]["version"]
    state = payload["data"]["state"]
    assert version["status"] == "draft"
    assert version["source"] == "flywheel_align"
    assert state["active_by_style"]["buying_guide"].startswith("baseline_buying_guide")
    assert version["version_id"] not in state["active_by_style"].values()
    assert payload["data"]["customer_output_allowed"] is False


def test_style_control_conflict_returns_409(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    response = _client().post(
        "/api/writing/style-control/align-draft",
        json={
            "style_code": "buying_guide",
            "industry_key": "education",
            "evidence_mode": "with_evidence",
            "expected_config_version": 999,
            "source_summary": {"sample_count": 128, "control_sample_count": 44},
        },
    )

    assert response.status_code == 409, response.text
    assert "config_version_conflict" in response.text


def test_style_control_write_actions_require_config_version(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    client = _client()
    config_version = client.get("/api/writing/style-control/versions").json()["data"]["config_version"]
    draft = client.post(
        "/api/writing/style-control/align-draft",
        json={
            "style_code": "buying_guide",
            "industry_key": "manufacturing",
            "evidence_mode": "with_evidence",
            "expected_config_version": config_version,
            "source_summary": {"sample_count": 140, "control_sample_count": 35},
        },
    ).json()["data"]

    assert client.post(
        "/api/writing/style-control/align-draft",
        json={
            "style_code": "buying_guide",
            "industry_key": "manufacturing",
            "evidence_mode": "with_evidence",
            "source_summary": {"sample_count": 140, "control_sample_count": 35},
        },
    ).status_code == 422
    assert client.post(
        "/api/writing/style-control/activate",
        json={"version_id": draft["version"]["version_id"], "note": "missing version"},
    ).status_code == 422
    assert client.post(
        "/api/writing/style-control/rollback",
        json={"style_code": "buying_guide", "note": "missing version"},
    ).status_code == 422
    assert client.post(
        "/api/writing/style-control/retire",
        json={"version_id": draft["version"]["version_id"], "note": "missing version"},
    ).status_code == 422


def test_style_control_activate_and_rollback(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    import services.article_data_health as data_health
    import services.article_experiment_registry as experiments

    monkeypatch.setattr(
        data_health,
        "get_article_data_health",
        lambda: {"version": "test-health-v1", "state": "ready"},
    )
    monkeypatch.setattr(
        experiments,
        "can_activate_candidate",
        lambda **_kwargs: {
            "allowed": True,
            "reason": "PASS",
            "experiment": {"experiment_id": 9001, "decision": "PASS"},
        },
    )
    client = _client()
    config_version = client.get("/api/writing/style-control/versions").json()["data"]["config_version"]
    draft_response = client.post(
        "/api/writing/style-control/align-draft",
        json={
            "style_code": "buying_guide",
            "industry_key": "manufacturing",
            "evidence_mode": "with_evidence",
            "expected_config_version": config_version,
            "source_summary": {"sample_count": 140, "control_sample_count": 35},
        },
    )
    draft = draft_response.json()["data"]
    version_id = draft["version"]["version_id"]

    activate = client.post(
        "/api/writing/style-control/activate",
        json={
            "version_id": version_id,
            "expected_config_version": draft["state"]["config_version"],
            "note": "activate",
            "confirm": "ACTIVATE_WRITING_STYLE_VERSION",
        },
    )
    assert activate.status_code == 200, activate.text
    active_payload = activate.json()["data"]
    assert active_payload["version"]["status"] == "active"
    assert active_payload["customer_output_allowed"] is False

    rollback = client.post(
        "/api/writing/style-control/rollback",
        json={
            "style_code": "buying_guide",
            "expected_config_version": active_payload["state"]["config_version"],
            "note": "rollback",
        },
    )
    assert rollback.status_code == 200, rollback.text
    rolled = rollback.json()["data"]
    assert rolled["active_version"]["stable_baseline"] is True
    assert rolled["customer_output_allowed"] is False

    response_text = json.dumps({"activate": active_payload, "rollback": rolled}, ensure_ascii=False)
    assert "prompt_text" not in response_text
    assert "owner_user_id" not in response_text
    assert "margin" not in response_text


def test_style_control_activate_requires_explicit_confirmation(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    client = _client()
    config_version = client.get("/api/writing/style-control/versions").json()["data"]["config_version"]
    draft = client.post(
        "/api/writing/style-control/align-draft",
        json={
            "style_code": "buying_guide",
            "industry_key": "manufacturing",
            "evidence_mode": "with_evidence",
            "expected_config_version": config_version,
            "source_summary": {"sample_count": 140, "control_sample_count": 35},
        },
    ).json()["data"]

    response = client.post(
        "/api/writing/style-control/activate",
        json={
            "version_id": draft["version"]["version_id"],
            "expected_config_version": draft["state"]["config_version"],
            "note": "missing confirmation",
        },
    )

    assert response.status_code == 409
    assert "activate_confirmation_required" in response.text


def test_style_control_evidence_chain_and_audit_are_safe(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)
    client = _client()
    config_version = client.get("/api/writing/style-control/versions").json()["data"]["config_version"]
    draft = client.post(
        "/api/writing/style-control/align-draft",
        json={
            "style_code": "risk_compliance",
            "industry_key": "legal",
            "evidence_mode": "with_evidence",
            "expected_config_version": config_version,
            "source_summary": {"sample_count": 120, "control_sample_count": 25},
        },
    ).json()["data"]
    version_id = draft["version"]["version_id"]

    chain = client.get(f"/api/writing/style-control/evidence-chain/{version_id}")
    audit = client.get("/api/writing/style-control/audit-log")

    assert chain.status_code == 200, chain.text
    assert audit.status_code == 200, audit.text
    response_text = json.dumps({"chain": chain.json(), "audit": audit.json()}, ensure_ascii=False)
    assert "prompt_text" not in response_text
    assert "owner_user_id" not in response_text
    assert "margin" not in response_text


def test_style_control_flywheel_summary_exposes_thresholds(monkeypatch, tmp_path):
    _temp_paths(monkeypatch, tmp_path)

    response = _client().get("/api/writing/style-control/flywheel-summary?industry_key=education&style_code=buying_guide")

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["industry_key"] == "education"
    assert payload["style_code"] == "buying_guide"
    assert "eligibility" in payload
    assert "eligibility_reason" in payload
    assert payload["sample_count_status"] == "unknown"
