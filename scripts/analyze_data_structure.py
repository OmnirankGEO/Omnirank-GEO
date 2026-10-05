# -*- coding: utf-8 -*-
"""
分析 Benchmark against 目录结构
统计60个关键词文件夹的数据分布
"""
import os
import re
from pathlib import Path
from collections import defaultdict

DATA_DIR = r"C:\AI-Test\AgentsCope-07\Benchmark against"

def extract_keyword_info(folder_name):
    """从文件夹名提取关键词信息"""
    # 格式: 提示词-XXX-参考资料N篇 or 提示词-XXX-参考文章N篇
    match = re.match(r'提示词-(.+?)[-·]参考[资料文章]+(\d+)篇?', folder_name)
    if match:
        keyword = match.group(1)
        article_count = int(match.group(2))
        return keyword, article_count
    
    # 备选格式
    match2 = re.match(r'提示词-(.+?)-(\d+)篇', folder_name)
    if match2:
        return match2.group(1), int(match2.group(2))
    
    return folder_name.replace('提示词-', ''), 0

def classify_keyword(keyword):
    """对关键词进行分类"""
    if 'GEO搜索优化公司排名' in keyword or 'ai搜索优化公司排名' in keyword:
        return '排名型-全国'
    elif '深圳' in keyword:
        if '排名' in keyword:
            return '排名型-地域(深圳)'
        elif '推荐' in keyword:
            return '推荐型-地域(深圳)'
        elif '哪家好' in keyword or '怎么挑选' in keyword or '如何选择' in keyword:
            return '选择型-地域(深圳)'
        elif '怎么样' in keyword:
            return '评价型-地域(深圳)'
        elif '有哪些' in keyword:
            return '列举型-地域(深圳)'
        else:
            return '其他-地域(深圳)'
    elif '国内' in keyword:
        return '排名型-国内'
    else:
        return '其他'

def analyze_subfolder(folder_path):
    """分析子文件夹，区分上榜/被引用"""
    ranked_companies = []  # 上榜公司
    cited_articles = 0     # 仅被引用文章数
    llm_responses = []     # LLM回答文件
    
    for item in os.listdir(folder_path):
        item_path = os.path.join(folder_path, item)
        
        if item.endswith('.docx'):
            if '回答' in item:
                llm_responses.append(item)
        elif os.path.isdir(item_path):
            # 检查是否为上榜公司目录
            rank_match = re.search(r'(第[一二三四五六七八九十]+名|排名第[一二三四五六七八九十\d]+)', item)
            if rank_match:
                # 提取公司名和文章数
                company_match = re.search(r'[-—](.+?)[-—·]', item)
                company_name = company_match.group(1) if company_match else item
                
                # 统计该目录下的docx数量
                docx_count = len([f for f in os.listdir(item_path) if f.endswith('.docx')])
                ranked_companies.append({
                    'rank': rank_match.group(1),
                    'company': company_name,
                    'articles': docx_count
                })
            elif '被提及' in item or '参考' in item or '相关' in item:
                # 仅被引用的资料
                docx_count = len([f for f in os.listdir(item_path) if f.endswith('.docx')])
                cited_articles += docx_count
    
    return ranked_companies, cited_articles, llm_responses

