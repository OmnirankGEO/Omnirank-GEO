"""
任务执行引擎
支持顺序/并行执行任务，SSE流式推送进度

用于：
1. 会议后的任务分配执行
2. 复杂诊断流程
3. 内容生成流程
4. 其他多步骤任务
"""

import os
import sys
import json
import asyncio
import time
from typing import List, Dict, Any, AsyncGenerator, Optional
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"          # 待执行
    RUNNING = "running"          # 执行中
    COMPLETED = "completed"      # 已完成
    FAILED = "failed"            # 失败
    PAUSED = "paused"            # 已暂停
    SKIPPED = "skipped"          # 已跳过


@dataclass
class Task:
    """单个任务定义"""
    id: str                          # 任务ID
    name: str                        # 任务名称
    assignee_id: str                 # 负责人员工ID
    assignee_name: str               # 负责人名称
    input_required: str              # 输入要求
    output_expected: str             # 期望产出
    acceptance_criteria: str         # 验收标准
    estimated_time: str              # 预估时间
    
    # 执行状态
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""                 # 执行结果
    error: str = ""                  # 错误信息
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    # 依赖关系
    depends_on: List[str] = field(default_factory=list)  # 依赖的任务ID列表
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "assignee_id": self.assignee_id,
            "assignee_name": self.assignee_name,
            "input_required": self.input_required,
            "output_expected": self.output_expected,
            "acceptance_criteria": self.acceptance_criteria,
            "estimated_time": self.estimated_time,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "depends_on": self.depends_on,
        }


class TaskExecutor:
    """
    任务执行引擎
    
    功能：
    1. 按顺序/并行执行任务
    2. 支持任务依赖
    3. SSE流式推送进度
    4. 支持暂停/继续
    """
    
    def __init__(self, tasks: List[Task], context: str = ""):
        self.tasks = {t.id: t for t in tasks}
        self.task_order = [t.id for t in tasks]
        self.context = context
        self.is_paused = False
        self.current_task_id: Optional[str] = None
        self.execution_id = f"exec_{int(time.time())}"
        self.results: Dict[str, str] = {}  # 存储各任务结果
        
    async def execute_stream(self) -> AsyncGenerator[dict, None]:
        """
        流式执行所有任务
        
        Yields:
            {
                "type": "start" | "task_start" | "task_log" | "task_complete" | "task_error" | "complete",
                "data": {...}
            }
        """
        from employees.employee_registry import get_employee
        
        # 发送开始事件
        yield {
            "type": "start",
            "data": {
                "execution_id": self.execution_id,
                "total_tasks": len(self.tasks),
                "tasks": [t.to_dict() for t in self.tasks.values()],
            }
        }
        
        completed_count = 0
        failed_count = 0
        
        for task_id in self.task_order:
            task = self.tasks[task_id]
            
            # 检查是否暂停
            while self.is_paused:
                await asyncio.sleep(0.5)
                yield {
                    "type": "paused",
                    "data": {"task_id": task_id, "message": "执行已暂停"}
                }
            
            # 检查依赖是否完成
            if task.depends_on:
                for dep_id in task.depends_on:
                    dep_task = self.tasks.get(dep_id)
                    if dep_task and dep_task.status != TaskStatus.COMPLETED:
                        task.status = TaskStatus.SKIPPED
                        task.error = f"依赖任务 {dep_id} 未完成"
                        yield {
                            "type": "task_skipped",
                            "data": {"task_id": task_id, "reason": task.error}
                        }
                        continue
            
            # 开始执行任务
            self.current_task_id = task_id
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now()
            
            yield {
                "type": "task_start",
                "data": {
                    "task_id": task_id,
                    "task_name": task.name,
                    "assignee": task.assignee_name,
                    "assignee_id": task.assignee_id,
                }
            }
            
            try:
                # 加载执行员工
                employee = get_employee(task.assignee_id)
                if not employee:
                    raise Exception(f"无法加载员工: {task.assignee_id}")
                
                # 构建任务提示词
                task_prompt = self._build_task_prompt(task)
                
                # 发送执行日志
                yield {
                    "type": "task_log",
                    "data": {
                        "task_id": task_id,
                        "message": f"{task.assignee_name} 开始执行任务...",
                        "level": "info",
                    }
                }
                
                # 执行任务
                result = await employee.execute_task(task_prompt, use_skills=False)
                
                if result.get("status") == "completed":
                    task.status = TaskStatus.COMPLETED
                    task.result = result.get("result", "")
                    task.completed_at = datetime.now()
                    self.results[task_id] = task.result
                    completed_count += 1
                    
                    yield {
                        "type": "task_complete",
                        "data": {
                            "task_id": task_id,
                            "task_name": task.name,
                            "result": task.result[:500] + "..." if len(task.result) > 500 else task.result,
                            "duration": (task.completed_at - task.started_at).total_seconds(),
                        }
                    }
                else:
                    raise Exception(result.get("error", "执行失败"))
                    
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                task.completed_at = datetime.now()
                failed_count += 1
                
                yield {
                    "type": "task_error",
                    "data": {
                        "task_id": task_id,
                        "task_name": task.name,
                        "error": str(e),
                    }
                }
        
        # 发送完成事件
        yield {
            "type": "complete",
            "data": {
                "execution_id": self.execution_id,
                "total_tasks": len(self.tasks),
                "completed": completed_count,
                "failed": failed_count,
                "results": self.results,
            }
        }
    
    def _build_task_prompt(self, task: Task) -> str:
        """构建任务执行提示词 - v2.0强化版"""
        # 获取已完成任务的结果作为上下文
        previous_results = ""
        for tid, result in self.results.items():
            prev_task = self.tasks.get(tid)
            if prev_task:
                previous_results += f"\n### {prev_task.name} 的产出\n{result[:1000]}\n"
        
        # v2.0: 智能截断背景信息，避免超过模型限制
        context_preview = self.context
        if len(self.context) > 8000:
            context_preview = self.context[:8000] + "\n...(背景信息过长已截断)"
        
        return f"""# 任务执行

## 任务名称
{task.name}

## 背景信息（会议资料）
{context_preview}

## 前序任务产出
{previous_results if previous_results else "无"}

## 输入要求
{task.input_required}

## 期望产出
{task.output_expected}

## 验收标准
{task.acceptance_criteria}

## ⚠️ 强制要求
1. **直接输出期望产出的内容本身**，不要输出"任务分解"或"执行计划"
2. **必须引用背景信息中的具体内容**（如"根据资料..."）
3. **输出必须是完整的、可直接使用的交付物**
4. **禁止"我建议"、"接下来我将"等开场白**

## 你的任务
请根据以上要求，**直接产出「期望产出」中描述的内容**。不要说准备做什么，直接做。
"""

    def pause(self):
        """暂停执行"""
        self.is_paused = True
    
    def resume(self):
        """继续执行"""
        self.is_paused = False
    
    def get_status(self) -> dict:
        """获取执行状态"""
        return {
            "execution_id": self.execution_id,
            "is_paused": self.is_paused,
            "current_task": self.current_task_id,
            "tasks": [t.to_dict() for t in self.tasks.values()],
        }


