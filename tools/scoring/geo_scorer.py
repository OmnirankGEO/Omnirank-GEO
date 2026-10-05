"""
GEO 评分工具
根据多维度数据计算 GEO 综合评分
v4.0 七维度评分体系
"""

import json
import logging
from typing import Any
from agentscope.tool import ToolResponse

logger = logging.getLogger("GEO-Scorer")

# [B6-2] lowercase 引擎名 → canonical(与 ENGINE_WEIGHTS / _get_engine_weights 键一致)
_ENGINE_CANONICAL = {
    "deepseek": "DeepSeek",
    "yuanbao": "元宝",
    "kimi": "Kimi",
    "doubao": "豆包",
    "qwen": "千问",
    "dashscope": "千问",
}


def _weighted_brand_ownership_shadow(ai_visibility_data: dict) -> float | None:
    """[B6-2] 按引擎权重加权的品牌词占有观测分(0-10 · 与维度5 满分对齐)。

    生产维度5 是"检出引擎数均权"(每个引擎同权),这里改按 geo_engine_weights/ENGINE_WEIGHTS 权重加权:
      加权检出率 = Σ(检出引擎权重) / Σ(全部引擎权重),映射到 10 分。
    只作观测(shadow),不改生产分。无 per-engine results → None。走引擎权重 SSOT(_get_engine_weights)。
    """
    results = (ai_visibility_data or {}).get("results") or []
    if not results:
        return None
    try:
        from services.placement_service import PlacementService, ENGINE_WEIGHTS
        try:
            weights = PlacementService()._get_engine_weights()
        except Exception:
            weights = dict(ENGINE_WEIGHTS)
        total_w = 0.0
        detected_w = 0.0
        for r in results:
            eng = _ENGINE_CANONICAL.get(str(r.get("engine", "")).lower(), r.get("engine", ""))
            w = float(weights.get(eng, 0.1))
            total_w += w
            if r.get("brand_detected"):
                detected_w += w
        if total_w <= 0:
            return None
        return round((detected_w / total_w) * 10.0, 2)
    except Exception as e:
        logger.warning(f"[B6-2] 加权观测分计算失败(跳过): {e}")
        return None


# GEO 7 维度评分配置（满分100分）- v4.0评分体系
SCORING_DIMENSIONS = {
    # 维度1: AI引擎可见度 (25分) - 权重最高
    "ai_engine_score": {
        "weight": 25,
        "desc": "🤖 AI引擎可见度",
        "full_desc": "DeepSeek/Kimi/豆包等AI引擎中的提及和推荐（权重最高，决定AI推荐）"
    },
    # 维度2: 社媒内容资产 (20分)
    "social_media_score": {
        "weight": 20,
        "desc": "📱 社媒内容资产",
        "full_desc": "抖音+小红书品牌内容数量与热门占比"
    },
    # 维度3: 网页内容资产 (18分)
    "web_content_score": {
        "weight": 18,
        "desc": "🌐 网页内容资产",
        "full_desc": "品牌词搜索结果数量、权威媒体引用次数"
    },
    # 维度4: 权威背书 (15分)
    "authority_score": {
        "weight": 15,
        "desc": "🏛️ 权威背书",
        "full_desc": "官方认证、媒体报道、行业奖项等"
    },
    # 维度5: 结构化内容 (12分)
    "structured_content_score": {
        "weight": 12,
        "desc": "💡 结构化内容",
        "full_desc": "FAQ覆盖度、长文内容、知识图谱完整性"
    },
    # 维度6: 内容质量 (5分)
    "content_quality_score": {
        "weight": 5,
        "desc": "📝 内容质量",
        "full_desc": "内容专业度、互动率综合评估"
    },
    # 维度7: 品牌基础 (5分)
    "brand_foundation_score": {
        "weight": 5,
        "desc": "🏷️ 品牌基础",
        "full_desc": "品牌词占位+内容更新频率"
    }
}


def get_coverage_level(score: float) -> tuple[str, str]:
    """根据分数获取覆盖等级"""
    if score >= 80:
        return ("优秀", "品牌在AI搜索中具有较高可见度")
    elif score >= 60:
        return ("良好", "品牌有一定覆盖但仍有提升空间")
    elif score >= 40:
        return ("一般", "品牌覆盖不足，需要系统化布局")
    elif score >= 20:
        return ("待改进", "品牌几乎无覆盖，急需开始建设")
    else:
        return ("空白", "品牌在目标领域完全空白")


