"""C 端透明成本估算工具（v3.2 Phase 2）

目标:
  C 端用户问"深圳全屋定制多少钱" → 返回透明成本（不是代理报价）
  - 市场饱和度
  - 需要投放多少篇
  - 内容生成成本（积分）+ 媒体发布成本（元）
  - 预估效果（30 天 AI 检出率）

对比代理端:
  tools/batch_pricing.py: 有 markup 加价（给客户报价用）
  tools/c_end_cost_estimate.py: 纯成本（给 C 端用户决策用）

复用:
  services/placement_service.recommend_for_publish - 平台和价格
  LLM + 简化启发 - 估算 competition_count（不做完整 Metaso 调研，降本）
"""

import math
import logging
from typing import Optional, Any, Awaitable, Callable

logger = logging.getLogger("GEO-CEndCost")

# 常量
POINTS_PER_YUAN = 130
CONTENT_COST_POINTS = 500  # 每篇 GEO 文章的积分成本（示例值）


# ============================================================
# Phase 4 PLAN 02 · 异常类
# ============================================================
# 2026-04-20 CTO-15.5: 重构 one_click_geo_plan 删除超时降级路径
# worker 层靠这两个 Exception 区分 "数据质量不够" vs "外部 API 故障",
# 分别走 failed:low_quality / failed:external_api 分支处理扣费联动


class LowQualityResult(Exception):
    """扩词 < 10 或 clusters == 0 等 "结果不够用" 的语义型失败.

    不 retry (重试也是同样输入同样结果),直接 failed:low_quality.
    partial 用于把已经扩到的部分词塞进 result_json 给前端展示.
    """

    def __init__(self, reason: str, partial: Optional[dict] = None):
        self.reason = reason
        self.partial = partial or {}
        super().__init__(reason)


class ExternalAPIError(Exception):
    """外部 API (秘塔 / DashScope / 5118 / advisor_flash) 故障.

    worker 层外层 retry 2 次(间隔 5s/20s),2 次都挂才真 failed:external_api.
    """

    def __init__(self, source: str, detail: str = ""):
        self.source = source
        self.detail = detail
        super().__init__(f"{source}: {detail}" if detail else source)


def _count_to_level(effective_count: int) -> int:
    """把有效竞争数（A+B*0.5+C*0.3）映射到 1-5 竞争等级（和老接口兼容）"""
    if effective_count <= 5:
        return 1       # 极低
    elif effective_count <= 15:
        return 2       # 较低
    elif effective_count <= 35:
        return 3       # 中等
    elif effective_count <= 70:
        return 4       # 激烈
    else:
        return 5       # 极高


async def estimate_market_saturation(
    keyword: str,
    industry: str = "",
    city: str = "",
) -> dict:
    """估算关键词的市场饱和度 — 走代理端真实算法（秘塔 + LLM ABCD 分类）

    2026-04-17 重构：原 LLM flash 直接猜 competition_count 虚高严重
    （"几百家品牌竞争"但真实广告文章可能只有几十条），导致 C 端看到的
    4 档托管方案篇数和代理端 batch_pricing 不一致。

    真实算法（与代理端 tools/keyword_value_scorer.fetch_metaso_batch 同源）：
      1. 秘塔搜索 100 条真实搜索结果
      2. LLM 批量分类 A/B/C/D:
         A 推广/SEO 竞品 (推荐/测评/榜单/排名) ×1.0
         B 行业资讯 (新闻/市场分析) ×0.5
         C 百科/教程 (词条/科普) ×0.3
         D 噪声 (无关/低质) ×0.0
      3. effective_competition = A + B*0.5 + C*0.3 — 真正的竞争池

    降级链路：metaso/LLM 失败 → fallback LLM flash 粗略估 → fallback 保底值
    Redis 缓存 24h（单词热门查询命中率高，成本摊薄）。

    Returns:
        {
            "competition_level": 1-5,           # 等级（UI 兼容）
            "competition_count": effective,     # = A + B*0.5 + C*0.3（真实池）
            "raw_count": A+B+C+D,               # 原始 metaso 返回条数
            "category_counts": {A, B, C, D},    # 透明化给前端
            "top_players": [...] / top_platforms,
            "market_size_hint": "大/中/小",
            "confidence": 0.6-0.95,
            "source": "metaso_llm_classify" / "llm_flash_estimate" / "fallback"
        }
    """
    # Redis 缓存 24h（热门词命中率高）
    _cache_key = f"mkt_sat:{keyword}:{industry}:{city}"
    try:
        from cache.redis_client import redis_get_json
        cached = redis_get_json(_cache_key)
        if cached and isinstance(cached, dict) and "competition_count" in cached:
            return cached
    except Exception as e:
        logger.debug(f"市场饱和度缓存读取失败（不影响）: {e}")

    # 优先走代理端真实算法：metaso 搜索 + LLM ABCD 分类
    try:
        from tools.keyword_value_scorer import fetch_metaso_batch
        real_batch = await fetch_metaso_batch([keyword], concurrency=1)
        real = real_batch.get(keyword, {})
        if real and real.get("source") != "fallback" and real.get("effective_competition"):
            cats = real.get("category_counts", {})
            effective = int(real["effective_competition"])
            result = {
                "competition_level": _count_to_level(effective),
                "competition_count": effective,
                "raw_count": sum(cats.values()) or real.get("content_count", 0),
                "category_counts": cats,
                "top_players": real.get("top_platforms", []),
                "market_size_hint": (
                    "大" if effective > 50 else
                    "中" if effective > 15 else
                    "小"
                ),
                "confidence": 0.95,   # 真实搜索 + 分类，置信度最高
                "source": "metaso_llm_classify",
            }
            # 缓存 24h
            try:
                from cache.redis_client import redis_set_json
                redis_set_json(_cache_key, result, ex=86400)
            except Exception:
                pass
            return result
    except Exception as e:
        logger.warning(f"[mkt_sat] metaso+ABCD 真实算法失败，降级 LLM flash: {e}")

    # 降级：LLM flash 快速估（成本低，无噪声过滤，置信度中）
    from services.llm.advisor_llm import advisor_flash
    prompt = f"""基于你的常识估算以下关键词的市场饱和度（不要联网查）：

关键词：{keyword}
行业：{industry or "未指定"}
城市：{city or "全国"}

请返回严格 JSON（不要 markdown 代码块）：
{{
  "competition_level": 1-5 整数（1=蓝海, 5=红海）,
  "competition_count": 10-500 整数（估算该词领域内同行数量）,
  "top_players": ["品牌A", "品牌B", "品牌C"],
  "market_size_hint": "大" 或 "中" 或 "小",
  "confidence": 0.6-0.9 浮点数（自信度）
}}

"""
    try:
        result = await advisor_flash(prompt, history=[])
        text = result.strip() if isinstance(result, str) else result.get("text", "").strip()
        # 剥离 markdown 代码块
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        import json
        data = json.loads(text.strip())
        # 约束
        data["competition_level"] = max(1, min(5, int(data.get("competition_level", 3))))
        data["competition_count"] = max(10, min(500, int(data.get("competition_count", 50))))
        data.setdefault("top_players", [])
        data.setdefault("market_size_hint", "中")
        data.setdefault("confidence", 0.7)
        data["source"] = "llm_flash_estimate"   # 标记降级来源
        # 缓存 2h（比真实算法的 24h 短，避免降级数据长期污染）
        try:
            from cache.redis_client import redis_set_json
            redis_set_json(_cache_key, data, ex=7200)
        except Exception as e:
            logger.debug(f"市场饱和度缓存写入失败（不影响）: {e}")
        return data
    except Exception as e:
        logger.warning(f"market_saturation LLM 失败，用默认值: {e}")
        # fallback: 保守估算
        fallback = {
            "competition_level": 3,
            "competition_count": 50,
            "top_players": [],
            "market_size_hint": "中",
            "confidence": 0.5,
            "source": "fallback",
        }
        # fallback 也缓存（避免连续 LLM 失败）
        try:
            from cache.redis_client import redis_set_json
            redis_set_json(_cache_key, fallback, ex=300)  # 5 分钟，失败时短缓存
        except Exception:
            pass
        return fallback


