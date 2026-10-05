"""
指定竞品深度分析模块

功能：
1. 自动识别输入格式（昵称 vs sec_user_id/user_id）
2. 获取竞品用户最新20条作品
3. 选择Top3高互动作品进行ASR转写
4. 汇总分析结果供报告使用
"""

import asyncio
import re
from typing import List, Dict, Tuple, Optional

# 导入TikHub MCP API
from tools.social.tikhub_mcp import (
    douyin_get_user_videos,
    douyin_search_users,
    xhs_get_user_notes,
    xhs_search_users
)


def detect_input_type(input_str: str) -> Tuple[str, str]:
    """
    自动识别输入类型
    
    Returns:
        (type, platform)
        - ("sec_user_id", "douyin") - 抖音用户ID
        - ("user_id", "xiaohongshu") - 小红书用户ID  
        - ("nickname", "unknown") - 昵称，需要搜索
    """
    input_str = input_str.strip()
    
    # 抖音 sec_user_id: 以 MS4wLjABAAAA 开头
    if input_str.startswith("MS4wLjABAAAA"):
        return ("sec_user_id", "douyin")
    
    # 小红书 user_id: 24位十六进制
    if len(input_str) == 24 and all(c in '0123456789abcdef' for c in input_str.lower()):
        return ("user_id", "xiaohongshu")
    
    # 否则视为昵称
    return ("nickname", "unknown")


async def resolve_user_id(
    input_str: str,
    preferred_platform: str = None
) -> Dict[str, str]:
    """
    解析用户输入，获取用户ID
    
    Args:
        input_str: 用户输入（可能是ID或昵称）
        preferred_platform: 优先搜索的平台（douyin/xiaohongshu）
        
    Returns:
        {
            "platform": "douyin" | "xiaohongshu",
            "user_id": "实际用户ID",
            "nickname": "用户昵称",
            "resolved_by": "direct" | "search"
        }
    """
    input_type, platform = detect_input_type(input_str)
    
    if input_type == "sec_user_id":
        # 抖音ID，直接使用
        return {
            "platform": "douyin",
            "user_id": input_str,
            "nickname": "",  # 后续从API获取
            "resolved_by": "direct"
        }
    
    elif input_type == "user_id":
        # 小红书ID，直接使用
        return {
            "platform": "xiaohongshu",
            "user_id": input_str,
            "nickname": "",
            "resolved_by": "direct"
        }
    
    else:
        # 昵称，需要搜索
        nickname = input_str
        
        # 并行搜索两个平台
        results = {}
        
        try:
            # 搜索抖音用户
            dy_result = await douyin_search_users(nickname, count=5)
            if dy_result and isinstance(dy_result, list) and len(dy_result) > 0:
                # 找最匹配的用户（昵称完全匹配优先）
                for user in dy_result:
                    user_nickname = user.get("nickname", "")
                    if user_nickname == nickname or nickname in user_nickname:
                        results["douyin"] = {
                            "user_id": user.get("sec_uid") or user.get("sec_user_id", ""),
                            "nickname": user_nickname,
                            "follower_count": user.get("follower_count", 0)
                        }
                        break
        except Exception as e:
            print(f"   ⚠️ 抖音用户搜索失败: {e}")
        
        try:
            # 搜索小红书用户
            xhs_result = await xhs_search_users(nickname)
            if xhs_result and isinstance(xhs_result, list) and len(xhs_result) > 0:
                for user in xhs_result:
                    user_nickname = user.get("nickname", "")
                    if user_nickname == nickname or nickname in user_nickname:
                        results["xiaohongshu"] = {
                            "user_id": user.get("user_id") or user.get("id", ""),
                            "nickname": user_nickname,
                            "follower_count": user.get("fans", 0)
                        }
                        break
        except Exception as e:
            print(f"   ⚠️ 小红书用户搜索失败: {e}")
        
        # 选择结果（优先指定平台，否则选粉丝多的）
        if preferred_platform and preferred_platform in results:
            chosen = results[preferred_platform]
            return {
                "platform": preferred_platform,
                "user_id": chosen["user_id"],
                "nickname": chosen["nickname"],
                "resolved_by": "search"
            }
        elif results:
            # 选粉丝最多的平台
            best_platform = max(results.keys(), key=lambda p: results[p].get("follower_count", 0))
            chosen = results[best_platform]
            return {
                "platform": best_platform,
                "user_id": chosen["user_id"],
                "nickname": chosen["nickname"],
                "resolved_by": "search"
            }
        else:
            return {
                "platform": "unknown",
                "user_id": "",
                "nickname": nickname,
                "resolved_by": "not_found"
            }


