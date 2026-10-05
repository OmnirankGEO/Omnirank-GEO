"""
P0.8 关键词意图分类 + 改写建议(CTO-15.7 2026-04-24)

触发:代理推"装修报价没有套路"(vol 300 · ¥1,411 月费 · 已选)· 避坑教育型 AI 0 品牌检出率 · 客户月底质疑不专业

老板 3 答复:
 · < 15% 红条 / 15-30% 橙条 / > 30% 绿条
 · 4 枚举先上:brand_decision / avoid_trap / info / noise
 · 改写建议 LLM 生成 · temp=0.3 · 3 个版本

特性:
 · LLM = qwen3-max(默认)· fallback DeepSeek(multi_llm_caller)
 · Redis 7 天缓存 · key=(keyword, industry, city) md5
 · 降级:LLM 失败返 info/low · 不阻塞 UI
 · json_repair 宽容解析
"""
from __future__ import annotations
import hashlib
import json
import logging
from typing import Literal, TypedDict

logger = logging.getLogger("GEO-KeywordIntent")

IntentType = Literal["brand_decision", "avoid_trap", "info", "noise"]


class IntentResult(TypedDict):
    intent: IntentType
    brand_recall_rate_estimate: float  # 0.0 - 1.0
    confidence: Literal["high", "medium", "low"]
    warning: str | None
    rewrite_suggestions: list[str]  # 3 条 LLM 改写(avoid_trap/info/noise 才给)


# ============ prompt ============
_SYSTEM_PROMPT = """你是 AI 搜索优化顾问 · 判断关键词在 AI 引擎(豆包/通义千问/DeepSeek/Kimi)中的意图类型 · 预估品牌命中率 · 必要时给改写建议。

4 种意图类型:
1. brand_decision(品牌决策型):问"哪家好/推荐/排名/哪家靠谱" · AI 会列具体品牌名 · 品牌命中率高(通常 > 0.35)
2. avoid_trap(避坑教育型):问"是否有套路/怎么避坑/骗局/警惕" · AI 给的是避坑指南 · 几乎不推荐具体品牌(通常 < 0.10)
3. info(信息搜索型):问定义/什么是/科普/介绍/区别 · AI 给百科信息 · 可能提品类但不点名品牌(通常 0.10 - 0.25)
4. noise(噪音型):和品牌所在行业无关或奇怪组合 · AI 不会相关推荐(通常 < 0.05)

改写建议规则:
- intent=brand_decision 时不需要改写(rewrite_suggestions=[])
- intent=avoid_trap / info / noise 时 · 给 3 条改写建议(品牌导向问法 + 加地域/行业锚点)
- 改写示例:"装修报价没有套路" → ["罗平 家装 靠谱推荐", "罗平装修公司 哪家好", "罗平 装修 排名"]

返回严格 JSON(不含任何额外文字):
{
  "intent": "brand_decision|avoid_trap|info|noise",
  "brand_recall_rate_estimate": 0.15,
  "confidence": "high|medium|low",
  "warning": "该词 AI 以避坑教育回答为主 · 很少推荐具体品牌" | null,
  "rewrite_suggestions": ["改写 1", "改写 2", "改写 3"]
}"""


def _cache_key(keyword: str, industry: str, city: str) -> str:
    raw = f"{keyword}|{industry}|{city}".encode("utf-8")
    return f"kw_intent:{hashlib.md5(raw).hexdigest()}"


def _degraded_result() -> IntentResult:
    """LLM 失败降级 · 不阻塞 UI(info + low confidence)"""
    return {
        "intent": "info",
        "brand_recall_rate_estimate": 0.20,
        "confidence": "low",
        "warning": "分类服务暂不可用 · 请人工审核该关键词是否符合品牌决策意图",
        "rewrite_suggestions": [],
    }


def _validate_and_normalize(parsed: dict) -> IntentResult:
    """校验 LLM 输出 + 归一化(容错)"""
    intent = parsed.get("intent", "info")
    if intent not in ("brand_decision", "avoid_trap", "info", "noise"):
        intent = "info"

    try:
        rate = float(parsed.get("brand_recall_rate_estimate", 0.20))
        rate = max(0.0, min(1.0, rate))  # clamp 0-1
    except (TypeError, ValueError):
        rate = 0.20

    confidence = parsed.get("confidence", "medium")
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    warning = parsed.get("warning")
    if warning == "" or warning == "null":
        warning = None

    rewrites = parsed.get("rewrite_suggestions") or []
    if not isinstance(rewrites, list):
        rewrites = []
    rewrites = [str(r).strip() for r in rewrites if r and str(r).strip()]
    rewrites = rewrites[:3]  # 最多 3 条

    return {
        "intent": intent,  # type: ignore[typeddict-item]
        "brand_recall_rate_estimate": rate,
        "confidence": confidence,  # type: ignore[typeddict-item]
        "warning": warning,
        "rewrite_suggestions": rewrites,
    }


