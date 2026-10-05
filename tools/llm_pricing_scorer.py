"""LLM-first 报价核心 · 一站式打分 + 算价 + 信息型剔除

老板 2026-05-12 LLM-first 全面改造方案 §3.4:
- 删除 compute_value_score 硬公式 + clamp [0.8, 2.0]
- 删除 INTENT_VALUE / FUNNEL_VALUE 硬编码 dict
- LLM 直接输出 0-5 价值评分 · 三档价格(integer yuan)· should_quote 标记

工程门禁(§4.6 + §4.7):
- Pydantic 强 schema 校验 · 失败整批 retry
- N=2 sample 取均 · 共识投票
- json_repair 兜底
- 价格软护栏 needs_review · 不覆盖 LLM
- fallback 时 carry should_quote=false 列表 · 不绕过信息型剔除

调用入口:
    from tools.llm_pricing_scorer import llm_score_and_price
    result = await llm_score_and_price(
        keywords=[{"keyword": "..."}, ...],
        industry="家居建材",
        city="深圳",
        business_scope="全屋定制设计、衣柜定制",
        n_samples=2,
    )
"""

from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import asyncio
import logging
import os
import statistics
import time
from typing import Any

from tools.llm_pricing_schema import (
    LLMPricedKeyword,
    LLMPricingBatch,
    apply_price_soft_guard,
    parse_llm_response,
)

logger = logging.getLogger("GEO-LLM-Pricing")

DEFAULT_MODEL = DEEPSEEK_OFFICIAL_FLASH
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 8000
DEFAULT_TIMEOUT_SEC = 90.0
MAX_RETRIES = 2


def _build_prompt(
    keywords: list[dict[str, Any]],
    industry: str,
    city: str,
    business_scope: str,
) -> str:
    """构造 LLM prompt · 强 schema 约束"""
    kw_lines = []
    for i, kw in enumerate(keywords, 1):
        name = kw.get("keyword", "")
        sv = kw.get("search_volume")
        cc = kw.get("competitor_count")
        sem = kw.get("sem_price")
        ctx = []
        if sv is not None:
            ctx.append(f"搜索量 {sv}")
        if cc is not None:
            ctx.append(f"竞品 {cc}")
        if sem:
            ctx.append(f"5118 SEM ¥{sem:.1f}")
        ctx_str = f"  [{' · '.join(ctx)}]" if ctx else ""
        kw_lines.append(f"{i}. {name}{ctx_str}")

    return f"""你是 OmniRank AI 资深 GEO 报价分析师。

业务背景:
- OmniRank 帮品牌在 AI 搜索引擎(DeepSeek/Kimi/豆包)被推荐
- 通过发布权威内容(知乎/小红书/媒体)让 AI 引用品牌
- 单条内容成本 60 元 · 代理售价 = 成本 × 2-5 倍

3 档套餐:
- 入门版 = 拿 15% SOV(用户问 7 次出现 1 次)· 基础曝光
- 标准版 = 拿 25% SOV(用户问 3 次出现 2 次)· 主推
- 旗舰版 = 拿 33% SOV(高频曝光)· 溢价

定价原则(LLM-first · 不要套公式):
- 转化最强的词(地域 + 决策意图 + 客户准备下单)= 客户最愿付钱 → 高价
- 信息型词(用户只查知识不下单)= **无报价价值** · should_quote=false · 三档全 -1
- 凭你对该行业 GEO 业务的理解给出真实合理价 · 范围 100-10000 元

定价区间参考(标准版主推 · 你自己判断):
- 区县 + 决策(哪家好/哪家靠谱)= 客户准备成交 + 锁定本地 → 标准 ¥2000-3500
- 城市级 + 决策 = 转化中等 → 标准 ¥1500-2500
- 头部品类(品牌推荐/排行)= 调研意图 → 标准 ¥1500-3000
- 3 档比例:入门 ≈ 标准 × 0.5-0.7 · 旗舰 ≈ 标准 × 1.4-1.8 · 必须严格递增

行业: {industry}  ·  城市: {city}  ·  客户业务范围: {business_scope}

请对以下关键词逐一输出。**严格 JSON · 不要 markdown · 不要解释**:

{{
  "keywords": [
    {{
      "keyword": "原文",
      "intent": "transactional|commercial|informational",
      "funnel": "decision|consideration|awareness",
      "value_score_0_5": 0.0-5.0 浮点 · 信息型 0,
      "entry_price_yuan": 整数元 · should_quote=false 时为 -1,
      "standard_price_yuan": 整数元 · should_quote=false 时为 -1,
      "flagship_price_yuan": 整数元 · should_quote=false 时为 -1,
      "should_quote": true|false,
      "reason_zh": "30 字内 · 面向代理给客户讲清楚",
      "business_line": "该词归属的业务线名 8 字内 · 例 全屋定制设计 / 衣柜定制"
    }}
  ]
}}

强约束:
- 字段名严格 *_yuan / *_0_5 · 不要用 standard_price 这种无单位字段
- 价格必须人民币整数元 · 不要用 5.0 这种倍率 · 不要用 0.5 万这种单位
- should_quote=false 时三档全 -1 · should_quote=true 时三档严格递增
- intent=informational + funnel=awareness 默认 should_quote=false(纯查知识无购买意图)
- business_line 必须是真业务(可独立报价的服务)· 不要用"福田区全屋定制"这种地域伪业务线

关键词列表:
{chr(10).join(kw_lines)}
"""


