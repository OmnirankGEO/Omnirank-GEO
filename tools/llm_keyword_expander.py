"""LLM-first 关键词扩展 · 替代现有 keyword_expander 的硬约束 region_drill 路径

方案 §3.1:
- 删除 region_drill "按权重分配区县" 硬塞
- LLM 自由决定地域 vs 通用比例(默认 50/50)
- 必须含 ≥ 30% 通用商业意图词(品牌/对比/价格/工厂/材料)
- 避免同地域 + 同核心词机械组合堆砌

工程门禁(§4.6 + §4.7):
- 输出每词带 business_line(让 LLM 一次性归并 · 不需要后续 rule 合并)
- 通过 LLMPricedKeyword schema 校验
- 双轨过渡 · 不动现有 keyword_expander · 仅在 flag ON 时调用本模块

调用入口:
    from tools.llm_keyword_expander import llm_expand_keywords
    result = await llm_expand_keywords(
        core_keywords=["全屋定制"],
        industry="家居建材",
        city="深圳",
        business_scope="全屋定制设计、衣柜",
        target_count=20,
    )
"""

from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import asyncio
import logging
import os
import time
from typing import Any

import aiohttp

from tools.llm_pricing_schema import parse_llm_response

logger = logging.getLogger("GEO-LLM-Expander")

DEFAULT_MODEL = DEEPSEEK_OFFICIAL_FLASH
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 8000
DEFAULT_TIMEOUT_SEC = 120.0
MAX_RETRIES = 2


def _build_expand_prompt(
    core_keywords: list[str],
    industry: str,
    city: str,
    business_scope: str,
    target_count: int,
) -> str:
    return f"""你是 OmniRank AI 资深 GEO 报价分析师 · 端到端为代理生成完整报价方案。

业务背景:
- OmniRank 帮品牌在 AI 搜索引擎(DeepSeek/Kimi/豆包)被推荐
- 通过发布权威内容让 AI 引用品牌
- 单条内容成本 60 元 · 代理售价 = 成本 × 2-5 倍

3 档套餐: 入门 15% SOV · 标准 25% SOV(主推) · 旗舰 33% SOV

代理输入:
- 核心词: {", ".join(core_keywords)}
- 行业: {industry}
- 客户城市: {city}
- 业务范围: {business_scope}
- 目标扩出关键词数: {target_count} 个

请端到端完成:

任务 1 · 扩词(基于行业 + 城市 + 业务范围)
- 真实搜索习惯:用户在 AI 搜索时会怎么问?
- 覆盖 3 类:头部品类词 / 地域 + 决策长尾 / 商业调研词
- **避免地域过度堆砌**:不要全部 "X 区 + 主词" 形式 · 50% 地域 50% 通用
- **必须含 ≥ 30% 通用商业意图词**(品牌/对比/价格/工厂/材料/选购)
- **避免纯信息词**(是什么/流程/什么意思 — 客户搜这些不下单 · 给 should_quote=false)

任务 2 · 业务线归并
- 把扩出的关键词归到 3-6 个业务线(基于客户业务范围)
- 业务线 = 客户可独立售卖的服务 · 不是地域不是修饰词
- 例:"全屋定制设计" / "衣柜定制" / "榻榻米定制" 是业务线 · "福田区全屋定制" 不是

任务 3 · 每词输出 intent/funnel/value/3 档价/理由(详见 schema)

定价指导:
- 区县 + 决策(哪家好/哪家靠谱)= 标准 ¥2000-3500
- 城市级 + 决策 = 标准 ¥1500-2500
- 头部品类(品牌推荐/排行)= 标准 ¥1500-3000
- 入门 ≈ 标准 × 0.5-0.7 · 旗舰 ≈ 标准 × 1.4-1.8 · 必须严格递增
- 信息型(查知识无购买)→ should_quote=false · 三档全 -1 · value_score=0

输出严格 JSON(不要 markdown · 不要解释):

{{
  "keywords": [
    {{
      "keyword": "...",
      "intent": "transactional|commercial|informational",
      "funnel": "decision|consideration|awareness",
      "value_score_0_5": 0.0-5.0,
      "entry_price_yuan": 整数元 或 -1,
      "standard_price_yuan": 整数元 或 -1,
      "flagship_price_yuan": 整数元 或 -1,
      "should_quote": true|false,
      "reason_zh": "30 字内推荐理由",
      "business_line": "归属业务线 8 字内"
    }}
  ]
}}

强约束:
- 字段名严格 *_yuan / *_0_5 · 禁用无单位 standard_price
- 价格人民币整数元 · 不用 5.0 倍率 · 不用 0.5 万
- should_quote=false 时三档全 -1
- 关键词不重复
- business_line 不是地域名(如"福田区全屋定制" 错 · "全屋定制设计" 对)
"""


