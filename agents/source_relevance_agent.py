"""
来源相关性验证 Agent (Source Relevance Agent)
验证搜索结果与品牌的真实相关性，过滤误判的无关引用

核心功能:
1. 检查品牌名出现位置（标题/正文/无关）
2. 判断是否同名异义（如"全域上榜"是行业热词还是公司名）
3. 返回相关性评分和判定
"""

import json
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
from typing import Dict, List, Any
import agentscope
from agentscope.message import Msg


async def verify_citation_relevance(
    brand_name: str,
    company_full_name: str,
    industry: str,
    citation: Dict[str, Any]
) -> Dict[str, Any]:
    """
    验证单条引用与品牌的相关性
    
    Args:
        brand_name: 品牌名/公司简称 (如 "全域上榜")
        company_full_name: 公司全称 (如 "全域上榜（深圳）科技有限公司")
        industry: 行业 (如 "搜索流量优化")
        citation: 引用数据 {title, url, snippet, source}
        
    Returns:
        {
            'is_relevant': bool,           # 是否相关
            'relevance_score': float,      # 相关性分数 0-1
            'relevance_type': str,         # 'direct'(直接相关) / 'industry'(行业参考) / 'irrelevant'(无关)
            'reason': str,                 # 判断理由
            'citation': dict               # 原始引用数据
        }
    """
    title = citation.get('title', '')
    snippet = citation.get('snippet', '')
    url = citation.get('url', '')
    
    # 快速过滤：如果品牌名完全不出现在标题和摘要中
    content = f"{title} {snippet}".lower()
    brand_lower = brand_name.lower()
    
    # 构建验证prompt
    prompt = f"""你是一个品牌相关性验证专家。请判断以下搜索结果是否与目标品牌相关。

## 目标品牌
- 品牌简称: {brand_name}
- 公司全称: {company_full_name or f'{brand_name}公司'}
- 所属行业: {industry}

## 搜索结果
- 标题: {title}
- 摘要: {snippet[:300] if snippet else '无'}
- 链接: {url}

## 重要提示
品牌名"{brand_name}"可能是由常用词组成的（如"全域"+"上榜"），需要判断：
1. 搜索结果中提到的"{brand_name}"是指这家公司，还是只是新闻标题中的常用词组合？
2. 如果是"XX全域上榜"这种描述某地区全部入选的新闻，与"{brand_name}"公司无关

## 请输出JSON格式
```json
{{
    "is_relevant": true/false,
    "relevance_score": 0.0-1.0,
    "relevance_type": "direct/industry/irrelevant",
    "reason": "判断理由（简洁）"
}}
```

relevance_type说明：
- "direct": 直接与{brand_name}公司相关的报道或内容
- "industry": 与{industry}行业相关但非本公司的参考资料
- "irrelevant": 完全无关（同名异义等）"""

    try:
        # 调用LLM进行判断
        from agentscope.models import load_model_by_config_name
        model = load_model_by_config_name("deepseek_model")
        
        response = model(
            [{"role": "user", "content": prompt}],
            parse="json"
        )
        
        if response.parsed:
            result = response.parsed
        else:
            # 尝试从文本中提取JSON
            import re
            json_match = re.search(r'\{[^{}]+\}', response.text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                # 默认返回行业相关
                result = {
                    "is_relevant": True,
                    "relevance_score": 0.5,
                    "relevance_type": "industry",
                    "reason": "无法自动判断，默认为行业参考"
                }
        
        result['citation'] = citation
        return result
        
    except Exception as e:
        # 发生错误时保守处理，标记为行业相关
        return {
            'is_relevant': True,
            'relevance_score': 0.5,
            'relevance_type': 'industry',
            'reason': f'验证过程出错: {str(e)[:50]}',
            'citation': citation
        }


async def batch_verify_citations_with_llm(
    brand_name: str,
    company_full_name: str,
    industry: str,
    citations: List[Dict[str, Any]],
    batch_size: int = 10
) -> Dict[str, Any]:
    """
    批量LLM验证引用相关性（高效版本）
    
    核心策略：
    1. 一次LLM调用验证多条引用（减少API调用次数）
    2. LLM同时识别客户业务和判断引用相关性
    3. 返回分类结果
    
    Args:
        brand_name: 品牌名/公司简称
        company_full_name: 公司全称
        industry: 行业
        citations: 引用列表
        batch_size: 每批验证数量（默认10）
        
    Returns:
        {
            'brand_direct': [...],      # 品牌直接引用（用于评分）
            'industry_reference': [...], # 行业参考（展示但不计分）
            'filtered': [...],           # 过滤掉的
            'stats': {...}
        }
    """
    all_brand_direct = []    # 品牌直接引用
    all_industry_ref = []    # 行业参考资料
    all_filtered = []        # 无关内容
    
    # 分批处理
    for i in range(0, len(citations), batch_size):
        batch = citations[i:i + batch_size]
        
        # 构建批量验证prompt
        citations_text = ""
        for idx, c in enumerate(batch, 1):
            title = c.get('title', '')[:80]
            snippet = (c.get('snippet', '') or '')[:100]
            citations_text += f"\n[{idx}] 标题: {title}\n    摘要: {snippet}\n"
        
        prompt = f"""你是品牌相关性验证专家。请判断以下搜索结果与目标品牌的关系。

## 客户信息
- 品牌名称: {brand_name}
- 公司全称: {company_full_name or f'{brand_name}公司'}
- 主营业务: {industry}

## 三层分类标准（非常重要！）

### 🔴 品牌直接引用 (brand_direct)
标题或正文中**明确提及品牌名"{brand_name}"**，且内容与{industry}业务相关。
例如："{brand_name}公司发布新产品"、"访谈{brand_name}创始人"

### 🟡 行业参考资料 (industry_reference)
与{industry}行业相关，但**未提及品牌名"{brand_name}"**。
这类内容只能作为参考资料，不能证明品牌曝光。
例如："{industry}行业报告"、"如何做好{industry}"

### ⚫ 无关内容 (irrelevant)
与品牌和业务都无关，或品牌名只是词语巧合。

## 待验证的搜索结果
{citations_text}

## 请输出JSON格式
```json
{{
    "brand_direct_ids": [1, 2],      // 明确提及品牌名的引用（用于评分）
    "industry_reference_ids": [3, 4], // 行业参考资料（只展示，不计分）
    "irrelevant_ids": [5, 6],         // 无关内容（过滤掉）
    "reasoning": "判断逻辑"
}}
```

关键：只有在标题或摘要中能看到"{brand_name}"这个词的，才能放入brand_direct_ids！"""

        try:
            import os
            import httpx
            import re
            from tools.llm_call_tracker import llm_track, usage_from_response_payload
            
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            if not api_key:
                print("   警告: 未设置DEEPSEEK_API_KEY，跳过LLM验证")
                all_relevant.extend(batch)
                continue
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with llm_track(
                    "source_relevance",
                    "deepseek",
                    model=DEEPSEEK_OFFICIAL_FLASH,
                ) as tracker:
                    response = await client.post(
                        "https://api.deepseek.com/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": DEEPSEEK_OFFICIAL_FLASH,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.3
                        }
                    )
                    if response.status_code == 200:
                        response_data_for_usage = response.json()
                        input_tokens, output_tokens, cached_tokens = usage_from_response_payload(response_data_for_usage)
                        tracker.record(
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_tokens=cached_tokens,
                            success=True,
                        )
                    else:
                        tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                
                if response.status_code == 200:
                    response_data = response.json()
                    content = response_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    
                    # 解析JSON - 匹配新的三层分类格式
                    json_match = re.search(r'\{[^{}]*"brand_direct_ids"[^{}]*\}', content, re.DOTALL)
                    if json_match:
                        result = json.loads(json_match.group())
                    else:
                        # 尝试更宽松的匹配
                        json_match = re.search(r'\{.*?\}', content, re.DOTALL)
                        if json_match:
                            result = json.loads(json_match.group())
                        else:
                            # 解析失败，保守处理：所有都作为行业参考
                            result = {
                                "brand_direct_ids": [],
                                "industry_reference_ids": list(range(1, len(batch) + 1)),
                                "irrelevant_ids": []
                            }
                else:
                    print(f"   LLM API错误: {response.status_code}")
                    result = {
                        "brand_direct_ids": [],
                        "industry_reference_ids": list(range(1, len(batch) + 1)),
                        "irrelevant_ids": []
                    }
            
            # 根据三层分类结果处理
            brand_direct_ids = set(result.get('brand_direct_ids', []))
            industry_ref_ids = set(result.get('industry_reference_ids', []))
            
            for idx, c in enumerate(batch, 1):
                if idx in brand_direct_ids:
                    # 品牌直接引用 - 用于评分
                    all_brand_direct.append({**c, '_tier': 'brand_direct', '_verified_by': 'llm'})
                elif idx in industry_ref_ids:
                    # 行业参考 - 只展示不计分
                    all_industry_ref.append({**c, '_tier': 'industry_reference', '_verified_by': 'llm'})
                else:
                    # 无关内容 - 过滤
                    all_filtered.append({**c, '_tier': 'irrelevant', '_filtered_reason': result.get('reasoning', '')[:50]})
                    
        except Exception as e:
            # 出错时保守处理，全部作为行业参考
            print(f"   LLM验证出错: {e}")
            for c in batch:
                all_industry_ref.append({**c, '_tier': 'industry_reference', '_verified_by': 'fallback'})

    
    return {
        'brand_direct': all_brand_direct,      # 品牌直接引用（用于评分）
        'industry_reference': all_industry_ref, # 行业参考（展示但不计分）
        'filtered': all_filtered,               # 过滤掉的
        'stats': {
            'total': len(citations),
            'brand_direct': len(all_brand_direct),
            'industry_reference': len(all_industry_ref),
            'filtered': len(all_filtered)
        }
    }