async def _call_llm_once(
    prompt: str,
    api_key: str,
    endpoint: str,
    model: str,
    temperature: float,
) -> tuple[str, dict[str, int]]:
    """单次 LLM call · 返回 (raw_text, usage)"""
    import aiohttp
    from tools.llm_call_tracker import infer_platform_from_url, llm_track, usage_from_response_payload

    async with aiohttp.ClientSession() as session:
        async with llm_track(
            "llm_pricing_scorer",
            infer_platform_from_url(endpoint),
            model=model,
        ) as tracker:
            async with session.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": DEFAULT_MAX_TOKENS,
                },
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SEC),
            ) as resp:
                data = await resp.json()

            if "choices" not in data:
                tracker.record(success=False, error_msg=f"LLM API 异常返回: {str(data)[:200]}")
                raise RuntimeError(f"LLM API 异常返回: {str(data)[:200]}")

            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
            tracker.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                success=True,
            )
            raw = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {}) or {}
            return raw, usage


async def _call_with_retry(
    prompt: str,
    api_key: str,
    endpoint: str,
    model: str,
    expected_keywords: set[str] | None = None,
) -> LLMPricingBatch:
    """LLM call 带 retry · 每次 retry 微调 temperature

    [CTO-15.23 2026-05-12 P1 修] 老板审计:加输入/输出关键词全量校验
      LLM 少吐一个词 / 改写一个词 → 此 sample 视为 fail · 触发 retry
      防静默漏报价

    Args:
        expected_keywords: 输入关键词集合 · 用于全量返回校验

    Raises:
        ValueError: 所有 retry 后仍校验失败
    """
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        temp = DEFAULT_TEMPERATURE + 0.05 * attempt
        try:
            raw, _usage = await _call_llm_once(prompt, api_key, endpoint, model, temp)
            batch = parse_llm_response(raw)
            if expected_keywords is not None:
                returned = {kw.keyword for kw in batch.keywords}
                missing = expected_keywords - returned
                extra = returned - expected_keywords
                if missing or extra:
                    raise ValueError(
                        f"LLM 返回关键词集合不匹配 · "
                        f"missing({len(missing)})={list(missing)[:3]} · "
                        f"extra({len(extra)})={list(extra)[:3]} · "
                        f"expected {len(expected_keywords)} returned {len(returned)}"
                    )
            return batch
        except Exception as exc:
            last_err = exc
            logger.warning("LLM call retry %d/%d 失败: %s", attempt + 1, MAX_RETRIES + 1, exc)
    raise ValueError(f"LLM 报价 retry {MAX_RETRIES + 1} 次后仍失败: {last_err}")


def _aggregate_samples(samples: list[LLMPricingBatch]) -> list[LLMPricedKeyword]:
    """N sample 聚合:价格取均 · intent/funnel 共识投票 · should_quote 取多数

    若任一 sample 标 should_quote=false · 直接尊重(防信息型词漏过)
    """
    if not samples:
        return []

    keyword_to_objs: dict[str, list[LLMPricedKeyword]] = {}
    for batch in samples:
        for kw in batch.keywords:
            keyword_to_objs.setdefault(kw.keyword, []).append(kw)

    aggregated: list[LLMPricedKeyword] = []
    for keyword, objs in keyword_to_objs.items():
        intents = [o.intent for o in objs]
        funnels = [o.funnel for o in objs]
        should_quotes = [o.should_quote for o in objs]
        intent_c = max(set(intents), key=intents.count)
        funnel_c = max(set(funnels), key=funnels.count)
        should_quote_c = sum(1 for x in should_quotes if x) > len(should_quotes) / 2
        if any(not x for x in should_quotes):
            should_quote_c = False

        if not should_quote_c:
            entry_p = standard_p = flagship_p = -1
            value_score = 0.0
        else:
            entry_real = [o.entry_price_yuan for o in objs if o.entry_price_yuan > 0]
            std_real = [o.standard_price_yuan for o in objs if o.standard_price_yuan > 0]
            flag_real = [o.flagship_price_yuan for o in objs if o.flagship_price_yuan > 0]
            if not (entry_real and std_real and flag_real):
                entry_p = standard_p = flagship_p = -1
                should_quote_c = False
                value_score = 0.0
            else:
                entry_p = round(statistics.mean(entry_real))
                standard_p = round(statistics.mean(std_real))
                flagship_p = round(statistics.mean(flag_real))
                if not (entry_p < standard_p < flagship_p):
                    span = max(standard_p, 100)
                    entry_p = max(100, round(span * 0.6))
                    flagship_p = round(span * 1.6)
                vss = [o.value_score_0_5 for o in objs]
                value_score = round(statistics.mean(vss), 2)

        reasons = [o.reason_zh for o in objs if o.reason_zh]
        reason = max(reasons, key=len) if reasons else ""

        business_lines = [o.business_line for o in objs if o.business_line]
        business_line = max(set(business_lines), key=business_lines.count) if business_lines else ""

        try:
            agg = LLMPricedKeyword(
                keyword=keyword,
                intent=intent_c,
                funnel=funnel_c,
                value_score_0_5=value_score,
                entry_price_yuan=entry_p,
                standard_price_yuan=standard_p,
                flagship_price_yuan=flagship_p,
                should_quote=should_quote_c,
                reason_zh=reason or "—",
                business_line=business_line,
            )
        except Exception as exc:
            logger.warning("聚合后 schema 校验失败 · keyword=%s err=%s · 降级 should_quote=false", keyword, exc)
            agg = LLMPricedKeyword(
                keyword=keyword,
                intent="informational",
                funnel="awareness",
                value_score_0_5=0.0,
                entry_price_yuan=-1,
                standard_price_yuan=-1,
                flagship_price_yuan=-1,
                should_quote=False,
                reason_zh="聚合失败 · 已剔除",
                business_line=business_line,
                needs_review=True,
                review_reason="N=2 sample 聚合 schema 校验失败",
            )

        agg = apply_price_soft_guard(agg)
        aggregated.append(agg)

    return aggregated


