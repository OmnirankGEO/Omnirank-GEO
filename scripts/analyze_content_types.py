# -*- coding: utf-8 -*-
"""
分析上榜文章的内容特征
问题2.1: 上榜文章有哪几种内容类型？各占比多少？
问题2.2: 上榜 vs 仅被引用的核心差异是什么？
"""
import os
import re
from pathlib import Path
from collections import defaultdict
from docx import Document
from docx.opc.exceptions import PackageNotFoundError

DATA_DIR = r"C:\AI-Test\AgentsCope-07\Benchmark against"

def extract_docx_features(docx_path):
    """提取DOCX文件的特征"""
    try:
        doc = Document(docx_path)
    except (PackageNotFoundError, Exception) as e:
        return None
    
    # 从文件名提取标题（去掉.docx后缀）
    filename = os.path.basename(docx_path)
    title = filename.replace('.docx', '')
    
    # 基础统计
    full_text = []
    bold_count = 0
    heading_count = 0
    list_count = 0
    
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            full_text.append(text)
            
            # 统计加粗
            for run in para.runs:
                if run.bold and run.text.strip():
                    bold_count += 1
            
            # 统计标题
            if para.style.name.startswith('Heading'):
                heading_count += 1
            
            # 统计列表
            if text.startswith(('•', '-', '·', '1.', '1、', '①')):
                list_count += 1
    
    content = '\n'.join(full_text)
    word_count = len(content)
    
    # 分析标题类型
    title_type = classify_title(title)
    
    return {
        'path': docx_path,
        'title': title[:60],
        'title_type': title_type,
        'word_count': word_count,
        'bold_count': bold_count,
        'heading_count': heading_count,
        'list_count': list_count,
        'has_year': bool(re.search(r'202[456]', title)),
        'has_ranking': bool(re.search(r'(排名|榜|TOP|top|前十|前\d)', title)),
        'has_recommend': bool(re.search(r'(推荐|指南|攻略)', title)),
    }

def classify_title(title):
    """分类标题类型"""
    if re.search(r'(排名|榜|TOP|top|前十|前\d)', title):
        return '排名榜单型'
    elif re.search(r'(推荐|指南|攻略|怎么选|如何选)', title):
        return '推荐指南型'
    elif re.search(r'(评测|对比|分析|测评)', title):
        return '评测分析型'
    elif re.search(r'(什么是|介绍|概述|了解)', title):
        return '科普介绍型'
    elif re.search(r'(案例|实战|经验|分享)', title):
        return '案例分享型'
    else:
        return '其他'

