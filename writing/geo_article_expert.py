"""Production article-review SSOT: hard gates plus provisional quality score.

The score is a pre-publication quality score, never a citation probability.
Outcome scoring is intentionally unavailable until direct observation lineage
passes the data-readiness gates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Final

from .article_length_contract import assess_length_compliance
from .client_presence_policy import evaluate_client_presence
from .evidence_first_policy import evaluate_content_trust
from .evidence_pack import validate_evidence_pack
from .evidence_precision_policy import evaluate_evidence_precision


REVIEW_VERSION: Final = "geo-article-expert-v1.0"
QUALITY_WEIGHTS: Final[dict[str, int]] = {
    "evidence_provenance": 25,
    "eeat_authority": 15,
    "question_fit": 15,
    "decision_value": 15,
    "extractable_structure": 10,
    "client_value_balance": 10,
    "compliance_readability": 10,
}

_HIGH_RISK_RE = re.compile(r"医疗|药物|细胞治疗|疾病|疗效|法律|诉讼|金融|收益|保险|安全认证|功效")


def is_high_risk_text(*parts: str) -> bool:
    """医疗/法律/金融高风险词表的**唯一** SSOT 判定入口(词表即上面的 _HIGH_RISK_RE)。

    [span 级 AI 免费修复 2026-07-30 · §2.1] 该类"缺的是人工签发,不是措辞",
    因此 span 级 AI 修复必须把它排除在外。排除门与本文件的 human_review_required
    **共用同一词表**(改词表两边同时生效,不许在别处再写第二份口径);
    **作用域刻意不同**:
      - review_article:industry + title + 正文前 1000 字 → 整篇是否必须人工签发;
      - 修复门(services/span_level_repair):industry + title + **该 span 本体**
        → 这一处是否属于第四类。用整篇作用域会把"文中提过安全认证的建材文"
        整体锁死 AI 修复,那是过度阻断而非风险控制。
    """
    return bool(_HIGH_RISK_RE.search(" ".join(str(part or "") for part in parts)))
_ACTION_RE = re.compile(r"核验|检查|步骤|清单|条件|适用|不适用|风险|建议")
_EEAT_RE = re.compile(r"来源|截至|公开|标准|法规|样本|方法|适用范围|限制")
_DIRECT_RE = re.compile(r"(?:答案|结论|首先|选择时|建议|应当|是指|取决于)")


@dataclass(frozen=True)
class GeoArticleReviewResult:
    review_version: str
    decision: str
    quality_score: int
    quality_dimensions: dict[str, int]
    hard_failures: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]
    evidence_summary: dict[str, Any]
    evidence_precision: dict[str, Any]
    # [WP12 P0-3 / P1-5] 客户存在感与篇幅分档:两者都是 A1 观测面,
    # 只进 warnings 与这两个只读字段,永不参与 decision=="blocked"。
    client_presence: dict[str, Any]
    length_compliance: dict[str, Any]
    human_review_required: bool
    outcome_score_status: str
    outcome_score: None
    confidence_grade: str
    explanation: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def review_article(
    *,
    title: str,
    content: str,
    evidence_pack: dict[str, Any],
    brand_fact_snapshot: dict[str, Any] | None = None,
    industry: str = "",
    target_question: str = "",
    client_brand: str = "",
    competitor_names: tuple[str, ...] = (),
    style_code: str = "",
    family_code: str = "",
    length_plan: dict[str, Any] | None = None,
) -> GeoArticleReviewResult:
    trust = evaluate_content_trust(title, content)
    evidence = validate_evidence_pack(evidence_pack)
    precision = evaluate_evidence_precision(content, evidence_pack, brand_fact_snapshot)
    presence = evaluate_client_presence(
        title,
        content,
        client_brand=client_brand,
        competitor_names=competitor_names,
        style_code=style_code,
        family_code=family_code,
    )
    length = assess_length_compliance(content, style_code=style_code or family_code, plan=length_plan)
    text = str(content or "")
    lower_title = str(title or "").casefold()
    lower_question = str(target_question or "").casefold()

    evidence_score = min(25, evidence["verified_count"] * 5 + evidence["publisher_count"] * 2)
    if evidence["refute_count"]:
        evidence_score = min(25, evidence_score + 3)
    eeat_score = min(15, 3 + len(set(_EEAT_RE.findall(text))) * 2)
    question_score = 4
    if lower_question and any(token in lower_title or token in text[:800].casefold() for token in lower_question.split() if len(token) > 1):
        question_score += 6
    if _DIRECT_RE.search(text[:800]):
        question_score += 5
    decision_score = min(15, 3 + len(set(_ACTION_RE.findall(text))) * 2)
    structure_score = min(10, 2 + min(4, text.count("\n##")) + min(2, text.count("|---")) + min(2, text.count("- ")))
    brand_count = text.casefold().count(str(client_brand or "").casefold()) if client_brand else 0
    balance_score = 8 if 1 <= brand_count <= 10 else (5 if brand_count == 0 else 3)
    if "适用" in text and ("限制" in text or "风险" in text):
        balance_score = min(10, balance_score + 2)
    compliance_score = 10 if not trust.hard else 0
    dimensions = {
        "evidence_provenance": evidence_score,
        "eeat_authority": eeat_score,
        "question_fit": min(15, question_score),
        "decision_value": decision_score,
        "extractable_structure": structure_score,
        "client_value_balance": balance_score,
        "compliance_readability": compliance_score,
    }
    score = sum(dimensions.values())
    hard = [asdict(item) for item in trust.hard]
    hard.extend(asdict(item) for item in precision.hard)
    warnings = [asdict(item) for item in trust.soft]
    warnings.extend(asdict(item) for item in precision.warnings)
    # [WP12 P0-3 / P1-5] A1 标注:定位 + 可执行出口,不影响 decision。
    warnings.extend(presence.payload()["findings"])
    warnings.extend(length["findings"])
    eeat_baseline_missing = {
        item.code for item in trust.soft
        if item.code in {"missing_source_boundary", "missing_limitations", "missing_verification_steps"}
    }
    if evidence["invalid"]:
        warnings.append({"code": "invalid_evidence_items", "details": evidence["invalid"]})
    high_risk = is_high_risk_text(industry, title, text[:1000])
    no_verified_evidence = evidence["verified_count"] == 0
    if hard:
        decision = "blocked"
    elif evidence["invalid"]:
        decision = "pending_human_review"
    elif high_risk or no_verified_evidence:
        decision = "pending_human_review"
    elif eeat_baseline_missing:
        decision = "pending_human_review"
    elif score >= 75:
        decision = "approved"
    else:
        decision = "pending_human_review"
    confidence = "C" if evidence["verified_count"] > 0 else "D"
    return GeoArticleReviewResult(
        review_version=REVIEW_VERSION,
        decision=decision,
        quality_score=score,
        quality_dimensions=dimensions,
        hard_failures=tuple(hard),
        warnings=tuple(warnings),
        evidence_summary=evidence,
        evidence_precision=precision.payload(),
        client_presence=presence.payload(),
        length_compliance=length,
        human_review_required=high_risk or no_verified_evidence,
        outcome_score_status="unavailable_until_direct_lineage_matures",
        outcome_score=None,
        confidence_grade=confidence,
        explanation=(
            "硬门失败，分数不能抵消。" if hard else
            "Evidence Pack 存在缺 URL 或缺主张的条目；请局部修复或人工确认继续。" if evidence["invalid"] else
            "高风险或无全文核验证据，必须人工签发。" if high_risk or no_verified_evidence else
            "E-E-A-T 基线缺少来源边界、限制或核验步骤；请局部修复或人工确认继续。" if eeat_baseline_missing else
            "分数仅表示发布前质量，不预测被 AI 引用；低分项可局部修复或人工确认继续。"
        ),
    )
