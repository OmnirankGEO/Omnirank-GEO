"""
GEO 内容写作 Agent
负责生成符合 GEO 优化规则的内容
"""

import os
import asyncio
from agentscope.agent import ReActAgent
from agentscope.model import DashScopeChatModel
from agentscope.formatter import DashScopeChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.tool import Toolkit

# 导入工具函数
import sys
sys.path.append('..')
from tools.search import metaso_search, search_web_for_geo, search_scholar_for_geo
from utils.knowledge_manager import (
    get_geo_rules, 
    get_platform_style, 
    get_ai_platform_mapping,
    get_title_formulas
)
from config.model_config import DASHSCOPE_CONFIG, OPENROUTER_CONFIG


# GEO 内容写作 Agent 系统提示词
GEO_WRITER_PROMPT = """你是一位专业的 GEO（Generative Engine Optimization）内容优化专家。

## 你的职责
根据客户需求，创作符合 GEO 优化规则的高质量内容，使内容能够被 AI 搜索引擎（如 DeepSeek、Kimi、文心一言）优先引用和推荐。

## 核心优化原则

### 1. 内容结构优化
- 使用清晰的标题层级（H1-H4）
- 关键信息放在段落开头
- 使用列表、表格提升可读性
- 添加「要点速览」或「TL;DR」总结

### 2. 权威性增强
- 引用权威数据和研究
- 添加专家观点和行业报告
- 注明数据来源和时间
- 使用统计数据支撑论点

### 3. 语言优化
- 自信断言式表达，避免模糊用语
- 使用专业术语但保持可读性
- 添加示例、案例增强可信度
- 保持客观中立的语气

### 4. AI 引擎优化
- 直接回答用户可能的问题
- 内容覆盖长尾关键词
- 提供完整解决方案而非片段
- 使用流畅的逻辑链接

## 工作流程
1. 分析写作任务和目标平台
2. 从知识库获取平台风格规范
3. 从知识库获取 GEO 优化规则
4. 搜索相关素材和数据支撑
5. 根据标题公式生成优化标题
6. 撰写符合规范的内容
7. 检查内容是否符合 GEO 规则

## 输出格式
- 使用 Markdown 格式
- 标题层级清晰
- 包含必要的列表和表格
- 添加权威来源引用
"""


async def create_geo_writer_agent() -> ReActAgent:
    """
    创建 GEO 内容写作 Agent
    
    Returns:
        ReActAgent: 配置好的 GEO 写作 Agent
    """
    # 准备工具集
    toolkit = Toolkit()
    
    # 注册素材采集工具
    toolkit.register_tool_function(search_web_for_geo)
    toolkit.register_tool_function(search_scholar_for_geo)
    
    # 注册知识库检索工具
    toolkit.register_tool_function(get_geo_rules)
    toolkit.register_tool_function(get_platform_style)
    toolkit.register_tool_function(get_ai_platform_mapping)
    toolkit.register_tool_function(get_title_formulas)
    
    # 创建 Agent
    agent = ReActAgent(
        name="GEO内容专家",
        sys_prompt=GEO_WRITER_PROMPT,
        model=DashScopeChatModel(
            model_name="qwen3.6-plus",  # 使用 qwen3.6-plus 平衡成本与质量
            api_key=DASHSCOPE_CONFIG["api_key"],
            stream=True,
            enable_thinking=False,
        ),
        formatter=DashScopeChatFormatter(),
        toolkit=toolkit,
        memory=InMemoryMemory(),
        max_iters=15,
    )
    
    return agent


async def generate_geo_article(
    topic: str,
    platform: str,
    target_keywords: list[str],
    industry: str,
    brand_name: str = ""
) -> str:
    """
    生成 GEO 优化文章
    
    Args:
        topic: 文章主题
        platform: 目标平台（知乎、百家号、公众号等）
        target_keywords: 目标关键词列表
        industry: 所属行业
        brand_name: 品牌名称（可选）
        
    Returns:
        生成的文章 Markdown 文本
    """
    from agentscope.message import Msg
    
    agent = await create_geo_writer_agent()
    
    keywords_str = "、".join(target_keywords)
    brand_str = f"并自然融入品牌「{brand_name}」的相关内容" if brand_name else ""
    
    msg = Msg(
        name="user",
        role="user",
        content=f"""请为以下需求创作一篇 GEO 优化文章：

**文章主题**：{topic}
**目标平台**：{platform}
**目标关键词**：{keywords_str}
**所属行业**：{industry}

请按照以下步骤进行：
1. 获取「{platform}」平台的内容风格规范
2. 获取 GEO 优化规则
3. 根据用户搜索意图获取标题公式
4. 搜索相关素材和数据支撑
5. 撰写一篇 1500-2000 字的专业文章{brand_str}

要求：
- 标题要能吸引 AI 引擎引用
- 内容结构清晰，使用 Markdown 格式
- 引用权威数据和来源
- 自然融入目标关键词
- 符合目标平台的风格规范

请开始创作。"""
    )
    
    response = await agent(msg)
    return response.content
