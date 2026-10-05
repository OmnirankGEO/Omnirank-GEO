"""
竞品对标报告生成模块
生成竞品对比分析报告
"""

from typing import List, Dict, Any
from datetime import datetime


def generate_benchmark_report(
    brand_name: str,
    own_data: dict,
    competitors: List[dict]
) -> dict:
    """
    生成竞品对标分析报告
    
    Args:
        brand_name: 品牌名称
        own_data: 自家数据 {douyin: {...}, xiaohongshu: {...}}
        competitors: 竞品详细数据列表
    
    Returns:
        对标报告数据
    """
    report = {
        "brand": brand_name,
        "generated_at": datetime.now().isoformat(),
        "summary": {},
        "comparison_table": [],
        "gap_analysis": {},
        "recommendations": []
    }
    
    # 1. 计算自家数据统计
    own_stats = _calculate_own_stats(own_data)
    
    # 2. 计算竞品平均值
    competitor_avg = _calculate_competitor_avg(competitors)
    
    # 3. 生成对比表格
    comparison_table = _generate_comparison_table(brand_name, own_stats, competitors)
    
    # 4. 差距分析
    gap_analysis = _analyze_gaps(own_stats, competitor_avg)
    
    # 5. 生成报告总结
    summary = _generate_summary(own_stats, competitor_avg, len(competitors))
    
    # 6. 生成改进建议
    recommendations = _generate_recommendations(gap_analysis)
    
    report["summary"] = summary
    report["comparison_table"] = comparison_table
    report["gap_analysis"] = gap_analysis
    report["recommendations"] = recommendations
    report["own_stats"] = own_stats
    report["competitor_avg"] = competitor_avg
    
    return report


def _calculate_own_stats(own_data: dict) -> dict:
    """计算自家数据统计"""
    stats = {
        "douyin": {
            "videos_collected": 0,
            "total_engagement": 0,
            "avg_engagement": 0
        },
        "xiaohongshu": {
            "notes_collected": 0,
            "total_engagement": 0,
            "avg_engagement": 0
        },
        "total_content": 0,
        "total_engagement": 0
    }
    
    # 抖音
    douyin = own_data.get("douyin", {})
    videos = douyin.get("videos", []) or douyin.get("top20", [])
    if videos:
        total_eng = sum(
            v.get("like_count", 0) + v.get("share_count", 0) * 2
            for v in videos
        )
        stats["douyin"]["videos_collected"] = len(videos)
        stats["douyin"]["total_engagement"] = total_eng
        stats["douyin"]["avg_engagement"] = round(total_eng / len(videos), 1)
    
    # 小红书
    xhs = own_data.get("xiaohongshu", {})
    notes = xhs.get("notes", []) or xhs.get("top20", [])
    if notes:
        total_eng = sum(
            n.get("like_count", 0) + n.get("collect_count", 0) * 2
            for n in notes
        )
        stats["xiaohongshu"]["notes_collected"] = len(notes)
        stats["xiaohongshu"]["total_engagement"] = total_eng
        stats["xiaohongshu"]["avg_engagement"] = round(total_eng / len(notes), 1)
    
    stats["total_content"] = (
        stats["douyin"]["videos_collected"] + 
        stats["xiaohongshu"]["notes_collected"]
    )
    stats["total_engagement"] = (
        stats["douyin"]["total_engagement"] + 
        stats["xiaohongshu"]["total_engagement"]
    )
    
    return stats


def _calculate_competitor_avg(competitors: List[dict]) -> dict:
    """计算竞品平均统计"""
    if not competitors:
        return {
            "avg_posts": 0,
            "avg_engagement": 0,
            "avg_likes": 0,
            "count": 0
        }
    
    total_posts = 0
    total_engagement = 0
    total_likes = 0
    valid_count = 0
    
    for comp in competitors:
        stats = comp.get("stats_summary", {})
        if stats:
            total_posts += stats.get("post_count", 0)
            total_engagement += comp.get("total_engagement", 0)
            total_likes += stats.get("total_likes", 0)
            valid_count += 1
    
    if valid_count == 0:
        valid_count = 1  # 避免除零
    
    return {
        "avg_posts": round(total_posts / valid_count, 1),
        "avg_engagement": round(total_engagement / valid_count, 1),
        "avg_likes": round(total_likes / valid_count, 1),
        "count": len(competitors)
    }


def _generate_comparison_table(
    brand_name: str,
    own_stats: dict,
    competitors: List[dict]
) -> List[dict]:
    """生成对比表格"""
    table = []
    
    # 自家数据行
    table.append({
        "name": f"⭐ {brand_name} (我方)",
        "platform": "多平台",
        "content_count": own_stats.get("total_content", 0),
        "total_engagement": own_stats.get("total_engagement", 0),
        "avg_engagement": round(
            own_stats.get("total_engagement", 0) / 
            max(own_stats.get("total_content", 1), 1), 1
        ),
        "is_own": True
    })
    
    # 竞品数据行
    for comp in competitors:
        stats = comp.get("stats_summary", {})
        profile = comp.get("profile", {}) or {}
        
        table.append({
            "name": comp.get("nickname", "未知"),
            "platform": comp.get("platform", ""),
            "content_count": stats.get("post_count", 0),
            "total_engagement": comp.get("total_engagement", 0),
            "avg_engagement": stats.get("avg_likes", 0),
            "fans": profile.get("fans", "N/A"),
            "priority_score": comp.get("priority_score", 0),
            "is_own": False
        })
    
    # 按互动量排序
    table.sort(key=lambda x: x.get("total_engagement", 0), reverse=True)
    
    return table


