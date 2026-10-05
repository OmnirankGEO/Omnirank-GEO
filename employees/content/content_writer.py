"""
正文撰稿人
负责长文撰写、AI改写、去AI味
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class ContentWriter(BaseEmployee):
    """
    ✍️ 正文撰稿人
    
    职责：
    1. 撰写高质量长文内容
    2. 改写和润色已有内容
    3. 优化AI生成内容，去除机器感
    
    技能：
    - article_writing: 长文撰写
    - content_rewriting: 内容改写
    - deai_optimization: 去AI味优化
    """
    
    def __init__(self):
        super().__init__(
            employee_id="content_writer",
            name="✍️ 正文撰稿人",
            department="content",
            sys_prompt="""# 角色定义
你是一位专业的内容撰稿人，擅长撰写有深度、有温度的品牌内容。

# 核心职责
1. 根据选题和大纲，撰写完整的长文内容
2. 对已有内容进行改写和润色
3. 优化AI生成的内容，使其更自然、更有人情味

# 写作风格要求
- 语言流畅自然，避免生硬表达
- 逻辑清晰，段落过渡顺畅
- 适当使用案例和故事
- 保持品牌调性一致

# 去AI味技巧
- 增加个人观点和态度
- 使用口语化表达
- 添加具体细节和场景
- 避免过于规整的结构
- 增加情感和温度

# 输出格式要求
- 使用Markdown格式
- 合理使用小标题
- 段落长度适中
- 重点内容突出显示
""",
            model="deepseek-v4-pro",
            temperature=0.7,
            max_tokens=8000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["article_writing", "content_rewriting", "deai_optimization"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """执行撰写技能"""
        results = {}
        
        if "撰写" in task or "写" in task or "write" in task.lower():
            results["article_writing"] = True
        
        if "改写" in task or "润色" in task or "rewrite" in task.lower():
            results["content_rewriting"] = True
            
        if "去AI" in task or "优化" in task or "deai" in task.lower():
            results["deai_optimization"] = True
        
        return results
