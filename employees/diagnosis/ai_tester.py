"""
AI测试员
负责检测品牌在各AI搜索引擎中的可见度
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee
from config.ai_engines import default_diagnosis_engines


class AITester(BaseEmployee):
    """
    🤖 AI测试员
    
    职责：
    1. 向多个AI引擎提问，检测品牌可见度
    2. 分析品牌在AI搜索中的表现
    3. 对比品牌词测试和场景词测试的结果
    
    技能：
    - ai_visibility_test: 多引擎AI可见度测试
    - brand_detection: 品牌提及检测
    """
    
    def __init__(self):
        super().__init__(
            employee_id="ai_tester",
            name="🤖 AI测试员",
            department="diagnosis",
            sys_prompt="""# 角色定义
你是一位专业的AI可见度测试员，负责检测品牌在各大AI搜索引擎中的表现。

# 核心职责
1. 设计测试问题（品牌词测试 + 场景词测试）
2. 向多个AI引擎（通义千问、DeepSeek、豆包、元宝）提问
3. 统计品牌被推荐/提及的次数
4. 分析测试结果，给出诊断

# 测试问题类型
1. **品牌词测试**：直接问"XX公司是什么"——测试AI是否认识品牌
2. **地区+行业测试**：问"深圳XX服务商推荐"——测试本地竞争力
3. **场景词测试**：问"XX哪家靠谱"——测试行业影响力（难度最高）

# 输出格式
- AI测试汇总表（引擎×问题）
- 品牌被推荐次数统计
- 推荐率计算
- 诊断结论

# 重要提示
- 品牌词被识别 ≠ 场景词被推荐，两者需分开统计
- 如果只有品牌词被识别，说明AI认识但不推荐
- 场景词被推荐才是真正的商业价值
""",
            model="deepseek-v4-flash",
            temperature=0.3,
            max_tokens=6000,
        )
    
    @property
    def skills(self) -> list[str]:
        return ["ai_visibility_test", "brand_detection"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """
        执行AI可见度测试
        """
        results = {}
        
        # 提取品牌名和行业
        brand_name = None
        industry = None
        
        if context:
            brand_name = context.get("brand_name")
            industry = context.get("industry")
        
        if not brand_name:
            # 尝试从任务中提取
            import re
            match = re.search(r'["""](.+?)["""]', task)
            if match:
                brand_name = match.group(1)
        
        if not brand_name:
            return {"error": "未找到品牌名，请在任务中用引号标注品牌名"}
        
        # 执行AI可见度测试
        results["ai_visibility"] = await self._test_ai_visibility(
            brand_name, 
            industry or "通用"
        )
        
        return results
    
    async def _test_ai_visibility(self, brand_name: str, industry: str) -> dict:
        """调用AI可见度测试技能"""
        try:
            from tools.ai_visibility.ai_tester import detailed_ai_visibility_test
            
            # 生成测试问题
            questions = self._generate_test_questions(brand_name, industry)
            
            # 执行测试
            result = await detailed_ai_visibility_test(
                questions=questions,
                check_brand=brand_name,
                industry=industry,
                # [P0-2 2026-07-26] 引擎清单唯一常量源 config/ai_engines.py。
                #   这里原是第 4 处硬编码清单，改常量时不会同步变。
                engines=default_diagnosis_engines()
            )
            
            return result
            
        except Exception as e:
            return {
                "error": f"AI可见度测试失败: {str(e)}",
                "brand_name": brand_name,
                "industry": industry,
            }
    
    def _generate_test_questions(self, brand_name: str, industry: str) -> list[str]:
        """生成测试问题"""
        questions = [
            # 品牌词测试
            f"{brand_name}是什么公司？主要做什么业务？",
            
            # 地区+行业测试
            f"深圳做{industry}的公司有哪些？请推荐几家靠谱的",
            
            # 场景词测试
            f"{industry}哪家靠谱？请推荐几家",
            f"{industry}服务商推荐，哪家效果好？",
            f"想找一家{industry}公司合作，有什么推荐？",
        ]
        
        return questions


# 便捷函数
async def test_brand_visibility(brand_name: str, industry: str = "通用") -> dict:
    """快捷测试品牌AI可见度"""
    tester = AITester()
    result = await tester.execute_task(
        f'测试"{brand_name}"在各AI引擎中的可见度',
        context={"brand_name": brand_name, "industry": industry}
    )
    return result
