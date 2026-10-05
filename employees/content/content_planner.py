"""
内容策划师
负责选题规划、标题生成、热点挖掘
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class ContentPlanner(BaseEmployee):
    """
    🎯 内容策划师
    
    职责：
    1. 根据品牌定位规划选题方向
    2. 生成吸引眼球的标题
    3. 挖掘行业热点话题
    
    技能：
    - topic_planning: 选题规划
    - title_generation: 标题生成
    - trend_mining: 热点挖掘
    """
    
    def __init__(self):
        super().__init__(
            employee_id="content_planner",
            name="🎯 内容策划师",
            department="content",
            sys_prompt="""# 角色定义
你是一位资深内容策划师，擅长为品牌策划有传播力的内容选题。

# 核心职责
1. 根据品牌行业和目标受众，规划系统化的选题矩阵
2. 为每个选题生成多个备选标题
3. 结合平台热点，挖掘内容机会

# 选题策划原则
- 用户痛点：解决目标用户的实际问题
- 品牌关联：与品牌业务紧密相关
- 平台适配：符合不同平台的内容调性
- 热度借势：结合当前热点话题

# 标题生成技巧
- 数字型：具体数字增加可信度
- 疑问型：引发好奇心
- 对比型：制造冲突感
- 利益型：明确用户收益

# 输出格式要求
- 选题需要分类分组
- 每个选题配3个备选标题
- 说明选题的预期效果
""",
            model="qwen3.7-max",
            temperature=0.7,
            max_tokens=4000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["topic_planning", "title_generation", "trend_mining"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """执行内容策划技能"""
        results = {}
        
        task_lower = task.lower()
        
        if "选题" in task or "规划" in task or "topic" in task_lower:
            results["topic_planning"] = True
        
        if "标题" in task or "title" in task_lower:
            results["title_generation"] = True
            
        if "热点" in task or "热搜" in task or "trend" in task_lower:
            results["trend_mining"] = True
        
        return results
