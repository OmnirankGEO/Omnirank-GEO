"""
社媒专项评分模块 - Social Scope Scorer

5维度评分体系（满分100分）：
- 品牌社媒存在感: 25分 — 品牌相关内容数量/质量/互动
- 竞品活跃度:     20分 — 头部竞品账号数量和质量
- 行业内容生态:   20分 — 行业内容丰富度、增长趋势
- 内容质量:       20分 — 爆款率、互动率、内容专业度
- 账号运营基础:   15分 — 自有账号、更新频率、粉丝规模
"""

import logging

logger = logging.getLogger("social-scope-scorer")

SOCIAL_SCOPE_DIMENSIONS = {
    "brand_social_presence":  {"name": "品牌社媒存在感", "max": 25, "icon": "📱"},
    "competitor_activity":    {"name": "竞品活跃度",     "max": 20, "icon": "🏆"},
    "industry_content_eco":   {"name": "行业内容生态",   "max": 20, "icon": "🔥"},
    "content_quality_score":  {"name": "内容质量",       "max": 20, "icon": "📝"},
    "operation_foundation":   {"name": "账号运营基础",   "max": 15, "icon": "📊"},
}

SOCIAL_SCOPE_MAX_SCORE = sum(d["max"] for d in SOCIAL_SCOPE_DIMENSIONS.values())  # 100


def _score_brand_social_presence(platform_data: dict, brand_content_stats: dict) -> int:
    """
    品牌社媒存在感（满分25分）

    综合品牌内容数量（抖音+小红书）和互动量
    """
    brand_content_stats = brand_content_stats or {}
    has_presence = brand_content_stats.get('has_brand_presence', False)
    douyin_count = brand_content_stats.get('douyin_content_count', 0)
    xhs_count = brand_content_stats.get('xiaohongshu_content_count', 0)
    total_count = douyin_count + xhs_count

    if not has_presence or total_count == 0:
        return 0

    score = 0

    # 内容数量维度（最高15分）
    if total_count >= 50:
        score += 15
    elif total_count >= 30:
        score += 12
    elif total_count >= 15:
        score += 9
    elif total_count >= 5:
        score += 6
    elif total_count >= 1:
        score += 3

    # 双平台覆盖加分（最高5分）
    if douyin_count > 0 and xhs_count > 0:
        score += 5
    elif douyin_count > 0 or xhs_count > 0:
        score += 2

    # 互动数据加分（最高5分）
    douyin_data = platform_data.get("douyin", {})
    xhs_data = platform_data.get("xiaohongshu", {})
    total_engagement = 0

    if isinstance(douyin_data, dict) and not douyin_data.get("skipped"):
        videos = douyin_data.get("videos", []) or douyin_data.get("top20", [])
        for v in videos:
            stats = v.get("stats", {})
            total_engagement += (stats.get("digg", 0) or v.get("like_count", 0))
            total_engagement += (stats.get("share", 0) or v.get("share_count", 0)) * 2

    if isinstance(xhs_data, dict) and not xhs_data.get("skipped"):
        notes = xhs_data.get("notes", []) or xhs_data.get("top20", [])
        for n in notes:
            total_engagement += n.get("like_count", 0)
            total_engagement += n.get("collect_count", 0) * 2

    if total_engagement >= 10000:
        score += 5
    elif total_engagement >= 3000:
        score += 3
    elif total_engagement >= 500:
        score += 1

    return min(25, score)


def _score_competitor_activity(competitor_data: dict, platform_data: dict) -> int:
    """
    竞品活跃度（满分20分）

    头部竞品账号数量、内容量、互动表现
    """
    competitor_data = competitor_data or {}
    score = 0

    # 从竞品分析数据中提取
    competitors = competitor_data.get("competitors", [])
    if not competitors:
        # 尝试从 platform_data 的搜索结果中推断竞品数量
        douyin_data = (platform_data or {}).get("douyin", {})
        xhs_data = (platform_data or {}).get("xiaohongshu", {})

        douyin_count = 0
        xhs_count = 0
        if isinstance(douyin_data, dict) and not douyin_data.get("skipped"):
            douyin_count = douyin_data.get("video_count", 0) or len(douyin_data.get("videos", []))
        if isinstance(xhs_data, dict) and not xhs_data.get("skipped"):
            xhs_count = xhs_data.get("note_count", 0) or len(xhs_data.get("notes", []))

        total_industry_content = douyin_count + xhs_count
        # 行业内容越多说明竞品越活跃
        if total_industry_content >= 100:
            score = 16
        elif total_industry_content >= 50:
            score = 12
        elif total_industry_content >= 20:
            score = 8
        elif total_industry_content >= 5:
            score = 4
        return min(20, score)

    # 有明确竞品列表
    num_competitors = len(competitors)
    if num_competitors >= 10:
        score += 10
    elif num_competitors >= 5:
        score += 7
    elif num_competitors >= 3:
        score += 5
    elif num_competitors >= 1:
        score += 3

    # 竞品平均内容质量
    total_comp_engagement = 0
    comp_content_count = 0
    for comp in competitors:
        comp_content_count += comp.get("content_count", 0)
        total_comp_engagement += comp.get("total_engagement", 0)

    if comp_content_count >= 100:
        score += 6
    elif comp_content_count >= 30:
        score += 4
    elif comp_content_count >= 10:
        score += 2

    if total_comp_engagement >= 50000:
        score += 4
    elif total_comp_engagement >= 10000:
        score += 2

    return min(20, score)


