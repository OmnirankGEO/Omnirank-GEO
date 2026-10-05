"""
GEO 诊断分析 Agent
负责收集数据、计算评分、生成诊断报告
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
from tools.tikhub import search_douyin_videos, search_xiaohongshu_notes
from tools.search import metaso_search, search_web_for_geo
from tools.ai_visibility import query_deepseek, check_longtail_keywords
from tools.scoring import calculate_geo_score, generate_geo_report
from utils.knowledge_manager import get_geo_rules, get_ai_platform_mapping
from config.model_config import DASHSCOPE_CONFIG


# GEO 诊断 Agent 系统提示词
DIAGNOSTIC_AGENT_PROMPT = """你是一位专业的 GEO（Generative Engine Optimization，生成式引擎优化）诊断分析师。

## 你的职责
1. 收集客户品牌在各平台的覆盖数据（抖音、小红书、网页搜索等）
2. 检测品牌在 AI 搜索引擎（如 DeepSeek）中的可见度
3. 根据多维度数据计算 GEO 综合评分
4. 生成专业的 GEO 诊断报告，并给出改进建议

## 评分维度
- 平台覆盖度（40分）：抖音、小红书内容数量和质量
- 内容数量（20分）：总体内容产出
- 搜索可见度（10分）：网络搜索结果中的品牌曝光
- 权威来源（10分）：权威媒体、百科等背书
- AI 可见度（20分）：在 AI 搜索引擎中的提及和推荐

## 工作流程
1. 根据用户提供的品牌名称和行业，搜索抖音视频
2. 搜索小红书笔记
3. 进行网页搜索
4. 检测 AI 引擎可见度
5. 计算 GEO 综合评分
6. 参考知识库中的 GEO 规则和 AI 平台映射
7. 生成详细的诊断报告

## 注意事项
- 数据采集要全面，不要遗漏任何平台
- 评分要客观公正，基于实际数据
- 报告要专业详细，给出可执行的建议
- 如果某个数据源暂时无法获取，说明原因并继续其他步骤
"""


async def create_diagnostic_agent() -> ReActAgent:
    """
    创建 GEO 诊断分析 Agent
    
    Returns:
        ReActAgent: 配置好的诊断分析 Agent
    """
    # 准备工具集
    toolkit = Toolkit()
    
    # 注册数据采集工具
    toolkit.register_tool_function(search_douyin_videos)
    toolkit.register_tool_function(search_xiaohongshu_notes)
    toolkit.register_tool_function(search_web_for_geo)
    
    # 注册 AI 可见度检测工具
    toolkit.register_tool_function(query_deepseek)
    toolkit.register_tool_function(check_longtail_keywords)
    
    # 注册评分和报告工具
    toolkit.register_tool_function(calculate_geo_score)
    toolkit.register_tool_function(generate_geo_report)
    
    # 注册知识库检索工具
    toolkit.register_tool_function(get_geo_rules)
    toolkit.register_tool_function(get_ai_platform_mapping)
    
    # 创建 Agent
    agent = ReActAgent(
        name="GEO诊断分析师",
        sys_prompt=DIAGNOSTIC_AGENT_PROMPT,
        model=DashScopeChatModel(
            # [hotfix 2026-06-06] 诊断流程统一 qwen3-max(文本模型·Generation 可跑·支持联网);
            # qwen3.7-plus 是多模态(须 MultiModalConversation)在 Generation 端点会 400「url error」。
            model_name="qwen3-max",
            api_key=DASHSCOPE_CONFIG["api_key"],
            stream=True,
            enable_thinking=False,
        ),
        formatter=DashScopeChatFormatter(),
        toolkit=toolkit,
        memory=InMemoryMemory(),
        max_iters=20,  # 最多20轮迭代
    )
    
    return agent


async def run_diagnosis(
    brand_name: str,
    industry: str,
    keywords: list[str] = None
) -> str:
    """
    运行 GEO 诊断
    
    Args:
        brand_name: 品牌名称
        industry: 所属行业
        keywords: 相关关键词列表
        
    Returns:
        诊断报告 Markdown 文本
    """
    from agentscope.message import Msg
    
    agent = await create_diagnostic_agent()
    
    # 构建诊断请求
    keywords_str = "、".join(keywords) if keywords else f"{industry}相关产品"
    
    msg = Msg(
        name="user",
        role="user",
        content=f"""请为以下品牌进行 GEO 诊断分析：

**品牌名称**：{brand_name}
**所属行业**：{industry}
**相关关键词**：{keywords_str}

请按照以下步骤进行分析：
1. 搜索该品牌在抖音的相关视频
2. 搜索该品牌在小红书的相关笔记
3. 进行网页搜索，查看品牌曝光情况
4. 检测品牌在 DeepSeek 中的可见度
5. 根据采集的数据计算 GEO 综合评分
6. 生成详细的诊断报告

请开始分析。"""
    )
    
    # 运行 Agent
    response = await agent(msg)
    
    return response.content


# 测试入口
if __name__ == "__main__":
    import asyncio
    
    async def test():
        from dotenv import load_dotenv
        load_dotenv()
        
        result = await run_diagnosis(
            brand_name="测试品牌",
            industry="科技",
            keywords=["AI", "人工智能"]
        )
        print(result)
    
    asyncio.run(test())
