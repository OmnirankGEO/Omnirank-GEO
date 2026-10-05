"""
销售版评分模块 - Sales Lite Scorer

评分规则与技术版完全一致，但只评估3个维度（满分63分）：
- AI引擎可见度: 25分
- 社媒内容资产: 20分  
- 网页内容资产: 18分

剩余37分需完整版诊断：权威背书(15)、结构化内容(12)、内容质量(5)、品牌基础(5)
"""

import logging

logger = logging.getLogger("sales-lite-scorer")

# 销售版评估的维度及满分
SALES_LITE_DIMENSIONS = {
    "ai_visibility_score": {"name": "AI引擎可见度", "max": 25, "icon": "🤖"},
    "social_media_score": {"name": "社媒内容资产", "max": 20, "icon": "📱"},
    "web_content_score": {"name": "网页内容资产", "max": 18, "icon": "🌐"},
}

# 需要完整版的维度
FULL_VERSION_DIMENSIONS = {
    "authority_score": {"name": "权威背书", "max": 15, "icon": "🏛️"},
    "structured_content_score": {"name": "结构化内容", "max": 12, "icon": "💡"},
    "content_quality_score": {"name": "内容质量", "max": 5, "icon": "📝"},
    "brand_foundation_score": {"name": "品牌基础", "max": 5, "icon": "🏷️"},
}

SALES_LITE_MAX_SCORE = sum(d["max"] for d in SALES_LITE_DIMENSIONS.values())  # 63分
FULL_VERSION_MAX_SCORE = sum(d["max"] for d in FULL_VERSION_DIMENSIONS.values())  # 37分


def calculate_ai_visibility_score(ai_visibility_data: dict) -> int:
    """
    AI引擎可见度评分（满分25分）
    
    评分规则（与技术版一致）：
    - 0次检测 → 0分
    - 1-2次 → 5分
    - 3-5次 → 10分
    - 6-10次 → 15分
    - 11-15次 → 20分
    - 16-20次 → 25分
    """
    if not ai_visibility_data or not isinstance(ai_visibility_data, dict):
        return 0
    
    detected_count = ai_visibility_data.get("detected_count", 0)
    
    if detected_count == 0:
        return 0
    elif detected_count <= 2:
        return 5
    elif detected_count <= 5:
        return 10
    elif detected_count <= 10:
        return 15
    elif detected_count <= 15:
        return 20
    else:
        return 25


def calculate_social_media_score(platform_data: dict) -> int:
    """
    社媒内容资产评分（满分20分）
    
    评分规则（与技术版一致）：
    - 0条内容 → 0分
    - 1-2条 → 4分
    - 3-5条 → 8分
    - 6-10条 → 12分
    - 11-20条 → 16分
    - 20+条 → 20分
    """
    if not platform_data or not isinstance(platform_data, dict):
        return 0
    
    # 从brand_identification获取品牌内容数
    brand_id = platform_data.get("brand_identification", {})
    if not isinstance(brand_id, dict):
        brand_id = {}
    
    douyin_count = brand_id.get("douyin_content_count", 0) or 0
    xhs_count = brand_id.get("xiaohongshu_content_count", 0) or 0
    total_count = douyin_count + xhs_count
    
    if total_count == 0:
        return 0
    elif total_count <= 2:
        return 4
    elif total_count <= 5:
        return 8
    elif total_count <= 10:
        return 12
    elif total_count <= 20:
        return 16
    else:
        return 20


def calculate_web_content_score(web_search_data: dict) -> int:
    """
    网页内容资产评分（满分18分）
    
    评分规则（与技术版一致）：
    - 0条引用 → 0分
    - 1-2条 → 4分
    - 3-5条 → 8分
    - 6-10条 → 12分
    - 11-20条 → 15分
    - 20+条 → 18分
    """
    if not web_search_data or not isinstance(web_search_data, dict):
        return 0
    
    # 统计品牌相关引用
    result_count = web_search_data.get("result_count", 0) or 0
    brand_direct = web_search_data.get("brand_direct_count", 0) or 0
    
    # 使用品牌直接引用数作为主要指标
    citation_count = brand_direct if brand_direct > 0 else result_count
    
    if citation_count == 0:
        return 0
    elif citation_count <= 2:
        return 4
    elif citation_count <= 5:
        return 8
    elif citation_count <= 10:
        return 12
    elif citation_count <= 20:
        return 15
    else:
        return 18


def get_level_from_score(score: int, max_score: int = 63) -> str:
    """根据分数返回等级（按百分比）"""
    if max_score == 0:
        return "空白"
    
    percentage = (score / max_score) * 100
    
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
    else:
        return "空白"


def calculate_sales_lite_score(
    ai_visibility_data: dict = None,
    platform_data: dict = None,
    web_search_data: dict = None
) -> dict:
    """
    销售版评分主函数
    
    返回格式：
    {
        "total_score": 45,           # 已评估维度总分（满分63）
        "max_score": 63,             # 已评估维度满分
        "level": "良好",
        "dimension_scores": {
            "ai_visibility_score": 15,
            "social_media_score": 18,
            "web_content_score": 12
        },
        "dimension_details": {...},   # 各维度详情
        "pending_dimensions": {...},  # 未评估维度（需完整版）
        "pending_max_score": 37       # 未评估维度满分
    }
    """
    # 计算各维度分数
    ai_score = calculate_ai_visibility_score(ai_visibility_data or {})
    social_score = calculate_social_media_score(platform_data or {})
    web_score = calculate_web_content_score(web_search_data or {})
    
    total_score = ai_score + social_score + web_score
    level = get_level_from_score(total_score, SALES_LITE_MAX_SCORE)
    
    # 构建维度详情
    dimension_scores = {
        "ai_visibility_score": ai_score,
        "social_media_score": social_score,
        "web_content_score": web_score
    }
    
    dimension_details = {}
    for key, config in SALES_LITE_DIMENSIONS.items():
        score = dimension_scores[key]
        dimension_details[key] = {
            "name": config["name"],
            "icon": config["icon"],
            "score": score,
            "max_score": config["max"],
            "percentage": round(score / config["max"] * 100, 1) if config["max"] > 0 else 0
        }
    
    # 构建未评估维度信息
    pending_dimensions = {}
    for key, config in FULL_VERSION_DIMENSIONS.items():
        pending_dimensions[key] = {
            "name": config["name"],
            "icon": config["icon"],
            "max_score": config["max"],
            "status": "需完整版诊断"
        }
    
    result = {
        "total_score": total_score,
        "max_score": SALES_LITE_MAX_SCORE,
        "level": level,
        "dimension_scores": dimension_scores,
        "dimension_details": dimension_details,
        "pending_dimensions": pending_dimensions,
        "pending_max_score": FULL_VERSION_MAX_SCORE,
        "is_sales_lite": True,
        "scoring_version": "sales_lite_v1.0"
    }
    
    # 详细评分日志
    logger.info(f"[销售版评分] 维度明细: AI可见度={ai_score}/25, 社媒资产={social_score}/20, 网页内容={web_score}/18")
    logger.info(f"[销售版评分] 总分: {total_score}/{SALES_LITE_MAX_SCORE} ({level})")
    
    return result
