"""api/ai_ops_api.py 测试。

权限用例调端点函数(async)+ 假 request.state.user(参考 test_bug_feedback_api.py),
不走 TestClient/中间件。DB 集成用例声明 clean_ai_ops + 真实测试库。
"""
import types

import pytest
from fastapi import HTTPException

import api.ai_ops_api as ai_ops_api

ADMIN = {"id": 1, "is_admin": True}
NON_ADMIN = {"id": 2, "is_admin": False}


def _req(user):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


# ==========================================
# 权限:非 admin 全部 403(不需要 DB)
# ==========================================

@pytest.mark.asyncio
async def test_overview_requires_admin():
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_overview(_req(NON_ADMIN))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_no_user_401():
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_overview(_req(None))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_create_task_requires_admin():
    body = ai_ops_api.CreateTaskRequest(kind="diagnose", title="x")
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_create_task(_req(NON_ADMIN), body)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_feedback_diagnose_requires_admin():
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_feedback_diagnose(1, _req(NON_ADMIN))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_kill_switch_requires_admin():
    body = ai_ops_api.KillSwitchRequest(enabled=True)
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_kill_switch(_req(NON_ADMIN), body)
    assert exc.value.status_code == 403


# ==========================================
# admin 正常路径(真实测试库)
# ==========================================

@pytest.mark.asyncio
async def test_overview_admin_ok(clean_ai_ops):
    result = await ai_ops_api.api_overview(_req(ADMIN))
    assert "health" in result
    assert "task_counts" in result
    assert result["policy"]["ai_ops.kill_switch"] == {"enabled": False}


@pytest.mark.asyncio
async def test_create_and_get_task(clean_ai_ops):
    body = ai_ops_api.CreateTaskRequest(kind="diagnose", title="诊断一下", instruction="查错")
    created = await ai_ops_api.api_create_task(_req(ADMIN), body)
    assert created["created"] is True
    task_id = created["task"]["id"]
    got = await ai_ops_api.api_get_task(task_id, _req(ADMIN))
    assert got["id"] == task_id
    # 默认风险按 kind:diagnose → L0
    assert got["risk_level"] == "L0"


@pytest.mark.asyncio
async def test_create_task_default_risk_for_fix(clean_ai_ops):
    body = ai_ops_api.CreateTaskRequest(kind="fix", title="修")
    created = await ai_ops_api.api_create_task(_req(ADMIN), body)
    assert created["task"]["risk_level"] == "L1"   # fix 默认 L1


@pytest.mark.asyncio
async def test_get_task_404(clean_ai_ops):
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_get_task(999999, _req(ADMIN))
    assert exc.value.status_code == 404


# ==========================================
# 反馈快捷任务
# ==========================================

@pytest.mark.asyncio
async def test_feedback_diagnose_creates_task(clean_ai_ops, make_bug_feedback):
    fid = make_bug_feedback(message="报价页点不动", urgency="high")
    result = await ai_ops_api.api_feedback_diagnose(fid, _req(ADMIN))
    assert result["created"] is True
    task = result["task"]
    assert task["feedback_id"] == fid
    assert task["kind"] == "diagnose"
    assert task["priority"] == "P1"        # high urgency → P1
    assert task["task_key"] == f"feedback:{fid}:diagnose"
    # P1-1:反馈正文快照进 source_context(Runner 不用再回读 faq_feedback)
    fb_ctx = task["source_context_jsonb"].get("feedback")
    assert fb_ctx and "报价页点不动" in fb_ctx["message"]


@pytest.mark.asyncio
async def test_feedback_diagnose_idempotent(clean_ai_ops, make_bug_feedback):
    fid = make_bug_feedback()
    r1 = await ai_ops_api.api_feedback_diagnose(fid, _req(ADMIN))
    r2 = await ai_ops_api.api_feedback_diagnose(fid, _req(ADMIN))
    assert r1["created"] is True
    assert r2["created"] is False
    assert r1["task"]["id"] == r2["task"]["id"]


