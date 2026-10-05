"""
TikHub API 端点配置中心
基于官方文档最新接口定义 (2026-01)

文档来源: https://docs.tikhub.io/
优先级说明:
- 小红书: APP > WEBV2 > WEB
- 抖音: search > APP-V3 > WEB
"""

from typing import Literal
from dataclasses import dataclass
from enum import Enum


# API 基础 URL
TIKHUB_BASE_URL = "https://api.tikhub.io"


class Platform(Enum):
    """平台枚举"""
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    SHIPINHAO = "wechat_channels"


@dataclass
class APIEndpoint:
    """API 端点配置"""
    path: str
    method: Literal["GET", "POST"]
    params: dict  # 默认参数
    doc_url: str  # 文档链接
    description: str


# =============================================================================
# 小红书 (Xiaohongshu) API 端点
# 优先级: APP > WEBV2 > WEB
# =============================================================================

XHS_ENDPOINTS = {
    # 获取用户信息 (APP)
    "get_user_info": APIEndpoint(
        path="/api/v1/xiaohongshu/app/get_user_info",
        method="GET",
        params={"user_id": ""},  # 用户ID (required)
        doc_url="https://docs.tikhub.io/310965845e0",
        description="获取小红书用户信息"
    ),
    
    # 获取用户作品列表 (APP)
    "get_user_notes": APIEndpoint(
        path="/api/v1/xiaohongshu/app/get_user_notes",
        method="GET",
        params={
            "user_id": "",  # 用户ID (required)
            "cursor": ""    # 翻页游标
        },
        doc_url="https://docs.tikhub.io/310965846e0",
        description="获取小红书用户作品列表"
    ),
    
    # 搜索用户 (WEB)
    "search_users": APIEndpoint(
        path="/api/v1/xiaohongshu/web/search_users",
        method="GET",
        params={
            "keyword": "",  # 搜索关键词 (required)
            "page": 1       # 页码
        },
        doc_url="https://docs.tikhub.io/191613686e0",
        description="搜索小红书用户"
    ),
    
    # 搜索笔记 (APP)
    "search_notes": APIEndpoint(
        path="/api/v1/xiaohongshu/app/search_notes",
        method="GET",
        params={
            "keyword": "",       # 搜索关键词 (required)
            "page": 1,           # 页码 (default: 1)
            "sort_type": 0,      # 排序: 0-综合, 1-最热, 2-最新
            "filter_note_type": 0,  # 类型: 0-全部, 1-视频, 2-图文
            "filter_note_time": 0   # 时间: 0-全部, 180-半年内
        },
        doc_url="https://docs.tikhub.io/310965843e0",
        description="搜索小红书笔记"
    ),
    
    # 获取笔记评论 (APP)
    "get_note_comments": APIEndpoint(
        path="/api/v1/xiaohongshu/app/get_note_comments",
        method="GET",
        params={
            "note_id": "",  # 笔记ID (required)
            "cursor": ""    # 翻页游标
        },
        doc_url="https://docs.tikhub.io/310965840e0",
        description="获取小红书笔记评论"
    ),
}


# =============================================================================
# 抖音 (Douyin) API 端点
# 优先级: search > APP-V3 > WEB
# =============================================================================

DOUYIN_ENDPOINTS = {
    # 搜索视频 V2 (POST - search)
    "fetch_video_search": APIEndpoint(
        path="/api/v1/douyin/search/fetch_video_search_v2",
        method="POST",
        params={
            "keyword": "",        # 搜索关键词 (required)
            "cursor": 0,          # 翻页游标
            "sort_type": "0",     # 排序: 0-综合, 1-最多点赞, 2-最新发布
            "publish_time": "0",  # 发布时间: 0-不限, 1-一天内, 7-一周内, 180-半年内
            "filter_duration": "0", # 时长: 0-不限, 1-1分钟内, 2-1-5分钟
            "content_type": "0",  # 内容类型: 0-全部
            "search_id": "",
            "backtrace": ""
        },
        doc_url="https://docs.tikhub.io/370212780e0",
        description="搜索抖音视频 (V2)"
    ),
    
    # 搜索用户 V2 (POST - search)
    "fetch_user_search": APIEndpoint(
        path="/api/v1/douyin/search/fetch_user_search_v2",
        method="POST",
        params={
            "keyword": "",  # 搜索关键词 (required)
            "cursor": 0     # 翻页游标
        },
        doc_url="https://docs.tikhub.io/370212785e0",
        description="搜索抖音用户 (V2)"
    ),
    
    # 获取用户主页作品 (GET - APP-V3)
    "fetch_user_post_videos": APIEndpoint(
        path="/api/v1/douyin/app/v3/fetch_user_post_videos",
        method="GET",
        params={
            "sec_user_id": "",  # 用户 sec_user_id (required)
            "max_cursor": 0,    # 翻页游标
            "count": 20,        # 每页数量
            "sort_type": 0      # 排序
        },
        doc_url="https://docs.tikhub.io/186826223e0",
        description="获取抖音用户主页作品"
    ),
    
    # 获取关键词推荐 (下拉词)
    "fetch_keyword_suggest": APIEndpoint(
        path="/api/v1/douyin/search/fetch_keyword_suggest",
        method="GET",
        params={
            "keyword": ""  # 关键词 (required)
        },
        doc_url="https://docs.tikhub.io/370212778e0",
        description="获取抖音关键词推荐(下拉词)"
    ),
    
    # [Phase 12.7] 获取视频评论 (APP-V3)
    "fetch_video_comments": APIEndpoint(
        path="/api/v1/douyin/app/v3/fetch_video_comments",
        method="GET",
        params={
            "aweme_id": "",  # 视频ID (required)
            "cursor": 0,    # 翻页游标
            "count": 50     # 每页数量
        },
        doc_url="https://docs.tikhub.io/186826225e0",
        description="获取抖音视频评论数据"
    ),
}


