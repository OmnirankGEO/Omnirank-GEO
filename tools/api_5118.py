"""
5118 API 集成模块
- 海量长尾词挖掘API v2
- 关键词搜索量信息API v2

配置说明 (根据官方文档):
- Base URL: https://apis.5118.com
- Authorization: 直接在header中用 Authorization: {apikey}
- Content-Type: application/x-www-form-urlencoded
"""
import os
import aiohttp
import asyncio
from typing import Optional
import json
from dotenv import load_dotenv
from tools.llm_call_tracker import llm_track

load_dotenv()

# [并发-5118 2026-06-10 打广告高并发] 5118 全局在途并发上限(进程内)。
# 5118 不返 429、不拒绝,但高并发下两步式轮询(submit + 最多10次poll)会堆积拖慢全场。
# 设安全顶(默认50·非紧闸)防 5118 偶发劣化时无限堆积;冷词才打 5118,缓存预热承接热词。
# 经 API_5118_MAX_CONCURRENCY 调(force-recreate 生效)。多账号矩阵(get_5118_pool)待老板给多账号后接。
_5118_semaphore = asyncio.Semaphore(int(os.getenv("API_5118_MAX_CONCURRENCY", "50")))


def safe_float(value, default=0.0):
    """安全转换为float，处理 '-' 或其他无效值"""
    if value is None or value == '' or value == '-':
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value, default=0):
    """安全转换为int，处理无效值"""
    if value is None or value == '' or value == '-':
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