async def _call_llm_once(
    prompt: str,
    api_key: str,
    endpoint: str,
    model: str,
    temperature: float,
) -> tuple[str, dict[str, int]]:
    from tools.llm_call_tracker import infer_platform_from_url, llm_track, usage_from_response_payload

    async with aiohttp.ClientSession() as session:
        async with llm_track(
            "llm_keyword_expander",
            infer_platform_from_url(endpoint),
            model=model,
        ) as tracker:
            async with session.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
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
                tracker.record(success=False, error_msg=f"LLM API 异常: {str(data)[:200]}")
                raise RuntimeError(f"LLM API 异常: {str(data)[:200]}")

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


async def llm_expand_keywords(
    core_keywords: list[str],
    industry: str,
    city: str,
    business_scope: str = "",
    target_count: int = 20,
    api_key: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """LLM-first 扩词 + 业务线归并 + 一站式打分

    Args:
        core_keywords: 核心词(代理输入)
        industry: brand.industry(锁定 · 不重判)
        city: brand.city
        business_scope: 业务范围(LLM 用作 business_line 归并 hint)
        target_count: 目标扩出关键词数
        api_key: 不传则从 DEEPSEEK_API_KEY env 读

    Returns:
        {
            "success": bool,
            "keywords": [LLMPricedKeyword.model_dump()...],
            "business_lines": [{"name": str, "keyword_count": int}],
            "should_quote_false_keywords": [str],
            "latency_ms": int,
            "model": str,
            "error": str | None,
        }
    """
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {"success": False, "keywords": [], "business_lines": [],
                "should_quote_false_keywords": [], "latency_ms": 0,
                "model": model, "error": "DEEPSEEK_API_KEY 未配置 · caller 走 fallback"}

    t0 = time.time()
    prompt = _build_expand_prompt(core_keywords, industry, city, business_scope, target_count)

    last_err: Exception | None = None
    batch = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            raw, _usage = await _call_llm_once(
                prompt, api_key, endpoint, model,
                temperature=DEFAULT_TEMPERATURE + 0.05 * attempt,
            )
            batch = parse_llm_response(raw)
            break
        except Exception as exc:
            last_err = exc
            logger.warning("llm_expand retry %d/%d: %s", attempt + 1, MAX_RETRIES + 1, exc)
            await asyncio.sleep(1.0 * (attempt + 1))

    if batch is None:
        return {"success": False, "keywords": [], "business_lines": [],
                "should_quote_false_keywords": [], "latency_ms": int((time.time() - t0) * 1000),
                "model": model, "error": f"LLM 扩词 retry {MAX_RETRIES + 1} 次失败: {last_err}"}

    from tools.llm_pricing_schema import apply_price_soft_guard
    guarded = [apply_price_soft_guard(kw) for kw in batch.keywords]

    bl_count: dict[str, int] = {}
    for kw in guarded:
        if kw.business_line:
            bl_count[kw.business_line] = bl_count.get(kw.business_line, 0) + 1
    business_lines = [{"name": name, "keyword_count": cnt} for name, cnt in sorted(bl_count.items(), key=lambda x: -x[1])]

    should_quote_false = [kw.keyword for kw in guarded if not kw.should_quote]

    latency_ms = int((time.time() - t0) * 1000)
    logger.info("llm_expand_keywords OK · cores=%d · expanded=%d · skipped=%d · BL=%d · latency=%dms",
                len(core_keywords), len(guarded), len(should_quote_false), len(business_lines), latency_ms)

    return {
        "success": True,
        "keywords": [kw.model_dump() for kw in guarded],
        "business_lines": business_lines,
        "should_quote_false_keywords": should_quote_false,
        "latency_ms": latency_ms,
        "model": model,
        "error": None,
    }
