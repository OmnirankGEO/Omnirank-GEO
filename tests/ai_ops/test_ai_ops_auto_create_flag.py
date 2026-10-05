"""反馈自动立案开关(ai_ops.auto_create_from_feedback · DB flag 后台可控)测试。

包A(2026-07-03):原来只有 env AI_OPS_AUTO_CREATE_FROM_FEEDBACK(改一次要动生产 .env
+ 重启),迁成 DB flag 后老板在设置页可点。env 通道兼容保留(任一开启即生效)。
铁律:flag 读取 fail-soft——DB 抖动按关闭处理,绝不影响用户提交反馈。
"""
import pytest

from db import ai_ops_db as aiops_db
from services.ai_ops import task_service


def _clear_env(monkeypatch):
    monkeypatch.delenv("AI_OPS_AUTO_CREATE_FROM_FEEDBACK", raising=False)


def _task_count() -> int:
    return len(aiops_db.list_tasks(limit=50))


# ==========================================
# 默认全关:不建任务
# ==========================================

def test_default_off_no_task(clean_ai_ops, make_bug_feedback, monkeypatch):
    _clear_env(monkeypatch)
    fid = make_bug_feedback()
    task_service.maybe_create_ai_ops_task_for_feedback(fid, 'bug', 'new')
    assert _task_count() == 0


# ==========================================
# DB flag 开:自动立案(后台按钮通道)
# ==========================================

def test_db_flag_on_creates_task(clean_ai_ops, make_bug_feedback, monkeypatch):
    _clear_env(monkeypatch)
    aiops_db.set_policy('ai_ops.auto_create_from_feedback', {'enabled': True})
    fid = make_bug_feedback(message="登录后白屏")
    task_service.maybe_create_ai_ops_task_for_feedback(fid, 'bug', 'new')
    tasks = aiops_db.list_tasks(limit=10)
    assert len(tasks) == 1
    assert tasks[0]['kind'] == 'diagnose'
    assert tasks[0]['source_type'] == 'feedback'
    assert tasks[0]['feedback_id'] == fid


def test_db_flag_on_idempotent_same_feedback(clean_ai_ops, make_bug_feedback, monkeypatch):
    """同一反馈重复触发只立一案(task_key 幂等)。"""
    _clear_env(monkeypatch)
    aiops_db.set_policy('ai_ops.auto_create_from_feedback', {'enabled': True})
    fid = make_bug_feedback()
    task_service.maybe_create_ai_ops_task_for_feedback(fid, 'bug', 'new')
    task_service.maybe_create_ai_ops_task_for_feedback(fid, 'bug', 'new')
    assert _task_count() == 1


def test_db_flag_on_skips_non_bug_and_existing(clean_ai_ops, make_bug_feedback, monkeypatch):
    _clear_env(monkeypatch)
    aiops_db.set_policy('ai_ops.auto_create_from_feedback', {'enabled': True})
    fid = make_bug_feedback()
    task_service.maybe_create_ai_ops_task_for_feedback(fid, 'faq', 'new')       # 非 bug
    task_service.maybe_create_ai_ops_task_for_feedback(fid, 'bug', 'existing')  # 重复提交去重
    assert _task_count() == 0


# ==========================================
# env 通道兼容保留(Deploy 已配 env 的环境不失效)
# ==========================================

def test_env_on_creates_task_even_if_db_flag_off(clean_ai_ops, make_bug_feedback, monkeypatch):
    monkeypatch.setenv("AI_OPS_AUTO_CREATE_FROM_FEEDBACK", "true")
    fid = make_bug_feedback()
    task_service.maybe_create_ai_ops_task_for_feedback(fid, 'bug', 'new')
    assert _task_count() == 1


# ==========================================
# fail-soft:DB flag 读取异常按关闭处理,不炸反馈提交
# ==========================================

def test_flag_read_failure_soft_no_task_no_raise(clean_ai_ops, make_bug_feedback, monkeypatch):
    _clear_env(monkeypatch)

    def boom(key, default=False):
        raise RuntimeError("db down")
    monkeypatch.setattr(task_service.aiops_db, "is_flag_enabled", boom)
    fid = make_bug_feedback()
    # 不抛异常(用户提交反馈的主链路绝不受影响),也不建任务
    task_service.maybe_create_ai_ops_task_for_feedback(fid, 'bug', 'new')
    assert _task_count() == 0


# ==========================================
# PATCH 白名单:新 key 可写,设置页按钮走的就是这条通道
# ==========================================

@pytest.mark.asyncio
async def test_patch_policy_accepts_new_key(clean_ai_ops):
    import types
    import api.ai_ops_api as ai_ops_api

    req = types.SimpleNamespace(state=types.SimpleNamespace(user={"id": 1, "is_admin": True}))
    body = ai_ops_api.PolicyPatchRequest(value={"enabled": True})
    res = await ai_ops_api.api_patch_policy('ai_ops.auto_create_from_feedback', req, body)
    assert res["ok"] is True
    assert aiops_db.is_flag_enabled('ai_ops.auto_create_from_feedback') is True