class API5118Client:
    """5118 API客户端"""
    
    # API Keys（统一从环境变量读取）
    LONGTAIL_API_KEY = os.environ.get("API_5118_LONGTAIL_KEY", "")
    SEARCH_VOLUME_API_KEY = os.environ.get("API_5118_SEARCH_VOLUME_KEY", "")

    # API Endpoints
    BASE_URL = "https://apis.5118.com"
    LONGTAIL_ENDPOINT = f"{BASE_URL}/keyword/word/v2"
    SEARCH_VOLUME_ENDPOINT = f"{BASE_URL}/keywordparam/v2"
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    def _longtail_headers_for_key(self, key: Optional[str] = None):
        """长尾词 API headers · [round-robin+限速 2026-06-11] key 由上层 acquire_round_robin_throttled 选定(均摊+QPS平滑)
        回落单 self.LONGTAIL_API_KEY 不变。"""
        return {
            "Authorization": key or self.LONGTAIL_API_KEY,  # 直接用key，不加前缀
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        }

    def _search_headers_for_key(self, key: Optional[str] = None):
        """搜索量 API headers · [round-robin+限速 2026-06-11] key 由上层选定;submit+poll 用同一 key(taskid 绑账号)。
        回落单 self.SEARCH_VOLUME_API_KEY 不变。"""
        return {
            "Authorization": key or self.SEARCH_VOLUME_API_KEY,  # 直接用key，不加前缀
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        }
    
    # ========================================
    # 海量长尾词挖掘API v2
    # ========================================
    async def mine_longtail_keywords(
        self,
        keyword: str,
        page_index: int = 1,
        page_size: int = 100,
        sort_fields: int = 4,
        sort_type: str = "desc"
    ) -> dict:
        """[round-robin+限速+failover 2026-06-11] 多账号 round-robin 均摊 + 每账号 QPS 平滑限速(不撞「每秒调用量超限」)
        + 某账号失败/被封自动换下一个 round-robin 账号重试·试遍全部·返首个成功。"""
        from tools.api_source_pool import get_5118_longtail_pool
        pool = get_5118_longtail_pool()
        _n = max(1, pool.size)
        _wait = float(os.getenv("API_5118_RATE_WAIT", "30"))
        _last = None
        for _ in range(_n):
            # round-robin 选账号(_rr 自增·分流)+ async 平滑限速等待该账号 QPS 配额;失败则下轮换下一个账号
            _key, _ok = await pool.acquire_round_robin_throttled(max_wait=_wait)
            _last = await self._mine_longtail_once(keyword, page_index, page_size, sort_fields, sort_type, _key=_key)
            if _last.get("success"):
                return _last
        return _last

    async def _mine_longtail_once(
        self,
        keyword: str,
        page_index: int = 1,
        page_size: int = 100,
        sort_fields: int = 4,  # 4=流量指数
        sort_type: str = "desc",
        _key: Optional[str] = None,
    ) -> dict:
        """
        挖掘长尾关键词
        
        Args:
            keyword: 核心关键词
            page_index: 页码 (1-based)
            page_size: 每页数量 (max 100)
            sort_fields: 排序字段 (4=流量指数, 7=PC搜索量, 8=移动搜索量)
            sort_type: 排序方式 (desc/asc)
        
        Returns:
            {
                "success": True/False,
                "total": 总数,
                "keywords": [...]
            }
        """
        await self._ensure_session()

        headers = self._longtail_headers_for_key(_key)

        # 使用 form data 而不是 JSON
        payload = {
            "keyword": keyword,
            "page_index": str(page_index),
            "page_size": str(page_size),
            "sort_fields": str(sort_fields),
            "sort_type": sort_type
        }

        # [并发-5118] 全局在途上限 · 防高并发长尾挖掘堆积
        await _5118_semaphore.acquire()
        try:
            async with llm_track(
                "5118_longtail",
                "5118",
                model="longtail",
                metadata={"page_index": page_index, "page_size": page_size},
            ) as tracker:
                async with self.session.post(
                    self.LONGTAIL_ENDPOINT,
                    headers=headers,
                    data=payload,  # 用 data 而不是 json
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    text = await response.text()
                
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    tracker.record(success=False, error_msg=f"Invalid JSON response: {text[:200]}")
                    return {
                        "success": False,
                        "error": f"Invalid JSON response: {text[:200]}",
                        "total": 0,
                        "keywords": []
                    }
                
                if data.get("errcode") != "0":
                    tracker.record(success=False, error_msg=data.get("errmsg", f"Error code: {data.get('errcode')}"))
                    return {
                        "success": False,
                        "error": data.get("errmsg", f"Error code: {data.get('errcode')}"),
                        "total": 0,
                        "keywords": []
                    }
                
                # 数据结构: data.word[] 
                data_obj = data.get("data", {})
                word_list = data_obj.get("word", [])
                
                keywords = []
                for item in word_list:
                    keywords.append({
                        "keyword": item.get("keyword", ""),
                        "index": safe_int(item.get("index")),
                        "mobile_index": safe_int(item.get("mobile_index")),
                        "haosou_index": safe_int(item.get("haosou_index")),
                        "sem_price": safe_float(item.get("sem_price") or item.get("bidword_price")),
                        "bidword_company_count": safe_int(item.get("bidword_company_count")),
                        "bidword_kwc": safe_int(item.get("bidword_kwc")),
                        "bidword_pcpv": safe_int(item.get("bidword_pcpv")),
                        "bidword_wisepv": safe_int(item.get("bidword_wisepv"))
                    })
                
                tracker.record(success=True, billable_units=len(keywords))
                return {
                    "success": True,
                    "total": data_obj.get("total", len(keywords)),
                    "page_count": data_obj.get("page_count", 1),
                    "keywords": keywords
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "total": 0,
                "keywords": []
            }
        finally:
            _5118_semaphore.release()

    # ========================================
    # 关键词搜索量信息API v2 (两步式)
    # ========================================
    async def get_keyword_search_volume(
        self,
        keywords: list[str],
        max_retries: int = 10,
        retry_delay: float = 1.0
    ) -> dict:
        """[round-robin+限速+failover 2026-06-11] round-robin 均摊 + 每账号 QPS 平滑限速;
        某账号 submit 失败 → 自动换下一个 round-robin 账号重试·试遍全部。
        (单账号内 submit+poll 仍同 key·taskid 不跨账号;换账号=重新 submit+poll)。"""
        from tools.api_source_pool import get_5118_search_pool
        pool = get_5118_search_pool()
        _n = max(1, pool.size)
        _wait = float(os.getenv("API_5118_RATE_WAIT", "30"))
        _last = None
        for _ in range(_n):
            _key, _ok = await pool.acquire_round_robin_throttled(max_wait=_wait)
            _last = await self._search_volume_once(keywords, max_retries, retry_delay, _key=_key)
            if _last.get("success"):
                return _last
        return _last

    async def _search_volume_once(
        self,
        keywords: list[str],
        max_retries: int = 10,
        retry_delay: float = 1.0,
        _key: Optional[str] = None,
    ) -> dict:
        """
        批量获取关键词搜索量信息 (两步式API)
        
        Step 1: 提交关键词列表获取 taskid
        Step 2: 用 taskid 轮询获取结果
        
        Args:
            keywords: 关键词列表 (最多50个)
            max_retries: 最大重试次数
            retry_delay: 重试间隔(秒)
        
        Returns:
            {
                "success": True/False,
                "keywords": [...]
            }
        """
        await self._ensure_session()
        
        # 限制最多50个关键词
        if len(keywords) > 50:
            keywords = keywords[:50]

        headers = self._search_headers_for_key(_key)

        # Step 1: 提交任务获取taskid
        keywords_str = "|".join(keywords)
        payload = {"keywords": keywords_str}

        # [并发-5118] 全局在途上限:holds 跨 submit+poll 整段 · 防高并发堆积塌方
        await _5118_semaphore.acquire()
        try:
            async with llm_track(
                "5118_search_volume_submit",
                "5118",
                model="search_volume",
                metadata={"keyword_count": len(keywords), "billable_units": len(keywords)},
            ) as tracker:
                async with self.session.post(
                    self.SEARCH_VOLUME_ENDPOINT,
                    headers=headers,
                    data=payload,  # 用 data 而不是 json
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    text = await response.text()
                
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    tracker.record(success=False, error_msg=f"Invalid JSON response: {text[:200]}")
                    return {
                        "success": False,
                        "error": f"Invalid JSON response: {text[:200]}",
                        "keywords": []
                    }
                
                if data.get("errcode") != "0":
                    tracker.record(success=False, error_msg=data.get("errmsg", f"Error code: {data.get('errcode')}"))
                    return {
                        "success": False,
                        "error": data.get("errmsg", f"Error code: {data.get('errcode')}"),
                        "keywords": []
                    }
                
                taskid = data.get("data", {}).get("taskid")
                if not taskid:
                    tracker.record(success=False, error_msg="No taskid returned")
                    return {
                        "success": False,
                        "error": "No taskid returned",
                        "keywords": []
                    }
                tracker.record(success=True)
            
            # Step 2: 轮询获取结果
            for retry in range(max_retries):
                await asyncio.sleep(retry_delay)
                
                async with llm_track(
                    "5118_search_volume_poll",
                    "5118",
                    model="search_volume",
                    metadata={"retry": retry + 1, "billable_units": 0},
                ) as tracker:
                    async with self.session.post(
                        self.SEARCH_VOLUME_ENDPOINT,
                        headers=headers,
                        data={"taskid": str(taskid)},
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:
                        text = await response.text()
                    
                    try:
                        result = json.loads(text)
                    except json.JSONDecodeError:
                        tracker.record(success=False, error_msg=f"Invalid JSON response: {text[:200]}")
                        continue
                    
                    if result.get("errcode") == "0":
                        tracker.record(success=True)
                        keyword_data = result.get("data", {}).get("keyword_param", [])
                        
                        if keyword_data:
                            keywords_result = []
                            for item in keyword_data:
                                keywords_result.append({
                                    "keyword": item.get("keyword", ""),
                                    "index": safe_int(item.get("index")),
                                    "mobile_index": safe_int(item.get("mobile_index")),
                                    "douyin_index": safe_int(item.get("douyin_index")),
                                    "haosou_index": safe_int(item.get("haosou_index")),
                                    "toutiao_index": safe_int(item.get("toutiao_index")),
                                    "sem_price": safe_float(item.get("bidword_price")),
                                    "competition": safe_int(item.get("bidword_kwc")),
                                    "bidword_pcpv": safe_int(item.get("bidword_pcpv")),
                                    "bidword_wisepv": safe_int(item.get("bidword_wisepv")),
                                    "bidword_company_count": safe_int(item.get("bidword_company_count")),
                                    "long_keyword_count": safe_int(item.get("long_keyword_count")),
                                    "age_best": item.get("age_best", ""),
                                    "sex_male": safe_float(item.get("sex_male")),
                                    "sex_female": safe_float(item.get("sex_female"))
                                })
                            
                            return {
                                "success": True,
                                "keywords": keywords_result
                            }
            
            return {
                "success": False,
                "error": "Timeout waiting for results",
                "keywords": []
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "keywords": []
            }
        finally:
            _5118_semaphore.release()

    # ========================================
    # 高级功能
    # ========================================
    async def analyze_keyword_value(self, keyword: str) -> dict:
        """分析单个关键词的价值"""
        result = await self.get_keyword_search_volume([keyword])
        
        if not result.get("success") or not result.get("keywords"):
            return {
                "keyword": keyword,
                "error": result.get("error", "Failed to get data"),
                "search_volume": 0,
                "commercial_value": 0,
                "competition_level": "unknown",
                "suggested_price": 0
            }
        
        data = result["keywords"][0]
        
        # 计算搜索量
        search_volume = data["index"] + data["mobile_index"]
        
        # 计算商业价值分 (0-100)
        sem_price = data["sem_price"]
        commercial_value = min(100, sem_price * 10)
        
        # 竞争等级
        competition_map = {1: "high", 2: "medium", 3: "low", 0: "unknown"}
        competition_level = competition_map.get(data["competition"], "unknown")
        
        # 建议月费计算
        if search_volume < 100:
            base_price = 500
        elif search_volume < 500:
            base_price = 1000
        elif search_volume < 2000:
            base_price = 2000
        else:
            base_price = 3500
        
        price_multiplier = 1 + (commercial_value / 100)
        suggested_price = int(base_price * price_multiplier)
        
        return {
            "keyword": keyword,
            "search_volume": search_volume,
            "sem_price": sem_price,
            "commercial_value": commercial_value,
            "competition_level": competition_level,
            "bidword_company_count": data["bidword_company_count"],
            "long_keyword_count": data["long_keyword_count"],
            "suggested_price": suggested_price
        }
    
    async def batch_analyze_keywords(self, keywords: list[str]) -> list[dict]:
        """批量分析关键词价值"""
        all_results = []
        
        for i in range(0, len(keywords), 50):
            batch = keywords[i:i+50]
            result = await self.get_keyword_search_volume(batch)
            
            if result.get("success"):
                for data in result["keywords"]:
                    search_volume = data["index"] + data["mobile_index"]
                    sem_price = data["sem_price"]
                    commercial_value = min(100, sem_price * 10)
                    
                    competition_map = {1: "high", 2: "medium", 3: "low", 0: "unknown"}
                    competition_level = competition_map.get(data["competition"], "unknown")
                    
                    if search_volume < 100:
                        base_price = 500
                    elif search_volume < 500:
                        base_price = 1000
                    elif search_volume < 2000:
                        base_price = 2000
                    else:
                        base_price = 3500
                    
                    price_multiplier = 1 + (commercial_value / 100)
                    suggested_price = int(base_price * price_multiplier)
                    
                    all_results.append({
                        "keyword": data["keyword"],
                        "search_volume": search_volume,
                        "sem_price": sem_price,
                        "commercial_value": commercial_value,
                        "competition_level": competition_level,
                        "suggested_price": suggested_price
                    })
        
        return all_results


# 全局客户端实例
_client: Optional[API5118Client] = None


async def get_5118_client() -> API5118Client:
    """获取5118 API客户端单例"""
    global _client
    if _client is None:
        _client = API5118Client()
    return _client


async def close_5118_client() -> None:
    """关闭 5118 singleton · 防 aiohttp ClientSession 在 app shutdown 时泄漏 warning.

    SSE 优化 2026-05-16(Codex flagged ResourceWarning):server.py shutdown event 调用此函数。
    """
    global _client
    if _client is not None:
        try:
            await _client.close()
        except Exception:
            pass
        _client = None


# ========================================
# 便捷函数
# ========================================
async def expand_keywords(keyword: str, count: int = 50) -> list[dict]:
    """拓展长尾关键词"""
    client = await get_5118_client()
    result = await client.mine_longtail_keywords(keyword, page_size=min(count, 100))
    return result.get("keywords", [])


async def get_keyword_value(keywords: list[str]) -> list[dict]:
    """获取关键词价值信息"""
    client = await get_5118_client()
    return await client.batch_analyze_keywords(keywords)


async def quick_quote(keywords: list[str]) -> dict:
    """快速批量报价"""
    results = await get_keyword_value(keywords)
    
    total_price = sum(r["suggested_price"] for r in results)
    
    count = len(keywords)
    if count > 20:
        discount = 0.8
    elif count > 10:
        discount = 0.85
    elif count > 5:
        discount = 0.9
    else:
        discount = 1.0
    
    final_price = int(total_price * discount)
    
    return {
        "keywords": results,
        "total_price": total_price,
        "discount": discount,
        "final_price": final_price,
        "avg_price": final_price // len(keywords) if keywords else 0
    }
