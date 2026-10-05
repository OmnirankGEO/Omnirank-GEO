"""
学术洞察 Agent (Academic Insight Agent)
对学术论文进行智能解读，提取核心观点并生成引用格式

核心功能:
1. 验证论文与业务的相关性
2. 提取每篇论文的核心观点
3. 生成"根据XXX论文的研究表明..."的引用格式
4. 过滤与业务无关的论文
"""

import json
from typing import Dict, List, Any
import agentscope
from agentscope.message import Msg


async def analyze_academic_papers(
    papers: List[Dict[str, Any]],
    industry: str,
    brand_name: str,
    business_keywords: List[str] = None
) -> Dict[str, Any]:
    """
    分析学术论文，生成智能解读
    
    Args:
        papers: 论文列表 [{title, authors, date, link, snippet}]
        industry: 行业名称
        brand_name: 品牌名称
        business_keywords: 业务关键词列表
        
    Returns:
        {
            'relevant_papers': [           # 相关论文
                {
                    'title': str,
                    'authors': str,
                    'date': str,
                    'link': str,
                    'key_insight': str,    # 核心观点
                    'relevance': str,      # 与业务的关联
                    'citation_text': str   # 引用格式文本
                }
            ],
            'filtered_papers': [...],      # 被过滤的无关论文
            'insight_summary': str,        # 整体洞察总结
            'stats': {...}
        }
    """
    if not papers:
        return {
            'relevant_papers': [],
            'filtered_papers': [],
            'insight_summary': '暂无学术论文数据',
            'stats': {'total': 0, 'relevant': 0, 'filtered': 0}
        }
    
    # 业务关键词
    if business_keywords is None:
        business_keywords = [
            industry,
            'AI搜索', 'GEO', '生成式引擎优化', 
            '搜索引擎优化', '内容营销', '品牌可见度'
        ]
    
    relevant_papers = []
    filtered_papers = []
    
    for paper in papers:
        result = await _analyze_single_paper(
            paper=paper,
            industry=industry,
            brand_name=brand_name,
            business_keywords=business_keywords
        )
        
        if result['is_relevant']:
            relevant_papers.append(result)
        else:
            filtered_papers.append({**paper, '_filter_reason': result.get('filter_reason', '与业务不相关')})
    
    # 生成整体洞察总结
    insight_summary = await _generate_insight_summary(
        relevant_papers=relevant_papers,
        industry=industry,
        brand_name=brand_name
    )
    
    return {
        'relevant_papers': relevant_papers,
        'filtered_papers': filtered_papers,
        'insight_summary': insight_summary,
        'stats': {
            'total': len(papers),
            'relevant': len(relevant_papers),
            'filtered': len(filtered_papers)
        }
    }


