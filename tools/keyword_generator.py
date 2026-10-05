"""

 LLM  5  GEO 
"""

import json
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import re
from typing import Optional
import httpx
import os


async def generate_diagnosis_keywords(
    company_name: str,
    industry: str,
    user_keywords: list[str],
    company_profile: str = "",
    model: str = "qwen3.7-max"
) -> list[str]:
    """
     LLM  5  GEO 
    
    Args:
        company_name: 
        industry: 
        user_keywords: 
        company_profile: 
        model: 
    
    Returns:
        5: [, 1, 2, 1, 2]
    """
    
    # 构建Prompt - 基于Coze优化
    keywords_str = ', '.join(user_keywords)
    system_prompt = f"""你是GEO诊断关键词生成专家。任务：为"{company_name}"生成5个诊断关键词。

## 业务背景
- 公司：{company_name}
- 行业：{industry}
- 用户提供关键词：{keywords_str}

## 关键词类型（必须包含）

1. 品牌精准词（核心）
   - 格式：品牌名+核心业务
   - 示例：驰鲸科技GEO优化

2. 行业通用词（2个）
   - 目标：测试行业竞争态势
   - 示例：工厂出海、B2B获客

3. 场景问答词（2个）
   - 格式：如何/怎么/哪家+行业场景
   - 示例：如何选择适合的GEO搜索优化服务

## 输出要求

严格输出JSON数组，不要任何解释：

["品牌词", "行业词1", "行业词2", "问答词1", "问答词2"]

# 重要提示

- 关键词长度：5-15字
- 贴近真实用户搜索习惯
- 避免生僻词或过度营销词
"""
    user_prompt = f"""{company_name}
{industry}
{', '.join(user_keywords)}
{company_profile[:500] if company_profile else ''}

5GEO"""

    #  DashScope API (qwen3-max)
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("    DASHSCOPE_API_KEY")
        return _fallback_keywords(company_name, industry, user_keywords)
    
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            async with llm_track("keyword_seed", "dashscope", model="qwen3.7-max") as tracker:
                response = await client.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "qwen3.7-max",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "temperature": 0.7,
                        "enable_thinking": False
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
                content = result["choices"][0]["message"]["content"]
                
                #  JSON 
                keywords = _parse_keywords(content)
                if len(keywords) >= 5:
                    print(f"   LLM : {keywords[:5]}")
                    return keywords[:5]
    
    except Exception as e:
        print(f"   LLM : {e}")
    
    return _fallback_keywords(company_name, industry, user_keywords)


def _parse_keywords(content: str) -> list[str]:
    """ LLM """
    #  markdown 
    clean = re.sub(r'```json\s*|\s*```', '', content).strip()
    
    try:
        keywords = json.loads(clean)
        if isinstance(keywords, list):
            return [str(k) for k in keywords if k]
    except:
        pass
    
    # 
    lines = [line.strip().strip('"\'') for line in content.split('\n') if line.strip()]
    return [l for l in lines if l and not l.startswith('[') and not l.startswith(']')]


def _fallback_keywords(company_name: str, industry: str, user_keywords: list[str]) -> list[str]:
    """"""
    keywords = []
    
    # 
    if user_keywords:
        keywords.append(f"{company_name} {user_keywords[0]}")
    else:
        keywords.append(f"{company_name} {industry}")
    
    # 
    keywords.append(industry)
    if user_keywords:
        keywords.append(user_keywords[0])
    else:
        keywords.append(f"{industry}")
    
    # 
    keywords.append(f"{industry}")
    if user_keywords:
        keywords.append(f"{user_keywords[0]}")
    else:
        keywords.append(f"{industry}")
    
    print(f"   : {keywords}")
    return keywords


def _render_question_framing_block(framing: str | None) -> str:
    """[#84 §2 · 2026-09-05] 给出题加一个**可选**框架(防守线用「点名品牌」那一族)。

    照 `_render_flywheel_material_block` 的形状办,三条边界一样:
      1. 不传 → 返回空串,**prompt 与改前逐字节一致**(线上诊断路径零行为变化);
      2. 只给**问法框架**,不给答案、不给题面原文 —— 给结论就是把出题变成填模板,
         而 Owner 要的是「我们有一套调教好的出题 AI」;
      3. 模型仍受后置的品牌名泄漏过滤 / 商业意图策略 / 选词质量守卫约束。

    🔴 为什么加在这里而不是在预览端点里做后处理:端点里做就会长出**第二套出题逻辑**,
       和线上那套迟早分家。加在唯一的生产者上,「线上不变」还能被判据逐字验证。
    """
    if framing != "brand_directed":
        return ""
    return (
        "\n\n【本次出题的额外框架】这一批问题请**点名品牌**来问 —— "
        "即问题里出现品牌名本身,例如「XX 靠不靠谱」「XX 是做什么的」「XX 适合谁」这一族。"
        "仍然要用客户自己会说的话,不要套用上面的错误示例。\n"
    )


def _render_flywheel_material_block(material: dict | None) -> str:
    """[B4 接主链 · 工单 B 2026-07-27] 把飞轮沉淀渲染成 prompt 参考块。

    三条边界（决定了它是"素材"而不是"结论"）：
      1. 没素材 → 返回空串，prompt 与改前逐字节一致；
      2. 只给同行实体名与问题种子，**不给答案、不给名录原文**——
         行业名录原样拼进题面正是 07-26/27 那两处 bug 的形状；
      3. 明确写"可以不用"。模型仍受后置的品牌名过滤 / 商业意图策略 / 质量守卫约束。
    """
    if not isinstance(material, dict):
        return ""
    seeds = [
        str(q).strip().replace("\n", " ")[:120]
        for q in (material.get("question_seeds") or [])
        if str(q or "").strip()
    ][:5]
    competitors = [
        str(c).strip().replace("\n", " ")[:60]
        for c in (material.get("competitor_seeds") or [])
        if str(c or "").strip()
    ][:10]
    if not seeds and not competitors:
        return ""

    lines = ["", "【本行业已有沉淀（仅供参考，可以不用）】"]
    if competitors:
        lines.append("- AI 回答里真实出现过的同行：" + "、".join(competitors))
    if seeds:
        lines.append("- 该行业买家真问过的问题（参考问法，不要照抄）：")
        lines.extend(f"  · {s}" for s in seeds)
    lines.append(
        "以上只是参考素材，**不是结论**：不要把同行名字、行业名录、地址、公司全称"
        "原样拼进问题里；仍按上面的规则自己判断该出什么题。"
    )
    return "\n".join(lines) + "\n"


