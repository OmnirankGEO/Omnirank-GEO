"""
GEO 知识库管理 - 简化版
直接读取知识库文件，使用简单的关键词匹配检索
用于快速测试，后续可升级为完整 RAG
"""

import logging
import os
from pathlib import Path
from agentscope.tool import ToolResponse
import json
import re

from services.runtime_file_alarm import report_missing

logger = logging.getLogger("GEO-KnowledgeManager")

# 知识库目录
KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"

# 知识库配置
KNOWLEDGE_BASES = {
    "geo_rules": {
        "file": "geo_specific/GEO优化规则库（完整版）.md",
        "description": "GEO优化规则，包含15条核心规则和行业专属规则",
    },
    "platform_styles": {
        "file": "部分平台内容风格.md",
        "description": "8大平台内容风格规范，包括知乎、微信公众号、百家号等",
    },
    "ai_mapping": {
        "file": "AI对话平台→内容平台映射（完整版）.md",
        "description": "6大AI引擎的P0/P1/P2平台分级策略",
    },
    "title_formulas": {
        "file": "2026年用户搜索意图与热门标题公式知识库.md",
        "description": "用户搜索意图分析和热门标题生成公式",
    },
}


class GEOKnowledgeManager:
    """GEO 知识库管理器 - 简化版"""
    
    def __init__(self):
        self.knowledge_contents = {}
        self._initialized = False
    
    async def initialize(self):
        """加载所有知识库文件"""
        if self._initialized:
            return
        
        for kb_name, config in KNOWLEDGE_BASES.items():
            file_path = KNOWLEDGE_DIR / config["file"]
            
            if not file_path.exists():
                # 🔴 WO_272:原来只 print 一行 Warning —— 07-12 起 4 个文件全缺,没人看见。
                report_missing(file_path, what="Knowledge file", logger=logger)
                continue
            
            content = file_path.read_text(encoding="utf-8")
            self.knowledge_contents[kb_name] = {
                "content": content,
                "description": config["description"],
                "sections": self._parse_sections(content)
            }
            print(f"Loaded knowledge base: {kb_name} ({len(content)} chars)")
        
        self._initialized = True
    
    def _parse_sections(self, content: str) -> list[dict]:
        """将内容按章节解析"""
        sections = []
        current_section = {"title": "", "content": ""}
        
        for line in content.split("\n"):
            if line.startswith("## "):
                if current_section["content"]:
                    sections.append(current_section)
                current_section = {"title": line[3:].strip(), "content": ""}
            else:
                current_section["content"] += line + "\n"
        
        if current_section["content"]:
            sections.append(current_section)
        
        return sections
    
    async def retrieve(
        self, 
        query: str, 
        kb_names: list[str] = None,
        top_k: int = 5
    ) -> list[dict]:
        """
        关键词匹配检索
        """
        if not self._initialized:
            await self.initialize()
        
        if kb_names is None:
            kb_names = list(self.knowledge_contents.keys())
        
        results = []
        query_lower = query.lower()
        query_words = set(re.findall(r'\w+', query_lower))
        
        for kb_name in kb_names:
            if kb_name not in self.knowledge_contents:
                continue
            
            kb = self.knowledge_contents[kb_name]
            
            for section in kb["sections"]:
                section_text = (section["title"] + " " + section["content"]).lower()
                
                # 计算匹配分数
                matches = sum(1 for w in query_words if w in section_text)
                if matches > 0:
                    score = matches / len(query_words) if query_words else 0
                    results.append({
                        "source": kb_name,
                        "title": section["title"],
                        "content": section["content"][:500] + "..." if len(section["content"]) > 500 else section["content"],
                        "score": score
                    })
        
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
    
    def get_full_content(self, kb_name: str) -> str:
        """获取知识库完整内容"""
        if kb_name in self.knowledge_contents:
            return self.knowledge_contents[kb_name]["content"]
        return ""


# 全局实例
geo_knowledge = GEOKnowledgeManager()


async def retrieve_geo_knowledge(
    query: str,
    knowledge_types: list[str] = None,
    top_k: int = 5
) -> ToolResponse:
    """
    从 GEO 知识库检索相关内容
    
    Args:
        query (str): 检索查询语句
        knowledge_types (list[str]): 要检索的知识库类型列表
            可选值: geo_rules, platform_styles, ai_mapping, title_formulas
            默认检索所有知识库
        top_k (int): 返回结果数量
        
    Returns:
        ToolResponse: 包含检索结果的响应
    """
    results = await geo_knowledge.retrieve(query, knowledge_types, top_k)
    
    return ToolResponse(
        content=[{"type": "text", "text": json.dumps(results, ensure_ascii=False)}]
    )


async def get_geo_rules(topic: str, top_k: int = 3) -> ToolResponse:
    """
    获取 GEO 优化规则
    
    Args:
        topic (str): 要查询的主题，如"标题优化"、"内容结构"
        top_k (int): 返回规则数量
        
    Returns:
        ToolResponse: 包含相关规则的响应
    """
    return await retrieve_geo_knowledge(topic, ["geo_rules"], top_k)


async def get_platform_style(platform: str) -> ToolResponse:
    """
    获取平台内容风格规范
    
    Args:
        platform (str): 平台名称，如"知乎"、"小红书"、"微信公众号"
        
    Returns:
        ToolResponse: 包含平台风格规范的响应
    """
    return await retrieve_geo_knowledge(f"{platform} 内容风格规范", ["platform_styles"], 3)


async def get_ai_platform_mapping(ai_engine: str) -> ToolResponse:
    """
    获取 AI 引擎的平台映射策略
    
    Args:
        ai_engine (str): AI 引擎名称，如"DeepSeek"、"Kimi"、"文心一言"
        
    Returns:
        ToolResponse: 包含平台映射和优先级的响应
    """
    return await retrieve_geo_knowledge(f"{ai_engine} 内容平台 映射", ["ai_mapping"], 3)


async def get_title_formulas(search_intent: str) -> ToolResponse:
    """
    获取标题生成公式
    
    Args:
        search_intent (str): 用户搜索意图，如"购买决策"、"问题解决"、"信息获取"
        
    Returns:
        ToolResponse: 包含标题公式的响应
    """
    return await retrieve_geo_knowledge(f"{search_intent} 标题公式", ["title_formulas"], 5)
