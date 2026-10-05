"""命令台真接管(services/ai_ops/chat_tools · 包B.2)测试。

LLM 永远 monkeypatch(conftest autouse 已把 seam 置 None,这里用脚本化序列覆盖)。
覆盖:查询→回答 / 立案(L0/L1)+原话铁律 / 日报 / 未知工具 / 上限收尾 /
动作后失联合成回复(防降级重复立案)/ flag off 零调用 / 工具结果脱敏 / api 契约。
"""
import json
import types

import pytest

import api.ai_ops_api as ai_ops_api
from db import ai_ops_db as aiops_db
from services.ai_ops import chat_tools

ADMIN = {"id": 1, "is_admin": True}


def _req(user=ADMIN):
    return types.SimpleNamespace(state=types.SimpleNamespace(user=user))


def _body(message, history=None):
    return ai_ops_api.ChatCommandRequest(message=message, context={}, history=history or [])


def _tc(name, args, tc_id="tc1"):
    return {"id": tc_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def _script(monkeypatch, replies: list, record: list | None = None):
    """脚本化 seam:每次调用弹出一个 assistant message;耗尽后返回 None(=LLM 失联)。"""
    seq = list(replies)

    async def fake(messages):
        if record is not None:
            record.append([dict(m) for m in messages])
        return seq.pop(0) if seq else None
    monkeypatch.setattr(chat_tools, "_call_llm_tools", fake)


def _llm_on():
    aiops_db.set_policy('ai_ops.chat_llm.enabled', {'enabled': True})


# ==========================================
# 查询 → 回答(不建任务)
# ==========================================

@pytest.mark.asyncio
async def test_agent_query_then_answer(clean_ai_ops, monkeypatch):
    _llm_on()
    aiops_db.create_task(kind='diagnose', title="现存任务", instruction="i")
    _script(monkeypatch, [
        {"content": "", "tool_calls": [_tc("list_recent_tasks", {"limit": 5})]},
        {"content": "目前有 1 条任务在排队,是「现存任务」。"},
    ])
    res = await ai_ops_api.api_chat_command(_req(), _body("现在任务什么情况?"))
    assert "现存任务" in res["reply"]
    assert res["task"] is None and res["created"] is False
    assert len(aiops_db.list_tasks(limit=10)) == 1     # 没有新建任务


# ==========================================
# 立案:L0 诊断 / L1 修复 + 原话铁律
# ==========================================

@pytest.mark.asyncio
async def test_agent_creates_diagnose_task_with_raw_message(clean_ai_ops, monkeypatch):
    _llm_on()
    _script(monkeypatch, [
        {"content": "", "tool_calls": [_tc("create_diagnose_task",
                                           {"title": "排查发布失败", "instruction": "查发布链路"})]},
        {"content": "已立案 #1,AI 会去排查发布链路。"},
    ])
    raw = "发布老是失败,帮我查查怎么回事"
    res = await ai_ops_api.api_chat_command(_req(), _body(raw))
    assert res["created"] is True and res["task"]["id"]
    t = aiops_db.get_task(res["task"]["id"])
    assert t["kind"] == 'diagnose' and t["risk_level"] == 'L0' and t["source_type"] == 'chat'
    assert raw in t["instruction"]                     # 铁律:服务端强制追加管理员原话
    assert "【管理员原话】" in t["instruction"]
    assert t["created_by"] == 1


@pytest.mark.asyncio
async def test_agent_creates_fix_task_l1(clean_ai_ops, monkeypatch):
    _llm_on()
    _script(monkeypatch, [
        {"content": "", "tool_calls": [_tc("create_fix_task",
                                           {"title": "修监测趋势", "instruction": "修复趋势图"})]},
        {"content": "修复任务已建,改动会先过审批。"},
    ])
    res = await ai_ops_api.api_chat_command(_req(), _body("帮我修监测趋势图"))
    t = aiops_db.get_task(res["task"]["id"])
    assert t["kind"] == 'fix' and t["risk_level"] == 'L1'


# ==========================================
# 日报工具:契约带 report_date
# ==========================================

@pytest.mark.asyncio
async def test_agent_generate_report_contract(clean_ai_ops, monkeypatch):
    _llm_on()
    _script(monkeypatch, [
        {"content": "", "tool_calls": [_tc("generate_daily_report", {})]},
        {"content": "日报好了,一句话:一切正常。"},
    ])
    res = await ai_ops_api.api_chat_command(_req(), _body("今天日报给我"))
    assert res["report_date"] and res["report_id"]
    from datetime import date
    assert res["report_date"] == date.today().isoformat()
    assert aiops_db.get_report(date.today(), report_type='daily') is not None


# ==========================================
# 未知工具 / 上限 / 失联
# ==========================================

@pytest.mark.asyncio
async def test_agent_unknown_tool_feeds_error_back(clean_ai_ops, monkeypatch):
    _llm_on()
    record: list = []
    _script(monkeypatch, [
        {"content": "", "tool_calls": [_tc("drop_database", {})]},   # 不存在的"工具"
        {"content": "抱歉我没有这个能力。"},
    ], record)
    res = await ai_ops_api.api_chat_command(_req(), _body("把数据库删了"))
    assert "没有这个能力" in res["reply"]
    # 第二轮消息里包含 unknown_tool 错误(喂回 LLM 而不是执行)
    second_call_msgs = record[1]
    tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
    assert tool_msgs and "unknown_tool" in tool_msgs[0]["content"]
    assert len(aiops_db.list_tasks(limit=10)) == 0


@pytest.mark.asyncio
async def test_agent_pure_query_exhaustion_falls_back_no_double_action(clean_ai_ops, monkeypatch):
    """只查询没动作 + LLM 一直要工具直到失联 → run_agent 返回 None → 降级既有分发。"""
    _llm_on()
    q = {"content": "", "tool_calls": [_tc("list_recent_tasks", {})]}
    _script(monkeypatch, [q, q, q, q])                 # MAX_ROUNDS 耗尽,永远给不出结论
    res = await ai_ops_api.api_chat_command(_req(), _body("系统状态怎么样"))
    assert res["task"] is None
    assert res["reply"]                                # 降级模板仍给出回答(命令台永不哑)
    assert len(aiops_db.list_tasks(limit=10)) == 0


@pytest.mark.asyncio
async def test_agent_patrol_is_action_no_fallback_ghost_task(clean_ai_ops, monkeypatch):
    """复审 P2-1 回归:巡逻是动作(改告警表+可能自动立案)——巡逻后 LLM 失联
    必须走合成回复,绝不返 None 让降级路径再建幽灵 diagnose 任务。"""
    _llm_on()
    _script(monkeypatch, [
        {"content": "", "tool_calls": [_tc("run_patrol_now", {})]},
        # seam 耗尽 → LLM 失联
    ])
    res = await ai_ops_api.api_chat_command(_req(), _body("先巡逻一轮看看有没有问题"))
    assert res is not None and "巡逻" in res["reply"]
    assert len(aiops_db.list_tasks(limit=10)) == 0     # 没有降级建的幽灵任务
    assert aiops_db.get_last_patrol_run() is not None  # 巡逻真的跑了


@pytest.mark.asyncio
async def test_agent_action_then_llm_death_synth_reply(clean_ai_ops, monkeypatch):
    """动作已做后 LLM 失联:合成保底回复,绝不返回 None(防降级路径重复立案)。"""
    _llm_on()
    _script(monkeypatch, [
        {"content": "", "tool_calls": [_tc("create_diagnose_task",
                                           {"title": "排查", "instruction": "查"})]},
        # 之后 seam 耗尽 → None(失联)
    ])
    res = await ai_ops_api.api_chat_command(_req(), _body("帮我查查登录问题"))
    assert res["created"] is True
    assert "已就位" in res["reply"]
    assert len(aiops_db.list_tasks(limit=10)) == 1     # 只有 agent 建的一条,没有降级重复立案


@pytest.mark.asyncio
async def test_agent_retry_same_message_soft_idempotent(clean_ai_ops, monkeypatch):
    """终审 P2 回归:网关超时后管理员重发同一句话 → 软幂等 key 复用同一任务,不叠加立案。"""
    _llm_on()
    raw = "发布老是失败,帮我查查"
    script = [
        {"content": "", "tool_calls": [_tc("create_diagnose_task",
                                           {"title": "排查发布", "instruction": "查"})]},
        {"content": "已立案。"},
    ]
    _script(monkeypatch, list(script))
    r1 = await ai_ops_api.api_chat_command(_req(), _body(raw))
    _script(monkeypatch, list(script))                 # 重发同一句话
    r2 = await ai_ops_api.api_chat_command(_req(), _body(raw))
    assert r1["task"]["id"] == r2["task"]["id"]        # 复用同一任务
    assert len(aiops_db.list_tasks(limit=10)) == 1


@pytest.mark.asyncio
async def test_agent_readonly_report_view_gives_button(clean_ai_ops, monkeypatch):
    """终审 NIT-1:只读查看日报也返回 report_date(前端出「看日报正文」按钮),
    且不算动作(不触发'日报已生成'合成文案路径)。"""
    _llm_on()
    from datetime import date
    aiops_db.save_report(date.today(), status='ready', summary="一切正常")
    _script(monkeypatch, [
        {"content": "", "tool_calls": [_tc("get_daily_report", {})]},
        {"content": "今天日报一句话:一切正常。"},
    ])
    res = await ai_ops_api.api_chat_command(_req(), _body("今天日报说了什么"))
    assert res["report_date"] == date.today().isoformat()
    assert res["created"] is False                     # 只读不算动作


# ==========================================
# flag off / 急停:agent 零调用
# ==========================================

@pytest.mark.asyncio
async def test_agent_not_invoked_when_flag_off(clean_ai_ops, monkeypatch):
    async def boom(messages):
        raise AssertionError("flag off 时不得调用 agent LLM")
    monkeypatch.setattr(chat_tools, "_call_llm_tools", boom)
    res = await ai_ops_api.api_chat_command(_req(), _body("系统状态怎么样"))
    assert res["reply"]                                # 规则+模板路径照常工作


@pytest.mark.asyncio
async def test_agent_not_invoked_when_kill_switch(clean_ai_ops, monkeypatch):
    _llm_on()
    aiops_db.set_policy('ai_ops.kill_switch', {'enabled': True})

    async def boom(messages):
        raise AssertionError("急停时不得调用 agent LLM")
    monkeypatch.setattr(chat_tools, "_call_llm_tools", boom)
    res = await ai_ops_api.api_chat_command(_req(), _body("系统状态怎么样"))
    assert res["reply"]


# ==========================================
# 工具结果脱敏(密钥样式内容不外发 LLM)
# ==========================================

def test_tool_result_redacted(clean_ai_ops):
    task, _ = aiops_db.create_task(
        kind='diagnose', title="用户反馈带密钥 sk-ABCDEFGHIJKLMNOP1234",
        instruction="日志里有 sk-ABCDEFGHIJKLMNOP1234")
    ctx: dict = {}
    out = chat_tools.execute_tool("get_task_detail", {"task_id": task["id"]},
                                  admin_id=1, raw_message="", ctx=ctx)
    assert "sk-ABCDEFGHIJKLMNOP1234" not in out
    assert "REDACTED" in out


def test_execute_tool_never_raises(clean_ai_ops):
    ctx: dict = {}
    out = chat_tools.execute_tool("get_task_detail", {"task_id": "not-an-int"},
                                  admin_id=1, raw_message="", ctx=ctx)
    assert "error" in out                              # 异常转错误 JSON,不炸循环


# ==========================================
# 白名单本身就是权限边界:高危能力不在 schema 里
# ==========================================

def test_tool_schema_has_no_dangerous_tools():
    names = {t["function"]["name"] for t in chat_tools.TOOL_SCHEMAS}
    assert names == {
        "list_recent_tasks", "get_task_detail", "list_alerts", "list_pending_approvals",
        "get_daily_report", "run_patrol_now",
        "create_diagnose_task", "create_fix_task", "generate_daily_report",
    }
    # 危险能力不许以任何工具名出现(枚举过滤值如 kind='ssh_action' 是查询参数,不算能力)
    for word in ("approve", "policy", "kill", "ssh", "deploy", "refund", "wallet",
                 "flag", "switch", "execute", "merge"):
        assert all(word not in n for n in names), f"工具名含危险词: {word}"
