"""
法务专员
负责合同起草、法律咨询、法规查询
"""

import os
import sys
from typing import Optional, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employees.base_employee import BaseEmployee


class LegalAdvisor(BaseEmployee):
    """
    ⚖️ 法务专员
    
    职责：
    1. 起草和审核各类商业合同
    2. 提供法律咨询和风险评估
    3. 联网查询最新法律法规
    4. 处理知识产权和合规事务
    
    技能：
    - contract_drafting: 合同起草
    - legal_consultation: 法律咨询
    - law_search: 法规查询（联网）
    - risk_assessment: 风险评估
    """
    
    def __init__(self):
        super().__init__(
            employee_id="legal_advisor",
            name="⚖️ 法务专员",
            department="support",
            sys_prompt="""# 角色定义
你是一位资深法务专员，具备丰富的商业法律知识和实务经验。

# 核心职责
1. **合同起草与审核**
   - 起草各类商业合同（服务合同、合作协议、保密协议等）
   - 审核合同条款，识别风险点
   - 提供修改建议和风险提示

2. **法律咨询**
   - 解答日常法律问题
   - 分析法律风险
   - 提供合规建议

3. **法规查询**
   - 查阅最新法律法规
   - 关注政策变化
   - 提供法规解读

# 擅长领域
- 合同法与商事法
- 知识产权法（商标、著作权）
- 劳动法与人事合规
- 数据保护与隐私法规
- 广告法与消费者权益
- 电子商务法

# 合同起草原则
1. **权责清晰**：明确双方权利义务
2. **风险可控**：设置违约责任和救济措施
3. **条款完整**：涵盖所有必要条款
4. **语言规范**：使用标准法律术语
5. **公平合理**：平衡双方利益

# 合同标准结构
```
1. 首部（合同名称、编号、签订日期）
2. 当事人信息
3. 鉴于条款（背景说明）
4. 定义条款
5. 服务/合作内容
6. 权利与义务
7. 费用与支付
8. 保密条款
9. 知识产权
10. 违约责任
11. 争议解决
12. 附则
13. 签章
```

# 输出格式要求
- 合同使用规范法律文书格式
- 法律咨询使用结构化回答
- 标注法律依据和出处
- 提供风险提示（使用⚠️标注）

# 免责声明
在输出法律建议时，适当提醒用户：
"本建议仅供参考，重大法律事务建议咨询专业律师。"
""",
            model="qwen3.7-max",  # 使用Qwen模型，擅长中文法律文书
            temperature=0.3,  # 法律文书需要准确性
            max_tokens=10000,  # 合同可能较长
        )
    
    @property
    def skills(self) -> list[str]:
        return ["contract_drafting", "legal_consultation", "law_search", "risk_assessment"]
    
    async def _execute_skills(
        self, 
        task: str, 
        context: Optional[dict]
    ) -> dict[str, Any]:
        """执行法务技能"""
        results = {}
        task_lower = task.lower()
        
        # 合同起草
        if any(kw in task for kw in ["合同", "协议", "契约", "条款"]):
            results["contract_drafting"] = True
        
        # 法律咨询
        if any(kw in task for kw in ["法律", "咨询", "合规", "风险"]):
            results["legal_consultation"] = True
        
        # 法规查询 - 需要联网
        if any(kw in task for kw in ["法规", "法条", "规定", "政策", "最新"]):
            results["law_search"] = await self._search_laws(task)
        
        # 风险评估
        if any(kw in task for kw in ["审核", "评估", "风险", "问题"]):
            results["risk_assessment"] = True
        
        return results
    
    async def _search_laws(self, query: str) -> dict:
        """联网查询法律法规"""
        try:
            from tools.search.metaso_mcp import metaso_search
            # 添加法律关键词优化搜索
            search_query = f"{query} 法律法规 最新版"
            result = await metaso_search(search_query)
            return result
        except Exception as e:
            return {"error": f"法规查询失败: {str(e)}", "query": query}
    
    async def draft_contract(
        self, 
        contract_type: str, 
        parties: dict,
        terms: Optional[dict] = None
    ) -> str:
        """
        起草合同便捷方法
        
        Args:
            contract_type: 合同类型（如"服务合同"、"合作协议"）
            parties: 双方信息 {"甲方": {...}, "乙方": {...}}
            terms: 特殊条款
        
        Returns:
            合同文本
        """
        prompt = f"""请起草一份{contract_type}。

当事人信息：
{parties}

特殊要求：
{terms if terms else "无特殊要求，按标准模板起草"}

请按照标准合同格式输出完整合同文本。
"""
        result = await self.execute_task(prompt, {"contract_type": contract_type})
        return result.get("result", "")
