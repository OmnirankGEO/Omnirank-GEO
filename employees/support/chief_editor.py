"""
首席编辑
负责质量审核、语言统一、合规检查
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class ChiefEditor(BaseEmployee):
    """
    ✏️ 首席编辑
    
    职责：
    1. 审核内容质量，把控专业性
    2. 统一语言风格，保持品牌调性
    3. 检查合规性，避免敏感内容
    
    技能：
    - quality_review: 质量审核
    - style_unification: 风格统一
    - compliance_check: 合规检查
    """
    
    def __init__(self):
        super().__init__(
            employee_id="chief_editor",
            name="✏️ 首席编辑",
            department="support",
            sys_prompt="""# 角色定义
你是一位资深首席编辑，负责把控所有输出内容的质量和合规性。

# 核心职责
1. 审核内容的专业性、准确性、可读性
2. 统一语言风格，确保品牌调性一致
3. 检查是否存在敏感或违规内容

# 质量审核标准
- 事实准确：数据、案例需可核实
- 逻辑清晰：论点论据完整
- 表达流畅：无病句、错别字
- 结构合理：层次分明

# 风格统一要求
- 人称一致
- 语气一致
- 用词规范
- 格式统一

# 合规检查要点
- 无虚假宣传
- 无绝对化用语
- 无敏感政治内容
- 无侵权风险
- 数据引用需标注来源

# 输出格式要求
- 明确指出问题位置
- 给出修改建议
- 评估整体质量分数
- 标注是否可发布
""",
            model="deepseek-reasoner",
            temperature=0.3,
            max_tokens=4000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["quality_review", "style_unification", "compliance_check"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """执行编辑审核技能"""
        results = {}
        
        if "审核" in task or "检查" in task or "review" in task.lower():
            results["quality_review"] = True
        
        if "风格" in task or "统一" in task or "style" in task.lower():
            results["style_unification"] = True
            
        if "合规" in task or "敏感" in task or "compliance" in task.lower():
            results["compliance_check"] = True
        
        return results
