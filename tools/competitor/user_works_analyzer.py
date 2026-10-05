"""
用户作品列表分析器
用于深度分析候选账号的内容，判断是否为自家账号

功能:
1. 获取用户最近 10-20 条作品 (通过 TikHub MCP)
2. 分析作品标题中品牌名出现频率
3. 筛选最值得 ASR 转录的视频 (每用户最多 3 条)
"""

import os
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

# 使用 TikHub MCP 模块
from ..social.tikhub_mcp import (
    douyin_get_user_videos,
    xhs_get_user_notes
)


async def fetch_user_works_douyin(
    sec_user_id: str,
    count: int = 20
) -> List[dict]:
    """
    获取抖音用户主页作品数据 (通过 TikHub MCP)
    
    Args:
        sec_user_id: 用户的 sec_user_id
        count: 获取数量
    
    Returns:
        作品列表
    """
    try:
        result = await douyin_get_user_videos(sec_user_id, count)
        
        # 处理 MCP 返回格式
        if "error" in result:
            print(f"[ERROR] 抖音作品获取失败: {result.get('error')}")
            return []
        
        # MCP 返回格式: {"result": {"code": 200, "data": {"aweme_list": [...]}}}
        data = result.get("result", result)
        if data.get("code") == 200:
            return data.get("data", {}).get("aweme_list", [])
        
        return []
    except Exception as e:
        print(f"[ERROR] 获取抖音用户作品失败: {e}")
        return []


async def fetch_user_works_xhs(
    user_id: str,
    count: int = 20
) -> List[dict]:
    """
    获取小红书用户作品列表 (通过 TikHub MCP)
    
    Args:
        user_id: 用户ID
        count: 获取数量
    
    Returns:
        笔记列表
    """
    try:
        all_notes = []
        cursor = ""
        
        while len(all_notes) < count:
            result = await xhs_get_user_notes(user_id, cursor)
            
            # 处理 MCP 返回格式
            if "error" in result:
                print(f"[ERROR] 小红书作品获取失败: {result.get('error')}")
                break
            
            # MCP 返回格式: {"result": {"code": 200, "data": {"notes": [...], "cursor": ""}}}
            data = result.get("result", result)
            if data.get("code") == 200:
                inner_data = data.get("data", {})
                notes = inner_data.get("notes", [])
                all_notes.extend(notes)
                
                # 检查是否有更多
                cursor = inner_data.get("cursor", "")
                if not cursor or not notes:
                    break
            else:
                break
        
        return all_notes[:count]
    except Exception as e:
        print(f"[ERROR] 获取小红书用户作品失败: {e}")
        return []


def analyze_brand_frequency(
    works: List[dict],
    brand_name: str,
    platform: str
) -> dict:
    """
    分析作品中品牌名出现频率
    
    返回:
    {
        "total_works": 10,
        "brand_mentions": 5,
        "frequency": 0.5,
        "is_own_account": True/False,
        "reason": "品牌名出现频率 >50%"
    }
    """
    if not works:
        return {
            "total_works": 0,
            "brand_mentions": 0,
            "frequency": 0,
            "is_own_account": False,
            "reason": "无作品数据"
        }
    
    # 生成品牌变体
    brand_variants = [brand_name.lower()]
    for suffix in ["科技", "技术", "公司", "集团"]:
        if brand_name.endswith(suffix):
            brand_variants.append(brand_name[:-len(suffix)].lower())
            break
    
    # 统计品牌提及
    brand_mentions = 0
    
    for work in works:
        if platform == "douyin":
            title = work.get("desc", "").lower()
        else:  # xiaohongshu
            title = work.get("title", work.get("display_title", "")).lower()
        
        for variant in brand_variants:
            if variant in title:
                brand_mentions += 1
                break
    
    frequency = brand_mentions / len(works) if works else 0
    
    # 判断规则
    if frequency > 0.5:
        is_own = True
        reason = f"品牌名出现频率 {frequency:.0%} > 50%，判定为自家账号"
    elif frequency > 0.2:
        is_own = None  # 需要进一步验证
        reason = f"品牌名出现频率 {frequency:.0%}，需 LLM 二次判断"
    else:
        is_own = False
        reason = f"品牌名出现频率 {frequency:.0%} < 20%，判定为竞品/KOL"
    
    return {
        "total_works": len(works),
        "brand_mentions": brand_mentions,
        "frequency": frequency,
        "is_own_account": is_own,
        "reason": reason
    }


