"""
AI Ops Runner API · 本机 Codex Runner 的 HTTPS 通道 · 2026-07-03

背景(方案口径修正):本轮不做 Runner VM/容器,Codex 跑在老板本机。
本机不拿生产 DB 密码,只通过这组 HTTPS 端点与任务总线交互:
  heartbeat → claim → artifacts → finish / fail

鉴权:X-Runner-Token 头 == 服务器 env AI_OPS_RUNNER_TOKEN(Deploy 配置,不进 Git)。
  - 与 admin JWT 完全独立,绝不复用 admin token
  - 服务器未配置 token → 全部 503(fail-closed,Runner API 视为未启用)
  - 错/无 token → 401;比较用 hmac.compare_digest 防时序侧信道

路径为什么在 /api/public/ 前缀下:全局 auth 中间件(auth/middleware.py · 红线文件不改)
对 /api/* 强制 JWT,白名单前缀里只有 /api/public/ 适合挂"端点内自带鉴权"的外部调用方
(先例:/api/wallet/wechat-callback 验签、/api/tv/dashboard 内部 token)。
每个端点第一行强制 token 校验,401 前不做任何 DB 操作。

边界:只操作 ai_ops_* 域(任务/事件/产物/审批/心跳);Kill Switch 开启时 claim 不可领取;
patch 永不自动 merge——有 diff 一律走 create_approval 人工审批(waiting_review 语义 =
现有 waiting_approval 状态承载,前端已显示"待审批")。
"""

import hmac
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from db import ai_ops_db as aiops_db
from services.ai_ops import policy

logger = logging.getLogger("AiOps-RunnerAPI")
router = APIRouter(prefix="/api/public/ai-ops-runner", tags=["AI Ops Runner 通道"])

_MIN_TOKEN_LEN = 16


def _require_runner_token(request: Request) -> None:
    """Runner token 鉴权。未配置 → 503(未启用);错/无 → 401。任何 DB 操作之前调用。"""
    server_token = os.environ.get("AI_OPS_RUNNER_TOKEN", "") or ""
    if len(server_token) < _MIN_TOKEN_LEN:
        # fail-closed:没配 token(或太短)时通道整体不可用,绝不放行
        raise HTTPException(status_code=503, detail="Runner API 未启用(服务器未配置 AI_OPS_RUNNER_TOKEN)")
    provided = request.headers.get("x-runner-token", "") or ""
    if not provided or not hmac.compare_digest(provided, server_token):
        raise HTTPException(status_code=401, detail="Runner token 无效")


def _task_payload(task: dict) -> dict:
    """挑安全字段返回给本机(datetime 转 str;不回传 result/worktree 等服务器内部字段)。"""
    return {
        "id": task["id"],
        "kind": task.get("kind"),
        "title": task.get("title") or "",
        "instruction": task.get("instruction") or "",
        "risk_level": task.get("risk_level"),
        "priority": task.get("priority"),
        "status": task.get("status"),
        "created_at": str(task.get("created_at") or ""),
    }


def _require_task_owned(task_id: int, worker_id: str) -> dict:
    task = aiops_db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if (task.get("assigned_worker_id") or "") != worker_id:
        raise HTTPException(status_code=403, detail="任务不属于该 worker")
    return task


# ==========================================
# Pydantic
# ==========================================

class HeartbeatRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=100)
    host: str = Field('', max_length=200)
    version: str = Field('', max_length=100)
    env_enabled: bool = False
    codex_available: bool = False
    ssh_runner_enabled: bool = False


class ClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=100)


class ArtifactRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=100)
    task_id: int
    artifact_type: str = Field(..., min_length=1, max_length=50)
    title: str = Field('', max_length=300)
    content_text: str = Field('', max_length=600000)
    metadata: dict = Field(default_factory=dict)


class FinishRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=100)
    task_id: int
    summary: str = Field('', max_length=2000)
    returncode: int = 0
    # fix 任务的 patch(可选):有 diff 一律走人工审批,绝不自动 merge
    patch_diff: str = Field('', max_length=600000)
    patch_stat: str = Field('', max_length=5000)


class FailRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=100)
    task_id: int
    error: str = Field('', max_length=4000)


# ==========================================
# 端点
# ==========================================

@router.post("/heartbeat", summary="本机 Runner 心跳(enabled=false 也照发,证明在线未授权)")
async def api_runner_heartbeat(request: Request, body: HeartbeatRequest):
    _require_runner_token(request)
    aiops_db.upsert_heartbeat(
        body.worker_id,
        host=body.host,
        version=body.version,
        env_enabled=body.env_enabled,
        codex_available=body.codex_available,
        ssh_runner_enabled=body.ssh_runner_enabled,
        note="local-codex",
    )
    return {"ok": True}


