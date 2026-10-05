"""
动态员工类
支持从数据库配置动态创建员工实例
"""

from .base_employee import BaseEmployee
from typing import List


class DynamicEmployee(BaseEmployee):
    """
    动态创建的员工
    
    从数据库配置创建，无需预定义类
    """
    
    def __init__(
        self,
        employee_id: str,
        name: str,
        department: str,
        sys_prompt: str,
        model: str = "deepseek-v4-flash",
        temperature: float = 0.7,
        max_tokens: int = 15000,
        memory_type: str = "temporary",
        enable_search: bool = True,
        skill_list: List[str] = None,
    ):
        super().__init__(
            employee_id=employee_id,
            name=name,
            department=department,
            sys_prompt=sys_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            memory_type=memory_type,
            enable_search=enable_search,
        )
        self._skill_list = skill_list or []
    
    @property
    def skills(self) -> List[str]:
        """返回员工技能列表"""
        return self._skill_list
