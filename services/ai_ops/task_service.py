"""
AI Ops 任务编排 · 反馈接入服务 · 2026-07-01

职责:
  - 从问题反馈(faq_feedback)创建/复用 AI Ops 诊断/修复任务(幂等)。
  - 供两个调用方共用:
      1) 管理员手动 `POST /api/admin/ai-ops/feedback/{id}/diagnose|fix`(Batch A · api 层)
      2) 用户提交 bug 反馈后的自动建任务 hook(Batch B · api/faq_api.py)
  - 自动建任务失败绝不影响用户提交反馈(调用方 try/except + warning)。

不在这里做的事:不跑 Codex、不连生产、不写钱包/资金。只往任务总线写一条任务。
"""

import logging
from typing import Optional

from db import ai_ops_db as aiops_db
from services.ai_ops import redaction

logger = logging.getLogger("AiOps-TaskService")

# urgency(low/mid/high) → 任务优先级
_URGENCY_TO_PRIORITY = {'high': 'P1', 'mid': 'P2', 'low': 'P3'}


def feedback_task_key(feedback_id: int, kind: str) -> str:
    """幂等键。同一反馈同一动作只建一个任务。"""
    return f"feedback:{int(feedback_id)}:{kind}"


def get_feedback_row(feedback_id: int) -> Optional[dict]:
    """只读取一条 faq_feedback(不改 db/faq_db.py · 避免扩大改动面)。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, kind, message, urgency, status, screenshot_url, ai_answer,
                   submitter_identity, user_id, created_at
            FROM faq_feedback
            WHERE id = %s
            """,
            (feedback_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_feedback_task(
    feedback_id: int,
    task_kind: str = 'diagnose',
    *,
    created_by: Optional[int] = None,
    source_type: str = 'feedback',
    extra_context: Optional[dict] = None,
) -> tuple[dict, bool]:
    """
    从一条 bug 反馈创建(或幂等复用)AI Ops 任务。

    校验:反馈必须存在且 kind='bug'。
    返回 (task, created)。created=False 表示命中已有幂等任务。
    抛 ValueError:反馈不存在 / 不是 bug / task_kind 非法。
    """
    if task_kind not in ('diagnose', 'fix'):
        raise ValueError(f"feedback task 只支持 diagnose/fix,收到: {task_kind}")

    fb = get_feedback_row(feedback_id)
    if not fb:
        raise ValueError("feedback_not_found")
    if fb.get('kind') != 'bug':
        raise ValueError("feedback_not_bug")

    urgency = (fb.get('urgency') or 'low')
    priority = _URGENCY_TO_PRIORITY.get(urgency, 'P3')
    # 诊断只读 = L0;修复要改代码(worktree) = L1
    risk_level = 'L1' if task_kind == 'fix' else 'L0'

    message = (fb.get('message') or '').strip()
    preview = message.replace('\n', ' ')[:60]
    title = f"{'修复' if task_kind == 'fix' else '诊断'}反馈 #{feedback_id} · {preview}"

    # P1-1:在任务创建时(WEB 容器侧有全表权)把完整脱敏反馈快照写进 source_context,
    # Runner 只读 ai_ops_* 即可拿到全部上下文,不需要再回读 faq_feedback 业务表。
    # 截图只放 OSS key(不是签名 URL);Worker 需要时现签 + 下载成本地文件再 --image。
    context = {
        "feedback": {
            "id": feedback_id,
            "urgency": urgency,
            "submitter_identity": fb.get('submitter_identity') or '',
            "message": redaction.redact((fb.get('message') or '').strip()),
            "ai_answer": redaction.redact((fb.get('ai_answer') or '').strip()),
            "screenshot_key": (fb.get('screenshot_url') or ''),
            "page_path": None,  # best-effort;faq_feedback 无 page_path 列
        },
        # 顶层 screenshot_key 给 Worker 截图下载逻辑用
        "screenshot_key": (fb.get('screenshot_url') or ''),
    }
    if extra_context:
        context.update(extra_context)

    instruction = (
        f"用户反馈了一个问题(feedback #{feedback_id})。请阅读反馈上下文,"
        f"{'定位根因并在独立 worktree 内产出修复 diff + 跑相关测试' if task_kind == 'fix' else '只读诊断根因并给出修复方案,不要改文件'}。"
    )

    task, created = aiops_db.create_task(
        kind=task_kind,
        source_type=source_type,
        source_id=str(feedback_id),
        source_context=context,
        feedback_id=feedback_id,
        title=title,
        instruction=instruction,
        risk_level=risk_level,
        priority=priority,
        created_by=created_by,
        task_key=feedback_task_key(feedback_id, task_kind),
    )
    return task, created


def maybe_create_ai_ops_task_for_feedback(
    feedback_id: int,
    kind: str,
    envelope_status: str,
) -> None:
    """
    Batch B hook:用户提交 bug 反馈成功后调用。fire-and-forget,失败只 warning。

    参数:
      - kind: faq_feedback.kind(只对 'bug' 建任务)
      - envelope_status: create_feedback() 返回信封的 status('new'/'existing'),
                         只对真正新建的反馈建任务(去重防重复提交刷任务)。

    开关(任一开启即生效 · 默认全关):
      - DB flag `ai_ops.auto_create_from_feedback`(后台设置页可点,不用动 .env 重启)
      - env AI_OPS_AUTO_CREATE_FROM_FEEDBACK(旧通道,兼容保留)
    flag 读取 fail-soft:DB 抖动时按关闭处理,绝不影响用户提交反馈。
    """
    import os
    if kind != 'bug' or envelope_status != 'new':
        return
    env_on = os.getenv('AI_OPS_AUTO_CREATE_FROM_FEEDBACK', 'false').lower() in ('1', 'true', 'yes')
    if not env_on:
        try:
            if not aiops_db.is_flag_enabled('ai_ops.auto_create_from_feedback', default=False):
                return
        except Exception:  # noqa: BLE001 · DB 抖动按关闭处理,不影响反馈提交
            return
    try:
        task, created = create_feedback_task(feedback_id, 'diagnose', source_type='feedback')
        if created:
            logger.info("[ai_ops] 反馈 #%s 自动创建诊断任务 #%s", feedback_id, task.get('id'))
            schedule_auto_glm_triage(task)   # GLM 一线自动分诊(flag 默认关 · fire-and-forget)
    except Exception as e:  # noqa: BLE001 · 绝不影响用户提交反馈
        logger.warning("[ai_ops] 反馈 #%s 自动建任务失败(不影响提交): %s", feedback_id, e)


async def auto_glm_triage_if_enabled(task: dict) -> Optional[dict]:
    """
    GLM 一线自动分诊(多模型方案 §5.1-5.2 落地):feedback 来源的 queued 任务,
    flag `ai_ops.glm_triage.enabled` 开启时自动跑一线。

    分支:
      - 非 feedback 来源 / 非 queued / flag 关 → 直接跳过(不外呼,零成本)
      - GLM 判定非 bug → 任务 succeeded(answered_not_bug),user_reply 进 summary,
        Codex 不会领到它(一线把噪音挡在队列外)
      - small_bug / major_bug / needs_human → 任务留 queued,glm_triage artifact 已写,
        Codex claim 时 Runner API 自动把分诊结果附进上下文
      - GLM 失败/不可用 → fail-soft:任务原样留队列,只记 warn 事件,绝不阻塞
    """
    try:
        if task.get('source_type') != 'feedback' or task.get('status') != 'queued':
            return None
        if aiops_db.is_kill_switch_enabled():
            return None   # 急停冻结一切自主行为(不外呼 GLM 不关单);admin 手动分诊端点不受此限
        if not aiops_db.is_flag_enabled('ai_ops.glm_triage.enabled', default=False):
            return None
        from services.ai_ops import glm_triage
        result = await glm_triage.triage_task(task)
        if not result:
            return None                      # 失败 fail-soft:任务留队列
        if not result.get('is_bug'):
            aiops_db.update_task_status(
                task['id'], 'succeeded',
                summary=(f"GLM 一线判定非 bug:{result.get('summary', '')} · "
                         f"用户回复:{result.get('user_reply', '')}")[:500],
                result={"glm_triage": result, "answered_not_bug": True},
            )
            aiops_db.append_event(
                task['id'], 'answered_not_bug',
                "GLM 判定非 bug,已生成用户回复,不进 Codex 队列",
                payload={"severity": result.get('severity')},
            )
        return result
    except Exception as e:  # noqa: BLE001 · 一线分诊永不拖垮任务主链路
        logger.warning("[ai_ops] GLM 自动分诊异常(任务留队列): %s", e)
        return None


def schedule_auto_glm_triage(task: dict) -> None:
    """
    调度自动分诊,两种执行环境都覆盖:
      - 事件循环内(api_create_task 等 async 端点)→ fire-and-forget create_task
      - 无事件循环(faq hook 走 BackgroundTasks 线程池 · 包B)→ asyncio.run 同步跑,
        阻塞的是后台线程不是事件循环,响应早已返回,安全。
    分诊是增强环节:任何失败任务照样排队,可手动触发。
    """
    import asyncio
    try:
        asyncio.get_running_loop().create_task(auto_glm_triage_if_enabled(task))
        return
    except RuntimeError:
        pass  # 无 running loop → 走同步分支
    except Exception as e:  # noqa: BLE001
        logger.warning("[ai_ops] 调度自动 GLM 分诊失败(任务已排队): %s", e)
        return
    try:
        asyncio.run(auto_glm_triage_if_enabled(task))
    except Exception as e:  # noqa: BLE001
        logger.warning("[ai_ops] 后台线程自动 GLM 分诊失败(任务已排队): %s", e)