async def analyze_client_business(
    brand_name: str,
    industry: str,
    keywords: list[str],
    additional_info: str = "",
    client_location: str = "",
    business_scope: str = "",
    brand_cities: str = "",
    flywheel_material: dict | None = None,
    question_framing: str | None = None,  # [#84 §2] 不传 = prompt 逐字节与改前一致
) -> dict:
    """
    [Phase 13.6] 第0步：业务深度理解
    
    通过LLM分析客户资料，提取：
    1. 核心业务定位
    2. 目标客户画像
    3. 真实价值主张
    4. 真实用户可能问AI的问题
    5. 精准搜索词组（用于社媒对标）

    flywheel_material: [B4 接主链 · 工单 B 2026-07-27] 飞轮沉淀出来的**参考素材**
        （同行实体名 + 该行业买家真问过的问题种子）。只当参考喂进 prompt，
        不进结果、不绕过后置的品牌名泄漏过滤 / 商业意图策略 / 选词质量守卫 ——
        本周那两处 bug 的病根正是"没素材就硬编码"，所以这里给素材、不给结论。
        不传 = prompt 逐字节与改前一致（零行为变化）。

    brand_cities: [R5 · 2026-08-04] 品牌档案 ``brands.cities`` 原始串。只做两件事:
        并进地域 token 池（判"题面带没带地域"）、表单值没到市级时提供市级主地名。
        不传 = 只吃表单 client_location 的旧行为。
    """
    
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("   ⚠️ 未配置 DASHSCOPE_API_KEY，使用降级方案")
        return _finalize_business_context(
            _fallback_business_context(
                brand_name, industry, keywords, brand_cities=brand_cities,
            ),
            brand_name, industry, keywords, brand_cities=brand_cities,
        )

    # 2026-04-19 commit 23 第二道防线: industry 空 → LLM 没法生成精准问题(会输出"北京相关服务哪家强"泛化占位)
    # 直接走 fallback 模板 + industry 用 brand_name 兜底,避免给用户出"诊断报告和客户完全不沾边"的垃圾结果
    if not industry or not industry.strip():
        print(f"   ⚠️ industry 字段为空 → 走 fallback (避免 LLM 输出'相关服务'泛化问题)")
        # industry 兜底用 brand_name 做行业关键词
        return _finalize_business_context(
            _fallback_business_context(
                brand_name, brand_name, keywords,
                client_location=client_location, business_scope=business_scope,
                brand_cities=brand_cities,
            ),
            brand_name, brand_name, keywords,
            client_location=client_location, business_scope=business_scope,
            brand_cities=brand_cities,
        )
    
    system_prompt = f"""你是一个商业分析专家，擅长从有限的资料中准确理解客户的真实业务。

## 任务
分析客户提供的资料，提取业务核心信息，并生成**动态分配**的测试问题（共6-8个）。

## 🎯 三类测试问题（动态分配！）

### ⚠️ 第一步：判断客户业务类型

| 业务类型 | 判断依据 | 问题分配策略 |
|:---------|:---------|:-------------|
| **本地生活类** | 服务范围限于特定城市/地区 | 地区词为主（4-5个）|
| **全国/全球业务** | 服务范围覆盖全国或更广 | 超一级词为主（4-5个）|

### 1️⃣ 品牌认知词（固定1个）- 类型：brand_awareness
**测试目的**：AI是否知道这家公司
**问法**：直接问品牌名是什么/做什么
**示例**：
- "{brand_name}是什么公司？" → 类型：brand_awareness

### 2️⃣ 地区+行业词（至少1个）- 类型：regional_industry
**测试目的**：客户在本地有没有声量
**问法**：地区+行业+不同角度
**本地生活类要多问**（3-5个），用**不同角度词**拓展：
- "上海豪车租赁哪家靠谱？" → 类型：regional_industry
- "上海豪车租赁哪家车况最好？" → 类型：regional_industry
- "上海埃尔法租赁带司机推荐" → 类型：regional_industry

### 3️⃣ 超一级词（至少1个）- 类型：super_tier1
**测试目的**：行业含金量最高的词，竞争最激烈
**问法**：全国/行业通用词，不含地区
**全国业务要多问**（3-5个），用**不同角度词**拓展：
- "中国最靠谱的豪车租赁公司" → 类型：super_tier1
- "豪车租赁哪家靠谱？" → 类型：super_tier1
- "豪车租赁哪家车况最好？" → 类型：super_tier1
- "GEO优化公司排名TOP5" → 类型：super_tier1

## 动态分配示例

### 本地生活类（如上海豪车租赁）
```
品牌词×1 + 地区词×5 + 超一级词×2 = 8问
```

### 全国业务类（如GEO优化服务）
```
品牌词×1 + 地区词×2 + 超一级词×5 = 8问
```

## 输出格式（严格JSON）
{{
    "core_business": "一句话描述核心业务",
    "target_customers": "目标客户是谁",
    "value_proposition": "客户能获得什么价值",
    "business_type": "local（本地生活）或 national（全国业务）",
    "identified_regions": ["识别出的主要经营城市1", "城市2"],
    "real_user_questions": [
        "问题1", "问题2", "问题3", "问题4", "问题5", "问题6", "问题7", "问题8"
    ],
    "question_types": {{
        "问题1": "brand_awareness",
        "问题2": "regional_industry",
        "问题3": "regional_industry",
        "问题4": "super_tier1",
        "问题5": "super_tier1",
        ...
    }},
    "search_keyword_groups": [
        ["搜索词1", "搜索词2"],
        ["搜索词3", "搜索词4"]
    ]
}}

## ⚠️ 关键规则

1. **品牌认知问题必须包含品牌名**（唯一允许包含品牌名）
2. **地区词必须包含城市名**（如深圳、上海、北京）
3. **超一级词禁止包含品牌名和地区**（全国通用搜索词）
4. **每类至少1个**：brand_awareness≥1, regional_industry≥1, super_tier1≥1
5. **用不同角度词拓展**：哪家靠谱/哪家好/排名/推荐/价格/质量等

### 🚨 品牌名泄漏防护规则（极其重要！）

**除了 brand_awareness 类型的问题外，其他所有问题严禁出现品牌名“{brand_name}”或其任何部分！**

特别注意：即使品牌名与行业品类同名（如“奥特莱斯”既是公司名也是业态名），也必须避开。
这种情况下，用行业通用词替代，例如：
- ❗ 错误：“上海奥特莱斯哪家最值得去”（包含品牌名）
- ✅ 正确：“上海哪里有奢侈品折扣店？”（用行业通用词）
- ❗ 错误：“全国最好的奥特莱斯购物中心排名”
- ✅ 正确：“全国最好的奢侈品折扣购物中心排名”

## 关于 search_keyword_groups 的要求

搜索词组必须包含：
1. 行业通用词（如：TikTok代运营、工厂出海）
2. 客户品牌名（用于验证客户自己的社媒存在）
   - 例如：["{brand_name}", "TK代运营"]
"""

    # [P0-4 · 2026-07-26] 城市与业务范围必须进 prompt。
    #   旧版只喂 brand_name/industry/keywords/additional_info —— prompt 里写着
    #   "地区词必须包含城市名"，但模型根本不知道城市是哪个，只能瞎猜或省略。
    from services.diagnosis_question_quality import (
        SCOPE_NATIONAL,
        distill_trades,
        normalize_business_scope,
        resolve_primary_geo,
    )

    # [R5 · 2026-08-04] 主地名与选词矫正闸同源(表单到市级用表单,否则回落档案市级),
    # 否则 prompt 让 LLM 用"广东"出题、闸内却按"深圳"判地域,双方永远对不上。
    _city = resolve_primary_geo(client_location, brand_cities)
    _scope = normalize_business_scope(business_scope)
    _scope_line = (
        "全国业务（可以出不带地域的行业大词）"
        if _scope == SCOPE_NATIONAL
        else "区域业务（除品牌认知题外，**每一道题都必须带地域限定**）"
    )
    _city_line = _city or "（未提供，请只出不带地域的问题，不要编造城市名）"

    # [R6 · 2026-08-04] 生产病灶(brand 737 诊断 529):prompt 的示例里写着
    # f'"{_city}{industry}哪家好"',而 industry 是登记整串"卫生和社会工作 / 医疗美容服务"
    # → LLM **逐字照抄**,出了两道"广东卫生和社会工作 / 医疗美容服务哪家靠谱?"。
    # 这两道题带"广东"又带"哪家靠谱",选词矫正闸判它们**完全合格**、一个字都不改
    # —— 所以这条不修,下游怎么补都没用。示例改喂炼过的品类词。
    _trades = distill_trades(industry, keywords, brand_name=brand_name)
    _trade = _trades[0] if _trades else (industry or "").strip()
    _flywheel_block = _render_flywheel_material_block(flywheel_material)
    _framing_block = _render_question_framing_block(question_framing)

    user_prompt = f"""请分析以下客户资料：

公司/品牌：{brand_name}
行业：{industry}
主要经营城市：{_city_line}
业务范围：{_scope_line}
关键词：{', '.join(keywords)}
补充资料：{additional_info[:2000] if additional_info else '无'}

⚠️ 测试问题必须是**真实用户会打给 AI 的问句**（如"{_city or '某市'}{_trade}哪家好"、
"{_trade}怎么选"、"{_trade}一般怎么收费"），**不要把服务名称当搜索词**
（错误示例："全案代运营服务"、"精准询盘获客" —— 没有人会这样问 AI）。
⚠️ 上面"行业"那一栏可能是工商登记名录用语（如"卫生和社会工作 / 医疗美容服务"）。
**禁止把它原样抄进问题里** —— 没有人会问"深圳卫生和社会工作哪家好"。
请用客户自己会说的品类词（如"{_trade}"）。
{_flywheel_block}{_framing_block}
请严格按JSON格式输出分析结果。"""

    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
            async with llm_track("keyword_business_analysis", "dashscope", model="qwen3.7-max") as tracker:
                response = await client.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "qwen3.7-max",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "temperature": 0.7,
                        "enable_thinking": False
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
                content = result["choices"][0]["message"]["content"]
                clean = re.sub(r'```json\s*|\s*```', '', content).strip()
                # 移除思考过程（qwen3-max 可能返回 <think>...</think>）
                if '</think>' in clean:
                    clean = clean.split('</think>')[-1].strip()
                parsed = json.loads(clean)
                
                # 🚨 后处理过滤：确保非 brand_awareness 问题不含品牌名
                parsed = _filter_brand_from_questions(parsed, brand_name)

                # [SSOT geo-commercial-intent-governance-v1.0 §4.2 · 统一接线]
                # LLM 产出逐条过 CommercialQueryPolicy 唯一引擎,知识题等槽
                # 换成商业兜底问题(不缩减数量、不动合格槽)。
                parsed = _enforce_commercial_questions(
                    parsed, brand_name, industry, keywords,
                    client_location=client_location, business_scope=business_scope,
                    brand_cities=brand_cities,
                )

                # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.1] 公司题去重 + 层归属矫正。
                #   放在层配额守卫**之前**:腾出来的场景/地域槽由守卫按本层模板补齐,
                #   放之后则该层会低于样本下限,报告显示"本层未实测"。
                parsed = _dedupe_brand_directed_questions(
                    parsed, brand_name, industry, keywords,
                    client_location=client_location, business_scope=business_scope,
                    brand_cities=brand_cities,
                )

                # [P0-4 · 2026-07-26] 选词质量守卫：真实问法 + 区域适配 + 每层样本下限。
                #   生产实证：驰鲸 8 题是 "TikTok工厂出海获客服务" 这类**服务名称**，
                #   不是用户会打给 AI 的问句 → 决策获客层 0/7、场景转化层 0/20。
                parsed = _enforce_question_quality(
                    parsed, brand_name, industry,
                    client_location=client_location, business_scope=business_scope,
                    keywords=keywords, brand_cities=brand_cities,
                )

                return _finalize_business_context(
                    parsed, brand_name, industry, keywords,
                    client_location=client_location, business_scope=business_scope,
                    brand_cities=brand_cities,
                )

    except Exception as e:
        print(f"   ⚠️ 业务分析LLM失败: {e}")
    
    return _finalize_business_context(
        _fallback_business_context(
            brand_name, industry, keywords,
            client_location=client_location, business_scope=business_scope,
            brand_cities=brand_cities,
        ),
        brand_name, industry, keywords,
        client_location=client_location, business_scope=business_scope,
        brand_cities=brand_cities,
    )