def collect_all_articles():
    """收集所有文章并分类为上榜/仅引用"""
    ranked_articles = []
    cited_articles = []
    
    for folder in os.listdir(DATA_DIR):
        folder_path = os.path.join(DATA_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        
        for item in os.listdir(folder_path):
            item_path = os.path.join(folder_path, item)
            
            # 跳过LLM回答文件
            if item.endswith('.docx') and '回答' in item:
                continue
            
            if os.path.isdir(item_path):
                # 判断是上榜还是仅引用
                is_ranked = bool(re.search(r'(第[一二三四五六七八九十]+名|排名第)', item))
                
                # 遍历子目录中的docx
                for file in os.listdir(item_path):
                    if file.endswith('.docx'):
                        docx_path = os.path.join(item_path, file)
                        features = extract_docx_features(docx_path)
                        if features:
                            features['category'] = '上榜' if is_ranked else '仅引用'
                            features['source_folder'] = folder
                            if is_ranked:
                                ranked_articles.append(features)
                            else:
                                cited_articles.append(features)
    
    return ranked_articles, cited_articles

def analyze_differences(ranked, cited):
    """分析上榜与仅引用的差异"""
    def avg(lst, key):
        vals = [x[key] for x in lst if x[key] is not None]
        return sum(vals) / len(vals) if vals else 0
    
    def ratio(lst, key):
        vals = [x[key] for x in lst]
        return sum(vals) / len(vals) * 100 if vals else 0
    
    return {
        '上榜': {
            '样本数': len(ranked),
            '平均字数': avg(ranked, 'word_count'),
            '平均加粗数': avg(ranked, 'bold_count'),
            '平均标题数': avg(ranked, 'heading_count'),
            '含年份比例%': ratio(ranked, 'has_year'),
            '含排名词比例%': ratio(ranked, 'has_ranking'),
            '含推荐词比例%': ratio(ranked, 'has_recommend'),
        },
        '仅引用': {
            '样本数': len(cited),
            '平均字数': avg(cited, 'word_count'),
            '平均加粗数': avg(cited, 'bold_count'),
            '平均标题数': avg(cited, 'heading_count'),
            '含年份比例%': ratio(cited, 'has_year'),
            '含排名词比例%': ratio(cited, 'has_ranking'),
            '含推荐词比例%': ratio(cited, 'has_recommend'),
        }
    }

def generate_report(ranked, cited):
    """生成Markdown报告"""
    lines = []
    lines.append("# LLM引用内容类型研究报告（问题2.1-2.2）")
    lines.append("")
    lines.append(f"**生成时间**: 2026-02-03")
    lines.append("")
    
    # 问题2.1: 内容类型分布
    lines.append("## 一、问题2.1: 上榜文章内容类型分布")
    lines.append("")
    
    # 统计标题类型
    ranked_types = defaultdict(int)
    cited_types = defaultdict(int)
    for a in ranked:
        ranked_types[a['title_type']] += 1
    for a in cited:
        cited_types[a['title_type']] += 1
    
    lines.append("### 上榜文章标题类型")
    lines.append("")
    lines.append("| 类型 | 数量 | 占比 |")
    lines.append("|------|------|------|")
    for t, c in sorted(ranked_types.items(), key=lambda x: -x[1]):
        pct = c * 100 / len(ranked) if ranked else 0
        lines.append(f"| {t} | {c} | {pct:.1f}% |")
    lines.append("")
    
    lines.append("### 仅引用文章标题类型")
    lines.append("")
    lines.append("| 类型 | 数量 | 占比 |")
    lines.append("|------|------|------|")
    for t, c in sorted(cited_types.items(), key=lambda x: -x[1]):
        pct = c * 100 / len(cited) if cited else 0
        lines.append(f"| {t} | {c} | {pct:.1f}% |")
    lines.append("")
    
    # 问题2.2: 上榜vs仅引用差异
    lines.append("## 二、问题2.2: 上榜 vs 仅引用核心差异")
    lines.append("")
    
    diff = analyze_differences(ranked, cited)
    
    lines.append("### 量化对比")
    lines.append("")
    lines.append("| 指标 | 上榜 | 仅引用 | 差异 |")
    lines.append("|------|------|--------|------|")
    
    for key in ['样本数', '平均字数', '平均加粗数', '平均标题数', '含年份比例%', '含排名词比例%', '含推荐词比例%']:
        v1 = diff['上榜'][key]
        v2 = diff['仅引用'][key]
        if key == '样本数':
            d = '-'
        elif isinstance(v1, float):
            d = f"{v1-v2:+.1f}"
        else:
            d = f"{v1-v2:+.0f}"
        lines.append(f"| {key} | {v1:.1f} | {v2:.1f} | {d} |")
    lines.append("")
    
    # 关键洞察
    lines.append("### 关键洞察")
    lines.append("")
    
    if diff['上榜']['平均字数'] > diff['仅引用']['平均字数']:
        lines.append(f"1. **字数更多**：上榜文章平均 {diff['上榜']['平均字数']:.0f} 字 vs 仅引用 {diff['仅引用']['平均字数']:.0f} 字")
    else:
        lines.append(f"1. **字数相近或更少**：上榜 {diff['上榜']['平均字数']:.0f} 字 vs 仅引用 {diff['仅引用']['平均字数']:.0f} 字")
    
    if diff['上榜']['含年份比例%'] > diff['仅引用']['含年份比例%']:
        lines.append(f"2. **时效性更强**：上榜文章含年份比例 {diff['上榜']['含年份比例%']:.1f}% vs 仅引用 {diff['仅引用']['含年份比例%']:.1f}%")
    
    if diff['上榜']['含排名词比例%'] > diff['仅引用']['含排名词比例%']:
        lines.append(f"3. **排名信号更明确**：上榜含排名词 {diff['上榜']['含排名词比例%']:.1f}% vs 仅引用 {diff['仅引用']['含排名词比例%']:.1f}%")
    
    lines.append("")
    
    # 样本展示
    lines.append("## 三、上榜文章标题示例（前10）")
    lines.append("")
    for i, a in enumerate(ranked[:10], 1):
        lines.append(f"{i}. {a['title']}")
    lines.append("")
    
    return "\n".join(lines)

def main():
    print("正在收集文章...")
    ranked, cited = collect_all_articles()
    print(f"上榜文章: {len(ranked)}篇")
    print(f"仅引用文章: {len(cited)}篇")
    
    print("\n正在分析...")
    report = generate_report(ranked, cited)
    
    # 保存报告
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "LLM引用内容类型研究_问题2_报告.md"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"\n报告已保存: {output_path}")

if __name__ == "__main__":
    main()
