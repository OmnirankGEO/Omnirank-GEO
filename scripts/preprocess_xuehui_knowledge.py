"""
薛辉知识库预处理脚本

原版保真 + 核心知识点结构化清洗（带溯源 + 检索关键词）
完成原版与清洗版双向关联

只处理与视频拆解/用户拆解/评论区拆解相关的知识点
"""

import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

# 源文件路径
KNOWLEDGE_DATASET = Path("c:/AI-Test/AgentsCope-07/New folder/knowledge_dataset.jsonl")
STRUCTURED_KB = Path("c:/AI-Test/AgentsCope-07/New folder/final_structured_knowledge_base.jsonl")

# 输出目录 - 加入现有薛辉顾问知识库
OUTPUT_DIR = Path("c:/AI-Test/AgentsCope-07/geo_agentscope/data/advisors/xuehui-copywriting/knowledge")

# 与拆解相关的关键词过滤
BREAKDOWN_KEYWORDS = [
    '拆片', '拆解', '结构', '脚本', '观点', '过程', '故事', '知识', 
    '选题', '勾子', '开篇', '钩子', '火车节', '情绪', '完播率',
    '分析', '解构', '模型'
]

# 核心拆解相关的来源文件（精选）
CORE_BREAKDOWN_FILES = [
    # 核心拆解方法论
    '16.让爆款在你面前裸奔的拆片技巧',
    '17-1 短视频爆款拆片技巧-观点型',
    '17-2 过程型脚本-爆款拆解',
    '18-1 短视频爆款拆片技巧-故事型',
    '18-2 短视频爆款拆片技巧-知识型',
    # 脚本结构
    '10.划重点！离变现最近的脚本「晒过程」',
    '11.划重点！离变现最近的脚本「晒过程」',
    '9-1 过程型视频的结构搭建',
    '9-2 过程展示型脚本-测评产品型脚本',
    '9-3 任务挑战型脚本-事件体验型脚本',
    '9-4 晒过程六大勾子玩法',
    # 观点脚本
    '8.抖音上最吸"真"粉丝的「聊观点」脚本',
    '10-1 大流量观点选题技巧',
    '10-2 精准流量的观点选题技巧',
    '10-3 观点脚本六大论据',
    # 故事脚本
    '14.最能成交高客单高信任脚本「讲故事」',
    '15.最能成交高客单高信任脚本「讲故事」',
    '11-1 小有成就-成功案例',
    '11-2 平凡英雄型脚本-苦难经历型脚本',
    # 知识脚本
    '12.最能凸显自己专业的脚本「教知识」',
    '13.最能凸显自己专业的脚本「教知识」',
    '8-1 知识选题的五个维度',
    '8-2 解题型脚本-案例型脚本',
    '8-3 推荐型脚本-揭秘型脚本',
    # 选题技巧
    '5.正确使用八大爆款元素的选题技巧',
    '12-1 爆款选题方法论',
]


def is_breakdown_related(item: Dict, source_field: str = 'source_file') -> bool:
    """检查知识点是否与拆解相关"""
    source = item.get(source_field, '')
    keywords = item.get('keywords', '')
    title = item.get('title', item.get('file_name', ''))
    content = item.get('content', '')
    
    # 核心文件优先匹配
    for core_file in CORE_BREAKDOWN_FILES:
        if core_file in source or core_file in title:
            return True
    
    # 关键词匹配
    check_text = f"{source} {keywords} {title} {content[:200]}"
    for kw in BREAKDOWN_KEYWORDS:
        if kw in check_text:
            return True
    
    return False


