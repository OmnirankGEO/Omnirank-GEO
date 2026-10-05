"""
LLM-Based GEO Scoring Agent
使用LLM综合评估8个GEO维度，替代机械式if-else规则
"""

import json
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import httpx
import os
from typing import Any

# DeepSeek API配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = DEEPSEEK_OFFICIAL_FLASH  # 官方DeepSeek API使用 deepseek-chat


# LLM评分Prompt模板
GEO_SCORING_PROMPT = """# 角色定义
你是GEO（生成式引擎优化）评分专家。你的任务是根据采集的真实数据，客观评估品牌在AI搜索时代的可见度与被推荐潜力。

# 核心原则
1. **真实性优先**：评分必须反映品牌的真实表现
2. **证据驱动**：每个维度的得分必须基于具体数据证据
3. **整体判断**：综合考虑所有数据，识别矛盾和异常
4. **智能校验**：识别数据采集可能的不完整性，进行合理修正
5. **时效性意识**：关注近期（6个月内）的内容表现

# ⏰ 时间上下文（重要！）
- **当前日期**：{current_date}
- **有效数据窗口**：近6个月内的内容更有参考价值
- **时效性提示**：如果发现引用的产品/事件明显过时（超过1年），可能是数据采集问题

# 品牌信息
- 品牌名称：{brand_name}
- 所属行业：{industry}

# ⭐ 第一步：品牌知名度识别（非常重要！）

请首先判断这个品牌的真实知名度等级：

| 等级 | 特征 | 典型品牌示例 |
|:-----|:-----|:-------------|
| 🏆 国际知名 | 全球性品牌，拥有大规模用户群 | 苹果、三星、小米、华为、OPPO、vivo、特斯拉、宝马、耐克 |
| 🥇 国内头部 | 国内行业领导者，广泛知名 | 比亚迪、蔚来、格力、美的、海尔、滴滴、美团、字节跳动 |
| 🥈 行业知名 | 细分领域有影响力 | 大疆、科大讯飞、商汤、寒武纪 |
| 🥉 成长品牌 | 正在成长中，知名度有限 | 新兴创业公司 |
| ⬜ 新品牌 | 刚起步，知名度很低 | 初创公司 |

**关键判断**：根据你对"{brand_name}"的常识性了解，它属于哪个知名度等级？

# 采集数据摘要

## AI引擎测试结果
- 测试引擎：dashscope(千问)、deepseek、kimi、doubao(豆包)
- 有效测试次数：{ai_test_count}次（{ai_engines}个引擎 × {ai_questions}个问题，API失败的测试已排除）
- 品牌被推荐次数：{ai_mention_count}次
- 推荐率：{ai_rate}%（基于有效测试计算）
- ⚠️ 注意：如果测试次数少于预期（{ai_engines}×{ai_questions}），说明部分API调用失败，已自动排除。请基于有效测试结果评分。
- 各引擎详情：
{ai_engine_details}
- 负面提及：{negative_mentions}次

### ⭐ AI测试详细结果
{ai_dimension_details}

### 📋 逐题检测明细
{ai_detail_table}

### 🔍 问题类型识别指南（LLM请先分类再评分！）

**请先将测试问题分为三类：**

| 问题类型 | 识别特征 | 示例 | GEO价值 |
|:---------|:---------|:-----|:--------|
| **品牌词问题** | 问题中直接包含品牌名称 | "XX公司是什么？"、"XX是什么公司" | ⚠️ 极低 |
| **地区+行业词问题** | 包含城市名+行业，不含品牌名 | "上海豪车租赁推荐"、"深圳GEO服务商" | 🔶 中高 |
| **超一级词问题** | 全国通用搜索，不含地区和品牌 | "豪车租赁哪家靠谱"、"GEO公司排名TOP5" | 🔴 最高 |

⚠️ **检测精度重要提醒**：
- 品牌词直查（如"XX公司是什么公司"）返回的是公开数据库信息，不代表真正GEO可见度，权重较低。
- **真正的GEO可见度**体现在竞争性问题中AI主动推荐品牌，因此场景词和地区词权重较高。

**评分计算步骤：**

1. 将每个问题分类到上述三类之一
2. 按类型计算各引擎的检测率：
   - 品牌词检测率 = 品牌词问题中检测到的次数 / 品牌词问题总测试次数
   - 地区词检测率 = 地区词问题中检测到的次数 / 地区词问题总测试次数
   - 超一级词检测率 = 超一级词问题中检测到的次数 / 超一级词问题总测试次数
3. 应用权重计算最终得分：

   **综合质量 = 品牌词检测率×15% + 地区词检测率×55% + 超一级词检测率×30%**

   **AI引擎可见度得分 = 25 × 综合质量**

**示例计算：**
- 品牌词问题4次测试(1问×4引擎)，检测到4次 → 检测率100%
- 地区词问题20次测试(5问×4引擎)，检测到16次 → 检测率80%
- 超一级词问题8次测试(2问×4引擎)，检测到2次 → 检测率25%
- 综合质量 = 1.0×15% + 0.80×55% + 0.25×30% = 0.15 + 0.44 + 0.075 = 0.665
- AI引擎可见度 = 25 × 0.665 = 16.6 ≈ 17分

## 社交媒体数据

### ⭐ 品牌自有内容汇总（评分关键数据！）
- **品牌自有内容总数：{total_brand_content}条**（抖音{brand_douyin_count}条 + 小红书{brand_xhs_count}条）
- 覆盖平台数量：{brand_platform_count}个

### 抖音
- 搜索到相关视频：{douyin_total}条
- 品牌自有内容：{brand_douyin_count}条
- 最高点赞：{max_douyin_likes}，平均互动：{avg_douyin_engagement}

### 小红书  
- 搜索到相关笔记：{xhs_total}条
- 品牌自有内容：{brand_xhs_count}条
- 最高点赞：{max_xhs_likes}，平均互动：{avg_xhs_engagement}

## 网页搜索数据
- 搜索结果总数：{web_result_count}条
- 品牌直接引用：{brand_direct_count}条
- 权威来源引用：{authority_count}条
- 学术论文：{scholar_count}篇
- 文库文档：{document_count}篇

## 竞品情况
- 识别竞品数量：{competitor_count}个
- 品牌在热门内容中的占比：{brand_content_ratio}%

# 评分维度与规则（v4.0 - 基于AI检索行为设计）

> **设计原理**：不同AI引擎有不同数据偏好
> - 豆包：偏好头条系（抖音、今日头条）
> - Kimi：偏好网页、知乎、公众号
> - DeepSeek：综合网页+社媒

| 维度 | 满分 | 评分依据 | AI检索关联 |
|:-----|:----:|:---------|:-----------|
| 🤖 AI引擎可见度 | 25 | LLM测试中品牌被推荐的频率 | 直接验证最终效果 |
| 📱 社媒内容资产 | 20 | 抖音/小红书/头条内容布局 | 豆包/DeepSeek主要检索源 |
| 🌐 网页内容资产 | 18 | 网页搜索中的信息丰富度 | Kimi/秘塔主要检索源 |
| 🏛️ 权威背书 | 15 | 百科/媒体报道/行业奖项 | 增强AI引用可信度 |
| 💡 结构化内容 | 12 | FAQ/长文/知识库完整性 | AI易引用的格式 |
| � 内容质量 | 5 | 互动率/专业度综合评估 | 内容可信度信号 |
| 🏷️ 品牌基础 | 5 | 品牌词占有+更新频率 | 基础认知信号 |

**总分 = 25 + 20 + 18 + 15 + 12 + 5 + 5 = 100分**

---

## 🤖 AI引擎可见度（满分25分）评分规则

**请严格使用加权公式计算，不要使用扣分法！**

计算公式：**AI引擎可见度 = 25 × (品牌词检测率×15% + 地区词检测率×55% + 超一级词检测率×30%)**

各维度权重设计原理：

| 测试类型 | 权重 | 满分贡献 | 原因 |
|:---------|:----:|:--------:|:-----|
| 地区+行业词 | **55%** | 13.75分 | 最核心！本地行业搜索是大多数企业的主要获客场景 |
| 超一级词/场景词 | **30%** | 7.5分 | 加分项！能打上超一级词的公司不多，不应过度惩罚 |
| 品牌词 | **15%** | 3.75分 | 基础认知！品牌词被识别是可见度的基本门槛 |

**诊断话术**：
- 超一级词搜不到 → "在竞争性行业问题中AI完全不推荐您的品牌，这是GEO优化的核心目标"
- 地区词搜不到 → "在区域+行业搜索中未被AI提及，本地市场影响力缺失"
- 品牌词搜不到 → "AI对品牌缺乏基础认知（但品牌词直查权重低，不是主要问题）"
- 仅品牌词搜到 → "只在直接搜索品牌名时被找到（公开注册信息），在竞争性问题中没有存在感"

---

## 📱 社媒内容资产（满分20分）评分规则

> [!CRITICAL] **硬性规则：品牌自有内容为0条时，此维度必须得0分！**
> 这是最重要的评分原则，无论其他条件如何，0条内容=0分。

**评分计算方式**（严格按品牌自有内容**总数** = 抖音+小红书合计）：

| 品牌自有内容**总数** | 基础分 | 说明 |
|:----------------|:------:|:-----|
| 0条 | **0分** | 无内容=无资产，严格0分 |
| 1-2条 | 4分 | 刚起步 |
| 3-5条 | 8分 | 有基础 |
| 6-10条 | 12分 | 较活跃 |
| 11-20条 | 16分 | 活跃 |
| 20条以上 | 20分 | 优秀 |

⚠️ 重要：品牌自有内容总数 = 上方「品牌自有内容汇总」中的总数，不是单平台数量！

**附加条件**（仅在有内容时适用）：
- 内容互动率低于行业平均：-2分
- 仅单平台有内容：-2分

**诊断话术**：
- 0条品牌内容 → "品牌在抖音/小红书完全没有自有内容，AI无法检索到任何品牌信息"
- 无行业占位 → "在行业热门内容中缺乏存在感"

---

## 🌐 网页内容资产（满分18分）评分规则

> [!CRITICAL] **硬性规则：品牌直接引用为0条时，此维度必须得0分！**

**评分计算方式**（严格按品牌直接引用数量）：

| 品牌直接引用数量 | 基础分 | 说明 |
|:----------------|:------:|:-----|
| 0条 | **0分** | 无引用=无资产，严格0分 |
| 1-2条 | 6分 | 刚起步 |
| 3-5条 | 10分 | 有基础 |
| 6-10条 | 15分 | 良好 |
| 10条以上 | 18分 | 优秀 |

**诊断话术**：
- 品牌词搜不到 → "Kimi等AI从网页检索时找不到您的品牌"
- 地区词搜不到 → "区域缺口：本地市场信息缺失"

---

## 🏛️ 权威背书（满分15分）评分标准

| 表现 | 得分区间 |
|:-----|:--------:|
| 有百科词条+媒体报道+行业奖项 | 12-15分 |
| 有百科词条+少量媒体提及 | 8-11分 |
| 仅有百科词条或少量提及 | 4-7分 |
| 无任何权威来源 | 0-3分 |

---

## 💡 结构化内容（满分12分）评分标准

| 表现 | 得分区间 |
|:-----|:--------:|
| 有FAQ+长文+知识库，AI引用友好 | 9-12分 |
| 有长文或FAQ，结构较清晰 | 5-8分 |
| 仅有零散短内容 | 2-4分 |
| 无结构化内容 | 0-1分 |

---

## 📝 内容质量（满分5分）评分标准

| 表现 | 得分区间 |
|:-----|:--------:|
| 高互动率+专业深度内容 | 4-5分 |
| 中等互动，内容较专业 | 2-3分 |
| 低互动或内容粗糙 | 0-1分 |

---

## 🏷️ 品牌基础（满分5分）评分标准

| 表现 | 得分区间 |
|:-----|:--------:|
| 品牌词搜索占位好+近期持续更新 | 4-5分 |
| 品牌词有一定占位或有更新 | 2-3分 |
| 品牌词占位差且长期无更新 | 0-1分 |

---

# ⭐ 第二步：数据采集完整性检验与分数调整

对于**国际知名/国内头部/行业知名**的品牌，如果以下情况出现，说明数据采集可能不完整：

| 维度 | 异常情况 | 真实情况推断 | 建议调整 |
|:-----|:---------|:-------------|:---------|
| 🏛️ 权威背书 | 采集到0条权威引用 | 知名品牌必然被主流媒体报道过 | 根据品牌知名度给予合理分数(10-15) |
| 💡 结构化内容 | 无百科/长文内容 | 知名品牌通常有完善的百科词条 | 根据品牌知名度给予合理分数(8-12) |
| 📱 社媒内容资产 | 品牌账号内容少 | 知名品牌通常有官方账号运营 | 若总内容多但品牌内容少,考虑账号识别遗漏 |
| 🌐 网页内容资产 | 与AI推荐率严重不符 | 可能是搜索词/采集范围限制 | 参考AI推荐率适当调整 |

**调整原则**：
- 只对明确为知名品牌且数据明显异常的维度进行调整
- 调整幅度应合理,不超过该维度满分的80%
- 必须在输出中说明调整原因

# 评分输出要求

请输出严格的JSON格式（不要包含markdown代码块标记）：

{{
    "brand_recognition": {{
        "level": "国际知名/国内头部/行业知名/成长品牌/新品牌",
        "reasoning": "判断依据"
    }},
    "ai_test_analysis": {{
        "question_classification": [
            {{"question": "问题内容", "type": "品牌词/地区词/超一级词"}}
        ],
        "detection_rates": {{
            "brand_questions": {{"detected": N, "total": N, "rate": N}},
            "regional_questions": {{"detected": N, "total": N, "rate": N}},
            "super_tier1_questions": {{"detected": N, "total": N, "rate": N}}
        }},
        "weighted_score": N,
        "calculation": "计算过程说明"
    }},
    "data_completeness_check": {{
        "has_collection_gaps": true/false,
        "gap_dimensions": ["维度1", "维度2"],
        "adjustments_made": [
            {{"dimension": "维度名", "original_score": N, "adjusted_score": N, "reason": "调整原因"}}
        ]
    }},
    "scores": {{
        "ai_engine_score": N,
        "social_media_score": N,
        "web_content_score": N,
        "authority_score": N,
        "structured_content_score": N,
        "content_quality_score": N,
        "brand_foundation_score": N
    }},
    "total_score": N,
    "level": "空白/起步/成长/成熟/领先",
    "key_evidence": [
        {{"dimension": "维度名", "score": N, "reason": "给出此分的依据"}}
    ],
    "deductions": [
        {{"reason": "扣分原因", "points": N}}
    ],
    "data_anomaly": false,
    "anomaly_reason": "",
    "overall_assessment": "一句话总体评价"
}}

# 等级标准
- 0-20分：空白
- 21-40分：起步
- 41-60分：成长
- 61-80分：成熟
- 81-100分：领先

# 重要提醒
1. **先识别品牌知名度**，这决定了后续评分的基准线
2. 对于知名品牌，采集数据异常低的维度应考虑是数据采集问题而非品牌问题
3. 调整必须有明确理由，不能无根据抬高分数
4. 对于新品牌/成长品牌，严格按照采集数据评分，不做调整"""


