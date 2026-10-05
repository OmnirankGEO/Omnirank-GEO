"""
brand_field_suggester — CTO-15.3 对话式补齐引擎 v2 (2026-04-19)

老板元指令(CLAUDE.md 新增):
  **永远不中断对话** — GEO 工具遇到品牌/档案/字段缺失,禁止返 {error: "未选择品牌"} 类阻断.
  必须走 4 按钮选择:
    ① ✅ AI 推断值继续(免费)
    ② 🤖 AI 联网查全套(扣 130 积分,调 autofill_brand)
    ③ ✏️ 自己填(免费,跳 /my-clients/:id)
    ④ ❌ 取消
  对话内闭环,不让用户离开 /c/chat.

设计原则:
  - 一行接入: `guard = await ensure_brand_fields(ctx, required=['industry'])` 返非 None 就直接 return
  - 6 字段支持: industry / business / city / keywords / target_users / description
  - 4 按钮 confirm_card(推断/联网查/手填/取消) + 降级 action_card(推不出)
  - 多字段批量检测: 一次补齐所有缺失(不强迫用户补 5 次)
  - 代理端(user_mode!='c')默认不启用(保原行为,代理需要精确数据)

对应任务指令: docs/AI-CONTEXT/任务指令_CTO-15.3_P0_对话式补齐.md(+ 老板扩展方向)
接入清单: 17 个 GEO C 端工具(commit 25/26/27 分批接入)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

logger = logging.getLogger("GEO-BrandFieldSuggester")


# ==================== 字段元信息 ====================

# 字段 → (用户可见名, 是否核心必填, 推断难度)
FIELD_META: dict[str, dict[str, Any]] = {
    "industry": {"label": "行业", "core": True, "hint": "如 GEO 服务/奶茶/装修/SaaS"},
    "business": {"label": "业务描述", "core": True, "hint": "一句话介绍主营业务"},
    "city": {"label": "城市", "core": False, "hint": "如 深圳 / 全国线上"},
    "keywords": {"label": "核心关键词", "core": False, "hint": "3-5 个搜索词"},
    "target_users": {"label": "目标客群", "core": False, "hint": "主要服务谁"},
    "description": {"label": "品牌介绍", "core": False, "hint": "一段话介绍品牌"},
    # [CTO-15.9 2026-04-25 M1c T5] market_insight 5 字段(E 组) 对话式补齐
    "service_scope": {"label": "服务地域范围", "core": False, "hint": "local/national/hybrid"},
    "local_competitors": {"label": "本地/同区域竞品", "core": False, "hint": "2-3 家主要竞品名"},
    "authority_sources": {"label": "行业权威信息源", "core": False, "hint": "3-5 个权威媒体/协会/报告"},
    "hot_formats": {"label": "AI 搜索答案常见形式", "core": False, "hint": "3-5 种(攻略/榜单/评测)"},
    "my_differentiation": {"label": "差异化核心", "core": False, "hint": "<=60 字一句话"},
    # [WO_267] 行业大类(字典 key):对话里确认一次,此后行业判定以它为准(source=override)
    "industry_category": {"label": "行业大类", "core": False, "hint": "从大类列表里选一个(光伏/储能归「新能源」)"},
}

SUPPORTED_FIELDS = set(FIELD_META.keys())


# ==================== LLM 推断 Prompt ====================

INDUSTRY_PROMPT = """从下方信息推断品牌的行业归属(具体行业,不是泛化词)。

品牌名: {brand_name}
公司名: {company_name}
业务描述: {business}
城市: {city}

推断规则:
- 必须具体行业(如 "餐饮"/"GEO 服务"/"美容/SPA"/"SaaS/CRM"/"奶茶"),禁止"相关服务"/"通用商业"等泛化
- 信息严重不足(只有品牌名且推不出)→ needs_more_info=true
- confidence 严格: 强=品牌名+业务描述双命中; 中=单边命中; 弱=只从品牌名猜