def main():
    folders = [f for f in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, f))]
    
    print(f"=" * 60)
    print(f"数据目录分析报告")
    print(f"=" * 60)
    print(f"总文件夹数: {len(folders)}")
    print()
    
    # 分类统计
    category_stats = defaultdict(list)
    all_keywords = []
    llm_types = defaultdict(int)
    total_ranked = 0
    total_cited = 0
    all_companies = defaultdict(int)
    
    for folder in sorted(folders):
        folder_path = os.path.join(DATA_DIR, folder)
        keyword, expected_count = extract_keyword_info(folder)
        category = classify_keyword(keyword)
        
        ranked, cited, llm_files = analyze_subfolder(folder_path)
        
        category_stats[category].append({
            'keyword': keyword,
            'expected': expected_count,
            'ranked_companies': ranked,
            'cited_articles': cited
        })
        
        all_keywords.append(keyword)
        
        for llm_file in llm_files:
            if '豆包' in llm_file:
                llm_types['豆包'] += 1
            elif '千问' in llm_file:
                llm_types['千问'] += 1
            elif 'DeepSeek' in llm_file.lower():
                llm_types['DeepSeek'] += 1
            elif 'KIMI' in llm_file.upper():
                llm_types['KIMI'] += 1
            else:
                llm_types['其他'] += 1
        
        for company in ranked:
            all_companies[company['company']] += company['articles']
            total_ranked += company['articles']
        
        total_cited += cited
    
    # 输出分类统计
    print("-" * 60)
    print("一、关键词分类分布")
    print("-" * 60)
    for category, items in sorted(category_stats.items()):
        print(f"\n【{category}】共 {len(items)} 个关键词")
        for item in items:
            ranked_str = ", ".join([f"{c['company']}({c['articles']}篇)" for c in item['ranked_companies']])
            print(f"  • {item['keyword'][:30]}... | 期望{item['expected']}篇 | 上榜: {ranked_str or '无'} | 仅引用: {item['cited_articles']}篇")
    
    # LLM分布
    print("\n" + "-" * 60)
    print("二、LLM平台回答分布")
    print("-" * 60)
    for llm, count in sorted(llm_types.items(), key=lambda x: -x[1]):
        print(f"  {llm}: {count} 个文件夹")
    
    # 公司频次
    print("\n" + "-" * 60)
    print("三、上榜公司频次TOP15")
    print("-" * 60)
    top_companies = sorted(all_companies.items(), key=lambda x: -x[1])[:15]
    for i, (company, count) in enumerate(top_companies, 1):
        print(f"  {i:2}. {company}: {count}篇")
    
    # 汇总
    print("\n" + "-" * 60)
    print("四、数据汇总")
    print("-" * 60)
    print(f"  总关键词数: {len(folders)}")
    print(f"  关键词分类数: {len(category_stats)}")
    print(f"  上榜文章总数: {total_ranked}")
    print(f"  仅被引用文章数: {total_cited}")
    print(f"  文章总计: {total_ranked + total_cited}")

def save_report(content, filename):
    """保存报告到文件"""
    output_dir = Path(__file__).parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / filename
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"报告已保存: {output_path}")
    return output_path

