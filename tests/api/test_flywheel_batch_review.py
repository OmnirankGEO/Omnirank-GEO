"""媒体绑定候选批量审核端点(B 方案批量决策)回归测试。

批量端点是"AI 分组 + 人工一键确认整批"的落地:仍是人工触发的审核动作,
逐条容错(单条失败不拖垮整批),每条各自走单条 core 逻辑与审计。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


class _State:
    def __init__(self, user):
        self.user = user


class _Request:
    def __init__(self, user):
        self.state = _State(user)


def test_batch_review_route_registered():
    from api.media_entity_flywheel_api import router

    paths = [route.path for route in router.routes]
    assert "/api/admin/geo-placement-flywheel/media/binding-candidates/batch-review" in paths
    # 单条端点仍在(批量不是替代)
    assert "/api/admin/geo-placement-flywheel/media/binding-candidates/{candidate_id}/review" in paths


def test_batch_review_requires_admin():
    from api.media_entity_flywheel_api import (
        MediaBindingCandidateBatchReviewRequest,
        batch_review_media_binding_candidates,
    )

    req = MediaBindingCandidateBatchReviewRequest(candidate_ids=[1], decision="approve")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(batch_review_media_binding_candidates(req, _Request({"id": 2, "is_admin": False})))
    assert exc.value.status_code == 403


def test_batch_review_partial_failure_and_dedup(monkeypatch):
    from api import media_entity_flywheel_api as mod

    calls: list[tuple[int, str, str]] = []

    def fake_core(candidate_id, decision, note, operator_id):
        calls.append((candidate_id, decision, note))
        if candidate_id == 99:
            raise HTTPException(status_code=404, detail="候选绑定不存在")
        return {"status": "success", "candidate": {"id": candidate_id, "status": "approved"}}

    monkeypatch.setattr(mod, "_review_binding_candidate_core", fake_core)
    monkeypatch.setattr(mod, "init_media_entity_flywheel_tables", lambda: None)

    req = mod.MediaBindingCandidateBatchReviewRequest(
        candidate_ids=[7, 99, 7],  # 含重复 id,应去重
        decision="approve",
        review_note="",  # 空备注应自动生成默认备注
    )
    result = asyncio.run(
        mod.batch_review_media_binding_candidates(req, _Request({"user_id": 5, "is_admin": True}))
    )

    assert result["requested"] == 2  # 去重后 7、99
    assert [s["candidate_id"] for s in result["succeeded"]] == [7]
    # [P0-2 2026-08-15] 失败项契约扩了:除 candidate_id/error 外必须带媒体名与行业,
    #   否则跨行业一键通过时前端拿 id 映射不回名字,原因等于又丢了(工单 §二 P0-2)。
    #   这里不再用整字典相等锁死形状 —— 但也不放水:必需键逐个断言,且
    #   候选 99 本就不存在,media_name 必须是空串(不许编名字)。
    assert len(result["failed"]) == 1
    failure = result["failed"][0]
    assert failure["candidate_id"] == 99
    assert failure["error"] == "候选绑定不存在"
    assert set(failure) >= {
        "candidate_id", "error", "media_name", "entity_key", "industry_key", "media_source",
    }
    assert failure["media_name"] == ""
    assert result["production_takeover"] is False
    # 默认备注生成且传给每条 core
    assert all("批量人工确认通过" in note for _, _, note in calls)
    # 单条意外失败不拖垮整批:core 只被调 2 次(去重)
    assert len(calls) == 2


def test_batch_review_unexpected_error_isolated(monkeypatch):
    from api import media_entity_flywheel_api as mod

    def fake_core(candidate_id, decision, note, operator_id):
        if candidate_id == 1:
            raise RuntimeError("db blip")
        return {"status": "success", "candidate": {"id": candidate_id, "status": "rejected"}}

    monkeypatch.setattr(mod, "_review_binding_candidate_core", fake_core)
    monkeypatch.setattr(mod, "init_media_entity_flywheel_tables", lambda: None)

    req = mod.MediaBindingCandidateBatchReviewRequest(candidate_ids=[1, 2], decision="reject")
    result = asyncio.run(
        mod.batch_review_media_binding_candidates(req, _Request({"id": 1, "is_admin": True}))
    )
    assert [f["candidate_id"] for f in result["failed"]] == [1]
    assert result["failed"][0]["error"] == "RuntimeError"
    assert [s["candidate_id"] for s in result["succeeded"]] == [2]
