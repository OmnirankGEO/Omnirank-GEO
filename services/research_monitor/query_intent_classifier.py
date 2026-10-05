"""GEO research QUERY intent classifier (W1 · 语料标签层).

Reuses the 8-class taxonomy from `article_intent_classifier` (SSOT for labels),
but classifies the *search query / problem* itself (not an article body). This is
the "不同问题的不同标准答案" axis: knowing a query is a 榜单型 vs 对比型 vs 教程型
problem lets the distiller (W2) group corpora by (行业 × query_intent × style).

Same cheap model as article intent (deepseek-v4-flash via DashScope compat-mode),
temperature=0, JSON output. Persistence lives in db.writing_query_intent_db.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any, Mapping

import httpx

from services.research_monitor.article_intent_classifier import (
    ARTICLE_INTENT_LABELS,
    IntentClassification,
    _extract_json_object,
)
from tools.llm_call_tracker import llm_track, usage_from_response_payload

logger = logging.getLogger("GEO-ResearchMonitor.QueryIntent")

DASHSCOPE_COMPAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_QUERY_INTENT_MODEL = (
    os.getenv("RESEARCH_QUERY_INTENT_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
)

# 复用 article intent 的 8 类标签(不另造第 2 份口径)
QUERY_INTENT_LABELS = ARTICLE_INTENT_LABELS
QUERY_INTENT_TYPES = tuple(QUERY_INTENT_LABELS.keys())


def _dashscope_key() -> str:
    return os.getenv("DASHSCOPE_API_KEY", "").strip()


def normalize_query_intent_result(
    payload: Mapping[str, Any], *, model: str = DEFAULT_QUERY_INTENT_MODEL
) -> IntentClassification:
    intent_type = str(payload.get("intent_type") or "").strip()
    if intent_type not in QUERY_INTENT_LABELS:
        raise ValueError(f"invalid_query_intent:{intent_type}")
    try:
        confidence = float(payload.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(payload.get("reason") or "").strip()[:500]
    return IntentClassification(intent_type=intent_type, confidence=confidence, reason=reason, model=model)


def build_query_intent_prompt(*, query: str, industry: str = "") -> list[dict[str, str]]:
    system = (
        "你是中文搜索意图分析师。判断一个搜索问题背后用户想要的答案形态,只能从 8 个类型中选 1 个。"
        "关注用户'想得到什么样的答案',不要被行业词干扰。只返回 JSON。"
    )
    user = f"""分类定义(按用户想要的答案形态):
- ranking: 想要榜单/推荐清单/哪家好/TOP 几/品牌机构产品推荐。
- tutorial: 想要指南/教程/步骤/怎么做/怎么选/怎么办理/避坑方法。
- long_form: 想要资讯/观点/深度了解某话题,不指向排名/教程/报告。
- comparison: 想要对比/评测/A 和 B 哪个好/优缺点比较/多个方案取舍。
- data_report: 想要数据/统计/行情/报告/趋势数字/市场规模。
- policy: 想要政策/法规/标准/官方规定/合规要求。
- definition: 想问是什么/定义/概念/基础科普/原理。
- faq: 单点具体问题的直接解答(常见问题、能不能、多少钱这类一问一答)。

要求:
1. 只返回 JSON,格式: {{"intent_type":"ranking","confidence":0.9,"reason":"用户想要推荐榜单"}}
2. confidence 是 0 到 1 的小数,reason 用一句中文说明。
3. 若问题同时含多个意图,取最主要的一个。

