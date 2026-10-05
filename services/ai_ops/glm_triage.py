"""
AI Ops · GLM 一线分诊 · 2026-07-03

多模型方案第一环:国内 GLM 常驻做一线(判断是不是 bug / 生成普通回复 / 分级 /
整理上下文交给 Codex)。跑在生产 Web 容器内(同步调用,不需要独立 Runner)。

铁律:
  - flag `ai_ops.glm_triage.enabled` 默认 false;关闭时绝不请求外部 API(gate 在调用方,
    本模块 triage_task 内也再挡一道,双保险)
  - GLM 只分诊/回复/分级/整理上下文;不改代码、不执行 SSH、不部署、不 merge、不改任务状态机
  - 产出 = glm_triage artifact(结构化 JSON)+ 事件;Codex claim 时由 Runner API 附进上下文
  - key 用服务器 env GLM_API_KEY(兜底 ZHIPUAI_API_KEY,Deploy 配置,不进 Git);没 key 返 None
  - fail-soft:任何失败返回 None + warn 事件,不影响任务主链路
"""

import json
import logging
import os
import re
from typing import Optional

from db import ai_ops_db as aiops_db

logger = logging.getLogger("AiOps-GlmTriage")

GLM_MODEL = "glm-5.2"
GLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
GLM_TIMEOUT_S = 30.0

SEVERITIES = ('small_bug', 'major_bug', 'needs_human')

# 拼接不 format(JSON 花括号规避惯例)
_PROMPT_PREFIX = """你是运维一线分诊员。根据下面的问题材料,只输出一个 JSON(不要解释),字段:
{"is_bug": true/false,
 "severity": "small_bug"|"major_bug"|"needs_human",
 "user_reply": "给反馈用户看的中文回复(是使用问题就直接解答;是 bug 就说明已受理)",
 "summary": "给管理员看的一句话问题摘要",
 "suspected_area": "frontend|backend|ops|billing|auth|unknown",
 "repro_hint": "复现线索(没有就空串)",
 "needs_codex": true/false}
判定规则:不是 bug(使用问题/误报/需求建议)→ is_bug=false,needs_codex=false;
涉及资金/登录/权限/数据丢失 → severity=needs_human;拿不准 → needs_human。

问题材料:
"""


def _get_glm_key() -> str:
    return os.environ.get("GLM_API_KEY", "") or os.environ.get("ZHIPUAI_API_KEY", "") or ""


async def _call_glm(prompt: str) -> Optional[str]:
    """真实 HTTP 调用,返回 content;任何失败返回 None(测试 monkeypatch 这个 seam)。"""
    api_key = _get_glm_key()
    if not api_key:
        logger.warning("[ai_ops] GLM key 未配置(GLM_API_KEY),分诊跳过")
        return None
    body = {
        "model": GLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 800,
        "stream": False,
        # GLM-5.2 支持深度思考;分诊是轻分类任务,关掉换低延迟(官方文档参数)
        "thinking": {"type": "disabled"},
    }
    try:
        import httpx
        from tools.llm_call_tracker import llm_track, usage_from_response_payload
        async with httpx.AsyncClient(timeout=GLM_TIMEOUT_S) as client:
            async with llm_track("ai_ops_glm_triage", "glm", model=GLM_MODEL) as tracker:
                resp = await client.post(
                    GLM_URL,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=body,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                    tracker.record(input_tokens=input_tokens, output_tokens=output_tokens,
                                   cached_tokens=cached_tokens, success=True)
                else:
                    tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            logger.warning("[ai_ops] GLM 分诊 HTTP %s", resp.status_code)
            return None
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        logger.warning("[ai_ops] GLM 分诊调用失败: %s", e)
        return None


def _parse(content: str) -> Optional[dict]:
    try:
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        severity = data.get("severity")
        if severity not in SEVERITIES:
            data["severity"] = "needs_human"     # 幻觉分级 → 最保守
        return {
            "is_bug": bool(data.get("is_bug")),
            "severity": data["severity"],
            "user_reply": str(data.get("user_reply") or "")[:2000],
            "summary": str(data.get("summary") or "")[:500],
            "suspected_area": str(data.get("suspected_area") or "unknown")[:50],
            "repro_hint": str(data.get("repro_hint") or "")[:1000],
            "needs_codex": bool(data.get("needs_codex")),
        }
    except Exception:  # noqa: BLE001
        return None


async def triage_task(task: dict) -> Optional[dict]:
    """
    对一条任务做 GLM 一线分诊。产出 glm_triage artifact + 事件;返回结构化结果。
    - flag 关 → 返回 None,绝不外呼(双保险,gate 主责在调用方)
    - 只写 artifact/event,不改任务 status(分诊不动状态机)
    - 任何失败 → None + warn 事件
    """
    if not aiops_db.is_flag_enabled('ai_ops.glm_triage.enabled', default=False):
        return None
    task_id = task["id"]

    # 材料 = 任务快照里的脱敏上下文(与 Codex 同源:source_context.feedback 已过 redact)
    from services.ai_ops import context_builder
    material = context_builder.build_ops_context(task)[:6000]

    content = await _call_glm(_PROMPT_PREFIX + material)
    result = _parse(content) if content else None
    if not result:
        aiops_db.append_event(task_id, "glm_triage_failed", "GLM 分诊失败/不可用(不影响任务主链路)",
                              severity="warn")
        return None

    aiops_db.add_artifact(
        task_id, "glm_triage", title="GLM 一线分诊",
        content_text=json.dumps(result, ensure_ascii=False, indent=2),
        metadata={"model": GLM_MODEL},
    )
    aiops_db.append_event(
        task_id, "glm_triaged",
        f"GLM 分诊:{'bug' if result['is_bug'] else '非bug'} · {result['severity']} · "
        f"{result['summary'][:80]}",
        payload={"severity": result["severity"], "needs_codex": result["needs_codex"]},
    )
    return result