def summarize_data(
    brand_name: str,
    industry: str,
    douyin_data: dict = None,
    xiaohongshu_data: dict = None,
    web_search_data: dict = None,
    ai_visibility_data: dict = None,
    brand_content_stats: dict = None,
    competitor_data: dict = None
) -> dict:
    """将原始数据转换为prompt需要的摘要格式"""
    
    if douyin_data is None:
        douyin_data = {}
    if xiaohongshu_data is None:
        xiaohongshu_data = {}
    if web_search_data is None:
        web_search_data = {}
    if ai_visibility_data is None:
        ai_visibility_data = {}
    if brand_content_stats is None:
        brand_content_stats = {}
    if competitor_data is None:
        competitor_data = {}
    
    # AI测试数据
    ai_results = ai_visibility_data.get("results", [])
    # 使用正确的字段名
    ai_test_count = ai_visibility_data.get("total_tests", len(ai_results) * 5)
    # 使用detected_count而非total_mentions
    ai_mention_count = ai_visibility_data.get("detected_count", 0)
    # 直接使用已计算的mention_rate
    ai_rate = ai_visibility_data.get("overall_mention_rate", 0)
    ai_engines = len(ai_results) if ai_results else 3
    ai_questions = ai_visibility_data.get("total_questions", 8)  # 默认8问
    
    # [NEW] 分维度AI测试统计
    dimension_stats = ai_visibility_data.get("dimension_stats", {})
    if not dimension_stats:
        # 尝试从detail_table推断维度统计
        detail_table = ai_visibility_data.get("detail_table", [])
        question_types = ai_visibility_data.get("question_types", {})
        
        if detail_table and question_types:
            # 按维度统计
            dimension_stats = {
                "brand_awareness": {"total": 0, "detected": 0},
                "regional_industry": {"total": 0, "detected": 0},
                "super_tier1": {"total": 0, "detected": 0}
            }
            for item in detail_table:
                q = item.get("question", "")
                q_type = question_types.get(q, "super_tier1")
                if q_type in dimension_stats:
                    # 统计有效引擎数（排除API失败）
                    has_valid = False
                    detected_any = False
                    for eng_result in item.get("results", {}).values():
                        if isinstance(eng_result, dict):
                            answer = eng_result.get("answer_summary", "")
                            if "查询失败" in answer or answer.startswith("Error"):
                                continue
                            has_valid = True
                            if eng_result.get("brand_detected"):
                                detected_any = True
                    if has_valid:
                        dimension_stats[q_type]["total"] += 1
                        if detected_any:
                            dimension_stats[q_type]["detected"] += 1

    
    # 如果没有直接的detected_count，尝试从engine_stats计算
    if ai_mention_count == 0:
        engine_stats = ai_visibility_data.get("engine_stats", {})
        for engine, stats in engine_stats.items():
            ai_mention_count += stats.get("detected", 0)
    
    # 如果还是没有ai_rate，重新计算
    if ai_rate == 0 and ai_test_count > 0 and ai_mention_count > 0:
        ai_rate = round(ai_mention_count / ai_test_count * 100, 1)
    
    # 各引擎详情
    engine_details = []
    negative_mentions = 0
    
    # 优先使用engine_stats
    engine_stats = ai_visibility_data.get("engine_stats", {})
    if engine_stats:
        for engine, stats in engine_stats.items():
            detected = stats.get("detected", 0)
            total = stats.get("total", 5)
            rate = stats.get("rate", 0)
            status = "✅" if detected > 0 else "❌"
            engine_details.append(f"  - {engine}: {status} ({detected}/{total}次推荐, {rate}%)")
    else:
        # 兼容旧格式
        for r in ai_results:
            engine = r.get("engine", "unknown")
            detected = r.get("brand_detected", False)
            mentions = r.get("mentions", 0)
            if isinstance(mentions, list):
                mentions = len(mentions)
            status = "✅" if detected else "❌"
            engine_details.append(f"  - {engine}: {status} ({mentions}次提及)")
            negative_mentions += r.get("negative_mentions", 0)
    
    # 抖音数据
    douyin_videos = douyin_data.get("top20", []) or douyin_data.get("videos", [])
    douyin_total = len(douyin_videos)
    brand_douyin_count = brand_content_stats.get("douyin_content_count", 0)
    
    max_douyin_likes = 0
    total_douyin_engagement = 0
    for v in douyin_videos:
        stats = v.get("stats", {})
        likes = stats.get("digg", 0) or v.get("like_count", 0)
        max_douyin_likes = max(max_douyin_likes, likes)
        total_douyin_engagement += likes
    avg_douyin_engagement = total_douyin_engagement // max(douyin_total, 1)
    
    # 小红书数据
    xhs_notes = xiaohongshu_data.get("top20", []) or xiaohongshu_data.get("notes", [])
    xhs_total = len(xhs_notes)
    brand_xhs_count = brand_content_stats.get("xiaohongshu_content_count", 0)
    
    max_xhs_likes = 0
    total_xhs_engagement = 0
    for n in xhs_notes:
        likes = n.get("like_count", 0) or n.get("liked_count", 0)
        max_xhs_likes = max(max_xhs_likes, likes)
        total_xhs_engagement += likes
    avg_xhs_engagement = total_xhs_engagement // max(xhs_total, 1)
    
    # 网页搜索数据
    web_result_count = web_search_data.get("result_count", 0)
    brand_direct_count = web_search_data.get("brand_direct_count", 0)
    authority_count = len(web_search_data.get("authority_sources", []))
    scholar_count = web_search_data.get("scholar_count", 0)
    document_count = web_search_data.get("document_count", 0)
    
    # 竞品数据
    competitors = competitor_data.get("competitors", [])
    competitor_count = len(competitors)
    brand_content_ratio = brand_content_stats.get("brand_content_ratio", 0)
    
    # 当前日期
    from datetime import datetime
    current_date = datetime.now().strftime("%Y年%m月%d日")
    
    return {
        "brand_name": brand_name,
        "industry": industry,
        "current_date": current_date,  # 添加当前日期
        "ai_test_count": ai_test_count,
        "ai_engines": ai_engines,
        "ai_questions": ai_questions,
        "ai_mention_count": ai_mention_count,
        "ai_rate": ai_rate,
        "ai_engine_details": "\n".join(engine_details) if engine_details else "  - 无AI测试数据",
        "ai_dimension_details": _format_dimension_stats(dimension_stats),
        "negative_mentions": negative_mentions,
        "douyin_total": douyin_total,
        "brand_douyin_count": brand_douyin_count,
        "max_douyin_likes": max_douyin_likes,
        "avg_douyin_engagement": avg_douyin_engagement,
        "xhs_total": xhs_total,
        "brand_xhs_count": brand_xhs_count,
        "max_xhs_likes": max_xhs_likes,
        "avg_xhs_engagement": avg_xhs_engagement,
        "web_result_count": web_result_count,
        "brand_direct_count": brand_direct_count,
        "authority_count": authority_count,
        "scholar_count": scholar_count,
        "document_count": document_count,
        "competitor_count": competitor_count,
        "brand_content_ratio": brand_content_ratio,
        "total_brand_content": brand_douyin_count + brand_xhs_count,
        "brand_platform_count": (1 if brand_douyin_count > 0 else 0) + (1 if brand_xhs_count > 0 else 0),
        "ai_detail_table": _format_detail_table(ai_visibility_data)
    }