行业: {industry or '(未提供)'}
搜索问题: {query or ''}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def classify_query_intent(
    *,
    query: str,
    industry: str = "",
    model: str = DEFAULT_QUERY_INTENT_MODEL,
) -> IntentClassification:
    api_key = _dashscope_key()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置,无法进行 query 意图分类")

    payload = {
        "model": model,
        "messages": build_query_intent_prompt(query=query, industry=industry),
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with llm_track(
            "research_monitor",
            "dashscope",
            model=model,
            metadata={"task": "query_intent_classification"},
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
    return normalize_query_intent_result(_extract_json_object(content_text), model=model)


# ---- 成本预估(dry_run 用,与 answer-entity/backfill 一致的口径:粗略 token × 单价)----
# deepseek-v4-flash 每次分类约 input 300 + output 40 tokens。单价以千 token 计(保守)。
_QUERY_INTENT_INPUT_TOKENS = 300
_QUERY_INTENT_OUTPUT_TOKENS = 40


def estimate_query_intent_cost(count: int) -> dict[str, Any]:
    """dry_run 成本预估:返回条数 + 预估 token + 预估费用(人民币,保守上限口径)。"""
    n = max(0, int(count or 0))
    # deepseek-v4-flash 约 ¥0.001/千 input token,¥0.004/千 output token(保守取整)。
    in_tokens = n * _QUERY_INTENT_INPUT_TOKENS
    out_tokens = n * _QUERY_INTENT_OUTPUT_TOKENS
    est_cost = round(in_tokens / 1000 * 0.001 + out_tokens / 1000 * 0.004, 4)
    return {
        "count": n,
        "est_input_tokens": in_tokens,
        "est_output_tokens": out_tokens,
        "est_cost_cny": est_cost,
        "model": DEFAULT_QUERY_INTENT_MODEL,
    }


# ============ backfill 编排(后台任务 · 内建 async 互斥 · 有界并发 · 逐条 fail-soft)============
# 🔴 禁止在 async 端点里 inline 全库(run_rebuild_guarded 是 sync-only,包不住 async LLM),
#    真跑走 asyncio.create_task(run_query_intent_backfill(...)) + 本模块内 async 互斥(见答案实体同款)。
# [出口审核修] threading.Lock:backfill 从 scheduler 新事件循环 + API 主循环两处触发,
# asyncio.Lock 不跨循环互斥;threading.Lock + acquire(blocking=False) 跨线程/循环真互斥。
_BACKFILL_LOCK = threading.Lock()
_QUERY_INTENT_CONCURRENCY = max(1, int(os.getenv("QUERY_INTENT_BACKFILL_CONCURRENCY", "4")))


def is_query_intent_backfill_running() -> bool:
    return _BACKFILL_LOCK.locked()


async def run_query_intent_backfill(
    *,
    industry: str | None = None,
    limit: int = 500,
    dry_run: bool = True,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """给尚未打标签的搜索问题批量分类。dry_run 只计数 + 预估成本(零 LLM)。

    真跑:内建线程锁互斥(同时只跑一个 backfill),有界并发,单条失败 fail-soft 记 failed
    不拖垮整批。上限由调用方 limit 控制(端点/scheduler 各带 cap)。
    """
    # 延迟 import,避免 db 层与 service 层在模块加载期形成环
    from db.writing_query_intent_db import list_unclassified_queries, upsert_query_intent

    # [review fix] 全表 GROUP BY 扫描是同步重活:挪线程池,不堵 uvicorn 唯一事件循环(WORKERS=1)
    targets = await asyncio.to_thread(list_unclassified_queries, industry=industry, limit=limit)
    if dry_run:
        return {
            "mode": "dry_run",
            "industry": industry or "all",
            "target_count": len(targets),
            **estimate_query_intent_cost(len(targets)),
            "targets_preview": [
                {"industry": t["industry"], "query": t["query"][:80], "freq": t["freq"]}
                for t in targets[:10]
            ],
        }

    if not targets:
        return {"status": "completed", "classified": 0, "failed": 0, "target_count": 0}
    if not _BACKFILL_LOCK.acquire(blocking=False):
        return {"status": "in_progress", "message": "query 分类 backfill 正在进行中,请稍后再试。"}
    try:
        sem = asyncio.Semaphore(max(1, int(concurrency or _QUERY_INTENT_CONCURRENCY)))
        counters = {"classified": 0, "failed": 0}

        async def _one(target: dict[str, Any]) -> None:
            async with sem:
                try:
                    res = await classify_query_intent(
                        query=target["query"], industry=target["industry"]
                    )
                    upsert_query_intent(
                        industry=target["industry"],
                        query=target["query"],
                        query_intent=res.intent_type,
                        confidence=res.confidence,
                        reason=res.reason,
                        model=res.model,
                    )
                    counters["classified"] += 1
                except Exception as exc:  # fail-soft:单条失败不拖垮整批
                    counters["failed"] += 1
                    logger.warning(
                        "[query-intent] 分类失败 industry=%s query=%s: %s",
                        target.get("industry"),
                        (target.get("query") or "")[:60],
                        str(exc)[:200],
                    )

        await asyncio.gather(*[_one(t) for t in targets], return_exceptions=True)
        logger.info(
            "[query-intent] backfill 完成 classified=%s failed=%s target=%s",
            counters["classified"],
            counters["failed"],
            len(targets),
        )
        return {
            "status": "completed",
            "target_count": len(targets),
            "classified": counters["classified"],
            "failed": counters["failed"],
        }
    finally:
        _BACKFILL_LOCK.release()