async def filter_relevant_citations(
    brand_name: str,
    company_full_name: str,
    industry: str,
    citations: List[Dict[str, Any]],
    min_score: float = 0.3
) -> Dict[str, Any]:
    """
    批量验证并过滤相关引用
    
    Args:
        brand_name: 品牌名
        company_full_name: 公司全称
        industry: 行业
        citations: 引用列表
        min_score: 最低相关性分数阈值
        
    Returns:
        {
            'direct_citations': [...],    # 直接相关的引用
            'industry_citations': [...],  # 行业参考资料
            'filtered_out': [...],        # 被过滤的无关引用
            'stats': {
                'total': int,
                'direct': int,
                'industry': int,
                'irrelevant': int
            }
        }
    """
    direct_citations = []
    industry_citations = []
    filtered_out = []
    
    for citation in citations:
        result = await verify_citation_relevance(
            brand_name=brand_name,
            company_full_name=company_full_name,
            industry=industry,
            citation=citation
        )
        
        if result['relevance_type'] == 'direct':
            direct_citations.append({
                **citation,
                '_relevance_score': result['relevance_score'],
                '_relevance_reason': result['reason']
            })
        elif result['relevance_type'] == 'industry' and result['relevance_score'] >= min_score:
            industry_citations.append({
                **citation,
                '_relevance_score': result['relevance_score'],
                '_relevance_reason': result['reason']
            })
        else:
            filtered_out.append({
                **citation,
                '_relevance_score': result['relevance_score'],
                '_relevance_reason': result['reason']
            })
    
    return {
        'direct_citations': direct_citations,
        'industry_citations': industry_citations,
        'filtered_out': filtered_out,
        'stats': {
            'total': len(citations),
            'direct': len(direct_citations),
            'industry': len(industry_citations),
            'irrelevant': len(filtered_out)
        }
    }


