"""
v3.4 复盘引擎 — 每周一凌晨跑

核心算法（详见 v2 第四章）：
  1. 收集过去 7 天该品牌的所有文章数据
  2. 计算每篇 effectiveness_score
  3. 找高效/低效案例（top 3 / bottom 3）
  4. LLM 提炼品牌专属策略 → 写入 brand_strategies
  5. 脱敏后反哺 industry_knowledge L2（如开启 share_consent）

数据来源（v3.4 必做埋点 8 天 Sprint 已建表）：
  - article_publish_snapshots  发文基线
  - daily_ranking_snapshots    每日排名快照
  - article_daily_metrics      每日效果聚合
  - article_generations.effectiveness_score 字段
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("GEO-Managed-Review")


async def weekly_learning(brand_id: int) -> Optional[dict]:
    """
    单个品牌的周复盘

    Returns:
        提炼出的策略 dict，或 None（如数据不足）
    """
    from db.managed_campaign_db import upsert_brand_strategy
    from db.connection import get_connection

    # 1. 取过去 7 天文章 + 效果数据
    articles = _get_articles_in_week(brand_id)
    if len(articles) < 2:
        logger.info(f"[Review] 品牌 {brand_id} 数据不足（{len(articles)} 篇），跳过本周复盘")
        return None

    # 2. 计算/更新 effectiveness_score
    for art in articles:
        score = _calc_effectiveness_score(art)
        art["effectiveness_score"] = score
        _save_effectiveness_score(art["id"], score)

    # 3. 高效/低效案例
    sorted_articles = sorted(articles, key=lambda a: a.get("effectiveness_score", 0), reverse=True)
    top_articles = sorted_articles[:3]
    bottom_articles = sorted_articles[-3:] if len(sorted_articles) >= 6 else []

    # 4. LLM 提炼策略
    try:
        strategy_data = await _llm_distill_strategy(brand_id, top_articles, bottom_articles)
    except Exception as e:
        logger.exception(f"[Review] LLM 提炼失败 {brand_id}: {e}")
        # 用启发式 fallback
        strategy_data = _heuristic_strategy_fallback(top_articles, bottom_articles)

    # 5. 写入 brand_strategies
    saved = upsert_brand_strategy(
        brand_id=brand_id,
        top_performing_styles=strategy_data.get("top_styles"),
        top_performing_platforms=strategy_data.get("top_platforms"),
        effective_keywords=strategy_data.get("effective_keywords"),
        losing_patterns=strategy_data.get("losing_patterns"),
        preferred_content_type=strategy_data.get("preferred_content_type"),
        preferred_platform_mix=strategy_data.get("preferred_platform_mix"),
        article_tone=strategy_data.get("article_tone"),
        shareable_patterns=strategy_data.get("shareable_patterns"),
        share_consent=True,  # v2 默认开启
    )

    # 6. 反哺公共库
    try:
        from .knowledge_reflow import reflow_to_industry_knowledge
        await reflow_to_industry_knowledge(brand_id, saved)
    except Exception as e:
        logger.warning(f"[Review] 反哺 L2 失败 {brand_id}: {e}")

    logger.info(f"[Review] 品牌 {brand_id} 复盘完成，本周 {len(articles)} 篇")
    return saved


# ============================================================
# 数据读取
# ============================================================

def _get_articles_in_week(brand_id: int) -> list[dict]:
    """读过去 7 天的所有文章 + 效果数据

    优先用 v3.4 埋点表（article_publish_snapshots / article_daily_metrics）
    fallback：从 article_generations 取
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 优先 join 埋点表
        try:
            cursor.execute("""
                SELECT
                  ag.id, ag.title, ag.created_at,
                  ag.effectiveness_score,
                  ag.detection_rate_delta,
                  ag.days_stayed_top10,
                  aps.platform AS publish_platform,
                  aps.baseline_detection_rate,
                  aps.baseline_rank,
                  COALESCE(SUM(adm.read_count), 0) AS total_read_count,
                  COALESCE(AVG(adm.read_rate_vs_baseline), 0) AS avg_read_rate_vs_baseline
                FROM article_generations ag
                LEFT JOIN article_publish_snapshots aps ON aps.article_id = ag.id
                LEFT JOIN article_daily_metrics adm ON adm.article_id = ag.id
                WHERE ag.brand_id = %s
                  AND ag.created_at >= NOW() - INTERVAL '7 days'
                GROUP BY ag.id, aps.platform, aps.baseline_detection_rate, aps.baseline_rank
                ORDER BY ag.created_at DESC
            """, (brand_id,))
            rows = cursor.fetchall() or []
            return [dict(r) for r in rows]
        except Exception as e:
            # 表不存在或字段缺失 → fallback
            logger.debug(f"_get_articles_in_week 用 fallback (埋点未到位): {e}")
            cursor.execute("""
                SELECT id, title, created_at, COALESCE(effectiveness_score, 0) AS effectiveness_score
                FROM article_generations
                WHERE brand_id = %s
                  AND created_at >= NOW() - INTERVAL '7 days'
                ORDER BY created_at DESC
            """, (brand_id,))
            rows = cursor.fetchall() or []
            return [dict(r) for r in rows]
    finally:
        conn.close()