def _format_dimension_stats(dimension_stats: dict) -> str:
    """格式化三维度AI测试统计"""
    if not dimension_stats:
        return "无分维度统计数据"

    lines = []
    dim_labels = {
        "brand_awareness": "🎯 品牌词",
        "regional_industry": "📍 地区+行业词",
        "super_tier1": "🔥 超一级词"
    }

    for dim_key, label in dim_labels.items():
        stats = dimension_stats.get(dim_key, {})
        total = stats.get("total", 0)
        detected = stats.get("detected", 0)
        if total > 0:
            rate = round(detected / total * 100, 1)
            status = "✅" if detected > 0 else "❌"
            lines.append(f"- {label}：{detected}/{total}次推荐 ({rate}%) {status}")
        else:
            lines.append(f"- {label}：未测试")

    return "\n".join(lines)


def _format_detail_table(ai_visibility_data: dict) -> str:
    """格式化逐题检测明细表，供LLM评分参考"""
    detail_table = ai_visibility_data.get("detail_table", [])
    question_types = ai_visibility_data.get("question_types", {})
    engines = ai_visibility_data.get("engines_tested", ["dashscope", "deepseek", "doubao", "yuanbao"])

    if not detail_table:
        return "无逐题明细数据"

    type_labels = {
        "brand_awareness": "品牌词",
        "regional_industry": "地区词",
        "super_tier1": "超一级词"
    }

    header = "| 问题 | 类型 | " + " | ".join(engines) + " |"
    separator = "|:-----|:-----" + "".join("|:----:" for _ in engines) + "|"
    lines = [header, separator]

    for item in detail_table:
        q = item.get("question", "")
        q_type = question_types.get(q, "super_tier1")
        type_label = type_labels.get(q_type, "未知")

        results = item.get("results", {})
        engine_cells = []
        for engine in engines:
            eng_result = results.get(engine, {})
            if isinstance(eng_result, dict):
                answer = eng_result.get("answer_summary", "")
                if "查询失败" in answer or answer.startswith("Error"):
                    engine_cells.append("⚠️失败")
                elif eng_result.get("brand_detected"):
                    engine_cells.append("✅检测到")
                else:
                    engine_cells.append("❌未检测")
            else:
                engine_cells.append("-")

        q_display = q if len(q) <= 25 else q[:22] + "..."
        lines.append(f"| {q_display} | {type_label} | {' | '.join(engine_cells)} |")

    return "\n".join(lines)


