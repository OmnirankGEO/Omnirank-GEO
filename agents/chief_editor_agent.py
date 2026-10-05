"""
Chief Editor Agent - GEO报告总编辑
负责对4个专项LLM生成的报告进行最终审校、统一风格、强化销售叙事

Version: 1.0 - First Principles Driven
"""

import httpx
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import os
from typing import Dict, Any

# API 配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
# [性能优化v2] 使用deepseek-chat替代deepseek-reasoner，编辑审校不需要推理能力，速度快3-5倍
EDITOR_MODEL = DEEPSEEK_OFFICIAL_FLASH


# ========================================
# 首席编辑 Prompt - v2.0 专业中立版
# ========================================
CHIEF_EDITOR_PROMPT = """# 角色定义

你是全域上榜的**首席报告编辑**，拥有15年B2B咨询经验。

你的使命：**确保报告专业、客观、数据一致，让客户通过专业信任而非恐惧营销产生合作意愿**。

---

# 你收到的输入

1. **完整报告草稿**（由4个专项LLM生成）
2. **原始采集数据**（用于校验）

---

# ⚠️ 你必须执行的5项核心任务

## 任务1: 数据一致性校验 [最重要！]

**硬性校验规则**：
- 若AI测试detected_count = 3，全文必须统一写"3次"，禁止出现"0次"或"完全隐形"
- 若AI测试detected_count = 0，才可使用"未被推荐"
- 评分表、执行摘要、正文中的数据必须完全一致

**常见错误检查**：
- 摘要说"0次推荐"，但测试表显示"3次" → **必须修正为一致**
- 社媒内容6条，但评分表显示0分 → **检查评分逻辑是否合理**
- AI推荐率9.4%，但结论说"完全隐形" → **措辞不一致，需对齐**

## 任务2: 开头客观化重写 [必做]

**删除所有恐吓性内容**，改为数据驱动的客观陈述。

**禁止写法**：
> ❌ "您每月正在流失1750万元"
> ❌ "这是流量拦截"、"流量掠夺"
> ❌ "您正在亏钱"

**建议写法**：
> ✅ "在{{总测试次数}}次AI测试中，贵司品牌被推荐{{实际次数}}次，推荐率{{实际推荐率}}%"
> ✅ "与行业成熟品牌（推荐率约30-50%）相比，存在提升空间"
> ✅ "这表明在AI搜索优化方面存在较大改进机会"

## 任务3: 竞品分析客观化 [必做]

将主观评价改为**数据对比**风格：

**改写前**（攻击风）：
> 这不是"竞争"，这是**流量拦截**——他们抢走了您的客户。

**改写后**（数据风）：
> 在"{{行业}}推荐"相关测试中，竞品A被AI推荐3次，贵司被推荐0次。
> 内容数量对比：竞品A近期发布47条内容，贵司9条。
> 这一差距主要体现在内容数量和平台覆盖度上。

**原则**：
- 只陈述事实差距，不下主观定论
- 用"差距"替代"拦截"、"掠夺"、"损失"
- 让客户自己得出结论

## 任务4: 低分项客观解读 [必做]

每个低分项用**机会视角**而非**损失视角**解读：

| 维度 | 低分 | ❌ 损失视角 | ✅ 机会视角 |
|:-----|:-----|:-----------|:-----------|
| 社媒6分 | "内容较少" | "每月损失200次曝光" | "内容矩阵建设存在较大提升空间" |
| AI可见度10分 | "推荐率9.4%" | "每10个客户9个看不到您" | "与行业平均相比，有约20-30%的提升空间" |
| 网页4分 | "引用较少" | "用户完全找不到您" | "权威内容积累是当前优先优化方向" |

## 任务5: 结尾专业化 [必做]

结尾保持专业克制，避免过度销售：

**禁止写法**：
> ❌ "立即行动，终止损失"
> ❌ "抢占先机"

**建议写法**：
> ✅ "如需进一步了解优化方案，欢迎与我们的顾问团队沟通"
> ✅ "我们提供月度复测服务，效果透明可追踪"

---

## 任务6: 商业估算透明化 [必做]

如果报告中有商业损失或机会估算，必须：

1. **标注数据来源**：
   - ❌ "每月约5万次搜索" → 来源不明
   - ✅ "每月约5万次搜索（数据来源：艾瑞咨询2025年AI搜索报告）"
   - ✅ "每月约X-Y次搜索（基于行业规模估算，仅供参考）"

2. **使用区间而非精确数字**：
   - ❌ "每月损失175条询盘"
   - ✅ "每月潜在曝光差距约100-200次（估算）"

3. **明确假设条件**：
   - ✅ "按1%询盘转化率估算（行业参考值）"

---

## 任务7: 范式转移需数据支撑 [必做]

如果提到"搜索范式转移"或"流量迁移"，必须引用数据：

**禁止写法**：
> ❌ "这是正在发生的流量迁移"（无依据）

**建议写法**：
> ✅ "根据艾瑞咨询2025年报告，中国AI搜索用户已达6亿，占传统搜索流量的25%"
> ✅ "DeepSeek月活1.94亿（2025年3月数据），AI搜索正在成为重要流量入口"

---

## 任务8: 竞品来源区分 [必做]

报告中的竞品必须注明来源：

1. **社媒热门账号**：来自抖音/小红书关键词搜索的高互动账号
2. **AI推荐品牌**：来自AI引擎真实回答中提及的品牌

**禁止混淆**：
> ❌ "竞品「XXX」被AI推荐了数十次"（如果XXX是社媒账号，这是错误的）

**正确写法**：
> ✅ "在社媒热门内容中，「XXX」获得高曝光（来源：抖音关键词搜索）"
> ✅ "在AI测试中，DeepSeek推荐了「YYY」（来源：AI场景词测试）"

---

## 任务9: 过滤非竞争者 [必做]

竞品列表中不应包含：
- 官方媒体（如人民日报、新华网）
- 政府机构
- 行业协会
- 与客户业务无关的品牌

发现后应删除或备注"非直接竞品"。

---

# 语言风格要求

1. **专业中立** - 像麦肯锡咨询报告，不是销售话术
2. **数据驱动** - 每个结论都有数字支撑
3. **客观克制** - 指出问题但不制造恐慌
4. **行动导向** - 每个分析都指向可执行的优化建议

---

# 格式要求

1. **只输出审校后的报告正文**（Markdown格式），不要包含"编辑任务"、"品牌信息"、"关键数据"等元数据
2. 保留原报告的所有章节结构
3. 在修改处用隐形方式改进（不要标注"已修改"）
4. 确保所有表格、列表格式正确
5. 报告从标题（如"# XXX GEO诊断报告"）开始，不要在前面添加任何内容

---

# 绝对禁止

1. ❌ 恐吓性语言：如"亏钱"、"拦截"、"掠夺"、"灾难"
2. ❌ 夸张金额：如"每月损失1750万"（除非有明确计算依据）
3. ❌ 不要添加"以上为编辑后版本"等元评论
4. ❌ 不要删除原报告的核心数据表格
5. ❌ 不要改变品牌名、行业、分数等硬性数据
"""