# 快速规则过滤（通用算法，不依赖硬编码的规则列表）
def quick_relevance_check(
    brand_name: str,
    citation: Dict[str, Any],
    company_suffix: str = "",  # 可选的公司后缀，如 "（深圳）科技有限公司"
    industry_keywords: List[str] = None  # 可选的行业关键词
) -> str:
    """
    通用品牌相关性快速检测算法
    
    核心思路：
    1. 语法位置分析 - 品牌名在标题中的位置是否像"实体名"还是"动作描述"
    2. 上下文共现分析 - 品牌名周围是否有表明"这是一家公司"的词汇
    3. 行业关键词匹配 - 标题是否与业务行业相关
    
    Returns:
        'likely_relevant' / 'likely_irrelevant' / 'needs_llm'
    """
    import re
    
    title = citation.get('title', '')
    snippet = citation.get('snippet', '') or ''
    
    if not title:
        return 'likely_irrelevant'
    
    # ========== 算法1: 语法位置分析 ==========
    # 检测品牌名是否作为句子主语（实体）还是动词/状语（描述）
    
    brand_pos = title.find(brand_name)
    if brand_pos != -1:
        # 获取品牌名前后的上下文
        before = title[:brand_pos]
        after = title[brand_pos + len(brand_name):]
        
        # 核心检测：品牌名是否被用作"动词短语"而非"实体名"
        # 
        # 误判模式特征：
        # 1. 品牌名前面是短词（2-6字，没有标点），通常是地名/主体
        # 2. 品牌名本身包含动词性质的词（如"上榜"、"入选"等）
        # 3. 整个标题有"排行榜"、"名单"、"评选"等语境词
        
        # 检查1: 短前缀 + 品牌名包含'上榜'/'入选'等动词
        verb_words_in_brand = ['上榜', '入选', '获评', '当选', '入围', '入列']
        brand_has_verb = any(v in brand_name for v in verb_words_in_brand)
        
        if brand_has_verb:
            before_stripped = before.strip()
            # 如果前面是2-6字的短词（可能是地名），且没有分隔符
            if 2 <= len(before_stripped) <= 6:
                if not any(p in before_stripped for p in ['、', '：', ':', '|', '-', '·', ',', '，']):
                    # 这很可能是"[地名][动词短语]"的模式
                    return 'likely_irrelevant'
        
        # 检查2: 整个标题有明显的"榜单/排名"语境
        ranking_context_words = ['百强', '入选', '上榜', '名单', '榜单', '评选', '获评', 
                                  '示范', '标杆', '试点', '优秀', '先进', '模范', '典型']
        ranking_score = sum(1 for w in ranking_context_words if w in title)
        
        if ranking_score >= 2:
            # 多个排名相关词 + 品牌名前有短前缀 = 很可能是误判
            before_stripped = before.strip()
            if len(before_stripped) >= 1 and len(before_stripped) <= 8:
                return 'likely_irrelevant'
        
        # 检查3: 品牌名 + "缘何"等疑问词
        question_words = ['缘何', '为何', '何以', '如何能', '怎么能']
        if any(q in after[:10] for q in question_words):
            return 'likely_irrelevant'
        
        # 检查4: 后面紧跟地名（如"全域上榜浙江"）
        # 如果品牌名后面紧跟2-4个字且无标点，可能是"[动词][地点]"
        after_stripped = after.strip()
        if 2 <= len(after_stripped.split()[0] if after_stripped.split() else '') <= 6:
            # 检查是否是"省/市/县/区"结尾，表明是地名
            if after_stripped and after_stripped[0] in '省市县区州':
                return 'needs_llm'

    
    # ========== 算法2: 公司实体共现分析 ==========
    # 检测是否有表明"这是一家公司/产品"的上下文词汇
    
    company_indicators = ['公司', '科技', '集团', '创始人', '创始', 'CEO', '融资', 
                          '产品', '服务', '方案', '客户', '官网', '官方', '品牌', 
                          '团队', '业务', '项目', '案例', '合作', '签约']
    
    # 如果标题中有公司指示词，很可能是真正在说这家公司
    title_with_snippet = f"{title} {snippet[:200]}"
    company_score = sum(1 for word in company_indicators if word in title_with_snippet)
    
    if company_score >= 2:  # 有2个以上公司相关词汇
        return 'likely_relevant'
    if brand_name in title and company_score >= 1:  # 品牌名出现且有1个公司词汇
        return 'likely_relevant'
    
    # ========== 算法3: 行业关键词匹配 ==========
    # 检测标题是否与业务行业相关
    
    default_industry_keywords = [
        # GEO/SEO相关
        'GEO', 'SEO', '优化', '搜索引擎', '生成式', 'AI搜索', 'AI优化',
        # 营销相关
        '流量', '推广', '营销', '获客', '转化', '投放', '广告',
        # 内容相关
        '内容策略', '内容运营', '社媒', '短视频', '抖音', '小红书'
    ]
    
    keywords_to_check = industry_keywords or default_industry_keywords
    title_lower = title.lower()
    
    if any(kw.lower() in title_lower for kw in keywords_to_check):
        return 'likely_relevant'
    
    # ========== 算法4: 新闻/政务语境检测 ==========
    # 检测是否是政务/新闻报道的语境（通常与公司无关）
    
    news_context_words = ['发布会', '新闻', '通报', '公告', '政府', '部门', '区县', 
                          '省份', '城市', '入选', '获评', '示范区', '试点', '高质量发展']
    
    if brand_name in title:
        news_score = sum(1 for word in news_context_words if word in title)
        if news_score >= 2:  # 2个以上政务词汇，可能是误判
            return 'needs_llm'  # 让LLM判断
    
    # ========== 算法5: 品牌名不在内容中 ==========
    
    if brand_name not in title and brand_name not in snippet:
        return 'likely_irrelevant'
    
    # ========== 默认：需要LLM判断 ==========
    
    return 'needs_llm'


