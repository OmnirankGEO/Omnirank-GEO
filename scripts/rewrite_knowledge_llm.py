"""
用LLM重写知识库的"重点提炼"部分
- 将口语化的课程转录重写为结构化的知识点
- 保持原有关键词不变
"""

import re
import os
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

REWRITE_PROMPT = """你是一个短视频编导知识整理专家。请将以下口语化的课程内容重写为**结构化的知识点**。

**章节标题**：{title}

**原始内容**：
{content}

**重写要求**：
1. 提取核心知识点，删除口语废话（"在这儿"、"这个地儿"、"对吧"等）
2. 如果内容提到软件操作，明确标注是哪个软件（如剪映）的哪个功能
3. 使用清晰的结构：
   - 用 `**加粗**` 标注核心概念
   - 用简洁的列表形式呈现
   - 每个知识点一行，不要写长段落
4. 如果原内容信息不足或无法理解，只保留能确定的知识点
5. 最多输出5-8个核心知识点

**输出格式示例**：
- **剪映界面布局**：分为预览区、时间轴、工具栏三大区域
- **导出设置**：位于右上角，用于设置视频分辨率和格式
- **时间轴操作**：拖拽素材到时间轴进行剪辑，支持分割和删除

请直接输出重写后的知识点列表（不要输出任何解释）："""


def get_llm_client():
    """获取LLM客户端"""
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    
    if not api_key:
        raise ValueError("未配置API密钥")
    
    return OpenAI(api_key=api_key, base_url=base_url)


def rewrite_content(client: OpenAI, title: str, content: str) -> str:
    """用LLM重写内容"""
    if not content.strip() or len(content.strip()) < 20:
        return ""
    
    prompt = REWRITE_PROMPT.format(title=title, content=content[:3000])
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"  ⚠️ LLM调用失败: {e}")
        return content  # 失败时保留原内容


def process_knowledge_base(input_path: str, output_path: str = None):
    """处理知识库"""
    
    input_file = Path(input_path)
    if not output_path:
        output_path = str(input_file.parent / f"{input_file.stem.replace('_优化版', '')}_深度优化版.md")
    
    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 初始化LLM
    client = get_llm_client()
    print("✅ LLM客户端初始化成功")
    
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
        
        # 查找"重点提炼"部分
        new_lines = []
        in_key_points = False
        key_points_content = []
        
        for i, line in enumerate(lines):
            if '### 💡 重点提炼' in line:
                in_key_points = True
                new_lines.append(line)
                continue
            
            # 遇到下一个标题或章节结束
            if in_key_points and (line.startswith('## ') or line.startswith('# ') or line.startswith('---')):
                in_key_points = False
                
                # 重写收集到的内容
                if key_points_content:
                    original_content = '\n'.join(key_points_content)
                    rewritten = rewrite_content(client, title, original_content)
                    if rewritten:
                        new_lines.append(rewritten)
                    new_lines.append('')
                
                new_lines.append(line)
                continue
            
            if in_key_points:
                key_points_content.append(line)
            else:
                new_lines.append(line)
        
        # 处理最后一个章节的重点提炼
        if in_key_points and key_points_content:
            original_content = '\n'.join(key_points_content)
            rewritten = rewrite_content(client, title, original_content)
            if rewritten:
                new_lines.append(rewritten)
        
        processed_sections.append('\n'.join(new_lines))
    
    # 组合并保存
    result = '\n'.join(processed_sections)
    
    # 更新头部说明
    result = result.replace(
        "> - 已用AI重新提取专业关键词",
        "> - 已用AI重新提取专业关键词\n> - 已用AI将口语内容重写为结构化知识点"
    )
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(result)
    
    print(f"\n✅ 知识库深度优化完成")
    print(f"   输出文件: {output_path}")
    
    return output_path


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
    else:
        input_path = r"docs\薛辉知识库\薛辉团队短视频编导知识库_完整版_优化版.md"
    
    process_knowledge_base(input_path)
