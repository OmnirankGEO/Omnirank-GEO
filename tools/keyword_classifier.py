"""
关键词地域/品牌/词类 LLM 判定(v2.0 · 2026-06-11 · pricing v2 LLM 评估师配套)

【v2.0 2026-06-11 老板拍 一次性重构】
  - 删 _fastpath 规则字典(永远填不满 · 跟 city_tier_map COUNTY_LIST 不一致是罗平爆价根因)
  - 删 _detect_geo / _has_ambiguous_geo_suffix / _is_brand_match / _CITY_PREFIXES lazy import
  - 全 LLM 判定:复用 _llm_classify_batch(deepseek-v4-flash 兜底链)
  - 失败 fallback:保守判 national_niche + needs_review=True(不静默猜)
  - 主算价路径已切 pricing_llm_assessor.assess_keyword_pricing(内部含 keyword_type 全判定)
    本函数为兼容历史调用方保留接口签名 · 目前已无活跃调用方

返回结构跟 v1 完全兼容(keyword_type/market_scope/is_brand_keyword/is_broad/geo_level/
confidence/reason/classify_source/needs_review)· 不破任何持久化字段。

红线:
  - 不碰 billing.py / 红线 4 文件 baseline
  - LLM 失败必兜底 · 绝不 raise 让上游算价崩
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger("GEO-KeywordClassifier")

VALID_KEYWORD_TYPES = (
    "brand_owned", "local_county", "local_city", "national_niche", "national_head",
)


def _market_scope_for(kw_type: str) -> str:
    return {
        "brand_owned": "national",
        "local_county": "county",
        "local_city": "city",
        "national_niche": "national",
        "national_head": "national",
    }.get(kw_type, "national")


def _is_broad_for(kw_type: str) -> bool:
    return kw_type in ("national_niche", "national_head")


def _make_result(kw_type: str, confidence: float, source: str,
                 reason: str = "", needs_review: bool = False) -> dict:
    return {
        "keyword_type": kw_type,
        "market_scope": _market_scope_for(kw_type),
        "is_brand_keyword": kw_type == "brand_owned",
        "is_broad": _is_broad_for(kw_type),
        "geo_level": _market_scope_for(kw_type),
        "confidence": round(float(confidence), 2),
        "reason": reason,
        "classify_source": source,
        "needs_review": bool(needs_review),
    }


async def _llm_classify_batch(
    keywords: list, brand_name: str, industry: str, city: str,
    five118_data: dict, metaso_data: dict,
) -> dict:
    """批量 LLM 判定 keyword_type(deepseek-v4-flash 兜底链 · temperature 0.1)· 失败返空 dict 由调用方回落。"""
    if not keywords:
        return {}
    try:
        from tools.multi_llm_caller import MultiLLMCaller
    except Exception as exc:
        logger.warning("keyword_classifier: multi_llm_caller import 失败 · %s", exc)
        return {}

    kw_lines = []
    for i, kw in enumerate(keywords):
        f5 = (five118_data or {}).get(kw, {})
        vol = f5.get("search_volume", 0)
        kw_lines.append(f"{i + 1}. {kw}(月搜索量约 {vol})")
    kw_list_str = "\n".join(kw_lines)

    prompt = f"""你是中文搜索关键词的地域/品牌/词类判定专家。给定品牌与行业背景 · 判断每个关键词的类型。

品牌:{brand_name or '(未提供)'}
行业:{industry or '(未提供)'}
默认城市:{city or '(未提供)'}

关键词列表:
{kw_list_str}

对每个关键词输出:
- keyword_type:五选一
  * brand_owned     = 含本品牌名的品牌词(固定低价 · 不与同行竞争)
  * local_county    = 县级/乡镇地域词(如"罗平装修"、"晋江搬家")
  * local_city      = 地级市及以上城市地域词(如"深圳全屋定制")
  * national_niche  = 无地名的全国细分/高客单词(如"别墅电梯定制"、"私人飞机租赁")
  * national_head   = 无地名的全国大词/红海词(如"装修公司"、"法律咨询")
- market_scope: national / province / city / county / township
- is_brand_keyword: true/false(是否含本品牌名)
- geo_level: national / province / city / county / township
- confidence: 0-1(你的置信度)
- reason: 简短中文理由(后台复盘用 · ≤ 50 字)
- needs_review: true/false(你不确定时设 true)

判定要点:
1. "XX区"若是"园区/小区/景区/产业园区"等非行政地名 → 不算地域词 · 按全国词判。
2. 含本品牌名 → brand_owned(即使带地名)。
3. 无任何地名 + 是某类产品/服务品类 → national_niche 或 national_head(按是否红海大词)。
4. 不确定就 needs_review=true · 别硬猜。

严格按 JSON 数组返回(不要任何其他文字):
[
  {{"keyword": "关键词1", "keyword_type": "national_niche", "market_scope": "national", "is_brand_keyword": false, "geo_level": "national", "confidence": 0.8, "reason": "...", "needs_review": false}}
]"""

    try:
        caller = MultiLLMCaller(temperature=0.1)
        content, _provider = await caller.call(prompt, verbose=False)
        json_match = re.search(r"\[.*\]", content, re.DOTALL)
        if not json_match:
            raise ValueError("LLM 响应无 JSON 数组")
        items = json.loads(json_match.group())
        results = {}
        for item in items:
            kw = item.get("keyword", "")
            matched_kw = kw
            if kw not in keywords:
                for original in keywords:
                    if kw and (kw in original or original in kw):
                        matched_kw = original
                        break
            kw_type = item.get("keyword_type", "")
            if kw_type not in VALID_KEYWORD_TYPES:
                continue
            results[matched_kw] = {
                "keyword_type": kw_type,
                "market_scope": item.get("market_scope") or _market_scope_for(kw_type),
                "is_brand_keyword": bool(item.get("is_brand_keyword", kw_type == "brand_owned")),
                "is_broad": _is_broad_for(kw_type),
                "geo_level": item.get("geo_level") or _market_scope_for(kw_type),
                "confidence": round(float(item.get("confidence", 0.6) or 0.6), 2),
                "reason": (item.get("reason") or "")[:200],
                "classify_source": "llm",
                "needs_review": bool(item.get("needs_review", False)),
            }
        return results
    except Exception as exc:
        logger.warning("keyword_classifier: LLM 判定失败 · 回落兜底 · %s", exc)
        return {}


async def classify_keyword_types(
    keywords: list,
    brand_name: str = "",
    brand_aliases=None,
    industry: str = "",
    city: str = "",
    five118_data: Optional[dict] = None,
    metaso_data: Optional[dict] = None,
) -> dict:
    """[v2.0 2026-06-11] 全 LLM 判定 keyword_type/market_scope/is_brand/geo_level · 无 fastpath。

    Returns: {keyword: {keyword_type, market_scope, is_brand_keyword, is_broad, geo_level,
                        confidence, reason, classify_source, needs_review}}
    """
    five118_data = five118_data or {}
    metaso_data = metaso_data or {}
    if not keywords:
        return {}

    llm_results = await _llm_classify_batch(
        list(keywords), brand_name, industry, city, five118_data, metaso_data
    )

    out = {}
    for kw in keywords:
        r = llm_results.get(kw)
        if r and r.get("confidence", 0) >= 0.5:
            out[kw] = r
        else:
            out[kw] = _make_result(
                "national_niche", 0.5, "fallback",
                "LLM 失败回落 · 保守判全国细分 · 待人工复核",
                needs_review=True,
            )
    return out
