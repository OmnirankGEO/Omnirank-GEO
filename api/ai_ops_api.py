"""
AI Ops Center 管理端 API · 2026-07-01

全部 admin-only(request.state.user.is_admin)。前端路由固定 requiredModule="users",
后端 _require_admin 为最终闸门。

端点前缀 /api/admin/ai-ops/*:
  GET   /overview
  GET   /tasks                       · POST /tasks
  GET   /tasks/{id}  /events  /artifacts
  POST  /tasks/{id}/cancel
  POST  /feedback/{feedback_id}/diagnose | /fix
  POST  /chat-command
  GET   /approvals · POST /approvals/{id}/approve|reject
  GET   /reports   · POST /reports/generate · GET /reports/{report_date}
  GET   /policies  · PATCH /policies/{key}  · POST /kill-switch

红线:本模块只创建任务/展示状态/写审批与审计,绝不在 request 线程里跑 Codex,
      绝不执行生产 SSH / DB 写 / 资金动作(那些由 Worker + SSH Runner + 人工审批处理)。
"""

import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from db import ai_ops_db as aiops_db
from services.ai_ops import task_service

logger = logging.getLogger("AiOps-API")
router = APIRouter(prefix="/api/admin/ai-ops", tags=["AI 运维控制塔"])


# ==========================================
# 权限工具(复制自 faq_api · 防跨模块耦合)
# ==========================================

def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _require_admin(request: Request) -> dict:
    user = _require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _user_id(user: dict) -> int:
    return int(user.get("id") or user.get("user_id") or 0)


# 各 kind 的默认风险等级(policy.py 在 Batch F 可细化/覆盖)
_DEFAULT_RISK_FOR_KIND = {
    'diagnose': 'L0',
    'report': 'L0',
    'code_review': 'L0',
    'fix': 'L1',
    'ssh_action': 'L3',
}


def _default_risk_for_kind(kind: str) -> str:
    return _DEFAULT_RISK_FOR_KIND.get(kind, 'L0')


# ==========================================
# Pydantic 模型
# ==========================================

class CreateTaskRequest(BaseModel):
    kind: str = Field('diagnose', description="diagnose/fix/report/ssh_action/code_review")
    source_type: str = Field('manual', description="feedback/chat/schedule/alert/manual")
    source_id: str = Field('', max_length=200)
    source_context: dict = Field(default_factory=dict)
    feedback_id: Optional[int] = None
    title: str = Field('', max_length=300)
    instruction: str = Field('', max_length=8000)
    priority: str = Field('P3', description="P0/P1/P2/P3")
    risk_level: Optional[str] = Field(None, description="不传按 kind 默认")


class ChatCommandRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    context: dict = Field(default_factory=dict)
    # v2 总管:前端带最近对话(最多 20 条 {"role": "user"|"ai", "text": "..."}),
    # 后端无状态;只喂给生成式回答做多轮上下文,不落库
    history: list[dict] = Field(default_factory=list, max_length=20)


class RejectRequest(BaseModel):
    reason: str = Field('', max_length=2000)


class PolicyPatchRequest(BaseModel):
    value: dict = Field(..., description="策略值,如 {\"enabled\": true}")


class KillSwitchRequest(BaseModel):
    enabled: bool = Field(..., description="true 开启急停 / false 解除")


# ==========================================
# 总览
# ==========================================

