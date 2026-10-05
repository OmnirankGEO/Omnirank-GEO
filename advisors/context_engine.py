"""
上下文引擎 - 三要素自动注入
负责为每次LLM调用注入：公司信息 + IP人设 + 顾问知识库
"""

import json
from pathlib import Path
from typing import Optional, Any


class ContextEngine:
    """
    上下文引擎 - 自动注入三要素到Prompt
    
    三要素：
    1. 公司信息（素材层）：行业背景、产品卖点、目标客户
    2. IP人设（表达层）：人物定位、说话风格、口头禅
    3. 顾问知识库（策略层）：方法论、框架、技巧
    """
    
    def __init__(self):
        self.questions_path = Path(__file__).parent / "config" / "questions.json"
        self._questions_cache = None
    
    def load_questions(self) -> dict:
        """加载问题框架配置"""
        if self._questions_cache is None:
            with open(self.questions_path, "r", encoding="utf-8") as f:
                self._questions_cache = json.load(f)
        return self._questions_cache
    
    def get_question_template(self, module: str, question_id: str) -> Optional[dict]:
        """获取问题模板"""
        questions = self.load_questions()
        module_config = questions.get(module)
        if not module_config:
            return None
        
        for q in module_config.get("questions", []):
            if q["id"] == question_id:
                return q
        return None
    
    def get_module_questions(self, module: str) -> list[dict]:
        """获取模块的所有问题"""
        questions = self.load_questions()
        module_config = questions.get(module)
        if not module_config:
            return []
        return module_config.get("questions", [])
    
    def format_question(self, question_template: dict, variables: dict) -> str:
        """填充问题模板变量"""
        question_text = question_template["question"]
        for var in question_template.get("input_vars", []):
            placeholder = "{" + var + "}"
            value = variables.get(var, f"[未提供{var}]")
            question_text = question_text.replace(placeholder, str(value))
        return question_text
    
    def build_company_context(self, project_data: dict) -> str:
        """构建公司上下文"""
        if not project_data:
            return ""
        
        parts = []
        
        # 🆕 最重要：完整的公司介绍（包含业务说明）
        if project_data.get("company_intro"):
            parts.append(f"📋 公司介绍：{project_data['company_intro']}")
        
        if project_data.get("industry"):
            parts.append(f"- 行业：{project_data['industry']}")
        if project_data.get("business"):
            parts.append(f"- 业务：{project_data['business']}")
        if project_data.get("target_audience"):
            parts.append(f"- 目标客户：{project_data['target_audience']}")
        if project_data.get("product_highlights"):
            parts.append(f"- 产品卖点：{project_data['product_highlights']}")
        if project_data.get("competitive_advantage"):
            parts.append(f"- 竞争优势：{project_data['competitive_advantage']}")
        
        # 🆕 核心卖点
        if project_data.get("core_selling_points"):
            selling_points = project_data['core_selling_points']
            if isinstance(selling_points, list):
                selling_text = "\n  ".join([f"• {p}" for p in selling_points if p])
            else:
                selling_text = str(selling_points)
            if selling_text:
                parts.append(f"💎 核心卖点：\n  {selling_text}")
        
        # 🆕 独特价值
        if project_data.get("unique_value"):
            parts.append(f"⭐ 独特价值：{project_data['unique_value']}")
        
        # 🆕 方法论
        if project_data.get("methodology"):
            parts.append(f"🔧 方法论：{project_data['methodology']}")

        # 🆕 structured_knowledge 摘要（客户案例/产品/成分知识）
        if project_data.get("structured_knowledge_summary"):
            parts.append(f"\n📚 专属知识库：\n{project_data['structured_knowledge_summary']}")

        return "\n".join(parts) if parts else "暂无公司信息"
    
    def build_persona_context(self, persona_data: dict) -> str:
        """构建人设上下文"""
        if not persona_data:
            return ""
        
        parts = []
        if persona_data.get("one_liner"):
            parts.append(f"- 人设定位：{persona_data['one_liner']}")
        if persona_data.get("speaking_style"):
            parts.append(f"- 说话风格：{persona_data['speaking_style']}")
        if persona_data.get("catchphrase"):
            parts.append(f"- 口头禅：{persona_data['catchphrase']}")
        if persona_data.get("pain_points"):
            parts.append(f"- 痛点经历：{persona_data['pain_points']}")
        if persona_data.get("background"):
            parts.append(f"- 人物背景：{persona_data['background']}")
        
        return "\n".join(parts) if parts else "暂无人设信息"
    
    def build_knowledge_context(self, knowledge_results: list[dict]) -> str:
        """构建知识库上下文"""
        if not knowledge_results:
            return "暂无相关方法论参考"
        
        parts = []
        for i, doc in enumerate(knowledge_results, 1):
            source = doc.get("filename", doc.get("source", "未知来源"))
            content = doc.get("content", "")
            parts.append(f"【参考{i}】来自《{source}》：\n{content}")
        
        return "\n\n".join(parts)
    
    def build_full_prompt(
        self,
        question: str,
        company_data: dict = None,
        persona_data: dict = None,
        knowledge_results: list[dict] = None,
        advisor_name: str = "顾问",
        advisor_base_prompt: str = None,
        extra_context: str = ""
    ) -> str:
        """
        构建完整Prompt，自动注入三要素
        
        Args:
            question: 用户问题（已填充变量）
            company_data: 公司信息
            persona_data: IP人设信息
            knowledge_results: RAG检索结果
            advisor_name: 顾问名称
            advisor_base_prompt: 顾问原生系统提示词（角色设定）
            extra_context: 额外上下文
        
        Returns:
            完整的Prompt文本
        """
        company_ctx = self.build_company_context(company_data)
        persona_ctx = self.build_persona_context(persona_data)
        knowledge_ctx = self.build_knowledge_context(knowledge_results)
        
        # 使用顾问原生角色设定（如果提供）
        if advisor_base_prompt:
            prompt_parts = [advisor_base_prompt]
        else:
            prompt_parts = [f"你是{advisor_name}，请基于你的专业知识和方法论回答问题。"]
        
        if company_ctx:
            prompt_parts.append(f"\n【公司背景】\n{company_ctx}")
        
        if persona_ctx:
            prompt_parts.append(f"\n【IP人设】\n{persona_ctx}")
        
        if knowledge_ctx:
            prompt_parts.append(f"\n【你的方法论参考】\n{knowledge_ctx}")
        
        if extra_context:
            prompt_parts.append(f"\n【补充信息】\n{extra_context}")
        
        prompt_parts.append(f"\n【问题】\n{question}")
        prompt_parts.append("\n请基于以上信息，给出专业的回答。")
        
        return "\n".join(prompt_parts)
    
    def build_multi_advisor_prompt(
        self,
        question: str,
        company_data: dict = None,
        persona_data: dict = None,
        main_advisor_knowledge: list[dict] = None,
        sub_advisor_knowledge: list[dict] = None,
        main_advisor_name: str = "主顾问",
        sub_advisor_name: str = None,
        successful_patterns: list[dict] = None,
        negative_feedback: list[dict] = None,
        extra_context: str = ""
    ) -> str:
        """
        构建多顾问融合Prompt
        
        多顾问融合逻辑：
        - 主顾问知识权重0.7，提供核心框架
        - 副顾问知识权重0.3，提供补充视角
        
        Args:
            question: 用户问题
            company_data: 公司信息
            persona_data: IP人设信息
            main_advisor_knowledge: 主顾问RAG检索结果
            sub_advisor_knowledge: 副顾问RAG检索结果
            main_advisor_name: 主顾问名称
            sub_advisor_name: 副顾问名称（None表示单顾问模式）
            successful_patterns: 历史成功套路
            negative_feedback: 历史失败教训
            extra_context: 额外上下文
        """
        company_ctx = self.build_company_context(company_data)
        persona_ctx = self.build_persona_context(persona_data)
        
        # 构建开头
        if sub_advisor_name:
            prompt_parts = [
                f"你同时具备{main_advisor_name}和{sub_advisor_name}的专业能力。",
                f"{main_advisor_name}是你的主要方法论来源，{sub_advisor_name}提供补充视角。"
            ]
        else:
            prompt_parts = [f"你是{main_advisor_name}，请基于你的专业知识和方法论回答问题。"]
        
        if company_ctx:
            prompt_parts.append(f"\n【客户背景】\n{company_ctx}")
        
        if persona_ctx:
            prompt_parts.append(f"\n【IP人设】\n{persona_ctx}")
        
        # 多顾问知识融合
        if main_advisor_knowledge or sub_advisor_knowledge:
            prompt_parts.append("\n【方法论参考】")
            
            if main_advisor_knowledge:
                main_ctx = self.build_knowledge_context(main_advisor_knowledge)
                prompt_parts.append(f"\n## {main_advisor_name}的核心方法（主要参考）：\n{main_ctx}")
            
            if sub_advisor_knowledge:
                sub_ctx = self.build_knowledge_context(sub_advisor_knowledge)
                prompt_parts.append(f"\n## {sub_advisor_name}的补充视角（辅助参考）：\n{sub_ctx}")
        
        # 历史经验反馈
        if successful_patterns:
            patterns_text = "\n".join([f"- {p.get('content', p)}" for p in successful_patterns[:5]])
            prompt_parts.append(f"\n【历史成功套路】\n{patterns_text}")
        
        if negative_feedback:
            feedback_text = "\n".join([f"- {f.get('content', f)}" for f in negative_feedback[:3]])
            prompt_parts.append(f"\n【需要避免的问题】\n{feedback_text}")
        
        if extra_context:
            prompt_parts.append(f"\n【补充信息】\n{extra_context}")
        
        prompt_parts.append(f"\n【问题】\n{question}")
        
        if sub_advisor_name:
            prompt_parts.append(f"\n请综合{main_advisor_name}的核心方法和{sub_advisor_name}的补充视角，给出专业的回答。")
        else:
            prompt_parts.append("\n请基于以上信息，给出专业的回答。")
        
        return "\n".join(prompt_parts)
    
    def build_experience_context(self, profile: dict) -> dict:
        """
        从档案中提取经验上下文
        
        Returns:
            {
                "successful_patterns": [...],
                "negative_feedback": [...],
                "brand_constraints": "..."
            }
        """
        if not profile:
            return {}
        
        return {
            "successful_patterns": profile.get("successful_patterns") or [],
            "negative_feedback": profile.get("negative_feedback") or [],
            "brand_constraints": profile.get("brand_constraints") or ""
        }


# 全局单例
_context_engine = None

def get_context_engine() -> ContextEngine:
    """获取上下文引擎单例"""
    global _context_engine
    if _context_engine is None:
        _context_engine = ContextEngine()
    return _context_engine

