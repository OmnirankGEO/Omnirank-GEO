"""
秘塔 MCP 搜索工具
基于 Model Context Protocol (MCP) 的智能搜索服务
支持: 网页搜索(100条)、网页读取、RAG问答
"""

import httpx

# [2026-08-08] 异常 → 永不为空的描述(单点)。原来写 str(e),而 SSLEOFError 的
# str() 是空串 → 消费方拿到 {"error": ""},看起来像成功。详见 tools/search/_err.py 抬头。
from tools.search._err import describe_exc
import json
import os
from typing import Optional, Literal
from agentscope.tool import ToolResponse
from tools.llm_call_tracker import llm_track


# MCP 配置
#
# [WO R2 2026-08-09 · 第三次咬人] 🔴 `api_key` 原来在**模块 import 时**就把
# `os.environ["METASO_API_KEY"]` 读死了。三个历史受害场景:
#
#   1. **admin 后台改 key 不生效** —— `config/settings_manager.py` 把设置写回
#      `os.environ["METASO_API_KEY"]`(:436),但本模块早就读完了,
#      必须**重启进程**才认。这是生产行为缺陷,不只是测试别扭。
#   2/3. 两个测试各自 `monkeypatch.setenv("METASO_API_KEY", ...)` —— 全是**死代码**
#      (setenv 晚于 import),用例实际靠 `.env` 里恰好有 key 才绿,
#      干净 CI 上会以另一种原因失败。
#
# 修法:字典里**不再预读 env**,改由 `_metaso_api_key()` 每次调用时现取。
#   · 默认路径:现取 env → admin 改完当场生效,不用重启;
#   · 官方测试接缝:`monkeypatch.setitem(METASO_MCP_CONFIG, "api_key", "…")` 覆盖;
#   · `monkeypatch.setenv` 现在**也真的有效**了 —— 那个坑对所有人一起消失。
METASO_MCP_CONFIG = {
    "url": "https://metaso.cn/api/mcp",
    # None = 未显式覆盖 → 走 env 现取。测试要固定值就 setitem 这里。
    "api_key": None,
}


def _metaso_api_key() -> str:
    """现取 API key:显式覆盖优先,否则读 env(不缓存)。"""
    override = METASO_MCP_CONFIG.get("api_key")
    if override:
        return str(override)
    return str(os.environ.get("METASO_API_KEY") or "")


async def metaso_web_search(
    query: str,
    scope: Literal["webpage", "document", "paper", "image", "video", "podcast"] = "webpage",
    include_summary: bool = True,
    include_raw_content: bool = False,
    size: int = 20
) -> ToolResponse:
    """
    网络搜索统一入口(签名与返回结构与历史版本一个字段不变)。

    [搜索 Provider 适配层 2026-07-28] 内部按场景键白名单路由:
    - 默认配置(无场景/无 SEARCH_PROVIDER_* env)→ 秘塔直连,与改前逐字节一致;
    - 场景解析为 doubao 且 scope=webpage → 豆包检索 + 结构 shim(双层 JSON wire
      形状与秘塔 MCP 完全一致,_v36_metaso_pages / extract_citations / agent_loop
      adapter 等消费方零改动);
    - 豆包异常/空结果 → 原参数原样重放秘塔(回退链)。
    """
    if scope == "webpage":
        from tools.search.provider_router import resolve_provider

        if resolve_provider() == "doubao":
            try:
                return await _doubao_web_search_as_metaso(
                    query, size=size, include_raw_content=include_raw_content,
                )
            except Exception:
                pass  # 回退链:豆包任何失败都原参数重放秘塔,不向消费方暴露差异
    return await _metaso_web_search_direct(
        query,
        scope=scope,
        include_summary=include_summary,
        include_raw_content=include_raw_content,
        size=size,
    )


