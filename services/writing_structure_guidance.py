"""Bounded writing-structure guidance for agent-side article generation.

This service exposes reviewed flywheel strategy as project-scoped guidance.
It never changes the global writing prompt and never trusts guidance text from
the frontend; the caller can only opt in with a boolean.
"""

from __future__ import annotations

import json
import re
from typing import Any

from db.writing_style_flywheel_db import get_active_strategy_version, load_structure_baseline_summary
from services.media_entity_flywheel import normalize_industry_key


DEFAULT_STRUCTURE_TEMPLATES: dict[str, dict[str, Any]] = {
    "ranking_recommendation": {
        "label": "榜单推荐类",
        "name": "筛选标准 + 分层推荐 + 适用人群",
        "rules": ["先说明筛选标准", "再分层推荐对象", "补充适用人群和避坑点", "结尾给中肯选择建议"],
        "guardrails": ["不得写成付费榜单口吻", "不得承诺固定排名"],
    },
    "comparison_review": {
        "label": "对比测评类",
        "name": "同维度对比 + 优缺点 + 决策建议",
        "rules": ["先定义对比维度", "逐项说明差异和适用场景", "补充风险和边界", "结尾给不同人群的选择建议"],
        "guardrails": ["不得无证据贬低竞品", "不得把主观偏好写成事实"],
    },
    "buying_guide": {
        "label": "选购指南类",
        "name": "选择标准 + 预算范围 + 避坑清单",
        "rules": ["先给选择标准", "再讲预算或成本边界", "补充核对清单和避坑点", "结尾给下一步判断方法"],
        "guardrails": ["不得承诺固定效果", "不得忽略客户知识库事实"],
    },
    "price_budget": {
        "label": "价格预算类",
        "name": "价格区间 + 影响因素 + 防踩坑",
        "rules": ["先说明价格受哪些因素影响", "给出区间或预算口径", "解释高低价差异", "补充避坑和核对建议"],
        "guardrails": ["价格必须写清口径", "没有事实依据时不要写具体报价"],
    },
    "faq_answer": {
        "label": "问答解惑类",
        "name": "直接回答 + 分点解释 + 常见追问",
        "rules": ["开头先回答核心问题", "正文分点解释原因", "补充常见追问", "结尾给稳妥建议"],
        "guardrails": ["不要长篇铺垫", "不要用绝对化承诺"],
    },
    "info_trend": {
        "label": "信息趋势类",
        "name": "结论摘要 + 背景原因 + 适用边界",
        "rules": ["先给结论摘要", "说明背景和原因", "补充数据或案例依据", "写清适用边界"],
        "guardrails": ["不要把趋势写成必然结果", "没有数据时不要伪造数据"],
    },
    "brand_case": {
        "label": "案例证明类",
        "name": "场景问题 + 解决方案 + 结果证据",
        "rules": ["先写客户场景", "说明遇到的问题", "解释解决方案", "用结果或反馈收尾"],
        "guardrails": ["案例必须来自客户资料", "不得夸大结果"],
    },
}

USER_CHOICE_TO_TEMPLATE = {
    "comparison": "comparison_review",
    "guide": "buying_guide",
    "checklist": "buying_guide",
    "risk": "buying_guide",
    "price": "price_budget",
    "data": "info_trend",
    "qa": "faq_answer",
    "case": "brand_case",
    "story": "brand_case",
}

STYLE_FAMILY_TO_TEMPLATE = {
    "ranking": "ranking_recommendation",
    "comparison": "comparison_review",
    "guide": "buying_guide",
    "faq": "faq_answer",
    "case": "brand_case",
    "data_report": "info_trend",
}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v or "").strip()]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v or "").strip()]
        except Exception:
            return [value.strip()]
    return []


