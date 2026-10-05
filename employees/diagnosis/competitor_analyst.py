"""
竞品分析师
负责竞品识别、差距对比、爆款拆解
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class CompetitorAnalyst(BaseEmployee):
    """
    📈 竞品分析师
    
    职责：
    1. 识别品牌的核心竞争对手
    2. 分析竞品与品牌的差距
    3. 拆解竞品爆款内容策略
    
    技能：
    - competitor_identification: 竞品识别
    - gap_analysis: 差距分析
    - viral_content_analysis: 爆款拆解
    """
    
    def __init__(self):
        super().__init__(
            employee_id="competitor_analyst",
            name="📈 竞品分析师",
            department="diagnosis",
            sys_prompt="""# 角色定义
你是一位专业的竞品分析师，负责为品牌提供竞争情报分析。

# 核心职责
1. 根据品牌行业和定位，识别核心竞争对手
2. 对比分析竞品与品牌在各维度的差距
3. 拆解竞品的爆款内容，提取可借鉴策略

# 分析维度
- 内容策略：选题方向、发布频率、内容形式
- 平台表现：粉丝量、互动率、增长趋势
- AI可见度：在AI引擎中的推荐频率
- 品牌定位：价值主张、目标客群、差异化

# 输出格式要求
- 使用Markdown格式
- 提供数据支撑
- 给出可执行的差异化建议

# 注意事项
- 客观分析，不贬低竞品
- 重点关注可借鉴的策略
- 结合品牌实际情况给建议
""",
            model="deepseek-v4-flash",
            temperature=0.5,
            max_tokens=4000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["competitor_identification", "gap_analysis", "viral_content_analysis"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """
        执行竞品分析技能
        """
        results = {}
        
        # 根据任务内容判断需要执行的技能
        task_lower = task.lower()
        
        if "竞品" in task or "竞争对手" in task or "competitor" in task_lower:
            results["competitor_identification"] = True
        
        if "差距" in task or "对比" in task or "gap" in task_lower:
            results["gap_analysis"] = True
            
        if "爆款" in task or "热门" in task or "viral" in task_lower:
            results["viral_content_analysis"] = True
        
        return results