# 异步版本的LLM相关性验证（用于needs_llm的情况）
async def verify_with_llm_if_needed(
    brand_name: str,
    company_full_name: str,
    industry: str,
    citations: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    对需要LLM判断的引用进行批量验证
    
    策略：
    1. 先用quick_relevance_check快速分类
    2. 只对'needs_llm'的引用调用LLM
    3. 合并结果
    """
    relevant = []
    needs_verification = []
    
    for citation in citations:
        result = quick_relevance_check(brand_name, citation)
        if result == 'likely_relevant':
            relevant.append(citation)
        elif result == 'needs_llm':
            needs_verification.append(citation)
        # 'likely_irrelevant' 直接丢弃
    
    # 对需要LLM判断的进行验证
    if needs_verification:
        for citation in needs_verification:
            llm_result = await verify_citation_relevance(
                brand_name=brand_name,
                company_full_name=company_full_name,
                industry=industry,
                citation=citation
            )
            if llm_result.get('relevance_type') in ['direct', 'industry']:
                if llm_result.get('relevance_score', 0) >= 0.3:
                    relevant.append({
                        **citation,
                        '_relevance_score': llm_result.get('relevance_score'),
                        '_verified_by': 'llm'
                    })
    
    return relevant


# 导出
__all__ = [
    'verify_citation_relevance',
    'filter_relevant_citations',
    'quick_relevance_check'
]