@router.get("/overview", summary="控制塔总览(健康 / 任务 / 反馈 / 审批 / 最新日报 / 策略)")
async def api_overview(request: Request):
    _require_admin(request)
    task_counts = aiops_db.count_tasks_by_status()
    approval_counts = aiops_db.count_approvals_by_status()

    # bug 反馈数量(复用 faq_db 同口径)
    feedback_counts = {}
    try:
        from db.faq_db import get_feedback_counts
        feedback_counts = get_feedback_counts(kind='bug')
    except Exception as e:  # noqa: BLE001
        logger.warning("[ai_ops] 读取反馈计数失败: %s", e)

    latest_report = aiops_db.get_latest_report('daily')
    policies = aiops_db.get_policies()

    running = task_counts.get('running', 0) + task_counts.get('waiting_approval', 0)
    # Runner 心跳(P1-B):无心跳/表未落库 → None,前端显示"未接入"(get_runner_status 内部已 fail-soft)
    runner = aiops_db.get_runner_status()
    # 巡逻(包B):firing 告警数 + 最后一次巡逻打卡(两者都 fail-soft,迁移未落库不 500)
    alerts_firing = aiops_db.count_firing_alerts()
    last_patrol = aiops_db.get_last_patrol_run()
    health = {
        "kill_switch": bool((policies.get('ai_ops.kill_switch') or {}).get('enabled')),
        "ai_ops_enabled": bool((policies.get('ai_ops.enabled') or {}).get('enabled')),
        "tasks_running": running,
        "tasks_failed": task_counts.get('failed', 0),
        "pending_approvals": approval_counts.get('pending', 0),
        "open_bug_feedback": (feedback_counts.get('pending', 0) + feedback_counts.get('read', 0)),
        "runner_online": bool(runner and runner.get('online')),
        "alerts_firing": alerts_firing,
    }
    from services.ai_ops.chat_intent import CHAT_LLM_MODEL
    return {
        "health": health,
        "task_counts": task_counts,
        "feedback_counts": feedback_counts,
        "approval_counts": approval_counts,
        "latest_report": latest_report,
        "policy": policies,
        # Runner(执行器)真实心跳状态(admin-only · 只含运行元数据无密钥)
        "runner": runner,
        # 巡逻状态(包B):开关 + 最后打卡;patrol=None 表示还没巡逻过
        "patrol": {
            "enabled": bool((policies.get('ai_ops.patrol.enabled') or {}).get('enabled')),
            "last_run": last_patrol,
            "alerts_firing": alerts_firing,
        },
        # 命令台 LLM 意图分类状态(设置页只读展示;密钥只在服务器 .env,绝不下发)
        "chat_llm": {
            "enabled": bool((policies.get('ai_ops.chat_llm.enabled') or {}).get('enabled')),
            "model": CHAT_LLM_MODEL,
        },
    }


# ==========================================
# 任务
# ==========================================

@router.get("/tasks", summary="任务列表")
async def api_list_tasks(
    request: Request,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    feedback_id: Optional[int] = None,
    limit: int = 100,
):
    _require_admin(request)
    if status is not None and status not in aiops_db.TASK_STATUSES:
        raise HTTPException(status_code=400, detail="status 无效")
    if kind is not None and kind not in aiops_db.TASK_KINDS:
        raise HTTPException(status_code=400, detail="kind 无效")
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit 范围 1-500")
    items = aiops_db.list_tasks(status=status, kind=kind, feedback_id=feedback_id, limit=limit)
    return {"items": items}