def _score_industry_content_eco(platform_data: dict) -> int:
    """
    行业内容生态（满分20分）

    行业内容丰富度：搜索到的行业内容总量
    """
    platform_data = platform_data or {}
    score = 0

    douyin_data = platform_data.get("douyin", {})
    xhs_data = platform_data.get("xiaohongshu", {})

    douyin_count = 0
    xhs_count = 0

    if isinstance(douyin_data, dict) and not douyin_data.get("skipped"):
        douyin_count = douyin_data.get("video_count", 0) or len(douyin_data.get("videos", []))

    if isinstance(xhs_data, dict) and not xhs_data.get("skipped"):
        xhs_count = xhs_data.get("note_count", 0) or len(xhs_data.get("notes", []))

    total = douyin_count + xhs_count

    # 行业内容总量评分
    if total >= 200:
        score += 14
    elif total >= 100:
        score += 11
    elif total >= 50:
        score += 8
    elif total >= 20:
        score += 5
    elif total >= 5:
        score += 2

    # 双平台活跃度加分
    if douyin_count > 0 and xhs_count > 0:
        score += 4
    elif douyin_count > 0 or xhs_count > 0:
        score += 2

    # 热门内容质量（检查是否有高互动内容存在）
    has_viral = False
    if isinstance(douyin_data, dict):
        for v in (douyin_data.get("videos", []) or douyin_data.get("top20", []))[:5]:
            stats = v.get("stats", {})
            likes = stats.get("digg", 0) or v.get("like_count", 0)
            if likes >= 10000:
                has_viral = True
                break

    if not has_viral and isinstance(xhs_data, dict):
        for n in (xhs_data.get("notes", []) or xhs_data.get("top20", []))[:5]:
            if n.get("like_count", 0) >= 5000:
                has_viral = True
                break

    if has_viral:
        score += 2

    return min(20, score)


def _score_content_quality(platform_data: dict, brand_content_stats: dict) -> int:
    """
    内容质量（满分20分）

    品牌内容互动率、爆款率、内容专业度
    """
    brand_content_stats = brand_content_stats or {}
    has_presence = brand_content_stats.get('has_brand_presence', False)
    total_content = brand_content_stats.get('total_content_count', 0)

    if not has_presence or total_content == 0:
        return 0

    score = 0
    total_engagement = 0
    content_count = 0
    viral_count = 0

    douyin_data = (platform_data or {}).get("douyin", {})
    xhs_data = (platform_data or {}).get("xiaohongshu", {})
    brand_douyin = brand_content_stats.get('douyin_content_count', 0)
    brand_xhs = brand_content_stats.get('xiaohongshu_content_count', 0)

    if isinstance(douyin_data, dict) and brand_douyin > 0:
        videos = douyin_data.get("videos", []) or douyin_data.get("top20", [])
        for v in videos:
            stats = v.get("stats", {})
            likes = stats.get("digg", 0) or v.get("like_count", 0)
            shares = stats.get("share", 0) or v.get("share_count", 0)
            total_engagement += likes + shares * 2
            content_count += 1
            if likes >= 1000:
                viral_count += 1

    if isinstance(xhs_data, dict) and brand_xhs > 0:
        notes = xhs_data.get("notes", []) or xhs_data.get("top20", [])
        for n in notes:
            likes = n.get("like_count", 0)
            collects = n.get("collect_count", 0)
            total_engagement += likes + collects * 2
            content_count += 1
            if likes >= 500:
                viral_count += 1

    if content_count == 0:
        return 0

    avg_engagement = total_engagement / content_count

    # 平均互动量评分（最高10分）
    if avg_engagement >= 1000:
        score += 10
    elif avg_engagement >= 500:
        score += 8
    elif avg_engagement >= 200:
        score += 6
    elif avg_engagement >= 50:
        score += 4
    elif avg_engagement > 0:
        score += 2

    # 爆款率评分（最高6分）
    viral_rate = viral_count / content_count
    if viral_rate >= 0.3:
        score += 6
    elif viral_rate >= 0.15:
        score += 4
    elif viral_rate >= 0.05:
        score += 2

    # 内容数量加分（最高4分）
    if total_content >= 30:
        score += 4
    elif total_content >= 10:
        score += 2

    return min(20, score)


