"""LLM-first 报价主入口 · 调 llm_pricing_scorer + cache namespace 隔离

老板 2026-05-12 LLM-first 改造方案 §3.4 + §4.7:
- 调 llm_score_and_price 拿三档价 + intent + value + reason
- 落 cache 时 pricing_engine_version='llm_v1'(独立 namespace)
- 失败 raise · caller(batch_pricing.generate_batch_quote)走 fallback 公式
- fallback 时 carry should_quote_false_keywords · 老公式 skip 它们防信息型词回流

flag 控制(在 batch_pricing.py 内查):
- LLM_FIRST_PRICING_ENABLED = false → 不进本模块
- LLM_FIRST_PRICING_AGENT_WHITELIST · 单代理灰度
- LLM_FIRST_PRICING_FALLBACK_TO_FORMULA · 失败是否降级

返回结构:跟老 generate_batch_quote 一致 · 前端代码无需改
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from services.price_lock_promise import (
    LOCK_MAP_KEY as _LOCK_MAP_KEY,
    record_locked as _record_locked,
)

logger = logging.getLogger("GEO-LLM-BatchQuote")

PRICING_ENGINE_VERSION = "llm_v1"

# [任务2 2026-06-08 LLM-first 成本地板防亏] 篇数口径(SOV 阶梯)· 与 tier_summaries 系数一致 · 成本地板用
#   与公式引擎(compute_banded_price)同口径:每词每档 selling ≥ ceil(篇数 × 单篇成本 × markup)。
_LLM_TIER_TARGET_SHARE = {"入门版": 0.10, "标准版": 0.20, "旗舰版": 0.30}
_LLM_TIER_ARTICLE_COEF = {"入门版": 4, "标准版": 7, "旗舰版": 10}


def _llm_per_word_articles(tier_name: str, effective_competition=None) -> int:
    """单词该档篇数(成本地板用)· 复用公式引擎篇数算法 compute_required_articles_banded(口径一致)。

    LLM-first 多无真实竞争信号(effective_competition=None)→ compute_required_articles_banded 打底
    MIN_ARTICLES_DEFAULT(=5·v1.3 所有词 ≥5 篇铁律);再取 max(LLM 阶梯系数 4/7/10) 保留三档差异(标准>入门)。
    LLM 未来返 competitor_count → 篇数随真实竞争上调(自动对齐公式引擎)。算法不可用时兜底 5。
    """
    ts = _LLM_TIER_TARGET_SHARE.get(tier_name, 0.20)
    coef = _LLM_TIER_ARTICLE_COEF.get(tier_name, 7)
    try:
        from tools.pricing_bands import compute_required_articles_banded
        banded = compute_required_articles_banded(effective_competition, ts, data_source="llm_estimate")
    except Exception:
        banded = 5
    return max(int(coef), int(banded))


# [任务3 2026-06-08] LLM-first 需单独报价(对齐公式引擎 super_red_ocean)·
#   should_quote=false 分两类:① 高价值/超范畴(商业信号 或 LLM 判值高 或强商业意图)→ 需单独报价
#   ② 真信息型(科普/流程/是什么)→ 信息型不报价。前者标 super_red_ocean=true(三档归0·不进套餐·前端「🔴 需单独报价」)。
_SINGLE_QUOTE_COMMERCIAL_SIGNALS = (
    "哪家好", "哪家强", "哪个好", "哪家", "推荐", "排名", "排行", "最好", "最强",
    "十大", "前十", "服务商", "公司", "厂家", "供应商", "品牌", "机构",
    "费用", "多少钱", "报价", "价格", "收费",
)
_SINGLE_QUOTE_VALUE_THRESHOLD = 3.5  # LLM 判商业价值高(≥3.5/5)但未出标准三档 → 超出标准范畴 → 需单独报价


def _classify_unquoted_keyword(keyword: str, value_score, intent: str) -> bool:
    """should_quote=false 词:判「需单独报价」(高价值/超范畴 → True)还是真信息型(→ False)。

    对齐公式引擎 super_red_ocean 语义(不出标准保证价 · 不进套餐 · 前端「🔴 需单独报价」)。
    判据(任一成立即需单独报价):① 含商业信号(哪家好/推荐/排名/服务商/报价 等)
      ② LLM 判商业价值高(value_score ≥ 3.5)③ intent 强商业(transactional/commercial)。
    真信息型(无以上信号 · 科普/流程类)→ False(信息型不报价)。
    """
    kw = keyword or ""
    if any(sig in kw for sig in _SINGLE_QUOTE_COMMERCIAL_SIGNALS):
        return True
    try:
        if float(value_score or 0) >= _SINGLE_QUOTE_VALUE_THRESHOLD:
            return True
    except (TypeError, ValueError):
        pass
    if intent in ("transactional", "commercial"):
        return True
    return False


async def _resolve_keyword_input(
    keywords: list[str],
    industry: str,  # 预留:Phase 2 加 industry context 注入
    city: str,  # 预留:Phase 2 加 city context 注入
    brand_name: str,  # 预留:Phase 2 加品牌名拦截分流
) -> list[dict[str, Any]]:
    """把字符串列表丰富成 LLM 需要的 context

    Bug A/C 兼容:品牌名 keyword 走老固定价路径 · 不进 LLM(防 LLM 对品牌名乱估)
    Phase 1 占位 · industry/city/brand_name 留给 Phase 2 加 5118 数据 + 品牌拦截
    """
    del industry, city, brand_name  # 显式标记预留 · Phase 2 加 industry/city/brand 拦截分流
    enriched = []
    for kw in keywords:
        enriched.append({"keyword": kw})
    return enriched


def _build_quote_data_from_llm(
    llm_keywords: list[dict[str, Any]],
    brand_name: str,
    industry: str,
    city: str,
    target_share: float,
    markup_override: float | None,
    cost_per_article: float | None = None,
) -> dict[str, Any]:
    """把 LLM 输出转换成跟老 generate_batch_quote 一致的 quote_data 结构

    前端代码无需改 · 只是数据来源换了

    [任务2 2026-06-08] 成本地板防亏:每词每档 selling ≥ ceil(篇数 × 单篇成本 × markup)。
      cost_per_article 支持服务商自设覆盖(模块2);None → 兜底系统默认单篇成本(¥60·与公式引擎 default 一致)。
    """
    if cost_per_article is None:
        try:
            from tools.transparent_pricing import get_cost_per_article
            cost_per_article = float(get_cost_per_article())
        except Exception:
            cost_per_article = 60.0
    cost_per_article = float(cost_per_article)
    # LLM-first 默认 markup=1.0(skip_markup / markup_override 已在 batch_pricing 回落公式引擎)·
    # 传入即 effective_markup · 成本地板与 LLM 出价同口径。
    floor_markup = float(markup_override) if markup_override else 1.0

    keyword_details = []
    sum_entry = sum_std = sum_flag = 0
    art_entry = art_std = art_flag = 0  # 累计篇数(单词每档 · total_articles 与成本地板同口径)
    quoted_count = 0

    for kw in llm_keywords:
        is_single_quote = False  # [任务3] 需单独报价(高价值/超范畴 · 对齐公式引擎 super_red_ocean)
        single_quote_reason = ""
        if not kw.get("should_quote", False):
            entry = standard = flagship = 0
            # [任务3] should_quote=false 分两类:高价值/超范畴 → 需单独报价(标 super_red_ocean·不裸¥0);真信息型 → 信息型不报价
            if _classify_unquoted_keyword(kw.get("keyword", ""), kw.get("value_score_0_5", 0.0), kw.get("intent", "")):
                is_single_quote = True
                single_quote_reason = "高价值 · 超出标准报价范畴 · 需单独报价"
        else:
            entry = int(kw.get("entry_price_yuan", 0))
            standard = int(kw.get("standard_price_yuan", 0))
            flagship = int(kw.get("flagship_price_yuan", 0))
            # [任务2 成本地板] 单词每档篇数(LLM 无竞争信号 → 打底 5/7/10)→ selling ≥ ceil(篇数 × 单篇成本 × markup)
            eff_comp = kw.get("competitor_count")  # LLM schema 多无竞争信号 → None → 打底篇数
            n_entry = _llm_per_word_articles("入门版", eff_comp)
            n_std = _llm_per_word_articles("标准版", eff_comp)
            n_flag = _llm_per_word_articles("旗舰版", eff_comp)
            entry = max(entry, math.ceil(n_entry * cost_per_article * floor_markup))
            standard = max(standard, math.ceil(n_std * cost_per_article * floor_markup))
            flagship = max(flagship, math.ceil(n_flag * cost_per_article * floor_markup))
            sum_entry += entry
            sum_std += standard
            sum_flag += flagship
            art_entry += n_entry
            art_std += n_std
            art_flag += n_flag
            quoted_count += 1

        keyword_details.append({
            "keyword": kw["keyword"],
            "intent": kw.get("intent", "informational"),
            "funnel_stage": kw.get("funnel", "awareness"),
            "value_score": kw.get("value_score_0_5", 0.0),
            "competitor_count": None,
            "search_volume": None,
            "should_quote": kw.get("should_quote", False),
            "needs_review": kw.get("needs_review", False),
            # [任务3] 需单独报价标(对齐公式引擎 super_red_ocean)· flat 路径 selection_api 透传 → 三档归0 + 不进套餐 + 前端「🔴 需单独报价」
            "super_red_ocean": is_single_quote,
            "super_red_ocean_level": "yellow" if is_single_quote else "none",
            "needs_single_quote": is_single_quote,
            "review_reason": single_quote_reason or kw.get("review_reason", ""),
            "business_line": kw.get("business_line", ""),
            "recommendation_reason": kw.get("reason_zh", ""),
            "entry_price": entry,
            "standard_price": standard,
            "flagship_price": flagship,
            "selling_price": standard,
            "pricing_engine_version": PRICING_ENGINE_VERSION,
        })

    tier_summaries = {
        "入门版": {"final_price": sum_entry, "total_articles": art_entry, "target_share": 0.10},
        "标准版": {"final_price": sum_std,   "total_articles": art_std, "target_share": 0.20},
        "旗舰版": {"final_price": sum_flag,  "total_articles": art_flag, "target_share": 0.30},
    }

    return {
        "brand_name": brand_name,
        "industry": industry,
        "city": city,
        "keyword_details": keyword_details,
        "keywords": keyword_details,
        "tier_summaries": tier_summaries,
        "tiers": tier_summaries,
        "target_share": target_share,
        "total_keywords": len(keyword_details),
        "quoted_keywords": quoted_count,
        "skipped_keywords": len(keyword_details) - quoted_count,
        "pricing_engine_version": PRICING_ENGINE_VERSION,
        "final_price": sum_std,
        "markup_ratio": markup_override or 1.0,  # [§4.5/决策5] 默认回成本
    }


def _build_markdown(quote_data: dict[str, Any]) -> str:
    """LLM-first 报价单 markdown · 跟老格式接近 · 加 LLM 标记"""
    brand = quote_data.get("brand_name", "客户")
    kw_details = quote_data.get("keyword_details", [])
    tiers = quote_data.get("tier_summaries", {})

    md = f"# OmniRank GEO 报价单 · {brand}\n\n"
    md += f"**报价引擎**: LLM-first ({PRICING_ENGINE_VERSION}) · **行业**: {quote_data.get('industry','')} · **城市**: {quote_data.get('city','')}\n\n"
    md += f"**有效关键词**: {quote_data.get('quoted_keywords', 0)} / {quote_data.get('total_keywords', 0)}"
    skipped = quote_data.get("skipped_keywords", 0)
    if skipped:
        md += f"  · 信息型剔除 {skipped} 个"
    md += "\n\n"

    md += "## 三档套餐\n\n"
    md += "| 套餐 | 总价 | 月均文章 | 目标曝光 |\n|---|---|---|---|\n"
    for name in ("入门版", "标准版", "旗舰版"):
        t = tiers.get(name, {})
        md += f"| **{name}** | ¥{t.get('final_price', 0):,} | {t.get('total_articles', 0)} 篇 | {int(t.get('target_share', 0)*100)}% SOV |\n"

    md += "\n## 关键词明细\n\n"
    md += "| 关键词 | 意图 | 价值 | 入门 | 标准 | 旗舰 | 推荐理由 |\n|---|---|---|---|---|---|---|\n"
    for kw in kw_details:
        if not kw.get("should_quote", False):
            md += f"| ~~{kw['keyword']}~~ | 信息型剔除 | - | - | - | - | {kw.get('recommendation_reason', '')} |\n"
            continue
        md += (f"| {kw['keyword']} | {kw.get('intent','')}/{kw.get('funnel_stage','')} | "
               f"{kw.get('value_score', 0):.2f} | "
               f"¥{kw.get('entry_price', 0):,} | ¥{kw.get('standard_price', 0):,} | "
               f"¥{kw.get('flagship_price', 0):,} | {kw.get('recommendation_reason','')} |\n")

    return md


async def save_llm_prices_to_cache(
    brand_name: str,
    industry: str,
    city: str,
    llm_keywords: list[dict[str, Any]],
    business_scope: str | None = None,
) -> tuple[int, Any]:
    """把 LLM 价格落到独立表 keyword_price_cache_llm

    [CTO-15.23 2026-05-12 Codex round-3 P0 修] 独立表方案
      不再走老 save_keyword_prices_cache · 老 keyword_price_cache 表完全不动
      代码 rollback 时老镜像不依赖新表 · 部署回滚安全(老板拍板)

    [Codex round-3 P1] business_scope 参与 cache key
      防同 brand 不同 business_scope 串台 LLM business_line / reason_zh

    [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE]
      Returns: (写入条数, expires_at) —— expires_at 是 DB 层真正落库的到期时刻,
      供报价层显示**真实**锁期。写入失败 → (0, None) → 不承诺锁期。

    Returns: (写入条数, expires_at | None)
    """
    if not llm_keywords:
        return 0, None
    try:
        from db.diagnosis_db import save_llm_keyword_prices_cache
    except ImportError:
        logger.warning("save_llm_keyword_prices_cache 不可用 · 跳过缓存写入")
        return 0, None

    try:
        expires_at = save_llm_keyword_prices_cache(
            brand_name=brand_name,
            llm_keywords=llm_keywords,
            industry=industry,
            city=city,
            business_scope=business_scope,
        )
        return len(llm_keywords), expires_at
    except Exception as exc:
        logger.warning("save_llm_keyword_prices_cache 失败: %s", exc)
        return 0, None


def _cache_row_to_llm_keyword(cache_row: dict[str, Any], keyword: str) -> dict[str, Any]:
    """把独立表 keyword_price_cache_llm 的 row 转成 LLMPricedKeyword 等价 dict

    [Codex round-3] 现读独立表 · 字段名跟新表 schema 对齐
    """
    should_quote = cache_row.get("should_quote", True)
    return {
        "keyword": keyword,
        "intent": cache_row.get("intent", "informational"),
        "funnel": cache_row.get("funnel_stage", "awareness"),
        "value_score_0_5": float(cache_row.get("value_score") or 0.0),
        "entry_price_yuan": int(cache_row.get("entry_price") if cache_row.get("entry_price") is not None else -1) if should_quote else -1,
        "standard_price_yuan": int(cache_row.get("standard_price") if cache_row.get("standard_price") is not None else -1) if should_quote else -1,
        "flagship_price_yuan": int(cache_row.get("flagship_price") if cache_row.get("flagship_price") is not None else -1) if should_quote else -1,
        "should_quote": bool(should_quote),
        "reason_zh": cache_row.get("llm_reason_zh") or "(7天价格锁 · 缓存返回)",
        "business_line": cache_row.get("business_line") or "",
        "needs_review": bool(cache_row.get("needs_review") or False),
        "review_reason": cache_row.get("review_reason") or "",
        "_from_cache": True,
        # [价格锁承诺 2026-08-05] 命中 LLM cache = 这词真锁着,锁到这行的 expires_at
        "_cache_expires_at": cache_row.get("expires_at"),
    }


def _read_llm_cache(
    brand_name: str,
    keywords: list[str],
    industry: str,
    city: str,
    business_scope: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Read-through cache · 查 LLM-first 独立表 keyword_price_cache_llm

    [CTO-15.23 2026-05-12 Codex round-3]
      P0 修:走独立 get_llm_cached_keyword_prices · 老 keyword_price_cache 0 触碰
      P1 修:brand-specific(不跨品牌全局)+ business_scope_hash 隔离

    Args:
        industry / city: 当前不参与 cache key(Phase 2 仅 brand+kw+scope · 后续可扩)
                         留参数防接口未来扩展时 batch_pricing_llm 调用方需大改

    Returns: {keyword: LLMPricedKeyword dict} · 仅未过期 + 同 brand + 同 scope_hash
    """
    del industry, city  # 显式标记预留 · Phase 2 brand-specific 暂不参与 cache key
    if not keywords:
        return {}
    try:
        from db.diagnosis_db import get_llm_cached_keyword_prices
    except ImportError:
        logger.warning("get_llm_cached_keyword_prices 不可用 · 跳过 cache 读")
        return {}

    try:
        cached = get_llm_cached_keyword_prices(
            brand_name=brand_name,
            keywords=keywords,
            business_scope=business_scope,
        )
    except Exception as exc:
        logger.warning("get_llm_cached_keyword_prices 异常 · 退化为全打 LLM: %s", exc)
        return {}

    result: dict[str, dict[str, Any]] = {}
    for kw, row in cached.items():
        try:
            result[kw] = _cache_row_to_llm_keyword(row, kw)
        except Exception as exc:
            logger.warning("cache row 转 LLM 格式失败 · 忽略 keyword=%s · %s", kw, exc)
    return result


