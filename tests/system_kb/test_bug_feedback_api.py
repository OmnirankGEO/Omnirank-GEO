"""问题反馈 API 测试（离线，不调用真实 OSS/通知）。"""
import types
from io import BytesIO

import pytest
from fastapi import BackgroundTasks, UploadFile


def _fake_request(user):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


def _upload_file(content: bytes = b"fake-image", content_type: str = "image/png") -> UploadFile:
    return UploadFile(
        filename="screen.png",
        file=BytesIO(content),
        headers={"content-type": content_type},
    )


@pytest.mark.asyncio
async def test_bug_feedback_allows_normal_user_and_saves_identity(monkeypatch):
    from api.faq_api import FeedbackRequest, api_submit_feedback
    import api.faq_api as faq_api

    saved = {}

    def fake_create_feedback(**kwargs):
        saved.update(kwargs)
        return {"id": 77, "status": "new"}

    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _user_id: 0)
    monkeypatch.setattr(faq_api, "create_feedback", fake_create_feedback)
    monkeypatch.setattr(faq_api, "_notify_admins_for_bug", lambda *args, **kwargs: None)

    body = FeedbackRequest(
        client_id="bug_normal_001",
        kind="bug",
        message="这个页面按钮点了没有反应",
    )
    bg = BackgroundTasks()
    result = await api_submit_feedback(_fake_request({"id": 10, "is_admin": False}), body, bg)

    assert result == {"id": 77, "status": "new"}
    assert saved["kind"] == "bug"
    assert saved["submitter_identity"] == "normal_user"
    assert saved["submitter_agent_level"] == 0
    # AI Ops hook 走 BackgroundTasks(响应后线程池执行,不阻塞事件循环 · 包B P3)
    assert len(bg.tasks) == 1


@pytest.mark.asyncio
async def test_bug_feedback_allows_agent_and_saves_ai_context(monkeypatch):
    from api.faq_api import FeedbackRequest, api_submit_feedback
    import api.faq_api as faq_api

    saved = {}

    def fake_create_feedback(**kwargs):
        saved.update(kwargs)
        return {"id": 88, "status": "new"}

    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _user_id: 1)
    monkeypatch.setattr(faq_api, "create_feedback", fake_create_feedback)
    monkeypatch.setattr(faq_api, "_notify_admins_for_bug", lambda *args, **kwargs: None)

    body = FeedbackRequest(
        client_id="bug_agent_001",
        kind="bug",
        message="报价页确认按钮点不了",
        urgency="high",
        screenshot_url="feedback/10/shot.png",
        ai_answer="请先检查必填字段。",
    )
    result = await api_submit_feedback(_fake_request({"id": 10, "is_admin": False}), body, BackgroundTasks())

    assert result == {"id": 88, "status": "new"}
    assert saved["kind"] == "bug"
    assert saved["screenshot_url"] == "feedback/10/shot.png"
    assert saved["ai_answer"] == "请先检查必填字段。"
    assert saved["submitter_identity"] == "agent"
    assert saved["submitter_agent_level"] == 1


@pytest.mark.asyncio
async def test_bug_screenshot_upload_allows_normal_user(monkeypatch):
    from api.faq_api import api_upload_feedback_screenshot
    import services.oss_service as oss_service

    monkeypatch.setattr(oss_service, "build_bug_feedback_key", lambda user_id, ext: f"feedback/{user_id}/shot.{ext}")
    monkeypatch.setattr(oss_service, "upload_bug_feedback_image", lambda key, raw, content_type: key)

    result = await api_upload_feedback_screenshot(
        _fake_request({"id": 10, "is_admin": False, "agent_level": 0}),
        _upload_file(),
    )

    assert result == {"ok": True, "screenshot_url": "feedback/10/shot.png"}


@pytest.mark.asyncio
async def test_bug_screenshot_upload_stores_private_key(monkeypatch):
    from api.faq_api import api_upload_feedback_screenshot
    import services.oss_service as oss_service

    monkeypatch.setattr(oss_service, "build_bug_feedback_key", lambda user_id, ext: f"feedback/{user_id}/shot.{ext}")
    monkeypatch.setattr(oss_service, "upload_bug_feedback_image", lambda key, raw, content_type: key)

    result = await api_upload_feedback_screenshot(
        _fake_request({"id": 10, "is_admin": False, "agent_level": 1}),
        _upload_file(),
    )

    assert result == {"ok": True, "screenshot_url": "feedback/10/shot.png"}