@router.post("/tasks", status_code=201, summary="创建任务")
async def api_create_task(request: Request, body: CreateTaskRequest):
    admin = _require_admin(request)
    if body.kind not in aiops_db.TASK_KINDS:
        raise HTTPException(status_code=400, detail="kind 无效")
    if body.kind == 'report':
        # report 只能走 /reports/generate 或命令台(Web 容器同步生成),不走通用 /tasks 排队(P1-2)
        raise HTTPException(status_code=400, detail="report 任务请用 /reports/generate 或命令台生成")
    if body.source_type not in aiops_db.SOURCE_TYPES:
        raise HTTPException(status_code=400, detail="source_type 无效")
    if body.priority not in aiops_db.PRIORITIES:
        raise HTTPException(status_code=400, detail="priority 无效")
    risk = body.risk_level or _default_risk_for_kind(body.kind)
    if risk not in aiops_db.RISK_LEVELS:
        raise HTTPException(status_code=400, detail="risk_level 无效")
    # P2-1:传了 feedback_id 就必须存在,避免孤儿任务
    if body.feedback_id is not None:
        from services.ai_ops.task_service import get_feedback_row
        if not get_feedback_row(body.feedback_id):
            raise HTTPException(status_code=404, detail="feedback_id 不存在")
    try:
        task, created = aiops_db.create_task(
            kind=body.kind,
            source_type=body.source_type,
            source_id=body.source_id,
            source_context=body.source_context,
            feedback_id=body.feedback_id,
            title=body.title or f"{body.kind} 任务",
            instruction=body.instruction,
            risk_level=risk,
            priority=body.priority,
            created_by=_user_id(admin),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if created and body.source_type == 'feedback':
        # GLM 一线自动分诊(flag 默认关 · fire-and-forget,不拖慢创建响应)
        from services.ai_ops.task_service import schedule_auto_glm_triage
        schedule_auto_glm_triage(task)
    return {"task": task, "created": created}


@router.get("/tasks/{task_id}", summary="任务详情")
async def api_get_task(task_id: int, request: Request):
    _require_admin(request)
    task = aiops_db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/tasks/{task_id}/events", summary="任务事件流(支持 after_id 增量)")
async def api_task_events(task_id: int, request: Request, after_id: Optional[int] = None, limit: int = 500):
    _require_admin(request)
    if not aiops_db.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit 范围 1-1000")
    return {"items": aiops_db.list_events(task_id, after_id=after_id, limit=limit)}


@router.get("/tasks/{task_id}/artifacts", summary="任务产物")
async def api_task_artifacts(task_id: int, request: Request, artifact_type: Optional[str] = None):
    _require_admin(request)
    if not aiops_db.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"items": aiops_db.list_artifacts(task_id, artifact_type=artifact_type)}


@router.post("/tasks/{task_id}/cancel", summary="取消任务")
async def api_cancel_task(task_id: int, request: Request):
    _require_admin(request)
    if not aiops_db.get_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    ok = aiops_db.cancel_task(task_id)
    return {"ok": ok}


# ==========================================
# 反馈快捷任务
# ==========================================

@router.post("/feedback/{feedback_id}/diagnose", status_code=201, summary="把反馈交给 AI 诊断")
async def api_feedback_diagnose(feedback_id: int, request: Request):
    admin = _require_admin(request)
    return _create_feedback_task_or_error(feedback_id, 'diagnose', _user_id(admin))


@router.post("/feedback/{feedback_id}/fix", status_code=201, summary="把反馈交给 Codex 修复")
async def api_feedback_fix(feedback_id: int, request: Request):
    admin = _require_admin(request)
    return _create_feedback_task_or_error(feedback_id, 'fix', _user_id(admin))


def _create_feedback_task_or_error(feedback_id: int, kind: str, created_by: int) -> dict:
    try:
        task, created = task_service.create_feedback_task(feedback_id, kind, created_by=created_by)
    except ValueError as e:
        code = str(e)
        if code == 'feedback_not_found':
            raise HTTPException(status_code=404, detail="反馈不存在")
        if code == 'feedback_not_bug':
            raise HTTPException(status_code=400, detail="只有 bug 反馈能交给 AI 处理")
        raise HTTPException(status_code=400, detail=code)
    return {"task": task, "created": created}


# ==========================================
# 命令台 v2(2026-07-01 人性化):
#   1. 问数类短问题 → 直接用 DB 真实数据回答(不建任务,不假装 LLM)
#   2. 日报意图 → Web 容器同步生成
#   3. 修复意图 → 建 fix 任务(L1);其余 → diagnose
#   每个响应都带 reply 人话字段:创建了什么 / 接下来会发生什么 / 需不需要你做什么。
#   总开关未启用时明确告知"任务只排队保留不自动执行"(修「命令台像坏了」的根因)。
# ==========================================

_CHAT_KIND_LABEL = {'diagnose': 'AI 诊断', 'fix': 'Codex 修复', 'code_review': '代码审查'}


def _answer_status() -> str:
    tc = aiops_db.count_tasks_by_status()
    ac = aiops_db.count_approvals_by_status()
    enabled = aiops_db.is_flag_enabled('ai_ops.enabled')
    kill = aiops_db.is_kill_switch_enabled()
    running = tc.get('running', 0) + tc.get('waiting_approval', 0)
    return (
        "当前系统状态:\n"
        f"· AI 运维总开关:{'已启用' if enabled else '未启用(任务只排队保留,不自动执行)'}\n"
        f"· Kill Switch:{'⚠️ 急停中,所有执行已冻结' if kill else '正常'}\n"
        f"· 进行中任务 {running} · 失败 {tc.get('failed', 0)} · 待审批 {ac.get('pending', 0)}"
    )


