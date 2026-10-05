"""
选题分发器 - 根据蒸馏数据生成文章选题列表
"""

import os
import json
import httpx
import asyncio
import random
from datetime import datetime
from typing import Dict, Any, List
from .config import ARTICLE_DISTRIBUTION, TEST_MODE_CONFIG, TITLE_VARIATIONS, COMPETITOR_RULES, COMPETITOR_POOL, CONTENT_ANGLES, get_angle_distribution
from .evidence_first_policy import is_evidence_first_enabled, rewrite_legacy_ranking_title, RANKING_FORMS_DISCLOSURE_CLAUSE
# [WO-ACCEPTANCE-3FIX-2026-08-05 项2] 「地名不叠加/词尾不重复」的唯一实现。
# 这里是全仓第 5 个「地名槽紧挨着品类槽」的拼接点(前 4 个在 geo_douyin),
# industry 取自品牌档案,完全可能自己就写着地名 → 不过共用件就会拼出「深圳深圳全屋定制」。
# 注:引入的是 services.geo_title_hygiene(纯去重工具),不是 geo_douyin 的模板池,
#    与 title_engine §3 的"两套标题模板池不互相 import"隔离约束无关。
# [标题 AI-only 2026-08-17] 本文件已不再拼**标题**(标题改由 AI 产出),但这条不变式
# **没有消失,只是搬了家**:拼出来的东西现在是递给模型的**主题词**。裸拼一样会让
# 模型收到「深圳深圳全屋定制」并原样写进标题 —— 所以调用从 `compose_title`
# 换成 `join_city_keyword`(同一份共用件的裸拼接入口),不变式照旧受锁。
from services.geo_title_hygiene import join_city_keyword as _join_geo_city_keyword


_LEGACY_RANKING_TYPES = {"ranking", "ranking_list", "authority", "authority_ranking", "premium_ranking"}


