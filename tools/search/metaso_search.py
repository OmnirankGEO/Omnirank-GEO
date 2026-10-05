"""
秘塔搜索 API 工具
用于网页搜索和学术搜索，获取 GEO 内容素材
"""

import httpx
import json
from typing import Literal
from agentscope.tool import ToolResponse

import sys
sys.path.append('../..')
from config.model_config import METASO_CONFIG


async def metaso_search(
    query: str,
    scope: Literal["webpage", "scholar"] = "webpage",
    include_summary: bool = True,
    size: int = 20,
    include_raw_content: bool = False,
    concise_snippet: bool = True
) -> ToolResponse:
    """
    搜索统一入口 - 网页或学术搜索(签名与返回结构不变)

    [搜索 Provider 适配层 2026-07-28] scope=webpage 时按场景键路由;默认配置
    秘塔直连逐字节一致;scope=scholar 永远秘塔(B 级能力,豆包没有);
    豆包异常/空结果 → 原参数原样重放秘塔。
    """
    if scope == "webpage":
        from tools.search.provider_router import resolve_provider

        if resolve_provider() == "doubao":
            try:
                from tools.search.doubao_search import doubao_search_webpages

                # include_raw_content 原样尊重(同 metaso_mcp 分支的理由:消费方会截断
                # 序列化 JSON 喂 LLM,无视该参数会让召回条数隐性退化)。
                rows = await doubao_search_webpages(
                    query,
                    size=max(1, min(int(size or 20), 50)),
                    need_content=bool(include_raw_content),
                )
                return ToolResponse(
                    content=[{"type": "text", "text": json.dumps({"webpages": rows}, ensure_ascii=False)}]
                )
            except Exception:
                pass  # 回退链:秘塔原参数重放
    return await _metaso_search_direct(
        query,
        scope=scope,
        include_summary=include_summary,
        size=size,
        include_raw_content=include_raw_content,
        concise_snippet=concise_snippet,
    )


async def _metaso_search_direct(
    query: str,
    scope: Literal["webpage", "scholar"] = "webpage",
    include_summary: bool = True,
    size: int = 20,
    include_raw_content: bool = False,
    concise_snippet: bool = True
) -> ToolResponse:
    """
    秘塔搜索 - 网页或学术搜索(直连实现 · 逻辑与历史版本逐字节一致)

    Args:
        query (str): 搜索查询语句
        scope (str): 搜索范围，"webpage"（网页）或 "scholar"（学术）
        include_summary (bool): 是否包含摘要，默认 True
        size (int): 返回结果数量，默认 20
        include_raw_content (bool): 是否包含原始内容，默认 False
        concise_snippet (bool): 是否使用简洁摘要，默认 True

    Returns:
        ToolResponse: 包含搜索结果的响应
    """
    api_key = METASO_CONFIG["api_key"]
    base_url = METASO_CONFIG["base_url"]
    endpoint = METASO_CONFIG["endpoints"]["search"]
    
    try:
        from tools.llm_call_tracker import llm_track
        from tools.metaso_health import metaso_result_error, record_metaso_result

        async with httpx.AsyncClient(timeout=60) as client:
            async with llm_track(
                "metaso_search",
                "metaso",
                model="search",
                metadata={"scope": scope, "size": size},
            ) as tracker:
                response = await client.post(
                    f"{base_url}{endpoint}",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    },
                    json={
                        "q": query,
                        "scope": scope,
                        "includeSummary": include_summary,
                        "size": size,
                        "includeRawContent": include_raw_content,
                        "conciseSnippet": concise_snippet
                    }
                )
                # [止血 2026-08-04] HTTP 200 不等于业务成功:秘塔余额不足等业务级失败
                #   走 200 通道(body 里 errCode=3000)。只判 status_code 会把断血记成 success。
                data = None
                if response.status_code != 200:
                    biz_err = f"HTTP {response.status_code}: {response.text[:200]}"
                else:
                    try:
                        data = response.json()
                    except Exception as parse_err:
                        data = None
                        biz_err = f"响应非 JSON: {parse_err}"
                    else:
                        biz_err = metaso_result_error(data)

                tracker.record(
                    success=biz_err is None,
                    error_msg=None if biz_err is None else str(biz_err)[:200],
                )
                record_metaso_result(biz_err is None, str(biz_err or ""), source="metaso_search")

            if response.status_code != 200:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error: HTTP {response.status_code} - {response.text}"}]
                )

            if biz_err:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error: 秘塔业务级失败 - {biz_err}"}]
                )

            return ToolResponse(
                content=[{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
            )
    except Exception as e:
        return ToolResponse(
            content=[{"type": "text", "text": f"Error: {str(e)}"}]
        )


async def search_web_for_geo(
    industry: str,
    keywords: str,
    company_name: str = ""
) -> ToolResponse:
    """
    为 GEO 内容采集网页素材
    
    Args:
        industry (str): 行业名称
        keywords (str): 关键词
        company_name (str): 公司名称（可选）
        
    Returns:
        ToolResponse: 包含搜索结果的响应
    """
    # 构建搜索查询
    query = f"{industry} {keywords} 最新数据 案例 2025"
    if company_name:
        query = f"{company_name} {query}"
    
    return await metaso_search(
        query=query,
        scope="webpage",
        size=20,
        include_summary=True
    )


async def search_scholar_for_geo(
    industry: str,
    keywords: str
) -> ToolResponse:
    """
    为 GEO 内容采集学术素材
    
    Args:
        industry (str): 行业名称
        keywords (str): 关键词
        
    Returns:
        ToolResponse: 包含学术搜索结果的响应
    """
    # 构建学术搜索查询
    query = f"{industry} {keywords} 研究报告 行业分析"
    
    return await metaso_search(
        query=query,
        scope="scholar",
        size=10,
        include_summary=True
    )
