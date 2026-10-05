"""
竞品数据采集模块
针对识别出的竞品账号，采集其详细数据
"""

import asyncio
import json
import httpx
import os
from typing import List, Dict, Any

from config.tikhub_api_config import (
    TIKHUB_BASE_URL, 
    XHS_ENDPOINTS, 
    DOUYIN_ENDPOINTS,
    SHIPINHAO_ENDPOINTS
)


# TikHub API Key
TIKHUB_API_KEY = os.environ.get("TIKHUB_API_KEY", "")


async def collect_competitor_data(
    competitors: List[dict],
    max_posts: int = 30
) -> List[dict]:
    """
    采集竞品账号的详细数据
    
    Args:
        competitors: 竞品列表 (来自 identify_competitors)
        max_posts: 每个竞品采集的最大作品数
    
    Returns:
        竞品详细数据列表
    """
    results = []
    
    # 并行采集所有竞品数据
    tasks = [
        _collect_single_competitor(comp, max_posts)
        for comp in competitors
    ]
    
    collected = await asyncio.gather(*tasks, return_exceptions=True)
    
    for i, data in enumerate(collected):
        if isinstance(data, Exception):
            print(f"  ⚠️ 采集竞品 {competitors[i].get('nickname')} 失败: {data}")
            # 使用基础数据
            results.append({
                **competitors[i],
                "profile": None,
                "posts": [],
                "collection_error": str(data)
            })
        else:
            results.append(data)
    
    return results


async def _collect_single_competitor(comp: dict, max_posts: int) -> dict:
    """采集单个竞品的详细数据"""
    platform = comp.get("platform", "")
    account_id = comp.get("account_id", "")
    nickname = comp.get("nickname", "")
    
    result = {
        **comp,
        "profile": None,
        "posts": [],
        "stats_summary": {}
    }
    
    if not account_id:
        return result
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {
            "Authorization": f"Bearer {TIKHUB_API_KEY}",
            "Content-Type": "application/json"
        }
        
        if platform == "douyin":
            # 抖音：获取用户作品
            result = await _collect_douyin_user(client, headers, comp, max_posts)
        elif platform == "xiaohongshu":
            # 小红书：获取用户信息和作品
            result = await _collect_xhs_user(client, headers, comp, max_posts)
        elif platform == "wechat_channels":
            # 视频号：获取用户主页
            result = await _collect_shipinhao_user(client, headers, comp, max_posts)
    
    return result


async def _collect_douyin_user(
    client: httpx.AsyncClient, 
    headers: dict, 
    comp: dict,
    max_posts: int
) -> dict:
    """采集抖音用户数据"""
    result = {**comp, "profile": None, "posts": [], "stats_summary": {}}
    account_id = comp.get("account_id", "")
    
    try:
        # 获取用户作品列表 (APP-V3)
        endpoint = DOUYIN_ENDPOINTS.get("fetch_user_post_videos")
        if not endpoint:
            return result
        
        url = f"{TIKHUB_BASE_URL}{endpoint.path}"
        params = {
            "sec_user_id": account_id,
            "max_cursor": 0,
            "count": min(max_posts, 30)
        }
        
        response = await client.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            data = response.json()
            videos = data.get("data", {}).get("aweme_list", []) if data.get("code") == 200 else []
            
            # 提取关键字段
            posts = []
            total_likes = 0
            total_comments = 0
            total_shares = 0
            
            for v in videos[:max_posts]:
                stats = v.get("statistics", {})
                likes = stats.get("digg_count", 0) or 0
                comments = stats.get("comment_count", 0) or 0
                shares = stats.get("share_count", 0) or 0
                
                posts.append({
                    "id": v.get("aweme_id"),
                    "desc": v.get("desc", "")[:100],
                    "create_time": v.get("create_time"),
                    "likes": likes,
                    "comments": comments,
                    "shares": shares
                })
                
                total_likes += likes
                total_comments += comments
                total_shares += shares
            
            result["posts"] = posts
            result["stats_summary"] = {
                "post_count": len(posts),
                "total_likes": total_likes,
                "total_comments": total_comments,
                "total_shares": total_shares,
                "avg_likes": round(total_likes / len(posts), 1) if posts else 0,
                "avg_comments": round(total_comments / len(posts), 1) if posts else 0,
                "avg_shares": round(total_shares / len(posts), 1) if posts else 0
            }
    except Exception as e:
        result["collection_error"] = str(e)
    
    return result


