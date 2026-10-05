"""命令台总管 v2(services/ai_ops/chat_agent + dispatcher 路由)测试。

LLM 永远 monkeypatch seam(_call_llm_generate / classify_intent / answer),不真调 API。
DB 集成用例声明 clean_ai_ops + 真实测试库。
"""
import types

import pytest

import api.ai_ops_api as ai_ops_api
from db import ai_ops_db as aiops_db
from services.ai_ops import chat_agent

ADMIN = {"id": 1, "is_admin": True}


def _req(user):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


def _llm_on():
    aiops_db.set_policy('ai_ops.chat_llm.enabled', {'enabled': True})


# ==========================================
# build_snapshot:真实 DB 数据,段独立失败
# ==========================================

def test_snapshot_contains_flags_counts_runner(clean_ai_ops):
    snap = chat_agent.build_snapshot("现在什么情况")
    assert "开关:总开关=关" in snap
    assert "任务:排队 0" in snap
    assert "Runner(执行器):未接入" in snap
    assert "最新日报:还没有生成过" in snap


def test_snapshot_reflects_real_rows(clean_ai_ops):
    task, _ = aiops_db.create_task(kind='diagnose', title="发布失败排查", instruction="查发布")
    aiops_db.create_approval(task['id'], 'merge_fix', risk_level='L3', requested_reason="测试审批")
    aiops_db.upsert_heartbeat("runner-x", env_enabled=False, codex_available=True)
    snap = chat_agent.build_snapshot("hi")
    assert f"#{task['id']}" in snap
    assert "发布失败排查" in snap
    assert "待处理 1" in snap          # 审批计数
    assert "Runner:在线" in snap
    assert "env授权执行=否" in snap


def test_snapshot_prefetches_referenced_task(clean_ai_ops):
    task, _ = aiops_db.create_task(kind='diagnose', title="A", instruction="指令原话")
    aiops_db.append_event(task['id'], 'error', "worker 异常:超时", severity='error')
    snap = chat_agent.build_snapshot(f"任务 #{task['id']} 为什么失败了")
    assert f"任务#{task['id']} 详情" in snap
    assert "指令原话" in snap
    assert "worker 异常:超时" in snap


def test_snapshot_referenced_task_missing(clean_ai_ops):
    snap = chat_agent.build_snapshot("#987654 咋回事")
    assert "任务#987654:不存在" in snap


# ==========================================
# answer:seam 降级契约 + history 组装
# ==========================================

@pytest.mark.asyncio
async def test_answer_returns_llm_content(clean_ai_ops, monkeypatch):
    async def fake(messages):
        return "总开关关着是安全设计。"
    monkeypatch.setattr(chat_agent, "_call_llm_generate", fake)
    assert await chat_agent.answer("为什么关着?") == "总开关关着是安全设计。"


@pytest.mark.asyncio
async def test_answer_none_on_llm_failure(clean_ai_ops, monkeypatch):
    async def fake(messages):
        return None
    monkeypatch.setattr(chat_agent, "_call_llm_generate", fake)
    assert await chat_agent.answer("为什么?") is None


@pytest.mark.asyncio
async def test_answer_builds_messages_with_history(clean_ai_ops, monkeypatch):
    captured = {}

    async def fake(messages):
        captured['messages'] = messages
        return "ok"
    monkeypatch.setattr(chat_agent, "_call_llm_generate", fake)
    history = [
        {"role": "user", "text": "现在系统什么情况?"},
        {"role": "ai", "text": "总开关未启用…"},
        {"role": "user", "text": ""},          # 空条丢弃
        "not-a-dict",                          # 脏数据丢弃
    ]
    await chat_agent.answer("为什么关闭的?", history)
    msgs = captured['messages']
    assert msgs[0]['role'] == 'system'
    assert "运维控制塔" in msgs[0]['content']       # 手册
    assert "当前系统快照" in msgs[0]['content']      # 快照
    assert [m['role'] for m in msgs[1:]] == ['user', 'assistant', 'user']
    assert msgs[-1]['content'] == "为什么关闭的?"


