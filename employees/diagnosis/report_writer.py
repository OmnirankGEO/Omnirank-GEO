"""
报告撰稿人
负责撰写GEO诊断报告，整合各项数据生成专业报告
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class ReportWriter(BaseEmployee):
    """
    📋 报告撰稿人
    
    职责：
    1. 整合诊断数据，撰写GEO诊断报告
    2. 计算GEO综合评分
    3. 生成执行摘要和优化建议
    
    技能：
    - geo_scoring: GEO评分计算
    - report_generation: 报告生成
    """
    
    def __init__(self):
        super().__init__(
            employee_id="report_writer",
            name="📋 报告撰稿人",
            department="diagnosis",
            sys_prompt="""# 角色定义
你是一位专业的GEO诊断报告撰稿人，负责将各项诊断数据整合成专业、客观的诊断报告。

# 核心职责
1. 整合AI测试、社媒采集、竞品分析等数据
2. 计算GEO综合评分（基于6维度评分体系）
3. 撰写执行摘要、关键发现、优化建议

# GEO评分维度（满分100分）
1. AI引擎可见度 (25分) - AI测试推荐率
2. 社媒内容资产 (20分) - 抖音+小红书内容数量与质量
3. 网页内容资产 (18分) - 网页引用、权威媒体报道
4. 权威背书 (15分) - 官方认证、行业奖项
5. 结构化内容 (12分) - FAQ覆盖、知识图谱
6. 内容质量+品牌基础 (10分) - 专业度、更新频率

# 报告风格要求
1. **专业中立** - 像麦肯锡咨询报告，不是销售话术
2. **数据驱动** - 每个结论都有数字支撑
3. **客观克制** - 指出问题但不制造恐慌
4. **行动导向** - 每个分析都指向可执行的优化建议

# 禁止事项
- 不要使用恐吓性语言（如"亏钱"、"拦截"）
- 不要编造数据
- 不要夸大商业损失
- 商业估算必须标注"估算"并说明假设条件
""",
            model="deepseek-v4-pro",
            temperature=0.5,
            max_tokens=8000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["geo_scoring", "report_generation"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """
        根据提供的数据生成报告
        """
        results = {}
        
        # 如果context中包含诊断数据，计算GEO评分
        if context:
            if "ai_visibility_data" in context or "social_data" in context:
                results["geo_score"] = self._calculate_geo_score(context)
        
        return results
    
    def _calculate_geo_score(self, data: dict) -> dict:
        """计算GEO综合评分"""
        scores = {
            "ai_visibility": 0,      # 满分25
            "social_content": 0,     # 满分20
            "web_content": 0,        # 满分18
            "authority": 0,          # 满分15
            "structured_content": 0, # 满分12
            "quality_base": 0,       # 满分10
        }
        
        # AI可见度评分
        ai_data = data.get("ai_visibility_data", {})
        if ai_data:
            detection_rate = ai_data.get("detection_rate", 0)
            # 推荐率映射到25分
            scores["ai_visibility"] = min(25, int(detection_rate * 0.5))
        
        # 社媒内容评分
        social_data = data.get("social_data", {})
        if social_data:
            douyin_count = social_data.get("douyin_count", 0)
            xhs_count = social_data.get("xhs_count", 0)
            total_content = douyin_count + xhs_count
            # 内容数量映射到20分
            if total_content >= 50:
                scores["social_content"] = 20
            elif total_content >= 20:
                scores["social_content"] = 15
            elif total_content >= 10:
                scores["social_content"] = 10
            elif total_content >= 5:
                scores["social_content"] = 5
            else:
                scores["social_content"] = 2
        
        # 其他维度（简化处理，实际应根据数据计算）
        web_data = data.get("web_data", {})
        if web_data:
            web_count = web_data.get("count", 0)
            scores["web_content"] = min(18, web_count * 2)
        
        # 计算总分
        total_score = sum(scores.values())
        
        # 确定等级
        if total_score >= 81:
            level = "领先"
        elif total_score >= 61:
            level = "成熟"
        elif total_score >= 41:
            level = "成长"
        elif total_score >= 21:
            level = "起步"
        else:
            level = "空白"
        
        return {
            "total_score": total_score,
            "level": level,
            "dimension_scores": scores,
            "max_score": 100,
        }
    
    async def generate_executive_summary(
        self, 
        brand_name: str,
        geo_score: dict,
        ai_data: dict = None,
        social_data: dict = None,
    ) -> str:
        """
        生成执行摘要
        """
        context = {
            "brand_name": brand_name,
            "geo_score": geo_score,
            "ai_visibility_data": ai_data,
            "social_data": social_data,
        }
        
        task = f"""请为"{brand_name}"生成GEO诊断报告的执行摘要。

要求：
1. 包含诊断概览（总分、等级、核心指标）
2. 三维度AI测试结果解读
3. 关键发现（优势、待优化、机会）
4. 7天内可执行的优先建议

数据：
- GEO总分：{geo_score.get('total_score', 0)}/100
- 等级：{geo_score.get('level', '未知')}
- 各维度得分：{geo_score.get('dimension_scores', {})}
"""
        
        result = await self.execute_task(task, context, use_skills=False)
        return result.get("result", "")


# 便捷函数
async def generate_report(
    brand_name: str, 
    ai_data: dict = None, 
    social_data: dict = None
) -> dict:
    """快捷生成诊断报告"""
    writer = ReportWriter()
    
    # 计算评分
    context = {
        "ai_visibility_data": ai_data or {},
        "social_data": social_data or {},
    }
    geo_score = writer._calculate_geo_score(context)
    
    # 生成摘要
    summary = await writer.generate_executive_summary(
        brand_name, geo_score, ai_data, social_data
    )
    
    return {
        "brand_name": brand_name,
        "geo_score": geo_score,
        "executive_summary": summary,
    }