def _answer_approvals() -> str:
    total = aiops_db.count_approvals_by_status().get('pending', 0)
    if not total:
        return "现在没有待审批的动作,不需要你处理。"
    items = aiops_db.list_approvals(status='pending', limit=5)
    head = "\n".join(f"· #{a['id']} {a['action_type']}({a['risk_level']})" for a in items)
    return f"共 {total} 条待审批:\n{head}\n到左侧「审批中心」逐条通过或驳回。"


def _answer_feedback() -> str:
    try:
        from db.faq_db import get_feedback_counts
        fc = get_feedback_counts(kind='bug')
        open_n = fc.get('pending', 0) + fc.get('read', 0)
        return (f"未处理的 bug 反馈共 {open_n} 条(待处理 {fc.get('pending', 0)} · 已读 {fc.get('read', 0)})。"
                "在「问题反馈」板块可以一键交给 AI 诊断。")
    except Exception:  # noqa: BLE001
        return "反馈计数暂时读不到,请到「问题反馈」板块直接查看。"


def _answer_tasks() -> str:
    tc = aiops_db.count_tasks_by_status()
    return (f"任务概况:排队 {tc.get('queued', 0)} · 运行中 {tc.get('running', 0)} · "
            f"待审批 {tc.get('waiting_approval', 0)} · 成功 {tc.get('succeeded', 0)} · 失败 {tc.get('failed', 0)}。")


# 问数意图 → 回答函数(数据全部来自真实 DB 查询;LLM 只负责分类不负责编数据)
_QUESTION_ANSWERERS = {
    'question_status': _answer_status,
    'question_approvals': _answer_approvals,
    'question_feedback': _answer_feedback,
    'question_tasks': _answer_tasks,
}


def _detect_intent_rules(message: str) -> str:
    """规则意图匹配:LLM 关闭/失败时的兜底(也是默认路径)。词表刻意用窄短语防误触。"""
    def _has(*kws: str) -> bool:
        return any(k in message for k in kws)

    if len(message) <= 30:
        if _has('系统状态', '运行状态', '健康状况', '系统怎么样', '现在怎么样', '整体情况', '概况', '系统什么情况'):
            return 'question_status'
        if _has('待审批', '要审批的', '审批队列'):
            return 'question_approvals'
        if _has('多少反馈', '待处理反馈', '反馈有多少', '多少bug', '多少 bug'):
            return 'question_feedback'
        if _has('任务概况', '多少任务', '任务列表', '任务情况'):
            return 'question_tasks'
    asks_question = (message.endswith(('?', '?'))
                     or _has('为什么', '是什么', '什么意思', '怎么回事', '干嘛的', '啥意思'))
    wants_generate = _has('生成', '出一份', '来一份', '写一份')
    # 「日报」纯提问("日报是什么")走总管回答不真生成;带生成动词或平铺直叙才生成。
    # 「报告」必须搭配生成类动词,避免"检查监控报告接口超时"误触发日报
    if ('日报' in message and (wants_generate or not asks_question)) or (
        ('报告' in message or 'report' in message.lower()) and wants_generate
    ):
        return 'report'
    if _has('修复', '修一下', '帮我修', 'fix'):
        return 'fix'
    # v2 总管:纯提问/解释类("为什么/是什么/怎么回事")→ 生成式回答,不建任务。
    # 带排查动词或故障症状词的仍是 diagnose(建可审计任务)——
    # "为什么发布失败了?"是报障不是闲聊,症状词优先建任务(复审 P2:防路由回归)。
    if asks_question and not _has(
        '检查', '排查', '诊断', '分析', '查一下', '查查', '帮我查',
        '报错', '失败', '异常', '超时', '打不开', '不能用', '坏了', '卡住', '很慢',
    ):
        return 'question_general'
    return 'diagnose'