@pytest.mark.asyncio
async def test_feedback_diagnose_not_found(clean_ai_ops):
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_feedback_diagnose(999999, _req(ADMIN))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_feedback_diagnose_rejects_non_bug(clean_ai_ops, make_bug_feedback):
    fid = make_bug_feedback(kind="faq")     # 不是 bug
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_feedback_diagnose(fid, _req(ADMIN))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_feedback_fix_is_l1(clean_ai_ops, make_bug_feedback):
    fid = make_bug_feedback()
    result = await ai_ops_api.api_feedback_fix(fid, _req(ADMIN))
    assert result["task"]["kind"] == "fix"
    assert result["task"]["risk_level"] == "L1"


# ==========================================
# 命令台 / 审批 / 策略 / Kill Switch
# ==========================================

@pytest.mark.asyncio
async def test_chat_command_report_generates_synchronously(clean_ai_ops):
    body = ai_ops_api.ChatCommandRequest(message="生成今天运营日报")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"]["kind"] == "report"   # 含"日报"→ report
    assert "report_id" in result                 # P1-2:Web 容器同步生成,不排给 Runner
    from db import ai_ops_db as aiops_db
    assert aiops_db.get_task(result["task"]["id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_chat_command_diagnose_creates_task(clean_ai_ops):
    body2 = ai_ops_api.ChatCommandRequest(message="检查今天 500 最多的接口")
    result2 = await ai_ops_api.api_chat_command(_req(ADMIN), body2)
    assert result2["task"]["kind"] == "diagnose"


@pytest.mark.asyncio
async def test_chat_command_reply_warns_when_disabled(clean_ai_ops):
    # 人性化 v2:总开关默认关 → reply 必须明示"任务只排队不自动执行"(修命令台像坏了的根因)
    body = ai_ops_api.ChatCommandRequest(message="检查今天 500 最多的接口")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert "未启用" in result["reply"]


@pytest.mark.asyncio
async def test_chat_command_fix_intent_creates_fix_task(clean_ai_ops):
    # 含"修复"→ 建 fix 任务(L1),不再误建 diagnose
    body = ai_ops_api.ChatCommandRequest(message="帮我修复监测趋势最新点不一致的问题")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"]["kind"] == "fix"
    assert result["task"]["risk_level"] == "L1"
    assert "已创建" in result["reply"]


@pytest.mark.asyncio
async def test_chat_command_status_question_answers_directly(clean_ai_ops):
    # 问数意图:直接用真实数据回答,不建任务
    body = ai_ops_api.ChatCommandRequest(message="现在系统状态怎么样")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"] is None
    assert result["created"] is False
    assert "总开关" in result["reply"]
    from db import ai_ops_db as aiops_db
    # 确实没建任务(count_tasks_by_status 零填充所有状态,空库=全 0)
    assert all(v == 0 for v in aiops_db.count_tasks_by_status().values())


@pytest.mark.asyncio
async def test_chat_command_approval_question_answers_directly(clean_ai_ops):
    body = ai_ops_api.ChatCommandRequest(message="有什么待审批的")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"] is None
    assert "审批" in result["reply"]


@pytest.mark.asyncio
async def test_chat_command_report_keyword_needs_generate_verb(clean_ai_ops):
    # "报告"必须搭配生成类动词才触发日报,避免"检查监控报告接口超时"误生成日报
    body = ai_ops_api.ChatCommandRequest(message="检查监控报告接口为什么超时")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"]["kind"] == "diagnose"
    assert "report_id" not in result


@pytest.mark.asyncio
async def test_chat_command_llm_not_called_when_flag_off(clean_ai_ops, monkeypatch):
    # ai_ops.chat_llm.enabled 默认关 → 绝不调 LLM(纯规则路径)
    from services.ai_ops import chat_intent

    async def boom(message):
        raise AssertionError("flag off 时不应调用 LLM")
    monkeypatch.setattr(chat_intent, 'classify_intent', boom)
    body = ai_ops_api.ChatCommandRequest(message="检查今天 500 最多的接口")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"]["kind"] == "diagnose"


@pytest.mark.asyncio
async def test_chat_command_llm_routes_long_question(clean_ai_ops, monkeypatch):
    # 开关开 + LLM 分类成问数 → 长问题也能直答(规则的 ≤30 字限制被 LLM 超越)
    from db import ai_ops_db as aiops_db
    from services.ai_ops import chat_intent
    aiops_db.set_policy('ai_ops.chat_llm.enabled', {"enabled": True})

    async def fake(message):
        return 'question_status'
    monkeypatch.setattr(chat_intent, 'classify_intent', fake)
    body = ai_ops_api.ChatCommandRequest(message="帮我看看现在整个平台的运行状况到底怎么样,有没有什么要注意的地方")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"] is None
    assert "总开关" in result["reply"]


@pytest.mark.asyncio
async def test_chat_command_llm_failure_falls_back_to_rules(clean_ai_ops, monkeypatch):
    # 开关开但 LLM 返回 None(超时/无key/解析失败)→ 自动降级规则,命令台不挂
    from db import ai_ops_db as aiops_db
    from services.ai_ops import chat_intent
    aiops_db.set_policy('ai_ops.chat_llm.enabled', {"enabled": True})

    async def fake(message):
        return None
    monkeypatch.setattr(chat_intent, 'classify_intent', fake)
    body = ai_ops_api.ChatCommandRequest(message="帮我修复监测趋势最新点不一致的问题")
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"]["kind"] == "fix"   # 规则兜底照样识别修复意图


@pytest.mark.asyncio
async def test_chat_command_llm_keeps_raw_instruction(clean_ai_ops, monkeypatch):
    # 铁律:LLM 只分类不改写 —— instruction 必须是用户原话
    from db import ai_ops_db as aiops_db
    from services.ai_ops import chat_intent
    aiops_db.set_policy('ai_ops.chat_llm.enabled', {"enabled": True})

    async def fake(message):
        return 'diagnose'
    monkeypatch.setattr(chat_intent, 'classify_intent', fake)
    raw = "监测中心趋势图最近三天全是空的,帮我查查是采集挂了还是展示问题"
    body = ai_ops_api.ChatCommandRequest(message=raw)
    result = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert result["task"]["instruction"] == raw


@pytest.mark.asyncio
async def test_patch_policy_chat_llm_key_allowed(clean_ai_ops):
    body = ai_ops_api.PolicyPatchRequest(value={"enabled": True})
    result = await ai_ops_api.api_patch_policy("ai_ops.chat_llm.enabled", _req(ADMIN), body)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_kill_switch_toggle(clean_ai_ops):
    body = ai_ops_api.KillSwitchRequest(enabled=True)
    result = await ai_ops_api.api_kill_switch(_req(ADMIN), body)
    assert result["kill_switch"] is True
    overview = await ai_ops_api.api_overview(_req(ADMIN))
    assert overview["health"]["kill_switch"] is True


@pytest.mark.asyncio
async def test_policy_patch(clean_ai_ops):
    body = ai_ops_api.PolicyPatchRequest(value={"enabled": True})
    result = await ai_ops_api.api_patch_policy("codex.fix.enabled", _req(ADMIN), body)
    assert result["ok"] is True
    policies = await ai_ops_api.api_get_policies(_req(ADMIN))
    assert policies["policies"]["codex.fix.enabled"] == {"enabled": True}


@pytest.mark.asyncio
async def test_generate_report_synchronous(clean_ai_ops):
    result = await ai_ops_api.api_generate_report(_req(ADMIN))
    assert result["task"]["kind"] == "report"
    assert "report_id" in result
    reports_list = await ai_ops_api.api_list_reports(_req(ADMIN))
    assert len(reports_list["items"]) == 1
    assert reports_list["items"][0]["generated_by_task_id"] == result["task"]["id"]  # P1-2 走任务总线


@pytest.mark.asyncio
async def test_create_task_bad_feedback_id_404(clean_ai_ops):
    body = ai_ops_api.CreateTaskRequest(kind="diagnose", title="x", feedback_id=999999)
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_create_task(_req(ADMIN), body)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_create_task_rejects_report_kind(clean_ai_ops):
    # P1-2:report 不走通用 /tasks(防旁路排队给 Runner)
    body = ai_ops_api.CreateTaskRequest(kind="report", title="日报")
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_create_task(_req(ADMIN), body)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_patch_policy_unknown_key_400(clean_ai_ops):
    body = ai_ops_api.PolicyPatchRequest(value={"enabled": True})
    with pytest.raises(HTTPException) as exc:
        await ai_ops_api.api_patch_policy("some.random.key", _req(ADMIN), body)
    assert exc.value.status_code == 400