@router.post("/claim", summary="领取下一条任务(Kill Switch/总开关关闭时返回不可领取)")
async def api_runner_claim(request: Request, body: ClaimRequest):
    _require_runner_token(request)
    # 与 worker.process_one 同序的三重闸门:kill → DB 总开关 →(领取后)kind flag 复检
    if policy.is_kill_switch_enabled():
        return {"task": None, "reason": "kill_switch_on"}
    if not policy.is_ai_ops_enabled():
        return {"task": None, "reason": "ai_ops_disabled"}
    task = aiops_db.claim_next_task(body.worker_id, allowed_kinds=policy.WORKER_DEFAULT_KINDS)
    if not task:
        return {"task": None, "reason": "queue_empty"}
    allowed, reason = policy.can_worker_claim(task)
    if not allowed:
        aiops_db.update_task_status(task["id"], "queued")  # 放回队列,不丢不失败
        aiops_db.append_event(task["id"], "requeued", f"暂不允许领取: {reason}", severity="warn")
        return {"task": None, "reason": reason}

    # 服务器侧现场构建脱敏上下文(本机不读业务表);GLM 分诊结果(如有)附在后面
    from services.ai_ops import context_builder
    context_md = context_builder.build_ops_context(task)
    try:
        triage = aiops_db.list_artifacts(task["id"], artifact_type="glm_triage")
        if triage:
            context_md += "\n\n## GLM 一线分诊结果\n" + (triage[0].get("content_text") or "")[:8000]
    except Exception:  # noqa: BLE001
        pass
    aiops_db.append_event(task["id"], "context_collected", "上下文已构造(服务器侧 · 已脱敏 · 经 Runner API 下发)")
    return {"task": _task_payload(task), "context": context_md, "reason": "ok"}


@router.post("/artifacts", summary="上传任务产物(codex 输出 / patch / 测试日志)")
async def api_runner_artifacts(request: Request, body: ArtifactRequest):
    _require_runner_token(request)
    task = _require_task_owned(body.task_id, body.worker_id)
    if task.get("status") != "running":
        raise HTTPException(status_code=409, detail=f"任务状态 {task.get('status')},不接受产物")
    artifact_id = aiops_db.add_artifact(
        body.task_id, body.artifact_type,
        title=body.title, content_text=body.content_text, metadata=body.metadata,
    )
    aiops_db.append_event(body.task_id, "artifact_uploaded",
                          f"Runner 上传产物 {body.artifact_type} · artifact={artifact_id}")
    return {"artifact_id": artifact_id}


@router.post("/finish", summary="任务完成(带 patch 一律进人工审批,绝不自动 merge)")
async def api_runner_finish(request: Request, body: FinishRequest):
    _require_runner_token(request)
    task = _require_task_owned(body.task_id, body.worker_id)
    if task.get("status") != "running":
        raise HTTPException(status_code=409, detail=f"任务状态 {task.get('status')},不能 finish")

    # fix 产出 patch → 与 worker.run_task fix 分支同规则:风险扫描 + 强制人工审批
    # (waiting_review 语义由现有 waiting_approval 状态承载;merge 永远人工/Deploy 执行)
    if body.patch_diff.strip():
        risks = policy.scan_diff_risks(body.patch_diff)
        aiops_db.add_artifact(body.task_id, "patch", title="git diff",
                              content_text=body.patch_diff[:500000],
                              metadata={"stat": body.patch_stat, "risks": risks})
        risky = bool(risks)
        aiops_db.create_approval(
            body.task_id, "merge_fix",
            risk_level="L4" if risky else "L3",
            requested_reason=(f"本机 Codex 修复 diff 待合并 · 校验命中风险: {risks}" if risky
                              else "本机 Codex 修复 diff 待人工审批后合并"),
            command_plan={"diff_stat": body.patch_stat, "risks": risks, "source": "local_codex_runner"},
        )
        aiops_db.append_event(
            body.task_id, "review_flagged" if risky else "review_pending",
            (f"diff 触风险,合并前必须人工审批: {risks}" if risky else "diff 待人工审批合并"),
            payload={"risks": risks}, severity="security" if risky else "info",
        )
        return {"status": "waiting_review"}

    ok = body.returncode == 0
    aiops_db.update_task_status(
        body.task_id, "succeeded" if ok else "failed",
        summary=(body.summary or "").strip()[:500] or "Runner 执行完成",
        result={"returncode": body.returncode, "kind": task.get("kind"), "source": "local_codex_runner"},
    )
    return {"status": "succeeded" if ok else "failed"}


@router.post("/fail", summary="任务失败上报")
async def api_runner_fail(request: Request, body: FailRequest):
    _require_runner_token(request)
    _require_task_owned(body.task_id, body.worker_id)
    aiops_db.append_event(body.task_id, "error", f"本机 Runner 上报失败: {body.error[:1000]}", severity="error")
    aiops_db.update_task_status(body.task_id, "failed", summary=f"Runner 失败: {body.error[:400]}")
    return {"ok": True}
