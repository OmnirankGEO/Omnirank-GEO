"""GEO research article intent classifier using DeepSeek via DashScope."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from tools.llm_call_tracker import llm_track, usage_from_response_payload

logger = logging.getLogger("GEO-ResearchMonitor.ArticleIntent")

DASHSCOPE_COMPAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_INTENT_MODEL = os.getenv("RESEARCH_ARTICLE_INTENT_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
MAX_EXCERPT_CHARS = 5000

ARTICLE_INTENT_LABELS = {
    "ranking": "榜单推荐型",
    "tutorial": "指南教程型",
    "long_form": "资讯/长文型",
    "comparison": "对比评测型",
    "data_report": "数据报告型",
    "policy": "政策权威型",
    "definition": "定义百科型",
    "faq": "FAQ型",
}
ARTICLE_INTENT_TYPES = tuple(ARTICLE_INTENT_LABELS.keys())


@dataclass(frozen=True)
class IntentClassification:
    intent_type: str
    confidence: float
    reason: str
    model: str


def _research_dashscope_key() -> str:
    return os.getenv("DASHSCOPE_API_KEY", "").strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise ValueError("empty_intent_response")
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, flags=re.S)
        if not m:
            raise
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("intent_response_not_object")
    return data


def normalize_intent_result(payload: Mapping[str, Any], *, model: str = DEFAULT_INTENT_MODEL) -> IntentClassification:
    intent_type = str(payload.get("intent_type") or "").strip()
    if intent_type not in ARTICLE_INTENT_LABELS:
        raise ValueError(f"invalid_intent_type:{intent_type}")
    try:
        confidence = float(payload.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(payload.get("reason") or "").strip()
    if len(reason) > 500:
        reason = reason[:500]
    return IntentClassification(intent_type=intent_type, confidence=confidence, reason=reason, model=model)


def build_intent_prompt(*, title: str, url: str, domain: str, content: str) -> list[dict[str, str]]:
    excerpt = (content or "")[:MAX_EXCERPT_CHARS]
    system = (
        "你是中文内容策略分析师。请判断文章的主要写作意图,只能从 8 个类型中选 1 个。"
        "优先根据正文结构判断,不要只看标题关键词。只返回 JSON。"
    )
    user = f"""分类定义:
- ranking: 榜单、排行、推荐清单、TOP、哪家好、品牌/机构/产品推荐。
- tutorial: 指南、教程、流程、步骤、方法、避坑、选购/办理/使用说明。
- long_form: 资讯、新闻、观点、深度长文、行业观察,不以排名/教程/报告为主。
- comparison: 对比、评测、横评、A vs B、优缺点比较、多个方案比较。
- data_report: 数据报告、白皮书、调研报告、统计解读、趋势报告。
- policy: 政策、法规、通知、办法、标准、官方权威解读。
- definition: 定义、百科、概念解释、是什么、基础知识科普。
- faq: 问答、FAQ、常见问题集合,正文以多个问题回答为主。

要求:
1. 只返回 JSON,格式: {{"intent_type":"ranking","confidence":0.92,"reason":"文章主体是推荐榜单"}}
2. 如果标题和正文冲突,以正文结构为准。
3. confidence 是 0 到 1 的小数,reason 用一句中文说明。

标题: {title or ''}
域名: {domain or ''}
URL: {url or ''}
正文节选:
{excerpt}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def classify_article_intent(
    *,
    title: str,
    url: str,
    domain: str,
    content: str,
    model: str = DEFAULT_INTENT_MODEL,
) -> IntentClassification:
    api_key = _research_dashscope_key()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置,无法进行文章意图分类")

    payload = {
        "model": model,
        "messages": build_intent_prompt(title=title, url=url, domain=domain, content=content),
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with llm_track(
            "research_monitor",
            "dashscope",
            model=model,
            metadata={"task": "article_intent_classification"},
        ) as tracker:
            try:
                resp = await client.post(DASHSCOPE_COMPAT_URL, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=True,
                )
            except Exception as exc:
                tracker.record(success=False, error_msg=str(exc)[:500])
                raise
    content_text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return normalize_intent_result(_extract_json_object(content_text), model=model)