def _enforce_question_quality(
    parsed: dict,
    brand_name: str,
    industry: str,
    *,
    client_location: str = "",
    business_scope: str = "",
    keywords: list | None = None,
    brand_cities: str = "",
) -> dict:
    """[P0-4] 把 8 问过一遍真实问法 / 区域适配 / 每层样本下限守卫（等槽替换，不缩减）。"""
    try:
        from config.ai_engines import DIAGNOSIS_ENGINES
        from services.diagnosis_question_quality import enforce_question_quality
    except Exception:  # pragma: no cover - 独立脚本兜底，不改变行为
        return parsed

    questions = list(parsed.get("real_user_questions") or [])
    if not questions:
        return parsed
    result = enforce_question_quality(
        questions,
        dict(parsed.get("question_types") or {}),
        brand_name=brand_name,
        industry=industry,
        city=client_location or ",".join(parsed.get("identified_regions") or []),
        business_scope=business_scope or parsed.get("business_type") or "",
        engine_count=len(DIAGNOSIS_ENGINES),
        # [2026-07-27 · diagnosis 489] 核心词喂给品类词提炼("TikTok工厂代运营"
        # 比 industry 登记名录整串像人话);下游 ai_test 只测前 8 题,守卫补的
        # 场景层题必须重排进前 8,否则"每层样本下限"被切片无效化(本层未实测)。
        keywords=keywords or [],
        max_tested_questions=8,
        # [R5 · 2026-08-04] 品牌档案城市并进地域 token 池
        brand_cities=brand_cities,
    )
    repairs = result.get("repairs") or []
    if repairs:
        for repair in repairs:
            print(
                f"   [选词质量] {repair.get('result')}: {repair.get('problems')} "
                f"| {repair.get('original') or '(新增)'} → {repair.get('replacement') or '(保留原题)'}"
            )
    parsed["real_user_questions"] = result["questions"]
    parsed["question_types"] = result["question_types"]
    parsed["question_quality"] = {
        "repairs": repairs,
        "layer_counts": result.get("layer_counts"),
        "business_scope": result.get("business_scope"),
        "city": result.get("city"),
        "min_questions_per_layer": result.get("min_questions_per_layer"),
        "rule_version": result.get("rule_version"),
    }
    return parsed


