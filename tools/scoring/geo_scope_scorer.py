"""
GEO专项评分模块 - GEO Scope Scorer

5维度评分体系（满分100分）：
- AI引擎推荐率: 30分 — 场景词+地区词测试推荐次数
- 网页内容资产: 25分 — 搜索结果数量+权威媒体引用
- 权威背书:     20分 — 百科/媒体/认证/行业奖项
- 结构化内容:   15分 — FAQ/白皮书/知识图谱完整性
- 品牌基础:     10分 — 品牌词占位+信息准确度
"""

import logging

logger = logging.getLogger("geo-scope-scorer")

GEO_SCOPE_DIMENSIONS = {
    "ai_recommendation_score": {"name": "AI引擎推荐率", "max": 30, "icon": "🤖"},
    "web_content_score":       {"name": "网页内容资产", "max": 25, "icon": "🌐"},
    "authority_score":         {"name": "权威背书",     "max": 20, "icon": "🏛️"},
    "structured_content_score":{"name": "结构化内容",   "max": 15, "icon": "💡"},
    "brand_foundation_score":  {"name": "品牌基础",     "max": 10, "icon": "🏷️"},
}

GEO_SCOPE_MAX_SCORE = sum(d["max"] for d in GEO_SCOPE_DIMENSIONS.values())  # 100