def _int_summary(summary: dict[str, Any], key: str) -> int:
    try:
        return int(summary.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _ready_from_summary(summary: dict[str, Any]) -> bool:
    return (
        _int_summary(summary, "adopted_group_count") >= 30
        and _int_summary(summary, "explicit_cited_count") >= 30
        and _int_summary(summary, "search_only_control_count") >= 30
        and _int_summary(summary, "engine_count") >= 2
        and _int_summary(summary, "sample_domain_count") >= 5
        and _int_summary(summary, "qualified_structure_lift_count") >= 2
    )


def _reference_ready_from_summary(summary: dict[str, Any]) -> bool:
    """Enough reference material to pick a default template without claiming causal lift."""
    if _ready_from_summary(summary):
        return True
    return max(
        _int_summary(summary, "style_feature_count"),
        _int_summary(summary, "search_only_control_count"),
        _int_summary(summary, "source_signal_count"),
        _int_summary(summary, "adopted_group_count") + _int_summary(summary, "explicit_cited_count"),
    ) >= 30


def _shorten(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def infer_default_article_type(
    title: str = "",
    *,
    user_choice: str = "auto",
    style_family: str = "",
) -> str:
    """Pick one backend template; the frontend should not expose template choices."""
    choice = str(user_choice or "auto").strip()
    if choice and choice != "auto" and choice in USER_CHOICE_TO_TEMPLATE:
        return USER_CHOICE_TO_TEMPLATE[choice]

    normalized_title = str(title or "")
    if re.search(r"(对比|测评|横评|哪个好|区别|优缺点)", normalized_title, re.I):
        return "comparison_review"
    if re.search(r"(价格|费用|报价|多少钱|预算|元|¥|￥)", normalized_title, re.I):
        return "price_budget"
    if re.search(r"(榜单|榜|排行|排名|TOP|top|十佳|推荐|哪家好)", normalized_title, re.I):
        return "ranking_recommendation"
    if re.search(r"(攻略|指南|避坑|怎么选|如何|流程|教程)", normalized_title, re.I):
        return "buying_guide"
    if re.search(r"[?？]|(怎么|为什么|怎么办|是否|可以吗)", normalized_title):
        return "faq_answer"
    if re.search(r"(案例|客户|项目|故事)", normalized_title):
        return "brand_case"

    family = str(style_family or "").strip()
    if family in STYLE_FAMILY_TO_TEMPLATE:
        return STYLE_FAMILY_TO_TEMPLATE[family]
    return "info_trend"


def build_default_structure_template(
    *,
    title: str = "",
    user_choice: str = "auto",
    style_family: str = "",
    source_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = source_summary or {}
    article_type = infer_default_article_type(
        title,
        user_choice=user_choice,
        style_family=style_family,
    )
    template = DEFAULT_STRUCTURE_TEMPLATES.get(article_type) or DEFAULT_STRUCTURE_TEMPLATES["info_trend"]
    reference_ready = _reference_ready_from_summary(summary)
    ready = _ready_from_summary(summary)
    return {
        "article_type": article_type,
        "article_type_label": template["label"],
        "recommended_template_name": template["name"],
        "template_rules": list(template["rules"]),
        "template_guardrails": list(template["guardrails"]),
        "default_enabled": bool(reference_ready),
        "default_reason": (
            f"后台已根据行业范文结构自动选择「{template['label']}」写法。"
            if reference_ready else
            "当前行业范文样本不足，暂不自动启用结构推荐。"
        ),
        "evidence_note": (
            "已达到答案采纳样本门槛，但仍只作为本项目结构参考。"
            if ready else
            "基于行业范文结构统计，尚未经采纳验证；这是写作参考，不是效果承诺。"
        ),
    }


def build_guidance_payload(
    strategy_row: dict[str, Any] | None,
    *,
    quote_id: int,
    industry: str,
) -> dict[str, Any]:
    """Convert an active strategy row into safe agent-facing guidance."""
    normalized_industry = normalize_industry_key(industry or "general")
    if not strategy_row:
        fallback_template = build_default_structure_template(
            source_summary={"style_feature_count": 30},
        )
        return {
            "success": True,
            "quote_id": quote_id,
            "industry_key": normalized_industry,
            # [A4] 既没有 active 版本也没有行业范文基线 → 走代码默认模板,同样如实落账。
            "strategy_id": None,
            "strategy_version": None,
            "resolution": "system_default",
            "available": True,
            "can_apply": False,
            "sample_status": "system_default",
            "status_label": "系统默认写法",
            "status_detail": "该行业尚未启用专属策略；系统会按每篇标题选择保守稳妥的默认写法。",
            "guidance": "系统会按每篇标题选择保守稳妥的默认结构。",
            "rules": [_shorten(rule, 80) for rule in fallback_template["template_rules"]],
            "guardrails": [_shorten(rule, 80) for rule in fallback_template["template_guardrails"]],
            "evidence": {},
            "source_summary": {},
            "confidence": 0,
            "production_takeover": False,
            **fallback_template,
            "default_reason": "系统会按每篇标题选择保守稳妥的默认结构。",
            "evidence_note": "使用保守默认结构；行业专属范文统计尚未启用，不是效果承诺。",
        }

    summary = _as_dict(strategy_row.get("source_summary"))
    style_family = str(strategy_row.get("style_family") or "")
    default_template = build_default_structure_template(
        style_family=style_family,
        source_summary=summary,
    )
    rules = _as_list(strategy_row.get("common_elements"))[:6]
    guardrails = _as_list(strategy_row.get("guardrails"))[:5]
    ready = _ready_from_summary(summary)
    default_enabled = bool(default_template["default_enabled"])
    effective_rules = (
        rules if ready and rules
        else [str(rule) for rule in default_template["template_rules"]][:6]
    )
    effective_guardrails = (
        guardrails if guardrails
        else [str(rule) for rule in default_template["template_guardrails"]][:5]
    )
    confidence = float(strategy_row.get("confidence") or 0)
    return {
        "success": True,
        "quote_id": quote_id,
        "industry_key": strategy_row.get("industry_key") or normalized_industry,
        "style_family": style_family,
        # [A4] 指派账本要按版本归因,payload 必须带回策略身份。行业基线路径没有 id
        # (strategy_row 由 build_structure_guidance_for_quote 现造),strategy_id 为 None,
        # resolution 记 industry_baseline —— 这一路仍要落账,否则 active 版本为 0 时管道永远无水。
        "strategy_id": strategy_row.get("id"),
        "strategy_version": strategy_row.get("strategy_version"),
        "resolution": "active_strategy" if strategy_row.get("id") else "industry_baseline",
        "available": True,
        "can_apply": bool(ready),
        "default_enabled": default_enabled,
        "sample_status": "ready" if ready else "observing",
        "status_label": "可试用" if ready else ("系统推荐写法" if default_enabled else "样本观察中"),
        "status_detail": (
            "管理员已启用该行业结构建议；代理可选择仅在本项目试用。"
            if ready else (
                "后台已按行业范文结构选择默认写法；不需要代理手动挑选模板。"
                if default_enabled else
                "管理员有启用版本，但范文样本仍不足，暂不建议用于生成。"
            )
        ),
        "guidance": _shorten(
            strategy_row.get("guidance")
            or default_template["default_reason"],
            420,
        ),
        "rules": [_shorten(rule, 80) for rule in effective_rules],
        "guardrails": [_shorten(rule, 80) for rule in effective_guardrails],
        "evidence": {
            "adopted_group_count": _int_summary(summary, "adopted_group_count"),
            "explicit_cited_count": _int_summary(summary, "explicit_cited_count"),
            "search_only_control_count": _int_summary(summary, "search_only_control_count"),
            "engine_count": _int_summary(summary, "engine_count"),
            "sample_domain_count": _int_summary(summary, "sample_domain_count"),
            "qualified_structure_lift_count": _int_summary(summary, "qualified_structure_lift_count"),
        },
        "source_summary": summary,
        "confidence": round(confidence, 3),
        "production_takeover": False,
        **default_template,
    }


def _topic_template_from_payload(
    payload: dict[str, Any],
    *,
    title: str = "",
    user_choice: str = "auto",
) -> dict[str, Any]:
    return build_default_structure_template(
        title=title,
        user_choice=user_choice,
        style_family=str(payload.get("style_family") or ""),
        source_summary=_as_dict(payload.get("source_summary")),
    )


def build_structure_guidance_for_quote(quote: dict[str, Any]) -> dict[str, Any]:
    quote_id = int(quote.get("id") or 0)
    industry = str(quote.get("industry") or "general")
    industry_key = normalize_industry_key(industry)
    row = get_active_strategy_version(industry_key)
    if not row:
        baseline = load_structure_baseline_summary(industry_key)
        if baseline:
            row = {
                "industry_key": industry_key,
                "style_family": baseline.get("style_family") or "guide",
                "guidance": "后台已读取该行业范文结构基线，生成时会按每篇标题自动选择稳妥结构。",
                "common_elements": [],
                "guardrails": ["不得承诺固定效果", "必须优先核对客户知识库事实"],
                "source_summary": baseline.get("source_summary") or {},
                "confidence": min(0.82, 0.45 + min(300, int(baseline.get("sample_count") or 0)) / 1000),
            }
            payload = build_guidance_payload(row, quote_id=quote_id, industry=industry)
            payload["sample_status"] = "baseline"
            payload["status_label"] = "系统推荐写法"
            payload["status_detail"] = "后台已按行业范文结构基线选择默认写法；不需要代理手动挑选模板。"
            payload["baseline"] = {
                "sample_count": int(baseline.get("sample_count") or 0),
                "style_distribution": baseline.get("style_distribution") or {},
            }
            payload["evidence_note"] = "基于行业范文结构统计，尚未经采纳验证；这是写作参考，不是效果承诺。"
            return payload
    return build_guidance_payload(row, quote_id=quote_id, industry=industry)


def build_bounded_structure_instruction(
    payload: dict[str, Any],
    *,
    title: str = "",
    user_choice: str = "auto",
) -> str:
    """Build a compact instruction used only after explicit project opt-in."""
    if not (payload.get("can_apply") or payload.get("default_enabled")):
        return ""
    topic_template = _topic_template_from_payload(
        payload,
        title=title,
        user_choice=user_choice,
    )
    template_rules = [
        str(rule).strip()
        for rule in (topic_template.get("template_rules") or [])
        if str(rule or "").strip()
    ]
    approved_rules = [
        str(rule).strip()
        for rule in (payload.get("rules") or [])
        if str(rule or "").strip()
    ]
    rules = approved_rules if payload.get("can_apply") and approved_rules else template_rules
    guardrails = [
        str(rule).strip()
        for rule in ((payload.get("guardrails") or []) + (topic_template.get("template_guardrails") or []))
        if str(rule or "").strip()
    ]
    guidance = _shorten(payload.get("guidance") or topic_template.get("default_reason") or "", 220)
    parts = [
        "只作为本项目结构参考，老版本写作方式仍保留。",
        "这是系统推荐写法，不是效果承诺；必须优先满足客户知识库事实、标题意图和合规边界。",
        f"推荐类型：{topic_template.get('article_type_label')}。",
        f"推荐模板：{topic_template.get('recommended_template_name')}。",
    ]
    if guidance:
        parts.append(f"结构方向：{guidance}")
    if rules:
        parts.append("写作顺序：" + "；".join(rules[:4]))
    if guardrails:
        parts.append("注意：" + "；".join(list(dict.fromkeys(guardrails))[:3]))
    return _shorten("\n".join(parts), 700)