def test_is_agent_or_admin_does_not_treat_paid_wallet_as_agent(monkeypatch):
    import api.faq_api as faq_api

    monkeypatch.setattr(faq_api, "_fetch_agent_level", lambda _user_id: 0)

    assert faq_api._is_agent_or_admin({"id": 10, "is_admin": False, "agent_level": 0}) is False
    assert faq_api._is_agent_or_admin({"id": 10, "is_admin": False, "agent_level": 1}) is True
    assert faq_api._is_agent_or_admin({"id": 10, "is_admin": True, "agent_level": 0}) is True


def test_feedback_signed_url_default_is_long_lived(monkeypatch):
    from services.oss_service import feedback_signed_url_ttl_seconds

    monkeypatch.delenv("OSS_FEEDBACK_URL_TTL_SECONDS", raising=False)

    assert feedback_signed_url_ttl_seconds() >= 10 * 365 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_admin_feedback_list_filters_kind_and_signs_bug_screenshot(monkeypatch):
    from api.faq_api import admin_list_feedback
    import api.faq_api as faq_api

    seen = {}

    def fake_list_feedback(**kwargs):
        seen.update(kwargs)
        return [{
            "id": 5,
            "kind": "bug",
            "message": "发布失败",
            "screenshot_url": "feedback/10/shot.png",
            "ai_answer": "小榜判断需要人工处理。",
            "submitter_identity": "normal_user",
            "submitter_agent_level": 0,
        }]

    monkeypatch.setattr(faq_api, "list_feedback", fake_list_feedback)
    monkeypatch.setattr(
        "services.oss_service.generate_feedback_signed_url",
        lambda key, expires_seconds=None: f"https://signed.local/{key}?ttl={'default' if expires_seconds is None else expires_seconds}",
    )

    result = await admin_list_feedback(
        _fake_request({"id": 1, "is_admin": True}),
        kind="bug",
    )

    assert seen["kind"] == "bug"
    assert result["items"][0]["screenshot_key"] == "feedback/10/shot.png"
    assert result["items"][0]["screenshot_url"] == "https://signed.local/feedback/10/shot.png?ttl=default"
    assert result["items"][0]["ai_answer"] == "小榜判断需要人工处理。"
    assert result["items"][0]["submitter_identity"] == "normal_user"


@pytest.mark.asyncio
async def test_admin_feedback_counts_accepts_kind_and_urgency_filters(monkeypatch):
    from api.faq_api import admin_feedback_counts
    import api.faq_api as faq_api

    seen = {}

    def fake_counts(kind=None, urgency=None):
        seen["kind"] = kind
        seen["urgency"] = urgency
        return {"pending": 2, "read": 0, "done": 0, "closed": 0}

    monkeypatch.setattr(faq_api, "get_feedback_counts", fake_counts)

    result = await admin_feedback_counts(
        _fake_request({"id": 1, "is_admin": True}),
        kind="bug",
        urgency="high",
    )

    assert seen["kind"] == "bug"
    assert seen["urgency"] == "high"
    assert result["pending"] == 2


@pytest.mark.asyncio
async def test_admin_update_feedback_status_passes_admin_id(monkeypatch):
    from api.faq_api import UpdateFeedbackRequest, admin_update_feedback
    import api.faq_api as faq_api

    seen = {}

    def fake_update_feedback_status(feedback_id, *, status=None, admin_note=None, handled_by=None):
        seen.update({
            "feedback_id": feedback_id,
            "status": status,
            "admin_note": admin_note,
            "handled_by": handled_by,
        })
        return True

    monkeypatch.setattr(faq_api, "update_feedback_status", fake_update_feedback_status)

    result = await admin_update_feedback(
        42,
        _fake_request({"id": 7, "is_admin": True}),
        UpdateFeedbackRequest(status="done"),
    )

    assert result == {"ok": True}
    assert seen == {
        "feedback_id": 42,
        "status": "done",
        "admin_note": None,
        "handled_by": 7,
    }


@pytest.mark.asyncio
async def test_admin_update_feedback_status_accepts_user_id_claim(monkeypatch):
    from api.faq_api import UpdateFeedbackRequest, admin_update_feedback
    import api.faq_api as faq_api

    seen = {}

    def fake_update_feedback_status(feedback_id, *, status=None, admin_note=None, handled_by=None):
        seen.update({
            "feedback_id": feedback_id,
            "status": status,
            "admin_note": admin_note,
            "handled_by": handled_by,
        })
        return True

    monkeypatch.setattr(faq_api, "update_feedback_status", fake_update_feedback_status)

    result = await admin_update_feedback(
        43,
        _fake_request({"user_id": 8, "is_admin": True}),
        UpdateFeedbackRequest(status="read"),
    )

    assert result == {"ok": True}
    assert seen == {
        "feedback_id": 43,
        "status": "read",
        "admin_note": None,
        "handled_by": 8,
    }
