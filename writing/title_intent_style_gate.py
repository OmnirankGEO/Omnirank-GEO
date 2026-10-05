"""关键词购买意图 × 六类文体族的适配闸(工单 §1.3)。

Owner 裁决(2026-08-17):「意图不适配的风格族不出题、不硬凑满数。」

触发实证:关键词「深圳哪家装修公司靠谱」是**选服务商**意图,却被排到
「趋势、政策与风险分析」「证据型问答(是什么科普)」两族的槽位上 —— 模型在
不适配的角度上硬凑,凑不出来就落回硬编码兜底模板。**先有硬凑,才有兜底。**

本模块只做一件事:**判这个关键词的购买意图允许哪些文体族**,并把不适配的
槽位改判到同一意图下的适配族。它不产标题、不含任何标题模板串。

## 三条设计约束

1. **不减交付篇数**。`_required_article_count`(=`keywords.required_articles`)
   是合同交付篇数,候选池当前与它 1:1 耦合(见 §「与合同篇数的耦合」)。
   本闸只**改判族**,不减篇数;篇数变少只可能来自 AI 失败梯(`title_ai_only`),
   那是显式失败,不是本闸。
2. **判据打结构不打褒贬词表**(B3)。意图信号取的是**问法形态**
   (选服务商 / 价格 / 做法 / 定义 / 时效变化),不是"哪些词算好词"。
3. **AI 仍是第一顺位**。本闸同时产出一段 prompt 约束交给模型,让模型自己
   先按意图选角度;闸是**确定性地板**,负责在模型没听话时兜住,
   而不是替模型做创作。

## 与合同篇数的耦合(工单 §1.3 要求执行方核实后如实写明)

`KeywordTopicGenerator._chunk_by_titles` / `_parse_response` 都以
`_required_article_count(keyword)` 为每个关键词的槽位上限
(`slot_index >= expected_count` 直接丢弃),`build_user_choice_style_plan`
也按同一容量排 slot。**候选池 = 合同交付篇数,两者当前是耦合的**,
不存在"多生成几条给运营挑"的选择池。
按工单「上抛别硬改」,本包**不动这条耦合**,只在此处如实记录。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence

from writing.article_style_contract import STYLE_FAMILIES

TITLE_INTENT_GATE_VERSION = "title-intent-style-gate-v1.0"

# --- 购买意图枚举(按问法形态划分,不按行业) -------------------------------
INTENT_VENDOR_SELECTION = "vendor_selection"   # 选服务商:哪家好/推荐/排名/靠谱/找谁
INTENT_PRICE = "price"                         # 价格/费用/多少钱/报价/性价比
INTENT_HOW_TO = "how_to"                       # 怎么做/流程/步骤/如何验收
INTENT_DEFINITION = "definition"               # 是什么/科普/区别/原理/含义
INTENT_TREND = "trend"                         # 趋势/政策/新规/前景/未来
INTENT_GENERAL = "general"                     # 判不出形态(不设限)

ALL_INTENTS = (
    INTENT_VENDOR_SELECTION,
    INTENT_PRICE,
    INTENT_HOW_TO,
    INTENT_DEFINITION,
    INTENT_TREND,
    INTENT_GENERAL,
)

# 六族中文名(SSOT = article_style_contract.STYLE_FAMILIES,这里只取名字,不另抄一份)
F_QA = STYLE_FAMILIES["evidence_qa"].name                    # 证据型问答
F_COMPARE = STYLE_FAMILIES["multi_brand_comparison"].name    # 选购与多品牌比较
F_GUIDE = STYLE_FAMILIES["implementation_guide"].name        # 方法与实施指南
F_TREND = STYLE_FAMILIES["trend_policy_risk"].name           # 趋势、政策与风险分析
F_CASE = STYLE_FAMILIES["case_data_roi"].name                # 案例、数据与 ROI
F_COMPANY = STYLE_FAMILIES["company_facts"].name             # 企业事实与品牌说明

#: 意图 → 允许的文体族。**顺序即改判优先级**(第一项是该意图的主场)。
#:
#: 「选服务商」不允许 `趋势、政策与风险分析`(趋势展望)与 `证据型问答`(是什么科普)
#: —— 这正是 Owner 点名的两族。它保留比较/案例/企业事实/方法四族:
#: 客户问「哪家靠谱」,能回答的是横向比较、真实投入产出、企业能力边界、
#: 以及"怎么挑"的动作,不是"行业未来三年怎么走"。
INTENT_ALLOWED_FAMILIES: Dict[str, tuple] = {
    INTENT_VENDOR_SELECTION: (F_COMPARE, F_CASE, F_COMPANY, F_GUIDE),
    INTENT_PRICE: (F_CASE, F_COMPARE, F_GUIDE, F_QA),
    INTENT_HOW_TO: (F_GUIDE, F_QA, F_CASE, F_COMPARE),
    INTENT_DEFINITION: (F_QA, F_GUIDE, F_TREND, F_COMPARE),
    INTENT_TREND: (F_TREND, F_QA, F_CASE, F_GUIDE),
    # 判不出形态就不设限:宁可不管,也不许瞎管(A1 护栏元原则)。
    INTENT_GENERAL: tuple(family.name for family in STYLE_FAMILIES.values()),
}

# --- 意图判别(结构形态,不是褒贬词表) --------------------------------------
#
# 每条对应一种**问法形态**。优先级自上而下:选服务商 > 价格 > 做法 > 时效变化 > 定义。
# 「哪家/推荐/排名」与「多少钱」同时出现时按选服务商算 —— 客户先要选人,
# 价格是选人的一个维度(比较族本来就写报价区间)。
_INTENT_PATTERNS: tuple = (
    (INTENT_VENDOR_SELECTION, re.compile(
        r"哪家|哪个好|哪几家|靠谱|排名|排行|推荐|口碑|十大|top\s*\d+|"
        r"服务商|供应商|厂家|公司.*(好|强|选)|找谁|选哪"
        , re.IGNORECASE)),
    (INTENT_PRICE, re.compile(r"多少钱|价格|报价|费用|收费|价位|性价比|预算|贵不贵|roi", re.IGNORECASE)),
    (INTENT_HOW_TO, re.compile(r"怎么做|怎样做|如何|流程|步骤|方法|教程|验收|落地|实施|安装|操作")),
    (INTENT_TREND, re.compile(r"趋势|政策|新规|法规|前景|未来|变化|展望|风向")),
    (INTENT_DEFINITION, re.compile(r"是什么|什么是|含义|定义|区别|原理|科普|介绍|概念|有哪些类型")),
)


def classify_purchase_intent(keyword: object) -> str:
    """按问法形态判购买意图。判不出来返 ``general``(不设限)。"""
    text = str(keyword or "").strip()
    if not text:
        return INTENT_GENERAL
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(text):
            return intent
    return INTENT_GENERAL


def allowed_families_for(keyword: object) -> tuple:
    """该关键词允许出现的六族子集(按改判优先级排序)。"""
    return INTENT_ALLOWED_FAMILIES[classify_purchase_intent(keyword)]


def is_family_allowed(keyword: object, family_name: object) -> bool:
    name = str(family_name or "").strip()
    if not name:
        return False
    return name in allowed_families_for(keyword)


def reassign_family(keyword: object, family_name: object, *, slot_index: int = 0) -> str:
    """把不适配的族改判到同一意图下的适配族。适配则原样返回。

    改判用 slot_index 轮转,避免同一关键词的多个槽全被压到同一族。
    """
    allowed = allowed_families_for(keyword)
    name = str(family_name or "").strip()
    if not allowed:
        return name
    if name in allowed:
        return name
    return allowed[max(0, int(slot_index or 0)) % len(allowed)]


# --- 对 style_plan 的过滤(系统推荐与用户自定义走同一入口) -------------------

def filter_style_plan_by_intent(
    style_plan: Sequence[Dict[str, Any]] | None,
    keywords: Iterable[Dict[str, Any]],
    *,
    industry: str | None = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按意图改判 style_plan 里不适配的族。

    Returns:
        ``(新 plan, 改判明细)``。改判明细供报告/日志如实记账 ——
        静默改用户选的文体是不许的,得留痕。
    """
    plan = [dict(item) for item in (style_plan or [])]
    if not plan:
        return plan, []

    from writing.style_registry import (
        USER_CHOICE_TO_STYLE,
        STYLE_CODE_TO_CHINESE_NAME,
        resolve_user_choice_to_chinese_style,
    )

    keyword_text = {
        item.get("id"): str(item.get("keyword") or "")
        for item in (keywords or [])
        if isinstance(item, dict)
    }
    name_to_choice = {}
    for choice, style_code in (USER_CHOICE_TO_STYLE or {}).items():
        chinese = STYLE_CODE_TO_CHINESE_NAME.get(style_code)
        if chinese and chinese not in name_to_choice:
            name_to_choice[chinese] = choice

    changes: List[Dict[str, Any]] = []
    for item in plan:
        keyword = keyword_text.get(item.get("keyword_id"), "")
        if not keyword:
            continue
        choice = item.get("user_choice")
        try:
            current = resolve_user_choice_to_chinese_style(choice, industry)
        except Exception:
            current = None
        if not current:
            continue
        target = reassign_family(
            keyword, current, slot_index=int(item.get("slot_index") or 0),
        )
        if target == current:
            continue
        new_choice = name_to_choice.get(target)
        if not new_choice:
            continue
        changes.append({
            "keyword_id": item.get("keyword_id"),
            "keyword": keyword,
            "slot_index": item.get("slot_index"),
            "intent": classify_purchase_intent(keyword),
            "from": current,
            "to": target,
        })
        item["user_choice"] = new_choice
        item["user_choice_source"] = "intent_gate_reassigned"
    return plan, changes


