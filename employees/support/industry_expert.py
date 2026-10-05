"""
行业专家
负责行业洞察、知识检索
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class IndustryExpert(BaseEmployee):
    """
    🧠 行业专家
    
    职责：
    1. 提供行业洞察和趋势分析
    2. 检索专业知识和案例
    3. 解答行业相关问题
    
    技能：
    - industry_insight: 行业洞察
    - knowledge_retrieval: 知识检索
    - expert_consultation: 专家咨询
    """
    
    def __init__(self):
        super().__init__(
            employee_id="industry_expert",
            name="🧠 行业专家",
            department="support",
            sys_prompt="""# 角色定义
你是一位资深行业专家，具备丰富的行业知识和洞察能力。

# 核心职责
1. 分析行业趋势，提供前瞻性洞察
2. 检索专业知识，提供权威参考
3. 解答复杂的行业问题

# 擅长领域
- 数字营销与品牌推广
- 社交媒体运营
- 内容营销策略
- AI与数字化转型
- B2B与B2C营销

# 分析框架
- PEST分析：政策、经济、社会、技术
- 5W2H：What、Why、Who、When、Where、How、How much
- SWOT分析：优势、劣势、机会、威胁

# 回答原则
- 观点需有数据或案例支撑
- 引用权威来源
- 区分事实与观点
- 保持客观中立

# 输出格式要求
- 结构化回答
- 重要观点加粗
- 提供延伸阅读建议
- 标注信息时效性
""",
            model="qwen3.7-max",
            temperature=0.5,
            max_tokens=6000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["industry_insight", "knowledge_retrieval", "expert_consultation"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """执行行业专家技能"""
        results = {}
        
        if "趋势" in task or "洞察" in task or "insight" in task.lower():
            results["industry_insight"] = True
        
        if "知识" in task or "检索" in task or "retrieval" in task.lower():
            results["knowledge_retrieval"] = True
            
        if "咨询" in task or "问题" in task or "expert" in task.lower():
            results["expert_consultation"] = True
        
        return results
