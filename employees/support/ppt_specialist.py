"""
PPT专员
负责PPT生成、模板设计
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class PPTSpecialist(BaseEmployee):
    """
    📊 PPT专员
    
    职责：
    1. 生成演示文稿内容
    2. 设计PPT模板和布局
    3. 优化PPT视觉效果
    
    技能：
    - ppt_generation: PPT生成
    - template_design: 模板设计
    - visual_optimization: 视觉优化
    """
    
    def __init__(self):
        super().__init__(
            employee_id="ppt_specialist",
            name="📊 PPT专员",
            department="support",
            sys_prompt="""# 角色定义
你是一位专业的PPT设计专员，擅长将内容转化为精美的演示文稿。

# 核心职责
1. 根据报告内容，生成PPT结构和文案
2. 设计专业、美观的PPT模板
3. 优化图表和视觉呈现

# PPT设计原则
- 一页一主题，信息聚焦
- 文字精简，图表优先
- 色彩统一，风格一致
- 层次清晰，重点突出

# 页面类型
- 封面页：标题、副标题、日期
- 目录页：章节概览
- 内容页：正文、图表、数据
- 过渡页：章节分隔
- 结束页：总结、联系方式

# 输出格式要求
- 按页面顺序输出
- 每页包含标题、内容、配图建议
- 提供配色方案建议
- 标注动画效果（如需要）
""",
            model="deepseek-v4-pro",
            temperature=0.5,
            max_tokens=6000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["ppt_generation", "template_design", "visual_optimization"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """执行PPT制作技能"""
        results = {}
        
        if "PPT" in task.upper() or "演示" in task or "幻灯片" in task:
            results["ppt_generation"] = True
        
        if "模板" in task or "template" in task.lower():
            results["template_design"] = True
            
        if "优化" in task or "美化" in task or "设计" in task:
            results["visual_optimization"] = True
        
        return results
