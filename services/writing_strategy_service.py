"""Shadow writing strategy generation for the GEO data flywheel."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from services.media_entity_flywheel import normalize_industry_key
from services.writing_style_feature_extractor import summarize_style_features


STRATEGY_VERSION = "writing_strategy_shadow_v1_2026-06-12"

STYLE_TO_GUIDANCE = {
    "ranking": "适合回答“哪家好/推荐/排名”类问题：开篇先给筛选标准，再给分层推荐与适用人群。",
    "comparison": "适合回答“哪个好/怎么选/差异”类问题：用同维度对比、避坑点和决策建议降低泛泛而谈。",
    "guide": "适合回答“如何/流程/攻略”类问题：按步骤讲清选择标准、准备材料、风险点与判断方法。",
    "faq": "适合回答高频疑问：用问答结构覆盖真实用户追问，答案短而具体。",
    "case": "适合做信任背书：用客户场景、问题、方案、结果形成可引用案例。",
    "data_report": "适合趋势和行业判断：先给数据口径，再给结论、原因和适用边界。",
}

STRUCTURE_RULE_BY_FEATURE = {
    "lead_answers_question": "开头先回答问题：200 字内直接给出判断，再解释选择标准。",
    "lead_has_selection_criteria": "开头先列筛选维度，让文章更容易被 AI 摘取判断框架。",
    "has_checklist": "正文用清单、步骤或维度组织，避免大段泛论。",
    "has_price_or_budget": "补充价格、预算或费用边界，提升可摘取证据密度。",
    "has_customer_case": "加入客户案例或真实场景，避免只有通用观点。",
    "has_risk_or_pitfall": "加入避坑和风险提示，保持中肯而非硬广。",
    "conclusion_has_decision_advice": "结尾给清晰选择建议，但不要承诺固定效果。",
}


def _score_signal_rows(rows: list[dict[str, Any]]) -> float:
    return round(sum(float(r.get("balanced_weight") or 0) for r in rows), 4)


def _score_outcomes(rows: list[dict[str, Any]]) -> float:
    score = 0.0
    for row in rows:
        status = str(row.get("publish_status") or "").lower()
        if status in {"published", "success", "completed"}:
            score += 0.2
        score += max(0.0, float(row.get("citation_lift_30d") or row.get("ai_citations_delta_30d") or 0)) * 0.1
        score += max(0.0, float(row.get("monitoring_brand_score_delta_30d") or 0)) * 0.01
    return round(score, 4)


def _first_structure_analysis(style_features: list[dict[str, Any]]) -> dict[str, Any]:
    for item in style_features:
        analysis = item.get("article_structure_analysis")
        if isinstance(analysis, dict):
            return analysis
    structures = [
        item.get("article_structure")
        for item in style_features
        if isinstance(item.get("article_structure"), dict)
    ]
    if not structures:
        return {}
    lift_rows: list[dict[str, Any]] = []
    for feature, rule in STRUCTURE_RULE_BY_FEATURE.items():
        share = sum(1 for structure in structures if structure.get(feature)) / max(1, len(structures))
        if share >= 0.5:
            lift_rows.append({
                "feature": feature,
                "label": rule.split("，", 1)[0].replace("。", ""),
                "adopted_share": round(share, 4),
                "control_share": 0,
                "lift": 1.0,
            })
    return {
        "adopted_group_count": len(structures),
        "explicit_cited_count": 0,
        "search_only_control_count": 0,
        "engine_count": 0,
        "sample_domain_count": 0,
        "feature_lift": lift_rows,
    }


def _build_structure_guidance(style_features: list[dict[str, Any]]) -> dict[str, Any]:
    analysis = _first_structure_analysis(style_features)
    adopted_count = int(analysis.get("adopted_group_count") or analysis.get("adopted_count") or 0)
    cited_count = int(analysis.get("explicit_cited_count") or analysis.get("cited_group_count") or 0)
    control_count = int(analysis.get("search_only_control_count") or analysis.get("control_group_count") or 0)
    engine_count = int(analysis.get("engine_count") or 0)
    domain_count = int(analysis.get("sample_domain_count") or analysis.get("domain_count") or 0)
    lift_rows = [
        row for row in (analysis.get("feature_lift") or [])
        if isinstance(row, dict)
    ]
    qualified_lifts = [
        row for row in lift_rows
        if float(row.get("lift") or 0) >= 1.5
        and str(row.get("feature") or "") != "conclusion_is_salesy"
    ]
    sample_ready = (
        adopted_count >= 30
        and cited_count >= 30
        and control_count >= 30
        and engine_count >= 2
        and domain_count >= 5
        and len(qualified_lifts) >= 2
    )
    source_summary = {
        "adopted_group_count": adopted_count,
        "explicit_cited_count": cited_count,
        "search_only_control_count": control_count,
        "engine_count": engine_count,
        "sample_domain_count": domain_count,
        "qualified_structure_lift_count": len(qualified_lifts),
    }
    if not sample_ready:
        return {
            "structure_status": "observing",
            "structure_rules": [],
            "structure_warnings": ["样本观察中：答案采纳、明确引用或搜索曝光对照样本不足，结构建议暂不进入默认策略。"],
            "source_summary": source_summary,
        }

    rules: list[str] = []
    for row in sorted(qualified_lifts, key=lambda item: float(item.get("lift") or 0), reverse=True):
        feature = str(row.get("feature") or "")
        rule = STRUCTURE_RULE_BY_FEATURE.get(feature)
        if not rule:
            label = str(row.get("label") or "").strip()
            if label:
                rule = f"{label}，但必须结合客户事实和行业边界。"
        if rule and rule not in rules:
            rules.append(rule)
        if len(rules) >= 6:
            break
    return {
        "structure_status": "ready",
        "structure_rules": rules,
        "structure_warnings": [],
        "source_summary": source_summary,
    }


def _strategy_version(industry_key: str, family: str, summary: dict[str, Any]) -> str:
    payload = {
        "industry_key": industry_key,
        "style_family": family,
        "common_elements": summary.get("common_elements") or [],
        "source": summary.get("feature_count") or 0,
    }
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{STRATEGY_VERSION}_{industry_key}_{family}_{stamp}_{digest}"


def build_strategy_candidate(
    *,
    industry_key: str,
    style_features: list[dict[str, Any]],
    source_signals: list[dict[str, Any]],
    outcome_signals: list[dict[str, Any]],
    operator_note: str = "",
) -> dict[str, Any]:
    industry_key = normalize_industry_key(industry_key)
    summary = summarize_style_features(style_features)
    family = summary["style_family"]
    evidence_score = _score_signal_rows(source_signals)
    outcome_score = _score_outcomes(outcome_signals)
    confidence = min(0.98, summary.get("confidence", 0) * 0.45 + min(1.0, evidence_score) * 0.40 + min(1.0, outcome_score) * 0.15)
    common_elements = summary.get("common_elements") or []
    structure_guidance = _build_structure_guidance(style_features)
    structure_rules = structure_guidance["structure_rules"]
    if structure_rules:
        common_elements = list(dict.fromkeys([*common_elements, *[rule.split("，", 1)[0] for rule in structure_rules]]))
    base_guidance = STYLE_TO_GUIDANCE.get(family, STYLE_TO_GUIDANCE["guide"])
    guidance = base_guidance
    if structure_rules:
        guidance = (
            f"{base_guidance} 本行业被答案采纳的文章常见结构："
            f"{'；'.join(structure_rules[:3])} 此建议需管理员审核，不自动接管生产写作。"
        )
    guardrails = [
        "不得自动替换线上写作策略",
        "不得承诺固定排名或固定 30 天效果",
        "必须保留客户知识库事实核查",
        "生产启用前必须由管理员审批版本",
    ]
    return {
        "strategy_version": _strategy_version(industry_key, family, summary),
        "industry_key": industry_key,
        "status": "shadow",
        "requires_admin_review": True,
        "style_family": family,
        "guidance": guidance,
        "common_elements": common_elements,
        "evidence_score": evidence_score,
        "outcome_score": outcome_score,
        "confidence": round(confidence, 3),
        "operator_note": operator_note,
        "guardrails": guardrails,
        "structure_status": structure_guidance["structure_status"],
        "structure_rules": structure_rules,
        "structure_warnings": structure_guidance["structure_warnings"],
        "production_takeover": False,
        "source_summary": {
            "source_signal_count": len(source_signals),
            "style_feature_count": len(style_features),
            "outcome_count": len(outcome_signals),
            **structure_guidance["source_summary"],
        },
    }
