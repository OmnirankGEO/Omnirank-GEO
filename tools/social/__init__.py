# Social Media Tools Package
from .tikhub_mcp import (
    tikhub_list_tools,
    tikhub_call_tool,
    # Douyin
    douyin_search_videos,
    douyin_get_user_videos,
    douyin_search_users,
    douyin_get_search_suggest,
    # XHS
    xhs_get_user_info,
    xhs_get_user_notes,
    xhs_search_notes,
    xhs_search_users,
    # Weixin
    weixin_search_videos,
    weixin_search_users,
    weixin_get_user_homepage,
)

__all__ = [
    "tikhub_list_tools",
    "tikhub_call_tool",
    "douyin_search_videos",
    "douyin_get_user_videos",
    "douyin_search_users",
    "douyin_get_search_suggest",
    "xhs_get_user_info",
    "xhs_get_user_notes",
    "xhs_search_notes",
    "xhs_search_users",
    "weixin_search_videos",
    "weixin_search_users",
    "weixin_get_user_homepage",
]