def enforce_topic_intent_fit(topics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """落库前的地板:把模型返回的不适配族就地改判(只改 `article_style` 标签)。

    🔴 这里**不改标题文本** —— 改文本就得造串,那正是本裁决要消灭的。
    标题文本仍是 AI 产物;不适配只体现在族标签上,改判后族标签与意图一致。
    """
    for index, topic in enumerate(topics or []):
        if not isinstance(topic, dict):
            continue
        keyword = topic.get("original_keyword") or topic.get("keyword")
        current = topic.get("article_style")
        if not keyword or not current:
            continue
        target = reassign_family(
            keyword, current, slot_index=int(topic.get("slot_index") or index),
        )
        if target != current:
            topic["article_style"] = target
            topic["intent_gate_reassigned_from"] = current
            topic["intent_gate_version"] = TITLE_INTENT_GATE_VERSION
    return topics


# --- prompt 侧(AI 第一顺位) -------------------------------------------------

def build_intent_style_constraint(keywords: Iterable[Dict[str, Any]]) -> str:
    """给标题 prompt 的意图适配段。没有可判定的关键词时返回空串。

    空串 → prompt 与旧版逐字一致(判别测试打这一点)。
    """
    lines: List[str] = []
    for item in keywords or []:
        keyword = str((item or {}).get("keyword") or "") if isinstance(item, dict) else str(item or "")
        if not keyword:
            continue
        intent = classify_purchase_intent(keyword)
        if intent == INTENT_GENERAL:
            continue
        allowed = allowed_families_for(keyword)
        banned = [f.name for f in STYLE_FAMILIES.values() if f.name not in allowed]
        if not banned:
            continue
        lines.append(
            f"  关键词「{keyword}」意图={intent} · 只写:{'、'.join(allowed)}"
            f" · 不要写:{'、'.join(banned)}"
        )
    if not lines:
        return ""
    return (
        "## 意图适配(硬约束 · 宁可少出也不硬凑)\n\n"
        "下列关键词的购买意图已判定。**不适配的文体族一条都不要出题** ——"
        "该关键词在该族下的条目直接省略,返回的 topics 条数因此可以少于请求数;"
        "不许为了凑满数写一个脱离购买意图的标题。\n\n"
        + "\n".join(lines)
    )