async def classify_keyword_intent(
    keyword: str,
    brand_context: dict,
    *,
    use_cache: bool = True,
) -> IntentResult:
    """
    分类关键词意图 + 返改写建议

    Args:
        keyword: 关键词 · 如 "装修报价没有套路"
        brand_context: {industry, city, brand_name} · 至少 industry+city
        use_cache: 是否用 Redis 7 天缓存(default True)

    Returns:
        IntentResult · LLM 失败降级到 info/low 不阻塞
    """
    if not keyword or not keyword.strip():
        return _degraded_result()

    industry = (brand_context or {}).get("industry", "") or ""
    city = (brand_context or {}).get("city", "") or ""
    brand_name = (brand_context or {}).get("brand_name", "") or ""

    # 缓存命中
    cache_key = _cache_key(keyword, industry, city)
    if use_cache:
        try:
            from cache.redis_client import redis_get_json
            cached = redis_get_json(cache_key)
            if cached:
                return _validate_and_normalize(cached)
        except Exception as e:
            logger.debug(f"[keyword_intent] 缓存读取异常(忽略): {e}")

    # 调 LLM
    user_msg = f"""品牌场景:
- 品牌名:{brand_name or '(未指定)'}
- 行业:{industry or '(未指定)'}
- 城市:{city or '(全国)'}

关键词:「{keyword}」

请分析该关键词的意图类型 + 品牌命中率 + 必要时改写建议 · 返严格 JSON。"""

    try:
        from tools.multi_llm_caller import call_llm_with_fallback

        full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_msg}"
        content = await call_llm_with_fallback(
            full_prompt,
            verbose=False,
            # [CTO-15.23 2026-05-11 Bug E] temperature 0.3 → 0.1 报价稳定 · 防 intent 抖动
            temperature=0.1,
            max_tokens=500,
        )

        if not content or "[所有API均失败]" in content:
            return _degraded_result()

        # json_repair 宽容解析
        try:
            from json_repair import repair_json
            parsed = json.loads(repair_json(content))
        except Exception:
            # 末路:正则提取 JSON 块
            import re
            match = re.search(r"\{[\s\S]*\}", content)
            if not match:
                return _degraded_result()
            try:
                parsed = json.loads(match.group(0))
            except Exception:
                return _degraded_result()

        if not isinstance(parsed, dict):
            return _degraded_result()

        result = _validate_and_normalize(parsed)

        # 写缓存(7 天)
        if use_cache:
            try:
                from cache.redis_client import redis_set_json
                redis_set_json(cache_key, result, ex=7 * 24 * 3600)
            except Exception as e:
                logger.debug(f"[keyword_intent] 缓存写入异常(忽略): {e}")

        return result

    except Exception as e:
        logger.warning(f"[keyword_intent] LLM 调用异常: {e}")
        return _degraded_result()


async def classify_keywords_batch(
    keywords: list[str],
    brand_context: dict,
    *,
    use_cache: bool = True,
    concurrency: int = 8,
) -> dict[str, IntentResult]:
    """
    批量分类 · asyncio.gather 并发 · 按 (keyword, industry, city) 缓存去重

    Returns:
        {keyword: IntentResult}
    """
    import asyncio

    if not keywords:
        return {}

    # 去重
    unique_keywords = list(dict.fromkeys(k for k in keywords if k and k.strip()))
    sem = asyncio.Semaphore(concurrency)

    async def _one(kw: str) -> tuple[str, IntentResult]:
        async with sem:
            result = await classify_keyword_intent(kw, brand_context, use_cache=use_cache)
            return kw, result

    results = await asyncio.gather(
        *[_one(kw) for kw in unique_keywords],
        return_exceptions=False,
    )
    return dict(results)


# ============ 辅助 · 供前端映射颜色档位 ============
def describe_health_level(rate: float) -> Literal["danger", "warn", "ok"]:
    """
    老板批 3 档:
      · rate < 0.15 → danger(红)
      · 0.15 ≤ rate < 0.30 → warn(橙)
      · rate ≥ 0.30 → ok(绿)
    """
    if rate < 0.15:
        return "danger"
    if rate < 0.30:
        return "warn"
    return "ok"