def _save_effectiveness_score(article_id: int, score: float) -> None:
    """更新 article_generations.effectiveness_score"""
    from db.connection import get_db
    if not article_id:
        return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE article_generations SET effectiveness_score = %s WHERE id = %s",
                (round(score, 2), article_id),
            )
    except Exception as e:
        logger.warning(f"_save_effectiveness_score 失败 article_id={article_id}: {e}")


# ============================================================
# 评分算法
# ============================================================

def _calc_effectiveness_score(article: dict) -> float:
    """
    effectiveness_score 计算（v2 公式）

    score = detection_rate_delta * 0.4 +
            rank_improvement * 0.3 +
            read_count_normalized * 0.2 +
            days_stayed_top10 * 0.1

    所有维度归一化到 0-100
    """
    detection_delta = float(article.get("detection_rate_delta", 0) or 0)
    detection_score = max(0, min(100, detection_delta * 5))  # 1% delta = 5 分

    # rank_improvement: 假设 rank 越小越好，improvement = baseline - current
    baseline_rank = article.get("baseline_rank")
    current_rank = article.get("current_rank")
    if baseline_rank and current_rank:
        rank_improvement = max(0, int(baseline_rank) - int(current_rank))
        rank_score = min(100, rank_improvement * 10)  # 升 1 名 = 10 分
    else:
        rank_score = 0

    # 阅读量归一化（按 1000 阅读 = 50 分线性）
    read_count = int(article.get("total_read_count", 0) or 0)
    read_score = min(100, read_count / 20)  # 2000 阅读 = 100 分

    days_top10 = int(article.get("days_stayed_top10", 0) or 0)
    days_score = min(100, days_top10 * 14)  # 7 天 top10 = 100 分

    score = (
        detection_score * 0.4 +
        rank_score * 0.3 +
        read_score * 0.2 +
        days_score * 0.1
    )
    return round(score, 2)


# ============================================================
# LLM 提炼策略
# ============================================================

async def _llm_distill_strategy(
    brand_id: int,
    top_articles: list[dict],
    bottom_articles: list[dict],
) -> dict:
    """LLM 提炼品牌专属策略"""
    from services.llm.advisor_llm import advisor_generate

    top_summary = _summarize_articles(top_articles, "高效")
    bottom_summary = _summarize_articles(bottom_articles, "低效") if bottom_articles else "（数据不足）"

    prompt = f"""你是 GEO 优化策略分析师。基于该品牌过去一周的文章效果数据，提炼出**品牌专属优化策略**。

【高效文章（前 3 篇）】
{top_summary}

【低效文章（后 3 篇）】
{bottom_summary}

请提炼并返回严格 JSON（不要 markdown 代码块）：
{{
  "top_styles": {{"故事型": 60, "数据型": 30, "对比型": 10}},
  "top_platforms": {{"知乎": 0.4, "今日头条": 0.3, "搜狐": 0.2, "公众号": 0.1}},
  "effective_keywords": ["关键词1", "关键词2"],
  "losing_patterns": ["纯营销文", "无数据支撑"],
  "preferred_content_type": "case_study",
  "preferred_platform_mix": {{"知乎": 0.4, "今日头条": 0.3, "搜狐": 0.2, "公众号": 0.1}},
  "article_tone": "data_driven",
  "shareable_patterns": {{
    "industry_insight": "本周该行业数据型文章在知乎效果最好",
    "platform_preference": "今日头条对该行业品牌历史来源曝光高于平均"
  }}
}}

要求：
- shareable_patterns 必须脱敏，不含品牌名/价格/内部数据
- 所有数据基于本周真实表现
- 如果数据不足，返回保守默认值
"""

    try:
        # ⚠️ 2026-04-17 (P0-J): 原调用 advisor_generate(..., model="qwen3-max", max_tokens=1000)
        # 但签名是 (prompt, advisor_id=None, temperature=None)，多余 kwarg 会 TypeError
        # advisor_generate 内部由 advisor 自己选模型（默认顾问 = qwen3-max），无需显式指定
        result = await advisor_generate(prompt, temperature=0.3)
        text = result.strip() if isinstance(result, str) else result.get("text", "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"_llm_distill_strategy 失败: {e}")
        raise


def _summarize_articles(articles: list[dict], label: str) -> str:
    """文章摘要（给 LLM 看）"""
    if not articles:
        return f"（{label}文章无数据）"
    lines = []
    for i, a in enumerate(articles, 1):
        platform = a.get("publish_platform", "未知")
        score = a.get("effectiveness_score", 0)
        title = a.get("title", "")[:50]
        lines.append(f"{i}. [{platform}] {title} (effectiveness={score})")
    return "\n".join(lines)


def _heuristic_strategy_fallback(
    top_articles: list[dict],
    bottom_articles: list[dict],
) -> dict:
    """LLM 失败时的启发式 fallback"""
    # 统计 top 文章的平台分布
    platform_count: dict[str, int] = {}
    for a in top_articles:
        p = a.get("publish_platform")
        if p:
            platform_count[p] = platform_count.get(p, 0) + 1
    total = sum(platform_count.values()) or 1
    platform_mix = {p: round(c / total, 2) for p, c in platform_count.items()}

    return {
        "top_styles": {},
        "top_platforms": platform_mix,
        "effective_keywords": [],
        "losing_patterns": [],
        "preferred_content_type": None,
        "preferred_platform_mix": platform_mix,
        "article_tone": None,
        "shareable_patterns": {
            "fallback_note": "本周高效文章分布：" + json.dumps(platform_count, ensure_ascii=False),
        },
    }
