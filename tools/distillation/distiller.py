"""
蒸馏系统 V4.2 — LLM 蒸馏管道

核心模块：将多平台 AI 回复按关键词聚合，用 LLM 提取结构化竞争情报。

功能：
- 蒸馏 Prompt 模板 + 品牌归一化指令
- pydantic JSON Schema 校验
- 单关键词蒸馏函数（含 3 次重试 + 降级方案）
- 批量蒸馏（asyncio.Semaphore 并发控制）

调用方式：
    from tools.distillation.distiller import distill_batch
    results = await distill_batch(keyword_groups, brand_name, known_brands)
"""

import json
import re
import os
import asyncio
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

import httpx
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("distiller")


# ==========================================
# 1. Pydantic Schema（LLM 输出校验）
# ==========================================

class BrandInfo(BaseModel):
    """LLM 识别的单个品牌信息"""
    name: str = Field(description="品牌名称（归一化后）")
    platforms_mentioned: List[str] = Field(default_factory=list, description="出现在哪些平台")
    ranks: Dict[str, int] = Field(default_factory=dict, description="各平台排名 {platform: rank}")
    recommendation_reasons: List[str] = Field(default_factory=list, description="被推荐的理由")
    description_keywords: List[str] = Field(default_factory=list, description="描述中的关键词")


class SourceInfo(BaseModel):
    """LLM 识别的信源信息"""
    url_or_reference: str = Field(description="URL 或引用描述")
    domain: Optional[str] = Field(default=None, description="域名")
    content_type: str = Field(default="未知", description="内容类型")
    cited_by_platforms: List[str] = Field(default_factory=list, description="被哪些平台引用")


class PlatformPreferences(BaseModel):
    """各平台偏好"""
    dominant_structure: str = Field(default="", description="主导结构")
    platform_preferences: Dict[str, str] = Field(default_factory=dict, description="平台偏好描述")


class ClientPosition(BaseModel):
    """客户品牌在此关键词的表现"""
    mentioned_in: int = Field(default=0, description="被提及的平台数")
    total_platforms: int = Field(default=0, description="总平台数")
    best_rank: Optional[int] = Field(default=None, description="最佳排名")
    worst_rank: Optional[int] = Field(default=None, description="最差排名")
    cited_strengths: List[str] = Field(default_factory=list, description="被引用的优势")
    gaps_vs_competitors: List[str] = Field(default_factory=list, description="与竞品的差距")


class DistillationResult(BaseModel):
    """LLM 蒸馏输出的完整 Schema"""
    keyword: str = Field(description="关键词")
    brands: List[BrandInfo] = Field(default_factory=list, description="识别的品牌列表")
    sources_cited: List[SourceInfo] = Field(default_factory=list, description="引用信源")
    response_patterns: PlatformPreferences = Field(default_factory=PlatformPreferences)
    client_position: ClientPosition = Field(default_factory=ClientPosition)
    optimization_hints: List[str] = Field(default_factory=list, description="优化建议")
    new_brands_discovered: List[str] = Field(default_factory=list, description="新发现的品牌")

    @field_validator('brands', mode='before')
    @classmethod
    def ensure_brands_list(cls, v):
        if isinstance(v, dict):
            return [v]
        return v or []


# ==========================================
# 2. Prompt 模板
# ==========================================

DISTILLATION_SYSTEM_PROMPT = """你是一个专业的 AI 搜索竞争情报分析师。

## 任务
分析同一关键词在不同 AI 搜索平台的回复，提取结构化竞争情报。

## 分析要求

### 品牌识别
1. 提取每条回复中被推荐/提及的所有公司/品牌
2. 记录每个品牌在每个平台的推荐排名（第几位被提及）
3. 提取每个品牌被推荐的理由（WHY）
4. **品牌归一化**：如果识别到的品牌名与"已知品牌列表"中的名称高度相似，请归一化为标准名称。不在列表中的新品牌原样输出。

### 信源分析
1. 提取回复中引用的 URL、文章、报告等
2. 区分直接引用（有 URL）和间接引用（"据某报告"）
3. 记录每个信源被哪些平台引用
4. **重要**：不要把纯数字引用编号（如 [1]、[2]、[3] 等）当作信源。只记录有实际名称或 URL 的信源。间接引用至少要有描述性名称（如"某行业报告"）

### 回复模式
1. 分析各平台回复的结构特征（列表、评分、对比等）
2. 总结各平台的内容偏好

### 客户品牌分析
1. 分析客户品牌在各平台的表现
2. 找出客户品牌的被引优势
3. 对比竞品，找出客户品牌的差距

### 优化建议
1. 基于分析给出具体的、可操作的优化建议
2. 建议必须有数据依据（如"在 X 平台排名第 Y，因为 Z"）

## 输出格式
严格输出 JSON，不要任何解释文字。结构：
```json
{
  "keyword": "关键词",
  "brands": [
    {
      "name": "品牌标准名",
      "platforms_mentioned": ["平台1", "平台2"],
      "ranks": {"平台1": 1, "平台2": 3},
      "recommendation_reasons": ["理由1", "理由2"],
      "description_keywords": ["关键特征1", "关键特征2"]
    }
  ],
  "sources_cited": [
    {
      "url_or_reference": "URL或引用描述",
      "domain": "域名或null",
      "content_type": "内容类型",
      "cited_by_platforms": ["平台1"]
    }
  ],
  "response_patterns": {
    "dominant_structure": "主导回复结构",
    "platform_preferences": {
      "doubao": "偏好描述",
      "kimi": "偏好描述"
    }
  },
  "client_position": {
    "mentioned_in": 3,
    "total_platforms": 4,
    "best_rank": 1,
    "worst_rank": 3,
    "cited_strengths": ["优势1"],
    "gaps_vs_competitors": ["差距1"]
  },
  "optimization_hints": ["建议1", "建议2"],
  "new_brands_discovered": ["不在已知列表中的新品牌"]
}
```"""


