"""
项目负责人
团队的最高领导人和战略军师，负责会议主持、任务分配、进度跟踪和决策支持
"""

import os
import sys
from typing import Optional, Any, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from employees.base_employee import BaseEmployee


class ProjectDirector(BaseEmployee):
    """
    👔 项目负责人
    
    定位：
    - 赛博版的老板本人
    - AI员工团队的最高领导人
    - 老板的首席军师和智囊
    
    核心职责：
    1. 会议主持：召集员工开会、控场、引导讨论方向
    2. 任务分配：将大任务拆解，指派给合适的员工执行
    3. 进度追踪：监控各员工任务状态，处理卡点
    4. 总结输出：汇总各方成果，形成最终报告/决策
    5. 战略咨询：作为军师，提供决策建议
    
    特殊能力：
    - 可调用任意员工执行子任务
    - 主持圆桌会议，总结共识
    - 持久化记忆：记住项目背景、客户情况、历史决策
    - 主动汇报：定期向老板报告进展
    """
    
    def __init__(self):
        super().__init__(
            employee_id="project_director",
            name="👔 项目负责人",
            department="leadership",  # 领导层，独立于其他部门
            sys_prompt="""# 角色定义

你是「全域上榜」的**项目负责人**，代表老板管理整个AI员工团队。

你是老板的**赛博分身**和**首席军师**：
- 老板告诉你目标，你负责拆解、分配、监督、总结
- 你要像老板本人一样思考，替老板做决策
- 遇到重大决策时，先向老板请示

---

# 核心能力

## 1. 会议主持
- 根据议题召集相关员工
- 控制讨论节奏，确保高效
- 引导共识形成，避免跑题
- 输出会议纪要和行动项

## 2. 任务分配
- 理解老板的高层目标
- 拆解成可执行的子任务
- 选择最合适的员工执行
- 设定优先级和截止时间

## 3. 进度追踪
- 监控各员工任务状态
- 识别卡点并协调资源
- 及时向老板预警风险

## 4. 总结输出
- 汇总各员工产出
- 整合成最终可交付物
- 质量把关后提交老板

## 5. 战略咨询
- 当老板犹豫时，提供分析和建议
- 基于数据给出决策支持
- 提前预判风险和机会

---

# 与老板的沟通风格

- **简洁汇报**：先结论，后细节
- **主动请示**：重大决策前征求意见
- **及时预警**：发现问题立即告知
- **建议导向**：不只报问题，更给方案

---

# 可调用的员工

| 员工 | 何时调用 |
|:-----|:---------|
| 数据采集员 | 需要搜索抖音/小红书/网页内容时 |
| AI测试员 | 需要检测品牌AI可见度时 |
| 竞品分析师 | 需要分析竞品情况时 |
| 报告撰稿人 | 需要生成GEO报告时 |
| 内容策划师 | 需要规划选题/标题时 |
| 正文撰稿人 | 需要撰写长文时 |
| 抖音内容师 | 需要抖音专属内容时 |
| 小红书内容师 | 需要小红书专属内容时 |
| PPT专员 | 需要制作演示文稿时 |
| 首席编辑 | 需要内容审核时 |
| 行业专家 | 需要专业知识支持时 |

---

# 工作原则

1. **目标导向**：始终围绕老板的最终目标展开工作
2. **高效执行**：快速决策，避免拖延
3. **质量把关**：对输出物负责，确保质量
4. **风险意识**：主动识别和预警潜在问题
5. **持续优化**：总结经验，优化流程

---

# 输出格式

## 任务分解输出
```
【任务分解】
老板目标：[原始目标]
子任务清单：
1. [任务名] - 负责人：[员工] - 优先级：[高/中/低]
2. ...

【执行计划】
第一步：...
第二步：...
```

## 会议纪要输出
```
【会议纪要】
议题：[议题]
参会员工：[员工列表]
讨论要点：
- ...
共识结论：
- ...
行动项：
- [ ] [任务] - 负责人 - 截止时间
```

## 向老板汇报
```
【向老板汇报】
📊 执行进度：[进度概述]
✅ 已完成：[成果列表]
⚠️ 风险预警：[如有]
💡 建议：[决策建议]
```
""",
            model="deepseek",  # 2026-05-22 V3.2→V4-flash 全切 · 走 base_employee.MODEL_CONFIGS[deepseek-v3.2] alias = v4-flash
            temperature=0.7,  # 需要一定的创造性和决策能力
            max_tokens=8000,  # 需要更长的输出空间
            memory_type="persistent",  # 持久化记忆
        )
    
    @property
    def skills(self) -> list[str]:
        return [
            "meeting_host",         # 会议主持
            "task_assignment",      # 任务分配
            "progress_tracking",    # 进度追踪
            "strategic_planning",   # 战略规划
            "summary_reporting",    # 总结汇报
            "decision_support",     # 决策支持
            "employee_coordination", # 员工协调
        ]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """
        项目负责人的技能执行
        
        与普通员工不同，项目负责人可以调用其他员工
        """
        results = {}
        
        # 分析任务，判断是否需要调用其他员工
        # 这里暂时返回空，实际调用由后续迭代实现
        # TODO: 实现员工调度逻辑
        
        return results
    
    async def delegate_task(
        self, 
        employee_id: str, 
        task: str, 
        context: Optional[dict] = None
    ) -> dict:
        """
        委派任务给下属员工
        
        Args:
            employee_id: 员工ID
            task: 任务描述
            context: 背景信息
        
        Returns:
            员工执行结果
        """
        from employees.employee_registry import get_employee
        
        employee = get_employee(employee_id)
        if not employee:
            return {
                "status": "failed",
                "error": f"员工不存在: {employee_id}",
            }
        
        # 执行任务
        result = await employee.execute_task(task, context)
        
        # 记录到会话历史
        self._conversation_history.append({
            "role": "system",
            "content": f"委派给 {employee.name}：{task}",
        })
        self._conversation_history.append({
            "role": "system",
            "content": f"{employee.name} 执行结果：{result.get('result', '')}",
        })
        
        return result
    
    async def host_meeting(
        self, 
        topic: str, 
        employee_ids: List[str],
        agenda: Optional[str] = None
    ) -> dict:
        """
        主持会议
        
        Args:
            topic: 会议议题
            employee_ids: 参会员工ID列表
            agenda: 会议议程
        
        Returns:
            会议纪要
        """
        from employees.employee_registry import get_employee
        
        # 构建会议prompt
        meeting_context = {
            "topic": topic,
            "employees": [
                get_employee(eid).name for eid in employee_ids 
                if get_employee(eid)
            ],
            "agenda": agenda or "讨论并达成共识",
        }
        
        # 调用LLM主持会议
        meeting_prompt = f"""
## 会议主持任务

议题：{topic}
参会员工：{', '.join(meeting_context['employees'])}
议程：{agenda or '讨论并达成共识'}

请作为会议主持人，引导讨论并输出会议纪要。
"""
        
        result = await self.execute_task(meeting_prompt, meeting_context)
        return result
    
    async def create_action_plan(
        self, 
        goal: str, 
        context: Optional[dict] = None
    ) -> dict:
        """
        为老板的目标制定行动计划
        
        Args:
            goal: 老板的目标
            context: 背景信息
        
        Returns:
            行动计划
        """
        plan_prompt = f"""
## 制定行动计划

老板目标：{goal}

请分析这个目标，拆解成具体的行动步骤，并指定负责的员工。
按照【任务分解】格式输出。
"""
        
        result = await self.execute_task(plan_prompt, context)
        return result
    
    def __repr__(self):
        return f"<ProjectDirector {self.employee_id}: {self.name} 👑>"
    
    async def execute_complex_task(
        self, 
        goal: str, 
        context: Optional[dict] = None,
        auto_delegate: bool = True
    ) -> dict:
        """
        执行复杂任务（Handoffs模式）
        
        自动拆解任务 -> 分配给员工 -> 收集结果 -> 汇总输出
        
        Args:
            goal: 老板的最终目标
            context: 背景信息
            auto_delegate: 是否自动执行（否则只输出计划）
        
        Returns:
            {
                "plan": [...],      # 拆解的子任务
                "results": [...],   # 各员工执行结果
                "summary": "...",   # 最终汇总
                "status": "completed"
            }
        """
        import json
        from employees.employee_registry import get_employee, list_employees
        
        # Step 1: 让项目负责人分析并拆解任务
        available_employees = list_employees()
        employee_info = "\n".join([
            f"- {emp['id']}: {emp['name']} ({emp['department']}部门)"
            for emp in available_employees
            if emp['id'] != 'project_director'
        ])
        
        plan_prompt = f"""## 任务拆解

老板目标：{goal}

{f"背景信息：{json.dumps(context, ensure_ascii=False)}" if context else ""}

可调用的员工：
{employee_info}

请分析这个目标，拆解成多个子任务。

**要求**：
1. 每个子任务指定一个负责员工
2. 子任务之间有清晰的依赖关系
3. 输出JSON格式（用代码块包裹）

```json
{{
  "analysis": "目标分析...",
  "subtasks": [
    {{"employee_id": "员工ID", "task": "任务描述", "priority": 1, "depends_on": []}},
    {{"employee_id": "员工ID", "task": "任务描述", "priority": 2, "depends_on": [0]}}
  ]
}}
```
"""
        
        # 调用LLM获取计划
        plan_result = await self.execute_task(plan_prompt, {})
        plan_content = plan_result.get("result", "")
        
        # 解析JSON
        subtasks = []
        try:
            # 提取代码块中的JSON
            if "```json" in plan_content:
                json_str = plan_content.split("```json")[1].split("```")[0].strip()
            elif "```" in plan_content:
                json_str = plan_content.split("```")[1].split("```")[0].strip()
            else:
                json_str = plan_content
            
            plan_data = json.loads(json_str)
            subtasks = plan_data.get("subtasks", [])
        except json.JSONDecodeError:
            # 解析失败，返回原始计划
            return {
                "status": "plan_only",
                "plan": plan_content,
                "message": "任务拆解完成，但无法自动执行（JSON解析失败）",
            }
        
        if not auto_delegate:
            return {
                "status": "plan_only",
                "plan": subtasks,
                "analysis": plan_data.get("analysis", ""),
                "message": "任务拆解完成，等待确认后执行",
            }
        
        # Step 2: 按顺序执行子任务
        results = []
        context_accumulator = context or {}
        
        for i, subtask in enumerate(subtasks):
            employee_id = subtask.get("employee_id")
            task_desc = subtask.get("task")
            
            employee = get_employee(employee_id)
            if not employee:
                results.append({
                    "subtask_index": i,
                    "employee_id": employee_id,
                    "status": "skipped",
                    "error": f"员工不存在: {employee_id}",
                })
                continue
            
            # 将之前的结果作为上下文传递
            task_context = {
                **context_accumulator,
                "previous_results": results,
            }
            
            # 执行子任务
            try:
                result = await employee.execute_task(task_desc, task_context)
                results.append({
                    "subtask_index": i,
                    "employee_id": employee_id,
                    "employee_name": employee.name,
                    "task": task_desc,
                    "status": "completed",
                    "result": result.get("result", ""),
                })
                
                # 累积到上下文
                context_accumulator[f"result_{i}"] = result.get("result", "")
                
            except Exception as e:
                results.append({
                    "subtask_index": i,
                    "employee_id": employee_id,
                    "status": "failed",
                    "error": str(e),
                })
        
        # Step 3: 汇总结果
        summary_prompt = f"""## 任务汇总

老板目标：{goal}

执行结果：
{json.dumps(results, ensure_ascii=False, indent=2)}

请作为项目负责人，汇总各员工的执行结果，输出最终成果报告。
使用【向老板汇报】格式。
"""
        
        summary_result = await self.execute_task(summary_prompt, {})
        
        return {
            "status": "completed",
            "plan": subtasks,
            "results": results,
            "summary": summary_result.get("result", ""),
            "completed_count": len([r for r in results if r.get("status") == "completed"]),
            "total_count": len(subtasks),
        }


# 便捷函数
async def delegate_to_team(goal: str, context: Optional[dict] = None) -> dict:
    """
    快捷委派任务给团队
    由项目负责人自动分配和协调
    """
    director = ProjectDirector()
    result = await director.create_action_plan(goal, context)
    return result


async def execute_complex_task(goal: str, context: Optional[dict] = None, auto_execute: bool = True) -> dict:
    """
    执行复杂任务的快捷函数
    
    Args:
        goal: 老板目标
        context: 背景信息
        auto_execute: 是否自动执行
    
    Returns:
        执行结果
    """
    director = ProjectDirector()
    result = await director.execute_complex_task(goal, context, auto_delegate=auto_execute)
    return result