严格 JSON(无 markdown):
{{"suggested_industry":"<=20 字 或 null","confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


BUSINESS_PROMPT = """基于品牌名推断可能的业务描述(一句话 <=40 字)。

品牌名: {brand_name}
公司名: {company_name}
城市: {city}

- 品牌名太泛(如"小明的店"/"ABC 公司") → needs_more_info=true
- 不要编造细节,只从命名线索推断大方向

严格 JSON: {{"suggested_business":"<=40 字 或 null","confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


CITY_PROMPT = """从品牌名和公司名推断品牌所在城市(中国城市名)。

品牌名: {brand_name}
公司名: {company_name}
业务描述: {business}

- 公司名带"(深圳)""北京"等 → 明确返对应城市
- 无线索 → suggested_city="全国",confidence="弱"(不报 needs_more_info,全国也是合理值)

严格 JSON: {{"suggested_city":"城市名 或 '全国'","confidence":"强|中|弱","reasoning":"<=20 字","needs_more_info":false}}"""


KEYWORDS_PROMPT = """为品牌推断 3-5 个核心业务关键词(搜索意图词,不是品牌名)。

品牌名: {brand_name}
行业: {industry}
业务: {business}
城市: {city}

规则:
- 关键词必须是用户搜索会用的词(如"奶茶店"/"GEO 服务"/"深圳装修"),不是品牌名
- 优先业务词(品牌词用户少搜)
- 每词 3-8 字

严格 JSON: {{"suggested_keywords":["词1","词2","词3"] 或 null,"confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


TARGET_USERS_PROMPT = """基于品牌业务推断目标客群(一句话 <=40 字)。

品牌名: {brand_name}
行业: {industry}
业务: {business}

- "中小企业 SaaS 用户"/"25-40 岁白领女性"/"深圳本地装修需求家庭"等
- 信息不足 → needs_more_info=true

严格 JSON: {{"suggested_target_users":"<=40 字 或 null","confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


DESCRIPTION_PROMPT = """为品牌生成一段简短介绍(60-120 字)。

品牌名: {brand_name}
行业: {industry}
业务: {business}
城市: {city}

- 介绍包含: 做什么 + 面向谁 + 差异点
- 禁编造具体数据(不说"500+ 客户"/"10 年经验"等)
- 信息不足 → needs_more_info=true

严格 JSON: {{"suggested_description":"<=120 字 或 null","confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


# [CTO-15.9 2026-04-25 M1c T5] market_insight 5 字段 prompt
SERVICE_SCOPE_PROMPT = """基于品牌业务推断服务地域范围(local/national/hybrid 三选一)。

品牌名: {brand_name}
行业: {industry}
业务: {business}
城市: {city}

判定规则:
- local: 线下门店 / 本地服务(餐饮/装修/美容/诊所等) · 城市有明确值
- national: 纯线上 / SaaS / 电商 / 知识付费 · 不分地域
- hybrid: 本地门店 + 线上外卖/快递 / 全国连锁带本地店
- 信息不足 → suggested_service_scope="local",confidence="弱"(local 是最常见的保守估)

严格 JSON: {{"suggested_service_scope":"local|national|hybrid","confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


LOCAL_COMPETITORS_PROMPT = """为品牌推断 2-3 家同区域 / 同业态主要竞品。

品牌名: {brand_name}
行业: {industry}
业务: {business}
城市: {city}

规则:
- 只推公开知名竞品 · 不编造
- 本地品牌优先同城(如"罗平装修" → 罗平县其他装修公司)
- 信息不足或无法核实 → needs_more_info=true

严格 JSON: {{"suggested_local_competitors":["竞品1","竞品2"] 或 null,"confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


AUTHORITY_SOURCES_PROMPT = """为品牌所在行业推断 3-5 个权威信息源(媒体/协会/报告/政府网站)。

行业: {industry}
业务: {business}
城市: {city}

规则:
- 优先行业权威(中国 XX 协会 / 艾瑞 / 易观 / 36氪等)
- 装修行业 → 中国建筑装饰协会 / 安居客 / 好好住
- 餐饮行业 → 中国烹饪协会 / 大众点评 / 中国餐饮报告
- 教育培训 → 教育部 / 新东方在线 / 艾瑞教育研究报告
- 信息不足 → 返 []

严格 JSON: {{"suggested_authority_sources":["来源1","来源2","来源3"] 或 [],"confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


HOT_FORMATS_PROMPT = """为品牌所在行业推断 3-5 种 AI 搜索答案里最常见的内容形式。

行业: {industry}
业务: {business}

规则:
- 具体形式(如"攻略"/"榜单"/"评测"/"避坑指南"/"案例实拍")
- 同行业有明显偏好(装修 → 避坑攻略;美妆 → 测评;SaaS → 对比评测)
- 信息不足 → 返 []

严格 JSON: {{"suggested_hot_formats":["形式1","形式2"] 或 [],"confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


MY_DIFFERENTIATION_PROMPT = """为品牌一句话提炼差异化核心(<=60 字)。

品牌名: {brand_name}
行业: {industry}
业务: {business}
城市: {city}

规则:
- 突出 3 大核心卖点(如:祖传秘方 + 本地 30 年 + 透明操作)
- 具体 · 避免空话("专业""品质""用心"类禁用)
- 信息不足只从品牌名推 → needs_more_info=true

严格 JSON: {{"suggested_my_differentiation":"<=60 字 或 null","confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


INDUSTRY_CATEGORY_PROMPT = """从下面的大类清单里,为品牌选**一个**行业大类。只许从清单里选;判不出就回 none。

品牌名: {brand_name}
公司名: {company_name}
行业(自由文本): {industry}
业务描述: {business}

大类清单(key: 名称 · 细分):
{category_options}

规则:
- 只能回清单里的 key(英文)或 none;不许自造、不许回中文名
- 品牌名 / 业务里出现 光伏 / 储能 / 充电桩 这类强信号时以它为准,不要被行业列里的「建筑」带偏
- confidence: 强=行业+业务双命中;中=单边命中;弱=只从品牌名猜

严格 JSON(无 markdown):
{{"suggested_industry_category":"<key 或 none>","confidence":"强|中|弱","reasoning":"<=30 字","needs_more_info":false}}"""


FIELD_PROMPTS = {
    "industry": INDUSTRY_PROMPT,
    "business": BUSINESS_PROMPT,
    "city": CITY_PROMPT,
    "keywords": KEYWORDS_PROMPT,
    "target_users": TARGET_USERS_PROMPT,
    "description": DESCRIPTION_PROMPT,
    # M1c T5 新增
    "service_scope": SERVICE_SCOPE_PROMPT,
    "local_competitors": LOCAL_COMPETITORS_PROMPT,
    "authority_sources": AUTHORITY_SOURCES_PROMPT,
    "hot_formats": HOT_FORMATS_PROMPT,
    "my_differentiation": MY_DIFFERENTIATION_PROMPT,
    "industry_category": INDUSTRY_CATEGORY_PROMPT,   # WO_267:闭集,选项由字典生成
}

# 字段 → JSON 结果里取值的 key
FIELD_RESULT_KEY = {
    "industry": "suggested_industry",
    "business": "suggested_business",
    "city": "suggested_city",
    "keywords": "suggested_keywords",
    "target_users": "suggested_target_users",
    "description": "suggested_description",
    # M1c T5 新增
    "service_scope": "suggested_service_scope",
    "local_competitors": "suggested_local_competitors",
    "authority_sources": "suggested_authority_sources",
    "hot_formats": "suggested_hot_formats",
    "my_differentiation": "suggested_my_differentiation",
    "industry_category": "suggested_industry_category",   # WO_267
}


# ==================== 工具函数 ====================

def _strip_json_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 2:
            t = parts[1]
            if t.startswith("json"):
                t = t[4:]
    return t.strip()


async def _llm_infer(prompt: str) -> dict[str, Any] | None:
    """调 advisor_flash + JSON 解析。异常返 None。"""
    try:
        from services.llm.advisor_llm import advisor_flash
    except Exception as e:  # pragma: no cover
        logger.warning(f"[brand_suggester] advisor_flash import 失败: {e}")
        return None
    try:
        raw = await advisor_flash(prompt, history=[])
        text = raw.strip() if isinstance(raw, str) else (raw or {}).get("text", "").strip()
        text = _strip_json_fence(text)
        return json.loads(text)
    except Exception as e:
        logger.warning(f"[brand_suggester] LLM 推断失败: {e}")
        return None


def _safe_str(v: Any, max_len: int = 80) -> str:
    if v is None:
        return ""
    return str(v).strip()[:max_len]


def _field_missing(brand: dict, profile: dict | None, field: str) -> bool:
    """检查字段是否缺失(空串/None/空 list 都算缺失)。

    M1c T5 扩展:market_insight 3 键(authority_sources/hot_formats/my_differentiation)
    从 profile.industry_brief JSONB 的顶层读取(非 profile 顶层字段)。
    """
    profile = profile or {}
    # [WO_267] 行业大类只认字典**新 key**:存量旧中文名(房产家居/科技服务…)是旧子串匹配器算的,
    #   不是用户选的 —— 当作「还没确认过」,弹一次确认卡。
    if field == "industry_category":
        from services.industry_taxonomy import brand_category_override
        return brand_category_override(brand) is None
    val = brand.get(field) if brand else None
    if not val:
        val = profile.get(field)

    # keywords 特殊别名: 代理端存 core_keywords,先兜底再判空
    if field == "keywords" and not val:
        val = (brand or {}).get("core_keywords") or profile.get("core_keywords")

    # [CTO-15.9 M1c T5] market_insight 3 键从 industry_brief JSONB flatten
    if field in ("authority_sources", "hot_formats", "my_differentiation") and not val:
        brief = profile.get("industry_brief")
        if isinstance(brief, dict):
            val = brief.get(field)
        elif isinstance(brief, str) and brief.strip():
            try:
                parsed = json.loads(brief)
                if isinstance(parsed, dict):
                    val = parsed.get(field)
            except Exception:
                pass

    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    if isinstance(val, list) and len(val) == 0:
        return True
    return False


def detect_missing_fields(
    brand: dict | None,
    profile: dict | None,
    required: Iterable[str],
) -> list[str]:
    """批量检测缺失字段。返回缺失字段名列表(按 required 顺序)。"""
    brand = brand or {}
    profile = profile or {}
    missing = []
    for f in required:
        if f not in SUPPORTED_FIELDS:
            logger.warning(f"[brand_suggester] 不支持字段: {f}")
            continue
        if _field_missing(brand, profile, f):
            missing.append(f)
    return missing


# ==================== 单字段推断 + confirm_card ====================

async def suggest_brand_field(
    ctx,
    field_name: str,
    brand: dict | None = None,
    profile: dict | None = None,
) -> dict:
    """LLM 推断单字段 + 返 4 按钮 confirm_card。

    返回:
      - needs_confirmation=True + 4 按钮 confirm_card(含 action_payload 写回 profile)
      - needs_confirmation=False + follow_up_question(需调用方走对话引导)
      - needs_confirmation=False + reason(LLM 失败,调用方走 action_card 降级)
    """
    if field_name not in SUPPORTED_FIELDS:
        return {
            "needs_confirmation": False,
            "reason": f"unsupported field: {field_name}",
            "field_name": field_name,
        }

    brand = brand or {}
    profile = profile or {}

    brand_name = _safe_str(brand.get("name") or brand.get("company_name"), 60) or "您的品牌"
    company_name = _safe_str(brand.get("company_name") or brand.get("name"), 80)
    business = _safe_str(
        brand.get("business") or profile.get("business") or brand.get("description"), 200
    )
    city = _safe_str(brand.get("city") or profile.get("city"), 20)
    industry = _safe_str(brand.get("industry") or profile.get("industry"), 40)

    deps = getattr(ctx, "deps", None)
    brand_id = brand.get("id") or getattr(deps, "current_brand_id", None)
    profile_id = profile.get("id") or getattr(deps, "current_profile_id", None)

    if field_name == "industry_category":
        return await _suggest_industry_category(
            brand=brand, industry=industry, business=business,
            brand_name=brand_name, company_name=company_name, brand_id=brand_id,
        )

    prompt = FIELD_PROMPTS[field_name].format(
        brand_name=brand_name,
        company_name=company_name or "未提供",
        business=business or "未提供",
        city=city or "未提供",
        industry=industry or "未提供",
    )
    parsed = await _llm_infer(prompt)

    # industry 专属: needs_more_info=true 时降级去问 business(二段式)
    if field_name == "industry" and parsed and parsed.get("needs_more_info"):
        logger.info("[brand_suggester] industry needs_more_info → 降级问 business")
        return await suggest_brand_field(ctx, "business", brand, profile)

    result_key = FIELD_RESULT_KEY[field_name]
    suggested_raw = (parsed or {}).get(result_key) if parsed else None

    # LLM 推断失败 or 显式 needs_more_info
    if not parsed or parsed.get("needs_more_info") or not suggested_raw:
        meta = FIELD_META[field_name]
        return {
            "needs_confirmation": False,
            "reason": f"{meta['label']}推断失败或信息不足",
            "field_name": field_name,
            "follow_up_question": (
                f"我从现有信息推不出「{meta['label']}」。能告诉我吗?\n"
                f"({meta['hint']})"
            ),
        }

    # 格式化推断值 — list 类型(keywords)要特殊处理
    if isinstance(suggested_raw, list):
        suggested = [_safe_str(x, 40) for x in suggested_raw if x][:5]
        suggested_display = "、".join(suggested)
    else:
        suggested = _safe_str(suggested_raw, 120)
        suggested_display = suggested

    confidence = (parsed or {}).get("confidence") or "中"
    reasoning = _safe_str((parsed or {}).get("reasoning"), 60)

    return _build_field_confirm_card(
        field_name=field_name,
        suggested=suggested,
        suggested_display=suggested_display,
        confidence=confidence,
        reasoning=reasoning,
        brand_name=brand_name,
        company_name=company_name,
        brand_id=brand_id,
        profile_id=profile_id,
    )


def _build_field_confirm_card(
    *,
    field_name: str,
    suggested: Any,
    suggested_display: str,
    confidence: str,
    reasoning: str,
    brand_name: str,
    company_name: str,
    brand_id: str | int | None,
    profile_id: str | int | None,
    show_autofill_hint: bool = True,
) -> dict:
    """单字段 3 按钮 confirm_card(修 commit 29 设计 bug)。

    CTO-15.3 commit 30 hotfix:
    - 旧对话确认卡 组件约束: 1 confirm + 1 cancel + 1 linkAction = 3 按钮
    - 之前 cancel_label 写"🤖 AI 联网查全套"但点击行为是取消 → 误导用户
    - 之前 title 仅"需要确认行业",确认后 AI 看到 `[已确认] 需要确认行业` 不会自动重启诊断 → 断链

    修复:
      1. title 含行为语义: "把{label}设为「{值}」并继续" → AI 下轮看到 `[已确认] 把行业设为GEO并继续` 会重调工具
      2. cancel_label 回到真取消语义 "❌ 不是,我详细说"
      3. autofill 路径挪到 description 提示 + _build_inference_failed_card 的 action_card 里(那里 send_message 行为正确)
      4. 按钮映射:
         - ✅ confirm → action_payload PATCH profile.{field} = suggested → setDecided('confirmed') + 发 `[已确认]{title}` → AI 重调工具
         - ❌ cancel → 用户重新描述 → AI 重新推断
         - ✏️ link → navigate /my-clients/:id 自己填
    """
    meta = FIELD_META[field_name]
    label = meta["label"]

    conf_hint = ""
    if confidence == "弱":
        conf_hint = "\n\n⚠️ AI 不太确定,如果不对请点「❌ 不是」或「✏️ 我自己改」"

    # 名字部分
    brand_display = f"「{brand_name}」"
    if company_name and company_name != brand_name:
        brand_display += f"(公司名: {company_name})"

    # autofill 提示放 description(让用户知道还有此路径)
    autofill_hint = (
        "\n\n💡 或者你也可以直接对我说「让 AI 帮我联网查全套」,"
        "我会调用品牌填充工具(扣 130 积分,一次性补齐行业/关键词/竞品/介绍)。"
    )

    description = (
        f"我看到您的品牌是{brand_display},但{label}字段还没填。\n\n"
        f"根据已有信息我推断{label}是:\n\n"
        f"  【{suggested_display}】\n\n"
        + (f"依据: {reasoning}\n\n" if reasoning else "")
        + "点 ✅ 我会写入品牌信息并继续,点 ❌ 请重新告诉我。"
        + conf_hint
        + (autofill_hint if show_autofill_hint else "")
    )

    # action_payload: 写回 profile(首选)或 brand(fallback)
    if profile_id:
        action_payload = {
            "endpoint": f"/api/profiles/{profile_id}/quick-update",
            "method": "PATCH",
            "body": {"field": field_name, "value": suggested},
        }
    elif brand_id:
        action_payload = {
            "endpoint": f"/api/brands/{brand_id}",
            "method": "PUT",
            "body": {field_name: suggested},
        }
    else:
        action_payload = None

    # 关键修复: title 含"继续"语义,AI 下轮 `[已确认]` 消息会自然触发重调工具
    # 例: title="把行业设为「GEO 服务」并继续" → onConfirm 发 `[已确认] 把行业设为「GEO 服务」并继续`
    # → LLM 理解用户同意 + 想继续之前的操作 → 重调 run_diagnosis
    title = f"把{label}设为「{suggested_display[:30]}」并继续"

    card: dict[str, Any] = {
        "needs_confirmation": True,
        "confirm_card": True,
        "confirm_id": f"brand_field_confirm:{brand_id or 'none'}:{field_name}",
        "title": title,
        "description": description,
        "confirm_label": "✅ 对,继续",
        "cancel_label": "❌ 不是,我详细说",
        "_meta": {
            "suggested_value": suggested,
            "confidence": confidence,
            "field_name": field_name,
        },
    }
    if action_payload:
        card["action_payload"] = action_payload
    card["link_action"] = {
        "label": "✏️ 我自己改(免费)",
        "path": f"/my-clients/{brand_id}" if brand_id else "/my-clients",
    }
    return card


async def _suggest_industry_category(
    *, brand: dict, industry: str, business: str,
    brand_name: str, company_name: str, brand_id: str | int | None,
) -> dict:
    """[WO_267 §5] 行业大类的确认卡:① 字典判(免费、可复现,吃品牌名/备注/种子词)
    ② 判不出才让 LLM 在**闭集**里选(只许回字典 key,回别的一律当没判出)。

    三个按钮 = ✅ 用推断值(写 brands.industry_category = key)/ ✏️ 自己从大类里选 / ❌ 取消。
    不提示「AI 联网查全套」—— 大类是从固定清单里选,不需要付费查询。
    """
    from services.industry_taxonomy import (
        OTHER_KEY, category_keys, get_category, load_taxonomy, resolve_industry,
    )

    ctx_brand = dict(brand or {})
    ctx_brand.pop("industry_category", None)      # 判定时不拿存量旧值当 override
    res = resolve_industry(industry or "", brand=ctx_brand)
    key, confidence, reasoning = None, "中", ""
    if res.category_key != OTHER_KEY:
        key = res.category_key
        confidence = "强" if res.confidence >= 0.9 else ("中" if res.confidence >= 0.7 else "弱")
        if res.matched_alias:
            reasoning = _safe_str(f"命中「{res.matched_alias}」", 60)
    else:
        options = "\n".join(
            f"{c.key}: {c.name}" + (" · " + "/".join(n for n, _ in c.subcategories) if c.subcategories else "")
            for c in load_taxonomy().categories if c.key != OTHER_KEY
        )
        parsed = await _llm_infer(FIELD_PROMPTS["industry_category"].format(
            brand_name=brand_name, company_name=company_name or "未提供",
            industry=industry or "未提供", business=business or "未提供",
            category_options=options,
        ))
        cand = _safe_str((parsed or {}).get(FIELD_RESULT_KEY["industry_category"]), 40)
        if parsed and not parsed.get("needs_more_info") and cand in category_keys() and cand != OTHER_KEY:
            key = cand
            confidence = (parsed or {}).get("confidence") or "弱"
            reasoning = _safe_str((parsed or {}).get("reasoning"), 60)

    if not key:
        return {
            "needs_confirmation": False,
            "reason": "行业大类推断失败或信息不足",
            "field_name": "industry_category",
            "follow_up_question": (
                "我判断不出品牌属于哪个行业大类。可以直接告诉我(比如「新能源」「工业制造」),"
                "或者去品牌资料里从大类列表选一个。"
            ),
        }

    cat = get_category(key)
    card = _build_field_confirm_card(
        field_name="industry_category", suggested=key, suggested_display=cat.name if cat else key,
        confidence=confidence, reasoning=reasoning, brand_name=brand_name,
        company_name=company_name, brand_id=brand_id, profile_id=None, show_autofill_hint=False,
    )
    # 大类存在 brands 上(档案表没有这一列)—— 固定走带 RBAC 的 PUT /api/my-clients/{id}
    card.pop("action_payload", None)
    if brand_id:
        card["action_payload"] = {
            "endpoint": f"/api/my-clients/{brand_id}",
            "method": "PUT",
            "body": {"industry_category": key},
        }
    card["confirm_label"] = "✅ 用这个大类"
    card["cancel_label"] = "❌ 取消"
    card["link_action"] = {
        "label": "✏️ 自己从大类里选(免费)",
        "path": f"/my-clients/{brand_id}" if brand_id else "/my-clients",
    }
    return card


# ==================== 多字段批量补齐 + 统一接入点 ====================

async def ensure_brand_fields(
    ctx,
    required: Iterable[str],
    brand: dict | None = None,
    profile: dict | None = None,
) -> dict | None:
    """🎯 工具接入点: 一行保证必填字段齐全。

    用法(GEO 工具):
        guard = await ensure_brand_fields(ctx, required=['industry'])
        if guard:
            return guard  # 缺字段 → 返 confirm_card/action_card,工具直接 return
        # 字段齐全,继续原流程

    Args:
        ctx: RunContext[OmniRankDeps]
        required: 必填字段列表 (industry/business/city/keywords/target_users/description)
        brand: 已有的 brand dict(若 None 会自动 fetch)
        profile: 已有的 profile dict(若 None 会自动 fetch)

    Returns:
        None: 字段齐全,调用方继续原流程
        dict: 补齐卡片(confirm_card/action_card),调用方直接 return 给 agent

    设计:
        - C 端 (user_mode='c') 启用;代理端 (user_mode='agent') 不启用(代理要精确输入)
        - brand 为空 → 走 create_brand 引导(用户已在 L0 1-brand 策略下必有一个,这里防御)
        - 多字段缺 → 一次性引导卡(不逐个推断,用户体验碎)
        - LLM 推断失败 → 返 action_card 引导用户走 3 按钮(autofill / 自己填 / 对话补问)
    """
    deps = getattr(ctx, "deps", None)
    user_mode = getattr(deps, "user_mode", "") or ""

    # 代理端跳过(保原行为,不改代理工作流)
    if user_mode != "c":
        return None

    # 自动 fetch brand(若未传)
    if brand is None:
        try:
            brand = await _fetch_brand(ctx)
        except Exception as e:
            logger.warning(f"[ensure_brand_fields] fetch brand 失败: {e}")
            brand = None

    # 没 brand → 引导建品牌(底部红线,正常不会走到)
    if not brand:
        return _build_no_brand_guidance()

    # 自动 fetch profile(若未传)
    if profile is None:
        try:
            profile = await _fetch_profile(ctx)
        except Exception as e:
            logger.warning(f"[ensure_brand_fields] fetch profile 失败: {e}")
            profile = None

    missing = detect_missing_fields(brand, profile, required)
    if not missing:
        return None  # 齐全,放行

    # 多字段缺 → 组多字段引导卡(不逐个推断,一次性给 3 选项)
    if len(missing) >= 2:
        return _build_multi_field_guidance(brand, missing)

    # 单字段缺 → LLM 推断 + 4 按钮 confirm_card
    first_missing = missing[0]
    suggestion = await suggest_brand_field(ctx, first_missing, brand, profile)

    if suggestion.get("needs_confirmation"):
        return suggestion

    # LLM 推断失败 → 降级 action_card 3 按钮(autofill / 手填 / 对话)
    return _build_inference_failed_card(
        brand=brand,
        missing_field=first_missing,
        follow_up=suggestion.get("follow_up_question", ""),
    )


# ==================== Fetch helpers ====================

async def _fetch_brand(ctx) -> dict | None:
    deps = getattr(ctx, "deps", None)
    if not deps:
        return None
    brand_id = getattr(deps, "current_brand_id", None)

    # 无 brand_id → 兜底查 my-clients / my-brand
    if not brand_id:
        try:
            resp = await deps.http_client.get(
                "/api/my-clients", headers=deps.auth_headers
            )
            if resp.status_code == 200:
                clients = (resp.json() or {}).get("clients", []) or []
                if len(clients) == 1:
                    brand_id = clients[0].get("id")
                    try:
                        deps.current_brand_id = brand_id
                    except Exception:
                        pass
        except Exception:
            pass

        if not brand_id:
            try:
                my_resp = await deps.http_client.get(
                    "/api/my-brand", headers=deps.auth_headers
                )
                if my_resp.status_code == 200:
                    my_brand = (my_resp.json() or {}).get("brand") or {}
                    if my_brand.get("id"):
                        brand_id = my_brand["id"]
                        try:
                            deps.current_brand_id = brand_id
                        except Exception:
                            pass
            except Exception:
                pass

        if not brand_id:
            return None

    try:
        resp = await deps.http_client.get(
            f"/api/brands/{brand_id}", headers=deps.auth_headers
        )
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
        brand = data.get("brand", data) or {}
        brand.setdefault("id", brand_id)
        return brand
    except Exception:
        return None


async def _fetch_profile(ctx) -> dict | None:
    deps = getattr(ctx, "deps", None)
    if not deps:
        return None
    profile_id = getattr(deps, "current_profile_id", None)
    if not profile_id:
        return None
    try:
        resp = await deps.http_client.get(
            f"/api/profiles/{profile_id}", headers=deps.auth_headers
        )
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
        profile = data.get("profile", data) or {}
        profile.setdefault("id", profile_id)
        return profile
    except Exception:
        return None


# ==================== 降级卡片构造 ====================

def _build_no_brand_guidance() -> dict:
    """无 brand 时引导建品牌(理论不会走到 — L0 策略默认必有 1 brand)。"""
    return {
        "action_card": True,
        "title": "还没有品牌,先建一个吧",
        "description": "GEO 工具需要先有品牌。告诉我品牌名,我立即建好。",
        "actions": [
            {"type": "navigate", "label": "去建品牌", "path": "/my-clients"},
        ],
    }


def _build_inference_failed_card(
    *,
    brand: dict,
    missing_field: str,
    follow_up: str,
) -> dict:
    """LLM 推断失败 → 3 按钮 action_card(autofill 130 / 手填 / 对话补)。"""
    meta = FIELD_META[missing_field]
    brand_name = brand.get("name") or "当前品牌"
    brand_id = brand.get("id")

    description = (
        (follow_up + "\n\n") if follow_up else
        f"{meta['label']}信息还没填,我也从现有信息推不出来。怎么继续?\n\n"
    ) + f"{meta['hint']}"

    actions = [
        {
            "type": "send_message",
            "label": "🤖 让 AI 联网查全套(-130 积分)",
            "message": f"[autofill] 帮我用 AI 联网查「{brand_name}」的企业信息",
        },
    ]
    if brand_id:
        actions.append({
            "type": "navigate",
            "label": "✏️ 我自己去品牌详情填(免费)",
            "path": f"/my-clients/{brand_id}",
        })
    actions.append({
        "type": "send_message",
        "label": "💬 直接告诉 AI",
        "message": f"我的{meta['label']}是:",
    })

    return {
        "action_card": True,
        "title": f"需要补{meta['label']}才能继续",
        "description": description,
        "actions": actions,
    }


def _build_multi_field_guidance(
    brand: dict,
    missing: list[str],
) -> dict:
    """多字段缺失 → 3 按钮 action_card(一把梭 autofill / 逐字段手填 / 先补最关键)。

    逻辑: 让用户选一次性授权 AI 查(autofill 扣 130 一键补齐), 或手动去填.
    不再对每个字段单独弹推断卡(用户体验碎).
    """
    brand_name = brand.get("name") or "当前品牌"
    brand_id = brand.get("id")

    missing_labels = [FIELD_META[f]["label"] for f in missing if f in FIELD_META]

    description = (
        f"我看到您的品牌是「{brand_name}」,"
        f"但还有这些关键信息没填:\n\n"
        + "".join(f"  • {label}\n" for label in missing_labels)
        + "\n如何补齐?"
    )

    actions: list[dict[str, Any]] = [
        {
            "type": "send_message",
            "label": "🤖 让 AI 联网查全套(-130 积分,一键补齐)",
            "message": f"[autofill] 帮我用 AI 联网查「{brand_name}」的企业信息",
        },
    ]
    if brand_id:
        actions.append({
            "type": "navigate",
            "label": "✏️ 去品牌详情自己填(免费,更准)",
            "path": f"/my-clients/{brand_id}",
        })
    actions.append({
        "type": "send_message",
        "label": f"💬 先补最关键的{missing_labels[0]}",
        "message": f"我的{missing_labels[0]}是:",
    })

    return {
        "action_card": True,
        "title": f"品牌信息缺 {len(missing)} 项,需要先补齐",
        "description": description,
        "actions": actions,
        "_meta": {"missing_fields": missing},
    }