def _enforce_commercial_questions(
    parsed: dict,
    brand_name: str,
    industry: str,
    keywords: list,
    *,
    client_location: str = "",
    business_scope: str = "",
    brand_cities: str = "",
) -> dict:
    """[SSOT geo-commercial-intent-governance-v1.0 §4.2 · Review-CTO 返工接线]

    诊断 8 问逐条过 CommercialQueryPolicy 唯一引擎:
      - 合格商业题 / 品牌直问(实体对照)原样保留;
      - 知识题/歧义题不得占用名额 → 按原槽位从商业兜底模板**等槽局部修复**
        (数量不变、不动其余槽、替换过程打印可见);
      - 兜底枯竭时保留原题(绝不静默缩减 §3.3),交由报告层提示。
    """
    try:
        from services.commercial_query_policy import POLICY_VERSION, evaluate
    except Exception:  # pragma: no cover - 独立脚本兜底,不改变行为
        return parsed
    # [WO 2026-08-06 §2] 商业闸**之上**再叠一道提名型闸(⊂ 商业)。
    #   商业闸判「服务商怎么选才不踩坑」合格是对的(提问对象=服务商),
    #   但那道题的 AI 答案是方法论清单、永远点不出厂商 —— 进诊断就是花钱买 0 格。
    #   拿不到提名闸时**退回只走商业闸**(与本次改动前行为完全一致),不擅自判废。
    try:
        from services.nomination_question_policy import is_nomination_question
    except Exception:  # pragma: no cover - 独立脚本兜底
        is_nomination_question = None  # type: ignore[assignment]

    def _question_eligible(candidate: str) -> bool:
        """一道题够不够格占用诊断槽位。

        两道闸**都**要过:商业(付费交付资格,SSOT)+ 提名(测得出品牌)。
        """
        if not evaluate(candidate, brand_name=brand_name).commercial_delivery_eligible:
            return False
        if is_nomination_question is None:
            return True
        ok, _reason = is_nomination_question(candidate, brand_name=brand_name)
        return ok

    questions = list(parsed.get("real_user_questions") or [])
    if not questions:
        return parsed
    qtypes = dict(parsed.get("question_types") or {})

    fallback = _fallback_business_context(
        brand_name, industry, keywords,
        client_location=client_location, business_scope=business_scope,
        brand_cities=brand_cities,
    )
    fallback_types = dict(fallback.get("question_types") or {})
    # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.1] 兜底池**必须剔掉品牌定向题**。
    #   真凶就在这里:_fallback_business_context 产物的第 0 条恒为
    #   f"{brand_name}是什么公司？",而下面的等槽替换会把它顶到**任意**槽位,
    #   还原样继承被替换槽的层标签(regional_industry / super_tier1)。
    #   生产实证:有 layer_key 的 32 份 v2 报告里 22 份因此多出一道"场景层公司题",
    #   客户拿自己的品牌词把自己的场景转化层顶到 50%(报告 551)。
    #   品牌层名额由 LLM 出题 + enforce_question_quality 的层配额守卫负责,不靠这个池子补。
    from services.brand_directed_question import is_brand_directed_text

    pool = [
        q for q in (fallback.get("real_user_questions") or [])
        if q not in questions and not is_brand_directed_text(q, brand_name)
    ]

    out: list = []
    replaced = 0
    for question in questions:
        if _question_eligible(question):
            out.append(question)
            continue
        substitute = None
        while pool:
            candidate = pool.pop(0)
            if candidate in out:
                continue
            if _question_eligible(candidate):
                substitute = candidate
                break
        if substitute is None:
            out.append(question)  # 兜底枯竭:保槽不缩减,不静默删除
            continue
        original_type = qtypes.pop(question, None)
        qtypes[substitute] = (
            original_type
            or fallback_types.get(substitute)
            or "super_tier1"
        )
        out.append(substitute)
        replaced += 1

    if replaced:
        print(
            f"   [CommercialQueryPolicy] 诊断问题等槽修复 {replaced} 个"
            f"知识/歧义题 → 商业问题({POLICY_VERSION})"
        )
    parsed["real_user_questions"] = out
    parsed["question_types"] = {
        q: qtypes.get(q, fallback_types.get(q, "super_tier1")) for q in out
    }
    return parsed


