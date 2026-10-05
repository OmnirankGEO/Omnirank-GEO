"""
关键词蒸馏 Agent
从用户输入和搜索建议中提炼出真实的用户常用搜索词

功能:
1. 调用搜索推荐 API 获取下拉词
2. LLM 分析关键词质量和相关性
3. 去重、聚合、排序输出优质关键词
"""

import asyncio
import os
import httpx
from typing import List, Dict, Any, Optional, Set

# 使用 TikHub MCP 获取搜索建议
from ..social.tikhub_mcp import (
    douyin_get_search_suggest,
    tikhub_call_tool
)


async def get_douyin_suggestions(keyword: str) -> List[str]:
    """
    获取抖音搜索下拉推荐词
    
    Args:
        keyword: 初始关键词
    
    Returns:
        推荐词列表
    """
    try:
        result = await douyin_get_search_suggest(keyword)
        
        # 解析 MCP 返回
        if "error" in result:
            print(f"  ⚠️ 抖音推荐词获取失败: {result.get('error')}")
            return []
        
        # 提取推荐词
        data = result.get("result", result)
        if data.get("code") == 200:
            suggestions_data = data.get("data", {})
            # 不同版本的返回格式可能不同
            suggestions = []
            
            # 尝试不同的数据结构
            if isinstance(suggestions_data, list):
                for item in suggestions_data:
                    if isinstance(item, str):
                        suggestions.append(item)
                    elif isinstance(item, dict):
                        suggestions.append(item.get("word", item.get("content", "")))
            elif isinstance(suggestions_data, dict):
                sug_list = suggestions_data.get("sug_list", suggestions_data.get("suggestions", []))
                for item in sug_list:
                    if isinstance(item, str):
                        suggestions.append(item)
                    elif isinstance(item, dict):
                        suggestions.append(item.get("word", item.get("content", "")))
            
            return [s for s in suggestions if s]
        
        return []
    except Exception as e:
        print(f"  ⚠️ 抖音推荐词异常: {e}")
        return []


async def get_xhs_suggestions(keyword: str) -> List[str]:
    """
    获取小红书搜索推荐词
    
    Args:
        keyword: 初始关键词
    
    Returns:
        推荐词列表
    """
    try:
        result = await tikhub_call_tool(
            tool_name="xiaohongshu_web_get_search_suggest",
            arguments={"keyword": keyword}
        )
        
        if "error" in result:
            # 尝试备用工具名
            result = await tikhub_call_tool(
                tool_name="xiaohongshu_app_get_search_suggest",
                arguments={"keyword": keyword}
            )
        
        if "error" in result:
            print(f"  ⚠️ 小红书推荐词获取失败: {result.get('error')}")
            return []
        
        data = result.get("result", result)
        if data.get("code") == 200:
            suggestions_data = data.get("data", {})
            suggestions = []
            
            if isinstance(suggestions_data, list):
                for item in suggestions_data:
                    if isinstance(item, str):
                        suggestions.append(item)
                    elif isinstance(item, dict):
                        suggestions.append(item.get("name", item.get("word", "")))
            elif isinstance(suggestions_data, dict):
                sug_list = suggestions_data.get("sug_list", suggestions_data.get("suggests", []))
                for item in sug_list:
                    if isinstance(item, str):
                        suggestions.append(item)
                    elif isinstance(item, dict):
                        suggestions.append(item.get("name", item.get("word", "")))
            
            return [s for s in suggestions if s]
        
        return []
    except Exception as e:
        print(f"  ⚠️ 小红书推荐词异常: {e}")
        return []


