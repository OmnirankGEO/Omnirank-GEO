"""Both existing callers map a live-version conflict to recoverable 409, not 500."""
import asyncio
import importlib
import sys
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest

from services.geo_douyin.publish_command import PublishAttemptConflict


def conflict(*args, **kwargs):
    raise PublishAttemptConflict("等待已有结果", command_request_id="existing-command")


@pytest.fixture
def api_module(monkeypatch):
    # Only transport/projection is under test here. Avoid unrelated diagnosis_db's
    # import-time schema initialization; authorization is independently integrated.
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, "auth.brand_access", SimpleNamespace(require_brand_access=lambda *a, **kw: None))
        yield importlib.import_module("api.geo_image_note_api")


def test_http_handler_rolls_back_and_returns_existing_command(monkeypatch, api_module):
    api = api_module
    from db import connection
    from services.geo_douyin import publish_batch_core, contract_funding

    events = []
    conn = SimpleNamespace(cursor=lambda: object(), commit=lambda: events.append("commit"),
                           rollback=lambda: events.append("rollback"), close=lambda: events.append("close"))
    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    monkeypatch.setattr(api, "_user", lambda request: {"id": 3, "is_admin": False})
    monkeypatch.setattr(api, "_guard", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_resolve_identity", lambda request: {"payer_user_id": 3})
    monkeypatch.setattr(api, "image_note_daily_limit", lambda: 10)
    monkeypatch.setattr(contract_funding, "resolve_settlement_authority", lambda **kwargs: {"authority": "direct_freeze"})
    monkeypatch.setattr(publish_batch_core, "materialize_publish_batch", conflict)
    req = api.PublishBatchRequest(request_id=str(uuid.uuid4()), expected_total_price_points=260, items=[{
        "item_request_id": str(uuid.uuid4()), "geo_post_id": 3, "post_revision_id": 12,
        "prepared_artifact_id": "1", "manifest_hash": "m" * 64,
        "media_id": 991012, "expected_price_fingerprint": "publish-v1:fixture",
    }])
    with pytest.raises(HTTPException) as err:
        asyncio.run(api.api_image_note_publish_batch(req, SimpleNamespace()))
    assert err.value.status_code == 409
    assert err.value.detail["code"] == "PUBLISH_ATTEMPT_EXISTS"
    assert err.value.detail["command_id"] == "pubcmd_existing-command"
    assert err.value.detail["retryable"] is False
    assert any(action["id"] == "load_command" for action in err.value.detail["actions"])
    assert events == ["rollback", "close"]


def test_xiaobang_adapter_maps_same_conflict_to_existing_progress(monkeypatch, api_module):
    from services import xiaobang_publish_execute as adapter, xiaobang_channel_eligibility
    from services.xiaobang_intent import IntentError
    from services.geo_douyin import publish_batch_core
    from services.defensive_geo.xiaobang import compute_estimate

    monkeypatch.setattr(adapter, "frozen_execution_binding", lambda row: {"channel_option_id": "fixture"})
    monkeypatch.setattr(xiaobang_channel_eligibility, "resolve_channel_option",
                        lambda *args, **kwargs: SimpleNamespace(resolved=True, eligible=True))
    monkeypatch.setattr(compute_estimate, "frozen_estimate_amount", lambda row: 260)
    monkeypatch.setattr(adapter, "build_execution_items", lambda *args, **kwargs: [{"media_id": 991012}])
    monkeypatch.setattr(publish_batch_core, "materialize_publish_batch", conflict)
    with pytest.raises(IntentError) as err:
        adapter.execute_publish_image_note(object(), intent_row={}, receipt={"receipt_id": "r"},
                                          identity={"tenant_owner_user_id": 3}, settlement={}, daily_limit=10)
    assert err.value.http_status == 409 and err.value.code == "PUBLISH_ATTEMPT_EXISTS"
    assert err.value.next_action == "查看进度"
    assert err.value.detail["command_id"] == "pubcmd_existing-command"
