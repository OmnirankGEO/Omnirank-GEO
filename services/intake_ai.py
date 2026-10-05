"""
intake_ai — 客户公开端 AI 帮填草稿生成 (CTO-E 2026-04-26)

红线:
  - 公开端不登录, 严禁泄漏 model / API key / 内部栈
  - 只返草稿, 不写库 (写库必须客户点提交 + 代理审核)
  - AI 不确定的字段必须为 null 或 needs_more_info=true, 不能编
  - 限频在调用方 (api/intake_api.py) 校验 token.ai_suggest_count < 5 + IP 滑动窗口

设计:
  - 直接走 dashscope.Generation.call (复用 services/knowledge_pipeline 的导入模式)
  - 不 import 社媒工具包(CTO-13 territory;该包已随开源 E3 删除)
  - 单次 LLM 调用产出多字段草稿 (省 token + 字段间一致性)
  - 返 dict: {field_key: {"value": ..., "confidence": "high|medium|low", "needs_more_info": bool}}
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("GEO-Intake-AI")

# 客户表单 → AI 推断字段映射 (和 FIELD_MAP 对齐 · 避免推 brand_id 等敏感)
AI_SUGGESTABLE_FIELDS = [
    "industry",          # 从品牌名 + 业务描述推
    "business",          # 从品牌名 + 行业推一句话主营
    "target_users",      # 从行业 + 业务推目标客户
    "selling_points",    # 从业务 + 行业推
    "core_value",        # 从业务推
    "company_intro",     # 从所有信息合成 1 段
    "service_scope",     # 从城市 + 业务推 local/national/hybrid
]


SYSTEM_PROMPT = """你是品牌资料整理顾问。你的任务是基于客户提供的零散信息,产出一份"草稿",用于客户审核后再提交。

铁律:
1. 只能基于客户输入的事实推断,不能编造未提及的内容
2. 不确定的字段必须返 null + needs_more_info=true,绝不"猜一个看似合理的"
3. confidence 严格分级:
   - high: 客户明确写了
   - medium: 多条线索能交叉验证推断
   - low: 只有一条弱线索 (这种应优先 needs_more_info)
4. industry 必须具体 (如 "餐饮/中式快餐"/"装修/家装设计"),禁止 "综合服务"/"相关业务" 类泛化
5. service_scope 只能是 "local"(本地)/"national"(全国)/"hybrid"(混合) 三选一,推不出就返 null
6. 任何字段超过 60 字必须截断或拒答
7. 输出严格 JSON,无 markdown 代码块,无解释,无 think 标签

