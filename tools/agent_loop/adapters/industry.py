from __future__ import annotations

from typing import Any

from tools.industry_knowledge_collector import get_industry_knowledge


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(item) for item in value.values() if item]
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("name") or item.get("title") or item.get("text") or item.get("content") or item.get("point")
                if text:
                    result.append(str(text))
            elif item:
                result.append(str(item))
        return result
    return [str(value)]


def _candidate_answers(knowledge: dict, limit: int = 5) -> list[str]:
    candidates: list[str] = []
    audience = knowledge.get("target_audience") if isinstance(knowledge, dict) else {}
    if isinstance(audience, dict):
        if audience.get("primary"):
            candidates.append(f"目标客户可以先从「{audience['primary']}」切入。")
        factors = _as_list(audience.get("decision_factors"))
        if factors:
            candidates.append(f"决策因素优先问：{'、'.join(factors[:4])}。")
    for label, key in (
        ("常见痛点", "pain_points"),
        ("典型产品", "typical_products"),
        ("服务范围", "service_scope"),
        ("内容角度", "content_angles"),
    ):
        items = _as_list(knowledge.get(key) if isinstance(knowledge, dict) else None)
        if items:
            candidates.append(f"{label}：{'、'.join(items[:4])}。")
    deduped: list[str] = []
    for item in candidates:
        text = item.strip()
        if text and text not in deduped:
            deduped.append(text)
        if len(deduped) >= max(1, int(limit or 5)):
            break
    return deduped


def _sync_query(industry: str, question: str, country: str, language: str, limit: int) -> dict:
    knowledge = get_industry_knowledge(industry, level="industry") or {}
    return {
        "source": "industry_knowledge",
        "industry": industry,
        "country": country,
        "language": language,
        "question": question,
        "knowledge": knowledge,
        "candidate_answers": _candidate_answers(knowledge, limit=limit),
        "fallback_policy": "ask_or_offer_choices_before_guessing",
        "limit": limit,
    }


async def query(industry: str, question: str, country: str = "", language: str = "", limit: int = 5) -> dict:
    # H-P0-1 fix · sync DB/file IO → to_thread (feedback_async_event_loop_block)
    import asyncio
    return await asyncio.to_thread(_sync_query, industry, question, country, language, limit)