def _finalize_business_context(
    parsed: dict,
    brand_name: str,
    industry: str,
    keywords: list,
    *,
    client_location: str = "",
    business_scope: str = "",
    brand_cities: str = "",
) -> dict:
    """[WO_BRAND_QUESTION_LEAK 2026-08-05] ``analyze_client_business`` 的**唯一收口**。

    🔴 存在的理由(2026-08-05 实测,不是设想):本函数所在的出题入口有**四个出口**,
    其中三个是降级出口(无 DASHSCOPE_API_KEY / industry 为空 / LLM 异常),
    它们 ``return _fallback_business_context(...)`` **直接返回**,
    绕过品牌名过滤、商业意图闸、公司题去重、选词质量守卫 —— 一个都没走。

    实测后果最重的是「industry 为空」那个出口:它把 ``brand_name`` 当 ``industry`` 传下去,
    ``distill_trades`` 炼不出品类词时最后一道回落是 ``str(industry).strip()`` ——
    于是**品类词 = 品牌名**,8 道题全含品牌名、7 道挂在竞争层。
    这类题必然命中 → 报告会给出一个接近满分的假可见度。
    (复现:``scripts/probe_fallback_exit_leak_2026_08_05.py``,「奥特莱斯」「碧玉良缘」
     两个真实生产品牌形态各 8 道题里 7 道错层。)

    收口只做**一件**事:保证不变量「题面含品牌名的题必挂品牌层」。
    它不丢词、不换词、不改题量 —— 所以放在任何出口上都安全,包括降级出口。

    ⚠️ 收口能做到与做不到的,分清楚(实测,不是设想):
      · 做到:错层被纠正 → 竞争两层变 0 样本 → ``partial_sample=True``、
        ``level_capped=True``、等级由主导级封顶降到成长级、两层显示「本层未实测」。
        污染态下这些标志**全是干净的**(等级主导级 · 每层都像有样本),那才是最危险的。
      · **做不到**:``total_score`` 仍是 100 —— 漏斗的权重重归一把品牌层的 20 分权重
        放大到了 100(``funnel_score`` audit P2 2026-06-10 的既有设计)。
        本工单边界明令「不改评分权重/漏斗结构」,所以不动,**但这是残留风险,已上报**。
    ``test_degraded_exit_funnel_flags_thin_coverage`` 钉住上面"做到"的那几个标志,
    谁把封顶去掉就转红。

    对 LLM 成功那条路是幂等的(链路里已经去过重,这里 surplus 为空原样返回),
    同时兜住一个残余口子:``_enforce_question_quality`` 的层配额守卫在引擎降级
    (floor ≥ 2)时会从品牌模板池再补一道公司题 —— 收口在它之后,补的那道也归得对。

    🔴 新增出口必须走这里。``test_every_generation_exit_goes_through_the_choke_point``
    用 AST 枚举本入口的全部 return,漏一个就转红 —— 这条不变量由锁保证,不靠人记得。
    """
    return _dedupe_brand_directed_questions(
        parsed, brand_name, industry, keywords,
        client_location=client_location, business_scope=business_scope,
        brand_cities=brand_cities,
    )


def _dedupe_brand_directed_questions(
    parsed: dict,
    brand_name: str,
    industry: str,
    keywords: list,
    *,
    client_location: str = "",
    business_scope: str = "",
    brand_cities: str = "",
) -> dict:
    """[WO_BRAND_QUESTION_LEAK 2026-08-05 §2.1] 指向公司的题**最多一道**,且必挂品牌层。

    生产实证(报告 551 等 22/32 份):8 问里出现两道公司题 ——
    「{品牌}有限公司是做什么的？」(品牌层,正确)+「{品牌}是什么公司？」(场景层,漏网)。
    后者必然命中,把场景转化层顶成 4/8=50%、总分虚高约 20 分,还让客户自己进了自家竞品榜。

    两件事一起做,顺序不可换:
      1. **保留**判定:优先留标签已是品牌层的那道;都不是则留第一道并改标品牌层
         (文本为准 —— 一道含品牌名的题挂在场景层就是错标,不是风格问题);
      2. 多出来的公司题走**等槽替换**(数量不变,SSOT §9.6 不静默缩减),
         替换池已剔除品牌题;池枯竭则保留原题但改标品牌层 ——
         宁可品牌层多一道,也绝不让它继续冒充场景层去污染竞争面。

    本函数**只处理层归属与重复**,不改题量、不动其余槽。
    """
    from services.brand_directed_question import is_brand_directed_text
    from services.diagnosis_question_quality import LAYER_BRAND

    questions = list(parsed.get("real_user_questions") or [])
    if not questions:
        return parsed
    qtypes = dict(parsed.get("question_types") or {})

    directed = [q for q in questions if is_brand_directed_text(q, brand_name)]
    if not directed:
        return parsed

    keeper = next((q for q in directed if qtypes.get(q) == LAYER_BRAND), directed[0])
    qtypes[keeper] = LAYER_BRAND

    surplus = [q for q in directed if q is not keeper and q != keeper]
    if not surplus:
        parsed["question_types"] = qtypes
        return parsed

    fallback = _fallback_business_context(
        brand_name, industry, keywords,
        client_location=client_location, business_scope=business_scope,
        brand_cities=brand_cities,
    )
    fallback_types = dict(fallback.get("question_types") or {})
    pool = [
        q for q in (fallback.get("real_user_questions") or [])
        if q not in questions and not is_brand_directed_text(q, brand_name)
    ]

    out: list[str] = []
    for question in questions:
        if question not in surplus:
            out.append(question)
            continue
        original_type = qtypes.get(question)
        substitute = None
        while pool:
            candidate = pool.pop(0)
            if candidate in out or candidate in questions:
                continue
            substitute = candidate
            break
        if substitute is None:
            # 池枯竭:保槽不缩减,但改标品牌层(它本来就是品牌题)。
            qtypes[question] = LAYER_BRAND
            out.append(question)
            print(f"   [公司题去重] 兜底池枯竭 · 保留原题并改标品牌层: {question}")
            continue
        qtypes.pop(question, None)
        qtypes[substitute] = (
            fallback_types.get(substitute)
            or (original_type if original_type != LAYER_BRAND else None)
            or "super_tier1"
        )
        out.append(substitute)
        print(f"   [公司题去重] 重复公司题等槽替换: 「{question}」 → 「{substitute}」")

    parsed["real_user_questions"] = out
    parsed["question_types"] = {q: qtypes.get(q, "super_tier1") for q in out}
    return parsed