# ==========================================
# 意图规则:纯提问不建任务,真排查仍 diagnose
# ==========================================

def test_rules_question_general():
    assert ai_ops_api._detect_intent_rules("运维总开关为什么关闭的?") == 'question_general'
    assert ai_ops_api._detect_intent_rules("这个板块是干嘛的") == 'question_general'
    assert ai_ops_api._detect_intent_rules("日报是什么") == 'question_general'   # 纯提问不真生成日报


def test_rules_diagnose_keeps_investigation():
    assert ai_ops_api._detect_intent_rules("检查今天 500 最多的接口并给出可能原因") == 'diagnose'
    assert ai_ops_api._detect_intent_rules("帮我查一下为什么发布失败") == 'diagnose'
    assert ai_ops_api._detect_intent_rules("帮我修复监测趋势最新点不一致的问题") == 'fix'


def test_rules_symptom_questions_still_diagnose():
    """报障式问句(症状词)必须仍建可审计任务,不能被 question_general 吞掉(复审 P2)。"""
    assert ai_ops_api._detect_intent_rules("为什么发布失败了?") == 'diagnose'
    assert ai_ops_api._detect_intent_rules("支付一直报错?") == 'diagnose'
    assert ai_ops_api._detect_intent_rules("用户说页面打不开是怎么回事?") == 'diagnose'


def test_rules_report_still_works():
    assert ai_ops_api._detect_intent_rules("生成今天运营日报") == 'report'
    assert ai_ops_api._detect_intent_rules("日报") == 'report'                    # 平铺直叙保持旧行为


def test_snapshot_shows_latest_events_not_oldest(clean_ai_ops):
    """事件多于 10 条时,快照必须是最新 10 条——失败原因在最后(复审 P2)。"""
    task, _ = aiops_db.create_task(kind='diagnose', title="T", instruction="i")
    for i in range(12):
        aiops_db.append_event(task['id'], 'progress', f"步骤{i}")
    aiops_db.append_event(task['id'], 'error', "最终失败原因:连接超时XYZ", severity='error')
    snap = chat_agent.build_snapshot(f"#{task['id']} 为什么失败")
    assert "最终失败原因:连接超时XYZ" in snap
    assert "步骤0" not in snap          # 最老的事件已被挤出窗口


def test_snapshot_dedup_before_truncate(clean_ai_ops):
    """重复点名同一任务不能挤掉后面的任务(复审 P3:先去重再截断)。"""
    t1, _ = aiops_db.create_task(kind='diagnose', title="任务甲", instruction="a")
    t2, _ = aiops_db.create_task(kind='diagnose', title="任务乙", instruction="b")
    snap = chat_agent.build_snapshot(f"#{t1['id']} #{t1['id']} #{t1['id']} 和 #{t2['id']} 什么情况")
    assert f"任务#{t1['id']} 详情" in snap
    assert f"任务#{t2['id']} 详情" in snap


def test_snapshot_redacts_secrets_before_llm(clean_ai_ops):
    """任务标题里的密钥样式内容必须在外发 LLM 前被脱敏(复审 P2:redaction 旁路)。"""
    secret = "sk-abcdefghij1234567890"
    aiops_db.create_task(kind='diagnose', title=f"用户反馈 {secret} 泄露", instruction="x")
    snap = chat_agent.build_snapshot("最近任务什么情况")
    assert secret not in snap
    assert "«REDACTED»" in snap


# ==========================================
# chat-command dispatcher 两路
# ==========================================

@pytest.mark.asyncio
async def test_chat_question_general_flag_off_guides(clean_ai_ops):
    body = ai_ops_api.ChatCommandRequest(message="运维总开关为什么关闭的?")
    res = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert res['task'] is None                       # 纯提问绝不建任务
    assert "命令台 AI 解析" in res['reply']            # 引导开启,不装懂