def _score_operation_foundation(brand_content_stats: dict) -> int:
    """
    账号运营基础（满分15分）

    自有账号数、更新频率、粉丝规模
    """
    brand_content_stats = brand_content_stats or {}
    has_presence = brand_content_stats.get('has_brand_presence', False)

    if not has_presence:
        return 0

    score = 0
    douyin_count = brand_content_stats.get('douyin_content_count', 0)
    xhs_count = brand_content_stats.get('xiaohongshu_content_count', 0)
    total_content = brand_content_stats.get('total_content_count', 0)

    # 自有账号数（最高6分）
    platforms_active = 0
    if douyin_count > 0:
        platforms_active += 1
    if xhs_count > 0:
        platforms_active += 1

    if platforms_active >= 2:
        score += 6
    elif platforms_active == 1:
        score += 3

    # 内容更新频率推断（最高5分）
    # 内容数量越多说明更新越频繁
    if total_content >= 50:
        score += 5
    elif total_content >= 20:
        score += 4
    elif total_content >= 10:
        score += 3
    elif total_content >= 5:
        score += 2
    elif total_content >= 1:
        score += 1

    # 粉丝规模（最高4分）— 从品牌统计中推断
    followers = brand_content_stats.get('total_followers', 0)
    if followers >= 100000:
        score += 4
    elif followers >= 10000:
        score += 3
    elif followers >= 1000:
        score += 2
    elif followers > 0:
        score += 1

    return min(15, score)


def get_level_from_percentage(percentage: float) -> str:
    """根据百分比返回等级"""
    if percentage >= 80:
        return "优秀"
    elif percentage >= 60:
        return "良好"
    elif percentage >= 40:
        return "中等"
    elif percentage >= 20:
        return "待提升"
    elif percentage > 0:
        return "起步"
    return "空白"


def calculate_social_scope_score(
    platform_data: dict = None,
    brand_content_stats: dict = None,
    competitor_data: dict = None,
) -> dict:
    """
    社媒专项评分主函数

    返回格式：
    {
        "total_score": 55,
        "max_score": 100,
        "level": "中等",
        "dimension_scores": {...},
        "dimension_details": {...},
        "is_social_scope": True,
        "scoring_version": "social_v1.0"
    }
    """
    platform_data = platform_data or {}
    brand_content_stats = brand_content_stats or {}
    competitor_data = competitor_data or {}

    presence = _score_brand_social_presence(platform_data, brand_content_stats)
    competitor = _score_competitor_activity(competitor_data, platform_data)
    industry = _score_industry_content_eco(platform_data)
    quality = _score_content_quality(platform_data, brand_content_stats)
    operation = _score_operation_foundation(brand_content_stats)

    dimension_scores = {
        "brand_social_presence": presence,
        "competitor_activity": competitor,
        "industry_content_eco": industry,
        "content_quality_score": quality,
        "operation_foundation": operation,
    }

    total_score = sum(dimension_scores.values())
    percentage = (total_score / SOCIAL_SCOPE_MAX_SCORE) * 100
    level = get_level_from_percentage(percentage)

    dimension_details = {}
    for key, config in SOCIAL_SCOPE_DIMENSIONS.items():
        score = dimension_scores[key]
        dimension_details[key] = {
            "name": config["name"],
            "icon": config["icon"],
            "score": score,
            "max_score": config["max"],
            "percentage": round(score / config["max"] * 100, 1) if config["max"] > 0 else 0,
        }

    result = {
        "total_score": total_score,
        "max_score": SOCIAL_SCOPE_MAX_SCORE,
        "level": level,
        "dimension_scores": dimension_scores,
        "dimension_details": dimension_details,
        "is_social_scope": True,
        "scoring_version": "social_v1.0",
    }

    logger.info(
        f"[社媒评分] 维度明细: 存在感={presence}/25, 竞品={competitor}/20, "
        f"行业={industry}/20, 质量={quality}/20, 运营={operation}/15"
    )
    logger.info(f"[社媒评分] 总分: {total_score}/{SOCIAL_SCOPE_MAX_SCORE} ({level})")

    return result