def build_user_prompt(
    keyword: str,
    client_brand: str,
    known_brands: List[str],
    platform_responses: Dict[str, str],
) -> str:
    """构建蒸馏用户 Prompt"""
    brands_str = ", ".join(known_brands) if known_brands else "无"
    
    prompt_parts = [
        f"## 关键词: {keyword}",
        f"## 客户品牌: {client_brand}",
        f"## 已知品牌列表（请归一化）: {brands_str}",
        "",
    ]
    
    for platform, response in platform_responses.items():
        # 截断过长的响应（防止超 token 限制）
        truncated = response[:4000] if len(response) > 4000 else response
        prompt_parts.append(f"### [平台: {platform}]")
        prompt_parts.append(truncated)
        prompt_parts.append("")
    
    prompt_parts.append("请严格按 JSON 格式输出分析结果。")
    
    return "\n".join(prompt_parts)


# ==========================================
# 3. LLM 调用 + 容错
# ==========================================

DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen3.7-max"
MAX_RETRIES = 3
CONCURRENCY_LIMIT = 5  # 最多 5 个并发蒸馏


async def _call_llm(
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    timeout: float = 120.0,
) -> Optional[str]:
    """调用 DashScope LLM API"""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        logger.error("未配置 DASHSCOPE_API_KEY")
        return None
    
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with llm_track("distillation", "dashscope", model=model) as tracker:
                response = await client.post(
                    DASHSCOPE_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )
                if response.status_code == 200:
                    result = response.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(result)
                    tracker.record(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        success=True,
                    )
                    content = result["choices"][0]["message"]["content"]
                    usage = result.get("usage", {})
                    total_tokens = usage.get("total_tokens", 0)
                    logger.info(f"LLM 调用成功，tokens: {total_tokens}")
                    return content
                else:
                    tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                    logger.error(f"LLM API 错误: HTTP {response.status_code} - {response.text[:200]}")
                    return None
    
    except httpx.TimeoutException:
        logger.error("LLM 调用超时")
        return None
    except Exception as e:
        logger.error(f"LLM 调用异常: {e}")
        return None


def _parse_json_from_llm(raw_content: str) -> Optional[dict]:
    """从 LLM 输出中解析 JSON（处理 markdown 包裹、思考过程等）"""
    if not raw_content:
        return None
    
    content = raw_content.strip()
    
    # 去掉 <think>...</think> 思考过程
    if "</think>" in content:
        content = content.split("</think>")[-1].strip()
    
    # 去掉 BOM 和不可见字符
    content = content.lstrip('\ufeff\u200b\u200c\u200d')
    
    # 去掉所有 markdown 代码块标记（不只是末尾）
    content = re.sub(r'```(?:json)?\s*\n?', '', content)
    content = content.strip()
    
    # 尝试直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.debug(f"直接解析失败: {e}")
    
    # 使用括号平衡法提取最外层 JSON 对象
    start_idx = content.find('{')
    if start_idx != -1:
        depth = 0
        in_string = False
        escape = False
        end_idx = -1
        
        for i in range(start_idx, len(content)):
            ch = content[i]
            if escape:
                escape = False
                continue
            if ch == '\\' and in_string:
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        
        if end_idx > start_idx:
            json_str = content[start_idx:end_idx + 1]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                # 尝试修复常见错误：尾部多余逗号
                fixed = re.sub(r',(\s*[}\]])', r'\1', json_str)
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError:
                    logger.warning(f"括号平衡提取后仍解析失败: {e}, 内容前200字: {json_str[:200]}")
    
    # 最后尝试：贪婪正则
    json_match = re.search(r'\{[\s\S]*\}', content)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    
    logger.warning(f"JSON 解析失败，原始内容前 200 字: {content[:200]}")
    return None


