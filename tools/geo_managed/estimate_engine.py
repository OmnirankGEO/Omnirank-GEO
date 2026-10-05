"""
v3.3/v3.4 估算引擎 — 90% 复用 c_end_cost_estimate

输入：keyword + city + industry + 模式（by_target_sov / by_budget）
输出：4 档预设 + 自定义反推 + 平台推荐 + 上榜时间预估 + 报价 3 天有效

核心对接（不重发明）:
  tools.c_end_cost_estimate.estimate_user_cost  → 复用现有篇数+成本算法
  services.placement_service.recommend_for_publish → 复用平台推荐
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("GEO-Managed-Estimate")


# 4 档 SOV → 前端话术（v2.1 锁定）
SOV_TIERS = {
    "entry":    {"sov": 15, "label": "偶尔被推荐"},
    "standard": {"sov": 25, "label": "经常被推荐", "recommended": True},
    "flagship": {"sov": 33, "label": "优先推荐"},
    "strong":   {"sov": 50, "label": "频繁被推荐"},
}

# 上榜时间预估（按竞争度映射）
UPRANK_DAYS = {
    1: "1-3 天", 2: "1-3 天", 3: "3-7 天",
    4: "7-14 天", 5: "7-14 天",
}

# v3.4 markup（对外不拆显，内嵌售价）
V34_MARKUP_FACTOR = 1.2


# 2026-04-20 commit 33 P1-C: 兜底默认平台组合(industry 为空或调研匹配失败时用)
# 避免前端 platform_mix_recommended 空数组无法渲染。
# 3 个通用 AI 搜索高来源曝光平台(与 PLATFORM_ENGINE_COVERAGE 顶部一致)
_DEFAULT_PLATFORM_MIX_FALLBACK = [
    {
        "platform": "知乎",
        "media_name": "知乎",
        "engines": ["豆包", "Kimi", "DeepSeek"],
        "geo_score": 30.0,
        "inclusion_rate": "高",
        "reason": "AI 搜索历史来源曝光较高 · 通用推荐",
    },
    {
        "platform": "百家号",
        "media_name": "百家号",
        "engines": ["豆包", "Kimi"],
        "geo_score": 25.0,
        "inclusion_rate": "中高",
        "reason": "百度系权重高 · 通用推荐",
    },
    {
        "platform": "搜狐网",
        "media_name": "搜狐",
        "engines": ["豆包", "Kimi", "DeepSeek"],
        "geo_score": 22.0,
        "inclusion_rate": "中",
        "reason": "新闻资讯权威 · 通用推荐",
    },
]


async def _fetch_platforms(industry: str, keyword: str, limit: int = 10) -> list:
    """预查平台推荐（sync → async 包装），供 estimate_word_plan 复用

    2026-04-20 commit 33 P1-C 修: industry 为空或 recommend_for_publish 返空时兜底 3 平台,
    避免前端 platform_mix_recommended 空数组无法渲染 GEO 方案卡.
    """
    try:
        import asyncio
        from services.placement_service import recommend_for_publish
        result = await asyncio.to_thread(recommend_for_publish, industry, keyword, "", limit)
        if result:
            return result
        logger.warning(
            f"[platform_mix fallback] recommend_for_publish 返空 "
            f"(industry='{industry}', keyword='{keyword}'), 走默认 3 平台组合"
        )
        return list(_DEFAULT_PLATFORM_MIX_FALLBACK)
    except Exception as e:
        logger.warning(f"recommend_for_publish 失败: {e}, 走默认 3 平台组合")
        return list(_DEFAULT_PLATFORM_MIX_FALLBACK)


def _sov_to_target_rank(sov_pct: int) -> str:
    """SOV → c_end_cost_estimate 的 target_rank 字段映射（deprecated 2026-04-17）

    保留用于向后兼容。新代码应直接传 target_share=sov_pct/100 到 estimate_user_cost，
    让 25% 和 33% 不再被压到同一个 top_10 档（破坏四档差异化）。
    """
    if sov_pct >= 50:
        return "top_3"
    elif sov_pct >= 25:
        return "top_10"
    else:
        return "top_30"


async def estimate_word_plan(
    keyword: str,
    city: Optional[str] = None,
    industry: Optional[str] = None,
    mode: str = "by_target_sov",
    target_sov_pct: Optional[int] = None,
    budget_yuan: Optional[float] = None,
    max_per_article_yuan: float = 200,
    check_frequency_per_day: int = 1,
    brand_id: Optional[int] = None,  # v3.4 才用
) -> dict:
    """
    单词托管估算 — 返回完整方案卡片数据

    Args:
        keyword: 关键词
        city / industry: 上下文
        mode: 'by_target_sov' (用户给目标) / 'by_budget' (用户给金额，反算)
        target_sov_pct: 用户给的目标 SOV（mode=by_target_sov 时）
        budget_yuan: 用户给的金额（mode=by_budget 时）
        max_per_article_yuan: 单篇硬性上限
        check_frequency_per_day: 监测频次（默认 1）
        brand_id: v3.4 全品牌套餐时传，会读 brand_strategies

    Returns:
        plan_card 数据，含：
          - 4 档预设 (entry / standard / flagship / strong)
          - 自定义反推 (mode=by_budget 时)
          - 平台推荐组合（动态）
          - 上榜时间预估
          - 报价 3 天有效期
    """
    import asyncio
    from tools.c_end_cost_estimate import estimate_user_cost, estimate_market_saturation

    # 1. 预查市场 + 平台（各查一次，4 档复用，避免 5 次重复 LLM/DB 调用）
    market, platform_mix = await asyncio.gather(
        estimate_market_saturation(keyword, industry or "", city or ""),
        _fetch_platforms(industry or "", keyword),
    )

    # 2. 4 档预设（传入 market + platforms，纯计算无外部调用）
    # 2026-04-17: 直接传 target_share=sov/100 保证 15%/25%/33%/50% 四档真实分化
    #             避开了旧的 top_3/top_10/top_30 三档 map 把 25%/33% 压平的问题
    tier_options: dict[str, dict] = {}
    for key, tier in SOV_TIERS.items():
        sov = tier["sov"]
        target_share = sov / 100.0
        try:
            result = await estimate_user_cost(
                keyword=keyword,
                city=city,
                industry=industry,
                target_share=target_share,
                market=market,
                platforms_raw=platform_mix,
            )
            cost_min, cost_max = result["cost_breakdown"]["total_yuan_range"]
            tier_options[key] = {
                "sov_pct": tier["sov"],
                "label": tier["label"],
                "articles": result["strategy"]["articles_needed"],
                "cost_yuan": round(cost_min),
                "cost_yuan_range": [round(cost_min), round(cost_max)],
                "detection_rate_pct": result["expected_effect"]["detection_rate_30d_pct"],
            }
            if tier.get("recommended"):
                tier_options[key]["recommended"] = True
        except Exception as e:
            logger.warning(f"估算 {key} 档失败: {e}")
            tier_options[key] = {
                "sov_pct": tier["sov"],
                "label": tier["label"],
                "articles": 0,
                "cost_yuan": 0,
                "error": str(e),
            }

    # 3. 用户给金额时反算（独立模块）
    custom = None
    if mode == "by_budget" and budget_yuan and budget_yuan > 0:
        from .reverse_calc import reverse_calc_from_budget
        custom = await reverse_calc_from_budget(
            budget_yuan=float(budget_yuan),
            keyword=keyword,
            market=market,
            city=city,
            industry=industry,
        )

    # 5. 报价有效期（v2.1 锁定 3 天）
    quoted_at = datetime.now()
    valid_until = quoted_at + timedelta(days=3)

    return {
        "plan_card": True,
        "keyword": keyword,
        "city": city or "",
        "industry": industry or "",
        "competition_level": market.get("competition_level", 3),
        "competition_count": market.get("competition_count", 50),  # effective_competition (A + B×0.5 + C×0.3)
        "competition_confidence": market.get("confidence", 0.7),
        # 2026-04-17: 暴露真实数据源明细给前端，让用户信任"几百家搜索结果但真实有效竞品 X"
        "competition_raw_count": market.get("raw_count", 0),        # metaso 搜回原始总条数
        "competition_breakdown": market.get("category_counts", {}),  # {A, B, C, D}
        "competition_source": market.get("source", "unknown"),       # metaso_llm_classify / llm_flash_estimate / fallback
        "estimated_uprank_days": UPRANK_DAYS.get(market.get("competition_level", 3), "3-7 天"),
        "tier_options": tier_options,
        "custom_input": custom,
        "platform_mix_recommended": platform_mix,
        "default_mode": "semi_auto",
        "mode_switch_label": "⚡ 切换全托管模式（AI 自主发布无需审核）",
        "estimate_quoted_at": quoted_at.isoformat(),
        "estimate_valid_until": valid_until.isoformat(),
        "estimate_validity_note": "此报价 3 天有效，过期需重新评估",
        "max_per_article_yuan": max_per_article_yuan,
        "check_frequency_per_day": check_frequency_per_day,
        "note": "价格根据该词实际竞争度算出。想调整告诉 AI 即可。",
        "requires_user_confirm": True,
    }


async def estimate_brand_plan(
    keywords: list[str],
    brand_id: int,
    user_id: int,
    city: Optional[str] = None,
    industry: Optional[str] = None,
    uniform_target_sov_pct: int = 25,
    per_keyword_sov: Optional[dict[str, int]] = None,
    total_budget_yuan: Optional[float] = None,
    max_per_article_yuan: float = 200,
    check_frequency_per_day: int = 1,
) -> dict:
    """
    v3.4 全品牌托管估算 — 多 keyword 合并 + markup 1.2x 内嵌

    Args:
        keywords: 关键词列表
        uniform_target_sov_pct: 统一目标 SOV
        per_keyword_sov: 每词独立 SOV {"keyword1": 25, "keyword2": 33}
        total_budget_yuan: 用户给总金额，反算每词

    Returns:
        含 subcampaigns + raw_total + final_total（含 markup）
    """
    if not keywords:
        return {"error": "请提供至少 1 个关键词"}

    subcampaigns = []
    raw_total = 0.0

    for kw in keywords:
        sov = (per_keyword_sov or {}).get(kw, uniform_target_sov_pct)
        sub_plan = await estimate_word_plan(
            keyword=kw,
            city=city,
            industry=industry,
            mode="by_target_sov",
            target_sov_pct=sov,
            max_per_article_yuan=max_per_article_yuan,
            check_frequency_per_day=check_frequency_per_day,
            brand_id=brand_id,
        )
        # 取该 SOV 对应的档位（25→standard, 33→flagship 等）
        tier_key = "standard"
        for k, t in SOV_TIERS.items():
            if t["sov"] == sov:
                tier_key = k
                break
        tier_data = sub_plan["tier_options"].get(tier_key) or sub_plan["tier_options"]["standard"]
        sub_cost = tier_data["cost_yuan"]
        subcampaigns.append({
            "keyword": kw,
            "sov_pct": sov,
            "sov_label": tier_data["label"],
            "articles": tier_data["articles"],
            "cost_yuan": sub_cost,
            "competition_level": sub_plan["competition_level"],
            "estimated_uprank_days": sub_plan["estimated_uprank_days"],
        })
        raw_total += sub_cost

    # markup 内嵌（前端不拆显）
    final_total = round(raw_total * V34_MARKUP_FACTOR)

    quoted_at = datetime.now()
    valid_until = quoted_at + timedelta(days=3)

    return {
        "brand_plan_card": True,
        "brand_id": brand_id,
        "user_id": user_id,
        "subcampaigns": subcampaigns,
        "total_articles": sum(s["articles"] for s in subcampaigns),
        "raw_total_yuan": round(raw_total),
        "markup_factor": V34_MARKUP_FACTOR,
        "final_total_yuan": final_total,        # 报给用户的最终售价
        "estimate_quoted_at": quoted_at.isoformat(),
        "estimate_valid_until": valid_until.isoformat(),
        "estimate_validity_note": "此报价 3 天有效，过期需重新评估",
        "default_mode": "semi_auto",
        "include_review_engine": True,           # v3.4 含每周复盘
        "note": "全品牌套餐含 5 引擎联动 + 每周复盘调优 + 品牌策略档案",
        "requires_user_confirm": True,
    }


async def re_estimate_for_resume(campaign: dict) -> dict:
    """
    用户从暂停恢复时调用 — 强制重新评估市场（如距上次估算 > 30 天 或 报价过期）

    返回新 estimate（用户必须再次确认才生效）
    """
    return await estimate_word_plan(
        keyword=campaign["keyword"],
        city=campaign.get("city"),
        industry=campaign.get("industry"),
        mode="by_target_sov",
        target_sov_pct=campaign.get("target_sov_pct", 25),
        max_per_article_yuan=float(campaign.get("max_per_article_yuan", 200)),
        check_frequency_per_day=int(campaign.get("check_frequency_per_day", 1)),
        brand_id=campaign.get("brand_id"),
    )