async def llm_first_batch_quote(
    keywords: list[str],
    brand_name: str = "客户",
    target_share: float = 0.20,
    industry: str = "",
    city: str = "",
    skip_markup: bool = False,
    markup_override: float | None = None,
    business_scope: str = "",
    cost_per_article_override: float | None = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """LLM-first 报价主路径

    [CTO-15.23 2026-05-12 Codex round-2 P0 修] Read-through cache 闭环
      1. 入口先读 llm_v1 cache · 命中跳过 LLM
      2. 未命中 keywords 才调 llm_score_and_price 打 LLM
      3. 合并 cache 命中 + LLM 新算 · 按原 keyword 顺序输出
      4. 仅新算的写入 cache(命中的不重写防 cached_at 漂移 + 减少 IO)

    Returns:
        (quote_data, markdown, should_quote_false_keywords)
        should_quote_false_keywords 用于 fallback 时 carry 防信息型词回流

    Raises:
        RuntimeError: LLM 失败 · caller 应走 fallback
    """
    from tools.llm_pricing_scorer import llm_score_and_price

    t0 = time.time()
    effective_markup = 1.0 if skip_markup else (markup_override if markup_override is not None else 1.0)  # [§4.5/决策5] 默认回成本

    # ---- Step 1: Read-through cache(brand-specific + business_scope_hash 隔离) ----
    cache_hits = _read_llm_cache(brand_name, keywords, industry, city, business_scope=business_scope)
    keywords_to_llm = [kw for kw in keywords if kw not in cache_hits]
    logger.info("llm_first_batch_quote · cache hit=%d / miss=%d / total=%d",
                len(cache_hits), len(keywords_to_llm), len(keywords))

    # ---- Step 2: 未命中才打 LLM ----
    llm_latency = 0
    n_samples = 0
    fresh_llm_kws: list[dict[str, Any]] = []
    if keywords_to_llm:
        enriched = await _resolve_keyword_input(keywords_to_llm, industry, city, brand_name)
        result = await llm_score_and_price(
            keywords=enriched,
            industry=industry,
            city=city,
            business_scope=business_scope,
            n_samples=2,
        )
        if not result.get("success"):
            raise RuntimeError(f"LLM-first 报价失败: {result.get('error')}")
        fresh_llm_kws = result["keywords"]
        llm_latency = result.get("latency_ms", 0) or 0
        n_samples = result.get("n_samples_actual", 0) or 0

        # 仅新算的写 cache(命中的不重写)· business_scope 参与 cache key
        try:
            n_cached, fresh_expires_at = await save_llm_prices_to_cache(
                brand_name, industry, city, fresh_llm_kws,
                business_scope=business_scope,
            )
        except Exception as exc:
            logger.warning("LLM cache 落库异常 · 不影响业务: %s", exc)
            n_cached, fresh_expires_at = 0, None
    else:
        n_cached = 0
        fresh_expires_at = None
        logger.info("llm_first_batch_quote · 全部命中 cache · 0 LLM call(7 天价格锁生效)")

    # ---- Step 3: 合并 cache 命中 + LLM 新算 · 按原 keyword 顺序输出 ----
    fresh_by_kw = {kw["keyword"]: kw for kw in fresh_llm_kws}
    merged: list[dict[str, Any]] = []
    for kw in keywords:
        if kw in cache_hits:
            merged.append(cache_hits[kw])
        elif kw in fresh_by_kw:
            merged.append(fresh_by_kw[kw])
        else:
            logger.warning("keyword 既不在 cache 也不在 LLM 输出 · skip: %s", kw)

    should_quote_false = [k["keyword"] for k in merged if not k.get("should_quote", False)]

    quote_data = _build_quote_data_from_llm(
        merged, brand_name, industry, city, target_share, effective_markup,
        cost_per_article=cost_per_article_override,
    )
    markdown = _build_markdown(quote_data)
    quote_data["skip_markup"] = skip_markup
    quote_data["effective_markup"] = effective_markup

    # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 逐词锁期(同公式路径口径)。
    #   LLM 路径写的是 brand-specific 的 keyword_price_cache_llm(不共享·无跨客户污染问题),
    #   命中的词锁到那行的 expires_at,本次新写的词锁到写入返回的 expires_at;
    #   写失败(fresh_expires_at=None)→ 一个都不登记 → 不承诺。
    _lock_map: dict = {}
    for _row in merged:
        if _row.get("_from_cache"):
            _record_locked(_lock_map, [_row.get("keyword")], _row.get("_cache_expires_at"))
    _record_locked(_lock_map, [k.get("keyword") for k in fresh_llm_kws], fresh_expires_at)
    quote_data[_LOCK_MAP_KEY] = _lock_map

    quote_data["_cache_hits"] = len(cache_hits)
    quote_data["_cache_misses"] = len(keywords_to_llm)
    quote_data["_cache_written"] = n_cached
    quote_data["_llm_latency_ms"] = llm_latency
    quote_data["_llm_n_samples"] = n_samples
    quote_data["_llm_model"] = "deepseek-v4-flash" if keywords_to_llm else "cache_only"
    quote_data["_total_latency_ms"] = int((time.time() - t0) * 1000)

    logger.info("llm_first_batch_quote OK · brand=%s · hit=%d miss=%d · skipped=%d · latency=%dms",
                brand_name, len(cache_hits), len(keywords_to_llm), len(should_quote_false),
                quote_data["_total_latency_ms"])

    return quote_data, markdown, should_quote_false
