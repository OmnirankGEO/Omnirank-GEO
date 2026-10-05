"""
Director Agent - 任务编排器
负责接收用户请求，分解任务，调度其他 Agent 完成工作
"""

import os
import asyncio
from agentscope.agent import ReActAgent
from agentscope.model import DashScopeChatModel
from agentscope.formatter import DashScopeChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.tool import Toolkit, ToolResponse
from agentscope.message import Msg

import sys
sys.path.append('..')
from config.model_config import DASHSCOPE_CONFIG


# Director Agent 系统提示词
DIRECTOR_PROMPT = """你是 GEO 业务系统的总调度 Agent（Director）。

## 你的职责
1. **理解用户需求**：准确理解用户想要完成的 GEO 相关任务
2. **任务分解**：将复杂任务分解为可执行的子任务
3. **调度执行**：调用合适的子 Agent 或工具完成子任务
4. **汇总报告**：整合结果，向用户呈现最终输出

## 可用的子任务类型

### 1. GEO 诊断分析
- 触发条件：用户想了解品牌的 GEO 可见度状况
- 工具：run_geo_diagnosis
- 输出：诊断报告

### 2. GEO 内容创作
- 触发条件：用户需要创作 GEO 优化内容
- 工具：run_geo_writing
- 输出：GEO 优化文章

### 3. 内容策略规划
- 触发条件：用户需要制定内容矩阵和发布计划
- 工具：run_content_strategy
- 输出：策略文档

### 4. AI 可见度检测
- 触发条件：用户想检测品牌在特定 AI 引擎中的表现
- 工具：batch_query_ai_engines
- 输出：可见度报告

### 5. 社媒数据采集
- 触发条件：用户需要采集抖音/小红书等平台数据
- 工具：search_douyin_videos, search_xiaohongshu_notes
- 输出：数据报告

## 工作原则

1. **先理解再行动**：确保完全理解用户需求后再开始执行
2. **分步执行**：复杂任务要分步骤执行，每步确认结果
3. **异常处理**：如果某个子任务失败，说明原因并继续其他任务
4. **结果汇总**：所有子任务完成后，整合输出给用户

## 示例对话

用户："帮我分析一下我们品牌在 AI 搜索中的表现"
你的思考：
- 这是一个 GEO 诊断任务
- 需要收集品牌信息
- 调用 run_geo_diagnosis

用户："帮我写一篇关于XX的知乎文章"
你的思考：
- 这是一个 GEO 内容创作任务
- 需要了解主题、关键词
- 调用 run_geo_writing
"""


async def run_geo_diagnosis(
    brand_name: str,
    industry: str,
    keywords: str = ""
) -> ToolResponse:
    """
    执行 GEO 诊断分析任务
    
    Args:
        brand_name (str): 品牌名称
        industry (str): 所属行业
        keywords (str): 相关关键词，多个关键词用逗号分隔
        
    Returns:
        ToolResponse: 诊断报告
    """
    from agents.diagnostic_agent import run_diagnosis
    
    keyword_list = [k.strip() for k in keywords.split(",")] if keywords else None
    result = await run_diagnosis(brand_name, industry, keyword_list)
    
    return ToolResponse(
        content=[{"type": "text", "text": result}]
    )


async def run_geo_writing(
    topic: str,
    platform: str,
    keywords: str,
    industry: str,
    brand_name: str = ""
) -> ToolResponse:
    """
    执行 GEO 内容创作任务
    
    Args:
        topic (str): 文章主题
        platform (str): 目标平台（知乎、百家号、公众号等）
        keywords (str): 目标关键词，多个用逗号分隔
        industry (str): 所属行业
        brand_name (str): 品牌名称（可选）
        
    Returns:
        ToolResponse: 生成的文章
    """
    from agents.geo_writer_agent import generate_geo_article
    
    keyword_list = [k.strip() for k in keywords.split(",")]
    result = await generate_geo_article(topic, platform, keyword_list, industry, brand_name)
    
    return ToolResponse(
        content=[{"type": "text", "text": result}]
    )


async def run_content_strategy(
    brand_name: str,
    industry: str,
    core_keywords: str,
    target_audience: str = ""
) -> ToolResponse:
    """
    执行内容策略规划任务
    
    Args:
        brand_name (str): 品牌名称
        industry (str): 所属行业
        core_keywords (str): 核心关键词，多个用逗号分隔
        target_audience (str): 目标受众描述
        
    Returns:
        ToolResponse: 内容策略文档
    """
    # 这里可以调用专门的策略 Agent，暂时返回模板
    strategy_template = f"""# {brand_name} GEO 内容策略规划

## 一、品牌分析
- **品牌名称**：{brand_name}
- **所属行业**：{industry}
- **核心关键词**：{core_keywords}
- **目标受众**：{target_audience or "待补充"}

## 二、平台策略
（待分析后填充）

## 三、关键词矩阵
（待分析后填充）

## 四、内容选题库
（待分析后填充）

---
*请使用 GEO 诊断功能获取更详细的分析*
"""
    
    return ToolResponse(
        content=[{"type": "text", "text": strategy_template}]
    )


async def create_director_agent() -> ReActAgent:
    """
    创建 Director Agent
    
    Returns:
        ReActAgent: 配置好的 Director Agent
    """
    # 准备工具集
    toolkit = Toolkit()
    
    # 注册任务执行工具
    toolkit.register_tool_function(run_geo_diagnosis)
    toolkit.register_tool_function(run_geo_writing)
    toolkit.register_tool_function(run_content_strategy)
    
    # 注册 Skills
    toolkit.register_agent_skill("skills/geo_diagnosis")
    toolkit.register_agent_skill("skills/geo_writing")
    toolkit.register_agent_skill("skills/content_strategy")
    
    # 获取 Skills 提示词
    skills_prompt = toolkit.get_agent_skill_prompt()
    
    # 创建 Agent
    agent = ReActAgent(
        name="Director",
        sys_prompt=DIRECTOR_PROMPT + "\n\n" + skills_prompt,
        model=DashScopeChatModel(
            model_name="qwen3.7-max",  # [2026-06-06] 使用最强模型做任务编排(qwen3-max→qwen3.7-max)
            api_key=DASHSCOPE_CONFIG["api_key"],
            stream=True,
            enable_thinking=False,
        ),
        formatter=DashScopeChatFormatter(),
        toolkit=toolkit,
        memory=InMemoryMemory(),
        max_iters=30,
    )
    
    return agent


async def chat_with_director(user_input: str) -> str:
    """
    与 Director Agent 对话
    
    Args:
        user_input: 用户输入
        
    Returns:
        Agent 回复
    """
    from utils.knowledge_manager import geo_knowledge
    
    # 初始化知识库
    await geo_knowledge.initialize()
    
    # 创建 Director
    agent = await create_director_agent()
    
    # 发送消息
    msg = Msg(name="user", role="user", content=user_input)
    response = await agent(msg)
    
    return response.content


# 交互式对话入口
if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    
    load_dotenv()
    
    async def main():
        print("GEO Director Agent 已启动")
        print("输入 'exit' 退出\n")
        
        while True:
            user_input = input("你: ")
            if user_input.lower() == "exit":
                break
            
            response = await chat_with_director(user_input)
            print(f"\nDirector: {response}\n")
    
    asyncio.run(main())
