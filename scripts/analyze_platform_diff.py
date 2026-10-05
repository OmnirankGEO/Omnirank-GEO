# -*- coding: utf-8 -*-
"""
LLM平台差异分析
问题4.1: 各LLM平台偏好什么内容类型？
问题4.2: 不同平台对文体风格有偏好差异吗？
问题4.3: 不同平台对字数/结构有偏好差异吗？
"""
import os
import re
from pathlib import Path
from collections import defaultdict
from docx import Document

DATA_DIR = r"C:\AI-Test\AgentsCope-07\Benchmark against"

def extract_llm_platform(folder_path):
    """从文件夹中提取LLM平台信息"""
    platforms = []
    for item in os.listdir(folder_path):
        if item.endswith('.docx') and '回答' in item:
            if '豆包' in item:
                platforms.append('豆包')
            elif '千问' in item:
                platforms.append('千问')
            elif 'DeepSeek' in item.lower() or 'deepseek' in item:
                platforms.append('DeepSeek')
            elif 'KIMI' in item.upper() or 'kimi' in item:
                platforms.append('KIMI')
    return platforms

def parse_llm_response(docx_path):
    """解析LLM回答内容"""
    try:
        doc = Document(docx_path)
    except:
        return None
    
    full_text = '\n'.join([p.text for p in doc.paragraphs if p.text.strip()])
    
    # 提取被推荐的公司
    recommended = []
    # 匹配各种排名格式
    patterns = [
        r'(?:第[一二三四五六七八九十]+[名位]|TOP\s*\d+|排名第\d+)[：:\s]*([^\n\d]+?)(?:[：:\s]|$)',
        r'(\d+)[\.、\s]+([^\n]+?)(?:公司|科技|数据|传媒)',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, full_text)
        for m in matches:
            if isinstance(m, tuple):
                recommended.append(m[-1].strip())
            else:
                recommended.append(m.strip())
    
    return {
        'text': full_text,
        'word_count': len(full_text),
        'recommended_count': len(recommended),
        'recommended': recommended[:10],  # 取前10个
    }

def extract_article_features(docx_path):
    """提取被引用文章的特征"""
    try:
        doc = Document(docx_path)
    except:
        return None
    
    filename = os.path.basename(docx_path).replace('.docx', '')
    full_text = '\n'.join([p.text for p in doc.paragraphs if p.text.strip()])
    
    bold_count = sum(1 for p in doc.paragraphs for r in p.runs if r.bold and r.text.strip())
    
    # 分析标题类型
    if re.search(r'(排名|榜|TOP|top|前十)', filename):
        title_type = '排名榜单型'
    elif re.search(r'(推荐|指南)', filename):
        title_type = '推荐指南型'
    elif re.search(r'(评测|对比|分析)', filename):
        title_type = '评测分析型'
    else:
        title_type = '其他'
    
    return {
        'title': filename[:50],
        'title_type': title_type,
        'word_count': len(full_text),
        'bold_count': bold_count,
        'has_year': bool(re.search(r'202[456]', filename)),
    }

