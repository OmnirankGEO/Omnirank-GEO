"""
TikHub API 端点管理器
实现动态端点切换和熔断机制，解决端点偶尔失效的问题
"""

import asyncio
import httpx
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Any
from agentscope.tool import ToolResponse

import sys
sys.path.append('..')
from config.model_config import TIKHUB_CONFIG
from tools.llm_call_tracker import llm_track


@dataclass
class EndpointStatus:
    """单个端点状态"""
    url: str
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    failure_count: int = 0
    is_healthy: bool = True


class TikHubEndpointManager:
    """TikHub API 端点动态管理器"""
    
    def __init__(self):
        self.domains = TIKHUB_CONFIG["domains"]
        self.endpoints = TIKHUB_CONFIG["endpoints"]
        self.circuit_breaker = TIKHUB_CONFIG["circuit_breaker"]
        self._endpoint_status: dict[str, EndpointStatus] = {}
        self._token = TIKHUB_CONFIG["api_key"]
    
    async def get_working_endpoint(
        self, 
        function: str,
        test_params: Optional[dict] = None
    ) -> Optional[str]:
        """获取可用的端点"""
        if function not in self.endpoints:
            raise ValueError(f"Unknown function: {function}")
        
        endpoint_paths = self.endpoints[function]
        
        for domain in self.domains:
            for endpoint_path in endpoint_paths:
                full_url = f"{domain}{endpoint_path}"
                
                if self._is_in_cooldown(full_url):
                    continue
                
                if await self._test_endpoint(full_url, test_params):
                    self._mark_success(full_url)
                    return full_url
                else:
                    self._mark_failure(full_url)
        
        return f"{self.domains[0]}{endpoint_paths[0]}"
    
    def _is_in_cooldown(self, url: str) -> bool:
        status = self._endpoint_status.get(url)
        if not status:
            return False
        
        max_failures = self.circuit_breaker["max_failures"]
        cooldown_seconds = self.circuit_breaker["cooldown_seconds"]
        
        if status.failure_count >= max_failures:
            if status.last_failure:
                cooldown_end = status.last_failure + timedelta(seconds=cooldown_seconds)
                if datetime.now() < cooldown_end:
                    return True
                status.failure_count = 0
                status.is_healthy = True
        return False
    
    async def _test_endpoint(self, url: str, params: Optional[dict]) -> bool:
        """测试端点可用性 - 按端点类型选择 POST/GET"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                headers = {"Authorization": f"Bearer {self._token}"}
                # 需要 POST 的端点：抖音视频搜索、微信视频号搜索
                needs_post = (
                    ("douyin" in url and "video_search" in url)
                    or "wechat_channels" in url
                )
                method = "POST" if needs_post else "GET"
                async with llm_track(
                    "tikhub_endpoint_probe",
                    "tikhub",
                    model="endpoint_probe",
                    metadata={"method": method, "url": url},
                ) as tracker:
                    if needs_post:
                        test_body = params or {"keyword": "test"}
                        # 微信端点用 keywords 字段
                        if "wechat_channels" in url and "keyword" in test_body:
                            test_body = {"keywords": test_body.pop("keyword", "test")}
                        response = await client.post(
                            url,
                            headers={**headers, "Content-Type": "application/json"},
                            json=test_body
                        )
                    else:
                        test_params = params or {"keyword": "test", "page": 1}
                        response = await client.get(
                            url,
                            headers=headers,
                            params=test_params
                        )
                    ok = response.status_code in [200, 400, 422]
                    tracker.record(success=ok, error_msg=None if ok else f"HTTP {response.status_code}")
                    return ok
        except Exception:
            return False
    
    def _mark_success(self, url: str):
        if url not in self._endpoint_status:
            self._endpoint_status[url] = EndpointStatus(url=url)
        status = self._endpoint_status[url]
        status.last_success = datetime.now()
        status.failure_count = 0
        status.is_healthy = True
    
    def _mark_failure(self, url: str):
        if url not in self._endpoint_status:
            self._endpoint_status[url] = EndpointStatus(url=url)
        status = self._endpoint_status[url]
        status.last_failure = datetime.now()
        status.failure_count += 1
        if status.failure_count >= self.circuit_breaker["max_failures"]:
            status.is_healthy = False


# 全局单例
tikhub_manager = TikHubEndpointManager()


async def _do_search_douyin(keyword: str, sort_type: str, publish_time: str, page: int) -> dict:
    """内部函数：执行抖音搜索"""
    endpoint = await tikhub_manager.get_working_endpoint(
        function="douyin_video_search",
        test_params={"keyword": keyword[:5], "page": 1}
    )
    
    async with httpx.AsyncClient(timeout=120) as client:
        async with llm_track(
            "tikhub_douyin_search",
            "tikhub",
            model="search",
            metadata={"endpoint": endpoint, "page": page},
        ) as tracker:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {tikhub_manager._token}"},
                json={
                    "keyword": keyword,
                    "sortType": sort_type,
                    "publishTime": publish_time,
                    "duration": "_0",
                    "page": page,
                    "searchId": ""
                }
            )
        
            if response.status_code != 200:
                tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                tikhub_manager._mark_failure(endpoint)
                raise Exception(f"HTTP {response.status_code}")
            tracker.record(success=True)
        
        return response.json()


async def _do_search_xiaohongshu(keyword: str, page: int, sort: str, note_time: str) -> dict:
    """内部函数：执行小红书搜索 (根据 TikHub 文档更新)"""
    endpoint = await tikhub_manager.get_working_endpoint(
        function="xiaohongshu_note_search",
        test_params={"keyword": keyword[:5], "page": 1}
    )
    
    async with httpx.AsyncClient(timeout=120) as client:
        # 根据 TikHub 文档使用正确的参数名
        # https://docs.tikhub.io/310965843e0
        async with llm_track(
            "tikhub_xiaohongshu_search",
            "tikhub",
            model="search",
            metadata={"endpoint": endpoint, "page": page},
        ) as tracker:
            response = await client.get(
                endpoint,
                headers={"Authorization": f"Bearer {tikhub_manager._token}"},
                params={
                    "keyword": keyword,
                    "page": page,
                    "sort_type": sort if sort else "general",  # 排序类型: general/hot/new
                    "filter_note_type": "不限",  # 笔记类型
                    "filter_note_time": note_time if note_time else "不限",  # 时间筛选
                }
            )
        
            if response.status_code != 200:
                tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                tikhub_manager._mark_failure(endpoint)
                raise Exception(f"HTTP {response.status_code}")
            tracker.record(success=True)
        
        return response.json()


def _clean_douyin_data(data: dict) -> dict:
    """清洗抖音数据，只保留关键字段"""
    if not data or "data" not in data:
        return {"status": "no_data", "count": 0}
        
    aweme_list = data.get("data", {}).get("aweme_list", [])
    
    # 兼容 V2 API 结构 (business_data)
    if not aweme_list:
        business_data = data.get("data", {}).get("business_data", [])
        for item in business_data:
            # 尝试提取 aweme_info
            info = item.get("data", {}).get("aweme_info")
            if info:
                # 补全 URL 构造所需的 aweme_id (有些接口用 id 或 aweme_id)
                if "aweme_id" not in info and "id" in info:
                    info["aweme_id"] = info["id"]
                aweme_list.append(info)

    if not aweme_list:
        return {"status": "success", "count": 0, "list": []}
        
    cleaned_list = []
    for item in aweme_list[:10]:  # 限制 Top 10
        # [Phase 12.6] 提取音频URL用于ASR转写
        music = item.get("music", {})
        play_url = music.get("play_url", {})
        audio_url_list = play_url.get("url_list", [])
        audio_url = audio_url_list[0] if audio_url_list else None
        
        cleaned_item = {
            "id": item.get("aweme_id"),
            "desc": item.get("desc"),
            "create_time": item.get("create_time"),
            "author": {
                "nickname": item.get("author", {}).get("nickname"),
                "id": item.get("author", {}).get("unique_id") or item.get("author", {}).get("short_id"),
                "follower_count": item.get("author", {}).get("follower_count", 0),  # 粉丝数
            },
            "stats": {
                "digg": item.get("statistics", {}).get("digg_count") or 0,
                "comment": item.get("statistics", {}).get("comment_count") or 0,
                "share": item.get("statistics", {}).get("share_count") or 0,
            },
            "url": f"https://www.douyin.com/video/{item.get('aweme_id')}",
            # [Phase 12.6] 保留音频URL用于ASR
            "music": {
                "title": music.get("title", ""),
                "author": music.get("author", ""),
                "audio_url": audio_url
            } if audio_url else None
        }
        cleaned_list.append(cleaned_item)
        
    return {
        "status": "success", 
        "count": len(cleaned_list),
        "total_raw": len(aweme_list),
        "list": cleaned_list
    }

def _clean_xhs_data(data: dict) -> dict:
    """清洗小红书数据，只保留关键字段
    
    API 返回结构: data.data.items[].note (TikHub V1 格式)
    """
    if not data or "data" not in data:
        return {"status": "no_data", "count": 0}
    
    # 处理嵌套结构: data.data.items
    inner_data = data.get("data", {})
    if isinstance(inner_data, dict) and "data" in inner_data:
        # TikHub 返回格式: {"data": {"success": true, "data": {"items": [...]}}}
        items_container = inner_data.get("data", {})
        notes = items_container.get("items", [])
    else:
        # 直接格式
        notes = inner_data.get("items", []) or inner_data.get("notes", [])
    
    if not notes:
        return {"status": "success", "count": 0, "list": []}
    
    cleaned_list = []
    for item in notes[:20]:  # 最多取 20 条
        # 处理 TikHub 格式: 每个 item 可能包含 "note" 字段
        note_data = item.get("note") if isinstance(item.get("note"), dict) else item
        
        # 跳过广告
        if item.get("model_type") == "ads":
            continue
        
        # 提取笔记数据
        note_id = note_data.get("id") or note_data.get("note_id")
        if not note_id:
            continue
            
        cleaned_item = {
            "id": note_id,
            "note_id": note_id,
            "title": note_data.get("title") or note_data.get("display_title"),
            "desc": note_data.get("desc"),
            "type": note_data.get("type", "normal"),
            "user": {
                "nickname": note_data.get("user", {}).get("nickname"),
                "id": note_data.get("user", {}).get("userid"),
            },
            "stats": {
                "likes": note_data.get("liked_count") or note_data.get("likes", 0),
                "comments": note_data.get("comments_count") or note_data.get("comments", 0),
                "collects": note_data.get("collected_count") or note_data.get("collects", 0),
                "shares": note_data.get("shared_count") or 0,
            },
            "timestamp": note_data.get("timestamp"),
            # 添加用于权重排序的字段
            "like_count": note_data.get("liked_count") or note_data.get("likes", 0),
            "comment_count": note_data.get("comments_count") or 0,
            "collect_count": note_data.get("collected_count") or 0,
        }
        cleaned_list.append(cleaned_item)
    
    return {
        "status": "success",
        "count": len(cleaned_list),
        "total_raw": len(notes),
        "list": cleaned_list
    }


def _clean_user_data_douyin(data: dict) -> dict:
    """清洗抖音用户信息"""
    if not data or "data" not in data:
        return {"status": "no_data"}
        
    user = data.get("data", {})
    # 可能是直接返回 user 对象，也可能是列表
    if "user_info" in user:
        user = user["user_info"]
    elif isinstance(user, list) and user:
        user = user[0]
        
    return {
        "status": "success",
        "nickname": user.get("nickname"),
        "id": user.get("uid") or user.get("unique_id"),
        "sec_uid": user.get("sec_uid"),
        "follower_count": user.get("follower_count"),
        "total_favorited": user.get("total_favorited"),
        "aweme_count": user.get("aweme_count"),
        "signature": user.get("signature"),
        "avatar": user.get("avatar_medium", {}).get("url_list", [""])[0] if user.get("avatar_medium") else ""
    }

def _clean_user_data_xhs(data: dict) -> dict:
    """清洗小红书用户信息"""
    if not data or "data" not in data:
        return {"status": "no_data"}
        
    user = data.get("data", {})
    return {
        "status": "success",
        "nickname": user.get("nickname") or user.get("name"),
        "id": user.get("red_id") or user.get("user_id"),
        "desc": user.get("desc"),
        "follower_count": user.get("fans") or user.get("follower_count"),
        "following_count": user.get("follows") or user.get("following_count"),
        "liked_count": user.get("liked") or user.get("liked_count"),
        "collected_count": user.get("collected") or user.get("collected_count"),
        "notes_count": user.get("notes") or user.get("notes_count"),
        "avatar": user.get("image") or user.get("avatar")
    }

def _clean_wechat_data(data: dict) -> dict:
    """清洗微信视频号数据"""
    if not data or "data" not in data:
        return {"status": "no_data"}
        
    items = data.get("data", {}).get("list", [])
    if not items:
        return {"status": "success", "count": 0, "list": []}
        
    cleaned_list = []
    for item in items[:10]:
        # 假设通用结构，如果不确定则尽量保留核心通用字段
        cleaned_item = {
            "desc": item.get("desc") or item.get("description"),
            "create_time": item.get("create_time") or item.get("createtime"),
            "user_name": item.get("nickname") or item.get("userName"),
            "stats": {
                "like": item.get("likeInfo", {}).get("likeCount"),
                "read": item.get("readCount"),
            }
        }
        cleaned_list.append(cleaned_item)
        
    return {
        "status": "success",
        "count": len(cleaned_list),
        "list": cleaned_list
    }


async def search_douyin_videos(
    keyword: str,
    sort_type: str = "_1",
    publish_time: str = "_180",
    page: int = 1
) -> ToolResponse:
    """
    搜索抖音视频（自动端点切换和重试）
    """
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            data = await _do_search_douyin(keyword, sort_type, publish_time, page)
            cleaned_data = _clean_douyin_data(data)
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps(cleaned_data, ensure_ascii=False)}]
            )
        except Exception as e:
            if attempt == max_retries - 1:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error after {max_retries} retries: {str(e)}"}]
                )
            await asyncio.sleep(1)


async def search_xiaohongshu_notes(
    keyword: str,
    page: int = 1,
    sort: str = "comment_descending",
    note_time: str = "半年内"
) -> ToolResponse:
    """
    搜索小红书笔记（自动端点切换和重试）
    """
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            data = await _do_search_xiaohongshu(keyword, page, sort, note_time)
            cleaned_data = _clean_xhs_data(data)
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps(cleaned_data, ensure_ascii=False)}]
            )
        except Exception as e:
            if attempt == max_retries - 1:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error after {max_retries} retries: {str(e)}"}]
                )
            await asyncio.sleep(1)



async def search_wechat_channels(keyword: str) -> ToolResponse:
    """
    搜索微信视频号内容
    
    Args:
        keyword (str): 搜索关键词
        
    Returns:
        ToolResponse: 包含搜索结果的响应
    """
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            endpoint = await tikhub_manager.get_working_endpoint(function="wechat_channels_search")

            async with httpx.AsyncClient(timeout=120) as client:
                # FIX: WeChat 端点只接受 POST，且字段名是 keywords（不是 keyword）
                async with llm_track(
                    "tikhub_wechat_channels_search",
                    "tikhub",
                    model="search",
                    metadata={"endpoint": endpoint},
                ) as tracker:
                    response = await client.post(
                        endpoint,
                        headers={
                            "Authorization": f"Bearer {tikhub_manager._token}",
                            "Content-Type": "application/json",
                        },
                        json={"keywords": keyword}
                    )
                
                    if response.status_code != 200:
                        tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                        tikhub_manager._mark_failure(endpoint)
                        raise Exception(f"HTTP {response.status_code}")
                    tracker.record(success=True)
                
                data = response.json()
                cleaned_data = _clean_wechat_data(data)
                return ToolResponse(
                    content=[{"type": "text", "text": json.dumps(cleaned_data, ensure_ascii=False)}]
                )
        except Exception as e:
            if attempt == max_retries - 1:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error after {max_retries} retries: {str(e)}"}]
                )
            await asyncio.sleep(1)


async def get_user_info(platform: str, user_id: str) -> ToolResponse:
    """
    获取用户信息
    
    Args:
        platform (str): 平台名称 ("douyin" 或 "xiaohongshu")
        user_id (str): 用户ID
        
    Returns:
        ToolResponse: 包含用户信息的响应
    """
    function_map = {
        "douyin": "douyin_user_search",
        "xiaohongshu": "xiaohongshu_user_info",
    }
    
    if platform not in function_map:
        return ToolResponse(
            content=[{"type": "text", "text": f"Error: Unsupported platform: {platform}"}]
        )
    
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            endpoint = await tikhub_manager.get_working_endpoint(function=function_map[platform])
            
            async with httpx.AsyncClient(timeout=60) as client:
                async with llm_track(
                    "tikhub_user_info",
                    "tikhub",
                    model="search",
                    metadata={"endpoint": endpoint, "platform": platform},
                ) as tracker:
                    response = await client.get(
                        endpoint,
                        headers={"Authorization": f"Bearer {tikhub_manager._token}"},
                        params={"user_id": user_id}
                    )
                
                    if response.status_code != 200:
                        tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                        tikhub_manager._mark_failure(endpoint)
                        raise Exception(f"HTTP {response.status_code}")
                    tracker.record(success=True)
                
                data = response.json()
                
                # Apply data cleaning based on platform
                cleaned_data = data
                if platform == "douyin":
                    cleaned_data = _clean_user_data_douyin(data)
                elif platform == "xiaohongshu":
                    cleaned_data = _clean_user_data_xhs(data)
                    
                return ToolResponse(
                    content=[{"type": "text", "text": json.dumps(cleaned_data, ensure_ascii=False)}]
                )
        except Exception as e:
            if attempt == max_retries - 1:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error after {max_retries} retries: {str(e)}"}]
                )
            await asyncio.sleep(1)
