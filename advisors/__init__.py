"""
顾问团模块
提供可扮演特定人物的AI顾问，支持RAG知识库
"""

from .base_advisor import BaseAdvisor
from .advisor_registry import AdvisorRegistry, get_advisor, list_advisors

__all__ = [
    "BaseAdvisor",
    "AdvisorRegistry", 
    "get_advisor",
    "list_advisors",
]