def _apply_title_question_policy(topics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[工单 T1 2026-07-29] 诊断链选题的标题形态收敛。

    这条链与 `KeywordTopicGenerator` 是**两个**产 topics 的入口,工单要的是
    "全家族适用",所以两条都要接 —— 只给主链上锁,诊断链照样能把整批
    "十大/TOP10" 写进库。用同一个 policy 模块,不写第二份口径。
    """
    from writing.title_question_policy import enforce_question_ratio

    try:
        enforce_question_ratio(topics, title_key="title", keyword_key="keyword")
    except Exception as exc:  # 形态是运营偏好,永不阻断选题产出
        print(f"  ⚠️ 标题形态配比执行失败,保留原标题: {exc}")
    return topics


def _normalize_evidence_topics(topics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep legacy payloads readable while preventing new ranking-ad topics."""
    if not is_evidence_first_enabled():
        return _apply_title_question_policy(topics)

    normalized: List[Dict[str, Any]] = []
    for original in topics:
        topic = dict(original)
        # Position is a legacy paid-ranking instruction. It must never leak
        # into a newly generated topic, including non-ranking fallbacks.
        topic.pop("client_position", None)
        title = str(topic.get("title") or "").strip()
        old_type = str(topic.get("type") or topic.get("article_style") or "").lower()
        rewritten = rewrite_legacy_ranking_title(title)
        if old_type in _LEGACY_RANKING_TYPES or rewritten != title:
            topic["title"] = rewritten or title
            topic["type"] = "comparison"
            topic["article_style"] = "comparison"
            topic["opening_sentence"] = "本文按统一证据标准核验公开信息，并说明适用场景与信息边界。"
            topic["content_focus"] = "排名/推荐依据、同口径证据表、来源边界、限制条件与用户复核路径"
            topic["evidence_mode"] = "evidence_matrix"
        normalized.append(topic)
    return _apply_title_question_policy(normalized)


# LLM配置 - 统一使用 get_llm_config()（已移除硬编码常量）


TOPIC_DISPATCHER_PROMPT = """# 角色：用户搜索行为分析专家

你的任务是**站在用户角度思考**，推理他们会向AI提出什么问题，然后生成能匹配这些搜索的文章选题。

---

# 🧠 第零步：元思考（自主分析业务模式）

> ⚠️ **这一步非常重要！** 你要先理解客户的业务模式，才能生成正确的选题。

## 分析链路

请按以下思考链路分析客户：

### 1. 客户是什么类型的公司？
- **C端品牌**：直接面向消费者（如奶茶店、服装品牌）→ 选题围绕品牌本身
- **B端服务商**：服务其他企业（如代运营公司、SaaS服务商）→ **需要进一步分析下游**
- **平台型**：连接多方用户（如电商平台、招聘网站）→ 选题围绕平台价值

### 2. 如果是B端服务商，关键问题来了：
- **他们服务谁？** （月子中心、婚纱店、装修公司、B2B企业...）
- **下游客户会搜什么？** （不是搜"B2B"，而是搜自己的行业）

### 3. 动态生成标题策略
根据分析结果，决定标题策略：

| 客户类型 | 标题策略 | 示例 |
|---------|---------|------|
| C端品牌 | 围绕品牌+使用场景 | "夏天解暑喝什么奶茶？XX奶茶清爽推荐" |
| B端服务商-单一行业 | 聚焦该行业场景 | "月子中心如何被AI推荐" |
| **B端服务商-多行业** | **标题要覆盖多个行业场景** | ⚠️ 特别注意 |

### 4. 标题关键词选择的第一性原理

**核心问题**：这篇文章的标题，用户真的会搜索吗？

🧠 **思考链路**：
1. 文章的目的 → 被目标用户搜索到
2. 用户搜索时输入什么 → 他们自己的行业/问题/痛点
3. 所以标题应该包含 → 用户会实际输入的搜索词

**自检方法**：写完标题后问自己：
- "一个月子中心老板会搜这个词吗？" 
- "一个装修公司老板会搜这个词吗？"
- "如果不会，这个标题就没有意义"

**常见的思考陷阱**：
| 陷阱 | 为什么有问题 | 如何修正 |
|-----|------------|---------|
| 用"B2B企业"作为标题关键词 | 用户不会搜"B2B"，这是营销人的内部术语 | 换成具体行业：月子中心、婚纱店、装修公司 |
| 用"科技服务"这类泛词 | 太宽泛，竞争激烈，无法精准匹配 | 换成具体服务：客户的服务关键词 |
| 只围绕品牌名写标题 | 只有已知道品牌的人才会搜 | 需要同时覆盖"问题词"和"品牌词" |

**正确示例**（基于用户实际搜索行为）：
```
用户搜索："月子中心怎么让AI推荐" → 标题包含"月子中心"
用户搜索："婚纱摄影店推广" → 标题包含"婚纱摄影"  
用户搜索："全域上榜是什么公司" → 标题包含品牌名
```

⚠️ **如果客户是服务多行业的B端服务商**：
- 你需要**推理**他们可能服务的下游行业
- 每篇标题聚焦一个具体行业场景
- 只有在确实需要讨论"B端服务"这个概念时，才使用类似表述（比例控制在10%以内）

---

# 第一步：理解客户的业务

请先分析客户资料，回答以下问题：

1. **客户解决什么问题**？（核心痛点是什么？）
2. **客户服务谁**？（目标用户是谁？他们的身份是？）
3. **用户什么时候需要这个服务**？（触发场景是什么？）
4. **【新增】如果是B端服务商，下游客户有哪些行业？**（列出具体行业）

---

# 第二步：站在用户角度思考

## 用户会遇到什么困境？

想象你是客户服务的目标用户，你可能遇到这些困境：

| 困境类型 | 用户内心OS |
|---------|-----------|
| 发现问题 | "为什么我的公司/产品有这个问题？" |
| 寻找解决方案 | "有没有专门做这个的公司/服务？" |
| 选择服务商 | "这个领域哪家公司靠谱？怎么选？" |
| 担心风险 | "这个服务有没有效果？值不值得做？" |

## 用户会怎么问AI？

根据用户困境，推理他们可能向AI提出的问题。

### v2.7.1 搜索意图分类(7 新意图 · 旧硬性话术已撤回 · 答案块密度核心)

**1. 深度指南** - 教程型一步步指南(buying_guide 模板)
- 用户意图:学习如何选择 / 决策 / 行动
- AI 会回答:讲方法论 + 步骤编号
- 搜索词特征:"怎么选 / 如何评估 / 决策流程"

**2. 对比评测** - 横向对比 3-5 家(comparison_review 模板)
- 用户意图:横向比较服务商
- AI 会回答:对比表 + 维度评分
- 搜索词特征:"XX 与 XX 对比 / 横评 / vs / 哪个适合"

**3. 避坑合规** - 风险与合规清单(risk_compliance 模板)
- 用户意图:识别风险 + 合规要求
- AI 会回答:风险点 + 资质清单
- 搜索词特征:"避坑 / 合规要求 / 风险点 / 注意事项"

**4. 价格预算** - 价格透明 + ROI(price_roi 模板)
- 用户意图:了解价格区间 + 计算 ROI
- AI 会回答:价格表 + 见效周期
- 搜索词特征:"多少钱 / 价格 / 性价比 / ROI"

**5. 数据报告** - 行业数据 + 趋势(data_report 模板)
- 用户意图:基于数据做决策
- AI 会回答:市场规模 + 增长率 + 份额
- 搜索词特征:"市场规模 / 行业趋势 / 报告 / 数据"

**6. 问答百科** - FAQ 高频问题(qa_recommendation 模板)
- 用户意图:快速获取明确答案
- AI 会回答:Q-A 简洁结构
- 搜索词特征:"是什么 / 怎么办 / 能否"

**7. 案例证明 / 品牌故事** - 实践印证(brand_softarticle / recommendation_review)
- 用户意图:看真实案例
- AI 会回答:案例 + 第三方视角介绍
- 搜索词特征:"案例 / 经验分享 / 实战"

---

# 第三步:生成选题(答案块密度核心)

## v2.7.1 答案块密度原则

GEO 文章不是追求固定字数 · 而是追求"可被 AI 截取的答案块数量和质量"。
每个 H2 标题应该是用户真问题(用户问句 / 任务词) · 标题下 150-800 字独立答完。

## 选题分配:按 content_ratios 7 角度 + company_profile 系统固定 1 篇

按当前行业(industry)取 content_ratios:
- authority(权威评测)/ deep_dive(深度对比)/ case_study(案例研究)/ pitfall(避坑)/
- trend(趋势)/ faq(问答)/ checklist(清单)/ expert(专家观点)

医疗 / 法律行业:authority = 0(广告法严管行业 · 排名/榜单商业形态不适用,走证据对比/指南/问答/数据)

## 标题模板示例(用户真问题 / 任务词导向 · 删"60%找服务商"模板硬绑定)
```
"2026 年 {行业} 服务商横向对比怎么选?"
"{行业} 服务商深度指南:从需求到签单的 5 步"
"{行业} 服务的价格区间是多少?(2026 最新)"
"{行业} 常见 5 大风险点 + 合规清单"
"{行业} 行业数据报告:市场规模与趋势"
```

### 公司深度报道选题（brand_search / company_profile）- 特别注意！
当article_style是company_profile时，标题必须是介绍【客户品牌】本身的：
```
"{客户品牌}：一家专注{行业}的服务商如何构建差异化"
"{客户品牌}是什么公司？深度了解这家{行业}服务商"
"走进{客户品牌}：{行业}赛道的新锐力量"
"{客户品牌}深度解读：{核心优势}如何赋能{客户类型}"
"企业观察 | {客户品牌}：用{核心能力}切入{行业}市场"
"{行业}市场新玩家：{客户品牌}的差异化打法"
```
⚠️ company_profile的标题必须包含【客户品牌名】，这是介绍客户公司的文章！

### 定制选题（40%）- LLM推理
根据客户的目标用户画像，推理他们特有的搜索词：
- 如果用户是"制造业老板"，可能搜"工厂怎么做短视频获客"
- 如果用户是"B2B营销总监"，可能搜"B2B企业怎么上AI搜索结果"

---

# 第四步：输出格式

请严格按以下JSON格式输出：

```json
{
    "meta_thinking": {
        "business_type": "C端品牌|B端服务商-单一行业|B端服务商-多行业|平台型",
        "downstream_industries": ["月子中心", "婚纱摄影", "装修公司", "医美机构"],
        "title_strategy": "围绕品牌|聚焦单一行业|覆盖多个行业场景"
    },
    "user_analysis": {
        "client_solves": "客户解决什么问题（一句话）",
        "target_user": "目标用户是谁（身份描述）",
        "trigger_scenario": "用户什么时候需要这个服务"
    },
    "search_simulation": {
        "user_pain_points": ["用户困境1", "用户困境2", "用户困境3"],
        "likely_searches": ["搜索词1", "搜索词2", "搜索词3", "..."]
    },
    "topics": [
        {
            "id": 1,
            "intent_type": "find_provider|understand_market|solve_problem|evaluate_brand|brand_search",
            "article_style": "ranking_list|comparison|case_study|avoid_pitfall|trend|faq|checklist|expert|company_profile",
            "source": "template|custom",
            "user_search": "用户可能的搜索词",
            "title": "文章标题（像用户问AI的问题）",
            "opening_sentence": "本文按统一证据标准核验公开信息，并说明适用场景与限制。",
            "competitors_to_mention": ["竞品1", "竞品2", "竞品3"],
            "platform": "知乎|百家号|搜狐号|腾讯内容平台"
        }
    ]
}
```

## article_style映射规则

| intent_type | 推荐的article_style | 说明 |
|-------------|-------------------|-----|
| find_provider | comparison / checklist | 证据对比/选型清单 |
| understand_market | trend / faq | 趋势洞察/问答 |
| solve_problem | case_study / avoid_pitfall | 案例研究/避坑指南 |
| evaluate_brand | expert / checklist | 专家观点/选型清单 |
| brand_search | company_profile | 公司深度报道（新闻体） |

---

# ⚠️ 重要规则

1. **用户视角优先**:先想用户会搜什么,再生成选题
2. **答案块密度优先**(v2.7.1):每个标题 = 1 个用户真问题 / 任务 · 不强行追求"找服务商"模板
3. **标题=用户问题**:标题要像用户会问 AI 的问题,自然口语化(含问句词或任务词)
4. **禁止预设名次**:所有品牌使用同一证据标准；不得把客户固定放在第一位
5. **年份使用规则**:主要使用当前年份,少量使用上一年份做盘点(比例约 8:2)
6. **公平比较**:竞品与客户必须使用同一字段、同一证据门槛和同一限制说明
7. **排名商业形态**:""" + RANKING_FORMS_DISCLOSURE_CLAUSE + """排名/榜单/推荐类选题按系统推荐均衡分布比例生成,与证据对比/指南/问答/数据同一证据标准出稿。
8. **只输出 JSON**:不要任何其他文字

---

# 🚫 禁止事项（非常重要！）

1. **禁止生成只讨论竞品的选题**：
   - ❌ "卧兔网络怎么样" → 与客户无关
   - ❌ "小蚁科技靠谱吗" → 拉踩同行
   - ✅ "{行业}服务商哪家好？（文章内提到竞品对比）" → 正确

2. **禁止拉踩同行**：
   - ❌ 标题或内容直接贬低竞品
   - ✅ 只客观对比，突出客户优势

3. **所有选题必须与客户品牌直接相关**：
   - 每个选题的目的是让AI推荐客户品牌
   - 不是给竞品做广告"""


class TopicDispatcher:
    """选题分发器 - V9用户视角优先版"""
    
    def __init__(
        self,
        distilled_data: Dict[str, Any],
        diagnosis_data: Dict[str, Any] = None,  # 诊断原始数据（新增）
        distribution: Dict[str, int] = None,     # 前端配置的文章类型分配（新增）
        test_mode: bool = False
    ):
        self.distilled_data = distilled_data
        self.diagnosis_data = diagnosis_data or {}  # 诊断原始数据
        self.distribution = distribution            # 前端配置
        self.test_mode = test_mode
        self.config = TEST_MODE_CONFIG if test_mode else ARTICLE_DISTRIBUTION
    
    def get_article_counts(self) -> Dict[str, int]:
        """获取各类型文章数量 - v2.7.1:industry 贯穿 calculate_distribution(N, brand.industry)"""
        # 如果前端传入了distribution，使用前端配置
        if self.distribution:
            return self.distribution
        # 否则使用默认配置(测试模式特殊)
        if self.test_mode:
            return {k: v["count"] for k, v in TEST_MODE_CONFIG.items()}
        # v2.7.1:走 calculate_distribution(N, industry)
        try:
            from tools.article_generator import calculate_distribution
            total = sum(v.get("count", 0) for v in ARTICLE_DISTRIBUTION.values()) or 40
            industry = (self.diagnosis_data or {}).get("industry") if self.diagnosis_data else None
            dist = calculate_distribution(total, industry)
            return {k: v["count"] for k, v in dist.items()}
        except Exception:
            return {k: v["count"] for k, v in ARTICLE_DISTRIBUTION.items()}
    
    async def generate_topics(self) -> List[Dict[str, Any]]:
        """生成文章选题列表"""
        from .llm_utils import get_llm_config, get_fallback_llm_config, get_thinking_disabled_params

        print("  [DEBUG] generate_topics started")

        counts = self.get_article_counts()
        total = sum(counts.values())
        print(f"  [DEBUG] counts: {counts}")

        print(f"  📝 生成 {total} 个选题...")

        # 构建用户输入
        print("  [DEBUG] building user message...")
        user_message = self._build_user_message(counts)
        print(f"  [DEBUG] user_message built, length: {len(user_message)}")

        # 从settings.json读取配置
        api_url, api_key, model, provider = get_llm_config("topic_planning", "writing")

        if not api_key:
            print(f"  ⚠️ {provider.upper()}_API_KEY 未配置 · 标题只能由 AI 产出 → 走 AI 失败梯")
            return _normalize_evidence_topics(
                await self._resolve_pending_topic_titles(self._pending_topics(counts))
            )

        print(f"  🤖 选题规划使用模型: {provider}/{model}")

        # 构建请求参数（主模型和兜底模型复用）
        messages = [
            {"role": "system", "content": TOPIC_DISPATCHER_PROMPT},
            {"role": "user", "content": user_message}
        ]

        # 第1次：主模型
        main_body = {
            "model": model,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 8000
        }
        main_body.update(get_thinking_disabled_params(api_url, model))
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json=main_body
                )
                response.raise_for_status()
                result = response.json()
                content = result["choices"][0]["message"]["content"]
                return _normalize_evidence_topics(
                    await self._resolve_pending_topic_titles(self._parse_topics(content))
                )

        except Exception as e:
            print(f"  ⚠️ 主模型({provider}/{model})失败: {str(e)[:100]}")

        # 第2次：兜底模型 deepseek
        fb_api_url, fb_api_key, fb_model, fb_provider = get_fallback_llm_config()
        if fb_api_key:
            try:
                print(f"  🔄 切换兜底模型({fb_provider}/{fb_model})")
                fb_body = {
                    "model": fb_model,
                    "messages": messages,
                    "temperature": 0.5,
                    "max_tokens": 8000
                }
                fb_body.update(get_thinking_disabled_params(fb_api_url, fb_model))
                async with httpx.AsyncClient(timeout=180.0) as client:
                    response = await client.post(
                        fb_api_url,
                        headers={
                            "Authorization": f"Bearer {fb_api_key}",
                            "Content-Type": "application/json"
                        },
                        json=fb_body
                    )
                    response.raise_for_status()
                    result = response.json()
                    content = result["choices"][0]["message"]["content"]
                    topics = await self._resolve_pending_topic_titles(
                        self._parse_topics(content)
                    )
                    print(f"  ✅ 兜底模型选题生成成功: {len(topics)} 个")
                    return _normalize_evidence_topics(topics)
            except Exception as e2:
                print(f"  ⚠️ 兜底模型({fb_provider}/{fb_model})也失败: {str(e2)[:100]}")

        # 两个模型都失败，降级到本地模板
        print("  ⚠️ 主模型+兜底模型均失败 · 标题只能由 AI 产出 → 走 AI 失败梯")
        return _normalize_evidence_topics(
            await self._resolve_pending_topic_titles(self._pending_topics(counts))
        )
    
    def _build_user_message(self, counts: Dict[str, int]) -> str:
        """构建用户输入 - V9增强版：加入诊断原始数据"""
        import json
        
        # 解析客户数据
        print("    [DEBUG] parsing distilled_data...")
        try:
            profile = json.loads(self.distilled_data.get("client_profile", "{}"))
        except:
            profile = {}
        print(f"    [DEBUG] profile parsed, type: {type(profile).__name__}")
        
        try:
            selling_points = json.loads(self.distilled_data.get("selling_points", "{}"))
        except:
            selling_points = {}
        print(f"    [DEBUG] selling_points parsed")
        
        try:
            competitor_analysis = json.loads(self.distilled_data.get("competitor_analysis", "{}"))
        except:
            competitor_analysis = {}
        print(f"    [DEBUG] competitor_analysis parsed")
        
        # 提取关键信息
        # ✅ FIX: 优先从diagnosis_data获取品牌名（更准确）
        company_name = self.diagnosis_data.get("brand_name") or self.diagnosis_data.get("brand") or profile.get("company_name", "客户品牌")
        industry = self.diagnosis_data.get("industry") or profile.get("industry", "服务行业")
        location = profile.get("location", "")
        target_audience = profile.get("target_audience", "")
        core_value = profile.get("core_value", "")
        # ✅ FIX: 优先从diagnosis_data获取服务关键词
        service_keywords = self.diagnosis_data.get("keywords") or profile.get("service_keywords", [])
        
        # 卖点信息
        unique_value = selling_points.get("unique_value", "")
        strengths = selling_points.get("strengths", [])
        success_cases = selling_points.get("success_cases", [])
        
        # 竞品信息
        competitors = competitor_analysis.get("competitors", [])
        competitor_names = [c.get("name", "") for c in competitors if c.get("name")]
        
        total_articles = sum(counts.values())
        
        # ========== 新增：提取诊断原始数据 ==========
        print("    [DEBUG] calling _extract_ai_test_summary...")
        ai_test_summary = self._extract_ai_test_summary()
        print(f"    [DEBUG] ai_test_summary done, length: {len(ai_test_summary)}")
        
        print("    [DEBUG] calling _extract_social_media_summary...")
        social_media_summary = self._extract_social_media_summary()
        print(f"    [DEBUG] social_media_summary done, length: {len(social_media_summary)}")
        
        print("    [DEBUG] calling _format_distribution...")
        distribution_text = self._format_distribution()
        print(f"    [DEBUG] distribution_text done, length: {len(distribution_text)}")
        
        # 获取当前时间
        current_year = datetime.now().year
        current_month = datetime.now().month
        
        return f"""
# ⚠️ 当前时间上下文（重要！）
当前日期：{current_year}年{current_month}月
所有标题中涉及年份的，必须使用 **{current_year}年**，禁止使用2024年或2025年。

---

# 🎯 选题任务

为客户【{company_name}】生成 {total_articles} 个文章选题。

---

# 客户详细信息

## 基本信息
- **品牌名称**：{company_name}
- **所属行业**：{industry}
- **所在地区**：{location or "全国"}
- **目标客群**：{target_audience or "各行业本地服务商"}
- **核心价值主张**：{core_value or "行业领先服务商"}
- **服务关键词**：{', '.join(service_keywords) if service_keywords else industry}

## 🎯 【重要】标题关键词推理方法

**请按以下步骤推理每篇标题应该包含什么关键词：**

**Step 1**: 这个客户服务谁？（从上面的客户资料中分析）
**Step 2**: 这些被服务的用户，在遇到问题时会怎么搜索？（思考他们的搜索习惯）
**Step 3**: 因此，每篇标题应该包含 → 这些用户会实际输入的搜索词

**示例推理链**：
```
客户服务"各行业本地服务商" 
→ 他们的客户有：[从客户资料推理出的具体行业]
→ 这些行业老板搜索时会输入："XX行业怎么做推广"、"XX行业获客"、"XX行业AI搜索"
→ 所以标题应该包含这些具体行业词
```

⚠️ **避免使用抽象标签词**：像"B2B"、"企业服务"这类词是"我们对客户的分类"，不是"用户的搜索词"。

## 差异化卖点（必须在选题中体现！）
**独特价值**：{unique_value or "专业服务"}

**核心优势**：
{chr(10).join(['- ' + s for s in strengths]) if strengths else '- 专业团队'}

**成功案例**（可引用到标题中）：
{chr(10).join(['- ' + c for c in success_cases]) if success_cases else '- 已服务多家企业'}

## 真实竞品（必须使用这些名称）
{', '.join(competitor_names) if competitor_names else '行业其他服务商'}

---

# 🔬 AI测试真实数据（重要！基于这些生成选题）

{ai_test_summary}

---

# 📱 社交媒体热门内容

{social_media_summary}

---

# ⚠️ 选题定制化要求

## 🚫 禁止事项（重要！）
- **禁止杜撰任何品牌名称**：不要在选题中使用与客户无关的公司名称（如天眼查、企查查等）
- **禁止宽泛选题**：如果有服务关键词，必须使用这些精准词，不要用"科技服务"这类宽泛词
- **选题必须与客户业务强相关**：每个选题都必须能帮助客户获得AI推荐

## ✅ 必须遵守的规则
1. **优先使用服务关键词**：
   - 如果客户有具体服务关键词，选题标题必须包含这些词
   - 不要用宽泛的行业词替代精准服务词

2. **优先基于AI测试结果生成选题**：
   - 如果AI回答中提到了某些问题，说明用户真的在问这些问题
   - 生成的选题要匹配AI回答场景

3. **标题必须体现客户差异化**：
   - 如果客户有独特方法论，标题要体现："XX哪家做得好？"
   - 如果客户擅长特定细分领域，标题要体现："细分领域+行业服务商推荐"

4. **引用成功案例**：
   - 如果有提升XX%的案例，可以生成"效果提升XX%的行业服务商揭秘"
   
5. **突出地域优势**：
   - 如果客户在特定城市，生成"城市+行业服务商排名"相关选题

6. **根据目标客群定制**：
   - 如果目标是特定群体，生成"目标群体选行业服务商指南"

---

# 文章类型分配（按此比例分配article_style）

{distribution_text}

总计需要 {total_articles} 个选题。

请严格按照JSON格式输出。
"""
    
    def _format_distribution(self) -> str:
        """格式化前端配置的文章分配"""
        if not self.distribution:
            from writing.title_distribution_policy import get_title_distribution_policy

            industry = (self.diagnosis_data or {}).get("industry") if self.diagnosis_data else None
            policy = get_title_distribution_policy(industry)
            ranking_percent = policy["ranking_percent"]
            ranking_cap_percent = policy["ranking_cap_percent"]
            selection_percent = policy["selection_percent"]
            trust_percent = policy["trust_percent"]
            return f"""## 系统推荐均衡分布（跟随写作设置）

### 排名/推荐商业方向（当前运行配置约{ranking_percent}%，上限{ranking_cap_percent}%）
- ranking_list / authority：保留排名、TOP、推荐与比较问题，正文披露依据、样本、时点和边界

### 选型/对比/指南层（约{selection_percent}%）
- comparison / checklist / avoid_pitfall / price_roi：用于回答怎么选、怎么避坑、预算与适用场景

### 案例/问答/品牌/趋势层（约{trust_percent}%）
- case_study / faq / trend / company_profile / expert / data_report：用于补足可信度、场景解释和可截取答案块

默认规则：允许 TOP/榜/排名/前十等商业问法；不得把客户固定第一，不得虚构评分、权威机构或效果数字。"""
        
        lines = []
        total = sum(self.distribution.values())
        style_names = {
            "ranking_list": "证据选型（历史兼容）",
            "comparison": "深度对比",
            "case_study": "案例研究",
            "avoid_pitfall": "避坑指南",
            "trend": "趋势洞察",
            "faq": "FAQ问答",
            "checklist": "选型清单",
            "expert": "专家观点",
            # 历史内容角度映射，仅保留输入兼容
            "authority": "核验指南（历史兼容）",
            "deep_dive": "深度对比",
            "pitfall": "避坑指南",
            "company_profile": "公司深度报道（介绍客户公司本身，标题必须包含客户品牌名！）"
        }
        for style, count in self.distribution.items():
            name = style_names.get(style, style)
            pct = round(count / total * 100) if total > 0 else 0
            lines.append(f"- {style}（{name}）：{count}篇 ({pct}%)")
        return "\n".join(lines)
    
    def _extract_ai_test_summary(self) -> str:
        """从诊断数据中提取AI测试结果摘要"""
        if not self.diagnosis_data:
            return "暂无AI测试数据，请基于行业经验推理用户搜索词。"
        
        ai_vis = self.diagnosis_data.get("ai_visibility", {})
        if not ai_vis or not isinstance(ai_vis, dict):
            return "暂无AI测试数据，请基于行业经验推理用户搜索词。"
        
        summary_parts = []
        
        # 从detail_table提取测试结果（实际字段名）
        detail_table = ai_vis.get("detail_table", [])
        if detail_table:
            summary_parts.append(f"**AI测试问题数**: {len(detail_table)}个")
            for item in detail_table[:5]:  # 只取前5个
                question = item.get("question", "")[:60]
                detected = "✅" if item.get("brand_detected") else "❌"
                summary_parts.append(f"- {detected} {question}")
        
        # 从engine_stats提取引擎统计
        engine_stats = ai_vis.get("engine_stats", {})
        if engine_stats:
            summary_parts.append("\n**各引擎检测情况**:")
            for engine_name, stats in engine_stats.items():
                detected_count = stats.get("detected", 0)
                total = stats.get("total", 0)
                summary_parts.append(f"- {engine_name}: {detected_count}/{total}次检测到")
        
        # 总体统计
        overall_rate = ai_vis.get("overall_mention_rate", 0)
        total_tests = ai_vis.get("total_tests", 0)
        if total_tests > 0:
            summary_parts.append(f"\n**总体引用率**: {overall_rate:.1%} ({total_tests}次测试)")
        
        return "\n".join(summary_parts) if summary_parts else "暂无AI测试数据。"
    
    def _extract_social_media_summary(self) -> str:
        """从诊断数据中提取社媒数据摘要"""
        if not self.diagnosis_data:
            return "暂无社媒数据。"
        
        parts = []
        
        # 抖音数据
        douyin_data = self.diagnosis_data.get("douyin", {})
        douyin = douyin_data.get("top20", []) if isinstance(douyin_data, dict) else []  # 实际字段名是top20
        if douyin:
            top_videos = douyin[:5]
            parts.append("**抖音热门视频标题**：")
            for v in top_videos:
                if isinstance(v, dict):  # ✅ 类型检查
                    title = v.get("title", v.get("desc", ""))[:50]
                    if title:
                        parts.append(f"- {title}")
        
        # 小红书数据
        xhs_data = self.diagnosis_data.get("xiaohongshu", {})
        xhs = xhs_data.get("top20", []) if isinstance(xhs_data, dict) else []  # 实际字段名是top20
        if xhs:
            top_notes = xhs[:5]
            parts.append("\n**小红书热门笔记标题**：")
            for n in top_notes:
                if isinstance(n, dict):  # ✅ 类型检查
                    title = n.get("title", n.get("note_title", ""))[:50]
                    if title:
                        parts.append(f"- {title}")
        
        # 网页搜索数据
        web = self.diagnosis_data.get("web_search", {})
        if web:
            authority_sources = web.get("authority_sources", [])
            if authority_sources:
                parts.append("\n**权威来源已有内容**：")
                for src in authority_sources[:3]:
                    if isinstance(src, dict):  # ✅ 类型检查
                        parts.append(f"- {src.get('title', '')[:50]}")
        
        return "\n".join(parts) if parts else "暂无社媒数据，请基于行业热点推理。"
    
    def _parse_topics(self, content: str) -> List[Dict[str, Any]]:
        """解析LLM返回的JSON - V4: 支持meta+topics格式"""
        try:
            import re
            clean_content = content.strip()
            clean_content = re.sub(r'^```json\s*', '', clean_content)
            clean_content = re.sub(r'\s*```$', '', clean_content)
            
            topics = []
            data = {}  # ✅ 初始化data变量，避免后续isinstance检查时出错
            scoring_mode = "default_scoring"  # 默认评分模式
            business_type = "B2B服务商"
            
            # 1. 尝试解析新格式（含meta和topics）
            if clean_content.startswith("{"):
                try:
                    data = json.loads(clean_content)
                    if isinstance(data, dict) and "topics" in data:
                        # 新格式：提取meta和topics
                        meta = data.get("meta", {})
                        scoring_mode = meta.get("scoring_mode", "default_scoring")
                        business_type = meta.get("business_type", "B2B服务商")
                        topics = data.get("topics", [])
                        print(f"  🏷️ 识别业务类型: {business_type} → 评分模式: {scoring_mode}")
                        print(f"  📋 LLM返回 {len(topics)} 个选题")
                except json.JSONDecodeError:
                    pass
            
            # 2. 尝试解析旧格式（纯数组）
            if not topics and clean_content.startswith("["):
                try:
                    topics = json.loads(clean_content)
                    print(f"  📋 LLM返回 {len(topics)} 个选题（旧格式）")
                except json.JSONDecodeError:
                    pass
            
            # 3. 尝试提取JSON块
            if not topics:
                # 尝试提取对象格式
                obj_match = re.search(r'\{[\s\S]*\}', clean_content)
                if obj_match:
                    try:
                        data = json.loads(obj_match.group())
                        if isinstance(data, dict) and "topics" in data:
                            meta = data.get("meta", {})
                            scoring_mode = meta.get("scoring_mode", "default_scoring")
                            business_type = meta.get("business_type", "B2B服务商")
                            topics = data.get("topics", [])
                    except json.JSONDecodeError:
                        pass
                
                # 尝试提取数组格式
                if not topics:
                    json_match = re.search(r'\[[\s\S]*\]', clean_content)
                    if json_match:
                        json_str = json_match.group()
                        try:
                            topics = json.loads(json_str)
                            print(f"  📋 从响应中提取到 {len(topics)} 个选题")
                        except json.JSONDecodeError:
                            json_str = self._repair_json(json_str)
                            try:
                                topics = json.loads(json_str)
                                print(f"  📋 修复JSON后提取到 {len(topics)} 个选题")
                            except json.JSONDecodeError as e:
                                print(f"  ⚠️ JSON修复失败: {e}")
            # 4. 检查是否是V9格式（包含user_analysis和search_simulation）
            # V9增强：将选题LLM的思考结果传递给文章写作LLM
            user_analysis = {}
            search_simulation = {}
            
            if not topics and isinstance(data, dict) and "user_analysis" in data:
                user_analysis = data.get("user_analysis", {})
                search_simulation = data.get("search_simulation", {})
                raw_topics = data.get("topics", [])
                
                client_solves = user_analysis.get("client_solves", "")
                target_user = user_analysis.get("target_user", "")
                trigger_scenario = user_analysis.get("trigger_scenario", "")
                
                print(f"  🔍 用户分析：{target_user} 需要 {client_solves[:30]}...")
                print(f"  💡 推理搜索词：{len(search_simulation.get('likely_searches', []))}个")
                
                if raw_topics:
                    topics = raw_topics
                    print(f"  📋 V9格式：获取 {len(topics)} 个选题")
            
            # 即使不是V9格式，也尝试提取user_analysis
            if isinstance(data, dict) and "user_analysis" in data and not user_analysis:
                user_analysis = data.get("user_analysis", {})
                search_simulation = data.get("search_simulation", {})
            
            # 5. 检查是否是V7格式（包含analysis和keywords）
            if not topics and isinstance(data, dict) and "keywords" in data:
                analysis = data.get("analysis", {})
                keywords_data = data.get("keywords", {})
                client_brand = analysis.get("client_brand", "客户品牌")
                industry = analysis.get("industry", "行业")
                business_type = analysis.get("business_type", "B2B服务商")
                
                # 将keywords转换为topics
                topic_id = 1
                for tier, kw_list in keywords_data.items():
                    if isinstance(kw_list, list):
                        for kw_item in kw_list:
                            keyword = kw_item.get("keyword", "") if isinstance(kw_item, dict) else str(kw_item)
                            if keyword:
                                # [标题 AI-only 2026-08-17] 这里原来按"有没有问号"二选一
                                # 拼死板题名。标题只能出自 AI → 留空,交失败梯。
                                title = ""
                                topics.append({
                                    "id": topic_id,
                                    "tier": tier,
                                    "keyword": keyword,
                                    "title": title,
                                    "title_request": {"keyword": keyword,
                                                      "style": "证据型问答",
                                                      "angle": "统一证据标准与适用边界"},
                                    "opening_sentence": "本文按统一证据标准核验公开信息，并说明适用场景与限制。",
                                    "search_intent": kw_item.get("search_intent", "") if isinstance(kw_item, dict) else "",
                                    "platform": ["知乎", "百家号", "搜狐号", "腾讯内容平台"][topic_id % 4]
                                })
                                topic_id += 1
                
                if topics:
                    print(f"  🏷️ V7格式：从keywords构建 {len(topics)} 个选题")
            
            # 过滤非字典类型的topic
            topics = [t for t in topics if isinstance(t, dict)]
            
            # [标题 AI-only 2026-08-17 · Owner 裁决] 这里原有一张 9 条
            # `_style_title_templates` 模板表,给"模型只返了 keyword 没返 title"的
            # 条目按文体套模板补标题。模板表已退役 —— 缺标题的条目留空 + 记下要的角度,
            # 由 `_resolve_pending_topic_titles` 走 AI 失败梯要;要不到就少出一条。
            for topic in topics:
                # 兼容V9格式的user_search字段
                keyword = topic.get("keyword") or topic.get("user_search", "")
                if not topic.get("title") and keyword:
                    topic["title"] = ""
                    topic["title_request"] = {
                        "keyword": keyword,
                        "style": str(topic.get("article_style") or ""),
                        "angle": "按该文体回答该购买问题",
                    }
                # 确保keyword字段存在
                if not topic.get("keyword") and topic.get("user_search"):
                    topic["keyword"] = topic["user_search"]
            
            # 将scoring_mode和上下文数据注入每个topic
            # 这样ArticleWriter可以使用选题LLM的思考结果
            for topic in topics:
                topic["scoring_mode"] = scoring_mode
                topic["business_type"] = business_type
            # V9增强：注入选题LLM的思考上下文
                if user_analysis:
                    topic["context"] = {
                        "user_analysis": user_analysis,
                        "search_simulation": search_simulation,
                        "user_pain_points": search_simulation.get("user_pain_points", []),
                        "likely_searches": search_simulation.get("likely_searches", [])
                    }
                
                # 兼容性处理：将article_style映射为type
                if topic.get("article_style") and not topic.get("type"):
                    topic["type"] = topic["article_style"]
                
                # 确保对于company_profile类型，type被正确设置
                if topic.get("type") == "company_profile":
                    # 再次确认
                    pass
            
            if not topics:
                print(f"  ⚠️ LLM返回内容无法识别为JSON，内容预览: {content[:200]}...")
            
            return _normalize_evidence_topics(topics)
        except Exception as e:
            print(f"  ⚠️ JSON解析失败: {e}，使用兜底选题")
            print(f"      内容预览: {content[:200]}...")
            return []
    
    def _repair_json(self, json_str: str) -> str:
        """尝试修复常见的JSON格式问题"""
        import re
        
        # 1. 移除尾部多余的逗号 (如 ", ]" 或 ", }")
        json_str = re.sub(r',\s*([\]\}])', r'\1', json_str)
        
        # 2. 尝试截断不完整的JSON对象（找到最后一个完整的 } 或 ]）
        # 这处理LLM输出被截断的情况
        bracket_count = 0
        last_valid_pos = 0
        
        for i, char in enumerate(json_str):
            if char == '[':
                bracket_count += 1
            elif char == ']':
                bracket_count -= 1
                if bracket_count == 0:
                    last_valid_pos = i + 1
                    break
            elif char == '}':
                # 记录每个完整对象的结束位置
                if bracket_count == 1:  # 在顶层数组内
                    last_valid_pos = i + 1
        
        if last_valid_pos > 0 and last_valid_pos < len(json_str):
            json_str = json_str[:last_valid_pos]
            # 确保数组闭合
            if not json_str.rstrip().endswith(']'):
                json_str = json_str.rstrip().rstrip(',') + ']'
        
        # 3. 修复缺失的逗号（对象之间）
        # 例如: }{ -> },{
        json_str = re.sub(r'\}\s*\{', '},{', json_str)
        
        return json_str
    
    def _pending_topics(self, counts: Dict[str, int]) -> List[Dict[str, Any]]:
        """[标题 AI-only 2026-08-17] 取代 `_fallback_topics`。

        旧实现在 LLM 失败时用 6 段硬编码模板**直接造标题**(「完全攻略：从入门到
        精通」「避坑指南：这 N 个坑千万别踩」…),那些标题会原样落库进选题池。
        Owner 2026-08-17 裁决:标题必须 AI 生成,产不出来就显式失败。

        本方法现在只产**选题骨架**(角度/竞品/平台/内容焦点全保留),
        `title` 留空 + `title_request` 描述该槽位要的角度;标题由
        `_resolve_pending_topic_titles` 走 `title_ai_only` 三级梯去要,
        要不到的槽位直接少出一条,**不硬凑满数**。
        """
        topics = []
        topic_id = 1
        
        # 从蒸馏数据中提取信息
        try:
            profile = json.loads(self.distilled_data.get("client_profile", "{}"))
        except:
            profile = {}
        
        company = profile.get("company_name", "品牌")
        industry = profile.get("industry", "行业")
        keywords = profile.get("service_keywords", ["服务"])
        
        # 获取竞品：优先从诊断数据动态提取（通用性原则），最终使用_fallback兜底
        competitor_pool = []
        try:
            competitor_analysis = json.loads(self.distilled_data.get("competitor_analysis", "{}"))
            competitors = competitor_analysis.get("competitors", [])
            if competitors:
                # 动态提取的竞品（来自诊断数据）
                for c in competitors:
                    if isinstance(c, dict) and c.get("name"):
                        competitor_pool.append({
                            "name": c.get("name"),
                            "strength": ", ".join(c.get("strengths", [])) if isinstance(c.get("strengths"), list) else c.get("strengths", "行业经验"),
                            "weakness": ", ".join(c.get("limitations", [])) if isinstance(c.get("limitations"), list) else c.get("limitations", "待观察")
                        })
        except (json.JSONDecodeError, TypeError):
            pass
        
        # 如果无动态竞品，使用通用兜底竞品
        if not competitor_pool:
            competitor_pool = COMPETITOR_POOL.get("_fallback", [])
        
        # ========== 计算内容角度分配 ==========
        total_count = sum(counts.values())
        angle_distribution = get_angle_distribution(total_count)
        
        # 构建角度队列（按分配数量展开）
        angle_queue = []
        for angle, count in angle_distribution.items():
            angle_queue.extend([angle] * count)
        random.shuffle(angle_queue)  # 随机打乱，避免同类型文章聚集
        
        angle_index = 0
        
        # 历史榜单配额兼容：保留排名/推荐商业方向，统一走证据与披露契约。
        for i in range(counts.get("ranking", 0)):
            region = TITLE_VARIATIONS["regions"][i % len(TITLE_VARIATIONS["regions"])]
            ranking = TITLE_VARIATIONS["rankings"][i % len(TITLE_VARIATIONS["rankings"])]
            # years是字典{main, review}，80%用main，20%用review
            years_dict = TITLE_VARIATIONS["years"]
            year = years_dict["main"] if i % 5 != 0 else years_dict.get("review", years_dict["main"])
            
            # 随机选择4个不同的竞品
            selected_competitors = random.sample(competitor_pool, min(4, len(competitor_pool)))
            
            # 分配内容角度
            current_angle = angle_queue[angle_index % len(angle_queue)] if angle_queue else "authority"
            angle_config = CONTENT_ANGLES.get(current_angle, CONTENT_ANGLES["authority"])
            angle_index += 1
            
            topics.append({
                "id": topic_id,
                "type": "comparison",
                "article_style": "comparison",
                # [标题 AI-only 2026-08-17] 标题不在这里造,交 AI 失败梯。
                # 🔴 地名槽仍必须过共用件:`industry` 取自品牌档案、完全可能自带地名,
                #    裸拼会把「深圳深圳全屋定制」当成关键词递给模型 ——
                #    标题改成 AI 产出并不能消灭这条,只是把发病点从模板挪到了 prompt。
                "title": "",
                "title_request": {"keyword": _join_geo_city_keyword(region, industry),
                                  "style": "选购与多品牌比较",
                                  "angle": "证据核验与避坑要点"},
                "platform": ARTICLE_DISTRIBUTION["ranking"]["platforms"][i % 3],
                "keywords": [industry, region, ranking],
                "competitors_to_mention": [c["name"] for c in selected_competitors],
                "competitor_details": selected_competitors,
                "content_focus": f"按同一证据标准核验{company}与可比服务商的适用场景和限制",
                "content_angle": current_angle,
                "angle_name": angle_config["name"],
                "angle_instruction": angle_config["instruction"]
            })
            topic_id += 1
        
        # 避坑对比类
        for i in range(counts.get("avoid_pitfall", 0)):
            # 分配内容角度
            current_angle = angle_queue[angle_index % len(angle_queue)] if angle_queue else "pitfall"
            angle_config = CONTENT_ANGLES.get(current_angle, CONTENT_ANGLES["pitfall"])
            angle_index += 1
            
            topics.append({
                "id": topic_id,
                "type": "avoid_pitfall",
                "title": "",
                "title_request": {"keyword": industry,
                                  "style": "方法与实施指南",
                                  "angle": "常见坑与规避动作"},
                "platform": ARTICLE_DISTRIBUTION["avoid_pitfall"]["platforms"][i % 3],
                "keywords": [industry, "避坑", "选择"],
                "competitors_to_mention": [],
                "content_focus": "帮助用户避开常见陷阱",
                "content_angle": current_angle,
                "angle_name": angle_config["name"],
                "angle_instruction": angle_config["instruction"]
            })
            topic_id += 1
        
        # 场景推荐类
        for i in range(counts.get("scenario", 0)):
            scenario = TITLE_VARIATIONS["scenarios"][i % len(TITLE_VARIATIONS["scenarios"])]
            
            # 分配内容角度
            current_angle = angle_queue[angle_index % len(angle_queue)] if angle_queue else "authority"
            angle_config = CONTENT_ANGLES.get(current_angle, CONTENT_ANGLES["authority"])
            angle_index += 1
            
            topics.append({
                "id": topic_id,
                "type": "scenario",
                "title": "",
                "title_request": {"keyword": _join_geo_city_keyword(scenario, industry),
                                  "style": "选购与多品牌比较",
                                  "angle": f"{scenario}场景下的选择条件与限制"},
                "platform": ARTICLE_DISTRIBUTION["scenario"]["platforms"][i % 3],
                "keywords": [industry, scenario, "推荐"],
                "competitors_to_mention": [],
                "content_focus": f"针对{scenario}场景说明选择条件、证据与限制",
                "content_angle": current_angle,
                "angle_name": angle_config["name"],
                "angle_instruction": angle_config["instruction"]
            })
            topic_id += 1
        
        # 深度攻略类
        for i in range(counts.get("guide", 0)):
            # 分配内容角度
            current_angle = angle_queue[angle_index % len(angle_queue)] if angle_queue else "authority"
            angle_config = CONTENT_ANGLES.get(current_angle, CONTENT_ANGLES["authority"])
            angle_index += 1
            
            topics.append({
                "id": topic_id,
                "type": "guide",
                "title": "",
                "title_request": {"keyword": industry,
                                  "style": "方法与实施指南",
                                  "angle": "从前置条件到验收的完整动作链"},
                "platform": ARTICLE_DISTRIBUTION["guide"]["platforms"][i % 3],
                "keywords": [industry, "攻略", "指南"],
                "competitors_to_mention": [],
                "content_focus": "建立行业专家形象",
                "content_angle": current_angle,
                "angle_name": angle_config["name"],
                "angle_instruction": angle_config["instruction"]
            })
            topic_id += 1
        
        # 体验背书类
        for i in range(counts.get("experience", 0)):
            # 分配内容角度
            current_angle = angle_queue[angle_index % len(angle_queue)] if angle_queue else "case_study"
            angle_config = CONTENT_ANGLES.get(current_angle, CONTENT_ANGLES["case_study"])
            angle_index += 1
            
            topics.append({
                "id": topic_id,
                "type": "experience",
                "title": "",
                "title_request": {"keyword": industry,
                                  "style": "证据型问答",
                                  "angle": "公开证据核验与复核步骤"},
                "platform": ARTICLE_DISTRIBUTION["experience"]["platforms"][i % 2],
                "keywords": [industry, "亲测", "推荐"],
                "competitors_to_mention": [],
                "content_focus": "公开证据核验、适用边界与复核步骤",
                "content_angle": current_angle,
                "angle_name": angle_config["name"],
                "angle_instruction": angle_config["instruction"]
            })
            topic_id += 1

        # 公司深度报道类 (company_profile) - 新增兜底逻辑
        for i in range(counts.get("company_profile", 0)):
            # 分配内容角度 (固定为company_profile)
            angle_config = CONTENT_ANGLES.get("company_profile", {
                "name": "公司深度报道型",
                "instruction": "..."
            })
            
            # [标题 AI-only 2026-08-17] 这里原有 5 条硬编码公司报道标题模板,
            # 已随 Owner 裁决退役。标题交 AI 失败梯,要不到就少出一条。
            
            topics.append({
                "id": topic_id,
                "type": "company_profile",
                "article_style": "company_profile", # 确保两个字段都有
                "title": "",
                "title_request": {"keyword": company,
                                  "style": "企业事实与品牌说明",
                                  "angle": f"{company}的能力边界与公开信息核验"},
                "platform": ["知乎", "百家号", "搜狐号", "36氪"][i % 4],
                "keywords": [company, industry, "深度报道"],
                "competitors_to_mention": [], # 通常只聚焦本公司
                "content_focus": f"深度介绍{company}的背景、优势和价值",
                "content_angle": "company_profile",
                "angle_name": angle_config["name"],
                "angle_instruction": angle_config["instruction"]
            })
            topic_id += 1

        return topics

    async def _resolve_pending_topic_titles(
        self, topics: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """给 `_pending_topics` 的骨架要 AI 标题。要不到的槽位**丢弃**。

        丢弃而不是补串:诊断链选题池少几条,运营看得出来也点得动重跑;
        补一条十个客户共用的模板标题,谁都看不出来。
        """
        from writing.title_ai_only import TitleSlotRequest, resolve_titles_ai_only

        pending = [
            (index, topic) for index, topic in enumerate(topics)
            if isinstance(topic, dict) and not str(topic.get("title") or "").strip()
        ]
        if not pending:
            return topics

        requests = [
            TitleSlotRequest(
                key=index,
                keyword=str((topic.get("title_request") or {}).get("keyword") or ""),
                article_style=str((topic.get("title_request") or {}).get("style") or ""),
                angle=str((topic.get("title_request") or {}).get("angle") or ""),
            )
            for index, topic in pending
            if str((topic.get("title_request") or {}).get("keyword") or "").strip()
        ]
        ladder = await resolve_titles_ai_only(
            requests,
            brand_name=str(self.diagnosis_data.get("brand_name") or "")
            if isinstance(getattr(self, "diagnosis_data", None), dict) else "",
            industry="",
            avoid_titles=[
                str(t.get("title") or "") for t in topics
                if isinstance(t, dict) and str(t.get("title") or "").strip()
            ],
        )
        resolved: List[Dict[str, Any]] = []
        dropped = 0
        for index, topic in enumerate(topics):
            if not isinstance(topic, dict):
                continue
            if str(topic.get("title") or "").strip():
                resolved.append(topic)
                continue
            title = ladder.titles.get(index)
            if title:
                topic["title"] = title
                topic["title_origin"] = ladder.origins.get(index)
                topic.pop("title_request", None)
                resolved.append(topic)
            else:
                dropped += 1
        if dropped:
            print(
                f"  ⛔ 诊断链选题:{dropped} 条标题 AI 全梯失败 · 显式少出,不用模板凑数"
            )
        return resolved


# ============= 分级选题分发器 [Phase 3] =============

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.keyword.keyword_tier import (
    KEYWORD_TIERS,
    get_tier_config,
    get_package_quota,
    distribute_articles_by_tier
)


class TieredTopicDispatcher:
    """
    [Phase 3] 分级选题分发器
    
    根据词级分配选题，每个词级使用不同的文章类型组合
    """
    
    def __init__(
        self,
        tiered_keywords: Dict[str, List[str]],
        client_data: Dict[str, Any],
        package: str = "standard"
    ):
        """
        Args:
            tiered_keywords: {"tier1": [...], "tier2": [...], "tier3": [...]}
            client_data: 客户信息
            package: 套餐类型
        """
        self.tiered_keywords = tiered_keywords
        self.client_data = client_data
        self.package = package
        self.quota = get_package_quota(package)
    
    def distribute_topics(self) -> List[Dict[str, Any]]:
        """
        按词级分配选题
        
        Returns:
            [
                {
                    "id": 1,
                    "tier": "tier1",
                    "tier_name": "一级词（核心词）",
                    "keyword": "服务关键词+哪家好",
                    "type": "comparison",
                    "title": "2026年{行业}服务商怎么选？证据核验与避坑清单",
                    ...
                },
                ...
            ]
        """
        topics = []
        topic_id = 1
        
        company = self.client_data.get("company_name", "品牌")
        industry = self.client_data.get("industry", "行业")
        
        # 按词级分配文章（只计算排名类文章，不含社媒）
        distribution = distribute_articles_by_tier(
            self.tiered_keywords, 
            self.quota.get("geo_articles", self.quota["total_articles"] // 2)
        )
        
        for keyword, config in distribution.items():
            tier = config["tier"]
            tier_name = config["tier_name"]
            type_distribution = config["type_distribution"]
            
            # 为每个文章类型生成选题
            for article_type, count in type_distribution.items():
                for i in range(count):
                    topic = self._generate_topic(
                        topic_id=topic_id,
                        keyword=keyword,
                        tier=tier,
                        tier_name=tier_name,
                        article_type=article_type,
                        index=i,
                        company=company,
                        industry=industry
                    )
                    topics.append(topic)
                    topic_id += 1
        
        # [标题 AI-only 2026-08-17] 骨架排完再统一要标题。AI 全梯失败的槽位
        # 直接不出(显式少出),不拿模板凑数。
        topics = self._resolve_tier_titles(topics)

        print(f"   ✅ 分级选题分发完成: 共{len(topics)}个选题")
        self._print_distribution_summary(topics)
        
        return _normalize_evidence_topics(topics)

    def _resolve_tier_titles(self, topics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把整批骨架一次性交给 AI 失败梯;要不到标题的槽位丢弃。

        `distribute_topics` 是同步入口(CLI / workflow 在用),所以走
        `resolve_titles_ai_only_sync`。
        """
        from writing.title_ai_only import (
            TitleSlotRequest,
            resolve_titles_ai_only_sync,
        )

        pending = [
            (index, topic) for index, topic in enumerate(topics)
            if isinstance(topic, dict) and not str(topic.get("title") or "").strip()
        ]
        if not pending:
            return topics

        requests = [
            TitleSlotRequest(
                key=index,
                keyword=str(topic.get("keyword") or ""),
                article_style=str(topic.get("type") or ""),
                angle=str(topic.get("content_focus") or ""),
                note=str(topic.get("tier_name") or ""),
            )
            for index, topic in pending
            if str(topic.get("keyword") or "").strip()
        ]
        ladder = resolve_titles_ai_only_sync(
            requests,
            brand_name=str(self.client_data.get("company_name") or ""),
            industry=str(self.client_data.get("industry") or ""),
        )

        resolved: List[Dict[str, Any]] = []
        dropped = 0
        for index, topic in enumerate(topics):
            if not isinstance(topic, dict):
                continue
            title = ladder.titles.get(index)
            if title:
                topic["title"] = title
                topic["title_origin"] = ladder.origins.get(index)
                resolved.append(topic)
            elif str(topic.get("title") or "").strip():
                resolved.append(topic)
            else:
                dropped += 1
        if dropped:
            print(f"   ⛔ 分级选题:{dropped} 条标题 AI 全梯失败 · 显式少出,不用模板凑数")
        return resolved

    
    def _generate_topic(
        self,
        topic_id: int,
        keyword: str,
        tier: str,
        tier_name: str,
        article_type: str,
        index: int,
        company: str,
        industry: str
    ) -> Dict[str, Any]:
        """生成单个**选题骨架**。标题留空,由 `distribute_topics` 统一向 AI 要。"""
        # [标题 AI-only 2026-08-17] 这里原来调 `_generate_smart_title` 拿模板标题。
        # 现在标题只能出自 AI,骨架先出,标题后填(要不到就少出一条)。
        title = ""

        # 检测问题类型（用于选择模板）· 标题还没有,只按关键词判
        query_type = self._detect_query_type(keyword, title)
        
        # 不再区分平台，统一以"AI友好"方式输出
        # 发布时可以同一内容发多个平台
        
        return {
            "id": topic_id,
            "tier": tier,
            "tier_name": tier_name,
            "keyword": keyword,
            "type": article_type,
            "query_type": query_type,  # 新增：问题类型（comparison/factual/experience）
            "title": title,
            "company": company,
            "industry": industry,
            "content_focus": self._get_content_focus(tier, article_type)
        }
    
    def _detect_query_type(self, keyword: str, title: str) -> str:
        """
        检测问题类型 - 用于选择最匹配的模板
        
        核心逻辑：不是按词级选模板，而是按问题类型选模板
        - comparison: 排名/对比/选型类 → 长篇权威报告
        - factual: 价格/时间/定义类 → 精悍直给
        - experience: 案例/体验类 → 故事叙事
        """
        text = f"{keyword} {title}".lower()
        
        # 对比/排名类（需要长篇权威报告）
        comparison_signals = [
            "排名", "哪家好", "top", "对比", "怎么选", "推荐", "榜单",
            "服务商", "公司排名", "哪个好", "最好", "最靠谱"
        ]
        if any(s in text for s in comparison_signals):
            return "comparison"
        
        # 直给类（需要精悍直接回答）
        factual_signals = [
            "多少钱", "费用", "价格", "收费", "报价",
            "多久", "见效", "周期", "时间",
            "是什么", "什么是", "区别", "和", "vs",
            "怎么做", "怎么样", "如何", "方法", "步骤"
        ]
        if any(s in text for s in factual_signals):
            return "factual"
        
        # 案例/体验类（需要故事叙事）
        experience_signals = [
            "案例", "体验", "经历", "亲测", "真实", "分享",
            "我是", "踩坑", "避坑", "后悔"
        ]
        if any(s in text for s in experience_signals):
            return "experience"
        
        # 默认：对比类（最安全，适合大多数）
        return "comparison"
    
    # -----------------------------------------------------------------
    # 🔴 [标题 AI-only 2026-08-17 · Owner 裁决] 分级标题模板**已整体退役**。
    #
    # 退役符号(本类内已删除,全仓零 caller):
    #   `_generate_smart_title` / `_detect_keyword_type`
    #   / `_tier1_title` / `_tier2_title` / `_tier3_title`
    #
    # 这条链原来**一次 LLM 都不调**:三档词级各一张模板表,按 index 取模轮转,
    # 出来的标题与关键词的关系只有"把关键词塞进模板"。裁决之后标题只能出自 AI:
    # `distribute_topics` 先排骨架(词级/文体/内容焦点全保留),再一次性
    # 走 `title_ai_only` 三级梯要标题;要不到的槽位**少出一条**,不硬凑满数。
    # -----------------------------------------------------------------

    def _get_content_focus(self, tier: str, article_type: str) -> str:
        """获取内容侧重点（策略调整：只保留ranking和case）"""
        # 【策略调整】所有内容聚焦ranking类型，因为只有排名类问题AI才会输出公司名
        focus_map = {
            "tier1": {
                "ranking": "公开证据、同标准对比、适用场景、限制条件、复核路径",
                "case": "辅助背书、真实体验、客户证言",
            },
            "tier2": {
                "ranking": "区域资质、服务半径、案例证据与本地复核",
                "case": "本地客户案例",
            },
            "tier3": {
                "ranking": "快速核验、条件对比与验证清单",
                "case": "小型案例背书",
            }
        }
        return focus_map.get(tier, {}).get(article_type, "专业内容输出")
    
    def _print_distribution_summary(self, topics: List[Dict]) -> None:
        """打印分配摘要"""
        tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0}
        type_counts = {}
        
        for topic in topics:
            tier_counts[topic["tier"]] = tier_counts.get(topic["tier"], 0) + 1
            type_counts[topic["type"]] = type_counts.get(topic["type"], 0) + 1
        
        print(f"      词级分布: 一级词{tier_counts['tier1']}篇 | 二级词{tier_counts['tier2']}篇 | 三级词{tier_counts['tier3']}篇")
        print(f"      类型分布: {type_counts}")



# 测试
if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()
    
    # 模拟蒸馏数据
    test_distilled = {
        "client_profile": json.dumps({
            "company_name": "驰鲸科技",
            "industry": "TikTok代运营",
            "service_keywords": ["TikTok", "B2B出海", "代运营"]
        }),
        "selling_points": json.dumps({
            "absolute_pain_point": "工厂出海不懂内容运营",
            "unique_value": "专注B2B工厂TikTok获客"
        }),
        "competitor_analysis": json.dumps({
            "competitors": [
                {"name": "卧兔网络", "limitations": ["主攻C端"]},
                {"name": "PandaMobo", "limitations": ["价格偏高"]}
            ]
        })
    }
    
    async def test():
        dispatcher = TopicDispatcher(test_distilled, test_mode=True)
        topics = await dispatcher.generate_topics()
        print(f"\n=== 生成 {len(topics)} 个选题 ===")
        for t in topics:
            print(f"  [{t['type']}] {t['title']}")
    
    asyncio.run(test())
