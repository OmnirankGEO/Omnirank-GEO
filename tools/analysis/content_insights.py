"""
内容洞察分析器 (Content Insights Analyzer)
分析社媒内容数据，提取热门话题、发布规律、爆款特征

核心功能:
1. 热门话题提取 - 从标题/描述中提取高频关键词
2. 发布时间分析 - 分析最佳发布时间
3. 爆款内容解码 - 分析高互动内容的共性特征
4. 标题模板提取 - 提取爆款标题的常用模式
"""

import re
from typing import Dict, List, Any
from collections import Counter
from datetime import datetime


# 停用词列表
STOP_WORDS = {
    '的', '了', '是', '在', '我', '有', '和', '就', '不', '人', '都', '一', '一个',
    '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好',
    '自己', '这', '那', '被', '做', '可以', '这个', '什么', '怎么', '如何', '为什么',
    '吗', '呢', '啊', '哦', '哈', '呀', '吧', '嘛', '哪', '谁', '怎样', '哪个',
    '的话', '来说', '已经', '或者', '还是', '可能', '但是', '而且', '因为', '所以',
}


def extract_hashtags(text: str) -> List[str]:
    """提取 #hashtag 标签"""
    pattern = r'#([^\s#]+)'
    return re.findall(pattern, text)


def extract_keywords(texts: List[str], top_n: int = 20) -> List[Dict[str, Any]]:
    """
    从文本列表中提取高频关键词
    
    Args:
        texts: 文本列表
        top_n: 返回前N个关键词
        
    Returns:
        [{'keyword': str, 'count': int, 'type': 'hashtag'/'word'}]
    """
    all_hashtags = []
    all_words = []
    
    for text in texts:
        if not text:
            continue
            
        # 提取hashtag
        hashtags = extract_hashtags(text)
        all_hashtags.extend(hashtags)
        
        # 提取中文词（简单分词：2-4字组合）
        clean_text = re.sub(r'[#@\s\d\W]', ' ', text)
        # 简单的中文分词：提取连续中文字符
        chinese_words = re.findall(r'[\u4e00-\u9fff]{2,6}', clean_text)
        for word in chinese_words:
            if word not in STOP_WORDS and len(word) >= 2:
                all_words.append(word)
    
    # 统计频率
    hashtag_counts = Counter(all_hashtags)
    word_counts = Counter(all_words)
    
    # 合并结果
    results = []
    
    # 添加hashtag
    for tag, count in hashtag_counts.most_common(top_n):
        results.append({
            'keyword': f"#{tag}",
            'count': count,
            'type': 'hashtag'
        })
    
    # 添加高频词
    for word, count in word_counts.most_common(top_n):
        # 检查是否已经作为hashtag存在
        if word not in [r['keyword'].replace('#', '') for r in results]:
            results.append({
                'keyword': word,
                'count': count,
                'type': 'word'
            })
    
    # 按频率排序
    results.sort(key=lambda x: x['count'], reverse=True)
    
    return results[:top_n]


def analyze_publish_time(contents: List[Dict]) -> Dict[str, Any]:
    """
    分析发布时间规律
    
    Args:
        contents: 内容列表，每个包含 create_time 字段
        
    Returns:
        {
            'best_hours': [(hour, count)],       # 最佳发布时段
            'best_weekdays': [(day, count)],     # 最佳发布日
            'hourly_distribution': {...},         # 每小时分布
            'weekday_distribution': {...}         # 每天分布
        }
    """
    hourly = Counter()
    weekday = Counter()
    
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    
    for content in contents:
        timestamp = content.get('create_time', 0)
        if not timestamp:
            continue
            
        try:
            dt = datetime.fromtimestamp(timestamp)
            hourly[dt.hour] += 1
            weekday[dt.weekday()] += 1
        except:
            continue
    
    # 时段分组
    time_slots = {
        '早间(6-9点)': sum(hourly.get(h, 0) for h in range(6, 10)),
        '上午(9-12点)': sum(hourly.get(h, 0) for h in range(9, 12)),
        '午间(12-14点)': sum(hourly.get(h, 0) for h in range(12, 14)),
        '下午(14-18点)': sum(hourly.get(h, 0) for h in range(14, 18)),
        '晚间(18-22点)': sum(hourly.get(h, 0) for h in range(18, 22)),
        '深夜(22-6点)': sum(hourly.get(h, 0) for h in list(range(22, 24)) + list(range(0, 6))),
    }
    
    best_slots = sorted(time_slots.items(), key=lambda x: x[1], reverse=True)[:3]
    best_weekdays = [(weekday_names[d], c) for d, c in weekday.most_common(3)]
    
    return {
        'best_time_slots': best_slots,
        'best_weekdays': best_weekdays,
        'hourly_distribution': dict(hourly),
        'weekday_distribution': {weekday_names[k]: v for k, v in weekday.items()},
        'total_analyzed': len(contents)
    }


