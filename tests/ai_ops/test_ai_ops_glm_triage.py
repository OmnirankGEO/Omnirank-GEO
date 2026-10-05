"""GLM 一线分诊(services/ai_ops/glm_triage)测试。

G-8:disabled 时绝不请求外部 API(seam boom 实锤)。LLM 永远 monkeypatch,不真调。
"""
import types

import pytest

import api.ai_ops_api as ai_ops_api
from db import ai_ops_db as aiops_db
from services.ai_ops import glm_triage

ADMIN = {"id": 1, "is_admin": True}


def _req(user):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


def _boom(monkeypatch):
    async def boom(prompt):
        raise AssertionError("GLM disabled 时不得请求外部 API")
    monkeypatch.setattr(glm_triage, "_call_glm", boom)


GOOD_JSON = ('{"is_bug": true, "severity": "small_bug", "user_reply": "已受理",'
             ' "summary": "按钮文案错误", "suspected_area": "frontend",'
             ' "repro_hint": "打开设置页", "needs_codex": true}')


# ==========================================
# G-8:flag off 绝不外呼
# ==========================================

@pytest.mark.asyncio
async def test_disabled_never_calls_external_api(clean_ai_ops, monkeypatch):
    _boom(monkeypatch)
    task, _ = aiops_db.create_task(kind='diagnose', title="t", instruction="i")
    assert await glm_triage.triage_task(task) is None       # flag 默认 false → 直接 None


@pytest.mark.asyncio
async def test_api_endpoint_disabled_400_no_external_call(clean_ai_ops, monkeypatch):
    _boom(monkeypatch)
    task, _ = aiops_db.create_task(kind='diagnose', title="t", instruction="i")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_glm_triage(task["id"], _req(ADMIN))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_api_endpoint_requires_admin(clean_ai_ops):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_glm_triage(1, _req({"id": 2, "is_admin": False}))
    assert exc.value.status_code == 403


# ==========================================
# flag on:结构化产出(artifact + event)· 不改任务状态
# ==========================================

@pytest.mark.asyncio
async def test_triage_writes_artifact_and_event(clean_ai_ops, monkeypatch):
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})

    async def fake(prompt):
        assert "问题材料" in prompt
        return GOOD_JSON
    monkeypatch.setattr(glm_triage, "_call_glm", fake)
    task, _ = aiops_db.create_task(kind='diagnose', title="按钮坏了", instruction="设置页按钮点不动")
    result = await glm_triage.triage_task(task)
    assert result["is_bug"] is True
    assert result["severity"] == "small_bug"
    assert result["needs_codex"] is True
    arts = aiops_db.list_artifacts(task["id"], artifact_type="glm_triage")
    assert len(arts) == 1
    assert "按钮文案错误" in arts[0]["content_text"]
    # 分诊不动任务状态机
    assert aiops_db.get_task(task["id"])["status"] == "queued"
    events = [e["event_type"] for e in aiops_db.list_events(task["id"])]
    assert "glm_triaged" in events


@pytest.mark.asyncio
async def test_triage_hallucinated_severity_goes_needs_human(clean_ai_ops, monkeypatch):
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})

    async def fake(prompt):
        return '{"is_bug": true, "severity": "catastrophic", "summary": "x"}'
    monkeypatch.setattr(glm_triage, "_call_glm", fake)
    task, _ = aiops_db.create_task(kind='diagnose', title="t", instruction="i")
    result = await glm_triage.triage_task(task)
    assert result["severity"] == "needs_human"      # 幻觉分级 → 最保守


@pytest.mark.asyncio
async def test_triage_llm_failure_soft(clean_ai_ops, monkeypatch):
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})

    async def fake(prompt):
        return None
    monkeypatch.setattr(glm_triage, "_call_glm", fake)
    task, _ = aiops_db.create_task(kind='diagnose', title="t", instruction="i")
    assert await glm_triage.triage_task(task) is None
    events = [e["event_type"] for e in aiops_db.list_events(task["id"])]
    assert "glm_triage_failed" in events            # 失败留痕,不影响主链路
    assert aiops_db.get_task(task["id"])["status"] == "queued"


# ==========================================
# 自动一线(auto_glm_triage_if_enabled · 老板方案 §5.1-5.2 · 测试 a-d)
# ==========================================

from services.ai_ops import task_service  # noqa: E402


def _feedback_task(**kw):
    defaults = dict(kind='diagnose', source_type='feedback', title="用户反馈", instruction="按钮点不动")
    defaults.update(kw)
    task, _ = aiops_db.create_task(**defaults)
    return task


@pytest.mark.asyncio
async def test_auto_triage_disabled_no_external_call(clean_ai_ops, monkeypatch):
    """(a) flag 关:自动路径零外呼。"""
    _boom(monkeypatch)
    task = _feedback_task()
    assert await task_service.auto_glm_triage_if_enabled(task) is None
    assert aiops_db.get_task(task["id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_auto_triage_skips_non_feedback_source(clean_ai_ops, monkeypatch):
    """管理员亲自下的指令(chat/manual)不进一线:GLM 无权替老板关任务。"""
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})
    _boom(monkeypatch)
    task, _ = aiops_db.create_task(kind='diagnose', source_type='chat', title="t", instruction="i")
    assert await task_service.auto_glm_triage_if_enabled(task) is None


