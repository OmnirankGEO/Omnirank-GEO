import asyncio
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
import psycopg2
from PIL import Image

from db import marketing_db
from db.connection import get_connection
from services.marketing import content_center, geo_factory, image_client, material_storage
from services.marketing.quality_assurance import validate_qr_reference, visual_qa


def _assert_same_image_pixels(stored: bytes, original: bytes):
    """Storage re-encodes (EXIF stripped); pixel content must be preserved."""
    stored_img = Image.open(io.BytesIO(stored)).convert("RGB")
    original_img = Image.open(io.BytesIO(original)).convert("RGB")
    assert stored_img.size == original_img.size
    assert list(stored_img.getdata()) == list(original_img.getdata())


def _assert_five_question_flags(flags: list, code: str) -> None:
    """五问合同新结构(SSOT §6):code + message + reason/repair_hint 至少其一
    + rule_version;仅含 code 的旧结构视为回归。"""
    assert len(flags) == 1
    entry = flags[0]
    assert entry["code"] == code
    assert entry.get("message")
    assert entry.get("reason") or entry.get("repair_hint")
    assert entry.get("rule_version")


@pytest.fixture(autouse=True)
def _private_material_root(monkeypatch, tmp_path):
    """Keep private GEO/QR artifacts on a test-only non-static volume."""
    monkeypatch.setenv("MARKETING_PRIVATE_UPLOAD_ROOT", str(tmp_path / "marketing-private"))


