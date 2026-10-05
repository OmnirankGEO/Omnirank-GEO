"""Runner API(api/ai_ops_runner_api.py · 本机 Codex 的 HTTPS 通道)测试。

端点函数直接调用 + SimpleNamespace(headers=dict) 伪 request(dict.get 兼容 Headers.get)。
token 经 monkeypatch.setenv;DB 集成用例声明 clean_ai_ops + 真实测试库。
"""
import types

import pytest
from fastapi import HTTPException

import api.ai_ops_runner_api as runner_api
from db import ai_ops_db as aiops_db

TOKEN = "test-runner-token-1234567890"
WORKER = "local-codex-test"


def _req(token=None):
    headers = {"x-runner-token": token} if token else {}
    return types.SimpleNamespace(headers=headers)


@pytest.fixture
def runner_token(monkeypatch):
    monkeypatch.setenv("AI_OPS_RUNNER_TOKEN", TOKEN)


def _hb(worker_id=WORKER, **kw):
    return runner_api.HeartbeatRequest(worker_id=worker_id, **kw)


# ==========================================
# 鉴权:未配置 503 · 错/无 token 401(G-1)
# ==========================================

@pytest.mark.asyncio
async def test_no_server_token_503(monkeypatch):
    monkeypatch.delenv("AI_OPS_RUNNER_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        await runner_api.api_runner_heartbeat(_req(TOKEN), _hb())
    assert exc.value.status_code == 503     # fail-closed:没配 token 通道整体不可用


@pytest.mark.asyncio
async def test_short_server_token_503(monkeypatch):
    monkeypatch.setenv("AI_OPS_RUNNER_TOKEN", "short")
    with pytest.raises(HTTPException) as exc:
        await runner_api.api_runner_heartbeat(_req("short"), _hb())
    assert exc.value.status_code == 503     # 弱 token 拒绝启用


@pytest.mark.asyncio
async def test_missing_token_401(runner_token):
    with pytest.raises(HTTPException) as exc:
        await runner_api.api_runner_heartbeat(_req(None), _hb())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_401(runner_token):
    with pytest.raises(HTTPException) as exc:
        await runner_api.api_runner_claim(_req("wrong-token-xxxxxxxxxxxx"),
                                          runner_api.ClaimRequest(worker_id=WORKER))
    assert exc.value.status_code == 401


# ==========================================
# heartbeat 写入(G-2)
# ==========================================

@pytest.mark.asyncio
async def test_heartbeat_writes(clean_ai_ops, runner_token):
    res = await runner_api.api_runner_heartbeat(
        _req(TOKEN), _hb(host="boss-pc", codex_available=True))
    assert res == {"ok": True}
    status = aiops_db.get_runner_status()
    assert status["worker_id"] == WORKER
    assert status["host"] == "boss-pc"
    assert status["online"] is True
    assert status["env_enabled"] is False    # 未授权执行也在线可见


# ==========================================
# claim 闸门(G-4:Kill Switch;总开关;队列空)
# ==========================================

@pytest.mark.asyncio
async def test_claim_blocked_by_kill_switch(clean_ai_ops, runner_token):
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    aiops_db.set_policy('ai_ops.kill_switch', {'enabled': True})
    aiops_db.create_task(kind='diagnose', title="x", instruction="x")
    res = await runner_api.api_runner_claim(_req(TOKEN), runner_api.ClaimRequest(worker_id=WORKER))
    assert res["task"] is None
    assert res["reason"] == "kill_switch_on"


@pytest.mark.asyncio
async def test_claim_blocked_when_ai_ops_disabled(clean_ai_ops, runner_token):
    aiops_db.create_task(kind='diagnose', title="x", instruction="x")
    res = await runner_api.api_runner_claim(_req(TOKEN), runner_api.ClaimRequest(worker_id=WORKER))
    assert res["task"] is None
    assert res["reason"] == "ai_ops_disabled"      # 默认总开关关:任务留队列
    # 任务仍 queued,没有被动过
    assert aiops_db.count_tasks_by_status()["queued"] == 1


@pytest.mark.asyncio
async def test_claim_success_returns_context(clean_ai_ops, runner_token):
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    task, _ = aiops_db.create_task(kind='diagnose', title="发布失败", instruction="查发布失败")
    res = await runner_api.api_runner_claim(_req(TOKEN), runner_api.ClaimRequest(worker_id=WORKER))
    assert res["reason"] == "ok"
    assert res["task"]["id"] == task["id"]
    assert res["task"]["instruction"] == "查发布失败"       # 原话不改写
    assert "查发布失败" in res["context"]                    # 服务器侧构建好的上下文
    assert aiops_db.get_task(task["id"])["status"] == "running"
    assert aiops_db.get_task(task["id"])["assigned_worker_id"] == WORKER


@pytest.mark.asyncio
async def test_claim_attaches_glm_triage(clean_ai_ops, runner_token):
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    task, _ = aiops_db.create_task(kind='diagnose', title="t", instruction="i")
    aiops_db.add_artifact(task["id"], "glm_triage", title="GLM 一线分诊",
                          content_text='{"severity": "small_bug", "summary": "按钮文案错"}')
    res = await runner_api.api_runner_claim(_req(TOKEN), runner_api.ClaimRequest(worker_id=WORKER))
    assert "GLM 一线分诊结果" in res["context"]
    assert "按钮文案错" in res["context"]


# ==========================================
# 离线不失败(G-5):无人领取的任务永远 queued,无超时自动失败
# ==========================================

@pytest.mark.asyncio
async def test_offline_runner_tasks_stay_queued(clean_ai_ops, pg_conn, runner_token):
    task, _ = aiops_db.create_task(kind='fix', title="老任务", instruction="x")
    cur = pg_conn.cursor()
    cur.execute("UPDATE ai_ops_tasks SET created_at = NOW() - INTERVAL '3 days' WHERE id = %s",
                (task["id"],))
    pg_conn.commit()
    # 3 天没人领:仍 queued,没有任何超时失败机制把它标 failed
    fresh = aiops_db.get_task(task["id"])
    assert fresh["status"] == "queued"
    assert fresh["finished_at"] is None


# ==========================================
# artifacts / finish / fail(G-6/G-7:waiting_review + 不自动 merge)
# ==========================================

async def _claim_one(kind="fix"):
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    if kind == "fix":
        aiops_db.set_policy('codex.fix.enabled', {'enabled': True})
    task, _ = aiops_db.create_task(kind=kind, title="t", instruction="i")
    res = await runner_api.api_runner_claim(_req(TOKEN), runner_api.ClaimRequest(worker_id=WORKER))
    assert res["task"]["id"] == task["id"]
    return task["id"]


@pytest.mark.asyncio
async def test_artifact_upload_requires_ownership(clean_ai_ops, runner_token):
    task_id = await _claim_one("diagnose")
    with pytest.raises(HTTPException) as exc:
        await runner_api.api_runner_artifacts(_req(TOKEN), runner_api.ArtifactRequest(
            worker_id="other-worker", task_id=task_id, artifact_type="codex_output"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_finish_with_patch_goes_waiting_review_no_auto_merge(clean_ai_ops, runner_token):
    task_id = await _claim_one("fix")
    await runner_api.api_runner_artifacts(_req(TOKEN), runner_api.ArtifactRequest(
        worker_id=WORKER, task_id=task_id, artifact_type="codex_output",
        title="codex", content_text="修好了"))
    res = await runner_api.api_runner_finish(_req(TOKEN), runner_api.FinishRequest(
        worker_id=WORKER, task_id=task_id, summary="fix done", returncode=0,
        patch_diff="diff --git a/x.py b/x.py\n+fixed_line\n", patch_stat="1 file changed"))
    assert res["status"] == "waiting_review"
    task = aiops_db.get_task(task_id)
    assert task["status"] == "waiting_approval"       # waiting_review 语义由该状态承载
    approvals = aiops_db.list_approvals(status='pending')
    assert len(approvals) == 1                        # 审批 pending = 没有任何自动 merge
    assert approvals[0]["action_type"] == "merge_fix"
    patches = aiops_db.list_artifacts(task_id, artifact_type="patch")
    assert len(patches) == 1


@pytest.mark.asyncio
async def test_finish_patch_redline_escalates_l4(clean_ai_ops, runner_token):
    task_id = await _claim_one("fix")
    res = await runner_api.api_runner_finish(_req(TOKEN), runner_api.FinishRequest(
        worker_id=WORKER, task_id=task_id, returncode=0,
        patch_diff="diff --git a/middleware/billing.py b/middleware/billing.py\n"
                   "--- a/middleware/billing.py\n+++ b/middleware/billing.py\n+x = 1\n"))
    assert res["status"] == "waiting_review"
    approvals = aiops_db.list_approvals(status='pending')
    assert approvals[0]["risk_level"] == "L4"         # 红线文件 → 升 L4


@pytest.mark.asyncio
async def test_finish_without_patch_succeeds(clean_ai_ops, runner_token):
    task_id = await _claim_one("diagnose")
    res = await runner_api.api_runner_finish(_req(TOKEN), runner_api.FinishRequest(
        worker_id=WORKER, task_id=task_id, summary="诊断结论", returncode=0))
    assert res["status"] == "succeeded"
    assert aiops_db.get_task(task_id)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_fail_marks_failed(clean_ai_ops, runner_token):
    task_id = await _claim_one("diagnose")
    res = await runner_api.api_runner_fail(_req(TOKEN), runner_api.FailRequest(
        worker_id=WORKER, task_id=task_id, error="本机 worktree 创建失败"))
    assert res == {"ok": True}
    task = aiops_db.get_task(task_id)
    assert task["status"] == "failed"
    assert "worktree" in task["summary"]
