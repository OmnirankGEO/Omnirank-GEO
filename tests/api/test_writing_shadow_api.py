"""
R6-H shadow-only artifact listing API tests.

The endpoint is admin-only and must expose only the safe listing projection,
never evidence sidecar content or local sidecar paths.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _build_app(is_admin: bool = True, logged_in: bool = True) -> FastAPI:
    app = FastAPI()

    class _InjectUserMW(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if logged_in:
                request.state.user = {
                    "id": 42,
                    "username": "admin_test" if is_admin else "user_test",
                    "is_admin": is_admin,
                }
            return await call_next(request)

    app.add_middleware(_InjectUserMW)
    from api.writing_shadow_api import router

    app.include_router(router)
    return app


def _client(is_admin: bool = True, logged_in: bool = True) -> TestClient:
    return TestClient(_build_app(is_admin=is_admin, logged_in=logged_in))


def _write_shadow_artifact(root: Path) -> None:
    from writing.shadow_only_injection import create_shadow_run_artifacts, load_shadow_injection_config

    article = "岱林生物拥有大量专利。"
    create_shadow_run_artifacts(
        case={
            "case_id": "API-LIST-1",
            "evidence_mode": "with_evidence",
            "evidence_packet_source": "trusted_research_storage",
            "risk_level": "high",
            "customer_article_mode": "stripped_safe_binding",
            "customer_article_sha256": hashlib.sha256(article.encode("utf-8")).hexdigest(),
            "evidence_packet": [{"evidence_id": "E1", "text": "岱林生物拥有大量专利。"}],
            "evidence_bindings": [
                {
                    "evidence_id": "E1",
                    "claim_text": "岱林生物拥有大量专利",
                    "claim_span": {"start": 0, "end": 10},
                }
            ],
        },
        article_text=article,
        guard={"shadow_decision": "pass_with_warning"},
        semantic={
            "semantic_decision": "pass_with_warning",
            "needs_human_review": True,
            "manual_review_reasons": ["soft_fabrication_warn"],
            "provider_count": 3,
            "required_provider_count": 3,
            "warn_count": 1,
            "issue_count": 1,
        },
        route={
            "shadow_injection_route": "manual_review",
            "customer_output_allowed": False,
            "manual_review_required": True,
            "manual_review_reasons": ["high_liability_manual_review", "soft_fabrication_warn"],
        },
        conflict={"needs_human_review": True, "conflict_count": 1},
        metric={
            "decision": "manual_review",
            "metric_retention_rate": 0.5,
            "reasons": ["metric_retention_below_minimum"],
        },
        output_root=root,
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
            }
        ),
        run_id="API_LIST_RUN",
    )


def test_writing_shadow_runs_requires_login(monkeypatch, tmp_path):
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(tmp_path / "admin_shadow_runs"))

    response = _client(logged_in=False).get("/api/writing/shadow-runs")

    assert response.status_code == 401, response.text


def test_writing_shadow_runs_requires_admin(monkeypatch, tmp_path):
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(tmp_path / "admin_shadow_runs"))

    response = _client(is_admin=False).get("/api/writing/shadow-runs")

    assert response.status_code == 403, response.text


def test_writing_shadow_runs_returns_empty_listing_for_missing_root(monkeypatch, tmp_path):
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(tmp_path / "admin_shadow_runs"))

    response = _client().get("/api/writing/shadow-runs")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["artifact_count"] == 0
    assert payload["data"]["runs"] == []


def test_writing_shadow_runs_lists_safe_projection_only(monkeypatch, tmp_path):
    root = tmp_path / "admin_shadow_runs"
    _write_shadow_artifact(root)
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(root))

    response = _client().get("/api/writing/shadow-runs")

    assert response.status_code == 200, response.text
    payload = response.json()
    row = payload["data"]["runs"][0]
    assert payload["success"] is True
    assert row["run_id"] == "API_LIST_RUN"
    assert row["case_id"] == "API-LIST-1"
    assert row["sidecar_present"] is True
    assert row["customer_output_allowed"] is False
    response_text = json.dumps(payload, ensure_ascii=False)
    assert "evidence_bindings" not in response_text
    assert "sidecar_path" not in response_text
    assert "artifact_paths" not in response_text


def test_writing_shadow_runs_exposes_safe_observability_summary(monkeypatch, tmp_path):
    root = tmp_path / "admin_shadow_runs"
    _write_shadow_artifact(root)
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(root))

    response = _client().get("/api/writing/shadow-runs")

    assert response.status_code == 200, response.text
    payload = response.json()
    observability = payload["data"]["observability"]
    assert observability["total_runs"] == 1
    assert observability["manual_review_required_count"] == 1
    assert observability["customer_output_closed_count"] == 1
    assert observability["customer_output_open_count"] == 0
    assert observability["recorder_error_count"] == 0
    assert observability["by_evidence_mode"]["with_evidence"] == 1
    assert observability["by_guard_decision"]["pass_with_warning"] == 1
    assert observability["top_blockers"]
    response_text = json.dumps(payload, ensure_ascii=False)
    assert "evidence_bindings" not in response_text
    assert "sidecar_path" not in response_text
    assert "artifact_paths" not in response_text


def test_writing_shadow_runs_rejects_public_artifact_root(monkeypatch, tmp_path):
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(tmp_path / "public"))

    response = _client().get("/api/writing/shadow-runs")

    assert response.status_code == 400, response.text
    assert "admin-only" in response.text


def test_writing_shadow_run_detail_requires_admin(monkeypatch, tmp_path):
    root = tmp_path / "admin_shadow_runs"
    _write_shadow_artifact(root)
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(root))

    response = _client(is_admin=False).get("/api/writing/shadow-runs/API_LIST_RUN")

    assert response.status_code == 403, response.text


def test_writing_shadow_run_detail_rejects_unsafe_run_id(monkeypatch, tmp_path):
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(tmp_path / "admin_shadow_runs"))

    response = _client().get("/api/writing/shadow-runs/..secret")

    assert response.status_code == 400, response.text


def test_writing_shadow_run_detail_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(tmp_path / "admin_shadow_runs"))

    response = _client().get("/api/writing/shadow-runs/MISSING_RUN")

    assert response.status_code == 404, response.text


def test_writing_shadow_run_detail_returns_safe_review_projection(monkeypatch, tmp_path):
    root = tmp_path / "admin_shadow_runs"
    _write_shadow_artifact(root)
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(root))

    response = _client().get("/api/writing/shadow-runs/API_LIST_RUN")

    assert response.status_code == 200, response.text
    payload = response.json()
    detail = payload["data"]
    assert payload["success"] is True
    assert detail["run_id"] == "API_LIST_RUN"
    assert detail["case_id"] == "API-LIST-1"
    assert detail["customer_output_allowed"] is False
    assert detail["blockers"]
    assert detail["article"]["sha256"]
    assert detail["article"]["sentence_summaries"]
    assert detail["binding_summaries"][0]["evidence_id"] == "E1"
    assert "岱林生物拥有大量专利" in detail["binding_summaries"][0]["claim_excerpt"]
    assert detail["manual_review"]["manual_review_required"] is True
    assert detail["manual_review"]["route"] == "manual_review"
    assert "high_liability_manual_review" in detail["manual_review"]["manual_review_reasons"]
    assert "soft_fabrication_warn" in detail["manual_review"]["manual_review_reasons"]
    assert detail["quality_summary"]["guard_decision"] == "pass_with_warning"
    assert detail["quality_summary"]["semantic_decision"] == "pass_with_warning"
    assert detail["quality_summary"]["provider_count"] == 3
    assert detail["quality_summary"]["metric_decision"] == "manual_review"
    assert detail["quality_summary"]["source_conflict_count"] == 1
    assert detail["source_summary"]["evidence_mode"] == "with_evidence"
    assert detail["source_summary"]["evidence_packet_source"] == "trusted_research_storage"
    assert detail["source_summary"]["trusted_source"] is True
    response_text = json.dumps(payload, ensure_ascii=False)
    assert "evidence_bindings" not in response_text
    assert "sidecar_path" not in response_text
    assert "artifact_paths" not in response_text
    assert str(root) not in response_text


def test_writing_shadow_run_detail_redacts_no_evidence_sentence_summaries(monkeypatch, tmp_path):
    from writing.shadow_only_injection import create_shadow_run_artifacts, load_shadow_injection_config

    root = tmp_path / "admin_shadow_runs"
    sensitive_article = "SENSITIVE_BODY 这是一篇无证据 live shadow 正文。第二句也不应进入详情响应。"
    create_shadow_run_artifacts(
        case={
            "case_id": "LIVE-API-NE-1",
            "evidence_mode": "no_evidence",
            "risk_level": "normal",
        },
        article_text=sensitive_article,
        guard={"shadow_decision": "missing"},
        semantic={"semantic_decision": "missing", "needs_human_review": True},
        route={
            "shadow_injection_route": "manual_review",
            "customer_output_allowed": False,
            "manual_review_required": True,
        },
        output_root=root,
        config=load_shadow_injection_config(
            env={
                "R6H_SHADOW_INJECTION_ENABLED": "true",
                "R6H_SHADOW_ONLY": "true",
            }
        ),
        run_id="LIVE_API_NE_DETAIL",
    )
    monkeypatch.setenv("R6H_SHADOW_ARTIFACT_ROOT", str(root))

    response = _client().get("/api/writing/shadow-runs/LIVE_API_NE_DETAIL")

    assert response.status_code == 200, response.text
    payload = response.json()
    detail = payload["data"]
    assert detail["source_summary"]["evidence_mode"] == "no_evidence"
    assert detail["article"]["sentence_summaries"] == []
    assert detail["article"]["body_preview_redacted"] is True
    assert detail["article"]["body_preview_policy"] == "no_evidence_body_redacted"
    response_text = json.dumps(payload, ensure_ascii=False)
    assert "SENSITIVE_BODY" not in response_text
    assert "这是一篇无证据" not in response_text