async def fetch_user_works(
    platform: str,
    user_id: str,
    count: int = 20
) -> List[Dict]:
    """
    获取用户最新作品
    
    Args:
        platform: "douyin" | "xiaohongshu"
        user_id: 用户ID
        count: 获取数量
        
    Returns:
        作品列表
    """
    works = []
    
    try:
        if platform == "douyin":
            result = await douyin_get_user_videos(user_id, count=count)
            
            # TikHub MCP返回格式: {result: {data: {aweme_list: [...]}}, tool_name, session_id}
            if result and isinstance(result, dict):
                # 解包MCP响应
                inner_result = result.get("result", result)
                if isinstance(inner_result, dict):
                    data = inner_result.get("data", inner_result)
                    if isinstance(data, dict):
                        works = data.get("aweme_list", []) or data.get("videos", [])
                    elif isinstance(data, list):
                        works = data
            elif result and isinstance(result, list):
                works = result
                
        elif platform == "xiaohongshu":
            result = await xhs_get_user_notes(user_id)
            
            # TikHub MCP返回格式: {result: {data: {data: {notes: [...]}}}}
            if result and isinstance(result, dict):
                inner_result = result.get("result", result)
                if isinstance(inner_result, dict):
                    data1 = inner_result.get("data", inner_result)
                    if isinstance(data1, dict):
                        # 尝试两层嵌套 data.data.notes
                        data2 = data1.get("data", data1)
                        if isinstance(data2, dict):
                            works = data2.get("notes", [])
                        elif isinstance(data2, list):
                            works = data2
                        # 降级: 直接从data1取notes
                        if not works:
                            works = data1.get("notes", [])
                    elif isinstance(data1, list):
                        works = data1
            elif result and isinstance(result, list):
                works = result
                
    except Exception as e:
        print(f"   ⚠️ 获取用户作品失败 ({platform}/{user_id}): {e}")
    
    # 确保works是列表
    if not isinstance(works, list):
        print(f"   ⚠️ 作品数据格式异常: {type(works)}")
        return []
    
    return works[:count]


def select_top_works(
    works: List[Dict],
    platform: str,
    top_n: int = 3
) -> List[Dict]:
    """
    选择互动数据最好的Top N作品
    """
    def get_engagement(work: Dict) -> int:
        if platform == "douyin":
            likes = work.get("statistics", {}).get("digg_count", 0) or work.get("digg_count", 0) or 0
            comments = work.get("statistics", {}).get("comment_count", 0) or work.get("comment_count", 0) or 0
            shares = work.get("statistics", {}).get("share_count", 0) or work.get("share_count", 0) or 0
            return likes + comments * 2 + shares * 3
        else:  # xiaohongshu
            likes = work.get("liked_count", 0) or work.get("like_count", 0) or 0
            collects = work.get("collected_count", 0) or work.get("collect_count", 0) or 0
            comments = work.get("comment_count", 0) or 0
            return likes + collects * 2 + comments * 2
    
    sorted_works = sorted(works, key=get_engagement, reverse=True)
    return sorted_works[:top_n]


def extract_work_info(work: Dict, platform: str) -> Dict:
    """
    从作品中提取关键信息
    """
    if platform == "douyin":
        return {
            "title": work.get("desc", "")[:100],
            "video_id": work.get("aweme_id", ""),
            "likes": work.get("statistics", {}).get("digg_count", 0) or work.get("digg_count", 0),
            "comments": work.get("statistics", {}).get("comment_count", 0) or work.get("comment_count", 0),
            "shares": work.get("statistics", {}).get("share_count", 0) or work.get("share_count", 0),
            "create_time": work.get("create_time", ""),
            "video_url": work.get("share_url", "") or work.get("video", {}).get("play_addr", {}).get("url_list", [""])[0] if work.get("video") else "",
            "platform": "douyin"
        }
    else:  # xiaohongshu
        return {
            "title": work.get("title", "") or work.get("display_title", ""),
            "note_id": work.get("note_id", "") or work.get("id", ""),
            "likes": work.get("liked_count", 0) or work.get("like_count", 0),
            "collects": work.get("collected_count", 0) or work.get("collect_count", 0),
            "comments": work.get("comment_count", 0),
            "create_time": work.get("time", "") or work.get("create_time", ""),
            "platform": "xiaohongshu"
        }