def generate_markdown_report():
    """生成Markdown格式的研究报告"""
    folders = [f for f in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, f))]
    
    category_stats = defaultdict(list)
    llm_types = defaultdict(int)
    total_ranked = 0
    total_cited = 0
    all_companies = defaultdict(int)
    keyword_details = []
    
    for folder in sorted(folders):
        folder_path = os.path.join(DATA_DIR, folder)
        keyword, expected_count = extract_keyword_info(folder)
        category = classify_keyword(keyword)
        
        ranked, cited, llm_files = analyze_subfolder(folder_path)
        
        category_stats[category].append({
            'keyword': keyword,
            'expected': expected_count,
            'ranked_companies': ranked,
            'cited_articles': cited
        })
        
        keyword_details.append({
            'folder': folder,
            'keyword': keyword,
            'category': category,
            'expected': expected_count,
            'ranked': ranked,
            'cited': cited,
            'llm_files': llm_files
        })
        
        for llm_file in llm_files:
            if '豆包' in llm_file:
                llm_types['豆包'] += 1
            elif '千问' in llm_file:
                llm_types['千问'] += 1
            elif 'DeepSeek' in llm_file.lower():
                llm_types['DeepSeek'] += 1
            elif 'KIMI' in llm_file.upper():
                llm_types['KIMI'] += 1
            else:
                llm_types['其他'] += 1
        
        for company in ranked:
            all_companies[company['company']] += company['articles']
            total_ranked += company['articles']
        total_cited += cited
    
    # 生成Markdown报告
    lines = []
    lines.append("# LLM引用数据结构研究报告")
    lines.append("")
    lines.append(f"**生成时间**: 2026-02-03")
    lines.append(f"**数据目录**: `{DATA_DIR}`")
    lines.append("")
    
    # 问题1.1: 样本量分析
    lines.append("## 一、问题1.1: 样本量分析")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总关键词数 | {len(folders)} |")
    lines.append(f"| 关键词分类数 | {len(category_stats)} |")
    lines.append(f"| 上榜文章数 | {total_ranked} |")
    lines.append(f"| 仅被引用文章数 | {total_cited} |")
    lines.append(f"| **文章总计** | **{total_ranked + total_cited}** |")
    lines.append("")
    lines.append("### 结论")
    lines.append(f"- 交接文档说442篇，实际统计到 **{total_ranked + total_cited}篇**，可能部分子目录未被遍历")
    lines.append(f"- 样本量分布：上榜 {total_ranked}篇 ({total_ranked*100/(total_ranked+total_cited):.1f}%) vs 仅引用 {total_cited}篇 ({total_cited*100/(total_ranked+total_cited):.1f}%)")
    lines.append("")
    
    # 问题1.2: 上榜vs被引用定义
    lines.append("## 二、问题1.2: \"上榜\" vs \"被引用\" 定义")
    lines.append("")
    lines.append("### 数据结构揭示的定义")
    lines.append("")
    lines.append("基于文件夹命名规则：")
    lines.append("")
    lines.append("| 类型 | 特征 | 示例 |")
    lines.append("|------|------|------|")
    lines.append("| **上榜** | 子目录命名包含排名+公司名 | `第一名-泓动数据-六篇相关`、`排名第二-星集GEO` |")
    lines.append("| **仅被引用** | 子目录命名为\"被提及的公司\"或\"参考资料\" | `被提及的公司的相关资料-8篇` |")
    lines.append("")
    lines.append("### 核心差异")
    lines.append("")
    lines.append("1. **上榜** = LLM在回答中**明确列入排名榜单**的公司（如\"第一名是XX公司\"）")
    lines.append("2. **被引用** = LLM在回答中**引用了其内容作为信息来源**，但未列入排名")
    lines.append("")
    lines.append("> 💡 **关键洞察**：被引用不等于上榜！公司内容可能被LLM阅读和引用，但不一定被推荐进榜单。")
    lines.append("")
    
    # 问题1.3: 关键词分类
    lines.append("## 三、问题1.3: 60个关键词分类")
    lines.append("")
    lines.append("### 分类统计")
    lines.append("")
    lines.append("| 分类 | 数量 | 占比 |")
    lines.append("|------|------|------|")
    for category, items in sorted(category_stats.items(), key=lambda x: -len(x[1])):
        pct = len(items) * 100 / len(folders)
        lines.append(f"| {category} | {len(items)} | {pct:.1f}% |")
    lines.append("")
    
    lines.append("### 关键词类型详解")
    lines.append("")
    for category, items in sorted(category_stats.items(), key=lambda x: -len(x[1])):
        lines.append(f"#### {category} ({len(items)}个)")
        lines.append("")
        for item in items[:5]:  # 每类最多显示5个
            lines.append(f"- {item['keyword']}")
        if len(items) > 5:
            lines.append(f"- *(+{len(items)-5}个...)*")
        lines.append("")
    
    # LLM平台分布
    lines.append("## 四、LLM平台分布")
    lines.append("")
    lines.append("| LLM平台 | 文件夹数 | 占比 |")
    lines.append("|---------|----------|------|")
    for llm, count in sorted(llm_types.items(), key=lambda x: -x[1]):
        pct = count * 100 / len(folders)
        lines.append(f"| {llm} | {count} | {pct:.1f}% |")
    lines.append("")
    
    # 公司排名
    lines.append("## 五、上榜公司频次排名 (TOP15)")
    lines.append("")
    lines.append("| 排名 | 公司 | 上榜文章数 |")
    lines.append("|------|------|------------|")
    top_companies = sorted(all_companies.items(), key=lambda x: -x[1])[:15]
    for i, (company, count) in enumerate(top_companies, 1):
        lines.append(f"| {i} | {company} | {count} |")
    lines.append("")
    
    return "\n".join(lines)

if __name__ == "__main__":
    main()
    print("\n" + "=" * 60)
    report = generate_markdown_report()
    save_report(report, "LLM引用数据结构研究_问题1_报告.md")