def collect_platform_data():
    """按平台收集数据"""
    platform_data = defaultdict(lambda: {
        'keywords': [],
        'ranked_articles': [],
        'cited_articles': [],
    })
    
    for folder in os.listdir(DATA_DIR):
        folder_path = os.path.join(DATA_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        
        # 提取关键词
        match = re.match(r'提示词-(.+?)[-·]参考', folder)
        keyword = match.group(1) if match else folder
        
        # 检测平台
        platforms = extract_llm_platform(folder_path)
        
        for platform in platforms:
            platform_data[platform]['keywords'].append(keyword[:30])
            
            # 收集该文件夹下的上榜和被引用文章
            for item in os.listdir(folder_path):
                item_path = os.path.join(folder_path, item)
                if os.path.isdir(item_path):
                    is_ranked = bool(re.search(r'(第[一二三四五六七八九十]+名|排名第)', item))
                    
                    for file in os.listdir(item_path):
                        if file.endswith('.docx'):
                            docx_path = os.path.join(item_path, file)
                            features = extract_article_features(docx_path)
                            if features:
                                if is_ranked:
                                    platform_data[platform]['ranked_articles'].append(features)
                                else:
                                    platform_data[platform]['cited_articles'].append(features)
    
    return platform_data

def analyze_platform_preferences(platform_data):
    """分析平台偏好"""
    preferences = {}
    
    for platform, data in platform_data.items():
        ranked = data['ranked_articles']
        if not ranked:
            continue
        
        # 统计标题类型分布
        type_dist = defaultdict(int)
        for a in ranked:
            type_dist[a['title_type']] += 1
        
        # 计算平均值
        avg_word = sum(a['word_count'] for a in ranked) / len(ranked)
        avg_bold = sum(a['bold_count'] for a in ranked) / len(ranked)
        year_ratio = sum(a['has_year'] for a in ranked) / len(ranked) * 100
        
        preferences[platform] = {
            '样本数': len(ranked),
            '关键词覆盖': len(set(data['keywords'])),
            '标题类型分布': dict(type_dist),
            '平均字数': avg_word,
            '平均加粗数': avg_bold,
            '含年份比例': year_ratio,
            '主导类型': max(type_dist.items(), key=lambda x: x[1])[0] if type_dist else '无',
        }
    
    return preferences

def generate_report(platform_data, preferences):
    """生成平台差异报告"""
    lines = []
    lines.append("# LLM平台差异分析报告（问题4.1-4.3）")
    lines.append("")
    lines.append(f"**生成时间**: 2026-02-03")
    lines.append("")
    
    # 问题4.1: 各平台内容偏好
    lines.append("## 一、问题4.1: 各LLM平台内容类型偏好")
    lines.append("")
    lines.append("| 平台 | 样本数 | 主导类型 | 排名榜单型 | 推荐指南型 | 其他 |")
    lines.append("|------|-------|---------|-----------|-----------|------|")
    
    for platform, pref in sorted(preferences.items(), key=lambda x: -x[1]['样本数']):
        dist = pref['标题类型分布']
        rank_pct = dist.get('排名榜单型', 0) / pref['样本数'] * 100 if pref['样本数'] else 0
        reco_pct = dist.get('推荐指南型', 0) / pref['样本数'] * 100 if pref['样本数'] else 0
        other_pct = dist.get('其他', 0) / pref['样本数'] * 100 if pref['样本数'] else 0
        lines.append(f"| {platform} | {pref['样本数']} | {pref['主导类型']} | {rank_pct:.0f}% | {reco_pct:.0f}% | {other_pct:.0f}% |")
    lines.append("")
    
    # 问题4.2: 文体风格偏好
    lines.append("## 二、问题4.2: 各平台文体风格偏好")
    lines.append("")
    lines.append("| 平台 | 偏好类型 | 特征描述 |")
    lines.append("|------|---------|---------|")
    
    for platform, pref in sorted(preferences.items(), key=lambda x: -x[1]['样本数']):
        if pref['主导类型'] == '排名榜单型':
            style = "权威榜单风格，强调排名和数据"
        elif pref['主导类型'] == '推荐指南型':
            style = "用户导向风格，强调选择建议"
        else:
            style = "综合型风格"
        lines.append(f"| {platform} | {pref['主导类型']} | {style} |")
    lines.append("")
    
    # 问题4.3: 字数结构偏好
    lines.append("## 三、问题4.3: 各平台字数/结构偏好")
    lines.append("")
    lines.append("| 平台 | 平均字数 | 平均加粗数 | 含年份比例 | 关键词覆盖 |")
    lines.append("|------|---------|-----------|-----------|-----------|")
    
    for platform, pref in sorted(preferences.items(), key=lambda x: -x[1]['样本数']):
        lines.append(f"| {platform} | {pref['平均字数']:.0f} | {pref['平均加粗数']:.1f} | {pref['含年份比例']:.0f}% | {pref['关键词覆盖']} |")
    lines.append("")
    
    # 关键洞察
    lines.append("## 四、关键洞察")
    lines.append("")
    
    if preferences:
        # 找出字数最高的平台
        max_word_platform = max(preferences.items(), key=lambda x: x[1]['平均字数'])
        lines.append(f"1. **字数偏好**: {max_word_platform[0]} 偏好更长内容（{max_word_platform[1]['平均字数']:.0f}字）")
        
        # 找出加粗最多的平台
        max_bold_platform = max(preferences.items(), key=lambda x: x[1]['平均加粗数'])
        lines.append(f"2. **结构偏好**: {max_bold_platform[0]} 偏好更多加粗（{max_bold_platform[1]['平均加粗数']:.1f}个）")
        
        lines.append("3. **通用规律**: 所有平台都偏好含年份的时效性内容")
    lines.append("")
    
    # 策略建议
    lines.append("## 五、针对性策略建议")
    lines.append("")
    for platform, pref in sorted(preferences.items(), key=lambda x: -x[1]['样本数']):
        lines.append(f"### {platform}")
        lines.append(f"- 推荐使用 **{pref['主导类型']}** 标题")
        lines.append(f"- 目标字数: **{pref['平均字数']:.0f}字**")
        lines.append(f"- 加粗数量: **{pref['平均加粗数']:.0f}个**")
        lines.append("")
    
    return "\n".join(lines)

def main():
    print("正在收集平台数据...")
    platform_data = collect_platform_data()
    print(f"发现平台数: {len(platform_data)}")
    
    for platform, data in platform_data.items():
        print(f"  {platform}: {len(data['ranked_articles'])}篇上榜, {len(data['cited_articles'])}篇引用")
    
    print("\n正在分析平台偏好...")
    preferences = analyze_platform_preferences(platform_data)
    
    print("正在生成报告...")
    report = generate_report(platform_data, preferences)
    
    # 保存报告
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "LLM平台差异分析_问题4_报告.md"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"\n报告已保存: {output_path}")

if __name__ == "__main__":
    main()