def _chat_task_reply(task: dict, created: bool) -> str:
    """任务创建后的人话回复:说清接下来会发生什么、要不要用户做什么。全部基于真实 flag 状态。"""
    kind_label = _CHAT_KIND_LABEL.get(task['kind'], task['kind'])
    if not created:
        return f"这件事已有相同任务(#{task['id']}),没有重复创建,点下方按钮可以看进度。"
    base = f"已创建{kind_label}任务 #{task['id']}「{task['title']}」。"
    if aiops_db.is_kill_switch_enabled():
        return base + "⚠️ 当前 Kill Switch 急停中,所有任务暂停执行,解除急停后才会处理。"
    if not aiops_db.is_flag_enabled('ai_ops.enabled'):
        return base + "⚠️ AI 运维总开关未启用:任务已排队保留、不会自动执行;开关启用后 AI 会自动领取处理。"
    if task['kind'] == 'fix':
        return base + "AI 会在隔离环境改代码、跑测试、生成改动;改动合并前一定会回到「审批中心」等你确认,不会自动上线。"
    return base + "AI 领取后开始分析,过程和结论都写在任务卡片里,处理完状态会变成「成功」。"


@router.post("/chat-command", status_code=201, summary="AI 命令台 · 问数直答 / 自然语言转任务")
async def api_chat_command(request: Request, body: ChatCommandRequest):
    admin = _require_admin(request)
    message = body.message.strip()

    # 0) LLM gate(policy `ai_ops.chat_llm.enabled` 默认关;急停时不调 LLM)
    llm_on = (aiops_db.is_flag_enabled('ai_ops.chat_llm.enabled')
              and not aiops_db.is_kill_switch_enabled())

    # 0.5) 真接管(包B.2):flag 开时整条消息交给工具型 Agent——它自己决定查什么、
    #      要不要立案、怎么汇报(白名单只到"立案",执行权限零变化)。
    #      返回 None = 未做任何动作且 LLM 不可用 → 安全降级到下面的既有意图分发;
    #      做过动作时 run_agent 内部必然给出回复,绝不让降级路径重复立案。
    if llm_on:
        from services.ai_ops import chat_tools
        agent_res = await chat_tools.run_agent(
            message, body.history, _user_id(admin),
            page=str((body.context or {}).get('page') or ''))
        if agent_res is not None:
            return agent_res

    # 0.6) 降级路径:意图分类(LLM 失败/关闭 → 规则匹配,命令台永远可用)
    intent: Optional[str] = None
    if llm_on:
        from services.ai_ops import chat_intent
        intent = await chat_intent.classify_intent(message)
    if intent is None:
        intent = _detect_intent_rules(message)

    # 1) 问数/提问 → 回答,不建任务。
    #    v2 总管:LLM 开启时用生成式回答(运行手册+真实数据快照+多轮 history,
    #    services/ai_ops/chat_agent · 只答不做 · 数字只准来自快照);
    #    失败/关闭降级回确定性模板——命令台永远可用。
    answerer = _QUESTION_ANSWERERS.get(intent)
    if answerer is not None:
        reply: Optional[str] = None
        if llm_on:
            from services.ai_ops import chat_agent
            reply = await chat_agent.answer(message, body.history)
        return {"task": None, "created": False, "reply": reply or answerer()}

    if intent == 'question_general':
        reply = None
        if llm_on:
            from services.ai_ops import chat_agent
            reply = await chat_agent.answer(message, body.history)
            # flag 已开但 LLM 超时/失败:别误导管理员去开已开的开关(复审 P3)
            fallback = ("AI 解析暂时不可用(可能超时),稍后再试。"
                        "也可以问我「系统状态/待审批/任务概况」直接拿数,或说「帮我查 XXX」创建诊断任务。")
        else:
            fallback = ("这类问题需要在「设置」页开启「命令台 AI 解析」我才能详细解答。"
                        "现在你可以:问我「系统状态/待审批/任务概况/反馈情况」直接拿数,"
                        "或说「帮我查 XXX」创建诊断任务。")
        return {"task": None, "created": False, "reply": reply or fallback}

    # 2) 日报 → Web 容器同步生成(P1-2:不排给只有 ai_ops_* 权限的 Runner)
    if intent == 'report':
        from datetime import date as _date
        from services.ai_ops import report_builder
        task, report_id = report_builder.generate_daily_report_with_task(
            source_type='chat', created_by=_user_id(admin))
        today = _date.today().isoformat()
        # 回复直接带日报真实摘要(老板要结论,不是"生成了去别处看");按钮直达正文
        report = aiops_db.get_report(_date.today(), report_type='daily') or {}
        r_summary = (report.get('summary') or '').strip()
        reply = (f"今日日报:{r_summary}\n点下方按钮看完整正文。" if r_summary
                 else "日报已生成,点下方按钮看完整正文。")
        return {"task": task, "report_id": report_id, "created": True,
                "report_date": today, "reply": reply}

    # 3) fix(L1 · 合并必审批)/ diagnose(L0 只读)
    #    instruction 永远存用户原话——LLM 不改写不裁剪(不插在 Codex 前面)
    kind = 'fix' if intent == 'fix' else 'diagnose'
    task, created = aiops_db.create_task(
        kind=kind,
        source_type='chat',
        source_id='',
        source_context={"page": (body.context or {}).get('page', ''), "raw_command": message},
        title=message[:60],
        instruction=message,
        risk_level=_default_risk_for_kind(kind),
        priority='P2',
        created_by=_user_id(admin),
    )
    return {"task": task, "created": created, "reply": _chat_task_reply(task, created)}