# =============================================================================
# 视频号 (WeChat Channels / Shipinhao) API 端点
# =============================================================================

SHIPINHAO_ENDPOINTS = {
    # 搜索最新视频
    "fetch_search_latest": APIEndpoint(
        path="/api/v1/wechat_channels/fetch_search_latest",
        method="GET",
        params={
            "keywords": ""  # 搜索关键词 (required)
        },
        doc_url="https://docs.tikhub.io/348640548e0",
        description="搜索视频号最新视频"
    ),
    
    # 搜索用户
    "fetch_user_search": APIEndpoint(
        path="/api/v1/wechat_channels/fetch_user_search",
        method="GET",
        params={
            "keywords": "",  # 搜索关键词 (required)
            "page": 1        # 页码
        },
        doc_url="https://docs.tikhub.io/348640550e0",
        description="搜索视频号用户"
    ),
    
    # 用户主页
    "fetch_home_page": APIEndpoint(
        path="/api/v1/wechat_channels/fetch_home_page",
        method="GET",
        params={
            "username": "",    # 用户名 (required)
            "last_buffer": ""  # 翻页游标
        },
        doc_url="https://docs.tikhub.io/348640552e0",
        description="获取视频号用户主页"
    ),
}


# =============================================================================
# 端点优先级配置 (用于自动回退)
# =============================================================================

ENDPOINT_PRIORITY = {
    "xiaohongshu": {
        "search_notes": [
            "/api/v1/xiaohongshu/app/search_notes",  # APP 优先
            "/api/v1/xiaohongshu/webv2/search_notes",
            "/api/v1/xiaohongshu/web/search_notes",
        ],
        "get_user_info": [
            "/api/v1/xiaohongshu/app/get_user_info",
            "/api/v1/xiaohongshu/webv2/get_user_info",
        ],
    },
    "douyin": {
        "search_videos": [
            "/api/v1/douyin/search/fetch_video_search_v2",  # search 优先
            "/api/v1/douyin/app/v3/search_videos",
            "/api/v1/douyin/web/search_videos",
        ],
        "search_users": [
            "/api/v1/douyin/search/fetch_user_search_v2",
            "/api/v1/douyin/app/v3/search_users",
        ],
    },
}


def get_endpoint(platform: str, function: str) -> APIEndpoint:
    """获取指定平台的API端点配置"""
    if platform == "xiaohongshu":
        return XHS_ENDPOINTS.get(function)
    elif platform == "douyin":
        return DOUYIN_ENDPOINTS.get(function)
    elif platform == "wechat_channels" or platform == "shipinhao":
        return SHIPINHAO_ENDPOINTS.get(function)
    return None


def get_full_url(platform: str, function: str) -> str:
    """获取完整 API URL"""
    endpoint = get_endpoint(platform, function)
    if endpoint:
        return f"{TIKHUB_BASE_URL}{endpoint.path}"
    return None


# 导出
__all__ = [
    "TIKHUB_BASE_URL",
    "Platform",
    "APIEndpoint",
    "XHS_ENDPOINTS",
    "DOUYIN_ENDPOINTS", 
    "SHIPINHAO_ENDPOINTS",
    "ENDPOINT_PRIORITY",
    "get_endpoint",
    "get_full_url",
]
