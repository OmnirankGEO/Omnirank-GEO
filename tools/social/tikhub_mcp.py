"""
TikHub MCP 社媒数据工具
基于 Model Context Protocol (MCP) 的社交媒体数据采集服务

支持平台:
- 抖音 (Douyin): 用户资料、视频搜索、作品列表
- 小红书 (XHS): 用户笔记、搜索
- TikTok: 完整 API
- 更多 700+ 端点

文档: https://docs.tikhub.io/
MCP 端点: https://mcp.tikhub.io
"""

import httpx
import json
import os
from typing import Optional, Dict, Any, List

from tools.tikhub_cost_tracking import tracked_tikhub_get, tracked_tikhub_post


# TikHub MCP 配置
TIKHUB_MCP_CONFIG = {
    "url": "https://mcp.tikhub.io",
    "api_key": os.environ.get("TIKHUB_API_KEY"),
}


async def tikhub_list_tools() -> Dict[str, Any]:
    """
    列出 TikHub MCP 所有可用工具
    
    Returns:
        工具列表及其描述
    """
    api_key = TIKHUB_MCP_CONFIG["api_key"]
    
    if not api_key:
        return {"error": "TIKHUB_API_KEY not configured"}
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await tracked_tikhub_get(
                client,
                f"{TIKHUB_MCP_CONFIG['url']}/tools",
                caller="tikhub_list_tools",
                model="mcp_tool",
                headers={
                    "Authorization": f"Bearer {api_key}"
                }
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "error": f"HTTP {response.status_code}",
                    "detail": response.text[:500]
                }
    except Exception as e:
        return {"error": str(e)}


async def tikhub_call_tool(
    tool_name: str,
    arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """
    调用 TikHub MCP 工具
    
    Args:
        tool_name: 工具名称 (如 'tikhub_web_fetch_user_profile')
        arguments: 工具参数
    
    Returns:
        工具返回结果
    """
    api_key = TIKHUB_MCP_CONFIG["api_key"]
    
    if not api_key:
        return {"error": "TIKHUB_API_KEY not configured"}
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await tracked_tikhub_post(
                client,
                f"{TIKHUB_MCP_CONFIG['url']}/tools/call",
                caller="tikhub_call_tool",
                model="mcp_tool",
                metadata={"tool_name": tool_name},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "tool_name": tool_name,
                    "arguments": arguments
                }
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "error": f"HTTP {response.status_code}",
                    "detail": response.text[:500]
                }
    except Exception as e:
        return {"error": str(e)}


# ==================== 抖音 API 封装 ====================

async def douyin_search_videos(
    keyword: str,
    publish_time: str = "180",  # 180天内
    sort_type: str = "1",       # 最多播放（热门）- 用于对标找数据最好的
    count: int = 20,
    cursor: int = 0,  # 分页游标
    search_id: str = ""  # 分页需要的搜索ID，从第一次响应获取
) -> Dict[str, Any]:
    """
    抖音视频搜索 V2
    
    Args:
        keyword: 搜索关键词
        publish_time: 发布时间筛选 (0=不限, 1=1天内, 7=7天内, 180=半年内)
        sort_type: 排序 (0=综合, 1=最多播放, 2=最新发布) - 默认1获取数据最好的
        count: 返回数量
        cursor: 分页游标，首次请求传0
        search_id: 分页搜索ID，首次请求传空字符串
    
    Returns:
        视频搜索结果列表
    """
    api_key = TIKHUB_MCP_CONFIG["api_key"]
    
    # 先尝试直接HTTP API（更稳定）
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await tracked_tikhub_post(
                client,
                "https://api.tikhub.io/api/v1/douyin/search/fetch_video_search_v2",
                caller="tikhub_douyin_video_search",
                model="search",
                metadata={"count": count, "cursor": cursor},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "keyword": keyword,
                    "cursor": cursor,
                    "count": count,
                    "sort_type": sort_type,
                    "publish_time": publish_time,
                    "filter_duration": "0",
                    "content_type": "0",
                    "search_id": search_id,
                    "backtrace": ""
                }
            )
            
            if response.status_code == 200:
                return response.json()
    except Exception as e:
        pass  # 回退到MCP
    
    # 回退到MCP调用
    return await tikhub_call_tool(
        tool_name="douyin_search_fetch_video_search_v2",
        arguments={
            "keyword": keyword,
            "cursor": cursor,
            "count": count,
            "sort_type": sort_type,
            "publish_time": publish_time,
            "filter_duration": "0",
            "content_type": "0",
            "search_id": search_id,
            "backtrace": ""
        }
    )


