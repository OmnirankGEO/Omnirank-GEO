"""Deterministic content/media opportunity derivation.

Opportunities are RULE-BASED over already-computed aggregate metrics — never
"rank in the top-ten article" defaults, never LLM-invented. Each opportunity
explains why (which model behavior gap it addresses) and always has
``auto_action_allowed = False`` (this batch only suggests; it never writes,
publishes, places, or charges).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# thresholds (bps) — part of the metric version
_HIGH_REFUSAL_NO_EVIDENCE = 1500
_HIGH_CRITERIA_ONLY = 1500
_HIGH_MENTIONED_ONLY = 1500
_LOW_CITATION = 3000
_LOW_EVIDENCE_COVERAGE = 3000
_HAS_PRESENCE = 2000


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    priority: int
    topic: str
    recommended_content_type: str
    recommended_evidence: list[str]
    recommended_media_pattern: list[str]
    reason: str
    sample_size: int
    stability: str  # stable | watch | insufficient
    auto_action_allowed: bool = False


def _stability_bucket(status: str) -> str:
    if status == "insufficient":
        return "insufficient"
    if status in ("watch", "shifted"):
        return "watch"
    return "stable"


def derive_opportunities(overall: Optional[dict], *, scope_label: str) -> list[Opportunity]:
    """Derive opportunities from the overall aggregate row for a scope-cell.

    Deterministic: same aggregate row always yields the same ordered list.
    Returns an empty list when there is no data (never fabricates).
    """
    if not overall or int(overall.get("valid_observations") or 0) <= 0:
        return []

    sample = int(overall["valid_observations"])
    stability = _stability_bucket(overall.get("stability_status", "stable"))
    candidates: list[tuple[int, dict]] = []  # (severity_bps, opportunity fields)

    refusal_no_ev = int(overall["refusal_no_evidence_rate_bps"])
    criteria = int(overall["criteria_only_rate_bps"])
    mentioned = int(overall["mentioned_only_rate_bps"])
    citation = int(overall["citation_rate_bps"])
    evidence_cov = int(overall["evidence_coverage_rate_bps"])
    presence = int(overall["presence_rate_bps"])

    if refusal_no_ev >= _HIGH_REFUSAL_NO_EVIDENCE:
        candidates.append((refusal_no_ev, {
            "topic": "可核验的资质与项目证据",
            "recommended_content_type": "qualification_evidence",
            "recommended_evidence": ["可核验的项目案例", "项目验收数据", "客户授权的公开证明"],
            "recommended_media_pattern": ["权威行业站点", "官网证据页"],
            "reason": "较多问题因证据不足没有给出具体推荐，补足可核验证据能提升被推荐概率。",
        }))
    if criteria >= _HIGH_CRITERIA_ONLY:
        candidates.append((criteria, {
            "topic": "服务商筛选方法与适用边界",
            "recommended_content_type": "comparison_method",
            "recommended_evidence": ["筛选标准", "适用边界", "常见误区"],
            "recommended_media_pattern": ["方法论长文", "对比表"],
            "reason": "部分回答只提供筛选标准而未形成品牌推荐，补充方法内容有助于进入候选。",
        }))
    if mentioned >= _HIGH_MENTIONED_ONLY:
        candidates.append((mentioned, {
            "topic": "能形成推荐的成功案例",
            "recommended_content_type": "case_study",
            "recommended_evidence": ["改造前后指标", "验收记录", "客户证言"],
            "recommended_media_pattern": ["案例深度文", "视频纪实"],
            "reason": "品牌被提到但未形成推荐，补充可核验案例有助于从'提及'转为'推荐'。",
        }))
    if presence >= _HAS_PRESENCE and citation <= _LOW_CITATION:
        candidates.append((10000 - citation, {
            "topic": "可被引用的数据与来源",
            "recommended_content_type": "data_report",
            "recommended_evidence": ["行业数据", "第三方检测", "可引用来源"],
            "recommended_media_pattern": ["数据报告", "权威媒体转载"],
            "reason": "已被提及但回答很少提供引用来源，补充可引用材料有助于交叉验证。",
        }))
    if evidence_cov <= _LOW_EVIDENCE_COVERAGE:
        candidates.append((10000 - evidence_cov, {
            "topic": "补齐经营与资质基础资料",
            "recommended_content_type": "qualification_evidence",
            "recommended_evidence": ["经营资质", "服务范围", "既往业绩"],
            "recommended_media_pattern": ["官网资料页", "权威目录收录"],
            "reason": "多数回答缺少可核验证据，应先补齐经营资料而不是先写排名文。",
        }))

    # de-dup by content-type, keep the highest-severity variant, then order
    best: dict[str, tuple[int, dict]] = {}
    for sev, fields in candidates:
        key = fields["recommended_content_type"]
        if key not in best or sev > best[key][0]:
            best[key] = (sev, fields)
    ordered = sorted(best.values(), key=lambda t: t[0], reverse=True)

    out: list[Opportunity] = []
    for idx, (_sev, fields) in enumerate(ordered, start=1):
        out.append(Opportunity(
            opportunity_id=f"opp-{scope_label}-{idx:03d}",
            priority=idx,
            topic=fields["topic"],
            recommended_content_type=fields["recommended_content_type"],
            recommended_evidence=fields["recommended_evidence"],
            recommended_media_pattern=fields["recommended_media_pattern"],
            reason=fields["reason"],
            sample_size=sample,
            stability=stability,
            auto_action_allowed=False,
        ))
    return out
