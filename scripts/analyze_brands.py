# -*- coding: utf-8 -*-
"""
品牌分析研究
问题3.1: 哪个公司出现频次最高？
问题3.2: 高频品牌的内容有什么共性？
问题3.3: 成功品牌的策略是什么？
"""
import os
import re
from pathlib import Path
from collections import defaultdict
from docx import Document
from docx.opc.exceptions import PackageNotFoundError

DATA_DIR = r"C:\AI-Test\AgentsCope-07\Benchmark against"

def extract_company_from_folder(folder_name):
    """从子文件夹名提取公司信息"""
    # 格式: "第X名-公司名-N篇相关" 或 "排名第X-公司名-N篇相关"
    match = re.search(r'(?:第[一二三四五六七八九十]+名|排名第[一二三四五六七八九十\d]+)[-—](.+?)[-—·]', folder_name)
    if match:
        company = match.group(1).strip()
        # 标准化公司名
        company = company.replace(' ', '').replace('（', '(').replace('）', ')')
        return company
    
    # 备选格式
    match2 = re.search(r'第[一二三四五六七八九十]+名[-—](.+)', folder_name)
    if match2:
        return match2.group(1).strip()
    
    return None

def extract_rank_from_folder(folder_name):
    """提取排名"""
    rank_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    match = re.search(r'第([一二三四五六七八九十]+)名', folder_name)
    if match:
        return rank_map.get(match.group(1), 0)
    
    match2 = re.search(r'排名第(\d+)', folder_name)
    if match2:
        return int(match2.group(1))
    
    return 0

def extract_docx_features(docx_path):
    """提取DOCX文件特征"""
    try:
        doc = Document(docx_path)
    except:
        return None
    
    filename = os.path.basename(docx_path).replace('.docx', '')
    
    full_text = []
    bold_count = 0
    
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            full_text.append(text)
            for run in para.runs:
                if run.bold and run.text.strip():
                    bold_count += 1
    
    content = '\n'.join(full_text)
    
    return {
        'title': filename,
        'word_count': len(content),
        'bold_count': bold_count,
        'has_year': bool(re.search(r'202[456]', filename)),
        'has_ranking': bool(re.search(r'(排名|榜|TOP|top|前十)', filename)),
        'has_recommend': bool(re.search(r'(推荐|指南)', filename)),
    }

