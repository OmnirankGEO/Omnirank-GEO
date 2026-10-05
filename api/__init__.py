"""API 模块。

保持历史 `from api import api_list_employees` 兼容, 但避免 package import 时
立刻加载 employee_api → diagnosis_db.init_db(), 让独立 API 单测不会误连数据库。
"""

from importlib import import_module

__all__ = [
    "api_list_employees",
    "api_get_employee",
    "api_execute_task",
    "api_get_task_history",
    "api_get_task_detail",
    "get_employees_for_display",
    "get_department_info",
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(name)
    employee_api = import_module(".employee_api", __name__)
    value = getattr(employee_api, name)
    globals()[name] = value
    return value