def _analyze_gaps(own_stats: dict, competitor_avg: dict) -> dict:
    """分析差距"""
    gaps = {
        "content_gap": 0,
        "engagement_gap": 0,
        "content_gap_percent": 0,
        "engagement_gap_percent": 0,
        "status": "neutral"
    }
    
    own_content = own_stats.get("total_content", 0)
    own_engagement = own_stats.get("total_engagement", 0)
    
    avg_content = competitor_avg.get("avg_posts", 0)
    avg_engagement = competitor_avg.get("avg_engagement", 0)
    
    # 内容差距
    if avg_content > 0:
        gaps["content_gap"] = round(own_content - avg_content, 1)
        gaps["content_gap_percent"] = round(
            (own_content - avg_content) / avg_content * 100, 1
        )
    
    # 互动差距
    if avg_engagement > 0:
        gaps["engagement_gap"] = round(own_engagement - avg_engagement, 1)
        gaps["engagement_gap_percent"] = round(
            (own_engagement - avg_engagement) / avg_engagement * 100, 1
        )
    
    # 判断状态
    if gaps["content_gap"] > 0 and gaps["engagement_gap"] > 0:
        gaps["status"] = "leading"  # 领先
    elif gaps["content_gap"] < 0 and gaps["engagement_gap"] < 0:
        gaps["status"] = "lagging"  # 落后
    else:
        gaps["status"] = "mixed"  # 混合
    
    return gaps


def _generate_summary(own_stats: dict, competitor_avg: dict, comp_count: int) -> dict:
    """生成报告摘要"""
    own_content = own_stats.get("total_content", 0)
    own_engagement = own_stats.get("total_engagement", 0)
    
    avg_content = competitor_avg.get("avg_posts", 0)
    avg_engagement = competitor_avg.get("avg_engagement", 0)
    
    # 生成文字描述
    content_comparison = "持平"
    if own_content > avg_content * 1.2:
        content_comparison = "领先"
    elif own_content < avg_content * 0.8:
        content_comparison = "落后"
    
    engagement_comparison = "持平"
    if own_engagement > avg_engagement * 1.2:
        engagement_comparison = "领先"
    elif own_engagement < avg_engagement * 0.8:
        engagement_comparison = "落后"
    
    return {
        "competitors_analyzed": comp_count,
        "own_content": own_content,
        "own_engagement": own_engagement,
        "avg_competitor_content": avg_content,
        "avg_competitor_engagement": avg_engagement,
        "content_comparison": content_comparison,
        "engagement_comparison": engagement_comparison,
        "overall_position": _determine_position(content_comparison, engagement_comparison)
    }


def _determine_position(content_comp: str, engagement_comp: str) -> str:
    """确定综合位置"""
    if content_comp == "领先" and engagement_comp == "领先":
        return "行业领先"
    elif content_comp == "落后" and engagement_comp == "落后":
        return "需要追赶"
    elif content_comp == "领先" or engagement_comp == "领先":
        return "中上水平"
    elif content_comp == "落后" or engagement_comp == "落后":
        return "有待提升"
    else:
        return "行业中游"


def _generate_recommendations(gap_analysis: dict) -> List[str]:
    """生成改进建议"""
    recommendations = []
    
    status = gap_analysis.get("status", "neutral")
    content_gap = gap_analysis.get("content_gap", 0)
    engagement_gap = gap_analysis.get("engagement_gap", 0)
    
    if content_gap < 0:
        recommendations.append(
            f"📝 内容数量低于竞品平均 {abs(content_gap):.0f} 条，"
            "建议增加发布频率，制定系统的内容日历"
        )
    
    if engagement_gap < 0:
        recommendations.append(
            f"📊 互动量低于竞品平均 {abs(engagement_gap):.0f}，"
            "建议优化内容质量，增加互动引导"
        )
    
    if status == "leading":
        recommendations.append(
            "🏆 目前处于领先位置，建议保持优势并持续创新内容形式"
        )
    elif status == "lagging":
        recommendations.append(
            "⚠️ 与竞品存在明显差距，建议进行系统的 GEO 优化"
        )
    
    if not recommendations:
        recommendations.append(
            "🎯 整体表现良好，可关注行业新趋势保持竞争力"
        )
    
    return recommendations


def format_benchmark_markdown(report: dict) -> str:
    """将对标报告转换为 Markdown 格式"""
    md = []
    
    md.append(f"# {report.get('brand', '品牌')} 竞品对标分析报告\n")
    md.append(f"> 生成时间: {report.get('generated_at', '')[:10]}\n")
    
    # 摘要
    summary = report.get("summary", {})
    md.append("## 📊 分析概要\n")
    md.append(f"- 分析竞品数: **{summary.get('competitors_analyzed', 0)}** 个")
    md.append(f"- 我方内容数: **{summary.get('own_content', 0)}** 条")
    md.append(f"- 综合位置: **{summary.get('overall_position', 'N/A')}**\n")
    
    # 对比表格
    table = report.get("comparison_table", [])
    if table:
        md.append("## 📈 竞品对比\n")
        md.append("| 账号 | 平台 | 内容量 | 总互动 | 平均互动 |")
        md.append("|------|------|--------|--------|----------|")
        for row in table:
            name = row.get("name", "")
            platform = row.get("platform", "")
            content = row.get("content_count", 0)
            total_eng = row.get("total_engagement", 0)
            avg_eng = row.get("avg_engagement", 0)
            md.append(f"| {name} | {platform} | {content} | {total_eng:,} | {avg_eng:,.1f} |")
        md.append("")
    
    # 改进建议
    recommendations = report.get("recommendations", [])
    if recommendations:
        md.append("## 💡 改进建议\n")
        for rec in recommendations:
            md.append(f"- {rec}")
        md.append("")
    
    return "\n".join(md)


# 导出
__all__ = ["generate_benchmark_report", "format_benchmark_markdown"]