def _filter_brand_from_questions(parsed: dict, brand_name: str) -> dict:
    """
    后处理过滤器：确保非 brand_awareness 类型的问题不包含品牌名
    
    解决“奥特莱斯”这类品牌名=行业通用名时，LLM 无法区分的问题
    """
    questions = parsed.get("real_user_questions", [])
    question_types = parsed.get("question_types", {})
    
    # 提取品牌名核心词（去掉公司后缀）
    brand_core = brand_name
    for suffix in ["(中国)有限公司", "（中国）有限公司", "有限公司", "股份有限公司", "集团", "公司"]:
        if brand_core.endswith(suffix) and len(brand_core) > len(suffix) + 1:
            brand_core = brand_core[:-len(suffix)]
    # 去括号
    brand_core = re.sub(r'[（(][^）)]*[）)]', '', brand_core).strip()
    
    if len(brand_core) < 2:
        return parsed  # 核心词太短，跳过过滤
    
    filtered_questions = []
    filtered_types = {}
    contaminated_count = 0
    
    for q in questions:
        q_type = question_types.get(q, "super_tier1")
        
        if q_type == "brand_awareness":
            # 品牌认知问题允许包含品牌名
            filtered_questions.append(q)
            filtered_types[q] = q_type
        elif brand_core in q:
            # 非品牌问题但包含品牌名 → 删除品牌名并重新组装
            clean_q = q.replace(brand_core, "").strip()
            # 如果删除后问题太短或无意义，跳过
            if len(clean_q) < 8:
                contaminated_count += 1
                print(f"   ⚠️ 过滤掉含品牌名的问题: {q}")
                continue
            filtered_questions.append(clean_q)
            filtered_types[clean_q] = q_type
            print(f"   🔧 清洗问题: 「{q}」 → 「{clean_q}」")
        else:
            filtered_questions.append(q)
            filtered_types[q] = q_type
    
    if contaminated_count > 0:
        print(f"   ⚠️ 共过滤 {contaminated_count} 个含品牌名的问题")
    
    parsed["real_user_questions"] = filtered_questions
    parsed["question_types"] = filtered_types
    return parsed


def _fallback_business_context(
    brand_name: str,
    industry: str,
    keywords: list[str],
    *,
    client_location: str = "",
    business_scope: str = "",
    brand_cities: str = "",
) -> dict:
    """降级方案：基于关键词简单推理，动态分配测试问题

    [P0-4 · 2026-07-26] 城市与业务范围以**用户填的为准**。
    旧版只从品牌名里猜城市，猜不到就硬写"深圳" —— 一个杭州客户会拿到
    全套深圳地域题，必然 0 命中，还看不出为什么。
    业务范围缺省按「区域」（多数代理客户是本地生意；给区域客户出无地域大词
    等于把他丢进全国竞品堆里比）。

    [R6 · 2026-08-04] 题面里的品类词不再用 ``industry`` 原文。它常是登记名录整串
    （"卫生和社会工作 / 医疗美容服务"），拼出来是"深圳卫生和社会工作 / 医疗美容服务
    哪家靠谱？" —— 生产实证 11 次诊断 / 11 个品牌的题面里出现过 industry 整串
    （489/511/515/529/129…）。改走 ``distill_trades``（L2 段 → 口语化 → 判废）。
    炼不出品类词时**保留 industry 原文**：这是 LLM 已经失败后的最后一道兜底，
    出个次优的题也比一道题都没有强（与模板池"宁可少出"的取舍点不同）。

    [R7 · 2026-08-04] 超一级词按受众选后缀：B2B 说"公司排名/服务商"，C 端不说。
    [R5 · 2026-08-04] 城市与选词矫正闸同源（``resolve_primary_geo``）。
    """
    from services.diagnosis_question_quality import (
        AUDIENCE_B2B,
        SCOPE_NATIONAL,
        distill_trades,
        industry_audience,
        normalize_business_scope,
        resolve_primary_geo,
    )

    _trades = distill_trades(industry, keywords, brand_name=brand_name)
    trade = _trades[0] if _trades else str(industry or "").strip()
    audience = industry_audience(industry, keywords, trade)

    detected_city = resolve_primary_geo(client_location, brand_cities)
    if not detected_city:
        # 用户没填 → 退回从品牌名猜（保留原有能力，但不再硬写默认城市）
        for city in ("深圳", "上海", "北京", "杭州", "广州", "成都", "东莞",
                     "南京", "武汉", "西安", "苏州", "重庆", "天津", "青岛"):
            if city in (brand_name or ""):
                detected_city = city
                break

    scope = normalize_business_scope(business_scope)
    # 业务范围显式为全国 → 全国题为主；否则只要有城市就按本地生意出题。
    is_local = scope != SCOPE_NATIONAL and bool(detected_city)
    if not detected_city:
        # 真的没有任何城市线索：用"本地"占位而不是假造一个城市名
        detected_city = "本地"
    
    if is_local:
        # 本地生活类：品牌词1 + 地区词5 + 超一级词2
        questions = [
            # 品牌认知 (1问)
            f"{brand_name}是什么公司？",
            # 地区+行业 (5问)
            f"{detected_city}{trade}哪家靠谱？",
            f"{detected_city}{trade}哪家服务好？",
            f"{detected_city}{trade}推荐几家",
            (f"给我推荐几家{detected_city}做{trade}的公司" if audience == AUDIENCE_B2B
             else f"{detected_city}做{trade}比较好的有哪几家？"),
            f"{detected_city}{trade}哪家性价比高？",
            # 超一级词 (2问)
            f"{trade}哪家好？推荐几家靠谱的",
            # [WO 2026-08-06 §2.2-2] 原为 f"{trade}一般怎么收费？" —— 一个提名信号
            # 都没有,AI 答的是价格区间不点名商家 → 这一槽必然测不出提及。
            # 兜底池是**等槽替换的供给方**,它自己产不合格题就等于"出口修了、
            # 兜底池还在产"(brandq 包刚踩过同一个坑)。换成提名型的收费问法。
            f"{trade}哪家性价比高？推荐几家",
        ]
        question_types = {
            questions[0]: "brand_awareness",
            questions[1]: "regional_industry",
            questions[2]: "regional_industry",
            questions[3]: "regional_industry",
            questions[4]: "regional_industry",
            questions[5]: "regional_industry",
            questions[6]: "super_tier1",
            questions[7]: "super_tier1"
        }
        business_type = "local"
    else:
        # 全国业务类：品牌词1 + 地区词2 + 超一级词5
        questions = [
            # 品牌认知 (1问)
            f"{brand_name}是什么公司？",
            # 地区+行业 (2问)
            (f"{detected_city}{trade}服务商推荐几家" if audience == AUDIENCE_B2B
             else f"{detected_city}{trade}推荐几家"),
            (f"给我推荐几家{detected_city}做{trade}的公司" if audience == AUDIENCE_B2B
             else f"{detected_city}做{trade}比较好的有哪几家？"),
            # 超一级词 (5问)
            f"{trade}哪家好？给我推荐几家靠谱的",
            (f"中国最靠谱的{trade}公司推荐" if audience == AUDIENCE_B2B
             else f"全国做{trade}比较有名的有哪些"),
            (f"{trade}公司排名TOP5" if audience == AUDIENCE_B2B
             else f"{trade}排名前十有哪些？"),
            f"{trade}哪家最专业？",
            f"{trade}一般怎么收费？哪家性价比高",
        ]
        question_types = {
            questions[0]: "brand_awareness",
            questions[1]: "regional_industry",
            questions[2]: "regional_industry",
            questions[3]: "super_tier1",
            questions[4]: "super_tier1",
            questions[5]: "super_tier1",
            questions[6]: "super_tier1",
            questions[7]: "super_tier1"
        }
        business_type = "national"
    
    return {
        "core_business": f"{industry}服务",
        "target_customers": f"需要{industry}服务的企业",
        "value_proposition": f"提供专业的{industry}解决方案",
        "business_type": business_type,
        "identified_regions": [detected_city],
        "real_user_questions": questions,
        "question_types": question_types,
        # [R6 · 2026-08-04] 搜索词组用炼过的品类词：登记名录整串拿去搜社媒搜不到
        # 任何东西。core_business/target_customers 那几行保留 industry 原文 ——
        # 它们是报告里的**业务描述**，不是题面，登记用语在那里是准确的。
        "search_keyword_groups": [
            [brand_name, trade],  # 第一组必须包含品牌名，验证客户社媒存在
            [trade, keywords[0] if keywords else trade],
            [keywords[1] if len(keywords) > 1 else trade, "服务商"]
        ]
    }