@router.post("/tasks/{task_id}/glm-triage", summary="GLM 一线分诊(手动触发 · flag 默认关)")
async def api_glm_triage(task_id: int, request: Request):
    _require_admin(request)
    if not aiops_db.is_flag_enabled('ai_ops.glm_triage.enabled', default=False):
        raise HTTPException(status_code=400, detail="GLM 一线分诊未启用(设置页开启 ai_ops.glm_triage.enabled)")
    task = aiops_db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    from services.ai_ops import glm_triage
    result = await glm_triage.triage_task(task)
    if not result:
        raise HTTPException(status_code=502, detail="GLM 分诊失败/不可用(已记录事件,不影响任务)")
    return {"result": result}


# ==========================================
# 审批
# ==========================================

@router.get("/approvals", summary="审批队列")
async def api_list_approvals(request: Request, status: Optional[str] = None, limit: int = 100):
    _require_admin(request)
    if status is not None and status not in aiops_db.APPROVAL_STATUSES:
        raise HTTPException(status_code=400, detail="status 无效")
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit 范围 1-500")
    return {"items": aiops_db.list_approvals(status=status, limit=limit)}


@router.post("/approvals/{approval_id}/approve", summary="审批通过(不代表立即执行)")
async def api_approve(approval_id: int, request: Request):
    admin = _require_admin(request)
    ap = aiops_db.get_approval(approval_id)
    if not ap:
        raise HTTPException(status_code=404, detail="审批不存在")
    ok = aiops_db.approve_action(approval_id, _user_id(admin))
    if not ok:
        raise HTTPException(status_code=409, detail="审批不是 pending 状态,无法通过")
    return {"ok": True}


@router.post("/approvals/{approval_id}/reject", summary="审批驳回")
async def api_reject(approval_id: int, request: Request, body: RejectRequest):
    admin = _require_admin(request)
    ap = aiops_db.get_approval(approval_id)
    if not ap:
        raise HTTPException(status_code=404, detail="审批不存在")
    ok = aiops_db.reject_action(approval_id, _user_id(admin), reason=body.reason)
    if not ok:
        raise HTTPException(status_code=409, detail="审批不是 pending 状态,无法驳回")
    return {"ok": True}


# ==========================================
# 日报
# ==========================================

@router.get("/reports", summary="日报列表")
async def api_list_reports(request: Request, limit: int = 60):
    _require_admin(request)
    if limit < 1 or limit > 365:
        raise HTTPException(status_code=400, detail="limit 范围 1-365")
    return {"items": aiops_db.list_reports(limit=limit)}


