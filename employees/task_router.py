"""
任务路由器
负责分析用户需求并分配给合适的员工
"""

import re
from typing import Optional
from employees.employee_registry import EmployeeRegistry, get_employee


# 技能关键词映射
SKILL_KEYWORDS = {
    # 数据采集相关
    "douyin_search": ["抖音", "抖音搜索", "抖音内容", "抖音视频", "dy"],
    "xhs_search": ["小红书", "红书", "xhs", "种草笔记"],
    "web_search": ["网页", "百度", "搜索", "网上", "网络", "秘塔"],
    "social_collect": ["社媒", "采集", "收集数据"],
    
    # AI测试相关
    "ai_visibility_test": ["AI测试", "AI可见", "chatgpt", "claude", "豆包", "kimi", "ai引擎", "大模型"],
    "citation_analysis": ["引用", "被引用", "引用分析"],
    
    # 竞品分析相关
    "competitor_identification": ["竞品", "竞争对手", "同行"],
    "gap_analysis": ["差距", "对比分析", "对比"],
    "viral_content_analysis": ["爆款", "热门内容", "拆解"],
    
    # 报告相关
    "geo_scoring": ["评分", "打分", "GEO评分", "得分"],
    "report_generation": ["报告", "诊断报告", "生成报告", "写报告"],
    
    # 内容策划相关
    "topic_planning": ["选题", "选题规划", "内容规划"],
    "title_generation": ["标题", "标题生成", "起标题"],
    "trend_mining": ["热点", "热搜", "趋势"],
    
    # 内容撰写相关
    "article_writing": ["写文章", "撰写", "长文"],
    "content_rewriting": ["改写", "润色", "重写"],
    "deai_optimization": ["去AI味", "去机器感", "人工优化"],
    
    # 抖音内容相关
    "hook_design": ["钩子", "开场", "抓眼球"],
    "script_writing": ["脚本", "视频脚本", "台词"],
    "douyin_copywriting": ["抖音话术", "抖音文案"],
    
    # 小红书内容相关
    "seeding_copywriting": ["种草", "种草文案", "笔记"],
    "cover_design": ["封面", "封面设计"],
    "hashtag_strategy": ["标签", "话题", "tag"],
    
    # PPT相关
    "ppt_generation": ["PPT", "演示文稿", "幻灯片"],
    "template_design": ["模板", "PPT模板"],
    
    # 审核相关
    "quality_review": ["审核", "检查", "校对", "质检"],
    "style_unification": ["风格统一", "语言统一"],
    "compliance_check": ["合规", "敏感词", "违规"],
    
    # 行业专家相关
    "industry_insight": ["行业洞察", "趋势分析", "市场分析"],
    "knowledge_retrieval": ["知识检索", "资料查询"],
    "expert_consultation": ["咨询", "专家意见", "行业问题"],
    
    # 法务相关
    "contract_drafting": ["合同", "协议", "契约", "条款", "起草合同", "写合同"],
    "legal_consultation": ["法律咨询", "法务", "法律问题", "律师"],
    "law_search": ["法规", "法律条款", "法条", "政策法规", "最新法律"],
    "risk_assessment": ["法律风险", "合规风险", "审核合同", "合同审核"],
}

# 员工技能映射（employee_id -> skills）
EMPLOYEE_SKILLS = {
    # 诊断部
    "data_collector": ["douyin_search", "xhs_search", "web_search", "social_collect"],
    "ai_tester": ["ai_visibility_test", "citation_analysis"],
    "competitor_analyst": ["competitor_identification", "gap_analysis", "viral_content_analysis"],
    "report_writer": ["geo_scoring", "report_generation"],
    
    # 内容部
    "content_planner": ["topic_planning", "title_generation", "trend_mining"],
    "content_writer": ["article_writing", "content_rewriting", "deai_optimization"],
    "douyin_creator": ["hook_design", "script_writing", "douyin_copywriting"],
    "xhs_creator": ["seeding_copywriting", "cover_design", "hashtag_strategy"],
    
    # 支持部
    "ppt_specialist": ["ppt_generation", "template_design"],
    "chief_editor": ["quality_review", "style_unification", "compliance_check"],
    "industry_expert": ["industry_insight", "knowledge_retrieval", "expert_consultation"],
    "legal_advisor": ["contract_drafting", "legal_consultation", "law_search", "risk_assessment"],
}

# 员工优先级（数字越小优先级越高）
EMPLOYEE_PRIORITY = {
    # 诊断部
    "data_collector": 1,
    "ai_tester": 2,
    "competitor_analyst": 3,
    "report_writer": 4,
    
    # 内容部
    "content_planner": 5,
    "content_writer": 6,
    "douyin_creator": 7,
    "xhs_creator": 8,
    
    # 支持部
    "ppt_specialist": 9,
    "chief_editor": 10,
    "industry_expert": 11,
    "legal_advisor": 8,  # 法务优先级较高
}


