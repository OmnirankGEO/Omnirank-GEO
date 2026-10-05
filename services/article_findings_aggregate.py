"""[工单 C-3 T2 2026-07-27] 文章 findings 同类聚合(输出层,只读不改判定)。

Owner:"几十处独立卡片毫无意义,应该聚合+一键修复"。生产实况:深档文每篇 61-88 处
findings 原样平铺给前端。本模块把 `articles.quality_warning` 里的五路 findings
(evidence_legal.hard / evidence.soft / evidence_precision.warnings /
client_presence.findings / length_compliance.findings)按 code 聚合成类型卡:

    {code, severity, count, title, message, source, spans: [...], repairable_count}

- **逐条结构原样保留**(quality_warning 不动)——修复链、既有前端逻辑全兼容;
- 聚合是纯函数、读取时计算,不落库、无迁移;
- span 带 matched_text(有才可段级修复)与 excerpt/evidence(展示定位)。
"""
from __future__ import annotations

from typing import Any

# 人话标题(缺省用 message 首句/code)。工程术语不出现在标题里。
_FRIENDLY_TITLES: dict[str, str] = {
    "claim_missing_inline_evidence": "数字缺少来源支撑",
    "inline_evidence_claim_mismatch": "引用来源与数字对不上",
    "unknown_inline_evidence_id": "引用了不存在的来源编号",
    "unverified_inline_evidence_id": "引用的来源还没核验过",
    "customer_fact_source_boundary_missing": "企业资料没标来源边界",
    "bibliography_only_support": "只有文末清单、缺少段内支撑",
    "no_verified_evidence_available": "全文没有已核验的外部证据",
    "default_positive_pressure": "压差方向不能写成默认值",
    "default_negative_pressure": "压差方向不能写成默认值",
    "inference_verification_action_missing": "工程推断缺少核验动作",
    "absolute_first_claim": "广告法绝对化用语",
    "absolute_superlative_claim": "广告法最高级用语",
    "ordered_ranking_title": "标题是榜单形态、需要正文披露依据",
    "self_invented_scoring_system": "自创评分体系",
    "manufactured_score": "评分缺少真实口径",
    "ordered_brand_candidates": "品牌排序缺少入选口径披露",
    "anonymous_authority": "借匿名权威背书",
    "unsourced_outcome_number": "效果百分比缺少来源",
    "missing_source_boundary": "没说明事实来源类型",
    "missing_limitations": "缺少适用边界说明",
    # [P1-2 2026-08-14] 题证一致性 + 实体绑定 advisory(人话标题,不露内部枚举)
    "entity_binding_unverified": "有资料可能不是这家公司的",
    "entity_binding_isolated": "已隔离其他公司的资料",
    "title_evidence_alignment_weak": "公开资料与本篇问题相关性偏弱",
}


def _finding_spans(raw: Any) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    return {
        "matched_text": str(item.get("matched_text") or ""),
        "excerpt": str(item.get("excerpt") or item.get("evidence") or ""),
        "evidence_ids": list(item.get("evidence_ids") or []),
    }


def _annotate_ai_repair(
    span: dict[str, Any], code: str, *, industry: str, title: str,
) -> dict[str, Any]:
    """[span 级 AI 免费修复 2026-07-30 · §2.1 / 锁 6] 标注这一处能不能给 AI 修。

    前端据此决定 `ai_fix_this_span` **渲不渲染**;端点用同一个 repair_route
    再拒一次(两层同源,不是两份口径)。医疗/法律/金融高风险 → False,
    出口只有"走人工签发"与"自己手动改"。
    """
    from services.span_level_repair import repair_route

    route = repair_route(
        code,
        span_text=span.get("matched_text") or span.get("excerpt") or "",
        industry=industry,
        title=title,
    )
    return {
        **span,
        "ai_repairable": bool(span.get("matched_text")) and bool(route["ai_repairable"]),
        "ai_repair_block_reason": route["block_reason"],
        "span_level": bool(route["span_level"]),
    }


def _collect(source: str, raw_list: Any, severity_fallback: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw_list, list):
        return out
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "severity": str(raw.get("severity") or severity_fallback),
            "message": str(raw.get("message") or ""),
            "source": source,
            "span": _finding_spans(raw),
        })
    return out


def aggregate_article_findings(
    quality_warning: Any, *, industry: str = "", title: str = "",
) -> list[dict[str, Any]]:
    """把 quality_warning 五路 findings 聚合成类型卡列表(severity 重的排前)。

    [span 级 AI 免费修复 2026-07-30] 每个 span additionally 带
    `ai_repairable` / `ai_repair_block_reason`,卡片带 `ai_repairable_count`
    —— 高风险(医疗/法律/金融)类前端**不渲染** AI 修复按钮(§2.1)。
    industry/title 缺省为空时退化成"只按 span 文本判高风险",不会误放行。
    """
    warning = quality_warning if isinstance(quality_warning, dict) else {}
    flat: list[dict[str, Any]] = []
    evidence_legal = warning.get("evidence_legal") or {}
    flat += _collect("evidence_legal", evidence_legal.get("hard"), "hard")
    evidence = warning.get("evidence") or {}
    flat += _collect("evidence", evidence.get("soft"), "soft")
    precision = warning.get("evidence_precision") or {}
    flat += _collect("evidence_precision", precision.get("warnings"), "advisory")
    client_presence = warning.get("client_presence") or {}
    flat += _collect("client_presence", client_presence.get("findings"), "advisory")
    length_compliance = warning.get("length_compliance") or {}
    flat += _collect("length_compliance", length_compliance.get("findings"), "advisory")

    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in flat:
        code = item["code"]
        if code not in grouped:
            grouped[code] = {
                "code": code,
                "severity": item["severity"],
                "count": 0,
                "title": _FRIENDLY_TITLES.get(code, item["message"][:24] or code),
                "message": item["message"],
                "source": item["source"],
                "spans": [],
                "repairable_count": 0,
                "ai_repairable_count": 0,
            }
            order.append(code)
        card = grouped[code]
        card["count"] += 1
        span = _annotate_ai_repair(item["span"], code, industry=industry, title=title)
        card["spans"].append(span)
        if span["matched_text"]:
            card["repairable_count"] += 1
        if span["ai_repairable"]:
            card["ai_repairable_count"] += 1

    rank = {"hard": 0, "soft": 1, "advisory": 2, "warning": 2}
    return sorted(
        (grouped[code] for code in order),
        key=lambda card: (rank.get(card["severity"], 3), -card["count"]),
    )