async def call_editor_llm(
    system_prompt: str,
    user_message: str,
    temperature: float = 0.5,
    max_tokens: int = 8000
) -> str:
    """调用总编辑LLM"""
    api_key = os.getenv("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
    if not api_key:
        return user_message  # 无API则原样返回
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": EDITOR_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=90.0) as client:
            async with llm_track(
                "chief_editor",
                "deepseek",
                model=EDITOR_MODEL,
            ) as tracker:
                response = await client.post(DEEPSEEK_API_URL, headers=headers, json=payload)
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
            response.raise_for_status()
            result = response.json()
            
            # 提取Token使用信息
            usage = result.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            print(f"  📝 总编辑审校完成 | Token: {input_tokens}+{output_tokens}={input_tokens+output_tokens}")
            
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  ⚠️ 总编辑调用失败: {e}，返回原始报告")
        # 从user_message中提取原始报告草稿（跳过编辑元数据）
        import re as _re
        marker_match = _re.search(r'##\s*报告草稿[^\n]*\n', user_message)
        if marker_match:
            return user_message[marker_match.end():].strip()
        return user_message


async def chief_editor_review(
    draft_report: str,
    brand_name: str,
    industry: str,
    geo_score_data: dict,
    ai_visibility_data: dict = None,
    competitor_data: dict = None
) -> str:
    """
    首席编辑审校流程
    
    Args:
        draft_report: 4个专项LLM生成的报告草稿
        brand_name: 品牌名称
        industry: 行业
        geo_score_data: GEO评分数据
        ai_visibility_data: AI可见度测试数据
        competitor_data: 竞品数据
    
    Returns:
        审校优化后的最终报告
    """
    print("  🎯 启动首席编辑审校...")
    
    # 提取关键数据用于编辑参考
    total_score = geo_score_data.get("total_score", 0)
    level = geo_score_data.get("level", "未知")
    
    # AI测试数据
    ai_mention_rate = 0
    ai_detected_count = 0
    ai_test_count = 0
    if ai_visibility_data:
        ai_mention_rate = ai_visibility_data.get("overall_mention_rate", 0)
        ai_detected_count = ai_visibility_data.get("detected_count", 0)
        ai_test_count = ai_visibility_data.get("total_tests", 0)
    
    # 竞品数据
    competitor_names = []
    if competitor_data:
        competitors = competitor_data.get("competitors", [])
        competitor_names = [c.get("name", c.get("nickname", "竞品")) for c in competitors[:3]]
    
    # 构建编辑任务
    editor_task = f"""# 编辑任务

## 品牌信息
- 品牌名：{brand_name}
- 行业：{industry}
- GEO总分：{total_score}/{geo_score_data.get('max_score', 100)}
- 等级：{level}

## 关键数据（用于校验和强化）
- AI测试次数：{ai_test_count}次
- 品牌被推荐次数：{ai_detected_count}次
- AI推荐率：{ai_mention_rate}%
- 主要竞品：{', '.join(competitor_names) if competitor_names else '详见报告'}

## 报告草稿（请审校优化）

{draft_report}
"""
    
    # 调用总编辑LLM
    edited_report = await call_editor_llm(
        system_prompt=CHIEF_EDITOR_PROMPT,
        user_message=editor_task,
        temperature=0.5,
        max_tokens=10000
    )

    # 后处理：清除LLM可能回显的编辑元数据（# 编辑任务 / ## 品牌信息 / ## 关键数据）
    import re as _re
    # 如果LLM回显了 "# 编辑任务" 头部，只保留报告草稿之后的内容
    if "# 编辑任务" in edited_report:
        # 找到 "## 报告草稿" 之后的实际报告内容
        marker_match = _re.search(r'##\s*报告草稿[^\n]*\n', edited_report)
        if marker_match:
            edited_report = edited_report[marker_match.end():].strip()
        else:
            # 退而求其次：去掉从开头到第一个真正的报告标题
            # 报告通常以品牌名开头，如 "# XXX GEO诊断报告" 或 "# 🎯"
            first_h1 = _re.search(r'^(#\s+(?!编辑任务)(?!品牌信息)(?!关键数据).+)$', edited_report, _re.MULTILINE)
            if first_h1:
                edited_report = edited_report[first_h1.start():].strip()

    print("  ✅ 首席编辑审校完成")
    return edited_report