@pytest.mark.asyncio
async def test_chat_question_general_flag_on_uses_agent(clean_ai_ops, monkeypatch):
    _llm_on()
    from services.ai_ops import chat_intent

    async def fake_classify(message):
        return 'question_general'

    async def fake_answer(message, history=None):
        assert message == "运维总开关为什么关闭的?"
        return "安全设计:执行链默认关,Runner 还没接入。"
    monkeypatch.setattr(chat_intent, "classify_intent", fake_classify)
    monkeypatch.setattr(chat_agent, "answer", fake_answer)
    body = ai_ops_api.ChatCommandRequest(message="运维总开关为什么关闭的?")
    res = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert res['task'] is None
    assert res['reply'] == "安全设计:执行链默认关,Runner 还没接入。"


@pytest.mark.asyncio
async def test_chat_question_status_agent_first_template_fallback(clean_ai_ops, monkeypatch):
    """问数类:LLM 开→总管回答优先;总管失败→降级确定性模板,命令台永远可用。"""
    _llm_on()
    from services.ai_ops import chat_intent

    async def fake_classify(message):
        return 'question_status'

    async def fake_answer_ok(message, history=None):
        return "系统一切正常,总开关关着(安全设计)。"
    monkeypatch.setattr(chat_intent, "classify_intent", fake_classify)
    monkeypatch.setattr(chat_agent, "answer", fake_answer_ok)
    body = ai_ops_api.ChatCommandRequest(message="现在系统什么情况?")
    res = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert res['reply'] == "系统一切正常,总开关关着(安全设计)。"

    async def fake_answer_fail(message, history=None):
        return None
    monkeypatch.setattr(chat_agent, "answer", fake_answer_fail)
    res = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert "当前系统状态" in res['reply']              # 模板兜底


@pytest.mark.asyncio
async def test_chat_flag_on_llm_fail_does_not_say_enable(clean_ai_ops, monkeypatch):
    """flag 已开但 LLM 失败:兜底文案不能误导管理员去开已开的开关(复审 P3)。"""
    _llm_on()
    from services.ai_ops import chat_intent

    async def fake_classify(message):
        return 'question_general'

    async def fake_answer(message, history=None):
        return None
    monkeypatch.setattr(chat_intent, "classify_intent", fake_classify)
    monkeypatch.setattr(chat_agent, "answer", fake_answer)
    body = ai_ops_api.ChatCommandRequest(message="这个板块是干嘛的")
    res = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert "暂时不可用" in res['reply']
    assert "开启「命令台 AI 解析」" not in res['reply']


@pytest.mark.asyncio
async def test_chat_history_passed_through(clean_ai_ops, monkeypatch):
    _llm_on()
    from services.ai_ops import chat_intent
    captured = {}

    async def fake_classify(message):
        return 'question_general'

    async def fake_answer(message, history=None):
        captured['history'] = history
        return "答"
    monkeypatch.setattr(chat_intent, "classify_intent", fake_classify)
    monkeypatch.setattr(chat_agent, "answer", fake_answer)
    body = ai_ops_api.ChatCommandRequest(
        message="为什么?",
        history=[{"role": "user", "text": "系统什么情况?"}, {"role": "ai", "text": "总开关未启用"}],
    )
    await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert captured['history'][0]['text'] == "系统什么情况?"


@pytest.mark.asyncio
async def test_chat_execution_path_untouched(clean_ai_ops, monkeypatch):
    """执行类意图(diagnose)完全不走总管:仍建可审计任务 + 确定性回复。"""
    _llm_on()
    from services.ai_ops import chat_intent

    async def fake_classify(message):
        return 'diagnose'

    async def boom(message, history=None):   # 总管若被调用直接炸,证明没被调用
        raise AssertionError("execution path must not call chat_agent")
    monkeypatch.setattr(chat_intent, "classify_intent", fake_classify)
    monkeypatch.setattr(chat_agent, "answer", boom)
    body = ai_ops_api.ChatCommandRequest(message="帮我查一下发布失败的原因")
    res = await ai_ops_api.api_chat_command(_req(ADMIN), body)
    assert res['task'] is not None
    assert res['task']['kind'] == 'diagnose'
    assert res['task']['instruction'] == "帮我查一下发布失败的原因"   # 原话不改写
