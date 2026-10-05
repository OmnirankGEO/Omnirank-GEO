"""
AI员工基类
基于AgentScope v1.0设计，支持单独调用和团队协作
"""

import os
import json
import httpx
from abc import ABC, abstractmethod
from typing import Optional, Any
from datetime import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.model_config import DEEPSEEK_CONFIG, DASHSCOPE_CONFIG
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH


class BaseEmployee(ABC):
    """
    AI员工基类
    
    设计原则：
    1. 每个员工是独立可调用的单元
    2. 支持技能(Skills)扩展
    3. 支持持久化/临时记忆
    4. 可被任务路由器调度
    """
    
    # 模型配置映射
    MODEL_CONFIGS = {
        # DeepSeek 官方 API（不支持联网）
        "deepseek": {
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": DEEPSEEK_OFFICIAL_FLASH,
            "supports_search": False,
        },
        # 键 "deepseek-reasoner" 是**注册表键**(调用方按它取档),不跟着改;
        # 改的是它发出去的 model_name。
        # 🔴 [WO_206 c1b② · Owner 2026-09-14 拍板「全部改成 deepseek-flash」]
        #    Deploy 206-d2 当天实打:官方 /models 只剩 deepseek-flash 与 deepseek-v4-pro;
        #    `deepseek-reasoner` 仍返 200,但**回显 deepseek-flash** —— 那一档已经没有了,
        #    这一行一直在**静默降级**跑 flash。改成常量不是「换模型」,是把已经发生的事写明。
        "deepseek-reasoner": {
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": DEEPSEEK_OFFICIAL_FLASH,
            "supports_search": False,
        },
        # DeepSeek-V4-Flash 通过 DashScope(2026-05-09 新增 · 速度+轻量任务 · 默认推荐)
        "deepseek-v4-flash": {
            "api_key_env": "DASHSCOPE_API_KEY",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_name": "deepseek-v4-flash",
            "supports_search": True,
        },
        # DeepSeek-V4-Pro 通过 DashScope(2026-05-09 新增 · 重度推理+展示质量 · 写文章/PPT 用)
        "deepseek-v4-pro": {
            "api_key_env": "DASHSCOPE_API_KEY",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_name": "deepseek-v4-pro",
            "supports_search": True,
        },
        # 2026-05-22 老板拍板:V3.2 → V4 全切 · key 保留兼容老 employee_configs.model_id rows
        # 实际配置走 v4-flash(更快更便宜)· 老 row 自动升级 V4
        "deepseek-v3.2": {
            "api_key_env": "DASHSCOPE_API_KEY",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_name": "deepseek-v4-flash",
            "supports_search": True,
        },
        # Qwen 模型（支持联网）
        "qwen": {
            "api_key_env": "DASHSCOPE_API_KEY",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_name": "qwen3.6-plus",
            "supports_search": True,
        },
        "qwen3.7-max": {
            "api_key_env": "DASHSCOPE_API_KEY",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_name": "qwen3.7-max",
            "supports_search": True,
        },
        # 豆包 - 需要在.env中配置 DOUBAO_ENDPOINT_ID
        # 如需使用豆包，请：
        # 1. 在火山引擎控制台创建endpoint
        # 2. 在.env中设置 DOUBAO_ENDPOINT_ID=ep-xxxxxxxx
        "doubao": {
            "api_key_env": "DOUBAO_API_KEY",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "model_name": "DOUBAO_ENDPOINT_ID",  # 使用环境变量
            "supports_search": False,
            "requires_endpoint": True,  # 标记需要endpoint配置
        },
    }
    
    def __init__(
        self,
        employee_id: str,
        name: str,
        department: str,
        sys_prompt: str,
        model: str = "deepseek",
        temperature: float = 0.7,
        max_tokens: int = 4000,
        memory_type: str = "temporary",
        enable_search: bool = True,
        # 🆕 统一知识库支持
        use_unified_kb: bool = False,
        brand_id: int = None,
    ):
        """
        初始化员工
        
        Args:
            employee_id: 员工唯一标识（如 'data_collector'）
            name: 员工名称（如 '📡 数据采集员'）
            department: 所属部门（如 'diagnosis'）
            sys_prompt: 系统提示词
            model: 模型标识（'deepseek', 'qwen', 'qwen3-max'）
            temperature: 生成温度
            max_tokens: 最大Token数
            memory_type: 记忆类型（'temporary' 或 'persistent'）
            enable_search: 是否启用联网搜索（默认True，仅Qwen模型支持）
            use_unified_kb: 🆕 是否启用统一知识库（默认False，向后兼容）
            brand_id: 🆕 关联的客户ID（用于检索客户专属知识）
        """
        self.employee_id = employee_id
        self.name = name
        self.department = department
        self.sys_prompt = sys_prompt
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_type = memory_type
        self.enable_search = enable_search
        self.status = "online"
        
        # 🆕 统一知识库配置
        self.use_unified_kb = use_unified_kb
        self.brand_id = brand_id
        
        # 会话历史（临时记忆）
        self._conversation_history: list[dict] = []
        
        # 技能列表（子类覆盖）
        self._skills: list[str] = []
    
    @property
    @abstractmethod
    def skills(self) -> list[str]:
        """返回员工技能列表（子类必须实现）"""
        pass
    
    @property
    def description(self) -> str:
        """返回员工职责描述"""
        return self.sys_prompt.split('\n')[0] if self.sys_prompt else ""
    
    def get_profile(self) -> dict:
        """返回员工资料卡"""
        return {
            "id": self.employee_id,
            "name": self.name,
            "department": self.department,
            "description": self.description,
            "model": self.model,
            "skills": self.skills,
            "status": self.status,
            "memory_type": self.memory_type,
        }
    
    async def execute_task(
        self, 
        task: str, 
        context: Optional[dict] = None,
        use_skills: bool = True,
    ) -> dict:
        """
        执行任务（核心方法）
        
        Args:
            task: 任务描述
            context: 可选的背景信息
            use_skills: 是否启用技能调用
        
        Returns:
            {
                "employee_id": str,
                "task": str,
                "result": str,
                "status": "completed" | "failed",
                "execution_time": float,
                "skills_used": list[str],
            }
        """
        start_time = datetime.now()
        skills_used = []
        
        try:
            # 1. 预处理：如果需要，先调用技能获取数据
            skill_results = {}
            if use_skills:
                skill_results = await self._execute_skills(task, context)
                skills_used = list(skill_results.keys())
            
            # 2. 构建完整提示
            full_prompt = self._build_prompt(task, context, skill_results)
            
            # 3. 调用LLM
            result = await self._call_llm(full_prompt)
            
            # 4. 记录到会话历史
            self._conversation_history.append({
                "role": "user",
                "content": task,
                "timestamp": start_time.isoformat(),
            })
            self._conversation_history.append({
                "role": "assistant",
                "content": result,
                "timestamp": datetime.now().isoformat(),
            })
            
            execution_time = (datetime.now() - start_time).total_seconds()
            
            return {
                "employee_id": self.employee_id,
                "employee_name": self.name,
                "task": task,
                "result": result,
                "status": "completed",
                "execution_time": execution_time,
                "skills_used": skills_used,
            }
            
        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            return {
                "employee_id": self.employee_id,
                "employee_name": self.name,
                "task": task,
                "result": f"执行失败: {str(e)}",
                "status": "failed",
                "execution_time": execution_time,
                "skills_used": skills_used,
                "error": str(e),
            }
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """
        执行相关技能获取数据
        子类可覆盖此方法实现具体技能调用
        
        Returns:
            {"skill_name": skill_result, ...}
        """
        return {}
    
    def _build_prompt(
        self, 
        task: str, 
        context: Optional[dict],
        skill_results: dict[str, Any],
    ) -> str:
        """构建完整提示"""
        parts = []
        
        # 添加背景信息
        if context:
            parts.append("## 背景信息")
            for key, value in context.items():
                parts.append(f"- **{key}**: {value}")
            parts.append("")
        
        # 添加技能执行结果
        if skill_results:
            parts.append("## 技能执行结果")
            for skill_name, result in skill_results.items():
                parts.append(f"### {skill_name}")
                if isinstance(result, dict):
                    parts.append(f"```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```")
                else:
                    parts.append(str(result))
            parts.append("")
        
        # 添加任务
        parts.append("## 任务")
        parts.append(task)
        
        return "\n".join(parts)
    
    async def _call_llm(self, prompt: str) -> str:
        """调用LLM"""
        model_config = self.MODEL_CONFIGS.get(self.model, self.MODEL_CONFIGS["deepseek"])
        
        api_key = os.environ.get(model_config["api_key_env"], "")
        if not api_key:
            raise ValueError(f"API Key not found: {model_config['api_key_env']}")
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        messages = [
            {"role": "system", "content": self.sys_prompt},
            {"role": "user", "content": prompt},
        ]
        
        payload = {
            "model": model_config["model_name"],
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        
        # 为支持联网搜索的模型启用该功能
        # DashScope协议（Qwen、DeepSeek-V4 等）支持 enable_search 参数
        if self.enable_search and model_config.get("supports_search", False):
            payload["enable_search"] = True
            # 可选：启用搜索来源返回
            # payload["enable_source"] = True
        
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{model_config['base_url']}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            
            return data["choices"][0]["message"]["content"]
    
    def clear_history(self):
        """清空会话历史"""
        self._conversation_history = []
    
    def get_history(self) -> list[dict]:
        """获取会话历史"""
        return self._conversation_history.copy()
    
    # ==================== 🆕 知识库支持 ====================
    
    async def retrieve_knowledge(
        self,
        query: str,
        brand_id: int = None,
        top_k: int = 5,
    ) -> list[dict]:
        """
        🆕 检索统一知识库
        
        Args:
            query: 检索查询
            brand_id: 可选的客户ID（覆盖初始化时的设置）
            top_k: 返回结果数量
        
        Returns:
            检索结果列表
        """
        if not self.use_unified_kb:
            return []
        
        try:
            from tools.unified_knowledge import get_unified_rag
            rag = get_unified_rag()
            
            results = await rag.retrieve(
                query=query,
                role_type="employee",
                role_id=self.employee_id,
                brand_id=brand_id or self.brand_id,
                top_k=top_k,
            )
            return results
        except Exception as e:
            print(f"[{self.name}] ⚠️ 知识库检索失败: {e}")
            return []
    
    def set_brand_id(self, brand_id: int):
        """🆕 动态设置客户ID（用于切换客户上下文）"""
        self.brand_id = brand_id
    
    def __repr__(self):
        return f"<{self.__class__.__name__} {self.employee_id}: {self.name}>"