async def analyze_candidate_accounts(
    candidates: List[dict],
    brand_name: str,
    works_per_user: int = 15
) -> dict:
    """
    分析候选账号的作品列表，深度验证是否为自家账号
    
    Args:
        candidates: 候选账号列表 [{platform, account_id, nickname, ...}]
        brand_name: 品牌名称
        works_per_user: 每用户获取作品数
    
    Returns:
        {
            "own_accounts": [...],      # 确认的自家账号
            "competitors": [...],       # 确认的竞品
            "uncertain": [...],         # 需要进一步验证的账号
            "asr_candidates": [...]     # 值得 ASR 的视频
        }
    """
    result = {
        "own_accounts": [],
        "competitors": [],
        "uncertain": [],
        "asr_candidates": []
    }
    
    for candidate in candidates:
        platform = candidate.get("platform", "")
        account_id = candidate.get("account_id", "")
        nickname = candidate.get("nickname", "")
        
        print(f"  🔍 分析账号: {nickname} ({platform})")
        
        # 获取用户作品
        if platform == "douyin":
            works = await fetch_user_works_douyin(account_id, works_per_user)
        elif platform == "xiaohongshu":
            works = await fetch_user_works_xhs(account_id, works_per_user)
        else:
            works = []
        
        # 分析品牌频率
        analysis = analyze_brand_frequency(works, brand_name, platform)
        candidate["works_analysis"] = analysis
        candidate["works"] = works
        
        # 分类
        if analysis["is_own_account"] is True:
            result["own_accounts"].append(candidate)
            print(f"    → 自家账号 ({analysis['reason']})")
        elif analysis["is_own_account"] is False:
            result["competitors"].append(candidate)
            print(f"    → 竞品账号 ({analysis['reason']})")
            
            # 收集 ASR 候选视频 (每用户最多 3 条)
            asr_videos = select_top_videos_for_user(works, platform, max_count=3)
            for video in asr_videos:
                video["_from_user"] = nickname
                video["_platform"] = platform
            result["asr_candidates"].extend(asr_videos)
        else:
            result["uncertain"].append(candidate)
            print(f"    → 待验证 ({analysis['reason']})")
    
    return result


def select_top_videos_for_user(
    works: List[dict],
    platform: str,
    max_count: int = 3
) -> List[dict]:
    """
    从用户作品中选择最值得 ASR 的视频
    
    规则:
    - 每用户最多 max_count 条
    - 优先选择互动量高的
    - 过滤掉太短的内容
    """
    if not works:
        return []
    
    # 计算互动得分并排序
    scored_works = []
    
    for work in works:
        if platform == "douyin":
            stats = work.get("statistics", {})
            engagement = (
                stats.get("digg_count", 0) +  # 点赞
                stats.get("comment_count", 0) * 2 +  # 评论权重更高
                stats.get("share_count", 0) * 3  # 分享权重最高
            )
            duration = work.get("video", {}).get("duration", 0) / 1000  # 毫秒转秒
            
            # 过滤太短的视频 (< 15秒)
            if duration < 15:
                continue
            
            # 提取音频 URL
            audio_url = work.get("music", {}).get("play_url", {}).get("url_list", [None])[0]
            if not audio_url:
                video_url = work.get("video", {}).get("play_addr", {}).get("url_list", [None])[0]
                audio_url = video_url  # 备用
            
            work["_audio_url"] = audio_url
            work["_engagement"] = engagement
            
        else:  # xiaohongshu
            engagement = (
                work.get("liked_count", work.get("like_count", 0)) +
                work.get("collected_count", work.get("collect_count", 0)) * 2
            )
            work["_engagement"] = engagement
            # 小红书目前无法直接提取音频
            work["_audio_url"] = None
        
        scored_works.append(work)
    
    # 按互动量排序
    scored_works.sort(key=lambda x: x.get("_engagement", 0), reverse=True)
    
    return scored_works[:max_count]


def select_final_asr_videos(
    asr_candidates: List[dict],
    keyword_count: int = 5
) -> List[dict]:
    """
    最终选择要进行 ASR 的视频
    
    规则:
    - 总数 = min(关键词数 × 2, 20)，最少 5 条
    - 优先选择有音频 URL 的
    - 按互动量排序
    """
    # 计算目标数量
    target_count = max(5, min(keyword_count * 2, 20))
    
    # 过滤有音频 URL 的
    with_audio = [v for v in asr_candidates if v.get("_audio_url")]
    without_audio = [v for v in asr_candidates if not v.get("_audio_url")]
    
    # 优先选择有音频的
    selected = sorted(with_audio, key=lambda x: x.get("_engagement", 0), reverse=True)
    
    # 如果不够，补充无音频的 (小红书可能需要其他方式处理)
    if len(selected) < target_count:
        remaining = sorted(without_audio, key=lambda x: x.get("_engagement", 0), reverse=True)
        selected.extend(remaining[:target_count - len(selected)])
    
    return selected[:target_count]


# 导出
__all__ = [
    "fetch_user_works_douyin",
    "fetch_user_works_xhs", 
    "analyze_brand_frequency",
    "analyze_candidate_accounts",
    "select_top_videos_for_user",
    "select_final_asr_videos"
]