def analyze_viral_content(contents: List[Dict], top_n: int = 5) -> Dict[str, Any]:
    """
    分析爆款内容特征
    
    Args:
        contents: 内容列表
        top_n: 取前N个高互动内容
        
    Returns:
        {
            'top_contents': [...],           # 高互动内容
            'common_keywords': [...],        # 爆款共性关键词
            'avg_engagement': float,         # 平均互动量
            'title_patterns': [...]          # 标题模式
        }
    """
    # 计算互动量并排序
    for content in contents:
        stats = content.get('stats', {})
        # 兼容不同数据结构
        likes = stats.get('digg', 0) or content.get('like_count', 0)
        comments = stats.get('comment', 0) or content.get('comment_count', 0)
        shares = stats.get('share', 0) or content.get('share_count', 0)
        collects = stats.get('collect', 0) or content.get('collect_count', 0)
        
        content['_total_engagement'] = likes + comments * 2 + shares * 3 + collects * 2
    
    # 按互动量排序
    sorted_contents = sorted(contents, key=lambda x: x.get('_total_engagement', 0), reverse=True)
    
    top_contents = sorted_contents[:top_n]
    
    # 提取爆款内容的标题/描述
    viral_texts = [c.get('desc', '') or c.get('title', '') for c in top_contents]
    
    # 提取爆款共性关键词
    common_keywords = extract_keywords(viral_texts, top_n=10)
    
    # 计算平均互动量
    total_engagement = sum(c.get('_total_engagement', 0) for c in contents)
    avg_engagement = total_engagement / len(contents) if contents else 0
    
    # 提取标题模式
    title_patterns = extract_title_patterns(viral_texts)
    
    return {
        'top_contents': [
            {
                'title': c.get('desc', '')[:50] + '...' if len(c.get('desc', '')) > 50 else c.get('desc', ''),
                'engagement': c.get('_total_engagement', 0),
                'author': c.get('author', {}).get('nickname', '未知'),
                'url': c.get('url', '')
            }
            for c in top_contents
        ],
        'common_keywords': common_keywords,
        'avg_engagement': round(avg_engagement, 1),
        'viral_threshold': round(avg_engagement * 3, 1),  # 3倍平均为爆款
        'title_patterns': title_patterns
    }


def extract_title_patterns(texts: List[str]) -> List[str]:
    """
    提取标题模式
    
    返回常见的标题撰写模式
    """
    patterns_found = []
    
    # 常见模式检测
    pattern_rules = [
        (r'如何', '如何型: 如何XXX'),
        (r'为什么', '为什么型: 为什么XXX'),
        (r'\d+个', '数字列表型: N个XXX'),
        (r'(?:必看|必读|必学|必备)', '必备型: 必看/必读XXX'),
        (r'(?:干货|攻略|指南|教程)', '教程型: XXX干货/攻略'),
        (r'(?:揭秘|真相|内幕)', '揭秘型: 揭秘XXX'),
        (r'(?:避坑|踩雷|别踩)', '避坑型: XXX避坑指南'),
        (r'(?:实测|亲测|测评)', '测评型: 实测/亲测XXX'),
        (r'\?|？', '疑问型: XXX?'),
        (r'(?:竟然|居然|没想到)', '惊喜型: 没想到XXX'),
    ]
    
    for text in texts:
        for pattern, description in pattern_rules:
            if re.search(pattern, text):
                if description not in patterns_found:
                    patterns_found.append(description)
    
    return patterns_found[:5]  # 返回最多5种模式