async def analyze_specified_competitors(
    competitor_inputs: List[str],
    brand_name: str,
    works_per_user: int = 20,
    top_asr_count: int = 3
) -> Dict:
    """
    分析指定竞品
    
    Args:
        competitor_inputs: 用户输入的竞品列表（可能是ID或昵称）
        brand_name: 客户品牌名
        works_per_user: 每个竞品获取多少作品
        top_asr_count: 选择多少个top作品做ASR
        
    Returns:
        {
            "competitors": [
                {
                    "input": "原始输入",
                    "nickname": "解析后的昵称",
                    "platform": "平台",
                    "works_count": 作品数,
                    "top_works": [Top作品列表],
                    "content_summary": "内容风格总结"
                }
            ],
            "asr_candidates": [需要ASR的视频列表],
            "analysis_summary": "整体分析摘要"
        }
    """
    results = {
        "competitors": [],
        "asr_candidates": [],
        "total_works_analyzed": 0
    }
    
    print(f"   🔍 开始分析 {len(competitor_inputs)} 个指定竞品...")
    
    for input_str in competitor_inputs:
        if not input_str or not input_str.strip():
            continue
            
        print(f"   📍 分析竞品: {input_str[:20]}...")
        
        # 1. 解析用户ID
        resolved = await resolve_user_id(input_str)
        
        if resolved["resolved_by"] == "not_found":
            print(f"      ⚠️ 未找到用户: {input_str}")
            results["competitors"].append({
                "input": input_str,
                "nickname": input_str,
                "platform": "unknown",
                "works_count": 0,
                "error": "用户未找到"
            })
            continue
        
        # 2. 获取用户作品
        works = await fetch_user_works(
            platform=resolved["platform"],
            user_id=resolved["user_id"],
            count=works_per_user
        )
        
        if not works:
            print(f"      ⚠️ 获取作品失败: {input_str}")
            results["competitors"].append({
                "input": input_str,
                "nickname": resolved["nickname"] or input_str,
                "platform": resolved["platform"],
                "user_id": resolved["user_id"],
                "works_count": 0,
                "error": "获取作品失败"
            })
            continue
        
        # 2.5 [NEW] 从作品中提取作者信息
        author_info = {}
        if works and len(works) > 0:
            first_work = works[0]
            if resolved["platform"] == "douyin":
                author = first_work.get("author", {})
                author_info = {
                    "nickname": author.get("nickname", ""),
                    "unique_id": author.get("unique_id", ""),  # 抖音号
                    "signature": author.get("signature", ""),  # 个性签名
                    "enterprise_verify": author.get("enterprise_verify_reason", ""),  # 企业认证
                    "custom_verify": author.get("custom_verify", ""),  # 个人认证
                    "uid": author.get("uid", ""),
                }
            elif resolved["platform"] == "xiaohongshu":
                # 小红书作品中的作者信息
                user = first_work.get("user", {}) or first_work.get("note_user", {})
                author_info = {
                    "nickname": user.get("nickname", "") or user.get("name", ""),
                    "user_id": user.get("user_id", "") or user.get("id", ""),
                    "red_id": user.get("red_id", ""),  # 小红书号
                }
        
        # 使用从作品提取的昵称（优先级高于resolved）
        final_nickname = author_info.get("nickname") or resolved["nickname"] or input_str
        
        # 3. 选择Top作品
        top_works = select_top_works(works, resolved["platform"], top_n=top_asr_count)
        
        # 4. 提取作品信息
        work_infos = [extract_work_info(w, resolved["platform"]) for w in top_works]
        all_work_infos = [extract_work_info(w, resolved["platform"]) for w in works[:10]]  # 保留前10条概览
        
        # 4.5 [NEW] 计算互动统计（去极值：去掉最高、最低各1条后取平均）
        all_likes = sorted([extract_work_info(w, resolved["platform"]).get("likes", 0) for w in works])
        if len(all_likes) > 2:
            trimmed_likes = all_likes[1:-1]  # 去掉最高和最低
            avg_likes = sum(trimmed_likes) // len(trimmed_likes)
        else:
            avg_likes = sum(all_likes) // len(all_likes) if all_likes else 0
        total_likes = sum(all_likes)
        
        # 5. 添加到ASR候选
        for w in work_infos:
            if resolved["platform"] == "douyin" and w.get("video_url"):
                results["asr_candidates"].append({
                    "competitor": final_nickname,
                    "platform": "douyin",
                    "video_url": w["video_url"],
                    "video_id": w["video_id"],
                    "title": w["title"],
                    "likes": w["likes"]
                })
        
        # 6. 记录竞品分析结果（增强版）
        competitor_result = {
            "input": input_str,
            "nickname": final_nickname,
            "platform": resolved["platform"],
            "user_id": resolved["user_id"],
            "works_count": len(works),
            "top_works": work_infos,
            "recent_works": all_work_infos,
            # [NEW] 作者详细信息
            "author_info": author_info,
            "unique_id": author_info.get("unique_id", ""),  # 抖音号/小红书号
            "signature": author_info.get("signature", ""),  # 个性签名
            "enterprise_verify": author_info.get("enterprise_verify", ""),  # 企业认证
            # [NEW] 互动统计
            "total_likes": total_likes,
            "avg_likes": avg_likes,
            "top1_likes": work_infos[0]["likes"] if work_infos else 0,
            "resolved_by": resolved["resolved_by"]
        }
        
        results["competitors"].append(competitor_result)
        results["total_works_analyzed"] += len(works)
        
        print(f"      ✅ 获取 {len(works)} 条作品，Top {len(work_infos)} 已选中")
    
    print(f"   📊 竞品分析完成: {len(results['competitors'])} 个竞品, {results['total_works_analyzed']} 条作品")
    
    return results


# 导出
__all__ = [
    "detect_input_type",
    "resolve_user_id", 
    "fetch_user_works",
    "select_top_works",
    "analyze_specified_competitors"
]