async def distill_keywords(
    initial_keywords: List[str],
    brand_name: str = "",
    industry: str = "",
    max_output: int = 15
) -> Dict[str, Any]:
    """
    关键词蒸馏：从初始关键词中提炼出真实的用户常用词
    
    Args:
        initial_keywords: 用户提供的初始关键词列表
        brand_name: 品牌名称
        industry: 行业信息
        max_output: 最大输出关键词数
    
    Returns:
        {
            "distilled_keywords": [...],    # 蒸馏后的关键词
            "suggestions_collected": int,    # 收集的推荐词数量
            "sources": {...}                 # 各平台来源统计
        }
    """
    print(f"  🔍 开始关键词蒸馏，初始词数: {len(initial_keywords)}")
    
    all_suggestions: Set[str] = set()
    sources = {
        "douyin": [],
        "xiaohongshu": [],
        "initial": initial_keywords.copy()
    }
    
    # 1. 收集各平台的搜索建议
    for keyword in initial_keywords[:10]:  # 限制初始词数量
        print(f"    📥 获取推荐词: {keyword}")
        
        # 抖音推荐词
        dy_suggestions = await get_douyin_suggestions(keyword)
        sources["douyin"].extend(dy_suggestions)
        all_suggestions.update(dy_suggestions)
        
        # 小红书推荐词  
        xhs_suggestions = await get_xhs_suggestions(keyword)
        sources["xiaohongshu"].extend(xhs_suggestions)
        all_suggestions.update(xhs_suggestions)
        
        # 避免请求过快
        await asyncio.sleep(0.3)
    
    print(f"    ✅ 共收集 {len(all_suggestions)} 个推荐词")
    
    # 2. 清洗和过滤
    cleaned_keywords = _clean_keywords(
        list(all_suggestions),
        brand_name=brand_name,
        industry=industry
    )
    
    # 3. 排序和去重（按出现频率）
    keyword_scores = _score_keywords(
        cleaned_keywords,
        sources=sources,
        initial_keywords=initial_keywords
    )
    
    # 4. 取 TOP N
    sorted_keywords = sorted(keyword_scores.items(), key=lambda x: x[1], reverse=True)
    distilled = [kw for kw, score in sorted_keywords[:max_output]]
    
    # 确保初始关键词在前面
    final_keywords = []
    for kw in initial_keywords:
        if kw not in final_keywords:
            final_keywords.append(kw)
    for kw in distilled:
        if kw not in final_keywords:
            final_keywords.append(kw)
    
    result = {
        "distilled_keywords": final_keywords[:max_output],
        "suggestions_collected": len(all_suggestions),
        "sources": {
            "douyin_count": len(sources["douyin"]),
            "xiaohongshu_count": len(sources["xiaohongshu"]),
            "initial_count": len(sources["initial"])
        }
    }
    
    print(f"  ✅ 蒸馏完成，输出 {len(result['distilled_keywords'])} 个关键词")
    
    return result


def _clean_keywords(
    keywords: List[str],
    brand_name: str = "",
    industry: str = ""
) -> List[str]:
    """
    清洗关键词：去除无效词、过短词、无关词
    """
    cleaned = []
    
    # 无效词列表
    stop_words = {"的", "了", "是", "在", "有", "和", "与", "或", "如何", "怎么", "什么"}
    
    for kw in keywords:
        if not kw or len(kw) < 2:
            continue
        
        # 去除纯数字
        if kw.isdigit():
            continue
        
        # 去除过短的词
        if len(kw) < 2:
            continue
        
        # 去除停用词
        if kw in stop_words:
            continue
        
        cleaned.append(kw.strip())
    
    return list(set(cleaned))


def _score_keywords(
    keywords: List[str],
    sources: Dict[str, List[str]],
    initial_keywords: List[str]
) -> Dict[str, float]:
    """
    为关键词评分：基于出现频率和来源多样性
    """
    scores = {}
    
    for kw in keywords:
        score = 0.0
        
        # 初始词加分
        if kw in initial_keywords:
            score += 10.0
        
        # 抖音出现次数
        dy_count = sources["douyin"].count(kw)
        score += dy_count * 2.0
        
        # 小红书出现次数
        xhs_count = sources["xiaohongshu"].count(kw)
        score += xhs_count * 2.0
        
        # 跨平台出现加分
        if dy_count > 0 and xhs_count > 0:
            score += 3.0
        
        # 长度适中加分
        if 3 <= len(kw) <= 8:
            score += 1.0
        
        scores[kw] = score
    
    return scores


# 导出
__all__ = [
    "distill_keywords",
    "get_douyin_suggestions",
    "get_xhs_suggestions"
]
