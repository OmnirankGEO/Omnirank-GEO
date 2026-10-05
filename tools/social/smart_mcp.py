"""
TikHub MCP 智能调用层 v2.0
统一所有TikHub调用到MCP，实现自动发现+智能回退+熔断机制

设计原则：
1. 保持现有成功的调用不变
2. 失效时自动切换到备用端点
3. 启动时缓存工具列表
4. 统一MCP调用，减少维护成本

文档: https://docs.tikhub.io/
MCP端点: https://mcp.tikhub.io
"""

import httpx
import json
import os
import re
import asyncio
from typing import Optional, Dict, Any, List, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from tools.tikhub_cost_tracking import tracked_tikhub_get, tracked_tikhub_post


# TikHub MCP 配置
TIKHUB_MCP_URL = "https://mcp.tikhub.io"


@dataclass
class ToolStatus:
    """工具健康状态"""
    name: str
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    failure_count: int = 0
    is_healthy: bool = True


@dataclass 
class ToolPattern:
    """工具匹配模式配置"""
    action: str  # 业务动作名
    patterns: List[str]  # 工具名匹配模式, 支持通配符
    priority: List[str]  # 优先级排序 (app > v3 > web)
    required_params: List[str]  # 必需参数
    param_mappings: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # 参数映射
    data_path: str = "result.data"  # 数据验证路径


# 工具模式定义 - 按业务功能分类
TOOL_PATTERNS = {
    # ======= 抖音 (优先级: APP > V3 > WEB) =======
    "douyin_user_videos": ToolPattern(
        action="douyin_user_videos",
        patterns=["douyin_*_fetch_user_post_videos"],
        priority=["app", "app_v3", "web"],  # APP优先
        required_params=["sec_user_id"],
        param_mappings={
            "douyin_web_fetch_user_post_videos": {
                "sort_type": "filter_type",  # Web版用filter_type
                "max_cursor": lambda x: str(x),  # 转字符串
            }
        },
        data_path="result.data.aweme_list"
    ),
    "douyin_video_detail": ToolPattern(
        action="douyin_video_detail",
        patterns=["douyin_*_fetch_one_video"],
        priority=["app", "app_v3", "web"],  # APP优先
        required_params=["aweme_id"],
        data_path="result.data.aweme_detail"
    ),
    "douyin_video_comments": ToolPattern(
        action="douyin_video_comments",
        patterns=["douyin_*_fetch_video_comments"],
        priority=["app", "app_v3", "web"],  # APP优先
        required_params=["aweme_id"],
        data_path="result.data.comments"
    ),
    "douyin_video_search": ToolPattern(
        action="douyin_video_search",
        patterns=["douyin_*_fetch_video_search*", "douyin_search_*"],
        priority=["search", "app", "web"],  # search优先
        required_params=["keyword"],
        data_path="result.data"
    ),
    "douyin_user_search": ToolPattern(
        action="douyin_user_search",
        patterns=["douyin_*_fetch_user_search*", "douyin_*_search_users"],
        priority=["search", "app", "web"],  # search优先
        required_params=["keyword"],
        data_path="result.data"
    ),
    "douyin_hot_search": ToolPattern(
        action="douyin_hot_search",
        patterns=["douyin_*_fetch_hot_search*", "douyin_*_hot_*"],
        priority=["app", "web"],  # APP优先
        required_params=[],
        data_path="result.data"
    ),
    
    # ======= 小红书 =======
    "xhs_user_info": ToolPattern(
        action="xhs_user_info",
        patterns=["xiaohongshu_*_get_user_info*"],
        priority=["app", "web"],
        required_params=["user_id"],
        data_path="result.data"
    ),
    "xhs_user_notes": ToolPattern(
        action="xhs_user_notes",
        patterns=["xiaohongshu_*_get_user_notes*", "xiaohongshu_*_get_user_posted*"],
        priority=["app", "web"],
        required_params=["user_id"],
        data_path="result.data"
    ),
    "xhs_note_search": ToolPattern(
        action="xhs_note_search",
        patterns=["xiaohongshu_*_search_notes*"],
        priority=["app", "web"],
        required_params=["keyword"],
        data_path="result.data"
    ),
    "xhs_note_comments": ToolPattern(
        action="xhs_note_comments",
        patterns=["xiaohongshu_*_get_note_comments*"],
        priority=["app", "web"],
        required_params=["note_id"],
        data_path="result.data"
    ),
    
    # ======= 视频号 =======
    "weixin_video_search": ToolPattern(
        action="weixin_video_search",
        patterns=["weixin_video_*_search*", "wechat_*_search*"],
        priority=["web"],
        required_params=["keyword"],
        data_path="result.data"
    ),
}