@pytest.mark.asyncio
async def test_auto_triage_kill_switch_freezes_no_external_call(clean_ai_ops, monkeypatch):
    """Kill Switch 急停冻结一切自主行为:flag 全开也不外呼 GLM、不关单,任务原样排队。"""
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})
    aiops_db.set_policy('ai_ops.kill_switch', {'enabled': True})
    _boom(monkeypatch)
    task = _feedback_task()
    assert await task_service.auto_glm_triage_if_enabled(task) is None
    assert aiops_db.get_task(task["id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_auto_triage_not_bug_closes_task_no_codex(clean_ai_ops, monkeypatch):
    """(b) not_bug:写回复/事件,任务 succeeded,不交 Codex。"""
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})

    async def fake(prompt):
        return ('{"is_bug": false, "severity": "small_bug", "user_reply": "这是使用问题,'
                '请在设置页开启该功能", "summary": "非 bug:使用咨询", "needs_codex": false}')
    monkeypatch.setattr(glm_triage, "_call_glm", fake)
    task = _feedback_task()
    result = await task_service.auto_glm_triage_if_enabled(task)
    assert result["is_bug"] is False
    fresh = aiops_db.get_task(task["id"])
    assert fresh["status"] == "succeeded"                    # 不进 Codex 队列
    assert "使用问题" in fresh["summary"]                     # 用户回复进 summary
    events = [e["event_type"] for e in aiops_db.list_events(task["id"])]
    assert "answered_not_bug" in events
    # Codex(Runner API)领不到它
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    from services.ai_ops import policy as _policy
    assert aiops_db.claim_next_task("w", allowed_kinds=_policy.WORKER_DEFAULT_KINDS) is None


@pytest.mark.asyncio
async def test_auto_triage_bug_keeps_queued_and_claim_attaches(clean_ai_ops, monkeypatch):
    """(c) small_bug/major_bug:artifact 落库,任务留 queued,Codex claim 时带上分诊结果。"""
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})
    monkeypatch.setenv("AI_OPS_RUNNER_TOKEN", "test-runner-token-1234567890")

    async def fake(prompt):
        return ('{"is_bug": true, "severity": "major_bug", "user_reply": "已受理",'
                ' "summary": "登录后白屏", "suspected_area": "frontend", "needs_codex": true}')
    monkeypatch.setattr(glm_triage, "_call_glm", fake)
    task = _feedback_task(instruction="登录后白屏")
    result = await task_service.auto_glm_triage_if_enabled(task)
    assert result["severity"] == "major_bug"
    assert aiops_db.get_task(task["id"])["status"] == "queued"       # 留队列等 Codex
    assert len(aiops_db.list_artifacts(task["id"], artifact_type="glm_triage")) == 1
    # 端到端:Runner API claim 附带分诊结果
    aiops_db.set_policy('ai_ops.enabled', {'enabled': True})
    import types as _types
    import api.ai_ops_runner_api as runner_api
    req = _types.SimpleNamespace(headers={"x-runner-token": "test-runner-token-1234567890"})
    res = await runner_api.api_runner_claim(req, runner_api.ClaimRequest(worker_id="w1"))
    assert res["task"]["id"] == task["id"]
    assert "GLM 一线分诊结果" in res["context"]
    assert "登录后白屏" in res["context"]


@pytest.mark.asyncio
async def test_auto_triage_failure_task_stays_queued(clean_ai_ops, monkeypatch):
    """(d) GLM 失败 fail-soft:任务原样排队,不失败不阻塞。"""
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})

    async def fake(prompt):
        return None
    monkeypatch.setattr(glm_triage, "_call_glm", fake)
    task = _feedback_task()
    assert await task_service.auto_glm_triage_if_enabled(task) is None
    assert aiops_db.get_task(task["id"])["status"] == "queued"


def test_schedule_triage_sync_context_runs_via_asyncio_run(clean_ai_ops, monkeypatch):
    """包B:faq hook 走 BackgroundTasks 线程池(无事件循环)时,
    schedule_auto_glm_triage 回退 asyncio.run 同步完成分诊,不再静默跳过。"""
    aiops_db.set_policy('ai_ops.glm_triage.enabled', {'enabled': True})

    async def fake(prompt):
        return ('{"is_bug": false, "severity": "small_bug", "user_reply": "使用问题",'
                ' "summary": "非 bug", "needs_codex": false}')
    monkeypatch.setattr(glm_triage, "_call_glm", fake)
    task = _feedback_task()
    task_service.schedule_auto_glm_triage(task)            # 本测试是 sync,无 running loop
    assert aiops_db.get_task(task["id"])["status"] == "succeeded"   # 分诊真的跑完了