class TaskRouter:
    """
    任务路由器
    
    功能：
    1. 分析任务描述中的关键词
    2. 匹配最合适的员工
    3. 支持多员工协作任务
    """
    
    def __init__(self):
        self.registry = EmployeeRegistry.get_instance()
    
    def analyze_task(self, task: str) -> dict:
        """
        分析任务，提取技能需求
        
        Returns:
            {
                "matched_skills": ["skill1", "skill2"],
                "confidence": 0.85,
                "keywords_found": ["关键词1", "关键词2"],
            }
        """
        task_lower = task.lower()
        matched_skills = []
        keywords_found = []
        
        for skill, keywords in SKILL_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in task_lower:
                    if skill not in matched_skills:
                        matched_skills.append(skill)
                    keywords_found.append(keyword)
        
        # 计算置信度
        confidence = min(1.0, len(matched_skills) * 0.3 + len(keywords_found) * 0.1)
        
        return {
            "matched_skills": matched_skills,
            "confidence": round(confidence, 2),
            "keywords_found": list(set(keywords_found)),
        }
    
    def find_best_employee(self, task: str) -> dict:
        """
        找到最合适的员工
        
        Returns:
            {
                "employee_id": "data_collector",
                "employee_name": "数据采集员",
                "confidence": 0.85,
                "reason": "任务包含抖音搜索相关关键词",
                "matched_skills": ["douyin_search"],
            }
        """
        analysis = self.analyze_task(task)
        matched_skills = analysis["matched_skills"]
        
        if not matched_skills:
            # 没有匹配到技能，返回默认员工或提示
            return {
                "employee_id": None,
                "employee_name": None,
                "confidence": 0,
                "reason": "未能识别任务类型，请更明确地描述需求",
                "matched_skills": [],
                "suggestions": self._get_suggestions(),
            }
        
        # 计算每个员工的匹配分数
        employee_scores = {}
        for emp_id, emp_skills in EMPLOYEE_SKILLS.items():
            score = 0
            matched = []
            for skill in matched_skills:
                if skill in emp_skills:
                    score += 1
                    matched.append(skill)
            if score > 0:
                # 考虑优先级
                priority_bonus = (10 - EMPLOYEE_PRIORITY.get(emp_id, 10)) * 0.1
                employee_scores[emp_id] = {
                    "score": score + priority_bonus,
                    "matched": matched,
                }
        
        if not employee_scores:
            return {
                "employee_id": None,
                "employee_name": None,
                "confidence": 0,
                "reason": "没有员工具备所需技能",
                "matched_skills": matched_skills,
            }
        
        # 选择得分最高的员工
        best_emp_id = max(employee_scores, key=lambda x: employee_scores[x]["score"])
        best_info = employee_scores[best_emp_id]
        
        # 获取员工实例
        employee = get_employee(best_emp_id)
        emp_name = employee.name if employee else best_emp_id
        
        return {
            "employee_id": best_emp_id,
            "employee_name": emp_name,
            "confidence": analysis["confidence"],
            "reason": f"任务包含{', '.join(analysis['keywords_found'])}相关关键词",
            "matched_skills": best_info["matched"],
            "all_candidates": list(employee_scores.keys()),
        }
    
    def find_team(self, task: str, max_members: int = 3) -> list[dict]:
        """
        找到一组合适的员工（用于协作任务）
        
        Returns:
            [
                {"employee_id": "data_collector", "role": "数据采集"},
                {"employee_id": "report_writer", "role": "报告撰写"},
            ]
        """
        analysis = self.analyze_task(task)
        matched_skills = analysis["matched_skills"]
        
        team = []
        assigned_skills = set()
        
        # 按优先级排序员工
        sorted_employees = sorted(
            EMPLOYEE_SKILLS.items(),
            key=lambda x: EMPLOYEE_PRIORITY.get(x[0], 10)
        )
        
        for emp_id, emp_skills in sorted_employees:
            if len(team) >= max_members:
                break
            
            # 检查这个员工是否能贡献新技能
            new_skills = [s for s in emp_skills if s in matched_skills and s not in assigned_skills]
            if new_skills:
                employee = get_employee(emp_id)
                team.append({
                    "employee_id": emp_id,
                    "employee_name": employee.name if employee else emp_id,
                    "role": self._get_role_description(new_skills),
                    "skills": new_skills,
                })
                assigned_skills.update(new_skills)
        
        return team
    
    def _get_role_description(self, skills: list) -> str:
        """根据技能生成角色描述"""
        role_map = {
            "douyin_search": "抖音数据采集",
            "xhs_search": "小红书数据采集",
            "web_search": "网页数据采集",
            "ai_visibility_test": "AI可见度测试",
            "competitor_analysis": "竞品分析",
            "geo_scoring": "GEO评分",
            "report_generation": "报告撰写",
            "topic_planning": "选题策划",
            "content_writing": "内容撰写",
            "content_review": "内容审核",
        }
        return "、".join([role_map.get(s, s) for s in skills[:2]])
    
    def _get_suggestions(self) -> list[str]:
        """返回任务建议"""
        return [
            "搜索[品牌名]在抖音的相关内容",
            "测试[品牌名]在AI引擎中的可见度",
            "分析[品牌名]与竞品的差距",
            "为[品牌名]生成GEO诊断报告",
            "为[品牌名]策划选题",
        ]


# 单例
_router_instance = None

def get_router() -> TaskRouter:
    """获取路由器单例"""
    global _router_instance
    if _router_instance is None:
        _router_instance = TaskRouter()
    return _router_instance


async def route_task(task: str, auto_execute: bool = False) -> dict:
    """
    路由任务到合适的员工
    
    Args:
        task: 任务描述
        auto_execute: 是否自动执行
    
    Returns:
        {
            "success": True,
            "employee_id": "data_collector",
            "employee_name": "数据采集员",
            "confidence": 0.85,
            "reason": "...",
            "result": {...}  # 如果auto_execute=True
        }
    """
    router = get_router()
    match_result = router.find_best_employee(task)
    
    if not match_result["employee_id"]:
        return {
            "success": False,
            "error": match_result["reason"],
            "suggestions": match_result.get("suggestions", []),
        }
    
    result = {
        "success": True,
        **match_result,
    }
    
    if auto_execute:
        employee = get_employee(match_result["employee_id"])
        if employee:
            execution_result = await employee.execute_task(task)
            result["execution"] = execution_result
    
    return result