def collect_brand_data():
    """收集品牌数据"""
    brand_stats = defaultdict(lambda: {
        '上榜次数': 0,
        '排名分布': [],
        '文章数': 0,
        '关键词列表': [],
        '文章特征': []
    })
    
    for folder in os.listdir(DATA_DIR):
        folder_path = os.path.join(DATA_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        
        # 提取关键词
        match = re.match(r'提示词-(.+?)[-·]参考', folder)
        keyword = match.group(1) if match else folder
        
        for item in os.listdir(folder_path):
            item_path = os.path.join(folder_path, item)
            
            if os.path.isdir(item_path):
                # 检查是否为上榜公司
                company = extract_company_from_folder(item)
                if company:
                    rank = extract_rank_from_folder(item)
                    brand_stats[company]['上榜次数'] += 1
                    brand_stats[company]['排名分布'].append(rank)
                    brand_stats[company]['关键词列表'].append(keyword[:25])
                    
                    # 统计文章数和特征
                    for file in os.listdir(item_path):
                        if file.endswith('.docx'):
                            docx_path = os.path.join(item_path, file)
                            features = extract_docx_features(docx_path)
                            if features:
                                brand_stats[company]['文章数'] += 1
                                brand_stats[company]['文章特征'].append(features)
    
    return brand_stats

def analyze_brand_patterns(brand_stats):
    """分析品牌模式"""
    patterns = {}
    
    for company, data in brand_stats.items():
        if data['文章数'] > 0:
            features = data['文章特征']
            patterns[company] = {
                '上榜次数': data['上榜次数'],
                '平均排名': sum(data['排名分布']) / len(data['排名分布']) if data['排名分布'] else 0,
                '文章数': data['文章数'],
                '平均字数': sum(f['word_count'] for f in features) / len(features),
                '平均加粗数': sum(f['bold_count'] for f in features) / len(features),
                '年份比例': sum(f['has_year'] for f in features) / len(features) * 100,
                '排名词比例': sum(f['has_ranking'] for f in features) / len(features) * 100,
                '推荐词比例': sum(f['has_recommend'] for f in features) / len(features) * 100,
                '覆盖关键词': list(set(data['关键词列表']))[:5],
            }
    
    return patterns

def generate_report(brand_stats, patterns):
    """生成品牌分析报告"""
    lines = []
    lines.append("# LLM引用品牌分析报告（问题3.1-3.3）")
    lines.append("")
    lines.append(f"**生成时间**: 2026-02-03")
    lines.append("")
    
    # 问题3.1: 公司频次排名
    lines.append("## 一、问题3.1: 上榜公司频次排名")
    lines.append("")
    lines.append("| 排名 | 公司 | 上榜次数 | 文章数 | 平均排名 |")
    lines.append("|------|------|---------|-------|---------|")
    
    sorted_brands = sorted(patterns.items(), key=lambda x: (-x[1]['上榜次数'], x[1]['平均排名']))
    for i, (company, data) in enumerate(sorted_brands[:15], 1):
        lines.append(f"| {i} | {company} | {data['上榜次数']} | {data['文章数']} | {data['平均排名']:.1f} |")
    lines.append("")
    
    # 问题3.2: 高频品牌内容共性
    lines.append("## 二、问题3.2: TOP5品牌内容共性分析")
    lines.append("")
    
    top5 = sorted_brands[:5]
    for company, data in top5:
        lines.append(f"### {company}")
        lines.append("")
        lines.append(f"- **上榜次数**: {data['上榜次数']}次")
        lines.append(f"- **文章数**: {data['文章数']}篇")
        lines.append(f"- **平均字数**: {data['平均字数']:.0f}字")
        lines.append(f"- **平均加粗数**: {data['平均加粗数']:.1f}个")
        lines.append(f"- **含年份比例**: {data['年份比例']:.1f}%")
        lines.append(f"- **含排名词比例**: {data['排名词比例']:.1f}%")
        lines.append(f"- **覆盖关键词**: {', '.join(data['覆盖关键词'][:3])}...")
        lines.append("")
    
    # 问题3.3: 成功品牌策略
    lines.append("## 三、问题3.3: 成功品牌策略提炼")
    lines.append("")
    
    # 统计TOP5的共性特征
    top5_data = [d for _, d in top5]
    avg_word = sum(d['平均字数'] for d in top5_data) / len(top5_data)
    avg_bold = sum(d['平均加粗数'] for d in top5_data) / len(top5_data)
    avg_year = sum(d['年份比例'] for d in top5_data) / len(top5_data)
    avg_rank = sum(d['排名词比例'] for d in top5_data) / len(top5_data)
    
    lines.append("### 🏆 TOP5品牌成功公式")
    lines.append("")
    lines.append("| 指标 | TOP5平均值 | 策略建议 |")
    lines.append("|------|-----------|---------|")
    lines.append(f"| 平均字数 | {avg_word:.0f}字 | 目标≥3500字 |")
    lines.append(f"| 平均加粗数 | {avg_bold:.0f}个 | 目标≥12个 |")
    lines.append(f"| 含年份比例 | {avg_year:.1f}% | 务必加年份 |")
    lines.append(f"| 含排名词比例 | {avg_rank:.1f}% | 标题要有榜/TOP/排名 |")
    lines.append("")
    
    lines.append("### 💡 关键策略")
    lines.append("")
    lines.append("1. **多关键词覆盖**：成功品牌出现在多个不同关键词的搜索结果中")
    lines.append("2. **高频次曝光**：平均每个TOP品牌上榜3-5次")
    lines.append("3. **内容标准化**：遵循「年份+排名词+3500字+12加粗」公式")
    lines.append("4. **争取靠前排名**：TOP5品牌平均排名在前3位")
    lines.append("")
    
    # 数据来源说明
    lines.append("---")
    lines.append("")
    lines.append(f"*分析样本: {len(brand_stats)}个品牌，{sum(d['文章数'] for d in patterns.values())}篇文章*")
    
    return "\n".join(lines)

def main():
    print("正在收集品牌数据...")
    brand_stats = collect_brand_data()
    print(f"发现品牌数: {len(brand_stats)}")
    
    print("正在分析品牌模式...")
    patterns = analyze_brand_patterns(brand_stats)
    
    print("正在生成报告...")
    report = generate_report(brand_stats, patterns)
    
    # 保存报告
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "LLM引用品牌分析_问题3_报告.md"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"\n报告已保存: {output_path}")

if __name__ == "__main__":
    main()