class SmartMCPClient:
    """
    TikHub MCP 智能调用客户端
    
    功能：
    1. 启动时缓存可用工具列表
    2. 按模式匹配工具
    3. 按优先级自动回退
    4. 熔断机制防止频繁失败
    """
    
    def __init__(self):
        self._api_key = os.environ.get("TIKHUB_API_KEY")
        self._tools_cache: List[Dict] = []
        self._tool_status: Dict[str, ToolStatus] = {}
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = timedelta(hours=1)  # 缓存1小时
        
        # 熔断配置
        self._max_failures = 3
        self._cooldown_seconds = 300  # 5分钟冷却
    
    async def _ensure_tools_cached(self) -> None:
        """确保工具列表已缓存"""
        if self._tools_cache and self._cache_time:
            if datetime.now() - self._cache_time < self._cache_ttl:
                return
        
        # 刷新缓存
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await tracked_tikhub_get(
                    client,
                    f"{TIKHUB_MCP_URL}/tools",
                    caller="tikhub_smart_tools_cache",
                    model="mcp_tool",
                    headers={"Authorization": f"Bearer {self._api_key}"}
                )
                if response.status_code == 200:
                    result = response.json()
                    if isinstance(result, list):
                        self._tools_cache = result
                    else:
                        self._tools_cache = result.get("tools", [])
                    self._cache_time = datetime.now()
                    print(f"[SmartMCP] 已缓存 {len(self._tools_cache)} 个工具")
        except Exception as e:
            print(f"[SmartMCP] 工具缓存失败: {e}")
    
    def _match_tools(self, pattern: ToolPattern) -> List[str]:
        """
        根据模式匹配可用工具，按优先级排序
        """
        matched = []
        
        for tool in self._tools_cache:
            tool_name = tool.get("name", "")
            
            # 检查是否匹配任一模式
            for pat in pattern.patterns:
                regex = pat.replace("*", ".*")
                if re.match(regex, tool_name):
                    matched.append(tool_name)
                    break
        
        # 按优先级排序
        def priority_key(name: str) -> int:
            for i, prio in enumerate(pattern.priority):
                if prio in name.lower():
                    return i
            return 999  # 未匹配的放最后
        
        matched.sort(key=priority_key)
        return matched
    
    def _is_in_cooldown(self, tool_name: str) -> bool:
        """检查工具是否在冷却期"""
        status = self._tool_status.get(tool_name)
        if not status:
            return False
        
        if status.failure_count >= self._max_failures:
            if status.last_failure:
                cooldown_end = status.last_failure + timedelta(seconds=self._cooldown_seconds)
                if datetime.now() < cooldown_end:
                    return True
                # 冷却结束，重置
                status.failure_count = 0
                status.is_healthy = True
        return False
    
    def _mark_success(self, tool_name: str) -> None:
        """标记工具调用成功"""
        if tool_name not in self._tool_status:
            self._tool_status[tool_name] = ToolStatus(name=tool_name)
        status = self._tool_status[tool_name]
        status.last_success = datetime.now()
        status.failure_count = 0
        status.is_healthy = True
    
    def _mark_failure(self, tool_name: str) -> None:
        """标记工具调用失败"""
        if tool_name not in self._tool_status:
            self._tool_status[tool_name] = ToolStatus(name=tool_name)
        status = self._tool_status[tool_name]
        status.last_failure = datetime.now()
        status.failure_count += 1
        if status.failure_count >= self._max_failures:
            status.is_healthy = False
            print(f"[SmartMCP] 工具 {tool_name} 已熔断 ({status.failure_count}次失败)")
    
    def _adapt_params(self, tool_name: str, params: Dict, pattern: ToolPattern) -> Dict:
        """
        适配参数名称差异
        """
        mappings = pattern.param_mappings.get(tool_name, {})
        adapted = {}
        
        for key, value in params.items():
            new_key = mappings.get(key, key)
            if callable(new_key):
                # 如果是函数，用于转换值
                adapted[key] = new_key(value)
            elif isinstance(new_key, str):
                adapted[new_key] = value
            else:
                adapted[key] = value
        
        return adapted
    
    async def _call_tool(self, tool_name: str, arguments: Dict) -> Dict[str, Any]:
        """
        调用单个MCP工具
        """
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await tracked_tikhub_post(
                    client,
                    f"{TIKHUB_MCP_URL}/tools/call",
                    caller="tikhub_smart_call",
                    model="mcp_tool",
                    metadata={"tool_name": tool_name},
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
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
    
    def _validate_result(self, result: Dict, data_path: str) -> bool:
        """
        验证结果是否有效
        """
        if "error" in result:
            return False
        
        # 按路径查找数据
        parts = data_path.split(".")
        data = result
        for part in parts:
            if isinstance(data, dict):
                data = data.get(part)
            else:
                return False
            if data is None:
                return False
        
        # 检查是否有数据
        if isinstance(data, list):
            return len(data) > 0
        return data is not None
    
    async def smart_call(
        self,
        action: str,
        params: Dict[str, Any],
        fallback_tools: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        智能调用MCP工具
        
        Args:
            action: 业务动作 (如 "douyin_user_videos")
            params: 调用参数
            fallback_tools: 可选的回退工具列表（手动指定）
        
        Returns:
            MCP调用结果
        """
        await self._ensure_tools_cached()
        
        # 获取模式配置
        pattern = TOOL_PATTERNS.get(action)
        if not pattern and not fallback_tools:
            return {"error": f"未知的action: {action}"}
        
        # 获取匹配的工具列表
        if fallback_tools:
            matched_tools = fallback_tools
        else:
            matched_tools = self._match_tools(pattern)
        
        if not matched_tools:
            return {"error": f"未找到匹配的工具: {action}"}
        
        print(f"[SmartMCP] {action} 匹配到 {len(matched_tools)} 个工具: {matched_tools[:3]}...")
        
        # 依次尝试
        last_error = None
        for tool_name in matched_tools:
            # 跳过冷却中的工具
            if self._is_in_cooldown(tool_name):
                print(f"[SmartMCP] 跳过冷却中的工具: {tool_name}")
                continue
            
            # 适配参数
            adapted_params = self._adapt_params(tool_name, params, pattern) if pattern else params
            
            print(f"[SmartMCP] 尝试: {tool_name}")
            result = await self._call_tool(tool_name, adapted_params)
            
            # 检查结果
            if "error" in result:
                print(f"[SmartMCP] {tool_name} 失败: {result.get('error')}")
                self._mark_failure(tool_name)
                last_error = result
                continue
            
            # 验证数据有效性
            if pattern and not self._validate_result(result, pattern.data_path):
                print(f"[SmartMCP] {tool_name} 返回无效数据")
                # 不标记失败，可能是用户确实没数据
                last_error = result
                continue
            
            # 成功
            self._mark_success(tool_name)
            print(f"[SmartMCP] {tool_name} 成功")
            return result
        
        # 所有工具都失败
        if last_error:
            return last_error
        return {"error": "所有工具均调用失败"}
    
    async def direct_call(self, tool_name: str, arguments: Dict) -> Dict[str, Any]:
        """
        直接调用指定工具（不经过智能匹配）
        用于已知正常工作的调用，保持兼容
        """
        return await self._call_tool(tool_name, arguments)
    
    def get_health_status(self) -> Dict[str, Any]:
        """获取所有工具健康状态"""
        return {
            "cached_tools": len(self._tools_cache),
            "cache_age": str(datetime.now() - self._cache_time) if self._cache_time else None,
            "tool_status": {
                name: {
                    "healthy": s.is_healthy,
                    "failure_count": s.failure_count,
                    "last_success": str(s.last_success) if s.last_success else None,
                    "last_failure": str(s.last_failure) if s.last_failure else None,
                }
                for name, s in self._tool_status.items()
            }
        }


# 全局单例
smart_mcp = SmartMCPClient()


# ==================== 便捷封装函数 ====================
# 这些函数保持与原有接口兼容

async def get_user_videos(
    sec_user_id: str,
    count: int = 20,
    sort_type: str = "0",
    max_cursor: int = 0
) -> Dict[str, Any]:
    """
    获取用户主页视频（智能回退版）
    
    兼容原 douyin_get_user_videos_v3
    """
    return await smart_mcp.smart_call(
        action="douyin_user_videos",
        params={
            "sec_user_id": sec_user_id,
            "count": count,
            "sort_type": sort_type,
            "max_cursor": max_cursor
        }
    )


async def get_video_comments(
    aweme_id: str,
    count: int = 50,
    cursor: int = 0
) -> Dict[str, Any]:
    """
    获取视频评论（智能回退版）
    """
    return await smart_mcp.smart_call(
        action="douyin_video_comments",
        params={
            "aweme_id": aweme_id,
            "count": count,
            "cursor": cursor
        }
    )


async def search_videos(
    keyword: str,
    sort_type: str = "1",
    publish_time: str = "180",
    count: int = 20
) -> Dict[str, Any]:
    """
    搜索视频（智能回退版）
    """
    return await smart_mcp.smart_call(
        action="douyin_video_search",
        params={
            "keyword": keyword,
            "sort_type": sort_type,
            "publish_time": publish_time,
            "count": count
        }
    )


async def get_hot_search() -> Dict[str, Any]:
    """
    获取热搜榜（智能回退版）
    """
    return await smart_mcp.smart_call(
        action="douyin_hot_search",
        params={}
    )


# 导出
__all__ = [
    "SmartMCPClient",
    "smart_mcp",
    "TOOL_PATTERNS",
    "get_user_videos",
    "get_video_comments",
    "search_videos",
    "get_hot_search",
]
