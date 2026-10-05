"""
services/marketing/advisor_llm.py — 军师文案润色 LLM(flag 关默认不跑 · fail-soft)

设计要点(照抄 services/ai_ops/chat_intent.py 的 httpx+llm_track+fail-soft 房规):
  - 数字/结构永远来自规则草稿,LLM 只润色 touch_copy 措辞(总设计:军师建议撰写用强模型,
    但默认 flag 关时走确定性草稿,不阻塞)。
  - 双闸:marketing_agent.enabled + marketing_agent.advisor_llm.enabled,任一关 → 直接返回原草稿。
  - 出口再守卫:LLM 产出重新过 scan_forbidden;含承诺词/积分/竞品 → 丢弃 LLM 版,保留确定性草稿。
  - _call_llm 是可 monkeypatch 的私有 seam(测试注入)。
"""
import json
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import logging
import re
from typing import Optional

from db import marketing_db
from services.marketing.guards import scan_forbidden

logger = logging.getLogger("GEO-Marketing-Advisor")

# 模型口径(SSOT: config/model_config.py)· 润色用便宜稳定档;ops 可改此常量或接 get_model_for_task
ADVISOR_MODEL = DEEPSEEK_OFFICIAL_FLASH
_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
_TIMEOUT_S = 12.0


def _get_key() -> str:
    try:
        from services.llm.deepseek_key_pool import pick_deepseek_api_key
        return pick_deepseek_api_key("realtime") or ""
    except Exception:  # noqa: BLE001
        import os
        return os.environ.get("DEEPSEEK_API_KEY", "")


async def _call_llm(system_prompt: str, user_prompt: str) -> Optional[str]:
    """原始 HTTP seam(测试 monkeypatch 这里)。任何失败 → None。"""
    key = _get_key()
    if not key:
        return None
    try:
        import httpx
        from tools.llm_call_tracker import llm_track, usage_from_response_payload
        body = {
            "model": ADVISOR_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 600,
            "thinking": {"type": "disabled"},
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            async with llm_track("marketing_advisor_copy", "deepseek", model=ADVISOR_MODEL) as tracker:
                resp = await client.post(_DEEPSEEK_URL,
                                         headers={"Authorization": f"Bearer {key}",
                                                  "Content-Type": "application/json"},
                                         json=body)
                if resp.status_code == 200:
                    data = resp.json()
                    it, ot, ct = usage_from_response_payload(data)
                    tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct, success=True)
                    return data["choices"][0]["message"]["content"]
                tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
                return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[advisor] LLM 调用失败(保留确定性草稿): %s", e)
        return None


def _parse_copy(content: str) -> Optional[dict]:
    if not content:
        return None
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    out = {k: str(v)[:600] for k, v in data.items() if isinstance(v, str)}
    return out or None


async def enrich_touch_copy(draft: dict, system_prompt: str,
                            competitors: Optional[list] = None,
                            force: bool = False) -> dict:
    """润色 draft['touch_copy'](分渠道)。flag 关或失败 → 原样返回。

    force=True 跳过双闸:给"运营手动点击"类显式动作用(如 cases/draft),
    与「AI 帮我写」同一无闸口径——LLM 只写字不碰钱不外发,失败仍 fail-soft。
    自动路径(巡逻)不传 force,双闸照旧。

    返回润色后的 touch_copy dict(保证过出口守卫;否则回退确定性版)。
    """
    base_copy = draft.get("touch_copy") or {}
    # 双闸(force 的手动路径豁免)
    if not force:
        if not marketing_db.is_flag_enabled("marketing_agent.enabled", default=False):
            return base_copy
        if not marketing_db.is_flag_enabled("marketing_agent.advisor_llm.enabled", default=False):
            return base_copy

    channels = list(base_copy.keys())
    if not channels:
        return base_copy

    user_prompt = (
        "把下面各渠道的营销文案润色得更自然口语、更打动人,但严格保持事实与数字不变、"
        "不加任何承诺词、对客用'算力'不用'积分'。只输出 JSON,键为渠道名,值为润色后文案。\n"
        f"触发原因:{draft.get('trigger_reason','')}\n"
        f"觉醒阶段:{draft.get('awareness_stage','')}\n"
        f"原文案 JSON:{json.dumps(base_copy, ensure_ascii=False)}"
    )
    content = await _call_llm(system_prompt, user_prompt)
    enriched = _parse_copy(content)
    if not enriched:
        return base_copy

    # 出口守卫:任一渠道命中守卫词表(含 advisory) → 丢弃 LLM 版,保留确定性草稿
    # (内部 fail-safe,非用户面硬拦;用户输入面的硬拦只看法律禁止目录包)。
    result = {}
    for ch in channels:
        cand = enriched.get(ch)
        if cand:
            r = scan_forbidden(cand, competitors=competitors)
            result[ch] = cand if not r["flags"] else base_copy[ch]
        else:
            result[ch] = base_copy[ch]
    return result


async def draft_case_texts(direction: str, cohort_label: str, count_disp: str,
                           system_prompt: str) -> Optional[dict]:
    """[P0-B] 手动起草的文本层:LLM 写方案名 + 站内信文案(数字/人群由调用方 SQL 给定)。

    手动显式动作,无闸(与 enrich force 同口径);任一字段缺失或违规 → None(调用方回落确定性版)。
    """
    user_prompt = (
        "为下面的营销方向起草两样东西,只输出 JSON,不解释:\n"
        '{"title": "≤20字的方案名(说人话,写给审批人看,点明人群和动作)", '
        '"station": "≤80字站内信文案(自然口语,短句,以一个明确的动作收尾)"}\n'
        f"运营方向:{direction}\n"
        f"目标人群:{cohort_label}(实时圈选 {count_disp} 人)\n"
        "硬约束:不含任何承诺性用语;对客金额一律说'算力'不说'积分';不出现具体百分比。"
    )
    content = await _call_llm(system_prompt, user_prompt)
    data = _parse_copy(content or "")
    if not data:
        return None
    title = str(data.get("title", "")).strip()[:40]
    station = str(data.get("station", "")).strip()[:200]
    if not title or not station:
        return None
    for t in (title, station):
        if scan_forbidden(t)["flags"]:  # 任一守卫命中(含 advisory)弃 LLM 版,内部 fail-safe
            logger.warning("[advisor] 起草文本违规,弃 LLM 版")
            return None
    return {"title": title, "station": station}