async def _analyze_single_paper(
    paper: Dict[str, Any],
    industry: str,
    brand_name: str,
    business_keywords: List[str]
) -> Dict[str, Any]:
    """分析单篇论文"""
    
    title = paper.get('title', '')
    snippet = paper.get('snippet', '') or paper.get('summary', '')
    authors = paper.get('authors', '未知')
    date = paper.get('date', '')
    link = paper.get('link', '') or paper.get('url', '')
    
    # 快速相关性检查
    content = f"{title} {snippet}".lower()
    has_keywords = any(kw.lower() in content for kw in business_keywords)
    
    # 排除明显无关的论文
    irrelevant_patterns = [
        '地理信息', '地理位置', 'GEO坐标', '地球科学',  # GEO的其他含义
        '医学', '生物', '化学', '物理',  # 其他学科
    ]
    
    is_irrelevant = any(pattern in content for pattern in irrelevant_patterns)
    
    if is_irrelevant:
        return {
            'is_relevant': False,
            'filter_reason': '论文属于其他领域',
            **paper
        }
    
    # 使用LLM进行深度分析
    prompt = f"""你是一位学术研究分析专家。请分析以下论文是否与"{industry}"行业相关，并提取核心观点。

## 论文信息
- 标题: {title}
- 作者: {authors}
- 日期: {date}
- 摘要: {snippet[:500] if snippet else '无摘要'}

## 目标行业
{industry}（关键词：{', '.join(business_keywords[:5])}）

## 请输出JSON格式
```json
{{
    "is_relevant": true/false,
    "relevance_score": 0.0-1.0,
    "key_insight": "论文的核心观点（50字以内）",
    "business_relevance": "与{industry}行业的关联说明（30字以内）",
    "citation_text": "根据该论文的研究表明，...（生成一句可引用的话）",
    "filter_reason": "如果不相关，说明原因"
}}
```"""

    try:
        from agentscope.models import load_model_by_config_name
        model = load_model_by_config_name("deepseek_model")
        
        response = model(
            [{"role": "user", "content": prompt}],
            parse="json"
        )
        
        if response.parsed:
            result = response.parsed
        else:
            import re
            json_match = re.search(r'\{[^{}]+\}', response.text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                # 基于关键词判断
                result = {
                    "is_relevant": has_keywords,
                    "relevance_score": 0.5 if has_keywords else 0.2,
                    "key_insight": f"该研究探讨了{title[:30]}相关问题",
                    "business_relevance": f"可作为{industry}行业参考",
                    "citation_text": f"根据《{title[:20]}...》的研究，{industry}领域正在发生重要变化。",
                    "filter_reason": "" if has_keywords else "与业务关键词匹配度低"
                }
        
        return {
            'is_relevant': result.get('is_relevant', False),
            'title': title,
            'authors': authors,
            'date': date,
            'link': link,
            'key_insight': result.get('key_insight', ''),
            'relevance': result.get('business_relevance', ''),
            'citation_text': result.get('citation_text', ''),
            'relevance_score': result.get('relevance_score', 0),
            'filter_reason': result.get('filter_reason', '')
        }
        
    except Exception as e:
        # 降级处理
        return {
            'is_relevant': has_keywords,
            'title': title,
            'authors': authors,
            'date': date,
            'link': link,
            'key_insight': f"论文研究了{title[:30]}相关问题" if has_keywords else '',
            'relevance': f"供{industry}行业参考" if has_keywords else '',
            'citation_text': '',
            'relevance_score': 0.5 if has_keywords else 0.2,
            'filter_reason': '' if has_keywords else str(e)[:50]
        }


async def _generate_insight_summary(
    relevant_papers: List[Dict],
    industry: str,
    brand_name: str
) -> str:
    """生成学术洞察总结"""
    
    if not relevant_papers:
        return "暂无与业务相关的学术研究"
    
    # 收集所有核心观点
    insights = [p.get('key_insight', '') for p in relevant_papers if p.get('key_insight')]
    citations = [p.get('citation_text', '') for p in relevant_papers if p.get('citation_text')]
    
    if not insights:
        return f"发现{len(relevant_papers)}篇相关学术研究，建议深入阅读获取专业见解"
    
    # 构建总结prompt
    prompt = f"""基于以下学术研究观点，为{industry}行业的{brand_name}品牌生成一段简洁的学术洞察总结（100字以内）：

观点列表：
{chr(10).join(['- ' + i for i in insights[:5]])}

要求：
1. 总结要有学术感但不晦涩
2. 要与{industry}行业实践相结合
3. 给出对品牌的启示"""

    try:
        from agentscope.models import load_model_by_config_name
        model = load_model_by_config_name("deepseek_model")
        
        response = model([{"role": "user", "content": prompt}])
        return response.text if hasattr(response, 'text') else str(response)
        
    except Exception as e:
        # 降级处理
        return f"学术研究表明，{industry}领域正在经历重要变革。{insights[0] if insights else ''}"


def format_academic_section(analysis_result: Dict[str, Any]) -> str:
    """
    格式化学术论文章节的Markdown输出
    """
    relevant = analysis_result.get('relevant_papers', [])
    summary = analysis_result.get('insight_summary', '')
    stats = analysis_result.get('stats', {})
    
    if not relevant:
        return "*(暂无与业务相关的学术研究)*"
    
    output = ""
    
    # 洞察总结
    if summary:
        output += f"**学术洞察**: {summary}\n\n"
    
    # 引用列表
    output += "**相关研究引用**:\n\n"
    
    for i, paper in enumerate(relevant[:5], 1):
        citation_text = paper.get('citation_text', '')
        title = paper.get('title', '未知标题')
        link = paper.get('link', '')
        authors = paper.get('authors', '未知')
        date = paper.get('date', '')
        
        if citation_text:
            output += f"> {citation_text}\n"
            output += f"> — 《{title[:40]}{'...' if len(title) > 40 else ''}》"
            if authors != '未知':
                output += f", {authors}"
            if date:
                output += f" ({date})"
            if link:
                output += f" [查看原文]({link})"
            output += "\n\n"
    
    # 统计
    filtered_count = stats.get('filtered', 0)
    if filtered_count > 0:
        output += f"\n> *注：已过滤{filtered_count}篇与业务不相关的论文*\n"
    
    return output


# 导出
__all__ = [
    'analyze_academic_papers',
    'format_academic_section'
]