async def _doubao_web_search_as_metaso(
    query: str, *, size: int, include_raw_content: bool = False,
) -> ToolResponse:
    """豆包检索 → 秘塔 MCP 双层 wire 形状(外层 status/results → 内层 webpages)。

    🔴 ``include_raw_content`` 必须原样尊重:秘塔在 False 时只回短摘要,消费方
    (industry_knowledge_collector 的 `_metaso_search_safe` 等)会把序列化 JSON
    **截前 2000 字**喂给 LLM —— 若无视该参数一律内联全文,一条正文就吃光窗口,
    8 条召回退化成 1 条,这是"结构一样但内容口径变了"的隐性质量回归。
    """
    from tools.search.doubao_search import doubao_search_webpages

    rows = await doubao_search_webpages(
        query,
        size=max(1, min(int(size or 20), 50)),
        need_content=bool(include_raw_content),
    )
    inner = json.dumps({"webpages": rows}, ensure_ascii=False)
    payload = {
        "status": "success",
        "query": query,
        "scope": "webpage",
        "results": [{"type": "text", "text": inner}],
        "count": len(rows),
    }
    return ToolResponse(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])


async def _metaso_web_search_direct(
    query: str,
    scope: Literal["webpage", "document", "paper", "image", "video", "podcast"] = "webpage",
    include_summary: bool = True,
    include_raw_content: bool = False,
    size: int = 20
) -> ToolResponse:
    """
    秘塔 MCP 网络搜索(直连实现 · 逻辑与历史版本逐字节一致)

    Args:
        query: 搜索关键词
        scope: 搜索范围 (webpage/document/paper/image/video/podcast)
        include_summary: 通过网页摘要信息提升召回率
        include_raw_content: 抓取所有来源网页原文 (注意：数据量大)
        size: 返回结果数量 (最大100)

    Returns:
        搜索结果列表
    """
    api_key = _metaso_api_key()
    
    if not api_key:
        return ToolResponse(
            content=[{"type": "text", "text": json.dumps({"error": "METASO_API_KEY not configured"})}]
        )
    
    import asyncio as _asyncio
    from tools.search.provider_circuit_breaker import (
        PROVIDER_METASO,
        is_undelivered,
        record_delivered,
        record_undelivered,
        should_skip,
    )

    max_retries = 2  # 从 3 次降到 2 次，最坏耗时 30×2+3=63s 而非 60×3+6=186s

    for attempt in range(max_retries):
        # [WO 熔断单 2026-08-08] 🔴 闸放在 **llm_track 之外、每次 attempt 之前**。
        #   放里面等于"先记一笔成本再决定要不要打" —— 那正是 454 笔 ¥17.24 的成因。
        #   闸也必须在**重试循环内**:一次逻辑调用的第 2 次重试同样是一笔钱。
        _skip, _skip_reason = should_skip(PROVIDER_METASO)
        if _skip:
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps(
                    {"error": f"metaso circuit open ({_skip_reason})"}
                )}]
            )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # MCP 工具调用格式
                async with llm_track(
                    "metaso_web_search",
                    "metaso",
                    model="search",
                    metadata={"scope": scope, "size": min(size, 100), "attempt": attempt + 1},
                ) as tracker:
                    response = await client.post(
                        METASO_MCP_CONFIG["url"],
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "metaso_web_search",
                                "arguments": {
                                    "q": query,
                                    "scope": scope,
                                    "includeSummary": include_summary,
                                    "includeRawContent": include_raw_content,
                                    "size": min(size, 100)  # 最大100条
                                }
                            }
                        }
                    )

                    # [WO 熔断单 2026-08-08] 拿到任何 HTTP 响应 = **送达**了 → 合闸。
                    #   500 / errCode≠0 都算送达:那是业务层的事,归 metaso_health 管,
                    #   不该让本熔断把供应商停掉(也不该白白挡住后面能成功的调用)。
                    record_delivered(PROVIDER_METASO)

                    if response.status_code != 200:
                        tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                        if attempt < max_retries - 1:
                            print(f"[Metaso] ⚠️ HTTP {response.status_code}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                            await _asyncio.sleep(3)
                            continue
                        return ToolResponse(
                            content=[{"type": "text", "text": json.dumps({
                                "error": f"HTTP {response.status_code}",
                                "detail": response.text[:500]
                            })}]
                        )

                    # 防御空响应
                    try:
                        data = response.json()
                    except (json.JSONDecodeError, ValueError):
                        tracker.record(success=False, error_msg="Invalid JSON response")
                        if attempt < max_retries - 1:
                            print(f"[Metaso] ⚠️ 响应JSON解析失败，{3}秒后重试 ({attempt + 1}/{max_retries})")
                            await _asyncio.sleep(3)
                            continue
                        return ToolResponse(
                            content=[{"type": "text", "text": json.dumps({"error": "Metaso返回空响应"})}]
                        )

                    # 处理 MCP 响应
                    if "result" in data:
                        result = data["result"]
                        tracker.record(success=True)
                        return ToolResponse(
                            content=[{"type": "text", "text": json.dumps({
                                "status": "success",
                                "query": query,
                                "scope": scope,
                                "results": result.get("content", result) if isinstance(result, dict) else result,
                                "count": len(result.get("content", [])) if isinstance(result, dict) else 0
                            }, ensure_ascii=False)}]
                        )
                    elif "error" in data:
                        tracker.record(success=False, error_msg=str(data["error"])[:200])
                        if attempt < max_retries - 1:
                            print(f"[Metaso] ⚠️ API错误: {data['error']}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                            await _asyncio.sleep(3)
                            continue
                        return ToolResponse(
                            content=[{"type": "text", "text": json.dumps({
                                "error": data["error"].get("message", str(data["error"]))
                            })}]
                        )
                    else:
                        tracker.record(success=True)
                        return ToolResponse(
                            content=[{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
                        )

        except Exception as e:
            # [WO 熔断单 2026-08-08] 只把「确定未送达」记进熔断计数。
            #   泛 except 里混着 JSON 解析错、超时读错等**已送达**的异常,
            #   把它们一起算上会用错判据停供应商。
            if is_undelivered(e):
                record_undelivered(PROVIDER_METASO, f"{type(e).__name__}: {str(e)[:120]}")
            if attempt < max_retries - 1:
                print(f"[Metaso] ⚠️ 异常: {e}，{3}秒后重试 ({attempt + 1}/{max_retries})")
                await _asyncio.sleep(3)
                continue
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps({"error": describe_exc(e)})}]
            )


async def metaso_web_reader(
    url: str,
    format: Literal["json", "markdown"] = "markdown"
) -> ToolResponse:
    """
    秘塔 MCP 网页内容读取
    
    Args:
        url: 要读取的URL地址
        format: 输出格式 (json/markdown)
    
    Returns:
        网页内容
    """
    api_key = _metaso_api_key()
    
    if not api_key:
        return ToolResponse(
            content=[{"type": "text", "text": json.dumps({"error": "METASO_API_KEY not configured"})}]
        )
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with llm_track(
                "metaso_web_reader",
                "metaso",
                model="reader",
                metadata={"format": format},
            ) as tracker:
                response = await client.post(
                    METASO_MCP_CONFIG["url"],
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "metaso_web_reader",
                            "arguments": {
                                "url": url,
                                "format": format
                            }
                        }
                    }
                )

                if response.status_code != 200:
                    tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                    return ToolResponse(
                        content=[{"type": "text", "text": json.dumps({
                            "error": f"HTTP {response.status_code}"
                        })}]
                    )

                data = response.json()
            
                if "result" in data:
                    tracker.record(success=True)
                    return ToolResponse(
                        content=[{"type": "text", "text": json.dumps({
                            "status": "success",
                            "url": url,
                            "content": data["result"]
                        }, ensure_ascii=False)}]
                    )
                else:
                    tracker.record(success=True)
                    return ToolResponse(
                        content=[{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
                    )
                
    except Exception as e:
        return ToolResponse(
            content=[{"type": "text", "text": json.dumps({"error": describe_exc(e)})}]
        )


async def metaso_chat(
    message: str,
    model: str = "fast"
) -> ToolResponse:
    """
    秘塔 MCP 智能问答 (基于RAG)
    
    Args:
        message: 用户问题
        model: 使用的模型 (fast/default)
    
    Returns:
        AI 回答
    """
    api_key = _metaso_api_key()
    
    if not api_key:
        return ToolResponse(
            content=[{"type": "text", "text": json.dumps({"error": "METASO_API_KEY not configured"})}]
        )
    
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with llm_track(
                "metaso_chat",
                "metaso",
                model="chat",
                metadata={"metaso_model": model},
            ) as tracker:
                response = await client.post(
                    METASO_MCP_CONFIG["url"],
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "metaso_chat",
                            "arguments": {
                                "message": message,
                                "model": model
                            }
                        }
                    }
                )

                if response.status_code != 200:
                    tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                    return ToolResponse(
                        content=[{"type": "text", "text": json.dumps({
                            "error": f"HTTP {response.status_code}"
                        })}]
                    )

                data = response.json()
            
                if "result" in data:
                    tracker.record(success=True)
                    return ToolResponse(
                        content=[{"type": "text", "text": json.dumps({
                            "status": "success",
                            "message": message,
                            "answer": data["result"]
                        }, ensure_ascii=False)}]
                    )
                else:
                    tracker.record(success=True)
                    return ToolResponse(
                        content=[{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
                    )
                
    except Exception as e:
        return ToolResponse(
            content=[{"type": "text", "text": json.dumps({"error": describe_exc(e)})}]
        )


# ========================================
# 引用提取工具函数
# ========================================

# 权威来源域名列表
AUTHORITY_DOMAINS = [
    '36kr.com', 'zhihu.com', 'baike.baidu.com', 'wikipedia.org',
    'huxiu.com', 'sina.com', 'sohu.com', 'toutiao.com', 
    'weixin.qq.com', 'mp.weixin.qq.com',
    'cnki.net', 'wanfangdata.com', 'gov.cn',
    'thepaper.cn', 'jiemian.com', 'caixin.com', 'yicai.com',
    'kr-asia.com', 'technode.com', 'pandaily.com'
]


def extract_domain(url: str) -> str:
    """从URL提取域名"""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc
        # 移除www前缀
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except:
        return url


def is_authority_source(url: str) -> bool:
    """判断是否为权威来源"""
    domain = extract_domain(url).lower()
    return any(auth_domain in domain for auth_domain in AUTHORITY_DOMAINS)


def extract_citations(results: list) -> dict:
    """
    从秘塔搜索结果中提取引用链接
    
    Args:
        results: 搜索结果列表（可能是嵌套结构）
        
    Returns:
        {
            'citations': [...],           # 完整引用列表
            'authority_sources': [...],   # 权威来源列表
            'authority_count': int        # 权威来源数量
        }
    """
    citations = []
    authority_sources = []
    
    # 处理可能的嵌套结构
    items = []
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                # 检查是否是文本类型的MCP响应
                if item.get('type') == 'text':
                    try:
                        text_content = item.get('text', '')
                        if isinstance(text_content, str):
                            parsed = json.loads(text_content)
                            if isinstance(parsed, list):
                                items.extend(parsed)
                            elif isinstance(parsed, dict) and 'webpages' in parsed:
                                items.extend(parsed['webpages'])
                    except:
                        pass
                else:
                    items.append(item)
            elif isinstance(item, list):
                items.extend(item)
    elif isinstance(results, dict):
        if 'webpages' in results:
            items = results['webpages']
        elif 'content' in results:
            items = results['content']
    
    # 提取引用信息
    for item in items:
        if not isinstance(item, dict):
            continue
            
        url = item.get('link', '') or item.get('url', '')
        if not url:
            continue
            
        # 判断是否权威来源
        is_auth = is_authority_source(url)
        
        citation = {
            'title': item.get('title', ''),
            'url': url,
            'snippet': item.get('snippet', '') or item.get('description', ''),
            'source': extract_domain(url),
            'date': item.get('date', '') or item.get('publishedDate', ''),
            'authors': item.get('authors', []),
            'is_authority': is_auth,
            'position': item.get('position', len(citations) + 1)
        }
        citations.append(citation)
        
        # 记录权威来源
        if is_auth:
            source = extract_domain(url)
            if source not in authority_sources:
                authority_sources.append(source)
    
    return {
        'citations': citations,
        'authority_sources': authority_sources,
        'authority_count': len(authority_sources)
    }


async def metaso_search_with_citations(
    query: str,
    scope: Literal["webpage", "document", "paper", "image", "video", "podcast"] = "webpage",
    size: int = 30
) -> dict:
    """
    秘塔搜索并提取完整引用信息
    
    Args:
        query: 搜索关键词
        scope: 搜索范围
        size: 结果数量
        
    Returns:
        {
            'query': str,
            'scope': str,
            'result_count': int,
            'citations': [...],
            'authority_sources': [...],
            'authority_count': int,
            'source': 'metaso_mcp'
        }
    """
    # 调用基础搜索
    # 🔴 [B 级钉死 2026-07-28] 引用链保持秘塔直连:即使调用方处于场景灰度上下文
    # (如 evidence),这里也**不进 provider 路由**(工单 §1-B:引用结构耦合,不可替)。
    # Evidence 场景要吃豆包走 provider_router.citation_search,不从本函数换源。
    response = await _metaso_web_search_direct(
        query=query,
        scope=scope,
        include_summary=True,
        include_raw_content=False,
        size=size
    )
    
    # 解析响应
    try:
        content = response.content[0] if response.content else {}
        if isinstance(content, dict) and content.get('type') == 'text':
            result_data = json.loads(content.get('text', '{}'))
        else:
            result_data = content
    except:
        result_data = {}
    
    # 提取引用
    results = result_data.get('results', [])
    citation_data = extract_citations(results)
    
    return {
        'query': query,
        'scope': scope,
        'result_count': len(citation_data['citations']),
        'citations': citation_data['citations'],
        'authority_sources': citation_data['authority_sources'],
        'authority_count': citation_data['authority_count'],
        'source': 'metaso_mcp'
    }


async def metaso_scholar_search(query: str, size: int = 20) -> dict:
    """
    秘塔学术搜索
    
    Args:
        query: 搜索关键词
        size: 结果数量
        
    Returns:
        学术论文搜索结果（包含引用）
    """
    return await metaso_search_with_citations(query, scope="paper", size=size)


async def metaso_document_search(query: str, size: int = 20) -> dict:
    """
    秘塔文库搜索
    
    Args:
        query: 搜索关键词
        size: 结果数量
        
    Returns:
        文库搜索结果（包含引用）
    """
    return await metaso_search_with_citations(query, scope="document", size=size)


async def metaso_industry_analysis(
    brand_name: str,
    industry: str,
    questions: list = None
) -> dict:
    """
    使用秘塔RAG问答进行行业分析
    
    Args:
        brand_name: 品牌名称
        industry: 行业名称
        questions: 自定义问题列表（可选）
        
    Returns:
        {
            'brand_name': str,
            'industry': str,
            'insights': [
                {'question': str, 'answer': str, 'key_points': [str]},
                ...
            ],
            'top_brands': [str],           # 识别的头部品牌
            'industry_trends': [str],      # 行业趋势
            'recommendations': [str]       # GEO优化建议
        }
    """
    # 默认行业分析问题
    if questions is None:
        questions = [
            f"搜索'{industry}'时，有哪些品牌或公司经常被提及？列出前5个",
            f"{industry}行业的头部企业有哪些？他们的核心竞争优势是什么？",
            f"{industry}行业近期有什么重要趋势或变化？"
        ]
    
    insights = []
    top_brands = []
    industry_trends = []
    
    for question in questions:
        try:
            response = await metaso_chat(question, model="fast")
            
            # 解析响应 - 增强处理嵌套JSON
            try:
                content = response.content[0] if response.content else {}
                if isinstance(content, dict) and content.get('type') == 'text':
                    result_data = json.loads(content.get('text', '{}'))
                else:
                    result_data = content
            except:
                result_data = {}
            
            answer = ""
            if isinstance(result_data, dict):
                answer = result_data.get('answer', '')
                
                # 如果answer是字符串但看起来像JSON，尝试解析
                if isinstance(answer, str) and answer.strip().startswith('{'):
                    try:
                        answer_parsed = json.loads(answer)
                        # 提取summary或其他文本字段
                        if isinstance(answer_parsed, dict):
                            answer = answer_parsed.get('summary', '') or answer_parsed.get('text', '')
                    except:
                        pass
                
                # 如果answer仍然包含JSON格式的citations，清理它
                if isinstance(answer, str) and '"citations"' in answer:
                    # 移除citations部分，只保留开头的摘要
                    import re
                    # 尝试提取纯文本部分
                    clean_match = re.match(r'^([^{]+)', answer)
                    if clean_match:
                        answer = clean_match.group(1).strip()
                    else:
                        # 如果整个都是JSON，尝试解析并提取
                        try:
                            data = json.loads(answer)
                            if isinstance(data, dict):
                                # 尝试获取摘要类字段
                                answer = data.get('summary', '') or data.get('answer', '') or data.get('text', '')
                            elif isinstance(data, list) and data:
                                # 如果是列表，连接所有summary
                                summaries = [item.get('summary', '') for item in data if isinstance(item, dict)]
                                answer = ' '.join(filter(None, summaries))
                        except:
                            pass
                
                if isinstance(answer, dict):
                    # MCP响应可能嵌套
                    answer = answer.get('content', [])
                    if isinstance(answer, list):
                        answer = ' '.join(
                            item.get('text', '') 
                            for item in answer 
                            if isinstance(item, dict)
                        )
            
            # 提取关键点（简单分句）
            key_points = []
            if answer:
                sentences = answer.replace('。', '.|').replace('；', ';|').split('|')
                key_points = [s.strip() for s in sentences if len(s.strip()) > 10][:5]
                
                # 尝试识别品牌名（简单正则）
                import re
                # 匹配中文公司/品牌名
                brand_pattern = r'[一-龥]{2,6}(?:科技|公司|集团|网络|软件|互联|传媒|咨询|服务)'
                found_brands = re.findall(brand_pattern, answer)
                for b in found_brands[:5]:
                    if b not in top_brands and b != brand_name:
                        top_brands.append(b)
                
                # 识别趋势关键词
                trend_keywords = ['趋势', '发展', '增长', '变化', '创新', '未来', '升级', '转型']
                for sentence in sentences:
                    if any(kw in sentence for kw in trend_keywords) and len(sentence) > 15:
                        if sentence.strip() not in industry_trends:
                            industry_trends.append(sentence.strip())
            
            insights.append({
                'question': question,
                'answer': answer[:1000] if answer else "未获取到回答",  # 截断过长回答
                'key_points': key_points
            })
            
        except Exception as e:
            insights.append({
                'question': question,
                'answer': f"分析失败: {str(e)}",
                'key_points': []
            })
    
    # 生成GEO优化建议
    recommendations = []
    if top_brands:
        recommendations.append(f"学习头部品牌 {', '.join(top_brands[:3])} 的内容策略")
    if industry_trends:
        recommendations.append("围绕行业趋势创作权威内容，提升AI引用潜力")
    recommendations.append(f"在知乎/百科等平台建立'{industry}'相关词条")
    
    return {
        'brand_name': brand_name,
        'industry': industry,
        'insights': insights,
        'top_brands': top_brands[:5],
        'industry_trends': industry_trends[:3],
        'recommendations': recommendations
    }


# 导出
__all__ = [
    "metaso_web_search", 
    "metaso_web_reader", 
    "metaso_chat",
    "metaso_search_with_citations",
    "metaso_scholar_search",
    "metaso_document_search",
    "metaso_industry_analysis",
    "extract_citations",
    "is_authority_source",
    "AUTHORITY_DOMAINS"
]
