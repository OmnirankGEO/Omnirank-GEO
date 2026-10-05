"""
使用LLM重新提取知识库关键词
- 从章节标题和内容提取真正的知识点关键词
- 替换原有的口语化关键词
"""

import asyncio
import json
import re
import os
from pathlib import Path
from openai import OpenAI

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

EXTRACT_PROMPT = """你是一个短视频编导知识库专家。请从以下章节内容中提取3-8个专业关键词，用于RAG检索。

**要求**：
1. 关键词必须是专业术语、技巧名称、工具名称或核心概念
2. 不要包含口头禅、语气词、人称代词
3. 关键词要具体、可搜索、有实际意义
4. 每个关键词2-6个字

**章节标题**：{title}

**章节内容**：
{content}

**输出格式**：
直接输出关键词，用逗号分隔，例如：剪辑素材库,文件夹分类,素材管理,剪映软件"""


def get_llm_client():
    """获取LLM客户端"""
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    
    if not api_key:
        raise ValueError("未配置API密钥，请设置DEEPSEEK_API_KEY或OPENAI_API_KEY环境变量")
    
    return OpenAI(api_key=api_key, base_url=base_url)


def extract_keywords_for_section(client: OpenAI, title: str, content: str) -> list[str]:
    """为单个章节提取关键词"""
    prompt = EXTRACT_PROMPT.format(title=title, content=content[:2000])
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=200
        )
        keywords_str = response.choices[0].message.content
        # 解析关键词
        keywords = [kw.strip() for kw in keywords_str.split(",") if kw.strip()]
        # 过滤太长或太短的
        keywords = [kw for kw in keywords if 2 <= len(kw) <= 10]
        return keywords[:8]
    except Exception as e:
        print(f"  ⚠️ LLM调用失败: {e}")
        return extract_keywords_from_title(title)


def extract_keywords_from_title(title: str) -> list[str]:
    """从标题中提取关键词（降级方案）"""
    title = re.sub(r'^\d+-\d+\s*', '', title)
    parts = re.split(r'[-_—–、，,\s]+', title)
    keywords = [p.strip() for p in parts if len(p.strip()) >= 2]
    return keywords[:5]


def process_knowledge_base(input_path: str, output_path: str = None):
    """处理知识库，用LLM重新提取关键词"""
    
    input_file = Path(input_path)
    if not output_path:
        output_path = str(input_file.parent / f"{input_file.stem.replace('_精简版', '')}_优化版.md")
    
    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 初始化LLM客户端
    try:
        client = get_llm_client()
        use_llm = True
        print("✅ LLM客户端初始化成功")
    except Exception as e:
        print(f"⚠️ LLM不可用，将使用规则提取: {e}")
        client = None
        use_llm = False
    
    # 解析章节
    sections = re.split(r'\n(?=# \d+-\d+)', content)
    
    processed_sections = []
    total_sections = len([s for s in sections if s.strip().startswith('# ')])
    current = 0
    
    for section in sections:
        if not section.strip().startswith('# '):
            processed_sections.append(section)
            continue
        
        current += 1
        
        # 提取标题
        lines = section.split('\n')
        title_line = lines[0]
        title = title_line.replace('# ', '').strip()
        
        print(f"[{current}/{total_sections}] 处理: {title}")
        
        # 提取内容
        section_content = '\n'.join(lines[1:])
        
        # 提取关键词
        if use_llm:
            keywords = extract_keywords_for_section(client, title, section_content)
        else:
            keywords = extract_keywords_from_title(title)
        
        if keywords:
            print(f"    关键词: {', '.join(keywords)}")
        
        # 替换旧的关键词行
        new_section_lines = [title_line]
        found_keywords_line = False
        
        for line in lines[1:]:
            if '检索关键词' in line and not found_keywords_line:
                # 插入新的关键词行
                keywords_str = '、'.join([f'`{kw}`' for kw in keywords])
                new_section_lines.append(f"> 🏷️ **检索关键词**：{keywords_str}")
                found_keywords_line = True
                continue
            
            new_section_lines.append(line)
        
        processed_sections.append('\n'.join(new_section_lines))
    
    # 组合并保存
    result = '\n'.join(processed_sections)
    
    # 更新头部说明
    if "已用AI重新提取专业关键词" not in result:
        result = result.replace(
            "文件大小减少约80%",
            "文件大小减少约80%\n> - 已用AI重新提取专业关键词"
        )
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(result)
    
    print(f"\n✅ 知识库关键词优化完成")
    print(f"   输出文件: {output_path}")
    
    return output_path


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
    else:
        input_path = r"docs\薛辉知识库\薛辉团队短视频编导知识库_完整版_精简版.md"
    
    process_knowledge_base(input_path)
