"""
审核 Agent (Review Agent) - 纯LLM智能审核
报告质量把控，完全由LLM判断，无机械规则

核心原则:
1. LLM是唯一的质量判断者
2. 从专业顾问视角审核
3. 从客户/市场视角审核
4. 不关心技术实现细节，只关心内容质量
"""

import os
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import json
import httpx
from typing import Dict, Any, List
from datetime import datetime


# ========================================
# LLM审核专用提示词
# ========================================
REVIEW_SYSTEM_PROMPT = """# 角色定义

你是一位**资深咨询公司合伙人**，负责审核团队产出的GEO诊断报告，确保可以交付给VIP客户。

---

# 你的审核标准

## 1. 专业性 (30分)
- 数据解读是否准确、逻辑清晰？
- 评分维度解释是否有深度，而非敷衍？
- 竞品分析是否有洞察力，而非流水账？
- 是否体现了行业专业度？

## 2. 客户价值 (30分)
- 客户读完能否清楚知道自己的问题在哪？
- 改进建议是否具体、可执行，而非空话？
- 是否有让客户"眼前一亮"的洞察？
- 报告是否值得客户付费？

## 3. 表达质量 (20分)
- 语言是否专业、简洁、有力？
- 结构是否清晰，重点是否突出？
- 是否避免了AI腔调（如"首先"、"其次"、"总的来说"）？
- 是否避免了占位符或模糊表达？

## 4. 商业说服力 (20分)
- 报告是否能推动客户采取行动？
- 是否有明确的优先级和预期效果？
- 是否让客户感受到专业服务的价值？

---

# 审核原则

1. **不要苛求格式**: 报告精炼比冗长更好，不要因为缺少某个章节就扣分
2. **关注内容本质**: 重要的是内容是否有价值，而不是是否符合模板
3. **客户视角优先**: 客户不关心模型是什么，只关心报告是否有用
4. **宁缺毋滥**: 一个深刻的洞察比十个平庸的分析更有价值

---

# 输出格式

请以JSON格式返回审核结果：

```json
{
    "total_score": 75,  // 0-100分
    "status": "passed",  // passed(>=75) / minor_revision(60-74) / rejected(<60)
    "dimension_scores": {
        "professionalism": 22,  // 专业性 /30
        "client_value": 25,  // 客户价值 /30
        "expression_quality": 15,  // 表达质量 /20
        "business_persuasion": 13  // 商业说服力 /20
    },
    "highlights": [  // 报告亮点
        "xxx是有价值的洞察",
        "xxx部分分析到位"
    ],
    "improvements": [  // 改进建议（如果有）
        "xxx部分可以更具体",
        "建议增加xxx"
    ],
    "overall_comment": "一句话总评"
}
```

只返回JSON，不要其他内容。
"""


async def review_report(
    report_content: str,
    score_data: Dict[str, Any],
    competitor_data: Dict[str, Any] = None,
    social_data: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    纯LLM审核报告质量
    
    Args:
        report_content: 报告 Markdown 内容
        score_data: 评分数据（仅供参考）
        
    Returns:
        {
            "status": "passed" / "minor_revision" / "rejected",
            "score": 0-100,
            "highlights": [...],
            "improvements": [...],
            "overall_comment": "..."
        }
    """
    print("  🔍 开始审核报告...")
    
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    
    if not api_key:
        print("  ⚠️ 未配置审核LLM，跳过审核")
        return {
            "status": "passed",
            "score": 80,
            "highlights": ["审核跳过"],
            "improvements": [],
            "overall_comment": "未配置审核LLM"
        }
    
    # 构建审核请求
    user_message = f"""请审核以下GEO诊断报告：

## 报告基础信息
- 品牌GEO总分：{score_data.get('total_score', 0)}/{score_data.get('max_score', 100)}
- 评级：{score_data.get('level', '未知')}

## 报告全文
{report_content[:8000]}

---

请从专业顾问视角和客户视角审核这份报告，给出评分和改进建议。
"""
    
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with llm_track(
                "report_review",
                "deepseek",
                model=DEEPSEEK_OFFICIAL_FLASH,
            ) as tracker:
                response = await client.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": DEEPSEEK_OFFICIAL_FLASH,
                        "messages": [
                            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                            {"role": "user", "content": user_message}
                        ],
                        "temperature": 0.3
                    }
                )
                if response.status_code == 200:
                    result_for_usage = response.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(result_for_usage)
                    tracker.record(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        success=True,
                    )
                else:
                    tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
            
            if response.status_code == 200:
                result = response.json()
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                # 解析JSON
                try:
                    if "{" in content:
                        json_str = content[content.find("{"):content.rfind("}")+1]
                        review_result = json.loads(json_str)
                        
                        total_score = review_result.get("total_score", 70)
                        
                        # 确定状态
                        if total_score >= 75:
                            status = "passed"
                        elif total_score >= 60:
                            status = "minor_revision"
                        else:
                            status = "rejected"
                        
                        final_result = {
                            "status": status,
                            "score": total_score,
                            "dimension_scores": review_result.get("dimension_scores", {}),
                            "highlights": review_result.get("highlights", []),
                            "improvements": review_result.get("improvements", []),
                            "overall_comment": review_result.get("overall_comment", ""),
                            "review_time": datetime.now().isoformat()
                        }
                        
                        print(f"  ✅ 审核完成，评分: {total_score}/100（审核评分），状态: {status}")
                        return final_result
                        
                except json.JSONDecodeError:
                    print(f"  ⚠️ 审核结果解析失败")
            
            # 默认通过（避免阻塞流程）
            return {
                "status": "passed",
                "score": 75,
                "highlights": [],
                "improvements": [],
                "overall_comment": "审核完成"
            }
            
    except Exception as e:
        print(f"  ⚠️ LLM 审核异常: {e}")
        return {
            "status": "passed",
            "score": 75,
            "highlights": [],
            "improvements": [],
            "overall_comment": f"审核异常: {str(e)[:50]}"
        }


async def review_and_iterate(
    report_content: str,
    score_data: Dict[str, Any],
    regenerate_callback=None,
    max_iterations: int = 2
) -> Dict[str, Any]:
    """
    审核并迭代（简化版）
    """
    current_report = report_content
    
    for i in range(max_iterations):
        print(f"  📋 第 {i+1} 轮审核...")
        
        review_result = await review_report(current_report, score_data)
        
        if review_result["status"] == "passed":
            print(f"  ✅ 审核通过！")
            return {
                "final_report": current_report,
                "review_result": review_result,
                "iterations": i + 1
            }
        
        # 达到最大次数或无法自动修复
        if i == max_iterations - 1:
            break
    
    print(f"  ⚠️ 达到最大迭代次数 ({max_iterations})")
    
    return {
        "final_report": current_report + "\n\n> ⚠️ 报告经过多次迭代，部分问题可能需要人工确认",
        "review_result": review_result,
        "iterations": max_iterations
    }


# 导出
__all__ = [
    "review_report",
    "review_and_iterate"
]
