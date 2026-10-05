"""
智能 API 端点管理器
自动检测端点可用性，实现熔断和自动切换

核心特性:
1. 端点优先级列表 (版本号越大越优先)
2. 熔断机制 (连续失败触发熔断)
3. 自动健康检查
4. 记录端点状态持久化
"""

import os
import json
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class EndpointHealth:
    """端点健康状态"""
    url: str
    tool_name: str  # MCP 工具名
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    failure_count: int = 0
    success_count: int = 0
    is_healthy: bool = True
    cooldown_until: Optional[datetime] = None
    
    def to_dict(self) -> Dict:
        return {
            "url": self.url,
            "tool_name": self.tool_name,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "is_healthy": self.is_healthy,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None
        }


# 端点优先级配置
# 规则: 版本号越大越优先，但如果高版本失败，自动降级到低版本
ENDPOINT_PRIORITY = {
    # 小红书搜索笔记 - 多个备选端点
    "xhs_search_notes": [
        {"tool": "xiaohongshu_app_search_notes", "version": 3, "url": "/api/v1/xiaohongshu/app/search_notes"},
        {"tool": "xiaohongshu_web_v2_fetch_search_notes", "version": 2, "url": "/api/v1/xiaohongshu/web_v2/fetch_search_notes"},
        {"tool": "xiaohongshu_web_search_notes_v3", "version": 1.5, "url": "/api/v1/xiaohongshu/web/search_notes_v3"},
        {"tool": "xiaohongshu_web_search_notes", "version": 1, "url": "/api/v1/xiaohongshu/web/search_notes"},
    ],
    # 小红书获取用户笔记
    "xhs_user_notes": [
        {"tool": "xiaohongshu_app_get_user_notes", "version": 2, "url": "/api/v1/xiaohongshu/app/get_user_notes"},
        {"tool": "xiaohongshu_web_get_user_notes_v2", "version": 1.5, "url": "/api/v1/xiaohongshu/web/get_user_notes_v2"},
        {"tool": "xiaohongshu_web_get_user_notes", "version": 1, "url": "/api/v1/xiaohongshu/web/get_user_notes"},
    ],
    # 小红书获取用户信息
    "xhs_user_info": [
        {"tool": "xiaohongshu_app_get_user_info", "version": 2, "url": "/api/v1/xiaohongshu/app/get_user_info"},
        {"tool": "xiaohongshu_web_v2_fetch_user_info", "version": 1.5, "url": "/api/v1/xiaohongshu/web_v2/fetch_user_info"},
        {"tool": "xiaohongshu_web_get_user_info_v2", "version": 1.2, "url": "/api/v1/xiaohongshu/web/get_user_info_v2"},
        {"tool": "xiaohongshu_web_get_user_info", "version": 1, "url": "/api/v1/xiaohongshu/web/get_user_info"},
    ],
    # 抖音视频搜索
    "douyin_search_videos": [
        {"tool": "douyin_search_fetch_video_search_v2", "version": 2, "url": "/api/v1/douyin/search/video_search_v2"},
        {"tool": "douyin_web_search_video", "version": 1.5, "url": "/api/v1/douyin/web/search_video"},
        {"tool": "douyin_app_search_video", "version": 1, "url": "/api/v1/douyin/app/search_video"},
    ],
    # 抖音用户作品
    "douyin_user_videos": [
        {"tool": "douyin_app_fetch_user_post_videos", "version": 2, "url": "/api/v1/douyin/app/fetch_user_post_videos"},
        {"tool": "douyin_web_fetch_user_post_videos", "version": 1.5, "url": "/api/v1/douyin/web/fetch_user_post_videos"},
    ],
    # 视频号搜索
    "weixin_search": [
        {"tool": "weixin_video_search_videos", "version": 1, "url": "/api/v1/weixin/video/search_videos"},
    ],
}


