"""
同步员工配置脚本
将实际的员工类配置（包括系统提示词）同步到数据库
"""

import os
import sys
import asyncio
import io

# 设置UTF-8输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from employees.employee_registry import get_employee
from db.diagnosis_db import save_employee_config, init_db, init_default_departments


async def sync_all_employees():
    """同步所有员工配置到数据库"""
    
    # 确保数据库和部门表已初始化
    init_db()
    init_default_departments()
    
    # 所有员工ID列表
    employee_ids = [
        # 领导层
        "project_director",
        # 诊断部
        "data_collector",
        "ai_tester",
        "competitor_analyst",
        "report_writer",
        # 内容部
        "content_planner",
        "content_writer",
        "douyin_creator",
        "xhs_creator",
        # 支持部
        "ppt_specialist",
        "chief_editor",
        "industry_expert",
    ]
    
    synced_count = 0
    failed_count = 0
    
    print("=" * 60)
    print("开始同步员工配置到数据库...")
    print("=" * 60)
    
    for emp_id in employee_ids:
        try:
            # 从注册中心获取员工实例
            employee = get_employee(emp_id)
            
            if not employee:
                print(f"⚠️  {emp_id}: 员工不存在，跳过")
                failed_count += 1
                continue
            
            # 提取配置
            profile = employee.get_profile()
            
            # 获取头像（从name中提取emoji）
            avatar = "🤖"
            if employee.name:
                # 提取第一个emoji字符
                import re
                emoji_match = re.search(r'[\U0001F300-\U0001F9FF]', employee.name)
                if emoji_match:
                    avatar = emoji_match.group(0)
            
            # 保存到数据库
            success = save_employee_config(
                employee_id=employee.employee_id,
                name=employee.name,
                department_id=employee.department,
                avatar=avatar,
                description=profile.get("description", ""),
                model_id=employee.model,
                temperature=employee.temperature,
                max_tokens=employee.max_tokens,
                system_prompt=employee.sys_prompt,  # 🔑 关键：同步系统提示词
                skills=profile.get("skills", []),
                mcp_config={},  # 暂时为空
                memory_type=employee.memory_type,
                usage_scope="all",
                is_active=True,
                sort_order=employee_ids.index(emp_id),
            )
            
            if success:
                print(f"✅ {employee.name} ({emp_id})")
                print(f"   模型: {employee.model}")
                print(f"   提示词长度: {len(employee.sys_prompt)} 字符")
                synced_count += 1
            else:
                print(f"❌ {emp_id}: 保存失败")
                failed_count += 1
                
        except Exception as e:
            print(f"❌ {emp_id}: {str(e)}")
            failed_count += 1
    
    print()
    print("=" * 60)
    print(f"同步完成: ✅ {synced_count} 个成功, ❌ {failed_count} 个失败")
    print("=" * 60)
    print()
    print("现在可以在前端「员工设置」页面查看和编辑完整的员工配置了！")


if __name__ == "__main__":
    asyncio.run(sync_all_employees())