# ============= 分级关键词生成 [Phase 2] =============

from tools.keyword.keyword_tier import (
    KEYWORD_TIERS, 
    PACKAGE_QUOTAS, 
    get_package_quota,
    validate_keywords_for_package
)


async def generate_tiered_keywords(
    brand_name: str,
    industry: str,
    package: str = "standard",
    user_keywords: list[str] = None,
    additional_info: str = ""
) -> dict:
    """
    [Phase 2] 生成分级关键词（含客户画像推理）

    先推理目标客户画像和搜索习惯，再据此生成关键词。
    专业客户（B2B/技术型）会搜型号参数，不会搜"哪家好"。

    Returns:
        {
            "customer_persona": {...},  # 目标客户画像
            "tier1": ["...", ...],
            "tier2": ["...", ...],
            "tier3": ["...", ...],
            "keyword_reasons": {"关键词": "推荐理由", ...}
        }
    """
    quota = get_package_quota(package)
    user_kw_str = ", ".join(user_keywords) if user_keywords else "无"

    system_prompt = f"""你是GEO关键词策略专家。任务分两步：先分析目标客户画像，再据此生成关键词。

## ⚡ 第一步：推理目标客户画像

根据行业和补充信息，判断这家公司的**终端客户**是谁：

| 客户类型 | 典型行业 | 搜索特征 | 关键词策略 |
|:---------|:---------|:---------|:-----------|
| **C端消费者** | 装修/教育/餐饮/零售 | 泛搜：哪家好、推荐、排名、怎么选 | 以"推荐/排名/对比"类泛词为主 |
| **B端企业采购** | 工业设备/企业服务/原材料 | 场景搜：行业+解决方案+应用场景 | 以"解决方案/应用场景/行业案例"为主 |
| **专业技术人员** | 仪器仪表/工业自动化/医疗器械 | 精搜：型号+参数+规格+选型+对比 | 以"型号参数/选型指南/技术对比"为主 |
| **B端+C端混合** | 建材/软件/金融 | 混合搜：既有泛搜也有场景搜 | 均衡分配 |

**关键判断依据**：
- 客户提供的关键词里如果有型号、参数、规格等词 → 大概率是技术型客户
- 行业本身的专业度（工业设备 vs 奶茶店）
- 补充信息中的线索（服务对象描述等）

## ⚡ 第二步：按画像生成关键词

### 套餐配额
- 一级词（核心词）：{quota['tier1']}个
- 二级词（区域词/细分词）：{quota['tier2']}个
- 三级词（长尾精准词）：{quota['tier3']}个

### 词级定义（根据客户类型调整）

【一级词（核心词）】高竞争，行业级关键词
- C端客户 → "XX推荐"、"XX排名"、"XX哪家好"
- B端客户 → "XX解决方案"、"XX服务商排名"、"XX行业应用"
- 技术客户 → "XX选型指南"、"XX参数对比"、"XX技术方案"

【二级词（区域/细分词）】中等竞争
- C端客户 → "城市+行业+推荐"
- B端客户 → "行业+应用场景"、"细分领域+解决方案"
- 技术客户 → "具体型号/系列+对比"、"应用领域+设备选型"

【三级词（长尾精准词）】低竞争，高转化
- C端客户 → "XX多少钱"、"XX怎么选不踩坑"
- B端客户 → "XX采购注意事项"、"XX投入产出比"、"XX成功案例"
- 技术客户 → "XX型号参数表"、"XX vs YY哪个好"、"XX安装调试指南"

## ⚠️ 重要规则
1. **禁止在关键词中包含品牌名"{brand_name}"**
2. 关键词是目标客户的真实搜索词，不是品牌推广词
3. 每个关键词必须附带一句推荐理由（说明为什么目标客户会搜这个词）

## 输出格式（严格JSON）

```json
{{
    "customer_persona": {{
        "type": "C端消费者/B端企业采购/专业技术人员/B端+C端混合",
        "description": "一句话描述目标客户是谁",
        "search_behavior": "他们的搜索习惯特征",
        "decision_journey": "他们的决策链路（如：需求确认→选型对比→询价→采购）"
    }},
    "tier1": ["关键词1", "关键词2", ...],
    "tier2": ["关键词1", "关键词2", ...],
    "tier3": ["关键词1", "关键词2", ...],
    "keyword_reasons": {{
        "关键词1": "推荐理由（15字内，说明目标客户为什么会搜这个词）",
        "关键词2": "推荐理由",
        ...
    }}
}}
```"""

    user_prompt = f"""行业：{industry}
用户关键词参考：{user_kw_str}
补充信息：{additional_info[:500] if additional_info else '无'}

请先分析目标客户画像，再生成分级关键词（不要包含品牌名）。"""

    # 使用 DeepSeek API
    from services.llm.deepseek_key_pool import has_deepseek_key, adeepseek_post_with_failover
    if not has_deepseek_key():
        print("    ⚠️ 缺少 DEEPSEEK_API_KEY，使用降级方案")
        return _fallback_tiered_keywords(brand_name, industry, quota, user_keywords)

    try:
        # [failover 2026-06-11] 多 key 失败自动换下一个重试(单 key=直调·向后兼容)·内含 llm_track·URL 无 v1
        response = await adeepseek_post_with_failover(
            {
                "model": DEEPSEEK_OFFICIAL_FLASH,  # DeepSeek V3.2
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.7
            },
            url="https://api.deepseek.com/chat/completions",
            timeout=30.0,
            track_name="keyword_tiered_generation",
            track_model=DEEPSEEK_OFFICIAL_FLASH,
        )
        result = response.json()
        content = result["choices"][0]["message"]["content"]

        # 解析JSON
        tiered_keywords = _parse_tiered_keywords(content, quota)
        if tiered_keywords:
            print(f"   ✅ LLM生成分级关键词:")
            print(f"      一级词({len(tiered_keywords.get('tier1', []))}): {tiered_keywords.get('tier1', [])}")
            print(f"      二级词({len(tiered_keywords.get('tier2', []))}): {tiered_keywords.get('tier2', [])}")
            print(f"      三级词({len(tiered_keywords.get('tier3', []))}): {tiered_keywords.get('tier3', [])}")
            return tiered_keywords

    except Exception as e:
        print(f"   ⚠️ LLM生成分级关键词失败: {e}")

    return _fallback_tiered_keywords(brand_name, industry, quota, user_keywords)