def process_structured_knowledge() -> List[Dict]:
    """处理结构化知识库 (final_structured_knowledge_base.jsonl)"""
    if not STRUCTURED_KB.exists():
        print(f"⚠️ 文件不存在: {STRUCTURED_KB}")
        return []
    
    items = []
    with open(STRUCTURED_KB, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                item = json.loads(line)
                if is_breakdown_related(item):
                    items.append(item)
            except json.JSONDecodeError:
                continue
    
    print(f"📚 structured KB: 筛选出 {len(items)} 条拆解相关知识点")
    return items


def process_raw_knowledge() -> List[Dict]:
    """处理原版知识库 (knowledge_dataset.jsonl)"""
    if not KNOWLEDGE_DATASET.exists():
        print(f"⚠️ 文件不存在: {KNOWLEDGE_DATASET}")
        return []
    
    items = []
    with open(KNOWLEDGE_DATASET, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                item = json.loads(line)
                if is_breakdown_related(item, source_field='file_name'):
                    items.append(item)
            except json.JSONDecodeError:
                continue
    
    print(f"📖 raw KB: 筛选出 {len(items)} 条拆解相关知识块")
    return items


def generate_structured_md(item: Dict, index: int) -> str:
    """生成结构化清洗版 Markdown"""
    title = item.get('title', f'知识点_{index}')
    knowledge_point = item.get('knowledge_point', '')
    core_conclusion = item.get('core_conclusion', '')
    logic = item.get('logic', '')
    case_study = item.get('case_study', '')
    notes = item.get('notes', '')
    keywords = item.get('keywords', '')
    source_file = item.get('source_file', '')
    source_paragraphs = item.get('source_paragraphs', '')
    
    md = f"""# {title}

> **来源**: {source_file}  
> **段落**: {source_paragraphs}  
> **检索关键词**: {keywords}

## 知识点
{knowledge_point}

## 核心结论
{core_conclusion}

## 逻辑链
{logic}

## 案例
{case_study}

## 备注
{notes}

---
*溯源: 原版文件 `{source_file}` 段落 {source_paragraphs}*
"""
    return md


def generate_raw_md(items_by_file: Dict[str, List[Dict]]) -> Dict[str, str]:
    """生成原版保真 Markdown（按文件聚合）"""
    results = {}
    
    for file_name, chunks in items_by_file.items():
        # 按 chunk_id 排序
        sorted_chunks = sorted(chunks, key=lambda x: x.get('chunk_id', 0))
        
        content_parts = [f"# {file_name}\n\n> 原版课程内容保真记录\n"]
        for chunk in sorted_chunks:
            chunk_id = chunk.get('chunk_id', 0)
            content = chunk.get('content', '')
            content_parts.append(f"\n## 段落 {chunk_id}\n\n{content}\n")
        
        results[file_name] = '\n'.join(content_parts)
    
    return results


def create_index_file(structured_items: List[Dict], raw_files: List[str]) -> str:
    """创建知识库索引文件"""
    index = {
        "created_at": datetime.now().isoformat(),
        "version": "1.0",
        "description": "薛辉老师课程知识库 - 视频拆解专题",
        "structured_count": len(structured_items),
        "raw_file_count": len(raw_files),
        "categories": {
            "拆片方法论": [],
            "脚本结构": [],
            "选题技巧": [],
            "钩子玩法": []
        },
        "search_keywords": list(set(
            kw.strip() 
            for item in structured_items 
            for kw in item.get('keywords', '').split(',')
            if kw.strip()
        ))
    }
    
    # 分类索引
    for item in structured_items:
        source = item.get('source_file', '')
        title = item.get('title', '')
        
        if '拆片' in source or '拆解' in source:
            index['categories']['拆片方法论'].append(title)
        elif '脚本' in source or '结构' in source:
            index['categories']['脚本结构'].append(title)
        elif '选题' in source:
            index['categories']['选题技巧'].append(title)
        elif '勾子' in source or '钩子' in source or '开篇' in source:
            index['categories']['钩子玩法'].append(title)
    
    return json.dumps(index, ensure_ascii=False, indent=2)


def main():
    print("=" * 60)
    print("薛辉知识库预处理 - 视频拆解专题")
    print("=" * 60)
    print()
    
    # 1. 处理结构化知识库
    structured_items = process_structured_knowledge()
    
    # 2. 处理原版知识库
    raw_items = process_raw_knowledge()
    
    # 3. 按文件聚合原版内容
    raw_by_file = {}
    for item in raw_items:
        file_name = item.get('file_name', 'unknown')
        if file_name not in raw_by_file:
            raw_by_file[file_name] = []
        raw_by_file[file_name].append(item)
    
    # 4. 创建输出目录
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    breakdown_dir = OUTPUT_DIR / "breakdown"
    breakdown_dir.mkdir(exist_ok=True)
    
    # 5. 输出结构化清洗版
    print(f"\n📝 生成结构化知识点...")
    for i, item in enumerate(structured_items):
        md_content = generate_structured_md(item, i)
        
        # 生成安全文件名
        title = item.get('title', f'kp_{i}')
        safe_name = title.replace('/', '_').replace('\\', '_').replace(' ', '_')[:60]
        safe_name = ''.join(c for c in safe_name if c.isalnum() or c in '_-')
        
        output_file = breakdown_dir / f"{i:04d}_{safe_name}.md"
        output_file.write_text(md_content, encoding='utf-8')
    
    print(f"   ✅ 生成 {len(structured_items)} 个结构化知识点文件")
    
    # 6. 输出原版保真版
    print(f"\n📖 生成原版保真文档...")
    raw_mds = generate_raw_md(raw_by_file)
    
    raw_dir = breakdown_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    
    for file_name, content in raw_mds.items():
        safe_name = file_name.replace('/', '_').replace('\\', '_').replace('.txt', '')[:60]
        safe_name = ''.join(c for c in safe_name if c.isalnum() or c in '_-')
        
        output_file = raw_dir / f"{safe_name}.md"
        output_file.write_text(content, encoding='utf-8')
    
    print(f"   ✅ 生成 {len(raw_mds)} 个原版保真文档")
    
    # 7. 生成索引文件
    print(f"\n📋 生成知识库索引...")
    index_content = create_index_file(structured_items, list(raw_mds.keys()))
    index_file = breakdown_dir / "_index.json"
    index_file.write_text(index_content, encoding='utf-8')
    print(f"   ✅ 索引文件: {index_file}")
    
    # 8. 输出统计
    print()
    print("=" * 60)
    print("处理完成!")
    print("=" * 60)
    print(f"  输出目录: {breakdown_dir}")
    print(f"  结构化知识点: {len(structured_items)} 条")
    print(f"  原版保真文档: {len(raw_mds)} 个")
    print()
    print("下一步: 运行向量化")
    print("  python -m advisors.vectorizer --advisor_id xuehui-copywriting")


if __name__ == "__main__":
    main()