def _score_ai_recommendation(ai_visibility_data: dict) -> int:
    """
    AI引擎推荐率（满分30分）

    基于问题价值分层动态评分：
    - 场景词/超一级词（40%权重 = 12分）
    - 地区+行业词（40%权重 = 12分）
    - 品牌直查（20%权重 = 6分）
    交叉校验：分层评分不低于整体检测率50%
    """
    if not ai_visibility_data or not isinstance(ai_visibility_data, dict):
        return 0

    dimension_stats = ai_visibility_data.get("dimension_stats", {})
    detected_count = ai_visibility_data.get("detected_count", 0)
    total_tests = ai_visibility_data.get("total_tests", 1)

    overall_rate = detected_count / max(total_tests, 1) if total_tests > 0 else 0
    baseline_score = round(30 * overall_rate)

    if dimension_stats:
        # 场景词/超一级词 (40% * 30 = 12分)
        super_stats = dimension_stats.get("super_tier1", {})
        super_total = super_stats.get("total", 0)
        super_detected = super_stats.get("detected", 0)
        super_rate = super_detected / max(super_total, 1) if super_total > 0 else 0
        scene_score = round(12 * super_rate)

        # 地区+行业词 (40% * 30 = 12分)
        regional_stats = dimension_stats.get("regional_industry", {})
        regional_total = regional_stats.get("total", 0)
        regional_detected = regional_stats.get("detected", 0)
        regional_rate = regional_detected / max(regional_total, 1) if regional_total > 0 else 0
        regional_score = round(12 * regional_rate)

        # 品牌直查 (20% * 30 = 6分)
        brand_stats = dimension_stats.get("brand_awareness", {})
        brand_total = brand_stats.get("total", 0)
        brand_detected = brand_stats.get("detected", 0)
        brand_rate = brand_detected / max(brand_total, 1) if brand_total > 0 else 0
        brand_score = round(6 * brand_rate)

        dimension_score = scene_score + regional_score + brand_score
        return max(dimension_score, baseline_score // 2)
    else:
        return baseline_score


def _score_web_content(web_search_data: dict) -> int:
    """
    网页内容资产（满分25分）

    基于品牌直接引用数量评分(只有明确提及品牌的才算)。

    CTO-B 2026-04-26 W1 修 G2:删除 result_count 兜底。
      旧逻辑:brand_direct_count==0 时 fallback 到 result_count → 任意搜索结果数都被算成
              "网页内容资产"分数,品牌 0 提及也能拿满分,虚高严重。
      新逻辑:严格用 brand_direct_count(品牌直接引用数);0 → 0 分,与"无证据无结论"原则一致。
              result_count 仅作为上下文展示用(报告里说明"搜索结果 N 条但 0 条直引品牌")。
    """
    if not web_search_data or not isinstance(web_search_data, dict):
        return 0

    brand_direct_count = web_search_data.get("brand_direct_count", 0) or 0
    # 严格用品牌直引;无品牌直引 = 0 分(此前 fallback 到 result_count 是 G2 虚高 bug)
    citation_count = brand_direct_count

    if citation_count >= 20:
        return 25
    elif citation_count >= 15:
        return 21
    elif citation_count >= 10:
        return 17
    elif citation_count >= 5:
        return 13
    elif citation_count >= 3:
        return 9
    elif citation_count >= 1:
        return 5
    return 0


def _score_authority(web_search_data: dict) -> int:
    """
    权威背书（满分20分）

    统计品牌直接引用中的权威来源数量
    """
    if not web_search_data or not isinstance(web_search_data, dict):
        return 0

    brand_direct_citations = web_search_data.get("brand_direct_citations", [])
    authority_domains = [
        '36kr', '虎嗅', 'baike.baidu', '知乎', '新浪', 'sohu',
        'gov.cn', 'edu.cn', 'qq.com', '澎湃', '界面', '百科',
        '央视', '人民网', '新华', 'thepaper'
    ]

    brand_authority_count = 0
    for citation in brand_direct_citations:
        url = citation.get('url', '').lower()
        source = citation.get('source', '').lower()
        for domain in authority_domains:
            if domain.lower() in url or domain.lower() in source:
                brand_authority_count += 1
                break

    if brand_authority_count >= 8:
        return 20
    elif brand_authority_count >= 5:
        return 16
    elif brand_authority_count >= 3:
        return 12
    elif brand_authority_count >= 2:
        return 8
    elif brand_authority_count >= 1:
        return 4
    return 0


def _score_structured_content(web_search_data: dict, ai_visibility_data: dict) -> int:
    """
    结构化内容（满分15分）

    评估FAQ覆盖度、长文内容、知识图谱完整性
    基于权威来源+AI引用潜力综合判断
    """
    if not web_search_data:
        web_search_data = {}
    if not ai_visibility_data:
        ai_visibility_data = {}

    score = 0

    # FAQ/问答类内容（从web搜索中检测知乎/百度知道/FAQ页面）
    brand_direct_citations = web_search_data.get("brand_direct_citations", [])
    faq_domains = ['zhihu.com', 'zhidao.baidu', 'faq', '问答', '百度知道']
    faq_count = 0
    for citation in brand_direct_citations:
        url = citation.get('url', '').lower()
        title = citation.get('title', '').lower()
        for domain in faq_domains:
            if domain in url or domain in title:
                faq_count += 1
                break

    if faq_count >= 5:
        score += 8
    elif faq_count >= 3:
        score += 6
    elif faq_count >= 1:
        score += 3

    # 百科词条（结构化知识图谱的标志）
    baike_count = 0
    for citation in brand_direct_citations:
        url = citation.get('url', '').lower()
        if 'baike.baidu' in url or 'baike.sogou' in url or '百科' in citation.get('source', ''):
            baike_count += 1

    if baike_count >= 2:
        score += 5
    elif baike_count >= 1:
        score += 3

    # AI引擎能引用品牌信息 = 结构化做得好
    engines_detected = ai_visibility_data.get("brand_detected_count", 0)
    if engines_detected >= 3:
        score += 2

    return min(15, score)


def _score_brand_foundation(ai_visibility_data: dict, web_search_data: dict) -> int:
    """
    品牌基础（满分10分）

    品牌词占位 + 信息准确度
    """
    if not ai_visibility_data:
        ai_visibility_data = {}
    if not web_search_data:
        web_search_data = {}

    score = 0

    # 品牌词AI识别率
    engines_detected = ai_visibility_data.get("brand_detected_count", 0)
    total_engines = ai_visibility_data.get("total_engines", 3)
    if engines_detected >= total_engines and total_engines > 0:
        score += 5
    elif engines_detected >= 2:
        score += 3
    elif engines_detected >= 1:
        score += 1

    # 品牌网页搜索占位
    brand_direct_count = web_search_data.get("brand_direct_count", 0)
    if brand_direct_count >= 10:
        score += 5
    elif brand_direct_count >= 5:
        score += 3
    elif brand_direct_count >= 1:
        score += 1

    return min(10, score)


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


def calculate_geo_scope_score(
    ai_visibility_data: dict = None,
    web_search_data: dict = None,
) -> dict:
    """
    GEO专项评分主函数

    返回格式：
    {
        "total_score": 68,
        "max_score": 100,
        "level": "良好",
        "dimension_scores": {...},
        "dimension_details": {...},
        "is_geo_scope": True,
        "scoring_version": "geo_v1.0"
    }
    """
    ai_visibility_data = ai_visibility_data or {}
    web_search_data = web_search_data or {}

    ai_rec = _score_ai_recommendation(ai_visibility_data)
    web = _score_web_content(web_search_data)
    auth = _score_authority(web_search_data)
    struct = _score_structured_content(web_search_data, ai_visibility_data)
    brand = _score_brand_foundation(ai_visibility_data, web_search_data)

    dimension_scores = {
        "ai_recommendation_score": ai_rec,
        "web_content_score": web,
        "authority_score": auth,
        "structured_content_score": struct,
        "brand_foundation_score": brand,
    }

    total_score = sum(dimension_scores.values())
    percentage = (total_score / GEO_SCOPE_MAX_SCORE) * 100
    level = get_level_from_percentage(percentage)

    dimension_details = {}
    for key, config in GEO_SCOPE_DIMENSIONS.items():
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
        "max_score": GEO_SCOPE_MAX_SCORE,
        "level": level,
        "dimension_scores": dimension_scores,
        "dimension_details": dimension_details,
        "is_geo_scope": True,
        "scoring_version": "geo_v1.0",
    }

    logger.info(
        f"[GEO评分] 维度明细: AI推荐率={ai_rec}/30, 网页={web}/25, "
        f"权威={auth}/20, 结构化={struct}/15, 品牌基础={brand}/10"
    )
    logger.info(f"[GEO评分] 总分: {total_score}/{GEO_SCOPE_MAX_SCORE} ({level})")

    return result
