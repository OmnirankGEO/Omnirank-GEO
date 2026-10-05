"""
v3.3 金额反推 SOV — 用户给金额，反算可达 SOV + 篇数

数学原理:
  正向：N = SOV × C / (1 - SOV)            （c_end_cost_estimate.calc_required_articles）
  反向：SOV = N / (N + C)
  即根据预算先算可买多少篇，再反推 SOV

复用:
  - tools.c_end_cost_estimate.estimate_detection_rate  现有检出率公式
  - services.placement_service.recommend_for_publish   平台均价
"""

import logging
from typing import Optional

logger = logging.getLogger("GEO-Managed-Reverse")


# 监测固定 30 天周期 + 默认 1 次/天
DEFAULT_MONITORING_DAYS = 30
DEFAULT_MONITORING_COST_PER_CHECK = 0.29


async def _estimate_avg_article_cost(
    keyword: str,
    industry: Optional[str] = None,
) -> float:
    """
    估算单篇平均成本（写作 ¥10 + 媒体加权平均）

    走 placement_service.recommend_for_publish 拿真实媒体价加权
    fallback：B 类（¥65）+ C 类（¥45）平均 ≈ ¥55，加写作 ¥10 = ¥65/篇
    """
    try:
        from services.placement_service import recommend_for_publish
        platforms = recommend_for_publish(
            industry=industry or "",
            keywords=keyword,
            limit=10,
        )
        if platforms:
            prices = [float(p.get("our_price_yuan", 0)) for p in platforms if p.get("our_price_yuan")]
            valid = [p for p in prices if p > 0]
            if valid:
                avg_media = sum(valid) / len(valid)
                return round(10 + avg_media, 2)  # 写作 ¥10 + 媒体均价
    except Exception as e:
        logger.warning(f"_estimate_avg_article_cost 走 fallback: {e}")

    # fallback：保守估计
    return 110.0  # ¥10 写 + ¥100 发


async def reverse_calc_from_budget(
    budget_yuan: float,
    keyword: str,
    market: dict,
    city: Optional[str] = None,
    industry: Optional[str] = None,
    check_frequency_per_day: int = 1,
) -> dict:
    """
    用户给金额 → 反算可达 SOV / 篇数 / 检出率

    Args:
        budget_yuan: 用户预算（必须 > 0）
        keyword: 关键词
        market: estimate_market_saturation 的输出 (含 competition_count / competition_level)
        city / industry: 用于 placement 推荐
        check_frequency_per_day: 监测频次（默认 1）

    Returns:
        {
            "achievable_sov_pct": int,        # 可达 SOV (上限 50%)
            "achievable_articles": int,        # 可写文章数
            "detection_rate_30d_pct": int,     # 30 天预估检出率
            "breakdown": {
              "monitoring_yuan": float,
              "write_publish_yuan": float,
            },
            "matched_tier_label": "标准上榜" | "入门试水" | ...
        }
    """
    if budget_yuan <= 0:
        return {
            "achievable_sov_pct": 0,
            "achievable_articles": 0,
            "detection_rate_30d_pct": 30,
            "breakdown": {"monitoring_yuan": 0, "write_publish_yuan": 0},
            "matched_tier_label": "余额不足",
        }

    competitor_count = max(1, int(market.get("competition_count", 50)))
    competition_level = int(market.get("competition_level", 3))

    # 1. 单篇平均成本（写作 + 平台加权均价）
    avg_article_cost = await _estimate_avg_article_cost(keyword, industry)

    # 2. 监测费（30 天 × 频次 × ¥0.29）
    monitoring_cost = check_frequency_per_day * DEFAULT_MONITORING_COST_PER_CHECK * DEFAULT_MONITORING_DAYS

    # 3. 可写篇数
    affordable_for_articles = max(0.0, budget_yuan - monitoring_cost)
    max_articles = int(affordable_for_articles / avg_article_cost) if avg_article_cost > 0 else 0
    max_articles = max(0, min(max_articles, 50))  # 不超过 50 篇

    # 4. 反算 SOV (篇数公式逆解)
    if max_articles == 0:
        achievable_sov = 0.0
    else:
        achievable_sov = max_articles / (max_articles + competitor_count)
    achievable_sov = min(0.50, achievable_sov)  # 上限 50%

    # 5. 估算检出率（复用 c_end_cost_estimate 现有公式）
    competition_factor = 1 - (competition_level - 1) * 0.08
    detection_rate = min(0.85, 0.30 + max_articles * 0.10 * competition_factor)
    detection_rate = max(0.30, detection_rate) if max_articles > 0 else 0.30

    # 6. 匹配档位标签（用户视角）
    sov_pct = round(achievable_sov * 100)
    if sov_pct >= 50:
        tier_label = "频繁被推荐"
    elif sov_pct >= 33:
        tier_label = "优先推荐"
    elif sov_pct >= 25:
        tier_label = "经常被推荐"
    elif sov_pct >= 15:
        tier_label = "偶尔被推荐"
    elif sov_pct > 0:
        tier_label = "略有曝光"
    else:
        tier_label = "余额不足"

    return {
        "achievable_sov_pct": sov_pct,
        "achievable_articles": max_articles,
        "detection_rate_30d_pct": round(detection_rate * 100),
        "breakdown": {
            "monitoring_yuan": round(monitoring_cost, 2),
            "write_publish_yuan": round(max_articles * avg_article_cost, 2),
        },
        "matched_tier_label": tier_label,
        "avg_article_cost_yuan": avg_article_cost,
    }