def _create_fallback_result(keyword: str, platform_responses: Dict[str, str]) -> DistillationResult:
    """降级方案：至少记录基础信息"""
    return DistillationResult(
        keyword=keyword,
        brands=[],
        sources_cited=[],
        response_patterns=PlatformPreferences(
            dominant_structure="降级-未分析",
            platform_preferences={p: "降级-未分析" for p in platform_responses},
        ),
        client_position=ClientPosition(
            total_platforms=len(platform_responses),
        ),
        optimization_hints=["蒸馏失败，请检查 LLM 配置后重新运行"],
    )


async def distill_keyword(
    keyword: str,
    client_brand: str,
    known_brands: List[str],
    platform_responses: Dict[str, str],
    model: str = DEFAULT_MODEL,
    min_platforms: int = 2,
) -> tuple[DistillationResult, int, str]:
    """
    蒸馏单个关键词（核心函数）
    
    Args:
        keyword: 监测关键词
        client_brand: 客户品牌名
        known_brands: 已知品牌列表（用于归一化）
        platform_responses: {platform_name: full_response}
        model: LLM 模型
        min_platforms: 最少平台数
    
    Returns:
        (result, tokens_used, quality_flag)
    """
    # 过滤有效响应
    valid_responses = {
        p: r for p, r in platform_responses.items()
        if r and len(r.strip()) > 100
    }
    
    if len(valid_responses) < min_platforms:
        logger.warning(f"[{keyword}] 有效平台数不足: {len(valid_responses)} < {min_platforms}")
        return _create_fallback_result(keyword, platform_responses), 0, "degraded"
    
    # 构建 Prompt
    user_prompt = build_user_prompt(keyword, client_brand, known_brands, valid_responses)
    
    # 重试逻辑
    for attempt in range(MAX_RETRIES):
        raw_content = await _call_llm(
            system_prompt=DISTILLATION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model,
        )
        
        if not raw_content:
            logger.warning(f"[{keyword}] 第 {attempt + 1} 次 LLM 调用返回空")
            continue
        
        # 解析 JSON
        parsed = _parse_json_from_llm(raw_content)
        if not parsed:
            logger.warning(f"[{keyword}] 第 {attempt + 1} 次 JSON 解析失败")
            continue
        
        # pydantic 校验
        try:
            result = DistillationResult(**parsed)
            # 估算 tokens（精确值需要从 API 返回获取）
            tokens_est = len(user_prompt) // 2 + len(raw_content) // 2
            logger.info(f"[{keyword}] 蒸馏成功: {len(result.brands)} 品牌, {len(result.sources_cited)} 信源")
            return result, tokens_est, "auto"
        except Exception as e:
            logger.warning(f"[{keyword}] 第 {attempt + 1} 次 Schema 校验失败: {e}")
            continue
    
    # 全部失败 → 降级
    logger.error(f"[{keyword}] 蒸馏失败（{MAX_RETRIES} 次重试），使用降级结果")
    return _create_fallback_result(keyword, valid_responses), 0, "degraded"


# ==========================================
# 4. 批量蒸馏（并发控制）
# ==========================================

async def distill_batch(
    keyword_groups: Dict[str, Dict[str, str]],
    client_brand: str,
    known_brands: List[str],
    model: str = DEFAULT_MODEL,
    concurrency: int = CONCURRENCY_LIMIT,
) -> List[tuple[str, DistillationResult, int, str]]:
    """
    批量蒸馏多个关键词
    
    Args:
        keyword_groups: {keyword: {platform: full_response}}
        client_brand: 客户品牌名
        known_brands: 已知品牌列表
        model: LLM 模型
        concurrency: 最大并发数
    
    Returns:
        [(keyword, result, tokens, quality_flag), ...]
    """
    semaphore = asyncio.Semaphore(concurrency)
    results = []
    total = len(keyword_groups)
    completed = 0
    
    async def _process_one(keyword: str, responses: Dict[str, str]):
        nonlocal completed
        async with semaphore:
            result, tokens, quality = await distill_keyword(
                keyword=keyword,
                client_brand=client_brand,
                known_brands=known_brands,
                platform_responses=responses,
                model=model,
            )
            completed += 1
            print(f"  [{completed}/{total}] {keyword}: "
                  f"{len(result.brands)} 品牌, {quality}")
            return keyword, result, tokens, quality
    
    tasks = [
        _process_one(kw, resp)
        for kw, resp in keyword_groups.items()
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 过滤掉异常
    valid_results = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"蒸馏任务异常: {r}")
        else:
            valid_results.append(r)
    
    succeeded = sum(1 for _, _, _, q in valid_results if q != "degraded")
    degraded = sum(1 for _, _, _, q in valid_results if q == "degraded")
    total_tokens = sum(t for _, _, t, _ in valid_results)
    
    print(f"\n📊 批量蒸馏完成: {succeeded} 成功, {degraded} 降级, "
          f"共 {total_tokens} tokens")
    
    return valid_results