输出格式:
{
  "industry": {"value": "<=20 字 或 null", "confidence": "high|medium|low", "needs_more_info": bool},
  "business": {"value": "<=40 字 或 null", "confidence": "high|medium|low", "needs_more_info": bool},
  "target_users": {"value": "<=40 字 或 null", "confidence": "high|medium|low", "needs_more_info": bool},
  "selling_points": {"value": "<=60 字 或 null", "confidence": "high|medium|low", "needs_more_info": bool},
  "core_value": {"value": "<=60 字 或 null", "confidence": "high|medium|low", "needs_more_info": bool},
  "company_intro": {"value": "<=60 字 或 null", "confidence": "high|medium|low", "needs_more_info": bool},
  "service_scope": {"value": "local|national|hybrid 或 null", "confidence": "high|medium|low", "needs_more_info": bool}
}"""


def _build_user_prompt(form_payload: Dict[str, Any], brand_hint: Dict[str, Any]) -> str:
    """把客户当前填写的所有信息 + brand 已知信息合成一段 context."""
    lines = []
    lines.append("【已知品牌信息】")
    for k, label in (
        ("brand_name", "品牌名称"),
        ("industry", "行业"),
        ("cities", "城市/区域"),
        ("company_name", "公司名"),
    ):
        v = brand_hint.get(k)
        if v:
            lines.append(f"  {label}: {v}")

    lines.append("\n【客户当前填写】")
    customer_lines = []
    for key in (
        "brand_name", "company_name", "industry", "cities", "service_scope",
        "business", "target_users", "selling_points", "core_value",
        "success_cases", "testimonials", "company_intro", "pain_points",
        "competitors", "products", "main_link", "qualifications_text",
        "highlight_business", "extra_notes",
    ):
        v = form_payload.get(key)
        if v in (None, "", []):
            continue
        if isinstance(v, list):
            v = "、".join(str(x) for x in v)
        v_str = str(v).strip()
        if v_str:
            customer_lines.append(f"  {key}: {v_str[:200]}")
    if customer_lines:
        lines.extend(customer_lines)
    else:
        lines.append("  (客户暂未填写, 仅基于已知品牌信息推断)")

    lines.append("\n基于以上信息,按系统提示输出 JSON。任何字段宁缺勿编。")
    return "\n".join(lines)


def _strip_think_and_md(text: str) -> str:
    """去 <think> 标签 + ```json``` 围栏."""
    if not text:
        return ""
    # 去 think
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "")
    return text.strip()


def _safe_parse_json(text: str) -> Optional[Dict[str, Any]]:
    text = _strip_think_and_md(text)
    if not text:
        return None
    # 抓最外层 {...}
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = text[start:end + 1]
    try:
        return json.loads(blob)
    except Exception:
        return None


def _validate_and_clean(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """对 LLM 输出做强类型 + 长度约束. 失败的字段直接置空."""
    cleaned: Dict[str, Any] = {}
    if not isinstance(parsed, dict):
        return cleaned

    length_caps = {
        "industry": 20,
        "business": 40,
        "target_users": 40,
        "selling_points": 60,
        "core_value": 60,
        "company_intro": 60,
    }

    for key in AI_SUGGESTABLE_FIELDS:
        entry = parsed.get(key)
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        confidence = entry.get("confidence", "low")
        needs_more = bool(entry.get("needs_more_info"))

        if confidence not in ("high", "medium", "low"):
            confidence = "low"

        if key == "service_scope":
            if isinstance(value, str):
                v = value.strip().lower()
                value = v if v in ("local", "national", "hybrid") else None
            else:
                value = None
        else:
            if not isinstance(value, str) or not value.strip():
                value = None
            else:
                value = value.strip()
                cap = length_caps.get(key)
                if cap and len(value) > cap:
                    value = value[:cap]

        if needs_more or value is None:
            value = None
            needs_more = True

        cleaned[key] = {
            "value": value,
            "confidence": confidence,
            "needs_more_info": needs_more,
        }
    return cleaned


def _empty_drafts() -> Dict[str, Any]:
    return {
        k: {"value": None, "confidence": "low", "needs_more_info": True}
        for k in AI_SUGGESTABLE_FIELDS
    }


async def generate_intake_draft(
    form_payload: Dict[str, Any],
    brand_hint: Dict[str, Any],
) -> Dict[str, Any]:
    """生成草稿. 公开端调用. 失败/缺 key/超时一律降级返 _empty_drafts() (不抛细节).

    返结构 (永不抛):
      {
        "drafts": {field: {value, confidence, needs_more_info}, ...},
        "degraded": bool,
        "degraded_reason": "..."  # 仅 dev/log, 不返客户端
      }
    """
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        logger.warning("[Intake-AI] DASHSCOPE_API_KEY 未配置, 降级")
        return {
            "drafts": _empty_drafts(),
            "degraded": True,
            "degraded_reason": "missing_api_key",
        }

    try:
        import dashscope
        from dashscope import Generation
    except Exception as e:
        logger.warning(f"[Intake-AI] dashscope import 失败: {e}")
        return {
            "drafts": _empty_drafts(),
            "degraded": True,
            "degraded_reason": "sdk_unavailable",
        }

    user_prompt = _build_user_prompt(form_payload, brand_hint)
    model_name = os.getenv("INTAKE_AI_MODEL", "qwen3.7-max")

    # 同步调用包装到 to_thread (FastAPI async 兼容)
    import asyncio

    def _sync_call():
        from tools.llm_call_tracker import llm_track_sync, usage_from_response_payload

        try:
            with llm_track_sync(
                caller="intake_ai_draft",
                platform="dashscope",
                model=model_name,
            ) as tracker:
                response = Generation.call(
                    api_key=api_key,
                    model=model_name,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    result_format="message",
                )
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(response)
                status = getattr(response, "status_code", 0)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=status == 200,
                    error_msg=None if status == 200 else str(getattr(response, "message", ""))[:200],
                )
                return response
        except Exception as exc:
            return exc

    try:
        response = await asyncio.wait_for(asyncio.to_thread(_sync_call), timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("[Intake-AI] LLM timeout")
        return {
            "drafts": _empty_drafts(),
            "degraded": True,
            "degraded_reason": "timeout",
        }

    if isinstance(response, Exception):
        logger.warning(f"[Intake-AI] LLM call exception: {type(response).__name__}")
        return {
            "drafts": _empty_drafts(),
            "degraded": True,
            "degraded_reason": "llm_error",
        }

    try:
        status = getattr(response, "status_code", 0)
        if status != 200:
            logger.warning(f"[Intake-AI] LLM bad status: {status}")
            return {
                "drafts": _empty_drafts(),
                "degraded": True,
                "degraded_reason": f"llm_status_{status}",
            }
        text = response.output.choices[0].message.content
    except Exception as e:
        logger.warning(f"[Intake-AI] response parse failed: {e}")
        return {
            "drafts": _empty_drafts(),
            "degraded": True,
            "degraded_reason": "response_parse_failed",
        }

    parsed = _safe_parse_json(text or "")
    if not parsed:
        return {
            "drafts": _empty_drafts(),
            "degraded": True,
            "degraded_reason": "json_parse_failed",
        }

    drafts = _validate_and_clean(parsed)
    # 兜底空字段
    for k in AI_SUGGESTABLE_FIELDS:
        drafts.setdefault(k, {"value": None, "confidence": "low", "needs_more_info": True})

    return {
        "drafts": drafts,
        "degraded": False,
        "degraded_reason": None,
    }


def public_safe_response(internal: Dict[str, Any]) -> Dict[str, Any]:
    """剥掉 degraded_reason 等内部信息, 公开端只返客户能看的."""
    return {
        "drafts": internal.get("drafts", _empty_drafts()),
        "degraded": bool(internal.get("degraded")),
    }