@router.post("/reports/generate", status_code=201, summary="手动生成日报(走任务总线 · 同步生成)")
async def api_generate_report(request: Request):
    admin = _require_admin(request)
    from services.ai_ops import report_builder
    # 手动是管理员显式动作:走任务总线同步生成(有 task/events + generated_by_task_id),
    # 不依赖 Worker;自动管线(scheduler)才受 ai_ops.enabled gate。
    task, report_id = report_builder.generate_daily_report_with_task(
        source_type='manual', created_by=_user_id(admin))
    return {"task": task, "report_id": report_id, "created": True}


@router.get("/reports/{report_date}", summary="按日期取日报")
async def api_get_report(report_date: str, request: Request, report_type: str = 'daily'):
    _require_admin(request)
    if report_type not in aiops_db.REPORT_TYPES:
        raise HTTPException(status_code=400, detail="report_type 无效")
    try:
        d = datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="report_date 格式应为 YYYY-MM-DD")
    report = aiops_db.get_report(d, report_type=report_type)
    if not report:
        raise HTTPException(status_code=404, detail="日报不存在")
    return report


# ==========================================
# 告警 / 巡逻(包B · 主动巡逻)
# ==========================================

@router.get("/alerts", summary="告警列表(firing 优先)")
async def api_list_alerts(request: Request, status: Optional[str] = None, limit: int = 100):
    _require_admin(request)
    if status is not None and status not in aiops_db.ALERT_STATUSES:
        raise HTTPException(status_code=400, detail="status 无效")
    return {"alerts": aiops_db.list_alerts(status=status, limit=max(1, min(int(limit), 200))),
            "firing_count": aiops_db.count_firing_alerts()}


@router.post("/alerts/{alert_id}/resolve", summary="手动恢复一条告警")
async def api_resolve_alert(alert_id: int, request: Request):
    admin = _require_admin(request)
    row = aiops_db.resolve_alert_by_id(alert_id, resolved_by=_user_id(admin))
    if not row:
        raise HTTPException(status_code=404, detail="告警不存在或已恢复")
    logger.info("[ai_ops] 告警 #%s 被 admin=%s 手动恢复", alert_id, _user_id(admin))
    return {"ok": True, "alert": row}


@router.post("/patrol/run", summary="立即巡逻一轮(不依赖开关)")
async def api_run_patrol(request: Request):
    """admin 手动触发,force=True 绕过 ai_ops.patrol.enabled(手动=人工授权,同手动日报原则)。
    巡逻是一串阻塞 DB 查询 → to_thread,不占事件循环(WORKERS=1)。"""
    _require_admin(request)
    import asyncio
    from services.ai_ops.patrol import run_patrol
    result = await asyncio.to_thread(run_patrol, force=True)
    if result is None:
        raise HTTPException(status_code=500, detail="巡逻信号查询失败(看服务端日志)")
    return {"ok": True, **result}


# ==========================================
# 策略 / Kill Switch
# ==========================================

@router.get("/policies", summary="策略列表")
async def api_get_policies(request: Request):
    _require_admin(request)
    return {"policies": aiops_db.get_policies()}


@router.patch("/policies/{key}", summary="更新策略")
async def api_patch_policy(key: str, request: Request, body: PolicyPatchRequest):
    admin = _require_admin(request)
    if key not in aiops_db.POLICY_KEYS:
        raise HTTPException(status_code=400, detail=f"未知策略 key: {key}")
    aiops_db.set_policy(key, body.value, updated_by=_user_id(admin))
    logger.warning("[ai_ops] 策略变更 key=%s value=%s by=%s", key, body.value, _user_id(admin))
    return {"ok": True, "key": key, "value": body.value}


@router.post("/kill-switch", summary="Kill Switch 急停开关")
async def api_kill_switch(request: Request, body: KillSwitchRequest):
    admin = _require_admin(request)
    aiops_db.set_policy('ai_ops.kill_switch', {"enabled": bool(body.enabled)}, updated_by=_user_id(admin))
    logger.warning("[ai_ops] Kill Switch 被 admin=%s 置为 %s", _user_id(admin), body.enabled)
    return {"ok": True, "kill_switch": bool(body.enabled)}