async def call_llm_for_scoring(prompt: str) -> dict:
    """调用LLM进行评分"""
    from tools.llm_call_tracker import llm_track, usage_from_response_payload

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with llm_track("llm_geo_scorer", "deepseek", model=MODEL) as tracker:
            response = await client.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": "你是一个专业的GEO评分专家，严格按照要求输出JSON格式的评分结果。"},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000
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
        response.raise_for_status()
        result = response.json()
        content = result["choices"][0]["message"]["content"]
        
        # 清理可能的markdown代码块标记
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        
        return json.loads(content)


async def calculate_geo_score_llm(
    brand_name: str,
    industry: str,
    douyin_data: dict = None,
    xiaohongshu_data: dict = None,
    web_search_data: dict = None,
    ai_visibility_data: dict = None,
    brand_content_stats: dict = None,
    competitor_data: dict = None
) -> dict:
    """
    使用LLM综合评估8个GEO维度
    
    Returns:
        dict: 包含scores, total_score, level等评分结果
    """
    # 1. 生成数据摘要
    summary = summarize_data(
        brand_name=brand_name,
        industry=industry,
        douyin_data=douyin_data,
        xiaohongshu_data=xiaohongshu_data,
        web_search_data=web_search_data,
        ai_visibility_data=ai_visibility_data,
        brand_content_stats=brand_content_stats,
        competitor_data=competitor_data
    )
    
    # 2. 填充prompt
    prompt = GEO_SCORING_PROMPT.format(**summary)
    
    # 3. 调用LLM
    try:
        result = await call_llm_for_scoring(prompt)
        
        # 确保必要字段存在
        if "scores" not in result:
            raise ValueError("LLM返回结果缺少scores字段")
        
        # 计算总分（如果LLM没算对）
        scores = result["scores"]
        calculated_total = sum(scores.values())
        result["total_score"] = calculated_total
        
        # 确定等级
        if calculated_total >= 81:
            result["level"] = "领先"
        elif calculated_total >= 61:
            result["level"] = "成熟"
        elif calculated_total >= 41:
            result["level"] = "成长"
        elif calculated_total >= 21:
            result["level"] = "起步"
        else:
            result["level"] = "空白"
        
        # 添加维度详情（v4.0 新7维度）
        result["dimension_scores"] = scores
        result["dimension_details"] = {
            "ai_engine_score": {"score": scores.get("ai_engine_score", 0), "max_score": 25, "description": "🤖 AI引擎可见度"},
            "social_media_score": {"score": scores.get("social_media_score", 0), "max_score": 20, "description": "📱 社媒内容资产"},
            "web_content_score": {"score": scores.get("web_content_score", 0), "max_score": 18, "description": "🌐 网页内容资产"},
            "authority_score": {"score": scores.get("authority_score", 0), "max_score": 15, "description": "🏛️ 权威背书"},
            "structured_content_score": {"score": scores.get("structured_content_score", 0), "max_score": 12, "description": "💡 结构化内容"},
            "content_quality_score": {"score": scores.get("content_quality_score", 0), "max_score": 5, "description": "📝 内容质量"},
            "brand_foundation_score": {"score": scores.get("brand_foundation_score", 0), "max_score": 5, "description": "🏷️ 品牌基础"}
        }
        result["brand"] = brand_name
        
        print(f"  ✅ LLM评分完成: {calculated_total}/100 ({result['level']})")
        if result.get("data_anomaly"):
            print(f"  ⚠️ 数据异常: {result.get('anomaly_reason', '')}")
        
        return result
        
    except Exception as e:
        print(f"  ❌ LLM评分失败: {e}，使用备用机械评分")
        # 失败时回退到机械评分
        from tools.scoring.geo_scorer import calculate_geo_score
        fallback_result = await calculate_geo_score(
            douyin_data=douyin_data,
            xiaohongshu_data=xiaohongshu_data,
            web_search_data=web_search_data,
            ai_visibility_data=ai_visibility_data,
            brand_name=brand_name,
            brand_content_stats=brand_content_stats
        )
        # 解析ToolResponse
        import json
        content = fallback_result.content[0]["text"]
        return json.loads(content)