async def calculate_geo_score(
    douyin_data: dict = None,
    xiaohongshu_data: dict = None,
    web_search_data: dict = None,
    ai_visibility_data: dict = None,
    brand_name: str = "",
    brand_content_stats: dict = None  # [NEW] 品牌自有内容统计
) -> ToolResponse:
    """
    计算 GEO 综合评分 (8维度 - 对标 Coze)
    
    Args:
        douyin_data (dict): 抖音数据统计
        xiaohongshu_data (dict): 小红书数据统计
        web_search_data (dict): 网页搜索数据
        ai_visibility_data (dict): AI 可见度数据
        brand_name (str): 品牌名称
        brand_content_stats (dict): 品牌自有内容统计(来自BrandAccountIdentifier)
            - has_brand_presence: bool
            - douyin_content_count: int
            - xiaohongshu_content_count: int
            - total_content_count: int
        
    Returns:
        ToolResponse: 包含评分结果的响应
    """
    scores = {}
    
    # ========================================
    # 数据安全检查 - 防止 NoneType 错误
    # ========================================
    if douyin_data is None:
        douyin_data = {}
    if xiaohongshu_data is None:
        xiaohongshu_data = {}
    if web_search_data is None:
        web_search_data = {}
    if ai_visibility_data is None:
        ai_visibility_data = {}
    
    # ========================================
    # 维度1: 网页搜索可见度 (15分)
    # [Phase 11.2] 基于品牌直接引用数量评分（只有明确提及品牌的才算）
    # ========================================
    web_search_score = 0
    if web_search_data:
        # [Critical Fix] 使用brand_direct_count而非result_count
        # brand_direct_count = 明确提及品牌名的引用数量
        # result_count = 所有行业相关引用（包含未提及品牌的）
        brand_direct_count = web_search_data.get("brand_direct_count", 0)
        
        # 核心依据：品牌直接引用数量（不是行业引用）
        if brand_direct_count >= 20:
            web_search_score = 15  # 20+条品牌引用，高曝光
        elif brand_direct_count >= 10:
            web_search_score = 12  # 10条品牌引用，良好曝光
        elif brand_direct_count >= 5:
            web_search_score = 9   # 5条品牌引用，有一定曝光
        elif brand_direct_count >= 3:
            web_search_score = 6   # 3条品牌引用，基础曝光
        elif brand_direct_count >= 2:
            web_search_score = 4   # 2条品牌引用，初步可见
        elif brand_direct_count >= 1:
            web_search_score = 2   # 仅1条品牌引用，几乎不可见
        # 0条品牌引用 = 0分（即使有100条行业引用也不算品牌曝光）
    
    scores["web_search_score"] = web_search_score

    
    # ========================================
    # 维度2: 平台覆盖度 (15分)
    # [FIX] 现在基于品牌自有内容评分，而非行业搜索结果
    # ========================================
    platform_score = 0
    douyin_active = False
    xhs_active = False
    
    # 检查是否有品牌内容统计数据
    if brand_content_stats is None:
        brand_content_stats = {}
    
    has_brand_presence = brand_content_stats.get('has_brand_presence', False)
    brand_douyin_count = brand_content_stats.get('douyin_content_count', 0)
    brand_xhs_count = brand_content_stats.get('xiaohongshu_content_count', 0)
    
    if has_brand_presence:
        # 有品牌账号：基于品牌自有内容评分
        # 抖音评分（最多8分）
        if brand_douyin_count >= 10:
            platform_score += 8
            douyin_active = True
        elif brand_douyin_count >= 5:
            platform_score += 6
            douyin_active = True
        elif brand_douyin_count >= 1:
            platform_score += 3
            douyin_active = True
        
        # 小红书评分（最多7分）
        if brand_xhs_count >= 10:
            platform_score += 7
            xhs_active = True
        elif brand_xhs_count >= 5:
            platform_score += 5
            xhs_active = True
        elif brand_xhs_count >= 1:
            platform_score += 2
            xhs_active = True
    else:
        # 无品牌账号：平台覆盖度为0
        # 但仍检测原始数据中是否有潜在品牌内容（向后兼容）
        if douyin_data:
            video_count = douyin_data.get("video_count", 0) or len(douyin_data.get("videos", []))
            # 标记为行业数据，不纳入品牌评分
            # 仅当明确判定为品牌内容时才给分
        if xiaohongshu_data:
            note_count = xiaohongshu_data.get("note_count", 0) or len(xiaohongshu_data.get("notes", []))
            # 同上
    
    scores["platform_score"] = platform_score
    
    # ========================================
    # 维度3: 内容质量 (15分) - 基于互动数据
    # [FIX] 仅评估品牌自有内容的质量，无品牌内容=0分
    # ========================================
    content_quality_score = 0
    total_engagement = 0
    content_count = 0
    
    # 只有在有品牌存在时才评估内容质量
    if has_brand_presence and brand_content_stats.get('total_content_count', 0) > 0:
        # 抖音互动（仅评估品牌内容，但目前没有细分，暂用全部数据）
        if douyin_data and brand_douyin_count > 0:
            videos = douyin_data.get("videos", []) or douyin_data.get("top20", [])
            for v in videos:
                stats = v.get("stats", {})
                total_engagement += (stats.get("digg", 0) or v.get("like_count", 0))
                total_engagement += (stats.get("share", 0) or v.get("share_count", 0)) * 2
            content_count += brand_douyin_count  # 使用品牌内容数量
        
        # 小红书互动（仅评估品牌内容）
        if xiaohongshu_data and brand_xhs_count > 0:
            notes = xiaohongshu_data.get("notes", []) or xiaohongshu_data.get("top20", [])
            for n in notes:
                total_engagement += n.get("like_count", 0)
                total_engagement += n.get("collect_count", 0) * 2
            content_count += brand_xhs_count  # 使用品牌内容数量
        
        avg_engagement = total_engagement / max(content_count, 1)
        
        # 内容质量评分（基于品牌内容的平均互动）
        if content_count >= 20 and avg_engagement >= 500:
            content_quality_score = 15
        elif content_count >= 10 and avg_engagement >= 200:
            content_quality_score = 12
        elif content_count >= 5 and avg_engagement >= 100:
            content_quality_score = 9
        elif content_count >= 3 and avg_engagement >= 50:
            content_quality_score = 6
        elif content_count >= 1:
            content_quality_score = 3
    # else: 无品牌内容，内容质量得分为0
    
    scores["content_quality_score"] = content_quality_score
    
    # ========================================
    # 维度4: 权威背书 (10分)
    # [Phase 11.2] 只计算明确提及品牌的权威来源
    # ========================================
    authority_score = 0
    if web_search_data:
        # 获取品牌直接引用中的权威来源
        brand_direct_citations = web_search_data.get("brand_direct_citations", [])
        
        # 从品牌直接引用中提取权威来源
        # 权威来源定义：36kr、虎嗅、百度百科、知乎、新浪等
        authority_domains = ['36kr', '虎嗅', 'baike.baidu', '知乎', '新浪', 'sohu', 
                            'gov.cn', 'edu.cn', 'qq.com', '澎湃', '界面']
        brand_authority_count = 0
        
        for citation in brand_direct_citations:
            url = citation.get('url', '').lower()
            source = citation.get('source', '').lower()
            for domain in authority_domains:
                if domain.lower() in url or domain.lower() in source:
                    brand_authority_count += 1
                    break
        
        # 评分逻辑（基于品牌直接引用中的权威来源数量）
        if brand_authority_count >= 5:
            authority_score = 10  # 5+条品牌权威报道
        elif brand_authority_count >= 3:
            authority_score = 8   # 3-4条品牌权威报道
        elif brand_authority_count >= 2:
            authority_score = 5   # 2条品牌权威报道
        elif brand_authority_count >= 1:
            authority_score = 3   # 1条品牌权威报道
        # 0条品牌权威报道 = 0分
    
    scores["authority_score"] = authority_score

    
    # ========================================
    # 维度5: 品牌词占有 (10分) - 基于AI识别品牌词
    # ========================================
    brand_ownership_score = 0
    if ai_visibility_data:
        engines_detected = ai_visibility_data.get("brand_detected_count", 0)
        total_engines = ai_visibility_data.get("total_engines", 3)

        # 品牌词被AI识别的程度
        if engines_detected >= total_engines:
            brand_ownership_score = 10  # 所有AI都识别
        elif engines_detected >= 2:
            brand_ownership_score = 7
        elif engines_detected >= 1:
            brand_ownership_score = 4

    scores["brand_ownership_score"] = brand_ownership_score

    # [B6-2] 引擎加权观测分(shadow · flag 默认关 · 只多一个内部字段,不改生产分/总分/等级/历史报告)。
    # flag 关时 scores 与改前逐字节一致。老板看过新旧分差异分布后,才由 Deploy 决定是否切换生产口径。
    try:
        from writing.feature_switches import is_feature_enabled
        if ai_visibility_data and is_feature_enabled("diagnosis_engine_weight_shadow"):
            _weighted = _weighted_brand_ownership_shadow(ai_visibility_data)
            if _weighted is not None:
                scores["brand_ownership_score_weighted_shadow"] = _weighted
    except Exception:
        pass

    # ========================================
    # 维度6: AI引擎可见度 (20分) - GEO核心指标
    # 基于问题价值分层动态评分:
    #   超一级词(50%) + 地区词(30%) + 品牌词(20%)
    # 增加交叉校验：当整体检测率高但分层评分偏低时，取两者较高值
    # ========================================
    ai_visibility_score = 0
    if ai_visibility_data:
        dimension_stats = ai_visibility_data.get("dimension_stats", {})

        # 先计算整体检测率作为基准
        detected_count = ai_visibility_data.get("detected_count", 0)
        total_tests = ai_visibility_data.get("total_tests", 1)
        overall_rate = detected_count / max(total_tests, 1) if total_tests > 0 else 0
        baseline_score = round(20 * overall_rate)

        if dimension_stats:
            # 超一级词/场景词 (30% × 20 = 6分)
            super_stats = dimension_stats.get("super_tier1", {})
            super_total = super_stats.get("total", 0)
            super_detected = super_stats.get("detected", 0)
            super_rate = super_detected / max(super_total, 1) if super_total > 0 else 0
            high_value_score = round(6 * super_rate)

            # 地区+行业词 (55% × 20 = 11分)
            regional_stats = dimension_stats.get("regional_industry", {})
            regional_total = regional_stats.get("total", 0)
            regional_detected = regional_stats.get("detected", 0)
            regional_rate = regional_detected / max(regional_total, 1) if regional_total > 0 else 0
            medium_value_score = round(11 * regional_rate)

            # 品牌直查 (15% × 20 = 3分)
            brand_stats = dimension_stats.get("brand_awareness", {})
            brand_total = brand_stats.get("total", 0)
            brand_detected = brand_stats.get("detected", 0)
            brand_rate = brand_detected / max(brand_total, 1) if brand_total > 0 else 0
            low_value_score = round(3 * brand_rate)

            dimension_score = high_value_score + medium_value_score + low_value_score

            # 交叉校验：分层评分不应低于整体检测率评分的50%
            # 避免"很多AI搜到了但分数极低"的不合理情况
            ai_visibility_score = max(dimension_score, baseline_score // 2)
        else:
            ai_visibility_score = baseline_score

    scores["ai_visibility_score"] = ai_visibility_score
    
    # ========================================
    # 维度7: AI引用潜力 (10分) - 结构化内容
    # [FIX] 基于权威来源和内容质量，不依赖brand_mentions
    # ========================================
    ai_citation_score = 0
    
    # 评估AI可引用的结构化内容
    # 权威来源（百科、媒体文章）= AI最喜欢的引用源
    if authority_score >= 6:
        # 有3+个权威来源，AI引用潜力高
        ai_citation_score = 7
        if content_quality_score >= 9:
            ai_citation_score += 2  # 高质量社媒内容补充
        if web_search_score >= 12:
            ai_citation_score += 1  # 高网页曝光
    elif authority_score >= 2:
        # 有1-2个权威来源，有基础引用潜力
        ai_citation_score = 3
        if content_quality_score >= 9:
            ai_citation_score += 2
    elif content_quality_score >= 12 and brand_content_stats.get('total_content_count', 0) >= 20:
        # 无权威来源，但有大量高质量社媒内容
        ai_citation_score = 2  # 社媒内容对AI引用帮助有限
    # 否则 = 0分（无结构化文本内容）
    
    scores["ai_citation_score"] = min(10, ai_citation_score)
    
    # ========================================
    # 维度8: 更新频率 (5分)
    # [FIX] 无品牌内容时更新频率为0
    # ========================================
    update_frequency_score = 0
    # 只有在有品牌存在时才评估更新频率
    if has_brand_presence:
        # 基于平台活跃度判断
        if douyin_active and xhs_active:
            update_frequency_score = 5  # 双平台活跃
        elif douyin_active or xhs_active:
            update_frequency_score = 3  # 单平台活跃
        elif brand_content_stats.get('total_content_count', 0) >= 5:
            update_frequency_score = 2  # 有内容但不够活跃
    # else: 无品牌内容，更新频率为0
    
    scores["update_frequency_score"] = update_frequency_score
    
    # ========================================
    # 计算总分 (使用实际计算的维度key)
    # ========================================
    ACTUAL_SCORE_KEYS = [
        "web_search_score", "platform_score", "content_quality_score",
        "authority_score", "brand_ownership_score", "ai_visibility_score",
        "ai_citation_score", "update_frequency_score"
    ]
    total_score = sum(scores.get(k, 0) for k in ACTUAL_SCORE_KEYS)
    level, description = get_coverage_level(total_score)
    
    result = {
        "brand": brand_name,
        "total_score": total_score,
        "level": level,
        "level_description": description,
        "dimension_scores": scores,
        "dimension_details": {
            dim: {
                "score": scores.get(dim, 0),
                "max_score": config["weight"],
                "description": config["desc"],
                "full_description": config.get("full_desc", "")
            }
            for dim, config in SCORING_DIMENSIONS.items()
        },
        "recommendations": generate_recommendations_v2(scores, total_score)
    }
    
    return ToolResponse(
        content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
    )


def generate_recommendations(scores: dict, total_score: float) -> list[str]:
    """根据评分生成改进建议 (旧版，保留兼容性)"""
    recommendations = []
    
    if scores.get("platform_score", 0) < 8:
        recommendations.append("建议增加抖音和小红书平台的内容布局")
    
    if scores.get("content_quality_score", 0) < 8:
        recommendations.append("内容质量不足，建议制定系统的内容发布计划")
    
    if scores.get("web_search_score", 0) < 8:
        recommendations.append("网络搜索可见度低，建议投放权威媒体和SEO优化")
    
    if scores.get("authority_score", 0) < 5:
        recommendations.append("缺乏权威来源背书，建议获取行业认证或媒体报道")
    
    if scores.get("ai_visibility_score", 0) < 10:
        recommendations.append("AI 引擎可见度不足，建议进行 GEO 内容优化")
    
    if total_score < 40:
        recommendations.insert(0, "⚠️ 品牌整体覆盖度较低，建议进行全方位的GEO优化")
    
    return recommendations


def generate_recommendations_v2(scores: dict, total_score: float) -> dict:
    """
    根据 8 维度评分生成分级改进建议 (Coze 对齐版)
    """
    high = []     # 高优先级
    medium = []   # 中优先级
    ongoing = []  # 持续优化
    
    # ========== 高优先级判断 ==========
    if scores.get("ai_visibility_score", 0) < 10:
        high.append({
            "issue": "AI引擎可见度较低",
            "impact": "影响品牌在AI搜索中的曝光",
            "action": "在知乎、今日头条发布带品牌词的高质量文章，增加AI索引源"
        })
    
    if scores.get("brand_ownership_score", 0) < 7:
        high.append({
            "issue": "品牌词AI认知不足",
            "impact": "AI难以准确识别品牌信息",
            "action": "创建或完善百科词条，建立AI训练源的权威锚点"
        })
    
    if scores.get("ai_citation_score", 0) < 5:
        high.append({
            "issue": "缺乏结构化文本内容",
            "impact": "AI难以抓取和引用品牌内容",
            "action": "将视频内容转化为知乎问答、FAQ文档等文本格式"
        })
    
    # ========== 中优先级判断 ==========
    if scores.get("web_search_score", 0) < 9:
        medium.append({
            "issue": "网页搜索可见度有待提升",
            "action": "投放行业媒体软文，增加品牌词的搜索引擎覆盖"
        })
    
    if scores.get("platform_score", 0) < 10:
        medium.append({
            "issue": "社媒平台覆盖不均衡",
            "action": "优化抖音/小红书内容布局，提升多平台影响力"
        })
    
    if scores.get("content_quality_score", 0) < 9:
        medium.append({
            "issue": "内容互动表现待优化",
            "action": "优化内容形式，增加干货密度，提升用户互动意愿"
        })
    
    if scores.get("authority_score", 0) < 7:
        medium.append({
            "issue": "权威背书相对薄弱",
            "action": "争取行业认证或权威媒体报道"
        })
    
    # ========== 持续优化判断 ==========
    if scores.get("update_frequency_score", 0) < 4:
        ongoing.append({
            "issue": "内容更新频率需保持",
            "action": "制定内容日历，保持稳定的更新节奏"
        })
    
    if total_score >= 70:
        ongoing.append({
            "issue": "整体表现良好",
            "action": "保持现有优势，持续关注AI搜索趋势变化"
        })

    # 生成总结(元指令 14 SSOT · CTO-15.9 A.2 对齐删硬编码)
    # 之前: 硬编码 4 档 80/60/40 · 与 scoring_levels.py 6 档 LEVEL_META 不一致
    # 现在: 复用 SSOT · 顶部结论 / 正文 / 徽章 / PDF 全同源
    try:
        from tools.scoring.scoring_levels import get_summary
        summary = get_summary(total_score)
    except Exception:
        # fallback · 防 import 失败
        summary = "品牌GEO表现待评估"
    
    return {
        "high_priority": high,
        "medium_priority": medium,
        "ongoing": ongoing,
        "summary": summary,
        "total_issues": len(high) + len(medium) + len(ongoing)
    }


async def generate_geo_report(
    brand_name: str,
    industry: str,
    geo_score_data: dict,
    ai_visibility_data: dict = None,
    platform_data: dict = None,
    company_profile: str = "",
    additional_info: str = ""
) -> ToolResponse:
    """
    生成 GEO 诊断报告 (Template v5.1 - 深度咨询版)
    """
    from datetime import datetime
    import random
    
    # ========================================
    # 1. 数据准备
    # ========================================
    score = geo_score_data.get("total_score", 0)
    level = geo_score_data.get("level", "未知")
    recommendations = geo_score_data.get("recommendations", {})
    
    # 提取各板块数据
    if platform_data is None:
        platform_data = {}
        
    competitor_data = platform_data.get("competitor_analysis", {})
    douyin_raw = platform_data.get("douyin", {})
    xhs_raw = platform_data.get("xiaohongshu", {})
    web_raw = platform_data.get("web_search", {})
    scholar_raw = platform_data.get("scholar_search", {})
    document_raw = platform_data.get("document_search", {})
    industry_analysis_raw = platform_data.get("industry_analysis", {})
    content_insights_raw = platform_data.get("content_insights", {})
    web_search_data = web_raw  # 别名用于引用格式化函数
    
    # 确定等级标识
    if score >= 80:
        level_emoji = "🌟"
        level_desc = "行业领先"
    elif score >= 60:
        level_emoji = "🔷"
        level_desc = "良性发展"
    elif score >= 40:
        level_emoji = "⚠️"
        level_desc = "中等水平"
    elif score >= 20:
        level_emoji = "❌"
        level_desc = "基础薄弱"
    else:
        level_emoji = "⭕"
        level_desc = "尚未建立"
    
    # ========================================
    # 2. 辅助函数：生成表格
    # ========================================
    def _format_competitor_table(comp_data):
        if not comp_data or not comp_data.get("competitors"):
            return "*(暂无竞品数据)*"
        
        table = "| 竞品/账号 | 识别来源 | 粉丝/热度 | 内容类型 | 核心优势 |\n| :--- | :--- | :--- | :--- | :--- |\n"
        for comp in comp_data.get("competitors", [])[:5]:
            name = comp.get("name", "未知")
            platform = comp.get("platform", "未知")
            source = "抖音" if platform == "douyin" else "小红书" if platform == "xiaohongshu" else "搜索发现"
            
            # 互动热度（从total_engagement提取）
            engagement = comp.get("total_engagement", 0)
            if engagement >= 10000:
                fans = f"{engagement//10000}w+热度"
            elif engagement >= 1000:
                fans = f"{engagement}热度"
            else:
                fans = "未知"
            
            # 内容类型推断
            content_types = ["真人实拍+干货", "案例拆解", "方法论输出", "产品展示", "剧情演绎"]
            c_type = content_types[len(name) % len(content_types)] 
            strength = "行业知名度"
            table += f"| **{name}** | {source} | {fans} | {c_type} | {strength} |\n"
        return table

    def _format_content_table(items, platform="douyin"):
        if not items:
            return "*(暂无热门内容)*"
        
        # 按互动量排序，确保显示最优内容
        def get_engagement(item):
            # 优先使用已计算的_total_engagement
            if "_total_engagement" in item:
                return item["_total_engagement"]
            # 否则手动计算
            stats = item.get("stats", {})
            likes = stats.get("digg", 0) or item.get("like_count", 0)
            comments = stats.get("comment", 0) or item.get("comment_count", 0)
            shares = stats.get("share", 0) or item.get("share_count", 0)
            collects = stats.get("collect", 0) or item.get("collect_count", 0)
            return likes + comments * 2 + shares * 3 + collects * 2
        
        # 排序后取前3
        sorted_items = sorted(items, key=get_engagement, reverse=True)[:3]
        
        if platform == "douyin":
            table = "| 排名 | 视频标题 | 点赞 | 分享 | 内容特点 |\n| :--- | :--- | :--- | :--- | :--- |\n"
            for i, item in enumerate(sorted_items, 1):
                # 抖音使用desc字段作为标题
                desc_text = item.get("desc", "") or item.get("title", "")
                title = (desc_text[:35] + "...") if len(desc_text) > 35 else (desc_text or "无标题...")
                
                stats = item.get("stats", {})
                digg = stats.get("digg", 0) or item.get("like_count", 0)
                share = stats.get("share", 0) or item.get("share_count", 0)
                desc = "痛点狙击" if share > 100 else "干货输出"
                table += f"| {i} | {title} | {digg} | {share} | {desc} |\n"
        else: # xhs
            table = "| 排名 | 笔记标题 | 点赞 | 收藏 | 内容特点 |\n| :--- | :--- | :--- | :--- | :--- |\n"
            for i, item in enumerate(sorted_items, 1):
                # 小红书使用title字段，fallback到display_title
                title_text = item.get("title", "") or item.get("display_title", "") or item.get("desc", "")
                title = (title_text[:35] + "...") if len(title_text) > 35 else (title_text or "无标题...")
                
                likes = item.get("like_count", 0) or item.get("liked_count", 0)
                collect = item.get("collect_count", 0) or item.get("collected_count", 0)
                desc = "避坑指南" if "避雷" in title or "坑" in title else "经验分享"
                table += f"| {i} | {title} | {likes} | {collect} | {desc} |\n"
        return table

    def _format_ai_table(ai_data):
        if not ai_data:
            return "*(暂无 AI 测试数据)*"
        
        results = ai_data.get("results", [])
        table = "| AI引擎 | 提及情况 | 评价 |\n| :--- | :--- | :--- |\n"
        
        engine_map = {"deepseek": "DeepSeek", "yuanbao": "元宝", "kimi": "Kimi", "doubao": "豆包", "dashscope": "千问"}
        
        for r in results:
            engine = engine_map.get(r.get("engine", ""), r.get("engine", ""))
            detected = r.get("brand_detected", False)
            mentions = r.get("mentions", [])
            count = len(mentions)
            
            if not detected:
                status = "❌ 未识别"
                comment = "完全未提及品牌，需建立基础语料"
            elif count > 2:
                status = "✅ 强关联"
                comment = "品牌与行业词绑定较好"
            else:
                status = "⚠️ 弱关联"
                comment = "仅在特定语境下出现"
                
            table += f"| {engine} | {status} ({count}次) | {comment} |\n"
        return table

    def _format_citations_table(web_search_data):
        """格式化引用来源表格"""
        if not web_search_data:
            return "*(暂无搜索数据)*"
        
        citations = web_search_data.get("citations", [])
        authority_sources = web_search_data.get("authority_sources", [])
        
        if not citations:
            return "*(暂无引用数据)*"
        
        # 权威来源表格
        authority_citations = [c for c in citations if c.get("is_authority", False)]
        
        if not authority_citations:
            return f"> 共 {len(citations)} 条搜索结果，未发现权威媒体来源"
        
        table = "| 来源 | 标题 | 链接 |\n| :--- | :--- | :--- |\n"
        
        for citation in authority_citations[:10]:  # 最多显示10条
            source = citation.get("source", "未知")
            title = citation.get("title", "无标题")
            # 截断过长标题
            if len(title) > 40:
                title = title[:40] + "..."
            url = citation.get("url", "")
            
            # 格式化来源名称
            source_display = source.replace(".com", "").replace(".cn", "")
            if "zhihu" in source.lower():
                source_display = "知乎"
            elif "36kr" in source.lower():
                source_display = "36氪"
            elif "baike.baidu" in source.lower():
                source_display = "百度百科"
            elif "huxiu" in source.lower():
                source_display = "虎嗅"
            elif "weixin" in source.lower() or "mp.weixin" in source.lower():
                source_display = "微信公众号"
            elif "sina" in source.lower():
                source_display = "新浪"
            elif "toutiao" in source.lower():
                source_display = "今日头条"
            elif "thepaper" in source.lower():
                source_display = "澎湃新闻"
            elif "gov.cn" in source.lower():
                source_display = "政府网站"
            
            table += f"| {source_display} | {title} | [查看原文]({url}) |\n"
        
        # 添加摘要
        summary = f"\n> **共发现 {len(authority_citations)} 个权威来源**，可作为品牌宣传素材的可信引用"
        
        return table + summary

    def _format_scholar_table(scholar_data):
        """格式化学术论文表格 (Phase 10: 含智能解读)"""
        if not scholar_data:
            return "*(暂无学术搜索数据)*"
        
        citations = scholar_data.get("citations", [])
        academic_analysis = scholar_data.get("academic_analysis", {})
        
        if not citations:
            return "*(未找到相关学术论文)*"
        
        output = ""
        
        # [Phase 10] 显示学术洞察总结
        insight_summary = academic_analysis.get("insight_summary", "")
        if insight_summary and insight_summary != "暂无与业务相关的学术研究":
            output += f"**学术洞察**: {insight_summary}\n\n"
        
        # [Phase 10] 显示引用格式的观点
        relevant_papers = academic_analysis.get("relevant_papers", [])
        if relevant_papers:
            output += "**研究引用**:\n\n"
            for paper in relevant_papers[:3]:  # 最多显示3个引用
                citation_text = paper.get("citation_text", "")
                title = paper.get("title", "")[:30]
                link = paper.get("link", "")
                if citation_text:
                    output += f"> {citation_text}\n"
                    output += f"> — 《{title}...》"
                    if link:
                        output += f" [查看原文]({link})"
                    output += "\n\n"
        
        # 表格显示
        output += "**论文列表**:\n\n"
        table = "| 论文标题 | 作者 | 发表日期 | 链接 |\n| :--- | :--- | :--- | :--- |\n"
        
        for citation in citations[:8]:  # 最多显示8条
            title = citation.get("title", "无标题")
            if len(title) > 35:
                title = title[:35] + "..."
            authors = citation.get("authors", [])
            author_str = ", ".join(authors[:2]) if authors else "未知"
            if len(author_str) > 20:
                author_str = author_str[:20] + "..."
            date = citation.get("date", "未知")
            url = citation.get("url", "") or citation.get("link", "")
            
            table += f"| {title} | {author_str} | {date} | [查看]({url}) |\n"
        
        output += table
        
        # 统计信息
        stats = academic_analysis.get("stats", {})
        filtered = stats.get("filtered", 0)
        if filtered > 0:
            output += f"\n> 共找到 {len(citations)} 篇相关论文（已过滤 {filtered} 篇无关研究）"
        else:
            output += f"\n> 共找到 {len(citations)} 篇相关学术论文"
        
        return output

    def _format_document_table(document_data):
        """格式化文库文档表格"""
        if not document_data:
            return "*(暂无文库搜索数据)*"
        
        citations = document_data.get("citations", [])
        if not citations:
            return "*(未找到相关行业报告)*"
        
        table = "| 文档标题 | 来源 | 日期 | 链接 |\n| :--- | :--- | :--- | :--- |\n"
        
        for citation in citations[:8]:  # 最多显示8条
            title = citation.get("title", "无标题")
            if len(title) > 35:
                title = title[:35] + "..."
            source = citation.get("source", "未知")
            date = citation.get("date", "未知")
            url = citation.get("url", "")
            
            table += f"| {title} | {source} | {date} | [查看]({url}) |\n"
        
        return table + f"\n> 共找到 {len(citations)} 份相关行业文档"

    def _format_industry_analysis(industry_data):
        """格式化行业洞察分析"""
        import re
        
        def clean_json_text(text):
            """清理文本中的JSON内容，只保留纯文本"""
            if not text or not isinstance(text, str):
                return str(text) if text else ""
            
            # 如果整个字符串是JSON，尝试提取summary
            if text.strip().startswith('{') or text.strip().startswith('['):
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        return data.get('summary', '') or data.get('text', '') or ''
                    elif isinstance(data, list) and data:
                        summaries = [item.get('summary', '') for item in data if isinstance(item, dict)]
                        return ' '.join(filter(None, summaries))
                except:
                    pass
            
            # 如果文本包含JSON片段，尝试提取纯文本部分
            if '"citations"' in text or '{"' in text or '[{' in text:
                # 找到JSON开始的位置
                match = re.search(r'[{\[]', text)
                if match and match.start() > 0:
                    # 保留JSON之前的纯文本
                    return text[:match.start()].strip()
                else:
                    # 尝试提取引号内的摘要内容
                    summary_match = re.search(r'"summary"\s*:\s*"([^"]+)"', text)
                    if summary_match:
                        return summary_match.group(1)
                    return ""
            
            return text
        
        if not industry_data:
            return "*(暂无行业分析数据)*"
        
        insights = industry_data.get("insights", [])
        top_brands = industry_data.get("top_brands", [])
        industry_trends = industry_data.get("industry_trends", [])
        recommendations = industry_data.get("recommendations", [])
        
        if not insights:
            return "*(未获取到行业洞察)*"
        
        output = ""
        
        # 头部品牌 - 清理可能的JSON
        if top_brands:
            clean_brands = [clean_json_text(b) for b in top_brands if clean_json_text(b)]
            if clean_brands:
                output += f"**行业头部品牌**: {', '.join(clean_brands)}\n\n"
        
        # 行业趋势 - 清理JSON
        if industry_trends:
            output += "**行业趋势动态**:\n"
            for trend in industry_trends[:3]:
                clean_trend = clean_json_text(trend)
                if clean_trend and len(clean_trend) > 10:
                    output += f"- {clean_trend[:150]}{'...' if len(clean_trend) > 150 else ''}\n"
            output += "\n"
        
        # 详细洞察（折叠展示）- 清理JSON
        output += "**AI问答洞察**:\n\n"
        for i, insight in enumerate(insights[:3], 1):
            question = insight.get("question", "")
            answer = insight.get("answer", "")
            
            # 清理answer中的JSON
            clean_answer = clean_json_text(answer)
            if not clean_answer:
                clean_answer = "未获取到有效回答"
            
            # 截断过长答案
            if len(clean_answer) > 300:
                clean_answer = clean_answer[:300] + "..."
            
            output += f"**Q{i}**: {question}\n"
            output += f"> {clean_answer}\n\n"
        
        # GEO优化建议 - 清理JSON
        if recommendations:
            output += "**GEO优化建议**:\n"
            for rec in recommendations:
                clean_rec = clean_json_text(rec)
                if clean_rec:
                    output += f"- {clean_rec}\n"
        
        return output

    def _format_content_insights(insights_data):
        """格式化内容洞察分析"""
        if not insights_data:
            return "*(暂无内容洞察数据)*"
        
        combined = insights_data.get("combined", {})
        if not combined:
            return "*(未获取到内容分析)*"
        
        output = ""
        
        # 热门话题关键词
        keywords = combined.get("industry_keywords", [])[:10]
        if keywords:
            output += "**热门话题词云**:\n"
            keyword_list = []
            for kw in keywords:
                word = kw.get("keyword", "")
                count = kw.get("count", 0)
                keyword_list.append(f"`{word}`({count})")
            output += " | ".join(keyword_list) + "\n\n"
        
        # 爆款内容分析
        viral = combined.get("overall_viral", {})
        if viral:
            avg_eng = viral.get("avg_engagement", 0)
            viral_threshold = viral.get("viral_threshold", 0)
            output += f"**互动基准**: 平均 {avg_eng:.0f} | 爆款阈值 {viral_threshold:.0f}+\n\n"
            
            # 标题模式
            patterns = viral.get("title_patterns", [])
            if patterns:
                output += "**爆款标题模式**:\n"
                for pattern in patterns[:5]:
                    output += f"- {pattern}\n"
                output += "\n"
        
        # 抖音发布时间分析
        dy_insights = insights_data.get("douyin", {})
        if dy_insights:
            publish_time = dy_insights.get("publish_time", {})
            best_slots = publish_time.get("best_time_slots", [])
            best_days = publish_time.get("best_weekdays", [])
            
            if best_slots or best_days:
                output += "**抖音最佳发布时间**:\n"
                if best_slots:
                    slots_str = ", ".join([f"{slot[0]}" for slot in best_slots[:2]])
                    output += f"- 时段: {slots_str}\n"
                if best_days:
                    days_str = ", ".join([f"{day[0]}" for day in best_days[:2]])
                    output += f"- 周几: {days_str}\n"
                output += "\n"
        
        # 小红书发布时间分析
        xhs_insights = insights_data.get("xiaohongshu", {})
        if xhs_insights:
            publish_time = xhs_insights.get("publish_time", {})
            best_slots = publish_time.get("best_time_slots", [])
            best_days = publish_time.get("best_weekdays", [])
            
            if best_slots or best_days:
                output += "**小红书最佳发布时间**:\n"
                if best_slots:
                    slots_str = ", ".join([f"{slot[0]}" for slot in best_slots[:2]])
                    output += f"- 时段: {slots_str}\n"
                if best_days:
                    days_str = ", ".join([f"{day[0]}" for day in best_days[:2]])
                    output += f"- 周几: {days_str}\n"
                output += "\n"
        
        return output if output else "*(分析数据不足)*"

    def _format_action_plan(action_plan_data, ai_visibility_data, industry):
        """格式化优化行动计划 (Phase 10)"""
        
        # 如果有LLM生成的专业计划，直接使用
        if action_plan_data and action_plan_data.get('action_plan_md'):
            return action_plan_data['action_plan_md']
        
        # 降级方案：使用模板生成
        ai_table = _format_ai_table(ai_visibility_data) if ai_visibility_data else ""
        weak_engine = "表现较弱的引擎"
        if "❌" in ai_table:
            # 尝试提取具体引擎名
            if "DeepSeek" in ai_table and "❌" in ai_table.split("DeepSeek")[1].split("\n")[0]:
                weak_engine = "DeepSeek"
            elif "Kimi" in ai_table and "❌" in ai_table.split("Kimi")[1].split("\n")[0]:
                weak_engine = "Kimi"
            elif "豆包" in ai_table:
                weak_engine = "豆包"
        
        return f"""## 📝 四、优化行动计划 (Action Plan)

### 🔴 第一阶段：速赢行动 (Quick Wins)
*   **AI投喂建设**：针对 AI 识别率低的引擎（如 {weak_engine}），发布带品牌词的高质量文章。
*   **知乎/百科布局**：创建或完善百科词条，确保 AI 训练源有权威锚点。

### 🟡 第二阶段：内容升级 (Content Upgrade)
*   **痛点内容重构**：参考竞品热门视频，将内容从"展示型"转向"解决问题型"。
*   **长尾词覆盖**：在视频标题和简介中高频植入行业长尾词（如"{industry}避坑"、"{industry}价格"）。

### 🟢 第三阶段：资产沉淀 (Asset Building)
*   **行业白皮书**：发布年度行业报告，争取被权威媒体引用，提升 Authority Score。
*   **全域口碑**：引导真实客户在社媒晒单评价，丰富第三方信源。"""

    # ========================================
    # 3. 报告内容生成
    # ========================================
    
    # 3.1 核心发现 (根据分数生成)
    core_findings = ""
    if score < 60:
        core_findings += f"**核心问题**：{brand_name} 在AI搜索中的可见度不足（{geo_score_data.get('dimension_scores', {}).get('ai_visibility_score', 0)}/20），主要原因是缺乏结构化的文本语料（如知乎回答、百科词条）。建议优先从内容优化和权威信号建设入手，快速提升AI搜索表现。\n"
    else:
        core_findings += f"**核心优势**：{brand_name} 在社媒平台表现活跃，内容质量较高，但在 AI 深度引用方面仍有提升空间。\n"

    # 安全获取建议
    high_list = recommendations.get('high_priority', [])
    medium_list = recommendations.get('medium_priority', [])
    
    issue_high = high_list[0]['issue'] if high_list else "暂无严重预警"
    issue_med = medium_list[0]['issue'] if medium_list else "各项指标健康"
    action_high = high_list[0]['action'] if high_list else "持续监测AI收录情况"

    report = f"""# 🎯 {brand_name} GEO诊断报告
> **诊断日期**：{datetime.now().strftime('%Y年%m月%d日')}
> **GEO总分**：{score}/100 | **等级**：{level_emoji} {level_desc}
> **诊断系统**：Agentscope GEO System v5.1 (Deep Analysis)

---

> 📊 **数据说明**
>
> 本报告为**GEO快速诊断版**，采用AI智能抽样分析技术：
>
> | 平台 | 抽样范围 | 分析依据 |
> | :--- | :--- | :--- |
> | 抖音 | 热门视频TOP20 + 相关账号 | 搜索排名靠前的内容 |
> | 小红书 | 热门笔记TOP20 | 半年内高互动内容 |
> | AI引擎 | DeepSeek/Kimi/豆包 | 真实行业提问测试 |
>
> **诊断价值**：快速发现问题 → 识别优化方向 → 制定行动计划

---

## 📌 执行摘要

**{brand_name}** 当前GEO综合评分为 **{score}/100**，处于**{level_desc}**水平。
{core_findings}

### 关键发现
| 类型 | 发现 |
| :--- | :--- |
| 🔴 **待改进** | {issue_high} |
| 🟡 **有潜力** | {issue_med} |
| 🟢 **立即行动** | {action_high} |

---

## 📊 一、GEO评分总览

| 维度 | 得分 | 满分 | 评价 | 深度解读 |
| :--- | :---: | :---: | :--- | :--- |
"""
    # 填充评分表
    dim_details = geo_score_data.get("dimension_details", {})
    for dim_key, dim_val in dim_details.items():
        s = dim_val['score']
        max_s = dim_val['max_score']
        desc = dim_val['description']
        
        # 动态生成评价
        ratio = s / max_s
        if ratio >= 0.8: evaluation = "✅ 优秀"
        elif ratio >= 0.6: evaluation = "✅ 良好"
        elif ratio >= 0.4: evaluation = "⚠️ 中等"
        else: evaluation = "❌ 短板"
        
        # 动态生成解读
        interpretation = ""
        if dim_key == "ai_visibility_score":
            if s < 10: interpretation = "行业通用词搜索时未被推荐，错失精准商机"
            else: interpretation = "品牌词已被AI收录，但行业词关联度待提升"
        elif dim_key == "platform_score":
            if s < 10: interpretation = "渠道发展不平衡，存在明显流量洼地"
            else: interpretation = "多平台布局均衡，流量获取能力强"
        elif dim_key == "content_quality_score":
                interpretation = "内容专业度与用户互动表现的综合反映"
        else:
                interpretation = dim_val.get('full_description', '')

        report += f"| {desc} | **{s}** | {max_s} | {evaluation} | {interpretation} |\n"

    report += f"""
---

## 🤖 二、AI引擎可见度详情

> 我们通过向 **DeepSeek、Kimi、豆包** 三大引擎提问真实行业问题，测试了品牌的AI可见度。

{_format_ai_table(ai_visibility_data)}

**AI可见度解读**：
*   **提及率分析**：当前品牌在AI端的提及情况直接反映了"被动检索"的能力。如果提及率低于 30%，说明只有用户明确搜品牌名时才能看到，**大量泛流量正在流失**。
*   **平台差异**：不同AI引擎的收录源不同（如豆包偏好头条系，Kimi偏好长文本）。需针对性补齐短板。

---

## 📚 2.5、权威引用来源 (Citation Sources)

> 以下为品牌相关的权威媒体报道和引用来源，可用于内容创作和宣传素材参考。

{_format_citations_table(web_search_data)}

### 📖 学术论文引用

> 行业相关学术研究，可用于增强内容专业性和权威性。

{_format_scholar_table(scholar_raw)}

### 📄 行业报告与白皮书

> 专业文库中的行业报告，可作为数据支撑和市场分析参考。

{_format_document_table(document_raw)}

### 🤖 AI行业洞察分析

> 基于秘塔AI问答分析行业格局、头部品牌和发展趋势。

{_format_industry_analysis(industry_analysis_raw)}

### 📊 内容洞察分析

> 基于采集的社媒内容数据，分析热门话题、发布规律和爆款特征。

{_format_content_insights(content_insights_raw)}

---

## 🏆 三、竞品对标分析 (Competitor Analysis)

### 3.1 竞品全景
> 基于大数据识别出的行业活跃竞品，排除名称相似误判。

{_format_competitor_table(competitor_data)}

### 3.2 热门内容分析 (Hot Content)

#### 抖音平台 (Douyin)
**抖音内容洞察**：
B2B/专业服务类内容已进入"深水区"。单纯的产品展示已难获客，高赞视频通常具备 **"痛点狙击"** (如解决具体客诉) 或 **"强信任背书"** (如晒真实订单/后台) 的特征。

{_format_content_table(douyin_raw.get("top20", []), "douyin")}

#### 小红书平台 (Xiaohongshu)
**小红书内容洞察**：
小红书用户更倾向于"避坑指南"和"真实经验"。如果搜索结果多为避雷贴或情绪宣泄，说明该赛道存在**巨大的内容错位机会**——发布高质量、客观的行业白皮书或科普贴，极易建立专业壁垒。

{_format_content_table(xhs_raw.get("top20", []), "xiaohongshu")}

---

{_format_action_plan(platform_data.get('action_plan', {}), ai_visibility_data, industry)}

---
> **总结**：GEO 优化不是一蹴而就的。建议优先解决 **AI 可见度** 问题，确保不缺席未来的流量入口，再逐步提升内容质量和权威背书。
"""

    return ToolResponse(
        content=[{"type": "text", "text": report}]
    )