async def douyin_get_user_videos(
    sec_user_id: str,
    count: int = 20
) -> Dict[str, Any]:
    """
    获取抖音用户主页作品
    
    Args:
        sec_user_id: 用户的 sec_user_id
        count: 获取数量
    
    Returns:
        用户作品列表
    """
    return await tikhub_call_tool(
        tool_name="douyin_app_fetch_user_post_videos",
        arguments={
            "sec_user_id": sec_user_id,
            "count": count
        }
    )


async def douyin_search_users(
    keyword: str,
    count: int = 10
) -> Dict[str, Any]:
    """
    抖音用户搜索
    
    Args:
        keyword: 搜索关键词
        count: 返回数量
    
    Returns:
        用户搜索结果
    """
    return await tikhub_call_tool(
        tool_name="douyin_search_fetch_user_search",
        arguments={
            "keyword": keyword,
            "cursor": 0,
            "count": count
        }
    )


async def douyin_get_search_suggest(
    keyword: str
) -> Dict[str, Any]:
    """
    获取抖音搜索下拉推荐词 (关键词蒸馏用)
    
    Args:
        keyword: 初始关键词
    
    Returns:
        推荐词列表
    """
    return await tikhub_call_tool(
        tool_name="douyin_search_fetch_search_suggest",
        arguments={
            "keyword": keyword
        }
    )


# ==================== v3.2.2 新增：用户详情 ====================

