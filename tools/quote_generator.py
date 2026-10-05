import httpx
import os
import asyncio
from datetime import datetime
from typing import Dict, Any


def generate_report_id(diagnosis_id: int = None) -> str:
    """
    生成报告编号
    格式: GEO-YYYYMMDD-XXXX
    """
    date_str = datetime.now().strftime("%Y%m%d")
    if diagnosis_id:
        return f"GEO-{date_str}-{diagnosis_id:04d}"
    else:
        timestamp = int(datetime.now().timestamp()) % 10000
        return f"GEO-{date_str}-{timestamp:04d}"


# ========================================
# 销售攻略 LLM Prompt
# ========================================
SALES_STRATEGY_SYSTEM_PROMPT = """# 角色

你是一位经验丰富的销售教练，正在为销售人员撰写**针对特定客户的攻略建议**。

---

# 核心原则

1. **说人话**：像朋友聊天一样自然，不要用"尊敬的客户"、"贵司"这类官腔
2. **利他思维**：真心为客户好，帮他解决问题，而不是"卖东西"
3. **有理有据**：每个建议都要有数据支撑，用诊断报告的数据说话
4. **不留销售痕迹**：不要用"促单"、"逼单"这类词，要让客户感觉是在帮他
5. **价格不降**：绝对不能承诺降价，但可以给福利、送服务

---

# 销售可动用的"特权福利"

销售可以根据情况承诺以下福利（但不能降价）：

| 福利 | 适用场景 | 话术参考 |
|:-----|:---------|:---------|
| 首周免费策略复盘 | 建立信任 | "签约后第一周我亲自给您做一次深度策略复盘，帮您理清接下来怎么做" |
| 前10篇优先发权威媒体 | 快速见效 | "前10篇我帮您安排发到权威媒体，让您先看到效果" |
| 赠送3篇知乎深度回答 | 高价值感知 | "我再送您3篇知乎回答，这个对Kimi引用特别有用" |
| 免费品牌词监控 | 增加粘性 | "签约后我帮您监控品牌词，有竞品抢占第一时间通知您" |
| 首月不满意可换套餐 | 降低顾虑 | "第一个月如果觉得量不对，随时可以调整套餐，没有任何额外费用" |
| 签年框送1个月 | 锚定年付 | "签一年的话相当于85折，再送您一个月，等于全年13个月服务" |
| 优先响应特权 | 大客户 | "您这边我亲自对接，有问题随时微信我，不用走工单" |

---

# 攻防策略

## 进攻（推动成交）

1. **数据冲击**：用诊断报告的数据让客户意识到问题严重性
2. **竞品刺激**：告诉他竞品已经在做了，不做会被甩开
3. **时间紧迫**：AI模型每季度更新，早做3个月=领先1年
4. **降低门槛**：先从入门版试，有效果再加量

## 防御（处理拒绝）

| 客户说 | 真正顾虑 | 应对思路 |
|:-------|:---------|:---------|
| "太贵了" | 价值感知不足 | 强调单价低（50元/篇），对比市场价（100-200） |
| "效果怎么保障" | 怕花冤枉钱 | 承诺每月AI测试报告，数据说话 |
| "再考虑考虑" | 没紧迫感 | 强调时间窗口，告诉他竞品动态 |
| "你们做过我们行业吗" | 怕不专业 | 强调GEO原理通用，可提供案例 |
| "我领导定" | 不是决策人 | 帮他准备给领导看的材料 |

---

# 输出格式

用以下结构输出，语气要像是销售主管在群里分享经验：

## 🎯 这个客户怎么聊

### 他是谁
（一句话描述客户画像）

### 最痛的点
（从诊断数据中找出最该攻的点，用数据说话）

### 怎么开场
（自然的开场白，不要太销售）

### 怎么推套餐
（推荐哪个套餐，为什么，怎么说）

### 他可能会问什么
（预判2-3个问题，每个问题给应对话术）

### 可以给什么福利
（根据客户情况，建议适合送的福利，最多2-3个）

### 如果他犹豫怎么办
（1-2招推动策略）

---

# 禁止事项

1. 禁止承诺降价
2. 禁止使用"促单"、"逼单"、"成交"这类词
3. 禁止生硬的官方话术
4. 禁止过度承诺效果（如"保证第一名"）
5. 禁止贬低竞品
"""



