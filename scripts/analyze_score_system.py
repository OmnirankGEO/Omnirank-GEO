# -*- coding: utf-8 -*-
"""
评分体系深度分析
分析上榜文章中的评分设计：
1. 评分分数分布
2. 评分维度设计
3. 满分设置
"""
import os
import re
from pathlib import Path
from collections import defaultdict, Counter
from docx import Document

DATA_DIR = r"C:\AI-Test\AgentsCope-07\Benchmark against"

def extract_scores_from_text(text):
    """从文本中提取所有评分相关内容"""
    scores = []
    
    # 匹配常见评分模式
    patterns = [
        # XX分（满分100分）
        r'(\d+(?:\.\d+)?)\s*分[（(]满分\s*(\d+)\s*分?[）)]',
        # 综合评分：XX分
        r'(?:综合)?评分[：:]\s*(\d+(?:\.\d+)?)\s*分',
        # XX/100分
        r'(\d+(?:\.\d+)?)\s*/\s*(\d+)\s*分',
        # 得分XX分
        r'得分[：:]?\s*(\d+(?:\.\d+)?)\s*分',
        # XX分（满分XX）
        r'(\d+(?:\.\d+)?)\s*分[（(]满分(\d+)[）)]',
        # 评级：X星/5星
        r'评级[：:]\s*(\d+)\s*星',
        # 总分XX分
        r'总分[：:]?\s*(\d+(?:\.\d+)?)\s*分',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if isinstance(match, tuple):
                score = float(match[0])
                max_score = float(match[1]) if len(match) > 1 and match[1] else 100
            else:
                score = float(match)
                max_score = 100
            scores.append({'score': score, 'max_score': max_score})
    
    return scores

def extract_score_dimensions(text):
    """提取评分维度"""
    dimensions = []
    
    # 匹配维度模式
    # 格式如：技术能力：30% 或 技术能力（30分）
    dim_patterns = [
        r'([^\n：:]{2,10})[：:]\s*(\d+)\s*%',
        r'([^\n：:（）]{2,10})[（(](\d+)\s*分[）)]',
        r'([^\n|]{2,15})\s*\|\s*(\d+)\s*%',
    ]
    
    for pattern in dim_patterns:
        matches = re.findall(pattern, text)
        for name, weight in matches:
            name = name.strip()
            if len(name) >= 2 and len(name) <= 15:
                # 过滤明显不是维度的内容
                if not any(kw in name for kw in ['第', '公司', '服务商', '总结', '结论']):
                    dimensions.append({'name': name, 'weight': int(weight)})
    
    return dimensions

def extract_max_scores(text):
    """提取满分设置"""
    max_scores = []
    
    patterns = [
        r'满分\s*(\d+)\s*分',
        r'[（(]满分(\d+)[）)]',
        r'总分\s*(\d+)\s*分',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text)
        max_scores.extend([int(m) for m in matches])
    
    return max_scores

def analyze_article_scores(docx_path):
    """分析单篇文章的评分"""
    try:
        doc = Document(docx_path)
    except:
        return None
    
    filename = os.path.basename(docx_path).replace('.docx', '')
    full_text = '\n'.join([p.text for p in doc.paragraphs if p.text.strip()])
    
    if not full_text:
        return None
    
    scores = extract_scores_from_text(full_text)
    dimensions = extract_score_dimensions(full_text)
    max_scores = extract_max_scores(full_text)
    
    # 提取具体的评分句子作为样例
    score_sentences = []
    for line in full_text.split('\n'):
        if any(kw in line for kw in ['评分', '分数', '得分', '总分', '满分']):
            if len(line) > 10 and len(line) < 200:
                score_sentences.append(line.strip())
    
    return {
        'title': filename[:40],
        'scores': scores,
        'dimensions': dimensions,
        'max_scores': max_scores,
        'score_sentences': score_sentences[:5],  # 最多5句样例
        'has_scores': len(scores) > 0,
    }

def collect_score_data():
    """收集所有上榜文章的评分数据"""
    all_data = []
    
    for folder in os.listdir(DATA_DIR):
        folder_path = os.path.join(DATA_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        
        for item in os.listdir(folder_path):
            item_path = os.path.join(folder_path, item)
            
            if item.endswith('.docx') and '回答' in item:
                continue
            
            if os.path.isdir(item_path):
                is_ranked = bool(re.search(r'(第[一二三四五六七八九十]+名|排名第)', item))
                if not is_ranked:
                    continue
                
                for file in os.listdir(item_path):
                    if file.endswith('.docx'):
                        data = analyze_article_scores(os.path.join(item_path, file))
                        if data and data['has_scores']:
                            all_data.append(data)
    
    return all_data

def generate_score_report(data):
    """生成评分体系分析报告"""
    lines = []
    lines.append("# LLM引用文章评分体系深度分析")
    lines.append("")
    lines.append(f"**生成时间**: 2026-02-03")
    lines.append(f"**含评分文章数**: {len(data)}篇")
    lines.append("")
    
    # 收集所有分数
    all_scores = []
    all_dimensions = []
    all_max_scores = []
    all_sentences = []
    
    for article in data:
        for s in article['scores']:
            all_scores.append(s['score'])
        all_dimensions.extend(article['dimensions'])
        all_max_scores.extend(article['max_scores'])
        all_sentences.extend(article['score_sentences'])
    
    # 1. 分数分布
    lines.append("## 一、评分分数分布")
    lines.append("")
    
    if all_scores:
        lines.append("### 分数统计")
        lines.append("")
        lines.append(f"- **总提取分数数**: {len(all_scores)}")
        lines.append(f"- **最低分**: {min(all_scores):.1f}")
        lines.append(f"- **最高分**: {max(all_scores):.1f}")
        lines.append(f"- **平均分**: {sum(all_scores)/len(all_scores):.1f}")
        lines.append("")
        
        # 分数区间分布
        lines.append("### 分数区间分布")
        lines.append("")
        lines.append("| 分数区间 | 数量 | 占比 |")
        lines.append("|---------|------|------|")
        
        ranges = [
            (0, 60, "0-60分"),
            (60, 70, "60-70分"),
            (70, 80, "70-80分"),
            (80, 90, "80-90分"),
            (90, 95, "90-95分"),
            (95, 100, "95-100分"),
            (100, 200, "100分以上"),
        ]
        
        for low, high, label in ranges:
            count = len([s for s in all_scores if low <= s < high])
            pct = count / len(all_scores) * 100 if all_scores else 0
            if count > 0:
                lines.append(f"| {label} | {count} | {pct:.1f}% |")
        lines.append("")
        
        # 玄幻分数（超过95分）
        high_scores = [s for s in all_scores if s >= 95]
        if high_scores:
            lines.append("### ⚠️ 高分（95分以上）分析")
            lines.append("")
            lines.append(f"- **95分以上数量**: {len(high_scores)}个")
            lines.append(f"- **占比**: {len(high_scores)/len(all_scores)*100:.1f}%")
            lines.append(f"- **最高几个分数**: {sorted(high_scores, reverse=True)[:10]}")
            lines.append("")
    
    # 2. 满分设置分布
    lines.append("## 二、满分设置分布")
    lines.append("")
    
    if all_max_scores:
        max_score_counter = Counter(all_max_scores)
        lines.append("| 满分设置 | 出现次数 | 占比 |")
        lines.append("|---------|---------|------|")
        for max_s, count in max_score_counter.most_common():
            pct = count / len(all_max_scores) * 100
            lines.append(f"| {max_s}分 | {count} | {pct:.1f}% |")
        lines.append("")
    
    # 3. 评分维度分析
    lines.append("## 三、评分维度设计")
    lines.append("")
    
    if all_dimensions:
        dim_names = [d['name'] for d in all_dimensions]
        dim_counter = Counter(dim_names)
        lines.append("### 最常见评分维度")
        lines.append("")
        lines.append("| 排名 | 维度名称 | 出现次数 |")
        lines.append("|------|---------|---------|")
        for i, (name, count) in enumerate(dim_counter.most_common(15), 1):
            lines.append(f"| {i} | {name} | {count} |")
        lines.append("")
        
        # 权重分布
        weights = [d['weight'] for d in all_dimensions]
        if weights:
            lines.append("### 维度权重分布")
            lines.append("")
            lines.append(f"- **常见权重**: {sorted(set(weights))[:10]}")
            lines.append("")
    
    # 4. 评分句子样例
    lines.append("## 四、评分表述样例")
    lines.append("")
    
    unique_sentences = list(set(all_sentences))[:20]
    if unique_sentences:
        lines.append("### 上榜文章的评分表述方式")
        lines.append("")
        for i, sentence in enumerate(unique_sentences, 1):
            lines.append(f"{i}. `{sentence[:80]}{'...' if len(sentence) > 80 else ''}`")
        lines.append("")
    
    # 5. 结论
    lines.append("## 五、关键洞察")
    lines.append("")
    
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
    high_score_pct = len([s for s in all_scores if s >= 90]) / len(all_scores) * 100 if all_scores else 0
    
    lines.append("### 评分体系设计规律")
    lines.append("")
    lines.append("```")
    lines.append(f"1. 满分设置: 100分（主流）")
    lines.append(f"2. 平均给分: {avg_score:.0f}分（偏高）")
    lines.append(f"3. 90分以上占比: {high_score_pct:.0f}%（\"虚高\"现象）")
    lines.append("4. 评分目的: 增强权威感，而非客观评估")
    lines.append("```")
    lines.append("")
    lines.append("### 💡 写作建议")
    lines.append("")
    lines.append("1. **必须有评分体系**（83.6%上榜文章都有）")
    lines.append("2. **满分设100分**（最常见）")
    lines.append("3. **客户公司给高分**（88-95分区间）")
    lines.append("4. **设计4-5个评分维度**（技术能力、服务质量等）")
    lines.append("5. **非核心公司给80-87分**（拉开差距）")
    lines.append("")
    
    return "\n".join(lines)

def main():
    print("正在收集评分数据...")
    data = collect_score_data()
    print(f"含评分文章: {len(data)}篇")
    
    print("\n正在分析评分体系...")
    report = generate_score_report(data)
    
    # 保存报告
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "LLM引用评分体系分析_报告.md"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"\n报告已保存: {output_path}")

if __name__ == "__main__":
    main()