def parse_tasks_from_markdown(markdown: str, participant_ids: List[str] = None) -> List[Task]:
    """
    从Markdown任务表格解析任务列表
    
    期望格式：
    | 序号 | 任务名称 | 负责人 | 输入要求 | 输出产物 | 验收标准 | 预估时间 |
    """
    import re
    
    tasks = []
    
    # 查找表格
    table_pattern = r'\|[^\n]+\|'
    lines = markdown.split('\n')
    table_lines = [l for l in lines if l.strip().startswith('|') and '---' not in l]
    
    if len(table_lines) < 2:
        return tasks
    
    # 跳过表头
    for line in table_lines[1:]:
        cells = [c.strip() for c in line.split('|')[1:-1]]
        if len(cells) >= 7:
            try:
                task = Task(
                    id=f"task_{len(tasks)+1}",
                    name=cells[1],
                    assignee_name=cells[2],
                    assignee_id=_guess_employee_id(cells[2], participant_ids),
                    input_required=cells[3],
                    output_expected=cells[4],
                    acceptance_criteria=cells[5],
                    estimated_time=cells[6],
                )
                tasks.append(task)
            except Exception as e:
                print(f"解析任务行失败: {e}")
    
    return tasks


def _guess_employee_id(name: str, participant_ids: List[str] = None) -> str:
    """根据名称猜测员工ID"""
    name_to_id = {
        "项目负责人": "project_director",
        "数据采集员": "data_collector",
        "AI测试员": "ai_tester",
        "竞品分析师": "competitor_analyst",
        "报告撰稿人": "report_writer",
        "内容策划师": "content_planner",
        "正文撰稿人": "content_writer",
        "抖音创作者": "douyin_creator",
        "小红书创作者": "xhs_creator",
        "PPT专员": "ppt_specialist",
        "首席编辑": "chief_editor",
        "行业专家": "industry_expert",
        "营销总管": "marketing_director",
    }
    
    # 尝试精确匹配（去掉emoji）
    clean_name = name
    for emoji in ['👔', '📊', '📡', '🤖', '📈', '📋', '📝', '✍️', '🎬', '📱', '📑', '🔍', '🏭']:
        clean_name = clean_name.replace(emoji, '').strip()
    
    for key, val in name_to_id.items():
        if key in clean_name or clean_name in key:
            return val
    
    return "project_director"  # 默认