async def llm_score_and_price(
    keywords: list[dict[str, Any]],
    industry: str,
    city: str,
    business_scope: str = "",
    n_samples: int = 2,
    api_key: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """LLM-first 一站式打分 + 算价

    Args:
        keywords: [{"keyword": str, "search_volume": int?, "competitor_count": int?, "sem_price": float?}]
        industry: brand.industry(由 batch_pricing _lock_brand_industry 锁定 · 此处不重判)
        city: brand.city
        business_scope: 客户业务范围(给 LLM 做 business_line 归并 hint)
        n_samples: N sample 取均 · 默认 2
        api_key: 不传则从 DEEPSEEK_API_KEY env 读

    Returns:
        {
            "success": bool,
            "keywords": [LLMPricedKeyword.model_dump()...],
            "should_quote_false_keywords": [str],  # 信息型剔除 · 给 fallback carry
            "latency_ms": int,
            "n_samples_actual": int,
            "model": str,
            "error": str | None,
        }
    """
    if not keywords:
        return {"success": True, "keywords": [], "should_quote_false_keywords": [], "latency_ms": 0,
                "n_samples_actual": 0, "model": model, "error": None}

    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {"success": False, "keywords": [], "should_quote_false_keywords": [], "latency_ms": 0,
                "n_samples_actual": 0, "model": model,
                "error": "DEEPSEEK_API_KEY 未配置 · 触发 caller fallback"}

    t0 = time.time()
    prompt = _build_prompt(keywords, industry, city, business_scope)

    # P1 修:输入关键词集合(原文)· 每个 sample 必须全量返回
    expected_keywords = {kw["keyword"] for kw in keywords if kw.get("keyword")}

    tasks = [
        _call_with_retry(prompt, api_key, endpoint, model, expected_keywords=expected_keywords)
        for _ in range(max(1, n_samples))
    ]
    sample_results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_samples: list[LLMPricingBatch] = []
    last_err: str | None = None
    for r in sample_results:
        if isinstance(r, LLMPricingBatch):
            valid_samples.append(r)
        elif isinstance(r, Exception):
            last_err = str(r)

    if not valid_samples:
        return {"success": False, "keywords": [], "should_quote_false_keywords": [],
                "latency_ms": int((time.time() - t0) * 1000), "n_samples_actual": 0,
                "model": model, "error": f"所有 sample 失败 · 触发 caller fallback · last_err={last_err}"}

    aggregated = _aggregate_samples(valid_samples)

    # P1 修:聚合后再校验全量覆盖 · 防 sample 间漏词(N=2 都漏同一词时聚合也漏)
    aggregated_keywords = {kw.keyword for kw in aggregated}
    coverage_missing = expected_keywords - aggregated_keywords
    if coverage_missing:
        return {"success": False, "keywords": [], "should_quote_false_keywords": [],
                "latency_ms": int((time.time() - t0) * 1000),
                "n_samples_actual": len(valid_samples), "model": model,
                "error": f"聚合后仍缺 {len(coverage_missing)} 个关键词: {list(coverage_missing)[:3]} · 触发 caller fallback"}

    should_quote_false = [kw.keyword for kw in aggregated if not kw.should_quote]

    latency_ms = int((time.time() - t0) * 1000)
    logger.info("llm_score_and_price OK · n_samples=%d/%d · keywords=%d · skipped=%d · latency=%dms",
                len(valid_samples), n_samples, len(aggregated), len(should_quote_false), latency_ms)

    return {
        "success": True,
        "keywords": [kw.model_dump() for kw in aggregated],
        "should_quote_false_keywords": should_quote_false,
        "latency_ms": latency_ms,
        "n_samples_actual": len(valid_samples),
        "model": model,
        "error": None,
    }