async def douyin_get_user_profile(
    sec_user_id: str
) -> Dict[str, Any]:
    """
    获取用户详细信息（粉丝数、作品数等）
    
    因为获取视频列表的API返回的author信息不完整，需要单独调用此API获取完整信息。
    
    Args:
        sec_user_id: 用户sec_user_id
    
    Returns:
        用户详细信息，包含：
        - nickname: 昵称
        - follower_count: 粉丝数
        - following_count: 关注数
        - total_favorited: 获赞数
        - aweme_count: 作品数
        - signature: 签名
        - avatar_larger: 头像URL
    """
    # 优先 REST API（MCP端点已不可用 2026-03）
    import httpx
    api_key = TIKHUB_MCP_CONFIG.get("api_key") or os.environ.get("TIKHUB_API_KEY")
    user = {}
    try:
        print(f"[TikHub REST] 获取用户信息: {sec_user_id[:20]}...")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await tracked_tikhub_get(
                client,
                "https://api.tikhub.io/api/v1/douyin/web/handler_user_profile",
                caller="tikhub_user_profile",
                model="user_profile",
                params={"sec_user_id": sec_user_id},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                user = data.get("user", {})
                if user:
                    print(f"[TikHub REST] 用户信息获取成功: {user.get('nickname', 'N/A')}")
    except Exception as e:
        print(f"[TikHub REST] 用户信息REST失败: {e}")

    # REST 失败则回退 MCP
    if not user:
        result = await tikhub_call_tool(
            tool_name="douyin_web_handler_user_profile",
            arguments={"sec_user_id": sec_user_id}
        )
        if "error" in result:
            return result
        mcp_result = result.get("result", {})
        data = mcp_result.get("data", {})
        user = data.get("user", {})
    
    if not user:
        return {"error": "无法获取用户信息"}
    
    # 提取头像URL
    avatar = user.get("avatar_larger", {}) or user.get("avatar_medium", {})
    avatar_url = None
    if avatar:
        urls = avatar.get("url_list", [])
        if urls:
            avatar_url = urls[0]
    
    return {
        "success": True,
        "user": {
            "sec_user_id": sec_user_id,
            "nickname": user.get("nickname"),
            "follower_count": user.get("follower_count", 0),
            "following_count": user.get("following_count", 0),
            "total_favorited": user.get("total_favorited", 0),
            "aweme_count": user.get("aweme_count", 0),
            "signature": user.get("signature", ""),
            "avatar_url": avatar_url,
            "unique_id": user.get("unique_id"),  # 抖音号
            "short_id": user.get("short_id"),
        }
    }


# ==================== v3.2 新增 MCP 封装 ====================

async def douyin_get_user_videos_v3(
    sec_user_id: str,
    count: int = 20,
    sort_type: str = "0",  # 0=最新排序, 1=最热排序
    max_cursor: int = 0
) -> Dict[str, Any]:
    """
    获取用户主页视频 V3（研究中心拆解用户功能使用）
    
    策略：先尝试已知正常的Web版API，失效时回退到SmartMCP智能调用层
    
    参数来源: https://docs.tikhub.io/186826143e0
    MCP工具: douyin_web_fetch_user_post_videos (首选)
    
    Args:
        sec_user_id: 用户sec_user_id
        count: 获取数量，建议不超过20
        sort_type: 排序类型 (0=最新排序-默认, 1=最热排序)
        max_cursor: 翻页游标，首次为0
    
    Returns:
        用户作品列表
    """
    # 1. 优先使用 REST API 直接调用（MCP端点已不可用 2026-03）
    import httpx
    api_key = TIKHUB_MCP_CONFIG.get("api_key") or os.environ.get("TIKHUB_API_KEY")
    REST_ENDPOINTS = [
        "/api/v1/douyin/app/fetch_user_post_videos",
        "/api/v1/douyin/web/fetch_user_post_videos",
    ]
    for ep in REST_ENDPOINTS:
        try:
            print(f"[TikHub REST] 尝试: {ep}")
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await tracked_tikhub_get(
                    client,
                    f"https://api.tikhub.io{ep}",
                    caller="tikhub_user_videos",
                    model="user_videos",
                    metadata={"count": count, "sort_type": sort_type, "max_cursor": max_cursor},
                    params={"sec_user_id": sec_user_id, "count": count, "sort_type": sort_type, "max_cursor": max_cursor},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    aweme_list = data.get("data", {}).get("aweme_list", [])
                    if aweme_list or data.get("code") == 200:
                        print(f"[TikHub REST] 成功: {ep} → {len(aweme_list)} 个视频")
                        return {"result": data}
                    print(f"[TikHub REST] {ep} 返回空数据，尝试下一个")
                else:
                    print(f"[TikHub REST] {ep} 状态码: {resp.status_code}")
        except Exception as e:
            print(f"[TikHub REST] {ep} 异常: {e}")

    # 2. REST 全部失败，尝试 MCP（可能恢复）
    print(f"[TikHub] REST失败，尝试MCP回退...")
    result = await tikhub_call_tool(
        tool_name="douyin_app_fetch_user_post_videos",
        arguments={"sec_user_id": sec_user_id, "count": count, "max_cursor": max_cursor, "sort_type": sort_type}
    )
    if "error" not in result:
        mcp_result = result.get("result", {})
        aweme_list = mcp_result.get("data", {}).get("aweme_list", [])
        if aweme_list:
            return result

    return result if result else {"error": "所有API端点均调用失败（REST+MCP）"}


async def douyin_get_video_comments(
    aweme_id: str,
    count: int = 50,
    cursor: int = 0
) -> Dict[str, Any]:
    """
    获取视频评论（研究中心评论分析功能使用）
    
    用途: 分析爆款视频评论，发现用户痛点和需求
    MCP工具: douyin_web_fetch_video_comments
    
    Args:
        aweme_id: 视频ID
        count: 获取评论数量
        cursor: 翻页游标
    
    Returns:
        评论列表
    """
    return await tikhub_call_tool(
        tool_name="douyin_web_fetch_video_comments",
        arguments={
            "aweme_id": aweme_id,
            "count": count,
            "cursor": cursor
        }
    )


async def douyin_get_single_video_v3(
    aweme_id: str
) -> Dict[str, Any]:
    """
    获取单个视频数据 V3（无版权限制）
    
    参数来源: https://docs.tikhub.io/406098636e0
    MCP工具: douyin_app_v3_fetch_one_video
    
    Args:
        aweme_id: 视频ID
    
    Returns:
        视频详情
    """
    # 尝试V3版本
    result = await tikhub_call_tool(
        tool_name="douyin_app_v3_fetch_one_video",
        arguments={
            "aweme_id": aweme_id
        }
    )
    
    # 如果V3失败，回退到普通版本
    if "error" in result:
        return await tikhub_call_tool(
            tool_name="douyin_web_fetch_one_video",
            arguments={
                "aweme_id": aweme_id
            }
        )
    
    return result


# ==================== 小红书 API 封装 ====================

async def xhs_get_user_info(
    user_id: str
) -> Dict[str, Any]:
    """
    获取小红书用户信息
    
    Args:
        user_id: 用户ID
    
    Returns:
        用户资料
    """
    return await tikhub_call_tool(
        tool_name="xiaohongshu_app_get_user_info",
        arguments={
            "user_id": user_id
        }
    )


async def xhs_get_user_notes(
    user_id: str,
    cursor: str = ""
) -> Dict[str, Any]:
    """
    获取小红书用户笔记列表
    
    Args:
        user_id: 用户ID
        cursor: 翻页游标
    
    Returns:
        笔记列表
    """
    args = {"user_id": user_id}
    if cursor:
        args["cursor"] = cursor
    
    return await tikhub_call_tool(
        tool_name="xiaohongshu_app_get_user_notes",  # Alternative: xiaohongshu_web_get_user_notes
        arguments=args
    )


async def xhs_search_notes(
    keyword: str,
    sort_type: str = "popularity",  # 热门排序 - 用于对标找数据最好的
    count: int = 20
) -> Dict[str, Any]:
    """
    小红书笔记搜索
    
    Args:
        keyword: 搜索关键词
        sort_type: 排序方式 (general=综合, popularity=热门, time=最新) - 默认热门获取数据最好的
        count: 返回数量
    
    Returns:
        笔记搜索结果
    """
    return await tikhub_call_tool(
        tool_name="xiaohongshu_app_search_notes",
        arguments={
            "keyword": keyword,
            "sort": sort_type,
            "page": 1,
            "page_size": count
        }
    )


async def xhs_search_users(
    keyword: str
) -> Dict[str, Any]:
    """
    小红书用户搜索
    
    Args:
        keyword: 搜索关键词
    
    Returns:
        用户搜索结果
    """
    return await tikhub_call_tool(
        tool_name="xiaohongshu_app_search_users",
        arguments={
            "keyword": keyword
        }
    )


# ==================== 视频号 API 封装 ====================

async def weixin_search_videos(
    keyword: str
) -> Dict[str, Any]:
    """
    视频号视频搜索
    
    Args:
        keyword: 搜索关键词
    
    Returns:
        视频搜索结果
    """
    return await tikhub_call_tool(
        tool_name="weixin_video_search_videos",
        arguments={
            "keyword": keyword
        }
    )


async def weixin_search_users(
    keyword: str
) -> Dict[str, Any]:
    """
    视频号用户搜索
    
    Args:
        keyword: 搜索关键词
    
    Returns:
        用户搜索结果
    """
    return await tikhub_call_tool(
        tool_name="weixin_video_search_users",
        arguments={
            "keyword": keyword
        }
    )


async def weixin_get_user_homepage(
    finder_username: str
) -> Dict[str, Any]:
    """
    视频号用户主页
    
    Args:
        finder_username: 视频号用户名
    
    Returns:
        用户主页数据
    """
    return await tikhub_call_tool(
        tool_name="weixin_video_get_user_homepage",
        arguments={
            "finder_username": finder_username
        }
    )


# ==================== 热榜 API ====================

async def douyin_get_hot_search() -> Dict[str, Any]:
    """
    获取抖音热搜榜（完整 50 条）

    使用 TikHub REST API (api.tikhub.io) 直接获取，
    MCP endpoint (mcp.tikhub.io/tools/call) 在 v2.0 后已失效。

    Returns:
        热搜榜列表，标准化结构
    """
    api_key = TIKHUB_MCP_CONFIG["api_key"]
    if not api_key:
        return {"status": "error", "error": "TIKHUB_API_KEY not configured", "trending": []}

    def _parse_trending(items: list) -> list:
        result = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            result.append({
                "rank": item.get("position", i + 1),
                "word": item.get("word", item.get("query", item.get("title", ""))),
                "hot_value": item.get("hot_value", item.get("heat", 0)) or 0,
                "event_time": item.get("event_time", ""),
                "label": item.get("label", 0),
            })
        return result

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await tracked_tikhub_get(
                client,
                "https://api.tikhub.io/api/v1/douyin/web/fetch_hot_search_result",
                caller="tikhub_hot_search",
                model="hot_search",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                body = resp.json()
                data = body.get("data", {})
                word_list = data.get("word_list") or data.get("data", {}).get("word_list", [])
                if isinstance(word_list, list) and len(word_list) > 0:
                    return {"status": "success", "trending": _parse_trending(word_list)}
    except Exception:
        pass

    # Fallback: try MCP endpoint (may work on older versions)
    result = await tikhub_call_tool(tool_name="douyin_web_fetch_hot_search_result", arguments={})
    if "error" not in result:
        mcp_data = result.get("result", result)
        data = mcp_data.get("data", {})
        word_list = data.get("word_list") or data.get("data", {}).get("word_list", [])
        if isinstance(word_list, list) and len(word_list) > 5:
            return {"status": "success", "trending": _parse_trending(word_list)}

    return {"status": "error", "error": "热搜API调用失败", "trending": []}


# 导出
__all__ = [
    # 通用
    "tikhub_list_tools",
    "tikhub_call_tool",
    # 抖音
    "douyin_search_videos",
    "douyin_get_user_videos",
    "douyin_search_users",
    "douyin_get_search_suggest",
    "douyin_get_hot_search",
    # v3.2 新增
    "douyin_get_user_videos_v3",
    "douyin_get_video_comments",
    "douyin_get_single_video_v3",
    # 小红书
    "xhs_get_user_info",
    "xhs_get_user_notes",
    "xhs_search_notes",
    "xhs_search_users",
    # 视频号
    "weixin_search_videos",
    "weixin_search_users",
    "weixin_get_user_homepage",
]

