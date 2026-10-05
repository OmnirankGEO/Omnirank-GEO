"""
行动计划 Agent (Action Plan Agent)
生成专业的、数据驱动的优化建议

核心功能:
1. 根据评分短板排序优先级
2. 参考竞品爆款策略
3. 结合行业趋势给出具体执行步骤
4. 包含时间节点和预期效果
"""

import json
from typing import Dict, List, Any
import agentscope
from agentscope.message import Msg


async def generate_professional_action_plan(
    brand_name: str,
    industry: str,
    score_data: Dict[str, Any],
    brand_identification: Dict[str, Any] = None,
    content_insights: Dict[str, Any] = None,
    competitor_data: List[Dict] = None,
    industry_analysis: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    生成专业的优化行动计划
    
    Args:
        brand_name: 品牌名称
        industry: 行业名称
        score_data: 8维度评分数据
        brand_identification: 品牌账号识别结果
        content_insights: 内容洞察分析
        competitor_data: 竞品分析数据
        industry_analysis: 行业洞察分析
        
    Returns:
        {
            'action_plan_md': str,         # Markdown格式的行动计划
            'priorities': [                # 优先级列表
                {
                    'level': 'P0/P1/P2',
                    'title': str,
                    'problem': str,
                    'root_cause': str,
                    'actions': [str],
                    'reference': str,       # 参考竞品/案例
                    'timeline': str,
                    'expected_result': str
                }
            ]
        }
    """
    
    # 1. 分析评分短板
    dimension_scores = score_data.get('dimension_scores', {})
    total_score = score_data.get('total_score', 0)
    
    # 维度映射表
    dimension_map = {
        'web_search': {'name': '网页搜索可见度', 'max': 15},
        'platform_coverage': {'name': '平台覆盖度', 'max': 15},
        'content_quality': {'name': '内容质量', 'max': 15},
        'authority': {'name': '权威背书', 'max': 10},
        'brand_recognition': {'name': '品牌词占有', 'max': 10},
        'ai_visibility': {'name': 'AI引擎可见度', 'max': 20},
        'ai_citation_potential': {'name': 'AI引用潜力', 'max': 10},
        'update_frequency': {'name': '更新频率', 'max': 5}
    }
    
    # 识别短板维度 (得分率<50%的维度)
    weak_dimensions = []
    for dim_key, dim_info in dimension_map.items():
        score = dimension_scores.get(dim_key, 0)
        max_score = dim_info['max']
        rate = score / max_score if max_score > 0 else 0
        if rate < 0.5:
            weak_dimensions.append({
                'key': dim_key,
                'name': dim_info['name'],
                'score': score,
                'max': max_score,
                'rate': rate,
                'gap': max_score - score
            })
    
    # 按缺口大小排序
    weak_dimensions.sort(key=lambda x: x['gap'], reverse=True)
    
    # 2. 提取竞品参考信息
    competitor_ref = ""
    if competitor_data:
        top_competitor = competitor_data[0] if competitor_data else {}
        competitor_ref = top_competitor.get('name', '行业头部账号')
    
    # 3. 提取热门话题
    hot_topics = []
    if content_insights:
        combined = content_insights.get('combined', {})
        keywords = combined.get('industry_keywords', [])[:5]
        hot_topics = [k.get('keyword', '') for k in keywords]
    
    # 4. 提取行业趋势
    industry_trends = []
    if industry_analysis:
        industry_trends = industry_analysis.get('industry_trends', [])[:3]
        top_brands = industry_analysis.get('top_brands', [])[:5]
    
    # 5. 构建LLM prompt
    prompt = f"""你是一位资深的GEO营销顾问，请为客户生成专业的优化行动计划。

## 客户信息
- 品牌名称: {brand_name}
- 所属行业: {industry}
- GEO总分: {total_score}/{score_data.get('max_score', 100)}

## 评分短板分析
{_format_weak_dimensions(weak_dimensions)}

## 竞品参考
- 头部竞品: {competitor_ref}
- 行业热门话题: {', '.join(hot_topics) if hot_topics else '待分析'}

## 行业趋势
{chr(10).join(['- ' + t[:100] for t in industry_trends]) if industry_trends else '- 待分析'}

## 请生成专业行动计划

要求：
1. 按P0/P1/P2优先级分类
2. P0为本周必须完成的紧急任务
3. 每个任务包含：问题、根因、具体行动（带可操作细节）、参考案例、预期效果
4. 行动建议要具体，不要空泛（如"在知乎发布3篇回答"而不是"增加内容"）
5. 参考竞品的成功策略

请直接输出Markdown格式的行动计划，格式如下：

## 📝 专业优化行动计划

### 🔴 P0 紧急（本周完成）

#### 问题1：[问题标题]
**现状**: [具体问题描述]
**根因**: [问题根本原因]
**行动**:
1. [具体行动1，包含细节]
2. [具体行动2]
**参考**: [竞品案例或数据支撑]
**预期**: [预期效果和时间节点]

### 🟡 P1 重要（两周内）
...

### 🟢 P2 优化（一个月内）
..."""

    try:
        from agentscope.models import load_model_by_config_name
        model = load_model_by_config_name("deepseek_model")
        
        response = model([{"role": "user", "content": prompt}])
        
        action_plan_md = response.text if hasattr(response, 'text') else str(response)
        
        # 清理可能的markdown代码块标记
        if action_plan_md.startswith('```'):
            lines = action_plan_md.split('\n')
            action_plan_md = '\n'.join(lines[1:-1] if lines[-1] == '```' else lines[1:])
        
        return {
            'action_plan_md': action_plan_md,
            'weak_dimensions': weak_dimensions,
            'competitor_ref': competitor_ref,
            'hot_topics': hot_topics
        }
        
    except Exception as e:
        # 降级方案：使用模板生成
        return _generate_fallback_plan(
            brand_name, 
            industry, 
            weak_dimensions, 
            competitor_ref, 
            hot_topics
        )


def _format_weak_dimensions(weak_dims: List[Dict]) -> str:
    """格式化短板维度"""
    if not weak_dims:
        return "暂无明显短板"
    
    lines = []
    for dim in weak_dims[:5]:
        lines.append(f"- {dim['name']}: {dim['score']}/{dim['max']}分 (缺口{dim['gap']}分)")
    return '\n'.join(lines)


def _generate_fallback_plan(
    brand_name: str,
    industry: str,
    weak_dimensions: List[Dict],
    competitor_ref: str,
    hot_topics: List[str]
) -> Dict[str, Any]:
    """降级方案：模板生成"""
    
    plan_md = f"""## 📝 专业优化行动计划

> 基于{brand_name}的GEO评分分析，以下是针对性的优化建议：

### 🔴 P0 紧急（本周完成）
"""
    
    # 根据短板生成建议
    for i, dim in enumerate(weak_dimensions[:2]):
        if dim['key'] == 'ai_visibility':
            plan_md += f"""
#### 问题{i+1}：AI引擎可见度不足
**现状**: AI引擎可见度得分{dim['score']}/{dim['max']}，品牌在DeepSeek/Kimi/豆包等AI搜索中未被推荐
**根因**: 缺乏结构化的文本语料供AI学习抓取
**行动**:
1. **知乎布局**: 发布3篇专业回答，标题包含"{industry}"关键词，正文自然植入"{brand_name}"
2. **百科词条**: 创建或完善百度百科词条"{brand_name}"，确保包含公司全称和主营业务
3. **头条发文**: 在今日头条发布2篇行业观点文章，建立AI可索引的权威内容源
**参考**: 参考{competitor_ref}的内容策略，其知乎回答多采用"痛点+方案+案例"结构
**预期**: 2周内AI引擎可见度提升至30%+，4周内达到50%+
"""
        elif dim['key'] == 'platform_coverage':
            plan_md += f"""
#### 问题{i+1}：平台覆盖不均衡
**现状**: 平台覆盖度得分{dim['score']}/{dim['max']}，存在明显的流量洼地
**根因**: 内容分发策略单一，未覆盖全渠道
**行动**:
1. **补齐短板平台**: 在缺失的平台开设官方账号
2. **内容适配**: 根据各平台特性调整内容形式（抖音重视觉，小红书重体验，知乎重深度）
3. **同步分发**: 建立一稿多发的内容工作流
**参考**: 行业热门话题：{', '.join(hot_topics[:3]) if hot_topics else '待分析'}
**预期**: 1个月内平台覆盖度提升至60%+
"""
        elif dim['key'] == 'content_quality':
            plan_md += f"""
#### 问题{i+1}：内容质量待提升
**现状**: 内容质量得分{dim['score']}/{dim['max']}，用户互动率较低
**根因**: 内容形式单一，缺乏用户价值传递
**行动**:
1. **痛点内容**: 创作"解决问题型"内容而非"展示型"内容
2. **标题优化**: 使用数字+痛点的标题模式（如"3个方法解决XX问题"）
3. **互动设计**: 在内容末尾设置互动引导（提问/投票）
**参考**: 参考{competitor_ref}的高赞内容模式
**预期**: 2周内互动率提升50%+
"""
    
    plan_md += """
### 🟡 P1 重要（两周内）

- **权威背书建设**: 争取1-2篇行业媒体报道或采访
- **案例沉淀**: 整理3个成功客户案例，制作成可传播的内容素材
- **长尾词覆盖**: 在视频标题和描述中植入行业长尾关键词

### 🟢 P2 优化（一个月内）

- **行业白皮书**: 策划发布年度行业报告，争取被权威媒体引用
- **口碑建设**: 引导满意客户在社媒发布真实评价
- **数据监控**: 建立GEO评分定期监测机制，持续优化
"""
    
    return {
        'action_plan_md': plan_md,
        'weak_dimensions': weak_dimensions,
        'competitor_ref': competitor_ref,
        'hot_topics': hot_topics
    }


# 导出
__all__ = [
    'generate_professional_action_plan'
]