def find_lowest_dimension(dimension_scores: Dict[str, int]) -> tuple:
    """找到最低分维度"""
    if not dimension_scores:
        return ("ai_visibility_score", 0)
    lowest_key = min(dimension_scores, key=lambda k: dimension_scores.get(k, 0))
    return (lowest_key, dimension_scores.get(lowest_key, 0))


async def generate_sales_strategy_with_llm(
    brand_name: str,
    industry: str,
    industry_category: str,
    total_score: int,
    level: str,
    dimension_scores: Dict[str, int],
    ai_visibility_data: Dict = None,
    competitor_data: Dict = None
) -> str:
    """
    使用LLM生成自然的销售攻略
    """
    # 构建用户输入
    user_message = f"""
# 客户信息

- **品牌名**：{brand_name}
- **行业**：{industry}
- **行业大类**：{industry_category}
- **GEO总分**：{total_score}/100
- **等级**：{level}

## 7维度得分（v4.0评分体系）

| 维度 | 得分 | 满分 | 状态 |
|:-----|:-----|:-----|:-----|
| AI引擎可见度 | {dimension_scores.get('ai_engine_score', 0)} | 25 | {'❌' if dimension_scores.get('ai_engine_score', 0) < 10 else '⚠️'} |
| 社媒内容资产 | {dimension_scores.get('social_media_score', 0)} | 20 | {'❌' if dimension_scores.get('social_media_score', 0) < 8 else '⚠️'} |
| 网页内容资产 | {dimension_scores.get('web_content_score', 0)} | 18 | {'❌' if dimension_scores.get('web_content_score', 0) < 7 else '⚠️'} |
| 权威背书 | {dimension_scores.get('authority_score', 0)} | 15 | {'❌' if dimension_scores.get('authority_score', 0) < 5 else '⚠️'} |
| 结构化内容 | {dimension_scores.get('structured_content_score', 0)} | 12 | {'❌' if dimension_scores.get('structured_content_score', 0) < 4 else '⚠️'} |
| 内容质量 | {dimension_scores.get('content_quality_score', 0)} | 5 | {'❌' if dimension_scores.get('content_quality_score', 0) < 2 else '⚠️'} |
| 品牌基础 | {dimension_scores.get('brand_foundation_score', 0)} | 5 | {'❌' if dimension_scores.get('brand_foundation_score', 0) < 2 else '⚠️'} |

## 套餐选项

| 套餐 | 月发布量 | 月费 | 适合 |
|:-----|:---------|:-----|:-----|
| 入门版 | 80篇 | ￥5,800 | 小众行业/测试 |
| 标准版 | 150篇 | ￥9,800 | 大多数客户 ⭐ |
| 专业版 | 300篇 | ￥18,800 | 竞争激烈行业 |
| 霸榜版 | 500篇+ | 面议 | 头部企业 |

请为销售人员生成针对这个客户的攻略建议。
"""

    # 添加AI测试数据（如果有）
    if ai_visibility_data:
        mention_rate = ai_visibility_data.get('overall_mention_rate', 0)
        user_message += f"""
## AI引擎测试结果

- 品牌被AI提及率：{mention_rate}%
- 测试问题数：{ai_visibility_data.get('total_tests', 0)}个
- 被提及的引擎数：{ai_visibility_data.get('engines_mentioned', 0)}/3个

{'⚠️ 注意：AI完全不认识这个品牌！' if mention_rate == 0 else ''}
"""

    # 添加竞品数据（如果有）
    if competitor_data:
        competitors = competitor_data.get('competitors', [])[:3]
        if competitors:
            user_message += "\n## 主要竞品\n"
            for c in competitors:
                user_message += f"- {c.get('name', '未知')}（{c.get('platform', '未知')}）\n"

    # 调用LLM (切换至DashScope)
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return "[LLM未配置] 请设置 DASHSCOPE_API_KEY"
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            from tools.llm_call_tracker import llm_track, usage_from_response_payload

            async with httpx.AsyncClient(timeout=60.0) as client:
                async with llm_track(
                    "quote_sales_strategy",
                    "dashscope",
                    model="qwen3.7-max",
                    metadata={"attempt": attempt + 1},
                ) as tracker:
                    response = await client.post(
                        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": "qwen3.7-max",
                            "messages": [
                                {"role": "system", "content": SALES_STRATEGY_SYSTEM_PROMPT},
                                {"role": "user", "content": user_message}
                            ],
                            "temperature": 0.7,
                            "max_tokens": 2000
                        }
                    )
                    if response.status_code == 200:
                        data_for_usage = response.json()
                        input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data_for_usage)
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
                return result["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"[LLM调用失败] 重试3次无效: {str(e)}。切换至兜底策略。")
                # 兜底：使用简单策略
                return generate_sales_strategy_simple(
                    brand_name,
                    industry,
                    total_score,
                    level,
                    find_lowest_dimension(dimension_scores)[0]
                )
            print(f"LLM调用失败，正在重试 ({attempt+1}/{max_retries})... Error: {e}")
            await asyncio.sleep(2)


# 同步版本（用于快速测试）
def generate_sales_strategy_simple(
    brand_name: str,
    industry: str,
    total_score: int,
    level: str,
    lowest_dimension: str
) -> str:
    """简化版销售攻略（不调用LLM）"""
    
    # 维度中文名 [v4.0 七维度]
    dim_names = {
        "ai_engine_score": "AI引擎可见度",
        "social_media_score": "社媒内容资产",
        "web_content_score": "网页内容资产",
        "authority_score": "权威背书",
        "structured_content_score": "结构化内容",
        "content_quality_score": "内容质量",
        "brand_foundation_score": "品牌基础"
    }
    dim_name = dim_names.get(lowest_dimension, "AI可见度")
    
    # 推荐套餐
    if total_score < 30:
        package = "标准版"
        reason = "分数比较低，需要一定的量才能见效"
    elif total_score < 50:
        package = "标准版"
        reason = "有一定基础，标准版可以稳步提升"
    else:
        package = "入门版"
        reason = "基础不错，入门版维护即可"
    
    return f"""# 🎯 {brand_name} 销售攻略

> 仅供内部参考，口语化表达

---

## 他是谁

{industry}行业，目前GEO{total_score}分（{level}），在AI搜索里基本没存在感。

---

## 最痛的点

**{dim_name}**得分最低。

简单说就是：客户问AI"推荐{industry}服务商"的时候，AI推荐的是别人，不是他。这个点可以重点打。

---

## 推荐套餐

**{package}**（{reason}）

话术参考：
> "您这个情况，我建议从{package}开始。如果效果好，后面再加量；如果觉得不对，第一个月随时可以调。"

---

## 如果他说"太贵了"

> "其实算下来每篇才50块，市场上同类服务100-200一篇。我们用AI降成本，把利润让给您了。"

---

## 如果他说"效果怎么保障"

> "我们每个月会给您一份AI引擎测试报告，就是拿5个问题去问AI，看看有几次推荐您。数据说话，效果一目了然。"

---

## 如果他犹豫

> "这样，我帮您申请一个福利：第一周我亲自给您做一次深度策略复盘，帮您理清接下来怎么做。签约的话这个复盘免费送。"

---

## 可以送的福利

1. 首周免费策略复盘（推荐）
2. 前10篇优先发权威媒体
3. 首月不满意可换套餐

---

*生成日期：{datetime.now().strftime("%Y-%m-%d")}*
"""



# 测试
if __name__ == "__main__":
    import asyncio
    
    # 测试简化版攻略
    strategy = generate_sales_strategy_simple(
        brand_name="驰鲸科技",
        industry="TikTok代运营",
        total_score=18,
        level="空白",
        lowest_dimension="ai_visibility_score"
    )
    print("=== 攻略测试 ===")
    print(strategy)