def analyze_content_insights(
    douyin_data: Dict = None,
    xiaohongshu_data: Dict = None,
    brand_name: str = ""
) -> Dict[str, Any]:
    """
    综合内容洞察分析
    
    Args:
        douyin_data: 抖音数据 {'top20': [...]}
        xiaohongshu_data: 小红书数据 {'top20': [...]}
        brand_name: 品牌名（用于过滤自身内容）
        
    Returns:
        完整的内容洞察报告
    """
    insights = {
        'brand_name': brand_name,
        'douyin': {},
        'xiaohongshu': {},
        'combined': {}
    }
    
    all_texts = []
    all_contents = []
    
    # 分析抖音数据
    if douyin_data:
        dy_contents = douyin_data.get('top20', [])
        dy_texts = [c.get('desc', '') for c in dy_contents]
        
        insights['douyin'] = {
            'content_count': len(dy_contents),
            'keywords': extract_keywords(dy_texts, top_n=15),
            'publish_time': analyze_publish_time(dy_contents),
            'viral_analysis': analyze_viral_content(dy_contents, top_n=5)
        }
        
        all_texts.extend(dy_texts)
        all_contents.extend(dy_contents)
    
    # 分析小红书数据
    if xiaohongshu_data:
        xhs_contents = xiaohongshu_data.get('top20', [])
        xhs_texts = [c.get('title', '') or c.get('display_title', '') or c.get('desc', '') 
                     for c in xhs_contents]
        
        insights['xiaohongshu'] = {
            'content_count': len(xhs_contents),
            'keywords': extract_keywords(xhs_texts, top_n=15),
            'publish_time': analyze_publish_time(xhs_contents),
            'viral_analysis': analyze_viral_content(xhs_contents, top_n=5)
        }
        
        all_texts.extend(xhs_texts)
        all_contents.extend(xhs_contents)
    
    # 综合分析
    if all_texts:
        insights['combined'] = {
            'total_contents': len(all_contents),
            'industry_keywords': extract_keywords(all_texts, top_n=20),
            'overall_viral': analyze_viral_content(all_contents, top_n=10)
        }
    
    return insights


