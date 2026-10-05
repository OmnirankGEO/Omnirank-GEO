"""
员工注册中心
负责管理所有AI员工的注册、获取和调度
"""

from typing import Dict, Optional, List
from .base_employee import BaseEmployee


class EmployeeRegistry:
    """
    员工注册中心 - 单例模式
    
    功能：
    1. 注册和管理所有员工实例
    2. 按ID/部门查询员工
    3. 获取员工列表
    """
    
    _instance: Optional["EmployeeRegistry"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # 在单例创建时初始化实例属性
            cls._instance._employees = {}
            cls._instance._initialized = False
        return cls._instance
    
    @classmethod
    def get_instance(cls) -> "EmployeeRegistry":
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def initialize(self, lazy: bool = True):
        """
        初始化所有员工
        
        Args:
            lazy: 是否延迟加载（默认True，第一次使用时才创建）
        """
        if self._initialized:
            return
        
        if not lazy:
            self._load_all_employees()
        
        self._initialized = True
    
    def _load_all_employees(self):
        """加载所有员工（内部方法）"""
        # 延迟导入，避免循环依赖
        from .project_director import ProjectDirector
        from .diagnosis import DataCollector, AITester, CompetitorAnalyst, ReportWriter
        from .content import ContentPlanner, ContentWriter, DouyinCreator, XhsCreator
        from .support import PPTSpecialist, ChiefEditor, IndustryExpert, LegalAdvisor
        
        # 领导层 (1人)
        self.register(ProjectDirector())
        
        # 诊断部 (4人)
        self.register(DataCollector())
        self.register(AITester())
        self.register(CompetitorAnalyst())
        self.register(ReportWriter())
        
        # 内容部 (4人)
        self.register(ContentPlanner())
        self.register(ContentWriter())
        self.register(DouyinCreator())
        self.register(XhsCreator())
        
        # 支持部 (4人)
        self.register(PPTSpecialist())
        self.register(ChiefEditor())
        self.register(IndustryExpert())
        self.register(LegalAdvisor())
    
    def register(self, employee: BaseEmployee):
        """注册员工"""
        self._employees[employee.employee_id] = employee
        # 避免Windows gbk编码问题，使用try-except
        try:
            print(f"  [OK] 注册员工: {employee.name} ({employee.employee_id})")
        except UnicodeEncodeError:
            print(f"  [OK] Registered: {employee.employee_id}")
    
    def get(self, employee_id: str) -> Optional[BaseEmployee]:
        """
        获取员工实例
        
        如果员工未加载，会尝试延迟加载
        """
        if employee_id not in self._employees:
            self._try_lazy_load(employee_id)
        
        return self._employees.get(employee_id)
    
    def _try_lazy_load(self, employee_id: str):
        """尝试延迟加载指定员工"""
        # 领导层
        if employee_id == "project_director":
            from .project_director import ProjectDirector
            self.register(ProjectDirector())
        
        # 诊断部
        elif employee_id == "data_collector":
            from .diagnosis import DataCollector
            self.register(DataCollector())
        elif employee_id == "ai_tester":
            from .diagnosis import AITester
            self.register(AITester())
        elif employee_id == "competitor_analyst":
            from .diagnosis import CompetitorAnalyst
            self.register(CompetitorAnalyst())
        elif employee_id == "report_writer":
            from .diagnosis import ReportWriter
            self.register(ReportWriter())
        
        # 内容部
        elif employee_id == "content_planner":
            from .content import ContentPlanner
            self.register(ContentPlanner())
        elif employee_id == "content_writer":
            from .content import ContentWriter
            self.register(ContentWriter())
        elif employee_id == "douyin_creator":
            from .content import DouyinCreator
            self.register(DouyinCreator())
        elif employee_id == "xhs_creator":
            from .content import XhsCreator
            self.register(XhsCreator())
        
        # 支持部
        elif employee_id == "ppt_specialist":
            from .support import PPTSpecialist
            self.register(PPTSpecialist())
        elif employee_id == "chief_editor":
            from .support import ChiefEditor
            self.register(ChiefEditor())
        elif employee_id == "industry_expert":
            from .support import IndustryExpert
            self.register(IndustryExpert())
        elif employee_id == "legal_advisor":
            from .support import LegalAdvisor
            self.register(LegalAdvisor())
        
        # 动态员工：从数据库加载
        else:
            self._try_load_from_db(employee_id)
    
    def _try_load_from_db(self, employee_id: str):
        """尝试从数据库加载动态员工"""
        try:
            import json
            from .dynamic_employee import DynamicEmployee
            from db.connection import get_connection as _get_conn

            conn = _get_conn()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, name, department_id, avatar, system_prompt,
                       model_id, temperature, max_tokens, memory_type, skills
                FROM employee_configs
                WHERE id = %s AND is_active = 1
            """, (employee_id,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                # 解析技能列表
                skills = []
                if row["skills"]:
                    try:
                        skills = json.loads(row["skills"])
                    except:
                        skills = []
                
                # 创建动态员工实例
                employee = DynamicEmployee(
                    employee_id=row["id"],
                    name=f"{row['avatar']} {row['name']}" if row["avatar"] and row["avatar"] not in row["name"] else row["name"],
                    department=row["department_id"],
                    sys_prompt=row["system_prompt"] or "",
                    model=row["model_id"] or "deepseek-v4-flash",  # 2026-05-22 V3.2→V4 全切
                    temperature=row["temperature"] or 0.7,
                    max_tokens=row["max_tokens"] or 15000,
                    memory_type=row["memory_type"] or "temporary",
                    skill_list=skills,
                )
                self.register(employee)
                
        except Exception as e:
            print(f"  [WARN] 无法从数据库加载员工 {employee_id}: {e}")
    
    def list_all(self) -> List[dict]:
        """列出所有已注册的员工"""
        # 确保所有员工已加载
        if not self._initialized:
            self._load_all_employees()
            self._initialized = True
        
        return [emp.get_profile() for emp in self._employees.values()]
    
    def list_by_department(self, department: str) -> List[dict]:
        """按部门列出员工"""
        if not self._initialized:
            self._load_all_employees()
            self._initialized = True
        
        return [
            emp.get_profile() 
            for emp in self._employees.values() 
            if emp.department == department
        ]
    
    def get_departments(self) -> List[dict]:
        """获取所有部门信息"""
        return [
            {
                "id": "leadership",
                "name": "👔 领导层",
                "description": "负责战略规划、任务分配和团队协调",
            },
            {
                "id": "diagnosis",
                "name": "📊 诊断部",
                "description": "负责品牌GEO诊断分析",
            },
            {
                "id": "content",
                "name": "✍️ 内容部",
                "description": "负责内容创作和选题策划",
            },
            {
                "id": "support",
                "name": "📑 支持部",
                "description": "负责报告交付和质量审核",
            },
        ]
    
    def clear(self):
        """清空所有员工（仅用于测试）"""
        self._employees.clear()
        self._initialized = False


# 便捷函数
def get_employee(employee_id: str) -> Optional[BaseEmployee]:
    """快捷获取员工"""
    return EmployeeRegistry.get_instance().get(employee_id)


def list_employees() -> List[dict]:
    """快捷列出所有员工"""
    return EmployeeRegistry.get_instance().list_all()
