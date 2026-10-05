"""
AI Ops 命令台 · LLM 意图分类 · 2026-07-01

只做一件事:把 admin 的一句话分类到固定意图集合,让命令台路由更聪明。

铁律:
  - 只分类,不改写:task.instruction 永远存用户原话
    (老板拍板:其他模型不插在 Codex 前面裁剪问题,LLM 不进主修复链路)
  - fail-open to rules:LLM 关闭 / 无 key / 超时 / 返回异常 → None,调用方降级回规则匹配,
    命令台永远可用
  - 默认关:policy `ai_ops.chat_llm.enabled`(走 PATCH /policies 白名单,有审计日志)
  - 复用平台密钥:deepseek_key_pool / DEEPSEEK_API_KEY(服务器 .env),
    不新增任何密钥配置面,前端绝不出现密钥输入
"""

import json
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("AiOps-ChatIntent")

# 意图集合(与 api/ai_ops_api.py 的路由 dispatcher 对齐)
INTENTS = (
    'question_status', 'question_approvals', 'question_feedback', 'question_tasks',
    'question_general',   # v2 总管:一般提问/解释/为什么类 → 生成式回答,不建任务
    'report', 'fix', 'diagnose',
)

CHAT_LLM_MODEL = DEEPSEEK_OFFICIAL_FLASH   # 平台默认模型(config/model_config.py 同源口径)
CHAT_LLM_TIMEOUT_S = 6.0               # 超时即降级规则,不能让命令台卡住
_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# 注意:不用 str.format(prompt 里的 JSON 花括号会炸),直接拼接
_PROMPT_PREFIX = """你是运维控制塔的意图分类器。把管理员这句话分类成下面意图之一,只输出 JSON,格式 {"intent": "..."}:
- question_status: 问系统整体状态/健康/开关情况
- question_approvals: 问有哪些待审批/要不要审批
- question_feedback: 问用户反馈/bug 数量情况
- question_tasks: 问任务数量/进展概况
- question_general: 一般提问/解释/为什么类(想要一个回答或解释,不是要排查系统问题;
  例如"总开关为什么是关的""任务5怎么回事""这个板块是干嘛的")
- report: 要生成运维日报/运营报告
- fix: 要修复/修改某个具体问题(会改代码)
- diagnose: 要排查/检查/分析某个系统问题(只查不改,会创建排查任务)
分不清"提问"还是"要排查"时:纯疑问句选 question_general,带明确排查指令选 diagnose。只输出 JSON,不要解释。

管理员的话:"""


def _get_api_key() -> str:
    """复用平台 DeepSeek key 池(带轮换),兜底 .env 单 key。没有 key 返回空串。"""
    try:
        from services.llm.deepseek_key_pool import pick_deepseek_api_key
        key = pick_deepseek_api_key("realtime")
        if key:
            return key
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("DEEPSEEK_API_KEY", "") or ""


async def _call_llm(prompt: str) -> Optional[str]:
    """真实 HTTP 调用,返回 content 文本;任何失败返回 None(测试 monkeypatch 这个 seam)。"""
    api_key = _get_api_key()
    if not api_key:
        return None
    body = {
        "model": CHAT_LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 50,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    try:
        import httpx
        from tools.llm_call_tracker import llm_track, usage_from_response_payload
        async with httpx.AsyncClient(timeout=CHAT_LLM_TIMEOUT_S) as client:
            async with llm_track("ai_ops_chat_intent", "deepseek", model=CHAT_LLM_MODEL) as tracker:
                resp = await client.post(
                    _DEEPSEEK_URL,
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
            logger.warning("[ai_ops] chat intent LLM HTTP %s,降级规则", resp.status_code)
            return None
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        logger.warning("[ai_ops] chat intent LLM 调用失败,降级规则: %s", e)
        return None


async def classify_intent(message: str) -> Optional[str]:
    """
    LLM 意图分类。返回 INTENTS 之一;任何失败(无 key/超时/解析失败/意图不在集合)→ None,
    调用方必须降级回规则匹配。本函数不做开关检查——flag gate 在 API 层。
    """
    content = await _call_llm(_PROMPT_PREFIX + (message or "")[:500])
    if not content:
        return None
    try:
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if not m:
            return None
        intent = json.loads(m.group(0)).get("intent")
        return intent if intent in INTENTS else None
    except Exception:  # noqa: BLE001
        return None
