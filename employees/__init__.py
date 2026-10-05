"""
AI员工团队系统
基于AgentScope v1.0构建的智能员工协作平台
"""

from .base_employee import BaseEmployee
from .employee_registry import EmployeeRegistry

__all__ = [
    "BaseEmployee",
    "EmployeeRegistry",
]