def _parse_tiered_keywords(content: str, quota: dict) -> Optional[dict]:
    """解析LLM返回的分级关键词JSON（含画像和理由）"""
    # 清理markdown标记
    clean = re.sub(r'```json\s*|\s*```', '', content).strip()

    # 移除思考过程
    if "</think>" in clean:
        clean = clean.split("</think>")[-1].strip()

    try:
        data = json.loads(clean)

        result = {
            "tier1": data.get("tier1", [])[:quota["tier1"]],
            "tier2": data.get("tier2", [])[:quota["tier2"]],
            "tier3": data.get("tier3", [])[:quota["tier3"]],
        }

        # 提取画像和理由（新增字段，向后兼容）
        if "customer_persona" in data:
            result["customer_persona"] = data["customer_persona"]
        if "keyword_reasons" in data:
            result["keyword_reasons"] = data["keyword_reasons"]

        return result

    except json.JSONDecodeError:
        # 尝试正则提取
        tier1 = re.findall(r'"tier1"\s*:\s*\[(.*?)\]', clean, re.DOTALL)
        tier2 = re.findall(r'"tier2"\s*:\s*\[(.*?)\]', clean, re.DOTALL)
        tier3 = re.findall(r'"tier3"\s*:\s*\[(.*?)\]', clean, re.DOTALL)

        def extract_keywords(match_list):
            if match_list:
                return [kw.strip().strip('"\'') for kw in match_list[0].split(',') if kw.strip()]
            return []

        return {
            "tier1": extract_keywords(tier1)[:quota["tier1"]],
            "tier2": extract_keywords(tier2)[:quota["tier2"]],
            "tier3": extract_keywords(tier3)[:quota["tier3"]]
        }


def _fallback_tiered_keywords(
    brand_name: str, 
    industry: str, 
    quota: dict,
    user_keywords: list[str] = None
) -> dict:
    """
    降级方案：模块化组合生成分级关键词
    
    借鉴竞品SAAS的设计：多列组合规则生成大量变体
    """
    import random
    
    # ===== 模块定义 =====
    # A: 行业/大类
    A_行业 = [industry]
    if user_keywords:
        A_行业.extend(user_keywords[:2])
    
    # B: 区域
    B_区域 = ["深圳", "上海", "北京", "杭州", "广州", "成都", "东莞", "苏州"]
    
    # C: 主体类型
    C_主体 = ["公司", "服务商", "团队", "机构", "平台", "工作室"]
    
    # D: 属性/定位
    D_属性 = ["专业", "靠谱", "正规", "本地", "性价比高", "有实力"]
    
    # E: 用户意图（转化导向）
    E_意图 = ["推荐", "哪家好", "怎么选", "排名", "TOP5", "排行榜", "对比"]
    
    # F: 询价/具体问题
    F_询价 = ["多少钱", "怎么收费", "价格", "报价", "费用"]
    
    # G: 场景/问答
    G_场景 = ["要多久见效", "有用吗", "靠谱吗", "怎么做", "注意事项", "和SEO的区别", "适合什么行业"]
    
    # ===== 组合规则 =====
    def combine(*modules):
        """组合多个模块"""
        result = []
        for combo in _product(*modules):
            result.append("".join(combo))
        return result
    
    def _product(*iterables):
        """笛卡尔积"""
        import itertools
        return list(itertools.product(*iterables))
    
    # 一级词规则: A+E (行业+意图) - 高竞争核心词
    tier1_combos = combine(A_行业, E_意图)  # "GEO优化哪家好", "GEO优化推荐"
    tier1_combos.extend(combine(A_行业, C_主体, E_意图))  # "GEO优化公司推荐"
    
    # 二级词规则: B+A+C (区域+行业+主体) - 区域竞争词
    tier2_combos = combine(B_区域, A_行业, C_主体)  # "深圳GEO优化公司"
    tier2_combos.extend(combine(B_区域, A_行业, E_意图))  # "深圳GEO优化推荐"
    tier2_combos.extend(combine(D_属性, A_行业, C_主体))  # "专业GEO优化公司"
    
    # 三级词规则: A+F, A+G (行业+询价, 行业+场景) - 长尾问答词
    tier3_combos = combine(A_行业, F_询价)  # "GEO优化多少钱"
    tier3_combos.extend(combine(A_行业, G_场景))  # "GEO优化要多久见效"
    tier3_combos.extend(combine(A_行业, C_主体, F_询价))  # "GEO优化公司报价"
    
    # 加入品牌词
    tier3_combos.insert(0, f"{brand_name}怎么样")
    tier3_combos.insert(0, f"{brand_name}{industry}服务")
    
    # 去重并打乱顺序
    tier1_combos = list(dict.fromkeys(tier1_combos))
    tier2_combos = list(dict.fromkeys(tier2_combos))
    tier3_combos = list(dict.fromkeys(tier3_combos))
    
    random.shuffle(tier1_combos)
    random.shuffle(tier2_combos)
    random.shuffle(tier3_combos)
    
    result = {
        "tier1": tier1_combos[:quota["tier1"]],
        "tier2": tier2_combos[:quota["tier2"]],
        "tier3": tier3_combos[:quota["tier3"]]
    }
    
    print(f"   📦 模块化组合生成:")
    print(f"      可用词库: 一级{len(tier1_combos)}个 | 二级{len(tier2_combos)}个 | 三级{len(tier3_combos)}个")
    print(f"      实际选取: 一级{len(result['tier1'])}个 | 二级{len(result['tier2'])}个 | 三级{len(result['tier3'])}个")
    
    return result