def calc_required_articles(competition_count: int, target_share: float) -> int:
    """计算需要投放的文章数（与 transparent_pricing.calculate_required_articles 一致）

    真实公式（系统既定，勿改）:
      required = ceil(target_share × competition / (1 - target_share))

    不对数值做人工分档 clamp — 之前 30/60/100 分段破坏了 "按 SOV 精确算篇数"
    的设计（25% 和 33% 算出篇数应该不同）。保留极值保护：
      - 最少 3 篇（低于 3 篇无法形成 GEO 曝光规模）
      - 最多 300 篇（单词托管单月上限，防 UI 爆表；超过走销售端批量报价）
    """
    if target_share >= 1:
        target_share = 0.9
    if target_share <= 0:
        target_share = 0.1
    required = math.ceil(target_share * competition_count / (1 - target_share))
    return max(3, min(required, 300))


def _fallback_platforms_from_research(industry: str, articles_needed: int) -> list[dict]:
    """当 mhz_media 为空无法交叉匹配时，用 GEO 调研真实数据降级

    数据来源（优先级）：
      1. services.placement_service.PlacementService 从 geo_engine_stats 表
         查该行业真实聚合评分（基于 geo_research_raw 原始引用记录聚合）
      2. 上述返空则用 PLATFORM_ENGINE_COVERAGE 全局兜底
         （同文件 L115，基于 2026-03 实测 7,557 条引用记录，知乎 79.6%/搜狐 45.6%/
         抖音 61.3%/今日头条 47.8%/新浪 48.1% 等真实引用率）

    ⚠️ 不硬编码"行业 → 媒体"映射 — 系统已有基于真实调研的数据源。
    """
    from services.placement_service import PLATFORM_ENGINE_COVERAGE, get_placement_service

    # 1. 优先查 geo_engine_stats 真实行业数据
    scoring: dict = {}
    try:
        svc = get_placement_service()
        scoring, matched = svc._match_research_industry(industry or "")
        if not scoring:
            scoring = PLATFORM_ENGINE_COVERAGE
    except Exception as e:
        logger.warning(f"_fallback_platforms 读调研失败，用全局兜底: {e}")
        scoring = PLATFORM_ENGINE_COVERAGE

    # 2. 按 score 排序 top N（N = articles 分配需要的平台数，最多 5 家）
    sorted_plats = sorted(
        scoring.items(),
        key=lambda x: x[1].get("score", 0),
        reverse=True,
    )
    top_n = min(5, max(2, articles_needed // 10 or 1), len(sorted_plats))
    chosen = sorted_plats[:top_n]

    # 3. 按 score 加权分配文章数（高引用率平台得更多文章）
    total_score = sum(info.get("score", 1) for _, info in chosen) or 1
    platforms: list[dict] = []
    assigned = 0
    for i, (name, info) in enumerate(chosen):
        if i == len(chosen) - 1:
            # 最后一个平台吃掉余数，保证总数等于 articles_needed
            count = articles_needed - assigned
        else:
            weight = info.get("score", 1) / total_score
            count = max(1, round(articles_needed * weight))
        assigned += count
        platforms.append({
            "platform": name,
            "count": count,
            # 价格保守估 — 生产 mhz_media 接入后走 recommend_for_publish 真实 our_price_yuan
            "cost_per_yuan": _estimate_platform_price(info.get("type", "")),
            "engines": info.get("engines", []),
            "inclusion_rate": f'调研分 {info.get("score", 0):.1f}',
        })
    return platforms


def _estimate_platform_price(plat_type: str) -> float:
    """按平台类型估价（mhz_media 空时的兜底价格，生产应从 mhz_media 读）"""
    # 基于外部发布通道历史价格区间的保守中位数
    return {
        "门户": 60.0,         # 示例值
        "权威媒体": 100.0,    # 示例值
        "UGC社区": 40.0,      # 示例值
        "字节系": 40.0,       # 示例值
        "搜索引擎": 30.0,     # 示例值
        "技术社区": 30.0,     # 示例值
        "消费社区": 40.0,
        "视频UGC": 60.0,      # 示例值
        "科技商业": 80.0,     # 示例值
        "百科": 20.0,
        "自媒体": 40.0,
        "财经媒体": 100.0,
    }.get(plat_type, 50.0)


def estimate_detection_rate(
    articles: int,
    competition_level: int,
    target_share: Optional[float] = None,
) -> float:
    """估算 30 天后该词 AI 检出率

    2026-04-18：传 target_share 时走 SOV 饱和曲线（4 档真实分化：
      SOV 15% → 56% / 25% → 76% / 33% → 86% / 50% → 97%），
    否则走旧的"articles × 10%"经验公式（封顶 85%）保持向后兼容。

    ⚠️ [E1 2026-06-05 假设·待校准] 下列常数【均无实证来源】(老板"是不是拍脑袋"=是):
      ① 饱和曲线指数 5(「5 次搜索」)· ② 竞争衰减 0.04/级 · ③ 旧公式 0.30/0.10/0.08。
      文档示例 56/76/86/97 是旧档值,已被产品定稿目标出现率 50/65/75 取代(见 selection_api TIER_CONFIG)。
      本轮【不改公式/数值】· 仅标注 · 上线后用监测真实上榜数据回灌校准(同 band Q7 回填思路)。
    """
    if target_share and 0 < target_share <= 1:
        # SOV 饱和曲线：5 次搜索中至少出现 1 次的概率 = 1 - (1 - SOV)^5  ← [假设·待校准] 指数 5 无实证
        base = 1 - (1 - target_share) ** 5
        competition_factor = 1.0 - (competition_level - 1) * 0.04  # [假设·待校准] 0.04/级 无实证
        return round(min(base * competition_factor, 0.97), 2)
    # 旧公式（老 caller 不传 target_share 时兼容）· [假设·待校准] 0.30/0.10/0.08 无实证
    base_rate = 0.30
    per_article = 0.10
    competition_factor = 1.0 - (competition_level - 1) * 0.08  # 竞争越激烈，每篇贡献越小
    rate = base_rate + articles * per_article * competition_factor
    return round(min(rate, 0.85), 2)


async def estimate_user_cost(
    keyword: str,
    city: Optional[str] = None,
    industry: Optional[str] = None,
    target_rank: str = "top_10",
    target_share: Optional[float] = None,
    market: Optional[dict] = None,
    platforms_raw: Optional[list] = None,
) -> dict[str, Any]:
    """C 端透明成本估算

    Args:
        keyword: 关键词，如"深圳全屋定制"
        city: 城市（可选）
        industry: 行业（可选）
        target_rank: "top_3" | "top_10" | "top_30"（老接口，兼容）
        target_share: 精确目标份额 0.0-1.0（2026-04-17 新增，优先于 target_rank）
                      让 SOV 15%/25%/33%/50% 不再被 map 压到 3 档，各档篇数真实分化
        market: 预计算的市场饱和度数据（传入则跳过 LLM 调用）
        platforms_raw: 预计算的平台列表（传入则跳过 DB 查询）

    Returns:
        完整的成本估算卡片数据
    """
    # 1. 市场饱和度（支持外部传入，避免重复 LLM 调用）
    if market is None:
        market = await estimate_market_saturation(keyword, industry or "", city or "")

    # 2. 文章数推算 — 优先使用精确 target_share，向后兼容 target_rank
    if target_share is None:
        target_share_map = {"top_3": 0.5, "top_10": 0.3, "top_30": 0.15}
        target_share = target_share_map.get(target_rank, 0.3)
    articles_needed = calc_required_articles(market["competition_count"], target_share)

    # 3. 平台分配（支持外部传入，避免重复 DB 查询）
    if platforms_raw is None:
        try:
            from services.placement_service import recommend_for_publish
            platforms_raw = recommend_for_publish(
                industry=industry or "",
                keywords=keyword,
                article_type="",
                limit=min(articles_needed * 2, 15),
            )
        except Exception as e:
            logger.warning(f"recommend_for_publish 失败: {e}")
            platforms_raw = []

    # 分配文章到平台（每平台 1 篇，多余的平均分）
    platforms: list[dict] = []
    if platforms_raw:
        take = min(articles_needed, len(platforms_raw))
        for i in range(take):
            p = platforms_raw[i]
            platforms.append({
                "platform": p.get("platform", p.get("media_name", "未知平台")),
                "count": 1,
                "cost_per_yuan": float(p.get("our_price_yuan", 0)),
                "engines": p.get("engines", []),
                "inclusion_rate": p.get("inclusion_rate", "-"),
            })
        # 如果 articles_needed > take，均摊剩余到已有平台
        remaining = articles_needed - take
        idx = 0
        while remaining > 0 and platforms:
            platforms[idx % len(platforms)]["count"] += 1
            remaining -= 1
            idx += 1
    else:
        # 2026-04-17: mhz_media 为空时用真实 GEO 调研数据降级
        # 数据源（优先级）：
        #   1. geo_engine_stats 表（按行业真实聚合评分）
        #   2. PLATFORM_ENGINE_COVERAGE 全局兜底（2026-03 实测 7,557 条引用记录）
        # 不再硬编码"行业 → 媒体"映射
        platforms = _fallback_platforms_from_research(industry or "", articles_needed)

    # 4. 成本核算（不加 markup）
    content_cost_points = articles_needed * CONTENT_COST_POINTS
    content_cost_yuan_equiv = content_cost_points / POINTS_PER_YUAN
    media_cost_yuan = sum(p["cost_per_yuan"] * p["count"] for p in platforms)

    # 5. 预估效果（2026-04-18：传 target_share 走 SOV 饱和曲线，4 档真实分化）
    detection_rate = estimate_detection_rate(
        articles_needed,
        market["competition_level"],
        target_share=target_share,
    )

    return {
        "keyword": keyword,
        "city": city or "",
        "industry": industry or "",
        "target_rank": target_rank,
        "market_analysis": market,
        "strategy": {
            "articles_needed": articles_needed,
            "platform_distribution": platforms,
        },
        "cost_breakdown": {
            "content_points": content_cost_points,
            "content_yuan_equiv": round(content_cost_yuan_equiv, 2),
            "media_yuan_range": [
                round(media_cost_yuan * 0.9, 2),
                round(media_cost_yuan * 1.1, 2),
            ],
            "total_yuan_range": [
                round(content_cost_yuan_equiv + media_cost_yuan * 0.9, 2),
                round(content_cost_yuan_equiv + media_cost_yuan * 1.1, 2),
            ],
        },
        "expected_effect": {
            "detection_rate_30d_pct": round(detection_rate * 100),
            "note": "基于投放策略的保守估算，实际受内容质量和平台审核影响",
        },
    }


async def one_click_geo_plan(
    brand_name: str = "",
    description: Optional[str] = None,
    profile_data: Optional[dict] = None,
    *,
    brand_id: Optional[int] = None,
    user_id: Optional[int] = None,
    data_mode: str = "full",
    brand_snapshot: Optional[dict] = None,
    progress_callback: Optional[Callable[[str, int, str], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """一句话生成完整 GEO 方案

    [CTO-15.5 2026-04-20 Phase 4 PLAN 02] 重构:删除超时降级路径 + 加 progress_callback
      · 删除 asyncio.wait_for(timeout=35/40) 裸 await,和代理端 selection_api.py:871/1036 对齐
      · 删除 L786-826 估算降级分支(结果结构不兼容,raise 比返垃圾好)
      · 新 progress_callback(stage, percent, message) 回调给 worker 层上报进度
      · 新 brand_snapshot 参数,D13 防 brand 中途被删(worker 保留启动时 snapshot)
      · 扩词 < 10 / clusters == 0 → raise LowQualityResult
      · 外部 API 故障 → raise ExternalAPIError(worker 层 retry 2 次)

    [CTO-15.4 2026-04-20 P0-F] profile_data 参数 · 传入时优先用真实品牌深度数据
      · profile_data.industry / business / city: 跳过 LLM 识别,直接用 DB 真实值
      · profile_data.industry_brief: v3.6 深度行业分析(service_scope/local_competitors/hot_formats)
      · 作用:解决 LLM 瞎编品牌名 + 乱猜行业 bug (老板实测 laowang='老王家常菜' 被编成'赵胡子贵阳')


    v1.1-ux-fix 2026-04-19: 关键词质量重构
      原实现: advisor_flash 单次 LLM 瞎生成 10 个词 → 用户反映"完全不搭嘎"
      现实现: 复用报价系统 expand_keywords_for_client (5118 长尾 + 地域下钻 + LLM)
      另: 删除前 7 免费 / 后 3 锁定分层, 全部开放

    v1.1-data-fix 2026-04-19: 让推荐真正基于 GEO 调研数据
      症状: "推荐和我们采集的数据没关联"
      根因: LLM 识别 industry 自由命名 ("AI saas 工具"/"火锅"), 但调研库 14 类
            精确/子串/bigram 匹配仅 25% 命中, 75% 走硬编码 PLATFORM_ENGINE_COVERAGE
      修复: LLM 识别时**强制从调研库 14 分类中选一个**, 一次 LLM 搞定

    v1.1-cache-fix 2026-04-19: 品牌识别结果 Redis 缓存 30 天 + 运营分析日志
      - 缓存 key: sha1(brand_name + description) → 同品牌复查 0 LLM 调用
      - 记录 industry_mapping_log: LLM 原 industry / 最终 industry / map_source / cache_hit
        运营可 SQL 查"哪些品牌映射到通用兜底"定位下一批该采集的调研分类

    步骤:
      0. 拉调研库真实 industries 列表
      1. [新] 先查 Redis 品牌识别缓存 → 命中直接用
      2. 未命中: advisor_flash 从 14 分类选 industry + 识别 city + 2-3 核心词
      3. 后处理校验: LLM 选错则 fallback _match_research_industry
      4. [新] 写缓存 + 写 industry_mapping_log
      5. 核心词喂 expand_keywords_for_client 真实检索
      6. estimate_user_cost × 10 并行算价
    """
    import asyncio
    import hashlib as _hashlib

    # ------------------------------------------------------------
    # [Phase 4 PLAN 02] progress_callback 内部 helper · 兼容 None
    # ------------------------------------------------------------
    async def _notify(stage: str, percent: int, message: str):
        if progress_callback is None:
            return
        try:
            await progress_callback(stage, percent, message)
        except asyncio.CancelledError:
            # D4: worker 层 cancel 检测靠这个 bubble up
            raise
        except Exception as e:
            logger.warning(f"[one_click_geo_plan] progress_callback 失败: {e}")

    # ------------------------------------------------------------
    # [Phase 4 PLAN 02] D13 brand_snapshot 注入
    # ------------------------------------------------------------
    # worker 层在 start_task 时已把 brand 真实数据打 snapshot 传进来,
    # 这样即便任务执行中 brand 被删 / industry 被改,我们仍用启动时的数据
    # (不查 DB 避开竞态). 没传 snapshot (老调用链) 走原逻辑.
    if brand_snapshot and isinstance(brand_snapshot, dict):
        if not brand_name:
            brand_name = brand_snapshot.get("name") or ""
        if not description:
            description = brand_snapshot.get("description") or None
        if profile_data is None:
            _snap_profile = brand_snapshot.get("profile") or {}
            if not isinstance(_snap_profile, dict):
                _snap_profile = {}
            profile_data = {
                "industry": (
                    brand_snapshot.get("industry")
                    or _snap_profile.get("industry")
                    or ""
                ),
                "city": (
                    _snap_profile.get("city")
                    or (
                        brand_snapshot.get("cities")
                        if isinstance(brand_snapshot.get("cities"), str)
                        else ""
                    )
                    or ""
                ),
                "business": _snap_profile.get("business") or "",
                "target_users": _snap_profile.get("target_users") or "",
                "industry_brief_status": _snap_profile.get("industry_brief_status"),
            }

    # stage=identify · 入口上报
    await _notify("identify", 5, "识别品牌与城市")

    # 0. 拉调研库真实 industries (保 LLM 选的是"能匹配上调研数据"的分类)
    research_industries: list = []
    try:
        from services.placement_service import get_placement_service
        research_industries = await asyncio.to_thread(
            lambda: get_placement_service().get_all_industries()
        )
    except Exception as e:
        logger.warning(f"one_click_geo_plan 拉调研行业列表失败: {e}")

    # 1-A. 先查品牌识别缓存 (30 天 TTL)
    #   cache_key 只基于 brand_name + description, 不包含 research_industries:
    #     调研行业列表扩容后老缓存仍可用 (final_industry 是 14 类之一, 新行业是扩张集)
    #     如需让所有老用户重映射, 改 cache_key 前缀版本号即可
    # [GEO-R8-CAN-002] 缓存 key 加租户维度 (brand_id / user_id):
    #   原 key 仅 brand_name+description → 两个不同租户同名品牌 (同/空描述、都无 profile industry)
    #   会命中同一条缓存, B 复用 A 的 industry/city/core_keywords (跨租户串味). 加租户标识隔离.
    #   前缀升 v2 强制老缓存重映射 (30 天 TTL 自然过期, 无正确性风险).
    _tenant_scope = f"b{brand_id}|u{user_id}" if (brand_id or user_id) else "anon"
    _brand_cache_key_src = f"{_tenant_scope}|{brand_name}|{description or ''}"
    _brand_cache_key = (
        f"cend_brand_identify:v2:{_hashlib.sha1(_brand_cache_key_src.encode('utf-8')).hexdigest()[:16]}"
    )
    brand_info = {
        "industry": "", "industry_display": "",
        "city": "", "keywords": [], "core_keywords": [],
    }
    _cached_identify = None

    # [CTO-15.4 2026-04-20 P0-F] 优先用 profile_data 真实数据,跳过 LLM 瞎编
    #   老板实测 bug: brand_name='老王家常菜'被 LLM 编成'赵胡子',industry 也乱猜
    #   profile_data 由上游 API full_plan 从 DB 查 brands + client_profiles 组装
    #   如果 profile_data 提供了真实 industry, 我们直接构造 _cached_identify 短路下方 LLM 路径
    if profile_data and isinstance(profile_data, dict):
        _real_industry = (profile_data.get("industry") or "").strip()
        _real_city = (profile_data.get("city") or "").strip()
        _real_business = (profile_data.get("business") or "").strip()
        if _real_industry:
            _std_industry = _real_industry
            _map_source = "profile_direct"
            # 如果真实 industry 不在调研库,走映射兜底
            if research_industries and _real_industry not in research_industries:
                try:
                    from services.placement_service import get_placement_service
                    svc = get_placement_service()
                    _, matched = await asyncio.to_thread(
                        lambda: svc._match_research_industry(_real_industry, brand=brand_snapshot)
                    )
                    if matched:
                        _std_industry = matched
                        _map_source = "profile_bigram"
                    else:
                        _std_industry = "通用" if "通用" in research_industries else _real_industry
                        _map_source = "profile_general_fallback"
                except Exception:
                    pass
            # 核心词种子: 业务描述 + 品牌名 + 行业
            _core_kws_seed: list = []
            if _real_business and len(_real_business) >= 3:
                _core_kws_seed.append(_real_business[:20])
            _core_kws_seed.append(brand_name)
            if _real_industry:
                _core_kws_seed.append(_real_industry)
            _cached_identify = {
                "industry": _std_industry,
                "industry_display": _real_industry,
                "city": _real_city or "全国",
                "core_keywords": _core_kws_seed[:3],
                "llm_industry": _real_industry,
                "map_source": _map_source,
            }
            logger.info(
                f"[one_click_geo_plan] P0-F 用 profile 真实数据跳过 LLM: brand_name='{brand_name}' "
                f"industry='{_std_industry}' city='{_real_city}' "
                f"business='{(_real_business or '')[:40]}' map_source={_map_source}"
            )

    # 只在 profile 分支没设置 _cached_identify 时才查 Redis (避免覆盖真实数据)
    if not _cached_identify:
        try:
            from cache.redis_client import redis_get_json
            _cached_identify = redis_get_json(_brand_cache_key)
        except Exception as e:
            logger.debug(f"[one_click_geo_plan] 读品牌识别缓存失败 (忽略): {e}")

    if _cached_identify and isinstance(_cached_identify, dict) and _cached_identify.get("industry"):
        brand_info = {
            "industry": _cached_identify.get("industry", ""),
            "industry_display": _cached_identify.get("industry_display", "") or _cached_identify.get("industry", ""),
            "city": _cached_identify.get("city", ""),
            "core_keywords": _cached_identify.get("core_keywords") or [brand_name],
        }
        logger.info(
            f"[one_click_geo_plan] 品牌识别缓存命中: {brand_name} → industry='{brand_info['industry']}' "
            f"(源 map_source={_cached_identify.get('map_source', 'cached')})"
        )
        # 分析日志: cache_hit=True
        try:
            from db.analytics_db import log_industry_mapping
            await asyncio.to_thread(
                log_industry_mapping,
                brand_name=brand_name,
                description=description or "",
                llm_industry=_cached_identify.get("llm_industry", ""),
                final_industry=brand_info["industry"],
                map_source=_cached_identify.get("map_source", "cached"),
                cache_hit=True,
                city=brand_info["city"],
                core_keywords=brand_info["core_keywords"],
            )
        except Exception:
            pass

    # 1-B. 缓存未命中 → 走 LLM 识别
    if not (_cached_identify and _cached_identify.get("industry")):
        _llm_industry_raw = ""
        _map_source = "error_fallback"  # 默认兜底, 走到哪一层就覆盖

        try:
            from services.llm.advisor_llm import advisor_flash
            if research_industries:
                industry_rule = (
                    f"**必须从下列调研库的标准分类中选一个最相近的**（这是真实 AI 搜索引用率数据的分类依据）：\n"
                    f"   {'、'.join(research_industries)}\n"
                    f"   如果品牌业务与上述分类都明显不匹配，选「通用」"
                )
            else:
                industry_rule = "所属行业（宽泛分类名，如'企业服务'/'装修建材'）"

            prompt = f"""分析品牌基础信息（严格 JSON，无 markdown）：

品牌名：{brand_name}
描述：{description or "无"}

要求识别:
1. 所属行业 - {industry_rule}
2. 行业展示名 (industry_display): 用户理解的具体行业俗名, 如"AI 写作 SaaS"/"深圳装修公司", 仅 UI 展示用
3. 主要城市（如未提及则"全国"）
4. 2-3 个最核心的业务关键词（种子词, 不是最终展示给用户的词）
   - 要具体业务词，不要品牌名
   - 如: 品牌"Jasper AI" → 核心词 ["AI 写作工具", "AI 内容生成"]
   - 如: 品牌"宅小秘" → 核心词 ["家政保洁", "日常打扫"]

返回:
{{
  "industry": "<调研库标准分类名>",
  "industry_display": "<用户理解的行业俗名>",
  "city": "主要城市或 '全国'",
  "core_keywords": ["核心业务词 1", "核心业务词 2"]
}}
"""
            result = await advisor_flash(prompt, history=[])
            text = result.strip() if isinstance(result, str) else result.get("text", "").strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            import json
            parsed = json.loads(text.strip())
            core_kws = parsed.get("core_keywords") or parsed.get("keywords") or []

            llm_industry = (parsed.get("industry") or "").strip()
            _llm_industry_raw = llm_industry
            industry_display = (parsed.get("industry_display") or llm_industry).strip()

            # 校验 LLM 选的 industry 是否在调研库; 不在则 fallback _match_research_industry
            standard_industry = llm_industry
            if research_industries and llm_industry in research_industries:
                _map_source = "llm_direct"
            elif research_industries and llm_industry and llm_industry not in research_industries:
                try:
                    from services.placement_service import get_placement_service
                    svc = get_placement_service()
                    _, matched = await asyncio.to_thread(
                        lambda: svc._match_research_industry(industry_display or llm_industry,
                                                             brand=brand_snapshot)
                    )
                    if matched:
                        standard_industry = matched
                        _map_source = "bigram_fallback"
                        logger.info(
                            f"[one_click_geo_plan] LLM industry='{llm_industry}' 不在调研库, "
                            f"映射到 '{matched}'"
                        )
                    else:
                        standard_industry = "通用" if "通用" in research_industries else (
                            research_industries[0] if research_industries else llm_industry
                        )
                        _map_source = "general_fallback"
                        logger.warning(
                            f"[one_click_geo_plan] LLM industry='{llm_industry}' 不在调研库且 "
                            f"映射未命中, 降级到 '{standard_industry}'"
                        )
                except Exception as e:
                    logger.warning(f"[one_click_geo_plan] industry 映射异常, 保留 LLM 原值: {e}")
                    _map_source = "error_fallback"
            else:
                # research_industries 拉取失败, LLM 输出什么就用什么
                _map_source = "llm_direct"

            brand_info = {
                "industry": standard_industry,
                "industry_display": industry_display or standard_industry,
                "city": parsed.get("city", ""),
                "core_keywords": core_kws[:3] if core_kws else [brand_name],
            }
            logger.info(
                f"[one_click_geo_plan] 品牌识别: {brand_name} · LLM 原 industry='{llm_industry}' "
                f"→ 调研库 industry='{standard_industry}' · display='{brand_info['industry_display']}' · "
                f"map_source={_map_source}"
            )

            # 写缓存 (30 天 TTL)
            try:
                from cache.redis_client import redis_set_json
                redis_set_json(_brand_cache_key, {
                    "industry": brand_info["industry"],
                    "industry_display": brand_info["industry_display"],
                    "city": brand_info["city"],
                    "core_keywords": brand_info["core_keywords"],
                    "llm_industry": _llm_industry_raw,
                    "map_source": _map_source,
                }, ex=30 * 24 * 3600)
            except Exception as _ce:
                logger.debug(f"[one_click_geo_plan] 写品牌识别缓存失败 (忽略): {_ce}")

        except Exception as e:
            logger.warning(f"one_click_geo_plan LLM 识别品牌失败，降级用 brand_name: {e}")
            brand_info = {
                "industry": "", "industry_display": brand_name,
                "city": "全国", "core_keywords": [brand_name],
            }
            _map_source = "error_fallback"

        # 分析日志: cache_hit=False (新识别)
        try:
            from db.analytics_db import log_industry_mapping
            await asyncio.to_thread(
                log_industry_mapping,
                brand_name=brand_name,
                description=description or "",
                llm_industry=_llm_industry_raw,
                final_industry=brand_info.get("industry", ""),
                map_source=_map_source,
                cache_hit=False,
                city=brand_info.get("city", ""),
                core_keywords=brand_info.get("core_keywords") or [],
            )
        except Exception:
            pass

    # 2. 调报价系统关键词扩展引擎（5118 长尾 + 地域下钻 + LLM 扩展）
    #
    # [Phase 4 PLAN 02 · CTO-15.5 2026-04-20] 删除 asyncio.wait_for 超时降级.
    # 老 L713-826 的 timeout=35s + 降级核心词 + estimate_user_cost × 10 分支被删除,
    # 改走裸 await (和代理端 api/selection_api.py:871 对齐);
    # worker 层用 asyncio.wait_for(total_timeout=900) 兜总超时,
    # 外部 API 故障 raise ExternalAPIError (worker retry 2 次),
    # 扩词 < 10 raise LowQualityResult (worker 标 failed:low_quality)
    import time as _time
    keywords: list = []
    expand_start = _time.time()
    await _notify("expand", 15, f"扩展关键词池 · 核心词={brand_info['core_keywords']}")
    try:
        from tools.keyword_expander import expand_keywords_for_client
        # P0.3 (CTO-15.7 2026-04-24):透传 profile_data · _llm_expand 调
        # get_industry_context 注入 v3.6/v3.7 道法术器素材池 · 解"老王家常菜 → 大家好入池"跑题
        expand_result = await expand_keywords_for_client(
            core_keywords=brand_info["core_keywords"],
            industry=brand_info["industry"],
            city=brand_info["city"],
            # [#46 字段错位修复] business_scope = 经营范围；description = 品牌简介，二者不同
            # profile_data.business 才是经营范围真实字段，description 只是 fallback 兜底
            business_scope=((profile_data or {}).get("business") or description or ""),
            # 扩 50 个 → 取前 30 (代理端基线对齐 · CTO-15.4 P0-B)
            target_count=50,
            profile_data=profile_data,  # P0.3 · 从 one_click_geo_plan 入参透传
            brand_name=brand_name,  # [CTO-15.23 2026-05-11] 品牌锚定透传
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # 外部 API (5118 / LLM / 网络) 故障 → worker retry
        logger.warning(
            f"[one_click_geo_plan] expand_keywords 异常 "
            f"({_time.time() - expand_start:.1f}s): {e}"
        )
        raise ExternalAPIError("expand_keywords", str(e))

    expand_elapsed = _time.time() - expand_start
    if expand_result.get("success") and expand_result.get("keywords"):
        expanded = expand_result["keywords"][:30]
        keywords = [
            (kw["keyword"] if isinstance(kw, dict) else kw)
            for kw in expanded
        ]
        logger.info(
            f"[one_click_geo_plan] expand_keywords 成功: "
            f"{len(keywords)} 词 · {expand_elapsed:.1f}s · 核心词={brand_info['core_keywords']}"
        )
    else:
        logger.warning(
            f"[one_click_geo_plan] expand_keywords 返回失败 ({expand_elapsed:.1f}s): "
            f"{expand_result.get('error', 'unknown')}"
        )

    # 质量校验: 少于 10 词无法聚出 3-5 主题包 → LowQualityResult
    if len(keywords) < 10:
        raise LowQualityResult(
            "expand_too_few",
            partial={"keywords": keywords, "core_keywords": brand_info["core_keywords"]},
        )

    # 兼容老字段
    brand_info["keywords"] = keywords

    # 3. [v1_2 CTO-14.0] 复用代理端同源报价引擎（C 端 skip_markup=True 透明成本）
    #    目的：C 端和代理端共用同一套 generate_cluster_quote（聚类 + 三维评分 + 三档定价 + 7 天全局价格锁）
    #    差异仅在 markup：代理端 2.0 溢价 / C 端 1.0 透明成本
    #
    # [Phase 4 PLAN 02] 同样删除 asyncio.wait_for(40s) + 降级 estimate_user_cost × 10,
    # 改走裸 await;外部 API 故障 raise ExternalAPIError;clusters 空 raise LowQualityResult
    await _notify("audit", 45, f"已获取 {len(keywords)} 词 · 正在做竞品饱和度")

    # [CTO-15.5 Q1.A+B 2026-04-20] 把 one_click 的 progress_callback 透传给 generate_cluster_quote
    # batch_pricing 内部会 call metaso_search(47%)/audit_cluster(55% 并行)/pricing(63%) 细粒度推进度
    # 签名适配: batch_pricing progress_callback 签名 (stage, label, percent)
    #          one_click progress_callback 签名 (stage, percent, message) — 参数顺序不同
    async def _inner_progress(stage: str, label: str, percent: int):
        await _notify(stage, percent, label)

    cluster_data: dict = {}
    try:
        from tools.batch_pricing import generate_cluster_quote
        cluster_data = await generate_cluster_quote(
            keywords=keywords,
            brand_name=brand_name,
            industry=brand_info["industry"],
            city=brand_info["city"],
            cached_competitors=None,
            skip_markup=True,
            progress_callback=_inner_progress,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[one_click_geo_plan] generate_cluster_quote 异常: {e}")
        raise ExternalAPIError("generate_cluster_quote", str(e))

    await _notify("cluster", 65, "聚类主题包")

    if not cluster_data or not cluster_data.get("clusters"):
        # [Workflow 审计修 2026-06-11 · 真实第一] 全断供 ≠ 低质量:数据断供应按外部服务故障
        #   归因(走 retry · 提示"网络繁忙稍后重试"),不能误报"请补充关键词"让用户错改输入
        if cluster_data and cluster_data.get("unavailable_keywords"):
            raise ExternalAPIError("pricing_unavailable", "网络繁忙,关键词暂时无法估价,请稍后重试")
        raise LowQualityResult(
            "cluster_empty",
            partial={"keywords": keywords, "brand_info": brand_info},
        )

    await _notify("pricing", 90, "生成三档定价")

    # 4. [新] 展开 cluster_data 得到前端友好字段
    tier_summaries = cluster_data.get("tier_summaries", {})
    clusters = cluster_data.get("clusters", [])

    # 3-a. 平台推荐聚合（从各词的 recommended_platforms 抠，取 top 8）
    platform_set: dict[str, int] = {}
    total_competition = 0
    for cluster in clusters:
        for kw_obj in list(cluster.get("core_keywords", [])) + list(cluster.get("covered_keywords", [])):
            if kw_obj.get("source") == "generated_variant":
                continue
            total_competition += int(kw_obj.get("effective_competition", kw_obj.get("competitor_count", 0)) or 0)
            for plat in kw_obj.get("recommended_platforms", []) or []:
                name = plat.get("platform") if isinstance(plat, dict) else str(plat)
                if not name:
                    continue
                platform_set[name] = platform_set.get(name, 0) + 1
    # v1_2: top 12 平台供前端按档位切片（基础 5 / 进阶 7 / 旗舰 9）
    platforms_overall = [
        {"platform": name, "total_count": cnt}
        for name, cnt in sorted(platform_set.items(), key=lambda x: -x[1])
    ][:12]

    # 3-b. 老字段兼容 keyword_package（扁平化所有词 + 角色标注）
    keyword_package: list[dict] = []
    for cluster in clusters:
        theme = cluster.get("theme", "")
        for kw_obj in cluster.get("core_keywords", []) or []:
            if kw_obj.get("source") == "generated_variant":
                continue
            keyword_package.append({
                "keyword": kw_obj.get("keyword"),
                "cluster_theme": theme,
                "role": "core",
                "entry_price": int(kw_obj.get("entry", {}).get("price", kw_obj.get("entry_price", 0)) or 0),
                "standard_price": int(kw_obj.get("standard", {}).get("price", kw_obj.get("standard_price", 0)) or 0),
                "flagship_price": int(kw_obj.get("flagship", {}).get("price", kw_obj.get("flagship_price", 0)) or 0),
                # v1_3 (CTO-15.1 2026-04-19): 加 strong 档对齐托管 SOV_TIERS 4 档
                "strong_price": int(kw_obj.get("strong", {}).get("price", kw_obj.get("strong_price", 0)) or 0),
                "competitor_count": kw_obj.get("competitor_count", 0),
                "difficulty_level": _count_to_level(int(kw_obj.get("effective_competition", kw_obj.get("competitor_count", 0)) or 0)),
            })
        for kw_obj in cluster.get("covered_keywords", []) or []:
            if kw_obj.get("source") == "generated_variant":
                continue
            keyword_package.append({
                "keyword": kw_obj.get("keyword"),
                "cluster_theme": theme,
                "role": "covered",
                "entry_price": int(kw_obj.get("entry_price", 0) or 0),
                "standard_price": int(kw_obj.get("standard_price", 0) or 0),
                "flagship_price": int(kw_obj.get("flagship_price", 0) or 0),
                # v1_3: 附赠词 strong 档
                "strong_price": int(kw_obj.get("strong_price", 0) or 0),
                "competitor_count": kw_obj.get("competitor_count", 0),
                "difficulty_level": _count_to_level(int(kw_obj.get("effective_competition", kw_obj.get("competitor_count", 0)) or 0)),
            })

    # 3-c. 8 维度 ℹ️ 小标签溯源（业务层稳定话术，不暴露具体算法）
    cluster_count = cluster_data.get("stats", {}).get("cluster_count", len(clusters))
    data_provenance = {
        "data_source": "OmniRank GEO 大数据",
        "industry_display": brand_info.get("industry_display", ""),
        "industry_canonical": brand_info.get("industry", ""),
        "city": brand_info.get("city", ""),
        "keyword_count": len(keyword_package),
        "cluster_count": cluster_count,
        "total_competition": total_competition,
        "dimensions": [
            {"name": "行业竞争密度", "detail": f"【{brand_info.get('industry_display') or brand_info.get('industry') or '行业'}】当前约 {total_competition} 条竞争内容"},
            {"name": "关键词真实热度", "detail": "月搜索量级分档加权"},
            {"name": "内容权威度分级", "detail": "头部品牌 / 行业媒体 / 百科 / 低质 四档加权"},
            {"name": "AI 引擎引用规律", "detail": "主流 AI（DeepSeek / Kimi / 豆包 / 通义）每次回答平均带 4 条来源"},
            {"name": "媒体平台 GEO 命中率", "detail": "各发布平台被 AI 引用的历史概率"},
            {"name": "地域细分权重", "detail": "本地搜索加成系数" if brand_info.get("city") and brand_info["city"] != "全国" else "全国口径无地域加成"},
            {"name": "关键词语义聚类", "detail": f"{cluster_count} 个主题包（同语义自动合并）"},
            {"name": "历史 GEO 实测样本", "detail": "50 万+ 实测记录 · 每日监测持续积累"},
        ],
        # 前端按档位切 platforms_overall：基础 5 / 进阶 7 / 旗舰 9
        "platform_tier_caps": {"entry": 5, "standard": 7, "flagship": 9},
    }

    # 3-d. 总投入范围：entry 核心价（最低）→ strong 全量价（最高）
    # v1_3 (CTO-15.1 2026-04-19): 4 档后最高档改为 strong（原最高是 flagship）
    entry_core = int(tier_summaries.get("entry", {}).get("core_price", 0) or 0)
    strong_full = int(tier_summaries.get("strong", {}).get("full_price", 0) or 0)
    flagship_full = int(tier_summaries.get("flagship", {}).get("full_price", 0) or 0)
    standard_core = int(tier_summaries.get("standard", {}).get("core_price", 0) or 0)
    # 最高档兜底：strong_full → flagship_full → standard_core × 2
    highest_price = strong_full if strong_full > entry_core else (
        flagship_full if flagship_full > entry_core else standard_core * 2
    )

    # [Phase 4 PLAN 02] 最后一次进度上报 (99%) · 让前端看到 "整理结果中"
    await _notify("pricing", 99, "整理结果")

    # [P1 补完 2026-08-08] 三档篇数随方案一起下发。
    #   不下发的后果是实测过的:前端只好拿 `Math.round(price/60)` 反推,33 个词错 32 个,
    #   合计多冻结 60% 容量。这里只是把 clusters 里**已经算好**的数搬到 keyword_package 上,
    #   不新算任何篇数(主合同阶梯仍在 tools/pricing_bands 单点)。
    from services.c_end_plan_capacity import attach_tier_article_capacity

    result = {
        "brand_name": brand_name,
        "brand_info": brand_info,
        # v1_3: 4 档 tier_summaries + 聚类（旧 C 端 GEO 方案卡 消费）
        "tier_summaries": tier_summaries,
        "clusters": clusters,
        # [Workflow 审计修 2026-06-11 · 真实第一] 断供词透传(C 端部分断供不许静默缺词)
        "unavailable_keywords": cluster_data.get("unavailable_keywords", []),
        # 老字段兼容（单档兜底展示）
        # [CTO-15.4 2026-04-20 P0-B] 老板反馈 C 端 10 词 vs 代理端 20-30 → 取 30 对齐代理端
        "keyword_package": keyword_package[:30],
        "locked_keywords": [],
        "platforms_overall": platforms_overall,
        "total_investment_yuan_range": [entry_core, highest_price],
        # 8 维度溯源（ℹ️ 小标签消费）
        "data_provenance": data_provenance,
        # 下一步（点"确认方案"会调新端点 from-c-end-plan 持久化到写作大厅）
        "next_actions": [
            {"label": "确认方案 · 开始写作", "action": "confirm_plan"},
            {"label": "调整关键词", "action": "adjust_keywords"},
        ],
    }
    return attach_tier_article_capacity(result)