class SmartEndpointManager:
    """
    智能端点管理器
    
    特性:
    - 自动端点切换
    - 熔断机制
    - 健康状态持久化
    - 自动降级
    """
    
    def __init__(
        self,
        state_file: str = None,
        max_failures: int = 3,
        cooldown_seconds: int = 300  # 5 分钟冷却
    ):
        self.state_file = state_file or str(Path(__file__).parent / "endpoint_state.json")
        self.max_failures = max_failures
        self.cooldown_seconds = cooldown_seconds
        self.health_status: Dict[str, EndpointHealth] = {}
        
        # 加载历史状态
        self._load_state()
    
    def _load_state(self):
        """从文件加载端点状态"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for key, val in data.items():
                        self.health_status[key] = EndpointHealth(
                            url=val.get("url", ""),
                            tool_name=val.get("tool_name", ""),
                            last_success=datetime.fromisoformat(val["last_success"]) if val.get("last_success") else None,
                            last_failure=datetime.fromisoformat(val["last_failure"]) if val.get("last_failure") else None,
                            failure_count=val.get("failure_count", 0),
                            success_count=val.get("success_count", 0),
                            is_healthy=val.get("is_healthy", True),
                            cooldown_until=datetime.fromisoformat(val["cooldown_until"]) if val.get("cooldown_until") else None
                        )
            except Exception as e:
                print(f"  ⚠️ 加载端点状态失败: {e}")
    
    def _save_state(self):
        """保存端点状态到文件"""
        try:
            data = {k: v.to_dict() for k, v in self.health_status.items()}
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"  ⚠️ 保存端点状态失败: {e}")
    
    def _is_in_cooldown(self, tool_name: str) -> bool:
        """检查端点是否在冷却期"""
        health = self.health_status.get(tool_name)
        if health and health.cooldown_until:
            return datetime.now() < health.cooldown_until
        return False
    
    def _get_health(self, tool_name: str, url: str = "") -> EndpointHealth:
        """获取或创建端点健康状态"""
        if tool_name not in self.health_status:
            self.health_status[tool_name] = EndpointHealth(url=url, tool_name=tool_name)
        return self.health_status[tool_name]
    
    def mark_success(self, tool_name: str):
        """标记端点成功"""
        health = self._get_health(tool_name)
        health.last_success = datetime.now()
        health.success_count += 1
        health.failure_count = 0  # 重置失败计数
        health.is_healthy = True
        health.cooldown_until = None
        self._save_state()
    
    def mark_failure(self, tool_name: str):
        """标记端点失败"""
        health = self._get_health(tool_name)
        health.last_failure = datetime.now()
        health.failure_count += 1
        
        # 达到最大失败次数，触发熔断
        if health.failure_count >= self.max_failures:
            health.is_healthy = False
            health.cooldown_until = datetime.now() + timedelta(seconds=self.cooldown_seconds)
            print(f"  ⚠️ 端点熔断: {tool_name}，冷却 {self.cooldown_seconds} 秒")
        
        self._save_state()
    
    def get_available_endpoints(self, function: str) -> List[Dict]:
        """
        获取可用的端点列表 (按优先级排序)
        
        Args:
            function: 功能名称 (如 'xhs_search_notes')
        
        Returns:
            可用端点列表
        """
        endpoints = ENDPOINT_PRIORITY.get(function, [])
        available = []
        
        for ep in endpoints:
            tool_name = ep["tool"]
            
            # 跳过在冷却期的端点
            if self._is_in_cooldown(tool_name):
                continue
            
            # 跳过标记为不健康的端点
            health = self.health_status.get(tool_name)
            if health and not health.is_healthy and not self._cooldown_expired(tool_name):
                continue
            
            available.append(ep)
        
        # 如果没有可用端点，强制返回所有端点
        if not available and endpoints:
            print(f"  ⚠️ 所有端点都不可用，尝试所有端点")
            return endpoints
        
        return available
    
    def _cooldown_expired(self, tool_name: str) -> bool:
        """检查冷却期是否已过"""
        health = self.health_status.get(tool_name)
        if health and health.cooldown_until:
            if datetime.now() >= health.cooldown_until:
                # 冷却期过，重置状态
                health.is_healthy = True
                health.failure_count = 0
                health.cooldown_until = None
                return True
        return False
    
    async def call_with_fallback(
        self,
        function: str,
        call_func: Callable,
        arguments: Dict[str, Any],
        success_check: Callable[[Dict], bool] = None
    ) -> Dict[str, Any]:
        """
        带自动降级的 API 调用
        
        Args:
            function: 功能名称
            call_func: MCP 调用函数 (async)
            arguments: 调用参数
            success_check: 成功检查函数
        
        Returns:
            API 返回结果
        """
        endpoints = self.get_available_endpoints(function)
        
        if not endpoints:
            return {"error": f"No endpoints available for {function}"}
        
        # 默认成功检查
        if success_check is None:
            success_check = lambda r: r.get("code") == 200 or r.get("result", {}).get("code") == 200
        
        last_error = None
        
        for ep in endpoints:
            tool_name = ep["tool"]
            
            try:
                print(f"  🔄 尝试端点: {tool_name}")
                result = await call_func(tool_name, arguments)
                
                # 检查是否成功
                if "error" not in result and success_check(result):
                    self.mark_success(tool_name)
                    print(f"  ✅ 端点成功: {tool_name}")
                    return result
                else:
                    # API 返回错误
                    error_msg = result.get("error", result.get("result", {}).get("message", "Unknown error"))
                    print(f"  ❌ 端点失败: {tool_name} - {error_msg}")
                    self.mark_failure(tool_name)
                    last_error = result
                    
            except Exception as e:
                print(f"  ❌ 端点异常: {tool_name} - {e}")
                self.mark_failure(tool_name)
                last_error = {"error": str(e)}
        
        # 所有端点都失败
        return last_error or {"error": "All endpoints failed"}
    
    def get_status_report(self) -> Dict[str, Any]:
        """获取所有端点状态报告"""
        report = {
            "healthy": [],
            "unhealthy": [],
            "in_cooldown": []
        }
        
        for tool_name, health in self.health_status.items():
            status = {
                "tool": tool_name,
                "success_count": health.success_count,
                "failure_count": health.failure_count,
                "last_success": health.last_success.isoformat() if health.last_success else None
            }
            
            if self._is_in_cooldown(tool_name):
                status["cooldown_until"] = health.cooldown_until.isoformat()
                report["in_cooldown"].append(status)
            elif health.is_healthy:
                report["healthy"].append(status)
            else:
                report["unhealthy"].append(status)
        
        return report


# 全局单例
endpoint_manager = SmartEndpointManager()


# 导出
__all__ = [
    "SmartEndpointManager",
    "endpoint_manager",
    "ENDPOINT_PRIORITY"
]
