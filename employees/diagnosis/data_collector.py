"""
数据采集员
负责全网内容采集，包括抖音视频、小红书笔记、网页内容
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class DataCollector(BaseEmployee):
    """
    📡 数据采集员
    
    职责：
    1. 搜索抖音视频内容
    2. 搜索小红书笔记
    3. 搜索网页内容（秘塔）
    
    技能：
    - douyin_search: 抖音视频搜索
    - xhs_search: 小红书笔记搜索
    - web_search: 网页搜索（秘塔MCP）
    """
    
    def __init__(self):
        super().__init__(
            employee_id="data_collector",
            name="📡 数据采集员",
            department="diagnosis",
            sys_prompt="""# 角色定义
你是一位专业的数据采集员，负责从各个平台搜索和采集内容数据。

# 核心职责
1. 根据用户指定的关键词或品牌名，搜索相关内容
2. 整理搜索结果，提取关键信息
3. 输出结构化的数据报告

# 输出格式要求
- 使用Markdown格式
- 包含搜索关键词、平台、结果数量
- 列出TOP内容（标题、互动数据、链接）
- 如果没有找到结果，说明原因

# 注意事项
- 只返回真实搜索到的数据
- 不要编造不存在的内容
- 互动数据要准确转换（如1.2万=12000）
""",
            model="deepseek-v4-flash",
            temperature=0.3,  # 数据采集需要准确性
            max_tokens=4000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["douyin_search", "xhs_search", "web_search"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """
        根据任务自动判断需要调用的技能
        """
        results = {}
        task_lower = task.lower()
        
        # 提取搜索关键词（从任务描述或context中）
        keyword = None
        if context and "keyword" in context:
            keyword = context["keyword"]
        elif context and "brand_name" in context:
            keyword = context["brand_name"]
        else:
            # 尝试从任务中提取关键词（简单实现）
            import re
            match = re.search(r'["""](.+?)["""]', task)
            if match:
                keyword = match.group(1)
        
        if not keyword:
            return {"error": "未找到搜索关键词，请在任务中用引号标注关键词"}
        
        # 根据任务内容判断调用哪些技能
        if "抖音" in task_lower or "douyin" in task_lower:
            results["douyin_search"] = await self._search_douyin(keyword)
        
        if "小红书" in task_lower or "xhs" in task_lower or "红书" in task_lower:
            results["xhs_search"] = await self._search_xhs(keyword)
        
        if "网页" in task_lower or "秘塔" in task_lower or "metaso" in task_lower:
            results["web_search"] = await self._search_web(keyword)
        
        # 如果没有指定平台，默认搜索全部
        if not results and keyword:
            results["douyin_search"] = await self._search_douyin(keyword)
            results["xhs_search"] = await self._search_xhs(keyword)
        
        return results
    
    async def _search_douyin(self, keyword: str) -> dict:
        """调用抖音搜索技能"""
        try:
            from tools.tikhub.tikhub_tools import search_douyin_videos
            # 参数: keyword, sort_type="_1", publish_time="_180", page=1
            result = await search_douyin_videos(keyword)
            return result
        except Exception as e:
            return {"error": f"抖音搜索失败: {str(e)}", "keyword": keyword}
    
    async def _search_xhs(self, keyword: str) -> dict:
        """调用小红书搜索技能"""
        try:
            from tools.tikhub.tikhub_tools import search_xiaohongshu_notes
            # 参数: keyword, page=1, sort="comment_descending", note_time="半年内"
            result = await search_xiaohongshu_notes(keyword)
            return result
        except Exception as e:
            return {"error": f"小红书搜索失败: {str(e)}", "keyword": keyword}
    
    async def _search_web(self, keyword: str) -> dict:
        """调用秘塔网页搜索技能"""
        try:
            from tools.search.metaso_mcp import metaso_search
            result = await metaso_search(keyword)
            return result
        except Exception as e:
            return {"error": f"网页搜索失败: {str(e)}", "keyword": keyword}


# 便捷函数
async def search_all_platforms(keyword: str) -> dict:
    """快捷搜索所有平台"""
    collector = DataCollector()
    result = await collector.execute_task(
        f'搜索"{keyword}"在抖音和小红书的相关内容',
        context={"keyword": keyword}
    )
    return result