async def analyze_content_with_llm(
    raw_insights: Dict[str, Any],
    brand_name: str,
    industry: str,
    competitor_data: List[Dict] = None
) -> Dict[str, Any]:
    """
    使用LLM对Python分析结果进行智能解读
    
    Args:
        raw_insights: Python分析的原始数据
        brand_name: 品牌名称
        industry: 行业
        competitor_data: 竞品数据
        
    Returns:
        {
            'ai_summary': str,             # AI智能总结
            'content_strategy': str,       # 内容策略建议
            'gap_analysis': str,           # 差距分析
            'actionable_suggestions': [str] # 可执行建议
        }
    """
    # 提取关键数据
    combined = raw_insights.get('combined', {})
    keywords = combined.get('industry_keywords', [])[:10]
    viral = combined.get('overall_viral', {})
    
    douyin_insights = raw_insights.get('douyin', {})
    xhs_insights = raw_insights.get('xiaohongshu', {})
    
    # 格式化关键词
    keyword_str = ', '.join([k.get('keyword', '') for k in keywords])
    
    # 格式化标题模式
    patterns = viral.get('title_patterns', [])
    patterns_str = ', '.join(patterns) if patterns else '暂无明确模式'
    
    # 格式化发布时间
    dy_time = douyin_insights.get('publish_time', {})
    dy_best_slots = dy_time.get('best_time_slots', [])
    dy_time_str = ', '.join([s[0] for s in dy_best_slots[:2]]) if dy_best_slots else '待分析'
    
    # 竞品信息
    competitor_ref = ""
    if competitor_data:
        top_comps = [c.get('name', '') for c in competitor_data[:3]]
        competitor_ref = ', '.join(top_comps)
    
    # 构建prompt
    prompt = f"""你是一位资深的社媒内容策略专家。请根据以下数据分析结果，为"{brand_name}"品牌生成智能内容洞察。

## 行业
{industry}

## 热门话题关键词（按热度排序）
{keyword_str}

## 爆款标题模式
{patterns_str}

## 抖音最佳发布时间
{dy_time_str}

## 平均互动量
{viral.get('avg_engagement', 0):.0f}（爆款阈值：{viral.get('viral_threshold', 0):.0f}+）

## 竞品账号
{competitor_ref or '待分析'}

## 请输出JSON格式
```json
{{
    "ai_summary": "一句话总结当前行业内容生态（50字以内）",
    "content_gap": "发现的内容空白/机会点（50字以内）",
    "strategy_suggestion": "针对{brand_name}的内容策略建议（100字以内）",
    "actionable_tips": [
        "具体可执行建议1",
        "具体可执行建议2",
        "具体可执行建议3"
    ]
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
            import json as json_module
            json_match = re.search(r'\{[^{}]+\}', response.text, re.DOTALL)
            if json_match:
                result = json_module.loads(json_match.group())
            else:
                result = _generate_fallback_insights(brand_name, industry, keywords, patterns)
        
        return {
            'ai_summary': result.get('ai_summary', ''),
            'content_gap': result.get('content_gap', ''),
            'strategy_suggestion': result.get('strategy_suggestion', ''),
            'actionable_tips': result.get('actionable_tips', []),
            'raw_data': {
                'keywords': keywords,
                'patterns': patterns,
                'avg_engagement': viral.get('avg_engagement', 0)
            }
        }
        
    except Exception as e:
        return _generate_fallback_insights(brand_name, industry, keywords, patterns)


def _generate_fallback_insights(brand_name, industry, keywords, patterns):
    """降级方案"""
    keyword_str = ', '.join([k.get('keyword', '') for k in keywords[:5]])
    
    return {
        'ai_summary': f'{industry}行业内容以教程干货为主，热门话题集中在{keyword_str}等方向',
        'content_gap': '深度案例解析和效果验证类内容较少，存在内容差异化机会',
        'strategy_suggestion': f'建议{brand_name}聚焦"效果验证"和"客户案例"方向，与泛教程内容形成差异化',
        'actionable_tips': [
            '创作"客户案例复盘"系列，展示真实效果',
            '在标题中加入具体数据（如"3倍增长"），提升可信度',
            f'结合热门话题{keywords[0].get("keyword", "")}创作相关内容' if keywords else '持续关注行业热点话题'
        ],
        'raw_data': {
            'keywords': keywords,
            'patterns': patterns,
            'avg_engagement': 0
        }
    }


def format_content_insights_with_ai(
    raw_insights: Dict[str, Any],
    llm_insights: Dict[str, Any] = None
) -> str:
    """
    格式化内容洞察分析的Markdown输出（含AI解读）
    """
    combined = raw_insights.get('combined', {})
    keywords = combined.get('industry_keywords', [])[:10]
    viral = combined.get('overall_viral', {})
    
    output = ""
    
    # AI智能总结（如果有）
    if llm_insights:
        ai_summary = llm_insights.get('ai_summary', '')
        if ai_summary:
            output += f"**AI洞察**: {ai_summary}\n\n"
        
        content_gap = llm_insights.get('content_gap', '')
        if content_gap:
            output += f"**内容机会**: {content_gap}\n\n"
    
    # 热门话题词云
    if keywords:
        output += "**热门话题词云**:\n"
        keyword_list = []
        for kw in keywords:
            word = kw.get("keyword", "")
            count = kw.get("count", 0)
            keyword_list.append(f"`{word}`({count})")
        output += " | ".join(keyword_list) + "\n\n"
    
    # 爆款标题模式
    patterns = viral.get('title_patterns', [])
    if patterns:
        output += "**爆款标题模式**:\n"
        for pattern in patterns[:5]:
            output += f"- {pattern}\n"
        output += "\n"
    
    # 互动基准
    avg_eng = viral.get('avg_engagement', 0)
    viral_threshold = viral.get('viral_threshold', 0)
    if avg_eng > 0:
        output += f"**互动基准**: 平均 {avg_eng:.0f} | 爆款阈值 {viral_threshold:.0f}+\n\n"
    
    # AI策略建议（如果有）
    if llm_insights:
        strategy = llm_insights.get('strategy_suggestion', '')
        if strategy:
            output += f"**内容策略**: {strategy}\n\n"
        
        tips = llm_insights.get('actionable_tips', [])
        if tips:
            output += "**可执行建议**:\n"
            for tip in tips:
                output += f"- {tip}\n"
            output += "\n"
    
    # 发布时间分析
    dy_insights = raw_insights.get('douyin', {})
    if dy_insights:
        publish_time = dy_insights.get('publish_time', {})
        best_slots = publish_time.get('best_time_slots', [])
        best_days = publish_time.get('best_weekdays', [])
        
        if best_slots or best_days:
            output += "**抖音最佳发布时间**:\n"
            if best_slots:
                slots_str = ", ".join([f"{slot[0]}" for slot in best_slots[:2]])
                output += f"- 时段: {slots_str}\n"
            if best_days:
                days_str = ", ".join([f"{day[0]}" for day in best_days[:2]])
                output += f"- 周几: {days_str}\n"
            output += "\n"
    
    return output if output else "*(分析数据不足)*"


# 导出
__all__ = [
    'analyze_content_insights',
    'analyze_content_with_llm',
    'format_content_insights_with_ai',
    'extract_keywords',
    'analyze_publish_time',
    'analyze_viral_content',
    'extract_title_patterns'
]
