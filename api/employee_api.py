"""
员工API接口
提供员工列表、任务执行、任务历史等功能
"""

import os
import sys
from typing import Optional
from datetime import datetime

# 路径设置
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.diagnosis_db import (
    save_employee_task,
    update_employee_task,
    get_employee_tasks,
    get_employee_task,
    get_employee_configs,
    get_departments,
    init_default_departments,
    init_default_employees,
    init_db,
)
from employees.employee_registry import EmployeeRegistry, get_employee, list_employees


# ============================================
# 员工相关API
# ============================================

async def api_list_employees() -> dict:
    """
    获取所有员工列表（从数据库读取配置）
    
    Returns:
        {
            "success": True,
            "employees": [...],
            "departments": [...]
        }
    """
    # 确保数据库初始化
    init_db()
    init_default_departments()
    init_default_employees()
    
    # 从数据库读取配置
    employees = get_employee_configs()
    departments = get_departments()
    
    # 转换为前端需要的格式
    formatted_employees = []
    for emp in employees:
        formatted_employees.append({
            "id": emp["id"],
            "name": emp["name"],  # 名字已经包含emoji，不需要再加
            "department": emp["department_id"],
            "department_name": emp.get("department_name", ""),
            "description": emp.get("description", ""),
            "skills": emp.get("skills", []),
            "model": emp.get("model_id", "deepseek-v4-flash"),  # 2026-05-22 V3.2→V4 全切
            "avatar": emp.get("avatar", "🤖"),
            "is_active": emp.get("is_active", True),
        })
    
    formatted_departments = []
    for dept in departments:
        formatted_departments.append({
            "id": dept["id"],
            "name": f"{dept.get('icon', '📁')} {dept['name']}",
            "description": dept.get("description", ""),
            "icon": dept.get("icon", "📁"),
        })
    
    return {
        "success": True,
        "employees": formatted_employees,
        "departments": formatted_departments,
        "count": len(formatted_employees),
    }


async def api_get_employee(employee_id: str) -> dict:
    """
    获取单个员工详情
    
    Args:
        employee_id: 员工ID
    
    Returns:
        {
            "success": True,
            "employee": {...}
        }
    """
    employee = get_employee(employee_id)
    
    if not employee:
        return {
            "success": False,
            "error": f"员工不存在: {employee_id}",
        }
    
    return {
        "success": True,
        "employee": employee.get_profile(),
    }


async def api_execute_task(
    employee_id: str,
    task: str,
    context: Optional[dict] = None,
) -> dict:
    """
    给员工分配任务并执行
    
    Args:
        employee_id: 员工ID
        task: 任务描述
        context: 可选的背景信息
    
    Returns:
        {
            "success": True,
            "task_id": int,
            "result": {...}
        }
    """
    # 获取员工
    employee = get_employee(employee_id)
    if not employee:
        return {
            "success": False,
            "error": f"员工不存在: {employee_id}",
        }
    
    # 保存任务记录
    task_id = save_employee_task(
        employee_id=employee_id,
        task_content=task,
        employee_name=employee.name,
        task_type="single",
        context=context,
        status="running",
    )
    
    try:
        # 执行任务
        result = await employee.execute_task(task, context)
        
        # 更新任务状态
        update_employee_task(
            task_id=task_id,
            status=result.get("status", "completed"),
            result=result.get("result", ""),
            skills_used=result.get("skills_used", []),
            execution_time=result.get("execution_time", 0),
            error_message=result.get("error"),
        )
        
        return {
            "success": True,
            "task_id": task_id,
            "result": result,
        }
        
    except Exception as e:
        # 记录错误
        update_employee_task(
            task_id=task_id,
            status="failed",
            error_message=str(e),
        )
        
        return {
            "success": False,
            "task_id": task_id,
            "error": str(e),
        }


async def api_get_task_history(
    employee_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """
    获取任务历史
    
    Args:
        employee_id: 可选，按员工筛选
        status: 可选，按状态筛选
        limit: 返回数量限制
    
    Returns:
        {
            "success": True,
            "tasks": [...]
        }
    """
    tasks = get_employee_tasks(
        employee_id=employee_id,
        status=status,
        limit=limit,
    )
    
    return {
        "success": True,
        "tasks": tasks,
    }


async def api_get_task_detail(task_id: int) -> dict:
    """
    获取任务详情
    
    Args:
        task_id: 任务ID
    
    Returns:
        {
            "success": True,
            "task": {...}
        }
    """
    task = get_employee_task(task_id)
    
    if not task:
        return {
            "success": False,
            "error": f"任务不存在: {task_id}",
        }
    
    return {
        "success": True,
        "task": task,
    }


# ============================================
# Streamlit集成辅助函数
# ============================================

def get_employees_for_display() -> list[dict]:
    """获取用于前端显示的员工列表"""
    registry = EmployeeRegistry.get_instance()
    employees = registry.list_all()
    
    # 按部门分组
    departments = {}
    for emp in employees:
        dept = emp.get("department", "other")
        if dept not in departments:
            departments[dept] = []
        departments[dept].append(emp)
    
    return {
        "by_department": departments,
        "all": employees,
    }


def get_department_info() -> list[dict]:
    """获取部门信息"""
    return [
        {
            "id": "diagnosis",
            "name": "📊 诊断部",
            "description": "负责品牌GEO诊断分析",
            "color": "#4CAF50",
        },
        {
            "id": "content",
            "name": "✍️ 内容部",
            "description": "负责内容创作和选题策划",
            "color": "#2196F3",
        },
        {
            "id": "support",
            "name": "📑 支持部",
            "description": "负责报告交付和质量审核",
            "color": "#FF9800",
        },
    ]