async def _collect_xhs_user(
    client: httpx.AsyncClient, 
    headers: dict, 
    comp: dict,
    max_posts: int
) -> dict:
    """采集小红书用户数据"""
    result = {**comp, "profile": None, "posts": [], "stats_summary": {}}
    user_id = comp.get("account_id", "")
    
    try:
        # 1. 获取用户信息
        info_endpoint = XHS_ENDPOINTS.get("get_user_info")
        if info_endpoint:
            url = f"{TIKHUB_BASE_URL}{info_endpoint.path}"
            params = {"user_id": user_id}
            
            response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200:
                    user_data = data.get("data", {})
                    result["profile"] = {
                        "nickname": user_data.get("nickname"),
                        "desc": user_data.get("desc"),
                        "fans": user_data.get("fansCount", 0),
                        "follows": user_data.get("follows", 0),
                        "notes_count": user_data.get("notes", 0)
                    }
        
        # 2. 获取用户作品
        notes_endpoint = XHS_ENDPOINTS.get("get_user_notes")
        if notes_endpoint:
            url = f"{TIKHUB_BASE_URL}{notes_endpoint.path}"
            params = {"user_id": user_id}
            
            response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200:
                    notes = data.get("data", {}).get("notes", [])
                    
                    posts = []
                    total_likes = 0
                    total_collects = 0
                    
                    for n in notes[:max_posts]:
                        liked = n.get("interactInfo", {}).get("likedCount", 0) or 0
                        collected = n.get("interactInfo", {}).get("collectedCount", 0) or 0
                        
                        posts.append({
                            "id": n.get("noteId"),
                            "title": n.get("displayTitle", "")[:100],
                            "type": n.get("type"),
                            "likes": liked,
                            "collects": collected
                        })
                        
                        total_likes += liked
                        total_collects += collected
                    
                    result["posts"] = posts
                    result["stats_summary"] = {
                        "post_count": len(posts),
                        "total_likes": total_likes,
                        "total_collects": total_collects,
                        "avg_likes": round(total_likes / len(posts), 1) if posts else 0,
                        "avg_collects": round(total_collects / len(posts), 1) if posts else 0
                    }
    except Exception as e:
        result["collection_error"] = str(e)
    
    return result


async def _collect_shipinhao_user(
    client: httpx.AsyncClient, 
    headers: dict, 
    comp: dict,
    max_posts: int
) -> dict:
    """采集视频号用户数据"""
    result = {**comp, "profile": None, "posts": [], "stats_summary": {}}
    username = comp.get("account_id", "")
    
    try:
        # 获取用户主页
        endpoint = SHIPINHAO_ENDPOINTS.get("fetch_home_page")
        if endpoint:
            url = f"{TIKHUB_BASE_URL}{endpoint.path}"
            params = {"username": username}
            
            response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                data = response.json()
                if data.get("code") == 200:
                    home_data = data.get("data", {})
                    # 视频号数据结构可能需要根据实际响应调整
                    result["profile"] = {
                        "nickname": home_data.get("nickname"),
                        "desc": home_data.get("desc"),
                    }
                    
                    videos = home_data.get("objectList", [])[:max_posts]
                    posts = []
                    
                    for v in videos:
                        posts.append({
                            "id": v.get("id"),
                            "desc": v.get("objectNonceId", "")[:100],
                        })
                    
                    result["posts"] = posts
                    result["stats_summary"] = {
                        "post_count": len(posts)
                    }
    except Exception as e:
        result["collection_error"] = str(e)
    
    return result


# 导出
__all__ = ["collect_competitor_data"]
