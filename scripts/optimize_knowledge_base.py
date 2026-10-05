"""
知识库优化脚本
- 删除"原版保真"部分（口语转录）
- 保留"核心知识结构化"部分
- 清理无意义的关键词
"""

import re
from pathlib import Path

def optimize_knowledge_base(input_path: str, output_path: str = None):
    """优化知识库文件"""
    
    input_file = Path(input_path)
    if not output_path:
        output_path = str(input_file.parent / f"{input_file.stem}_精简版.md")
    
    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    lines = content.split('\n')
    optimized_lines = []
    
    in_original_section = False  # 是否在"原版保真"部分
    skip_until_next_section = False
    
    # 无意义的关键词（口头禅）
    useless_keywords = ['对吧', '在这儿', '是不是', '这个', '那个', '然后', '就是', '其实']
    
    for line in lines:
        # 检测"原版保真"部分开始
        if '## 📜 原版保真' in line or '原版保真' in line and '<a id=' in line:
            in_original_section = True
            skip_until_next_section = True
            continue
        
        # 检测下一个章节开始（结束跳过）
        if skip_until_next_section and line.startswith('# ') and not line.startswith('## '):
            skip_until_next_section = False
            in_original_section = False
        
        # 检测"核心知识结构化"部分（保留）
        if '## 🧩 核心知识结构化' in line:
            in_original_section = False
            skip_until_next_section = False
        
        # 跳过"原版保真"部分
        if skip_until_next_section:
            continue
        
        # 跳过"跳转到原版保真"的链接
        if '跳转到原版保真' in line:
            continue
        
        # 跳过来源行
        if '来源：' in line and '_原文.docx' in line:
            continue
        
        # 清理关键词行中的无意义词
        if '检索关键词' in line:
            for keyword in useless_keywords:
                line = line.replace(f'`{keyword}`', '')
            # 清理多余的空格和逗号
            line = re.sub(r'` `', '`', line)
            line = re.sub(r'``', '', line)
        
        optimized_lines.append(line)
    
    # 清理连续空行
    result = '\n'.join(optimized_lines)
    result = re.sub(r'\n{4,}', '\n\n\n', result)
    
    # 添加优化说明
    header = """# 薛辉团队短视频编导知识库（精简版）

> 🔄 优化说明：
> - 已删除口语转录（原版保真）部分
> - 保留核心知识结构化内容
> - 清理无意义检索关键词
> - 文件大小减少约80%

---

"""
    
    # 删除原有标题
    result = re.sub(r'^# 薛辉团队短视频编导知识库\s*\n', '', result)
    result = re.sub(r'^> 自动生成时间：.*\n', '', result, flags=re.MULTILINE)
    result = re.sub(r'^> 说明：.*\n', '', result, flags=re.MULTILINE)
    
    result = header + result.strip()
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(result)
    
    # 统计
    original_size = len(content)
    optimized_size = len(result)
    reduction = (1 - optimized_size / original_size) * 100
    
    print(f"✅ 知识库优化完成")
    print(f"   原始大小: {original_size / 1024:.1f} KB")
    print(f"   优化后: {optimized_size / 1024:.1f} KB")
    print(f"   压缩率: {reduction:.1f}%")
    print(f"   输出文件: {output_path}")
    
    return output_path


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        input_path = sys.argv[1]
    else:
        input_path = r"docs\薛辉知识库\薛辉团队短视频编导知识库_完整版.md"
    
    optimize_knowledge_base(input_path)