@pytest.fixture
def provider_migration_db():
    """A real isolated PostgreSQL database with the pre-migration public table."""
    from psycopg2 import sql
    from psycopg2.extensions import parse_dsn

    params = parse_dsn(os.environ["TEST_DATABASE_URL"])
    params.pop("options", None)
    database_name = f"geo_provider_{uuid4().hex[:20]}"
    admin = psycopg2.connect(**params)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        target_params = {**params, "dbname": database_name}
        target = psycopg2.connect(**target_params)
        target.autocommit = True
        try:
            with target.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE public.marketing_material_generation_attempts (
                      id BIGSERIAL PRIMARY KEY,job_id BIGINT NOT NULL,attempt_no INTEGER NOT NULL,
                      provider TEXT NOT NULL DEFAULT '',provider_task_id TEXT NOT NULL DEFAULT '',
                      status TEXT NOT NULL DEFAULT 'pending',provider_cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
                      safety_status TEXT NOT NULL DEFAULT 'passed',safety_flags_jsonb JSONB NOT NULL DEFAULT '[]'::jsonb,
                      error_detail TEXT NOT NULL DEFAULT '',started_at TIMESTAMP NULL,finished_at TIMESTAMP NULL
                    )
                    """
                )
            yield target
        finally:
            target.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()",
                (database_name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
        admin.close()


def _job(geo: dict, *, user_id: int = 78130):
    request_id = f"review-{uuid4()}"
    snapshot = {**geo, "request_id": request_id, "request_hash": uuid4().hex}
    return marketing_db.create_or_get_material_job(
        user_id=user_id, request_id=request_id, request_hash=snapshot["request_hash"],
        material_kind="bundle", feature_code="mktg_bundle_std", input_fields={"_geo": snapshot},
    )[0]


def _geo(**patch):
    value = {
        "channels": ["professional_poster"],
        "only_components": [],
        "strategy": {"audience": "服务商", "single_action": "领取诊断", "single_value": "真实证据"},
        "teacher": {"teacher_id": "shu", "name": "舒老师", "version": "1.0.0"},
        "evidence": {"source_type": "none", "facts": [], "source_note": "未引用诊断数字"},
        "trend": {"used": False},
        "contact": {"mode": "none", "text": "", "qr_reference": None},
        "brand": {"name": "OmniRank"},
    }
    value.update(patch)
    return value


def _set_status_direct(job_id: int, status: str, error_summary: str = ""):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE marketing_material_jobs SET status=%s,error_summary=%s,finished_at=NOW() WHERE id=%s",
            (status, error_summary, int(job_id)),
        )
        conn.commit()
    finally:
        conn.close()

def _organization_charge(monkeypatch, *, claim_now: bool = True):
    monkeypatch.setenv("ORGANIZATION_SEATS_ENABLED", "true")
    monkeypatch.setenv("ORGANIZATION_SHARED_PAYER_ENABLED", "true")
    user_id = 100000 + (uuid4().int % 800000)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            # 🔴 [#113] 补 password_hash:它在 prod 形状的库里是 NOT NULL,
            #    漏了这一列时本 helper 在那种库上**建不出用户** —— 而失败读数
            #    (NotNullViolation)长得像被测对象坏了,会被当成缺陷的修前红读数。
            "INSERT INTO users(id,username,display_name,phone,email,password_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, f"geo-lease-{user_id}", "GEO 租约测试", f"13{user_id:09d}"[-11:],
             f"geo-{user_id}@example.test", "x"),
        )
        cur.execute("INSERT INTO user_roles(user_id,role_id) SELECT %s,id FROM roles WHERE name='user' LIMIT 1", (user_id,))
        cur.execute(
            "INSERT INTO user_wallets(user_id,paid_points,total_recharged,agent_level) VALUES (%s,2000000,2000000,1)",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()
    from services.organization_service import create_organization, resolve_identity
    from services.organization_billing import claim_live_charge, reserve_charge
    overview = create_organization(
        owner_user_id=user_id, name="GEO Provider Lease Test", request_id=f"org-{uuid4().hex}",
    )
    identity = resolve_identity(user_id, request_id=f"identity-{uuid4().hex}")
    charge = asyncio.run(reserve_charge(
        identity, execution_id=f"charge-{uuid4().hex}", feature_code="mktg_poster_basic",
        work_kind="geo_content_package", payload={"job_id": user_id}, brand_id=None, task_ref=f"geo-{user_id}",
    ))
    claim = claim_live_charge(identity, charge_link_id=int(charge["id"]), lease_seconds=60) if claim_now else None
    return overview, identity, charge, claim


def test_finish_attempt_atomically_persists_provider_and_visual_failure_evidence():
    job = _job(_geo())
    attempt = marketing_db.add_attempt(
        job_id=int(job["id"]), component_id="professional_poster:image:professional_poster",
        attempt_no=1,
    )
    marketing_db.anchor_attempt_before_submit(int(attempt["id"]), "guard-token")
    marketing_db.update_attempt_provider_state(
        int(attempt["id"]), submit_state="submitted", poll_state="polling", provider_task_id="task-123",
    )
    flags = [{"code": "qr_payload_mismatch_or_unreadable"}]
    marketing_db.finish_attempt(
        int(attempt["id"]), status="failed", provider_task_id="task-123",
        submit_state="submitted", poll_state="succeeded", safety_status="blocked_output",
        safety_flags=flags, error_detail="visual qa failed",
    )
    row = marketing_db.list_attempts(int(job["id"]))[0]
    assert row["status"] == "failed"
    assert row["provider_task_id"] == "task-123"
    assert row["submit_state"] == "submitted"
    assert row["poll_state"] == "succeeded"
    assert row["safety_flags_jsonb"] == flags
    assert row["finished_at"] is not None
    with pytest.raises(ValueError, match="already_terminal"):
        marketing_db.finish_attempt(int(attempt["id"]), status="succeeded")


def test_worker_lost_after_submit_anchor_never_posts_again(monkeypatch):
    geo = _geo()
    job = _job(geo)
    marketing_db.add_attempt(
        job_id=int(job["id"]), component_id="professional_poster:image:professional_poster",
        attempt_no=1, status="running", submit_state="anchored", submit_guard_token="before-kill",
    )
    posts = 0

    async def forbidden_submit(*_args, **_kwargs):
        nonlocal posts
        posts += 1
        raise AssertionError("a recovered anchored attempt must not POST")

    monkeypatch.setattr(image_client, "submit_image", forbidden_submit)
    result = asyncio.run(geo_factory._generate_image_component(
        job={**job, "resolution": "1k"},
        component={"component_id": "professional_poster:image:professional_poster", "channel": "professional_poster", "kind": "image", "slot": {"slot": "professional_poster", "size": "3:4"}},
        geo=geo, content={"title": "真实证据", "body": "先诊断再行动"}, owner_key="u78130",
    ))
    assert result == {"_pending": True, "error": "provider_submit_unknown"}
    assert posts == 0
    row = marketing_db.list_attempts(int(job["id"]))[0]
    assert row["submit_state"] == "outcome_unknown"
    assert row["status"] == "running"


def test_pre_migration_running_attempt_is_quarantined_before_any_provider_work(monkeypatch):
    geo = _geo()
    job = _job(geo)
    marketing_db.update_job(int(job["id"]), status="generating")
    marketing_db.add_attempt(
        job_id=int(job["id"]), attempt_no=1, component_id="",
        status="running", submit_state="not_started",
    )
    provider_calls = 0

    async def forbidden(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("legacy unknown attempt must stop before provider work")

    monkeypatch.setattr(geo_factory, "generate_channel_content", forbidden)
    result = asyncio.run(geo_factory.execute_geo_package_job({
        "job_id": int(job["id"]), "billing_kind": "legacy",
        "freeze_id": 1, "payer_user_id": int(job["user_id"]),
    }))
    row = marketing_db.list_attempts(int(job["id"]))[0]
    assert result == {
        "status": "generating", "job_id": int(job["id"]),
        "error": "provider_submit_unknown",
    }
    assert row["submit_state"] == "outcome_unknown"
    assert provider_calls == 0


def test_visual_qa_failure_uses_real_finish_attempt_contract(monkeypatch):
    geo = _geo()
    job = _job(geo)
    calls = 0

    async def submit(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"state": "submitted", "provider_task_id": f"qa-task-{calls}"}

    async def poll(task_id, **_kwargs):
        return {"state": "succeeded", "image_url": f"https://cdn.apimart.ai/{task_id}.png"}

    image = io.BytesIO()
    Image.new("RGB", (768, 1024), "white").save(image, format="PNG")

    async def blocked_qa(*_args, **_kwargs):
        return {"passed": False, "errors": [{"code": "qr_payload_mismatch_or_unreadable"}], "warnings": []}

    monkeypatch.setattr(image_client, "submit_image", submit)
    monkeypatch.setattr(image_client, "poll_image_task", poll)
    monkeypatch.setattr(image_client, "download_image", lambda *_args, **_kwargs: asyncio.sleep(0, result=image.getvalue()))
    monkeypatch.setattr(geo_factory, "visual_qa", blocked_qa)
    result = asyncio.run(geo_factory._generate_image_component(
        job={**job, "resolution": "1k"},
        component={"component_id": "professional_poster:image:professional_poster", "channel": "professional_poster", "kind": "image", "slot": {"slot": "professional_poster", "size": "3:4"}},
        geo=geo, content={"title": "真实证据", "body": "先诊断再行动"}, owner_key="u78130",
    ))
    assert result is None and calls == 3
    rows = marketing_db.list_attempts(int(job["id"]))
    assert len(rows) == 3
    assert all(row["status"] == "failed" and row["finished_at"] is not None for row in rows)
    for row in rows:
        _assert_five_question_flags(row["safety_flags_jsonb"], "qr_payload_mismatch_or_unreadable")


class _ProviderHandler(BaseHTTPRequestHandler):
    posts = 0
    delay = 0.0
    image_bytes = b""

    def log_message(self, *_args):
        return

    def do_POST(self):
        type(self).posts += 1
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        if type(self).delay:
            time.sleep(type(self).delay)
        body = json.dumps({"data": [{"task_id": "paid-task-1"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path.startswith("/tasks/"):
            body = json.dumps({
                "data": {"status": "succeeded", "result": {"images": [{"url": [f"http://127.0.0.1:{self.server.server_port}/image.png"]}]}},
            }).encode()
            content_type = "application/json"
        else:
            body = type(self).image_bytes
            content_type = "image/png"
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_real_submit_read_timeout_is_unknown_and_exactly_one_post(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _ProviderHandler.posts = 0
    _ProviderHandler.delay = 0.15
    monkeypatch.setenv("APIMART_API_KEY", "fake-key")
    monkeypatch.setenv("MARKETING_IMAGE_GENERATION_URL", f"http://127.0.0.1:{server.server_port}/v1/images/generations")
    monkeypatch.setattr(image_client, "_SUBMIT_TIMEOUT_S", 0.03)
    try:
        result = asyncio.run(image_client.submit_image("test"))
    finally:
        server.shutdown()
        server.server_close()
    assert result["state"] == "outcome_unknown"
    assert _ProviderHandler.posts == 1


def test_provider_http_5xx_is_unknown_not_an_auto_retry_signal(monkeypatch):
    class FailureHandler(_ProviderHandler):
        posts = 0
        def do_POST(self):
            type(self).posts += 1
            self.send_response(503)
            self.send_header("content-length", "0")
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FailureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("APIMART_API_KEY", "fake-key")
    monkeypatch.setenv("MARKETING_IMAGE_GENERATION_URL", f"http://127.0.0.1:{server.server_port}/generate")
    try:
        result = asyncio.run(image_client.submit_image("test"))
    finally:
        server.shutdown()
        server.server_close()
    assert result["state"] == "outcome_unknown"
    assert FailureHandler.posts == 1


def test_provider_200_without_task_id_is_unknown_not_retryable(monkeypatch):
    class MissingTaskHandler(_ProviderHandler):
        posts = 0
        def do_POST(self):
            type(self).posts += 1
            body = json.dumps({"data": []}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), MissingTaskHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("APIMART_API_KEY", "fake-key")
    monkeypatch.setenv("MARKETING_IMAGE_GENERATION_URL", f"http://127.0.0.1:{server.server_port}/generate")
    try:
        result = asyncio.run(image_client.submit_image("test"))
    finally:
        server.shutdown()
        server.server_close()
    assert result == {"state": "outcome_unknown", "error": "submit_missing_task_id"}
    assert MissingTaskHandler.posts == 1


def test_real_poll_timeout_recovers_by_task_id_without_second_post(monkeypatch):
    class PollTimeoutHandler(_ProviderHandler):
        posts = 0
        polls = 0
        image_bytes = b""

        def do_GET(self):
            if self.path.startswith("/tasks/"):
                type(self).polls += 1
                if type(self).polls == 1:
                    time.sleep(0.12)
                body = json.dumps({
                    "data": {"status": "succeeded", "result": {"images": [{
                        "url": [f"http://127.0.0.1:{self.server.server_port}/image.png"],
                    }]}}
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
            else:
                body = type(self).image_bytes
                self.send_response(200)
                self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    generated = io.BytesIO()
    Image.new("RGB", (768, 1024), "white").save(generated, format="PNG")
    PollTimeoutHandler.image_bytes = generated.getvalue()
    server = ThreadingHTTPServer(("127.0.0.1", 0), PollTimeoutHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("APIMART_API_KEY", "fake-key")
    monkeypatch.setenv("MARKETING_IMAGE_GENERATION_URL", f"http://127.0.0.1:{server.server_port}/generate")
    monkeypatch.setenv("MARKETING_IMAGE_TASK_URL", f"http://127.0.0.1:{server.server_port}/tasks/{{task_id}}")
    monkeypatch.setenv("MARKETING_IMAGE_DOWNLOAD_HOSTS", "127.0.0.1")
    monkeypatch.setenv("MARKETING_IMAGE_ALLOW_HTTP", "true")
    monkeypatch.setenv("MARKETING_IMAGE_ALLOW_PRIVATE_HOSTS", "127.0.0.1")
    monkeypatch.setattr(image_client, "_POLL_TIMEOUT_S", 0.03)

    async def qa_ok(*_args, **_kwargs):
        return {"passed": True, "errors": [], "warnings": []}

    monkeypatch.setattr(geo_factory, "visual_qa", qa_ok)
    geo = _geo()
    job = _job(geo)
    component = {
        "component_id": "professional_poster:image:professional_poster",
        "channel": "professional_poster", "kind": "image",
        "slot": {"slot": "professional_poster", "size": "3:4"},
    }
    try:
        first = asyncio.run(geo_factory._generate_image_component(
            job={**job, "resolution": "1k"}, component=component, geo=geo,
            content={"title": "真实证据"}, owner_key="u78130",
        ))
        assert first == {"_pending": True, "error": "provider_poll_pending"}
        attempt = marketing_db.list_attempts(int(job["id"]))[0]
        assert attempt["provider_task_id"] == "paid-task-1"
        assert attempt["poll_state"] == "pending"

        second = asyncio.run(geo_factory._generate_image_component(
            job={**job, "resolution": "1k"}, component=component, geo=geo,
            content={"title": "真实证据"}, owner_key="u78130",
        ))
    finally:
        server.shutdown()
        server.server_close()
    assert second and second["bundle_slot"] == component["component_id"]
    assert PollTimeoutHandler.posts == 1
    assert PollTimeoutHandler.polls >= 2


def test_live_authority_rejection_before_post_finishes_anchor_as_not_sent(monkeypatch):
    geo = _geo()
    job = _job(geo)
    posts = 0

    async def guarded_submit(*_args, before_request=None, **_kwargs):
        nonlocal posts
        if before_request:
            before_request()
        posts += 1
        return {"state": "submitted", "provider_task_id": "forbidden"}

    monkeypatch.setattr(image_client, "submit_image", guarded_submit)
    with pytest.raises(RuntimeError, match="revoked"):
        asyncio.run(geo_factory._generate_image_component(
            job={**job, "resolution": "1k"},
            component={"component_id": "professional_poster:image:professional_poster", "channel": "professional_poster", "kind": "image", "slot": {"slot": "professional_poster", "size": "3:4"}},
            geo=geo, content={"title": "真实证据"}, owner_key="u78130",
            provider_guard=lambda: (_ for _ in ()).throw(image_client.LiveAuthorityRejected("revoked")),
        ))
    assert posts == 0
    attempt = marketing_db.list_attempts(int(job["id"]))[0]
    assert attempt["status"] == "failed" and attempt["submit_state"] == "not_sent"


def test_copy_provider_guard_failure_is_never_swallowed_into_fallback(monkeypatch):
    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("provider call must not start")
    monkeypatch.setattr("tools.multi_llm_caller.call_llm_with_fallback", should_not_run)
    with pytest.raises(RuntimeError, match="authority_revoked"):
        asyncio.run(content_center.generate_channel_content(
            "private_chat", strategy=_geo()["strategy"], evidence=_geo()["evidence"],
            trend={"used": False}, contact={"mode": "none", "text": ""}, teacher=_geo()["teacher"],
            before_provider_call=lambda: (_ for _ in ()).throw(RuntimeError("authority_revoked")),
        ))


def test_copy_provider_rechecks_authority_before_each_fallback(monkeypatch):
    from tools.multi_llm_caller import MultiLLMCaller, ProviderCallGuardRejected

    providers_called = []
    guard_calls = 0

    async def failed_provider(self, provider, prompt):
        providers_called.append(provider["name"])
        return False, "provider_failed"

    def guard():
        nonlocal guard_calls
        guard_calls += 1
        # 1 = content preflight, 2 = first provider, 3 = second provider.
        if guard_calls == 3:
            raise RuntimeError("authority_revoked_between_copy_providers")

    monkeypatch.setattr(MultiLLMCaller, "_call_provider", failed_provider)
    with pytest.raises(ProviderCallGuardRejected, match="authority_revoked_between_copy_providers"):
        asyncio.run(content_center.generate_channel_content(
            "professional_poster",
            strategy=_geo()["strategy"], evidence=_geo()["evidence"],
            trend={"used": False}, contact={"mode": "none", "text": ""},
            teacher=_geo()["teacher"], before_provider_call=guard,
        ))

    assert providers_called == ["DeepSeek官方"]
    assert guard_calls == 3


def test_provider_safe_contact_excludes_private_qr_reference_fields():
    safe = content_center.provider_safe_contact({
        "mode": "qr", "reference_id": "not-here", "qr_reference": {
            "reference_id": "private.png", "payload_hash": "secret-payload", "file_sha256": "secret-file",
        },
    })
    assert safe == {"mode": "qr", "text": "", "qr_attached": True}
    assert "secret" not in json.dumps(safe)


def test_qr_upload_resolves_to_owned_data_uri_and_contact_none_scans_locally(monkeypatch):
    qrcode = pytest.importorskip("qrcode")
    qr_image = qrcode.make("https://example.test/exact-user-qr")
    qr_buffer = io.BytesIO()
    qr_image.save(qr_buffer, format="PNG")
    raw = qr_buffer.getvalue()
    validation = validate_qr_reference(raw)
    stored = material_storage.save_qr_reference("u78130", raw)
    uri, resolved = material_storage.qr_reference_data_uri("u78130", stored["reference_id"], stored["sha256"])
    assert uri.startswith("data:image/png;base64,")
    _assert_same_image_pixels(resolved, raw)
    assert validate_qr_reference(resolved)["payload_hash"] == validation["payload_hash"]
    assert validation["payload_hash"]
    with pytest.raises(ValueError, match="missing"):
        material_storage.qr_reference_data_uri("u78131", stored["reference_id"], stored["sha256"])

    clean = io.BytesIO()
    Image.new("RGB", (256, 256), "white").save(clean, format="PNG")

    async def vision_ok(*_args, **_kwargs):
        return {"vision_ok": True, "ocr_text": "", "risk_flags": []}

    monkeypatch.setattr("tools.vision.image_describe.describe_image_structured", vision_ok)
    qa = asyncio.run(visual_qa(
        clean.getvalue(), size="1:1", exact_copy={}, brand={}, evidence={}, contact={"mode": "none"},
    ))
    assert qa["passed"] is True


def test_qr_upload_api_returns_only_opaque_reference_never_persistent_path(monkeypatch):
    qrcode = pytest.importorskip("qrcode")
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from api.marketing_material_api import router
    monkeypatch.setenv("JWT_SECRET_KEY", "qr-api-test-secret")
    qr = qrcode.make("https://example.test/private-reference")
    buffer = io.BytesIO()
    qr.save(buffer, format="PNG")
    app = FastAPI()

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.user = {"id": 1, "user_id": 1}
        return await call_next(request)

    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/api/marketing/qr-reference", files={"file": ("qr.png", buffer.getvalue(), "image/png")},
        )
    assert response.status_code == 200, response.text
    reference = response.json()["qr_reference"]
    assert reference["reference_id"].endswith(".png")
    serialized = json.dumps(reference, ensure_ascii=False)
    assert "url" not in reference
    for private_fragment in ("/uploads/", "/app/data", "marketing-private", "qr-inputs", "storage_key"):
        assert private_fragment not in serialized


def test_private_geo_artifact_has_no_static_url_or_path_in_public_contract(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient

    public_root = tmp_path / "uploads"
    public_root.mkdir()
    monkeypatch.chdir(tmp_path)
    image = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(image, format="PNG")
    stored = material_storage.save_material_image("u-private", image.getvalue(), "poster.png", private=True)
    private_path = Path(stored["storage_key"]).resolve()
    assert private_path.is_file()
    assert public_root.resolve() not in private_path.parents
    assert stored["public_url"].startswith("private://")
    assert "/uploads/" not in stored["public_url"]

    app = FastAPI()
    app.mount("/uploads", StaticFiles(directory=str(public_root)), name="uploads")
    with TestClient(app) as client:
        guessed = client.get(f"/uploads/.private/marketing-materials/u-private/{private_path.name}")
    assert guessed.status_code == 404


def test_legacy_material_storage_keeps_relative_keys_and_public_url(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    image = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(image, format="PNG")
    stored = material_storage.save_material_image("u-legacy", image.getvalue(), "poster.png", private=False)
    assert stored["storage_key"].startswith("uploads/marketing-materials/u-legacy/")
    assert not Path(stored["storage_key"]).is_absolute()
    assert stored["thumbnail_key"].startswith("uploads/marketing-materials/u-legacy/")
    assert not Path(stored["thumbnail_key"]).is_absolute()
    assert stored["public_url"].startswith("/uploads/marketing-materials/u-legacy/")
    assert stored["thumbnail_url"].startswith("/uploads/marketing-materials/u-legacy/")


def test_qr_full_seam_uses_private_input_fails_bad_output_then_retries_to_scannable_composite(monkeypatch):
    qrcode = pytest.importorskip("qrcode")
    payload = "https://example.test/exact-retry-payload"
    qr_image = qrcode.make(payload).convert("RGB")
    source_buffer = io.BytesIO()
    qr_image.save(source_buffer, format="PNG")
    source = source_buffer.getvalue()
    validation = validate_qr_reference(source)
    stored_qr = material_storage.save_qr_reference("u78130", source)
    contact = {"mode": "qr", "text": "", "qr_reference": {
        "reference_id": stored_qr["reference_id"], "payload_hash": validation["payload_hash"],
        "file_sha256": stored_qr["sha256"],
    }}
    geo = _geo(contact=contact, brand={})
    job = _job(geo)
    submitted_inputs: list[list[str]] = []

    async def submit(*_args, image_urls=None, **_kwargs):
        submitted_inputs.append(list(image_urls or []))
        return {"state": "submitted", "provider_task_id": f"qr-task-{len(submitted_inputs)}"}

    async def poll(task_id, **_kwargs):
        return {"state": "succeeded", "image_url": f"https://cdn.apimart.ai/{task_id}.png"}

    blank_buffer = io.BytesIO()
    Image.new("RGB", (768, 1024), "white").save(blank_buffer, format="PNG")
    composite = Image.new("RGB", (768, 1024), "white")
    qr_large = qr_image.resize((420, 420), Image.Resampling.NEAREST)
    composite.paste(qr_large, ((768 - 420) // 2, (1024 - 420) // 2))
    composite_buffer = io.BytesIO()
    composite.save(composite_buffer, format="PNG")

    async def download(url):
        return blank_buffer.getvalue() if "qr-task-1" in url else composite_buffer.getvalue()

    async def vision_ok(*_args, **_kwargs):
        return {"vision_ok": True, "ocr_text": "", "risk_flags": ["qrcode"]}

    monkeypatch.setattr(image_client, "submit_image", submit)
    monkeypatch.setattr(image_client, "poll_image_task", poll)
    monkeypatch.setattr(image_client, "download_image", download)
    monkeypatch.setattr("tools.vision.image_describe.describe_image_structured", vision_ok)
    asset = asyncio.run(geo_factory._generate_image_component(
        job={**job, "resolution": "1k"},
        component={"component_id": "professional_poster:image:professional_poster", "channel": "professional_poster", "kind": "image", "slot": {"slot": "professional_poster", "size": "3:4"}},
        geo=geo, content={}, owner_key="u78130",
    ))
    assert asset and len(submitted_inputs) == 2
    assert all(any(value.startswith("data:image/png;base64,") for value in inputs) for inputs in submitted_inputs)
    rows = marketing_db.list_attempts(int(job["id"]))
    assert rows[0]["status"] == "failed"
    _assert_five_question_flags(rows[0]["safety_flags_jsonb"], "qr_payload_mismatch_or_unreadable")
    assert rows[1]["status"] == "succeeded"


def test_effective_revision_merges_parent_assets_and_public_state_hides_lineage():
    evidence = {
        "source_type": "diagnosis", "source_id": 918273, "brand_id": 817263,
        "organization_id": 716253, "created_by_membership_id": 615243,
        "snapshot_hash": "deadbeef-internal-snapshot", "facts": [{"label": "评分", "value": 71}],
        "source_note": "依据本次已发布诊断结果，生成时已冻结",
    }
    parent = _job(_geo(evidence=evidence))
    marketing_db.add_asset(job_id=int(parent["id"]), asset_kind="copy", bundle_slot="professional_poster:copy", content_text='{"title":"父文案"}')
    marketing_db.add_asset(job_id=int(parent["id"]), asset_kind="bundle_item", bundle_slot="professional_poster:image:professional_poster", url_stored="/uploads/parent.png")
    _set_status_direct(int(parent["id"]), "succeeded")
    child = _job(_geo(evidence=evidence, parent_job_id=int(parent["id"]), only_components=["professional_poster:copy"], revision_no=2))
    marketing_db.add_asset(job_id=int(child["id"]), asset_kind="copy", bundle_slot="professional_poster:copy", content_text='{"title":"子文案"}')
    _set_status_direct(int(child["id"]), "succeeded")
    child = marketing_db.get_job(int(child["id"]))
    state = geo_factory.job_public_state(child)
    slots = {asset["bundle_slot"]: asset for asset in state["assets"]}
    assert json.loads(slots["professional_poster:copy"]["content_text"])["title"] == "子文案"
    image_asset = slots["professional_poster:image:professional_poster"]
    assert image_asset["download_url"].endswith(f"/materials/{image_asset['id']}/download")
    assert "url_stored" not in image_asset and "thumbnail_url" not in image_asset
    public = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)
    for secret in ("918273", "817263", "716253", "615243", "deadbeef-internal-snapshot", "parent_job_id", "request_id"):
        assert secret not in public
    assert state["revision_no"] == 2


def test_generating_revision_keeps_settled_parent_and_hides_child_early_asset():
    parent = _job(_geo())
    marketing_db.add_asset(
        job_id=int(parent["id"]), asset_kind="copy",
        bundle_slot="professional_poster:copy", content_text='{"title":"父版本已结算"}',
    )
    marketing_db.add_asset(
        job_id=int(parent["id"]), asset_kind="bundle_item",
        bundle_slot="professional_poster:image:professional_poster",
        url_stored="private://marketing-materials/u78130/parent-settled.png",
    )
    _set_status_direct(int(parent["id"]), "succeeded")
    child = _job(_geo(
        parent_job_id=int(parent["id"]),
        only_components=["professional_poster:copy"], revision_no=2,
    ))
    marketing_db.add_asset(
        job_id=int(child["id"]), asset_kind="copy",
        bundle_slot="professional_poster:copy", content_text='{"title":"子版本尚未结算"}',
    )
    marketing_db.update_job(int(child["id"]), status="generating")

    state = geo_factory.job_public_state(marketing_db.get_job(int(child["id"])))
    slots = {asset["bundle_slot"]: asset for asset in state["assets"]}
    assert json.loads(slots["professional_poster:copy"]["content_text"])["title"] == "父版本已结算"
    assert slots["professional_poster:image:professional_poster"]["download_url"].endswith(
        f"/materials/{slots['professional_poster:image:professional_poster']['id']}/download"
    )
    assert "子版本尚未结算" not in json.dumps(state, ensure_ascii=False, default=str)


def test_provider_prompts_never_contain_internal_diagnosis_lineage():
    evidence = {
        "source_type": "diagnosis", "source_id": 991122, "brand_id": 882211,
        "organization_id": 773300, "created_by_membership_id": 664499,
        "snapshot_hash": "private-hash-123", "facts": [{"label": "评分", "value": 71}],
        "source_note": "依据本次已发布诊断结果，生成时已冻结",
    }
    prompt = content_center.visual_prompt(
        slot={"slot": "professional_poster", "size": "3:4"}, channel="professional_poster",
        strategy={"audience": "服务商", "single_action": "领取诊断", "single_value": "证据链"},
        content={"title": "先看真实诊断", "body": "评分 71"}, evidence=evidence,
        trend={"used": False}, contact={"mode": "none", "text": ""}, brand={
            "name": "OmniRank", "organization_id": 550011, "owner_user_id": 660022,
            "snapshot_hash": "private-brand-hash",
        },
    )
    for secret in (
        "991122", "882211", "773300", "664499", "private-hash-123",
        "550011", "660022", "private-brand-hash",
    ):
        assert secret not in prompt


def test_final_image_ocr_blocks_internal_lineage(monkeypatch):
    image = io.BytesIO()
    Image.new("RGB", (768, 1024), "white").save(image, format="PNG")

    async def leaked_ocr(*_args, **_kwargs):
        return {
            "vision_ok": True,
            "ocr_text": "内部诊断 991122",
            "risk_flags": [],
        }

    monkeypatch.setattr("tools.vision.image_describe.describe_image_structured", leaked_ocr)
    result = asyncio.run(visual_qa(
        image.getvalue(), size="3:4", exact_copy={}, brand={}, evidence={},
        contact={"mode": "none", "text": ""}, internal_lineage_values=["991122"],
    ))
    assert result["passed"] is False
    assert "internal_lineage_visible" in {error["code"] for error in result["errors"]}


def test_by_request_recovery_is_tenant_scoped_and_retry_rejects_live_parent():
    from types import SimpleNamespace
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from api.marketing_material_api import router

    owner_id = 78130
    other_id = 78131
    owner_job = _job(_geo(), user_id=owner_id)
    request_identity = owner_job["input_fields_jsonb"]["_geo"]["request_id"]
    _set_status_direct(int(owner_job["id"]), "succeeded")
    live_job = _job(_geo(), user_id=owner_id)
    org_job = _job(_geo(actor_snapshot={"organization_id": 42}), user_id=owner_id)
    org_request_identity = org_job["input_fields_jsonb"]["_geo"]["request_id"]
    _set_status_direct(int(org_job["id"]), "succeeded")

    app = FastAPI()

    class _TestOrgIdentity:
        """契约完整的组织身份 stub(对齐 _resolve_live_org_identity 的消费合同)。"""

        def __init__(self, organization_id: int):
            self.organization_id = organization_id
            self.membership_status = "active"
            self.membership_id = 90042
            self.actor_kind = "member"
            self.authority_version = 1
            self.organization_authority_version = 1
            self.membership_version = 1
            self.assignment_version = 1

        def require_active_organization(self):
            return None

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        user_id = int(request.headers.get("x-test-user") or owner_id)
        request.state.user = {"id": user_id, "user_id": user_id, "is_admin": False}
        if request.headers.get("x-test-org"):
            request.state.organization_identity = _TestOrgIdentity(int(request.headers["x-test-org"]))
        return await call_next(request)

    app.include_router(router)
    with TestClient(app) as client:
        denied = client.get(
            f"/api/marketing/content-packages/by-request/{request_identity}",
            headers={"x-test-user": str(other_id)},
        )
        allowed = client.get(
            f"/api/marketing/content-packages/by-request/{request_identity}",
            headers={"x-test-user": str(owner_id)},
        )
        retry_live = client.post(
            f"/api/marketing/jobs/{int(live_job['id'])}/retry",
            headers={"x-test-user": str(owner_id)},
            json={"request_id": f"retry-{uuid4().hex}", "component_ids": ["professional_poster:copy"]},
        )
        wrong_org = client.get(
            f"/api/marketing/content-packages/by-request/{org_request_identity}",
            headers={"x-test-user": str(owner_id), "x-test-org": "43"},
        )
        right_org = client.get(
            f"/api/marketing/content-packages/by-request/{org_request_identity}",
            headers={"x-test-user": str(owner_id), "x-test-org": "42"},
        )
    assert denied.status_code == 404
    assert allowed.status_code == 200 and int(allowed.json()["job"]["job_id"]) == int(owner_job["id"])
    assert retry_live.status_code == 409
    assert wrong_org.status_code == 404
    assert right_org.status_code == 200


def test_retry_api_persists_merged_revision_and_new_identity_after_failed_child(monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from api.marketing_material_api import router
    from middleware.billing import commit_freeze, release_freeze

    user_id = 820000 + (uuid4().int % 70000)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS customer_credit_freezes "
            "(id BIGSERIAL PRIMARY KEY,customer_user_id INTEGER,task_ref TEXT,status TEXT NOT NULL DEFAULT 'frozen')"
        )
        cur.execute(
            "INSERT INTO users(id,username,display_name,phone,email) VALUES (%s,%s,'Revision API','retry-phone-%s',%s)",
            (user_id, f"revision-api-{user_id}", user_id, f"revision-{user_id}@example.test"),
        )
        cur.execute("INSERT INTO user_roles(user_id,role_id) SELECT %s,id FROM roles WHERE name='user' LIMIT 1", (user_id,))
        cur.execute(
            "INSERT INTO user_wallets(user_id,paid_points,total_recharged,agent_level) VALUES (%s,2000000,2000000,1)",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()

    parent = _job(_geo(), user_id=user_id)
    marketing_db.add_asset(
        job_id=int(parent["id"]), asset_kind="copy", bundle_slot="professional_poster:copy",
        content_text='{"title":"父版本成功文案"}',
    )
    _set_status_direct(int(parent["id"]), "succeeded", "partial_free_release")

    scheduled = []

    async def capture_execution(ctx):
        scheduled.append(dict(ctx))
        return {"status": "generating", "job_id": int(ctx["job_id"])}

    monkeypatch.setattr(geo_factory, "execute_geo_package_job", capture_execution)
    app = FastAPI()

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.user = {"id": user_id, "user_id": user_id, "is_admin": False}
        return await call_next(request)

    app.include_router(router)
    with TestClient(app) as client:
        first_retry_id = f"retry-{uuid4().hex}"
        first = client.post(
            f"/api/marketing/jobs/{int(parent['id'])}/retry",
            json={"request_id": first_retry_id, "component_ids": ["professional_poster:image:professional_poster"]},
        )
        assert first.status_code == 200, first.text
        child_failed_id = int(first.json()["job_id"])
        failed_job = marketing_db.get_job(child_failed_id)
        asyncio.run(release_freeze(
            freeze_id=int(failed_job["freeze_id"]), reason="revision test child failed", user_id=user_id,
        ))
        _set_status_direct(child_failed_id, "failed", "all_components_failed")
        failed_state = client.get(f"/api/marketing/jobs/{child_failed_id}")
        assert failed_state.status_code == 200
        assert {asset["bundle_slot"] for asset in failed_state.json()["job"]["assets"]} == {"professional_poster:copy"}

        second_retry_id = f"retry-{uuid4().hex}"
        second = client.post(
            f"/api/marketing/jobs/{child_failed_id}/retry",
            json={"request_id": second_retry_id, "component_ids": []},
        )
        assert second.status_code == 200, second.text
        child_success_id = int(second.json()["job_id"])
        assert child_success_id != child_failed_id
        child_success = marketing_db.get_job(child_success_id)
        marketing_db.add_asset(
            job_id=child_success_id, asset_kind="bundle_item",
            bundle_slot="professional_poster:image:professional_poster",
            url_stored="/uploads/marketing-materials/revision-success.png",
        )
        asyncio.run(commit_freeze(
            freeze_id=int(child_success["freeze_id"]), reason="revision test child succeeded", user_id=user_id,
        ))
        _set_status_direct(child_success_id, "succeeded")
        success_state = client.get(f"/api/marketing/jobs/{child_success_id}")

    assert success_state.status_code == 200
    state = success_state.json()["job"]
    assert state["revision_no"] == 3
    assert {asset["bundle_slot"] for asset in state["assets"]} == {
        "professional_poster:copy", "professional_poster:image:professional_poster",
    }
    assert len(scheduled) == 2


def test_download_url_allowlist_rejects_arbitrary_and_private_targets(monkeypatch):
    monkeypatch.setenv("MARKETING_IMAGE_DOWNLOAD_HOSTS", "cdn.example.test")
    with pytest.raises(ValueError, match="host_forbidden"):
        image_client._validate_download_url("https://169.254.169.254/latest/meta-data")


def test_provider_attempt_migration_fresh_twice_rollback_forward_and_exact_catalog(provider_migration_db):
    forward = Path("scripts/migration_geo_provider_attempts_2026_07_21.sql").read_text(encoding="utf-8")
    rollback = Path("scripts/rollback_geo_provider_attempts_2026_07_21.sql").read_text(encoding="utf-8")
    conn = provider_migration_db
    with conn.cursor() as cur:
        cur.execute(forward)
        cur.execute(forward)
        cur.execute(rollback)
        cur.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='marketing_material_generation_attempts' AND column_name='submit_state'")
        assert cur.fetchone()[0] == 0
        cur.execute(forward)
        cur.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='marketing_material_generation_attempts' AND column_name='submit_state'")
        assert cur.fetchone()[0] == 1

        cur.execute(
            """
            SELECT c.conname,c.convalidated,pg_get_constraintdef(c.oid)
            FROM pg_catalog.pg_constraint c
            WHERE c.conrelid='public.marketing_material_generation_attempts'::regclass
              AND c.conname LIKE 'marketing_attempt_%_state_ck'
            ORDER BY c.conname
            """
        )
        checks = cur.fetchall()
        assert len(checks) == 4 and all(row[1] for row in checks)
        cur.execute(
            """
            SELECT cls.relname,i.indrelid='public.marketing_material_generation_attempts'::regclass,
                   i.indisunique,i.indisvalid,i.indisready,i.indkey::text,pg_get_expr(i.indpred,i.indrelid)
            FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class cls ON cls.oid=i.indexrelid
            JOIN pg_catalog.pg_namespace ns ON ns.oid=cls.relnamespace
            WHERE ns.nspname='public' AND cls.relname IN
              ('uq_marketing_attempt_component_no','idx_marketing_attempt_recovery')
            ORDER BY cls.relname
            """
        )
        indexes = cur.fetchall()
        assert len(indexes) == 2 and all(row[1] and row[3] and row[4] for row in indexes)


@pytest.mark.parametrize("lure_kind", ["true_or_check", "not_valid_check", "wrong_table_index", "wrong_default"])
def test_provider_attempt_migration_rejects_catalog_lures(provider_migration_db, lure_kind):
    forward = Path("scripts/migration_geo_provider_attempts_2026_07_21.sql").read_text(encoding="utf-8")
    conn = provider_migration_db
    with conn.cursor() as cur:
        cur.execute(forward)
        if lure_kind in {"true_or_check", "not_valid_check"}:
            cur.execute(
                "ALTER TABLE public.marketing_material_generation_attempts "
                "DROP CONSTRAINT marketing_attempt_submit_state_ck"
            )
            suffix = " NOT VALID" if lure_kind == "not_valid_check" else ""
            expression = (
                "TRUE OR submit_state IN ('not_started','anchored','submitted','outcome_unknown','not_sent','rejected')"
                if lure_kind == "true_or_check"
                else "submit_state IN ('not_started','anchored','submitted','outcome_unknown','not_sent','rejected')"
            )
            cur.execute(
                "ALTER TABLE public.marketing_material_generation_attempts "
                f"ADD CONSTRAINT marketing_attempt_submit_state_ck CHECK ({expression}){suffix}"
            )
        elif lure_kind == "wrong_table_index":
            cur.execute("DROP INDEX public.uq_marketing_attempt_component_no")
            cur.execute("CREATE TABLE public.same_name_index_lure(job_id BIGINT)")
            cur.execute("CREATE INDEX uq_marketing_attempt_component_no ON public.same_name_index_lure(job_id)")
        else:
            cur.execute(
                "ALTER TABLE public.marketing_material_generation_attempts "
                "ALTER submit_state SET DEFAULT 'anchored'"
            )
        with pytest.raises(psycopg2.Error, match="missing/weak/invalid CHECK|missing/wrong/invalid indexes|missing/wrong columns or defaults"):
            cur.execute(forward)


def test_provider_attempt_migration_ignores_wrong_schema_lures_and_pins_public(provider_migration_db):
    forward = Path("scripts/migration_geo_provider_attempts_2026_07_21.sql").read_text(encoding="utf-8")
    conn = provider_migration_db
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA lure")
        cur.execute("CREATE TABLE lure.marketing_material_generation_attempts(job_id BIGINT,component_id TEXT,attempt_no INTEGER)")
        cur.execute("CREATE UNIQUE INDEX uq_marketing_attempt_component_no ON lure.marketing_material_generation_attempts(job_id)")
        cur.execute("SET search_path TO lure,public")
        cur.execute(forward)
        cur.execute(
            "SELECT i.indrelid='public.marketing_material_generation_attempts'::regclass "
            "FROM pg_catalog.pg_index i WHERE i.indexrelid='public.uq_marketing_attempt_component_no'::regclass"
        )
        assert cur.fetchone()[0] is True


@pytest.mark.parametrize(
    "fact_sql",
    [
        "UPDATE public.marketing_material_generation_attempts SET status='running'",
        "UPDATE public.marketing_material_generation_attempts SET provider_task_id='paid-provider-task'",
        "UPDATE public.marketing_material_generation_attempts SET provider_result_jsonb='{\"image_url\":\"object://result\"}'::jsonb",
        "UPDATE public.marketing_material_generation_attempts SET resolution_state='manual_required'",
        "UPDATE public.marketing_material_generation_attempts SET materialization_state='materialized'",
        "UPDATE public.marketing_material_generation_attempts SET submit_state='outcome_unknown'",
    ],
)
def test_provider_attempt_rollback_refuses_retained_recovery_facts(provider_migration_db, fact_sql):
    forward = Path("scripts/migration_geo_provider_attempts_2026_07_21.sql").read_text(encoding="utf-8")
    rollback = Path("scripts/rollback_geo_provider_attempts_2026_07_21.sql").read_text(encoding="utf-8")
    conn = provider_migration_db
    with conn.cursor() as cur:
        cur.execute(forward)
        cur.execute(
            "INSERT INTO public.marketing_material_generation_attempts(job_id,attempt_no) VALUES (1,1)"
        )
        cur.execute(fact_sql)
        with pytest.raises(psycopg2.Error, match="rollback refused"):
            cur.execute(rollback)
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name='marketing_material_generation_attempts' AND column_name='provider_result_jsonb'"
        )
        assert cur.fetchone()[0] == 1


def test_lease_expiry_stops_before_real_provider_post(monkeypatch):
    _overview, _identity, charge, claim = _organization_charge(monkeypatch)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 second' WHERE charge_link_id=%s", (int(charge["id"]),))
        cur.execute("UPDATE organization_charge_links SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (int(charge["id"]),))
        conn.commit()
    finally:
        conn.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _ProviderHandler.posts = 0
    _ProviderHandler.delay = 0
    monkeypatch.setenv("APIMART_API_KEY", "fake-key")
    monkeypatch.setenv("MARKETING_IMAGE_GENERATION_URL", f"http://127.0.0.1:{server.server_port}/generate")
    from services.organization_billing import mark_external_side_effect_started
    try:
        with pytest.raises(image_client.LiveAuthorityRejected):
            asyncio.run(image_client.submit_image(
                "must not leave process",
                before_request=lambda: mark_external_side_effect_started(
                    charge_link_id=int(charge["id"]), claim_token=str(claim["claim_token"]), lease_seconds=60,
                ),
            ))
    finally:
        server.shutdown()
        server.server_close()
    assert _ProviderHandler.posts == 0


def test_authority_revoked_between_paid_posts_blocks_second_post(monkeypatch):
    overview, _identity, charge, claim = _organization_charge(monkeypatch)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _ProviderHandler.posts = 0
    _ProviderHandler.delay = 0
    monkeypatch.setenv("APIMART_API_KEY", "fake-key")
    monkeypatch.setenv("MARKETING_IMAGE_GENERATION_URL", f"http://127.0.0.1:{server.server_port}/generate")
    from services.organization_billing import mark_external_side_effect_started

    def guard():
        return mark_external_side_effect_started(
            charge_link_id=int(charge["id"]), claim_token=str(claim["claim_token"]), lease_seconds=60,
        )

    first = asyncio.run(image_client.submit_image("first paid image", before_request=guard))
    assert first["state"] == "submitted"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE organizations SET status='suspended',suspended_at=NOW(),authority_version=authority_version+1 WHERE id=%s", (int(overview["id"]),))
        conn.commit()
    finally:
        conn.close()
    try:
        with pytest.raises(image_client.LiveAuthorityRejected):
            asyncio.run(image_client.submit_image("second paid image", before_request=guard))
    finally:
        server.shutdown()
        server.server_close()
    assert _ProviderHandler.posts == 1


def test_twenty_concurrent_workers_obtain_only_one_live_claim(monkeypatch):
    _overview, identity, charge, first_claim = _organization_charge(monkeypatch, claim_now=False)
    assert first_claim is None
    from services.organization_billing import claim_live_charge

    def compete(_index):
        try:
            return claim_live_charge(identity, charge_link_id=int(charge["id"]), lease_seconds=60)
        except Exception as exc:
            return {"error": getattr(exc, "code", type(exc).__name__)}

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(compete, range(20)))
    assert sum(1 for result in results if result.get("claim_token")) == 1
    assert all(result.get("claim_token") or result.get("in_progress") or result.get("error") == "ORG_WORK_CLAIM_CONFLICT" for result in results)


def _wallet_points(user_id: int) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (int(user_id),))
        return int(cur.fetchone()["paid_points"])
    finally:
        conn.close()


def test_commit_response_lost_reconciles_real_charge_and_publishes_once(monkeypatch):
    overview, identity, charge, claim = _organization_charge(monkeypatch)
    from services.organization_billing import mark_external_side_effect_started
    mark_external_side_effect_started(
        charge_link_id=int(charge["id"]), claim_token=str(claim["claim_token"]), lease_seconds=60,
    )
    geo = _geo()
    job = _job(geo, user_id=int(identity.authenticated_user_id))
    marketing_db.update_job(
        int(job["id"]), status="generating", billing_ref=f"org:{int(charge['id'])}",
        cost_points=int(charge["estimated_points"]),
    )
    asset = marketing_db.add_asset(
        job_id=int(job["id"]), asset_kind="copy", bundle_slot="professional_poster:copy",
        content_text='{"title":"真实内容"}',
    )
    from services import organization_billing
    real_settle = organization_billing.settle_charge
    lost_once = True

    async def commit_then_lose_response(**kwargs):
        nonlocal lost_once
        result = await real_settle(**kwargs)
        if lost_once:
            lost_once = False
            raise ConnectionError("response_lost_after_commit")
        return result

    monkeypatch.setattr(organization_billing, "settle_charge", commit_then_lose_response)
    ctx = {
        "billing_kind": "organization", "organization_charge_id": int(charge["id"]),
        "claim_token": claim["claim_token"], "actual_points": int(charge["estimated_points"]),
    }
    first = asyncio.run(geo_factory._settle_geo(job, ctx, [asset], 1, external_started=True))
    assert first["status"] == "generating"
    assert marketing_db.get_job(int(job["id"]))["error_summary"] == "billing_commit_pending"
    points_after_commit = _wallet_points(int(identity.payer_user_id))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE organizations SET status='suspended',suspended_at=NOW(),authority_version=authority_version+1 WHERE id=%s",
            (int(overview["id"]),),
        )
        conn.commit()
    finally:
        conn.close()
    from services.organization_billing import get_charge_reconciliation_state
    assert get_charge_reconciliation_state(identity, charge_link_id=int(charge["id"]))["status"] == "committed"
    recovery_ctx = {**ctx, "claim_token": "", "settlement_only": True}
    second = asyncio.run(geo_factory._settle_geo(
        marketing_db.get_job(int(job["id"])), recovery_ctx, [asset], 1, external_started=True,
    ))
    assert second["status"] == "succeeded"
    assert _wallet_points(int(identity.payer_user_id)) == points_after_commit
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status,actual_points FROM organization_charge_links WHERE id=%s", (int(charge["id"]),))
        row = cur.fetchone()
        assert row["status"] == "committed"
        assert int(row["actual_points"]) == int(charge["estimated_points"])
        cur.execute("SELECT COUNT(*)::int AS count FROM notification_outbox WHERE business_id=%s AND terminal_state='completed'", (str(job["id"]),))
        assert int(cur.fetchone()["count"]) == 1
    finally:
        conn.close()


def test_manifest_orders_provider_attempt_migration_and_readiness_columns_exist():
    from db.migration_manifest import MIGRATIONS

    name = "scripts/migration_geo_provider_attempts_2026_07_21.sql"
    assert name in MIGRATIONS
    assert MIGRATIONS.index(name) == MIGRATIONS.index("scripts/migration_marketing_center_2026_07_04.sql") + 1
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='marketing_material_generation_attempts'"
        )
        have = {row["column_name"] for row in cur.fetchall()}
    finally:
        conn.close()
    assert {
        "component_id", "submit_state", "poll_state", "submit_guard_token",
        "last_heartbeat_at", "provider_result_jsonb", "materialization_state",
        "resolution_state", "resolved_at",
    }.issubset(have)
    readiness = Path("server.py").read_text(encoding="utf-8")
    assert "table_schema='public' AND table_name='marketing_material_generation_attempts'" in readiness
    assert '"last_heartbeat_at"' in readiness


def test_org_poll_only_reclaim_resumes_same_attempt_and_never_submits(monkeypatch):
    overview, identity, _unused_charge, _unused_claim = _organization_charge(monkeypatch, claim_now=False)
    geo = _geo(actor_snapshot={
        "organization_id": int(overview["id"]), "actor_kind": "owner",
        "membership_id": int(identity.membership_id), "membership_version": int(identity.membership_version),
    })
    job = _job(geo, user_id=int(identity.authenticated_user_id))
    frozen = job["input_fields_jsonb"]["_geo"]
    from services.organization_billing import (
        claim_live_charge, mark_external_side_effect_started, renew_poll_only_lease, reserve_charge,
    )
    charge = asyncio.run(reserve_charge(
        identity, execution_id=f"poll-only-{uuid4().hex}", feature_code="mktg_poster_basic",
        work_kind="geo_content_package", payload={"job_id": int(job["id"]), "geo_snapshot": frozen},
        brand_id=None, task_ref=f"poll-only-{job['id']}",
    ))
    claim = claim_live_charge(identity, charge_link_id=int(charge["id"]), lease_seconds=60)
    mark_external_side_effect_started(
        charge_link_id=int(charge["id"]), claim_token=str(claim["claim_token"]), lease_seconds=60,
    )
    marketing_db.update_job(
        int(job["id"]), status="generating", billing_ref=f"org:{int(charge['id'])}",
        cost_points=int(charge["estimated_points"]), error_summary="provider_poll_pending",
    )
    attempt = marketing_db.add_attempt(
        job_id=int(job["id"]), component_id="professional_poster:image:professional_poster",
        attempt_no=1, status="running", submit_state="submitted", poll_state="pending",
        provider_task_id="durable-task-1",
    )
    prepared = asyncio.run(geo_factory.prepare_geo_package_job(
        user_id=int(identity.authenticated_user_id), brand_id=None, geo_snapshot=frozen,
        request_hash=str(frozen["request_hash"]), resolution="1k", organization_identity=identity,
    ))
    ctx = prepared["_ctx"]
    assert int(ctx["poll_only_attempt_id"]) == int(attempt["id"])
    assert ctx["poll_only_task_id"] == "durable-task-1"
    posts = 0

    async def forbidden_submit(*_args, **_kwargs):
        nonlocal posts
        posts += 1
        raise AssertionError("poll recovery must not submit")

    async def poll(*_args, **_kwargs):
        return {"state": "pending", "error": "still_running"}

    monkeypatch.setattr(image_client, "submit_image", forbidden_submit)
    monkeypatch.setattr(image_client, "poll_image_task", poll)
    result = asyncio.run(geo_factory._generate_image_component(
        job={**job, "resolution": "1k"},
        component={"component_id": "professional_poster:image:professional_poster", "channel": "professional_poster", "kind": "image", "slot": {"slot": "professional_poster", "size": "3:4"}},
        geo=frozen, content={"title": "真实证据"}, owner_key=f"u{identity.authenticated_user_id}",
        poll_guard=lambda attempt_id, task_id: renew_poll_only_lease(
            charge_link_id=int(charge["id"]), claim_token=str(ctx["claim_token"]),
            attempt_id=attempt_id, provider_task_id=task_id, lease_seconds=60,
        ),
    ))
    assert result == {"_pending": True, "error": "provider_poll_pending"}
    assert posts == 0


def test_organization_owner_uses_frozen_org_charge_chain(monkeypatch):
    overview, identity, _unused_charge, _unused_claim = _organization_charge(monkeypatch, claim_now=False)
    request_id = f"owner-org-{uuid4().hex}"
    geo = _geo(
        request_id=request_id,
        actor_snapshot={
            "organization_id": int(overview["id"]), "actor_kind": "owner",
            "membership_id": int(identity.membership_id), "membership_version": int(identity.membership_version),
        },
    )
    geo["request_hash"] = geo_factory.canonical_hash({"owner-org": request_id})
    prepared = asyncio.run(geo_factory.prepare_geo_package_job(
        user_id=int(identity.authenticated_user_id), brand_id=None, geo_snapshot=geo,
        request_hash=geo["request_hash"], organization_identity=identity,
    ))
    assert prepared["_ctx"]["billing_kind"] == "organization"
    assert str(marketing_db.get_job(int(prepared["job_id"]))["billing_ref"]).startswith("org:")


def test_kill_after_provider_success_replays_materialization_without_second_post(monkeypatch):
    geo = _geo()
    job = _job(geo)
    attempt = marketing_db.add_attempt(
        job_id=int(job["id"]), component_id="professional_poster:image:professional_poster",
        attempt_no=1, status="running", submit_state="submitted", poll_state="polling",
        provider_task_id="kill-window-task",
    )
    child_code = (
        "import time\n"
        "from db import marketing_db\n"
        f"marketing_db.update_attempt_provider_state({int(attempt['id'])},poll_state='succeeded',"
        "provider_result={'image_url':'https://cdn.apimart.ai/kill-window.png'},materialization_state='pending')\n"
        "print('PROVIDER_RESULT_DURABLE',flush=True)\n"
        "time.sleep(60)\n"
    )
    env = os.environ.copy()
    env["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    process = subprocess.Popen(
        [sys.executable, "-c", child_code], cwd=str(Path.cwd()), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None and process.stdout.readline().strip() == "PROVIDER_RESULT_DURABLE"
    process.kill()
    process.wait(timeout=10)
    posts = 0
    image = io.BytesIO()
    Image.new("RGB", (768, 1024), "white").save(image, format="PNG")

    async def forbidden_submit(*_args, **_kwargs):
        nonlocal posts
        posts += 1
        raise AssertionError("durable success may only materialize")

    async def qa_ok(*_args, **_kwargs):
        return {"passed": True, "errors": [], "warnings": []}

    monkeypatch.setattr(image_client, "submit_image", forbidden_submit)
    monkeypatch.setattr(image_client, "download_image", lambda *_args, **_kwargs: asyncio.sleep(0, result=image.getvalue()))
    monkeypatch.setattr(geo_factory, "visual_qa", qa_ok)
    asset = asyncio.run(geo_factory._generate_image_component(
        job={**job, "resolution": "1k"},
        component={"component_id": "professional_poster:image:professional_poster", "channel": "professional_poster", "kind": "image", "slot": {"slot": "professional_poster", "size": "3:4"}},
        geo=geo, content={"title": "真实证据"}, owner_key="u78130",
    ))
    assert asset and posts == 0
    assert str(asset["url_stored"]).startswith("private://marketing-materials/")
    assert "/uploads/marketing-materials/" not in str(asset["url_stored"])
    private_bytes, private_mime = material_storage.read_material_public_reference(str(asset["url_stored"]))
    assert private_mime == "image/png"
    _assert_same_image_pixels(private_bytes, image.getvalue())
    recovered = marketing_db.get_attempt(int(attempt["id"]))
    assert recovered["status"] == "succeeded" and recovered["materialization_state"] == "materialized"
    assert len(marketing_db.list_attempts(int(job["id"]))) == 1


def test_revocation_after_provider_success_closes_attempt_without_second_post(monkeypatch):
    geo = _geo()
    job = _job(geo)
    attempt = marketing_db.add_attempt(
        job_id=int(job["id"]), component_id="professional_poster:image:professional_poster",
        attempt_no=1, status="running", submit_state="submitted", poll_state="succeeded",
        provider_task_id="paid-before-revoke",
        provider_result={"image_url": "https://cdn.apimart.ai/paid-before-revoke.png"},
        materialization_state="pending",
    )
    posts = 0
    downloads = 0

    async def forbidden_submit(*_args, **_kwargs):
        nonlocal posts
        posts += 1
        raise AssertionError("durable provider success must never submit again")

    async def forbidden_download(*_args, **_kwargs):
        nonlocal downloads
        downloads += 1
        raise AssertionError("revocation must be checked before provider download")

    monkeypatch.setattr(image_client, "submit_image", forbidden_submit)
    monkeypatch.setattr(image_client, "download_image", forbidden_download)
    with pytest.raises(image_client.LiveAuthorityRejected, match="assignment_revoked"):
        asyncio.run(geo_factory._generate_image_component(
            job={**job, "resolution": "1k"},
            component={
                "component_id": "professional_poster:image:professional_poster",
                "channel": "professional_poster", "kind": "image",
                "slot": {"slot": "professional_poster", "size": "3:4"},
            },
            geo=geo, content={"title": "真实证据"}, owner_key="u78130",
            provider_guard=lambda: (_ for _ in ()).throw(
                image_client.LiveAuthorityRejected("assignment_revoked")
            ),
        ))
    closed = marketing_db.get_attempt(int(attempt["id"]))
    assert posts == 0 and downloads == 0
    assert closed["status"] == "failed"
    assert closed["submit_state"] == "submitted" and closed["poll_state"] == "succeeded"
    assert closed["materialization_state"] == "failed"
    assert closed["safety_flags_jsonb"] == [{"code": "live_authority_revoked_after_provider"}]


def test_revocation_before_vision_post_converges_attempt_job_and_charge(monkeypatch):
    import httpx

    overview, identity, charge, claim = _organization_charge(monkeypatch)
    geo = _geo(actor_snapshot={
        "organization_id": int(identity.organization_id),
        "actor_kind": str(identity.actor_kind),
        "membership_id": int(identity.membership_id),
        "membership_version": int(identity.membership_version),
        "assignment_version": int(identity.assignment_version),
        "organization_authority_version": int(identity.organization_authority_version),
    })
    job = _job(geo, user_id=int(identity.authenticated_user_id))
    marketing_db.update_job(
        int(job["id"]), status="generating", billing_ref=f"org:{int(charge['id'])}",
        cost_points=int(charge["estimated_points"]),
    )
    image = io.BytesIO()
    Image.new("RGB", (768, 1024), "white").save(image, format="PNG")
    image_posts = 0
    vision_posts = 0

    async def local_copy(*_args, **_kwargs):
        return ({
            "title": "先看真实诊断", "body": "没有数据就明确留白",
            "cta": "领取诊断", "tags": ["#GEO"],
        }, {"passed": True, "errors": [], "warnings": []})

    async def submit(*_args, before_request=None, **_kwargs):
        nonlocal image_posts
        if before_request:
            guarded = before_request()
            if asyncio.iscoroutine(guarded):
                await guarded
        image_posts += 1
        return {"state": "submitted", "provider_task_id": "paid-before-vision-revoke"}

    async def poll(_task_id, before_request=None, **_kwargs):
        if before_request:
            guarded = before_request()
            if asyncio.iscoroutine(guarded):
                await guarded
        return {"state": "succeeded", "image_url": "https://cdn.apimart.ai/paid-before-vision-revoke.png"}

    async def download(_url):
        # Download is locally guarded first. Revoke in the exact gap after the
        # paid image result is durable but before the vision provider guard.
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE organizations SET status='suspended',suspended_at=NOW(),authority_version=authority_version+1 WHERE id=%s",
                (int(overview["id"]),),
            )
            conn.commit()
        finally:
            conn.close()
        return image.getvalue()

    async def forbidden_vision_post(*_args, **_kwargs):
        nonlocal vision_posts
        vision_posts += 1
        raise AssertionError("vision POST must be fenced by live authority")

    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-leave-process")
    monkeypatch.setattr(geo_factory, "generate_channel_content", local_copy)
    monkeypatch.setattr(image_client, "submit_image", submit)
    monkeypatch.setattr(image_client, "poll_image_task", poll)
    monkeypatch.setattr(image_client, "download_image", download)
    monkeypatch.setattr(httpx.AsyncClient, "post", forbidden_vision_post)
    ctx = {
        "job_id": int(job["id"]), "billing_kind": "organization",
        "organization_charge_id": int(charge["id"]), "claim_token": str(claim["claim_token"]),
        "actual_points": int(charge["estimated_points"]),
    }
    first = asyncio.run(geo_factory.execute_geo_package_job(ctx))
    second = asyncio.run(geo_factory.execute_geo_package_job(ctx))
    assert first["status"] == "partial_success"
    assert second["status"] in {"succeeded", "partial_success"}
    assert image_posts == 1 and vision_posts == 0
    attempts = marketing_db.list_attempts(int(job["id"]))
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["poll_state"] == "succeeded"
    assert attempts[0]["materialization_state"] == "failed"
    final_job = marketing_db.get_job(int(job["id"]))
    assert final_job["status"] == "succeeded"
    assert final_job["error_summary"] == "partial_free_release"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM organization_charge_links WHERE id=%s", (int(charge["id"]),))
        assert cur.fetchone()["status"] == "committed"
        cur.execute("SELECT status FROM organization_work_outbox WHERE charge_link_id=%s", (int(charge["id"]),))
        assert cur.fetchone()["status"] == "succeeded"
    finally:
        conn.close()


def test_manual_unknown_success_worker_reuses_attempt_without_submit(monkeypatch):
    geo = _geo()
    job = _job(geo)
    marketing_db.update_job(int(job["id"]), status="generating")
    attempt = marketing_db.add_attempt(
        job_id=int(job["id"]), component_id="professional_poster:image:professional_poster",
        attempt_no=1, status="running", submit_state="outcome_unknown", poll_state="not_started",
        resolution_state="manual_required",
    )
    marketing_db.resolve_unknown_attempt(
        attempt_id=int(attempt["id"]), job_id=int(job["id"]), resolution="succeeded",
        provider_task_id="manual-task", image_url="https://cdn.apimart.ai/manual-task.png",
        operator_user_id=1, note="provider console verified",
    )
    posts = 0
    image = io.BytesIO()
    Image.new("RGB", (768, 1024), "white").save(image, format="PNG")

    async def forbidden_submit(*_args, **_kwargs):
        nonlocal posts
        posts += 1
        raise AssertionError("manual success must not submit")

    async def content(*_args, **_kwargs):
        return ({"title": "真实诊断", "body": "先看清现状", "cta": "领取体验", "source_note": "未引用诊断数字"}, {"passed": True, "errors": [], "warnings": []})

    async def qa_ok(*_args, **_kwargs):
        return {"passed": True, "errors": [], "warnings": []}

    monkeypatch.setattr(image_client, "submit_image", forbidden_submit)
    monkeypatch.setattr(image_client, "download_image", lambda *_args, **_kwargs: asyncio.sleep(0, result=image.getvalue()))
    monkeypatch.setattr(geo_factory, "generate_channel_content", content)
    monkeypatch.setattr(geo_factory, "visual_qa", qa_ok)
    monkeypatch.setattr(geo_factory, "claim_evidence_qa", lambda *_args, **_kwargs: {"passed": True, "errors": [], "warnings": []})
    result = asyncio.run(geo_factory.recover_reconciled_geo_job(int(job["id"])))
    assert result["status"] == "succeeded" and posts == 0
    assert len(marketing_db.list_attempts(int(job["id"]))) == 1


def test_qr_reference_survives_process_restart_on_persistent_upload_root(monkeypatch, tmp_path):
    qrcode = pytest.importorskip("qrcode")
    persistent = tmp_path / "mounted-uploads-private"
    monkeypatch.setenv("MARKETING_PRIVATE_UPLOAD_ROOT", str(persistent))
    buffer = io.BytesIO()
    qrcode.make("https://example.test/persistent-qr").save(buffer, format="PNG")
    stored = material_storage.save_qr_reference("u-restart", buffer.getvalue())
    code = (
        "from services.marketing.material_storage import qr_reference_data_uri\n"
        f"uri,data=qr_reference_data_uri('u-restart',{stored['reference_id']!r},{stored['sha256']!r})\n"
        "ok = data and uri.startswith('data:image/png;base64,')\n"
        "print(f'ok:{len(data)}' if ok else 'bad',flush=True)\n"
    )
    env = os.environ.copy()
    env["MARKETING_PRIVATE_UPLOAD_ROOT"] = str(persistent)
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=str(Path.cwd()), env=env,
        capture_output=True, text=True, timeout=20, check=True,
    )
    # 字节长度断言:跨进程重启后读回的字节必须与原保存长度一致(不得只验前缀)
    assert completed.stdout.strip() == f"ok:{stored['size_bytes']}"


def test_confirm_rechecks_live_member_assignment_and_writes_nothing_after_revoke(monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from api.marketing_material_api import router
    from db.organization_db import resolve_identity
    from middleware.organization_guard import OrganizationGuardMiddleware

    overview, owner, _charge, _claim = _organization_charge(monkeypatch, claim_now=False)
    member_id = 600000 + (uuid4().int % 200000)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users(id,username,display_name,phone,email) VALUES (%s,%s,'素材员工',%s,%s)",
            (member_id, f"material-member-{member_id}", f"15{member_id:09d}"[-11:], f"material-member-{member_id}@example.test"),
        )
        cur.execute("INSERT INTO user_roles(user_id,role_id) SELECT %s,id FROM roles WHERE name='user' LIMIT 1", (member_id,))
        cur.execute(
            "SELECT id FROM organization_roles WHERE organization_id=%s AND NOT is_owner_role AND NOT is_system_role ORDER BY id LIMIT 1",
            (int(overview["id"]),),
        )
        role_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO organization_memberships(organization_id,user_id,role_id,status,is_owner) VALUES (%s,%s,%s,'active',FALSE) RETURNING id",
            (int(overview["id"]), member_id, role_id),
        )
        membership_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO brands(name,company_name,industry,owner_user_id) VALUES (%s,%s,%s,%s) RETURNING id",
            (f"权限客户-{member_id}", "权限客户", "企业服务", int(owner.principal_user_id)),
        )
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO organization_brand_assignments(organization_id,membership_id,brand_id,status,assigned_by_user_id,reason,request_id) "
            "VALUES (%s,%s,%s,'active',%s,'test assignment',%s)",
            (int(overview["id"]), membership_id, brand_id, int(owner.authenticated_user_id), f"assign-{uuid4().hex}"),
        )
        conn.commit()
    finally:
        conn.close()
    live_member = resolve_identity(member_id, request_id=f"material-confirm-{uuid4().hex}")
    geo = _geo(actor_snapshot={
        "organization_id": int(overview["id"]), "actor_kind": "member",
        "membership_id": membership_id, "membership_version": int(live_member.membership_version),
        "assignment_version": int(live_member.assignment_version),
    })
    job = _job(geo, user_id=member_id)
    marketing_db.update_job(int(job["id"]), status="succeeded")
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(image, format="PNG")
    stored = material_storage.save_material_image(
        f"u{member_id}", image.getvalue(), "member.png", private=True,
    )
    asset = marketing_db.add_asset(
        job_id=int(job["id"]), asset_kind="bundle_item", bundle_slot="professional_poster:image:professional_poster",
        url_stored=stored["public_url"], rights_confirmed=0,
    )
    unassigned_job = _job(geo, user_id=member_id)
    marketing_db.update_job(int(unassigned_job["id"]), status="succeeded")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO brands(name,company_name,industry,owner_user_id) VALUES (%s,%s,%s,%s) RETURNING id",
            (f"未分配客户-{member_id}", "未分配客户", "企业服务", int(owner.principal_user_id)),
        )
        unassigned_brand_id = int(cur.fetchone()["id"])
        cur.execute(
            "UPDATE marketing_material_jobs SET brand_id=%s WHERE id=%s",
            (unassigned_brand_id, int(unassigned_job["id"])),
        )
        conn.commit()
    finally:
        conn.close()
    unassigned_asset = marketing_db.add_asset(
        job_id=int(unassigned_job["id"]), asset_kind="bundle_item",
        bundle_slot="professional_poster:image:professional_poster",
        url_stored=stored["public_url"], rights_confirmed=0,
    )
    app = FastAPI()
    app.add_middleware(OrganizationGuardMiddleware)

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.user = {"id": member_id, "user_id": member_id, "is_admin": False}
        return await call_next(request)

    app.include_router(router)
    with TestClient(app) as client:
        allowed = client.get(f"/api/marketing/materials/{int(asset['id'])}/download")
        unassigned = client.get(f"/api/marketing/materials/{int(unassigned_asset['id'])}/download")
        member_reconcile = client.post(
            f"/api/marketing/jobs/{int(job['id'])}/attempts/1/reconcile-provider",
            json={"resolution": "failed", "note": "member must never reconcile"},
        )
    assert allowed.status_code == 200
    _assert_same_image_pixels(allowed.content, image.getvalue())
    assert unassigned.status_code in {403, 404}
    assert member_reconcile.status_code == 403
    assert member_reconcile.json()["detail"]["code"] == "ORG_ROUTE_NOT_CLASSIFIED"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE organization_brand_assignments SET status='revoked',revoked_at=NOW(),version=version+1 WHERE organization_id=%s AND membership_id=%s AND brand_id=%s",
            (int(overview["id"]), membership_id, brand_id),
        )
        cur.execute("UPDATE organization_memberships SET assignment_version=assignment_version+1 WHERE id=%s", (membership_id,))
        conn.commit()
    finally:
        conn.close()
    with TestClient(app) as client:
        denied = client.post(f"/api/marketing/materials/{int(asset['id'])}/confirm")
        denied_download = client.get(f"/api/marketing/materials/{int(asset['id'])}/download")
    assert denied.status_code == 403
    assert denied_download.status_code in {403, 404}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT rights_confirmed FROM marketing_material_assets WHERE id=%s", (int(asset["id"]),))
        assert int(cur.fetchone()["rights_confirmed"]) == 0
    finally:
        conn.close()


def test_owner_confirm_rechecks_organization_generation_and_writes_nothing(monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from api.marketing_material_api import router

    overview, owner, _charge, _claim = _organization_charge(monkeypatch, claim_now=False)
    geo = _geo(actor_snapshot={
        "organization_id": int(overview["id"]), "actor_kind": "owner",
        "membership_id": int(owner.membership_id),
        "membership_version": int(owner.membership_version),
        "assignment_version": int(owner.assignment_version),
        "organization_authority_version": int(owner.organization_authority_version),
    })
    job = _job(geo, user_id=int(owner.authenticated_user_id))
    marketing_db.update_job(int(job["id"]), status="generating", error_summary="")
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(image, format="PNG")
    stored = material_storage.save_material_image(
        f"u{int(owner.authenticated_user_id)}", image.getvalue(), "owner.png", private=True,
    )
    asset = marketing_db.add_asset(
        job_id=int(job["id"]), asset_kind="bundle_item",
        bundle_slot="professional_poster:image:professional_poster",
        url_stored=stored["public_url"], rights_confirmed=0,
    )
    app = FastAPI()

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.user = {
            "id": int(owner.authenticated_user_id),
            "user_id": int(owner.authenticated_user_id), "is_admin": False,
        }
        request.state.organization_identity = owner
        request.state.organization_request_id = "owner-confirm-generation-test"
        return await call_next(request)

    app.include_router(router)
    for unsettled_status, unsettled_error in (
        ("generating", ""),
        ("generating", "billing_commit_pending"),
        ("generating", "billing_release_pending"),
        ("generating", "provider_resolution_pending"),
        ("failed", "all_components_failed"),
        ("blocked", "forbidden_content"),
    ):
        marketing_db.update_job(
            int(job["id"]), status=unsettled_status, error_summary=unsettled_error,
        )
        with TestClient(app) as client:
            unsettled_download = client.get(f"/api/marketing/materials/{int(asset['id'])}/download")
            unsettled_confirm = client.post(f"/api/marketing/materials/{int(asset['id'])}/confirm")
        assert unsettled_download.status_code == 409
        assert unsettled_download.content != image.getvalue()
        assert unsettled_confirm.status_code == 409
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT rights_confirmed FROM marketing_material_assets WHERE id=%s", (int(asset["id"]),))
        assert int(cur.fetchone()["rights_confirmed"]) == 0
    finally:
        conn.close()
    marketing_db.update_job(int(job["id"]), status="succeeded", error_summary="")
    with TestClient(app) as client:
        allowed = client.get(f"/api/marketing/materials/{int(asset['id'])}/download")
    assert allowed.status_code == 200
    _assert_same_image_pixels(allowed.content, image.getvalue())
    marketing_db.update_job(
        int(job["id"]), status="succeeded", error_summary="partial_free_release",
    )
    with TestClient(app) as client:
        partial_allowed = client.get(f"/api/marketing/materials/{int(asset['id'])}/download")
    assert partial_allowed.status_code == 200
    _assert_same_image_pixels(partial_allowed.content, image.getvalue())
    marketing_db.update_job(int(job["id"]), status="succeeded", error_summary="")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE organizations SET authority_version=authority_version+1 WHERE id=%s",
            (int(overview["id"]),),
        )
        conn.commit()
    finally:
        conn.close()
    with TestClient(app) as client:
        denied = client.post(f"/api/marketing/materials/{int(asset['id'])}/confirm")
        denied_download = client.get(f"/api/marketing/materials/{int(asset['id'])}/download")
    assert denied.status_code == 403
    assert denied_download.status_code in {403, 404}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT rights_confirmed FROM marketing_material_assets WHERE id=%s", (int(asset["id"]),))
        assert int(cur.fetchone()["rights_confirmed"]) == 0
    finally:
        conn.close()


def test_delivery_rejects_composite_capability_generation_change(monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException
    from api import marketing_material_api
    from services.organization_contract import IdentityContext

    live = IdentityContext(
        request_id="delivery-generation-test",
        authenticated_user_id=99101,
        principal_user_id=99100,
        payer_user_id=99100,
        actor_kind="member",
        organization_id=55101,
        actor_user_id=99101,
        membership_id=77101,
        membership_version=8,
        capability_version=10,
        assignment_version=9,
        organization_authority_version=7,
        capabilities=frozenset({"materials.read_assigned"}),
    )
    request = SimpleNamespace(state=SimpleNamespace(
        user={"id": 99101, "user_id": 99101},
        organization_identity=live,
        organization_request_id="delivery-generation-test",
    ))
    asset = {
        "brand_id": None,
        "job_status": "succeeded",
        "job_error_summary": "",
        "is_final": True,
        "publish_allowed": 1,
        "input_fields_jsonb": {"_geo": {
            "actor_snapshot": {
                "organization_id": 55101,
                "actor_kind": "member",
                "membership_id": 77101,
                "membership_version": 8,
                "assignment_version": 9,
                "organization_authority_version": 7,
                # Only capability generation changed from 9 to 10.
                "authority_version": "7:8:9:9",
            },
            "evidence": {"source_type": "none"},
        }},
    }
    monkeypatch.setattr("db.organization_db.resolve_identity", lambda *_args, **_kwargs: live)
    with pytest.raises(HTTPException, match="组织或成员权限代际已变化") as caught:
        marketing_material_api._require_live_delivery_authority(request, asset)
    assert caught.value.status_code == 403


def test_expired_worker_lease_cannot_settle_but_settlement_recovery_converges(monkeypatch):
    _overview, identity, charge, claim = _organization_charge(monkeypatch)
    from services.organization_billing import mark_external_side_effect_started

    # Reconciliation may settle only from durable proof that the paid external
    # boundary was crossed while this worker still owned a live claim.
    mark_external_side_effect_started(
        charge_link_id=int(charge["id"]),
        claim_token=str(claim["claim_token"]),
        lease_seconds=60,
    )
    geo = _geo()
    job = _job(geo, user_id=int(identity.authenticated_user_id))
    marketing_db.update_job(
        int(job["id"]), status="generating", billing_ref=f"org:{int(charge['id'])}",
        cost_points=int(charge["estimated_points"]),
    )
    asset = marketing_db.add_asset(
        job_id=int(job["id"]), asset_kind="copy", bundle_slot="professional_poster:copy",
        content_text='{"title":"已完成但待结算"}',
    )
    points_before = _wallet_points(int(identity.payer_user_id))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 second' WHERE charge_link_id=%s", (int(charge["id"]),))
        cur.execute("UPDATE organization_charge_links SET lease_until=NOW()-INTERVAL '1 second' WHERE id=%s", (int(charge["id"]),))
        conn.commit()
    finally:
        conn.close()
    stale_ctx = {
        "billing_kind": "organization", "organization_charge_id": int(charge["id"]),
        "claim_token": str(claim["claim_token"]), "actual_points": int(charge["estimated_points"]),
    }
    blocked = asyncio.run(geo_factory._settle_geo(job, stale_ctx, [asset], 1, external_started=True))
    assert blocked["error"] == "billing_commit_pending"
    assert _wallet_points(int(identity.payer_user_id)) == points_before

    recovered = asyncio.run(geo_factory._settle_geo(
        marketing_db.get_job(int(job["id"])),
        {**stale_ctx, "claim_token": "", "settlement_only": True},
        [asset], 1, external_started=True,
    ))
    assert recovered["status"] == "succeeded"
    # Reservation already moved the points out of the spendable balance;
    # commit must not deduct them a second time.
    assert _wallet_points(int(identity.payer_user_id)) == points_before
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status,actual_points FROM organization_charge_links WHERE id=%s", (int(charge["id"]),))
        row = cur.fetchone()
        assert row["status"] == "committed" and int(row["actual_points"]) == int(charge["estimated_points"])
    finally:
        conn.close()


def test_real_subprocess_is_force_killed_after_commit_and_recovery_is_exactly_once(monkeypatch):
    _overview, identity, charge, claim = _organization_charge(monkeypatch)
    from services.organization_billing import mark_external_side_effect_started
    mark_external_side_effect_started(
        charge_link_id=int(charge["id"]), claim_token=str(claim["claim_token"]), lease_seconds=60,
    )
    child_code = (
        "import asyncio,time\n"
        "from services.organization_billing import settle_charge\n"
        # 子进程扮演持有真实 claim 的活跃 worker：settle 必须绑定存活租约
        # （外部独立审核裁决 2026-07-23，claim_token 必填）。
        f"asyncio.run(settle_charge(charge_link_id={int(charge['id'])},actual_points={int(charge['estimated_points'])},result_payload={{'kill_test':True}},claim_token={str(claim['claim_token'])!r}))\n"
        "print('COMMITTED',flush=True)\n"
        "time.sleep(60)\n"
    )
    env = os.environ.copy()
    env["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    process = subprocess.Popen(
        [sys.executable, "-c", child_code], cwd=str(Path.cwd()), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    line = process.stdout.readline().strip()
    if line != "COMMITTED":
        stderr = process.stderr.read() if process.stderr else ""
        process.kill()
        raise AssertionError(f"child did not commit: {line} {stderr}")
    process.kill()  # Windows TerminateProcess: no finally/atexit, kill-9 equivalent.
    process.wait(timeout=10)
    points_after_kill = _wallet_points(int(identity.payer_user_id))
    # kill-9 恢复重放：显式 reconciliation 专用入口（外部独立审核裁决
    # 2026-07-23：不再有"省略 token 即恢复"的隐式模式）。
    from services.organization_billing import settle_charge_via_reconciliation
    replay = asyncio.run(settle_charge_via_reconciliation(
        charge_link_id=int(charge["id"]), actual_points=int(charge["estimated_points"]),
        result_payload={"kill_test": True},
        recovery_identity="pytest:kill9-subprocess-replay",
    ))
    assert replay.get("replayed") is True
    assert _wallet_points(int(identity.payer_user_id)) == points_after_kill
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status,charge_tx_id FROM organization_charge_links WHERE id=%s", (int(charge["id"]),))
        row = cur.fetchone()
        assert row["status"] == "committed" and row["charge_tx_id"] is not None
        cur.execute("SELECT COUNT(*)::int AS count FROM point_transactions WHERE id=%s", (int(row["charge_tx_id"]),))
        assert int(cur.fetchone()["count"]) == 1
    finally:
        conn.close()


def test_real_fastapi_to_freeze_worker_fake_provider_and_settlement_full_chain(monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from api.marketing_material_api import router

    user_id = 900000 + (uuid4().int % 90000)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS customer_credit_freezes (id BIGSERIAL PRIMARY KEY,customer_user_id INTEGER,task_ref TEXT,status TEXT NOT NULL DEFAULT 'frozen')"
        )
        cur.execute(
            "INSERT INTO users(id,username,display_name,phone,email) VALUES (%s,%s,'FastAPI GEO','fastapi-phone-%s',%s)",
            (user_id, f"fastapi-geo-{user_id}", user_id, f"fastapi-{user_id}@example.test"),
        )
        cur.execute("INSERT INTO user_roles(user_id,role_id) SELECT %s,id FROM roles WHERE name='user' LIMIT 1", (user_id,))
        cur.execute(
            "INSERT INTO user_wallets(user_id,paid_points,total_recharged,agent_level) VALUES (%s,2000000,2000000,1)",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()

    generated = io.BytesIO()
    Image.new("RGB", (768, 1024), "white").save(generated, format="PNG")
    _ProviderHandler.image_bytes = generated.getvalue()
    _ProviderHandler.posts = 0
    _ProviderHandler.delay = 0
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    monkeypatch.setenv("APIMART_API_KEY", "fake-key")
    monkeypatch.setenv("MARKETING_IMAGE_GENERATION_URL", f"http://127.0.0.1:{provider.server_port}/generate")
    monkeypatch.setenv("MARKETING_IMAGE_TASK_URL", f"http://127.0.0.1:{provider.server_port}/tasks/{{task_id}}")
    monkeypatch.setenv("MARKETING_IMAGE_DOWNLOAD_HOSTS", "127.0.0.1")
    monkeypatch.setenv("MARKETING_IMAGE_ALLOW_HTTP", "true")
    monkeypatch.setenv("MARKETING_IMAGE_ALLOW_PRIVATE_HOSTS", "127.0.0.1")

    async def no_remote_copy(*_args, **_kwargs):
        return None

    async def passed_visual(image_bytes, **_kwargs):
        assert image_bytes == _ProviderHandler.image_bytes
        return {"passed": True, "errors": [], "warnings": [], "width": 768, "height": 1024, "ocr_text": ""}

    monkeypatch.setattr("tools.multi_llm_caller.call_llm_with_fallback", no_remote_copy)
    monkeypatch.setattr(geo_factory, "visual_qa", passed_visual)
    app = FastAPI()

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.user = {"id": user_id, "user_id": user_id, "is_admin": False}
        return await call_next(request)

    app.include_router(router)
    request_id = f"fastapi-full-{uuid4().hex}"
    payload = {
        "request_id": request_id,
        "brief": "向服务商老板推广一次真实 GEO 诊断，只邀请他领取诊断",
        "quick_task": "promote_geo",
        "strategy": {
            "audience": "服务商老板", "audience_status": "客户开始问 AI", "action_resistance": "担心没有证据",
            "human_problem": "AI 为什么没有提到客户品牌", "core_angle": "先看真实诊断",
            "single_value": "用证据链讲清 GEO", "evidence_statement": "没有证据不写数字", "single_action": "领取一次 GEO 诊断",
        },
        "teacher_id": "shu", "teacher_version": "1.0.0", "channels": ["professional_poster"],
        "brand_id": None, "evidence": {"source_type": "none", "diagnosis_id": None},
        "contact": {"mode": "none", "text": "", "qr_reference": None},
        "associate_recent_trend": False, "resolution": "1k", "only_components": [], "parent_job_id": None,
    }
    try:
        with TestClient(app) as client:
            first = client.post("/api/marketing/content-packages", json=payload)
            assert first.status_code == 200, first.text
            job_id = int(first.json()["job_id"])
            replay = client.post("/api/marketing/content-packages", json=payload)
            assert replay.status_code == 200 and int(replay.json()["job_id"]) == job_id
            state = None
            for _ in range(80):
                response = client.get(f"/api/marketing/jobs/{job_id}")
                assert response.status_code == 200, response.text
                state = response.json()["job"]
                if state["status"] not in {"pending", "generating"}:
                    break
                time.sleep(0.05)
            assert state and state["status"] == "succeeded", state
            assert len(state["assets"]) == 2
            conflict = client.post("/api/marketing/content-packages", json={**payload, "brief": payload["brief"] + " 改变"})
            assert conflict.status_code == 409
    finally:
        provider.shutdown()
        provider.server_close()
    assert _ProviderHandler.posts == 1
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM marketing_material_jobs WHERE id=%s", (job_id,))
        assert cur.fetchone()["status"] == "succeeded"
        cur.execute("SELECT COUNT(*)::int AS count FROM marketing_material_generation_attempts WHERE job_id=%s AND status='succeeded'", (job_id,))
        assert int(cur.fetchone()["count"]) == 1
        cur.execute("SELECT COUNT(*)::int AS count FROM point_transactions WHERE user_id=%s AND type='consume'", (user_id,))
        assert int(cur.fetchone()["count"]) == 1
    finally:
        conn.close()
