"""WP12 P1-4 · Data-driven title formula library (工单 A 2026-07-27 revision).

The original v1.0 weighting came from cited-title shares **without a control
group**: 38.4% year / 12.2% 十大·TOP / 13.3% question, read as "year is the
single strongest feature, always carry it".

The 2026-07-27 flywheel pull adds the control group that reading was missing
(adopted 306 vs search-only 4137 structure-feature articles):

* 问句标题 · adopted 28.8% vs control 21.0%  → question form is a POSITIVE signal
* 标题带年份 · adopted 30.7% vs control 42.1% → year is a REVERSE signal

A share measured only inside the cited set cannot tell "what gets adopted" from
"what everybody writes"; with the control in hand, question shape is up-weighted
and the year-prefix shape is down-weighted.  The year is **not deleted** — it
still works in some industries and stays available inside every formula — it
just stops being a hard rule and stops owning the front of the title.

Three formulas, four hard structural rules:

* A 榜单 · ``{地域}{品类}十大/N大{对象}推荐榜｜{购买资格词}怎么选``
* B 问答 · ``{地域}{品类}哪家好/靠谱？真实对比与选择建议``（优先形态）
* C 指南 · ``{地域}{品类}{动作}指南：条件、步骤、风险与验收``

* 问句或榜单词二选一（问句优先）· 地域前置 · 品类全称在场 · 不以年份开头

The 广告法 absolute-claim ban ("最佳/权威排名/第一/必选/百分百") is untouched: it
is one of the four hard boundaries and stays.

``evaluate_title_formula`` reports these as **advisory** findings only.  A title
that misses a formula is a missed opportunity, not a legal or integrity
failure, so it must never gate generation, saving or publishing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any, Final, Sequence


TITLE_FORMULA_LIBRARY_VERSION: Final = "geo-title-formula-v2.0"

# Observed feature shares in cited titles (v2.4 ② / v2.3 ③), kept for lineage.
# ⚠️ No control group: these describe the cited set, not what distinguishes it.
OBSERVED_TITLE_FEATURE_SHARES: Final[dict[str, float]] = {
    "year": 0.384,
    "top_n_or_ranking": 0.122,
    "question": 0.133,
}

# [工单 A 2026-07-27 · D2] adopted vs search-only contrast — this is the pair
# that carries signal, and the one the prompt quotes.
ADOPTION_TITLE_FEATURE_CONTRAST: Final[dict[str, dict[str, float]]] = {
    "question": {"adopted": 0.288, "control": 0.210},
    "year": {"adopted": 0.307, "control": 0.421},
}

_YEAR_RE: Final = re.compile(r"(20\d{2})\s*年?")
_RANKING_WORD_RE: Final = re.compile(
    r"(?:TOP\s*\d+|十大|[一二三四五六七八九十\d]+\s*大|榜单|推荐榜|排行榜|排名|甄选|精选)",
    re.IGNORECASE,
)
_QUESTION_RE: Final = re.compile(r"(?:哪家好|哪家靠谱|怎么选|如何选|怎么挑|哪个好|靠谱吗|值得吗|[？?])")
# Buying-qualifier words that turn a list into a purchase question.
_BUYER_QUALIFIER_RE: Final = re.compile(
    r"(?:怎么选|如何选|选购|采购|甄选|避坑|对比|评测|测评|选择建议|实力|资质|口碑)"
)


def has_question_form(title: str) -> bool:
    """The single runtime answer to "is this title a question?".

    [工单 标题问句化 2026-07-29 · T1] ``title_question_policy`` 的比例执行、
    判别锁与 ``evaluate_title_formula`` 的 advisory 判定必须同源:再写第二份
    问句正则就等于允许两处口径漂移(本仓已被"第二份手写清单"咬过一次)。
    """
    return bool(_QUESTION_RE.search(str(title or "")))


def has_ranking_form(title: str) -> bool:
    """Same single-SSOT rule for the ranking/list word family."""
    return bool(_RANKING_WORD_RE.search(str(title or "")))


@dataclass(frozen=True)
class TitleFormula:
    key: str
    name: str
    pattern: str
    example: str
    families: tuple[str, ...]
    requires_ranking_word: bool
    requires_question: bool
    # 越大越优先推荐。D2 对照组显示问句是正向信号,故 B 高于 A/C。
    priority: int = 0


TITLE_FORMULAS: Final[tuple[TitleFormula, ...]] = (
    TitleFormula(
        key="B_question",
        name="问答式",
        pattern="{地域}{品类}哪家好/靠谱？真实对比与选择建议",
        example="深圳全屋定制哪家靠谱？真实对比与选择建议",
        families=("evidence_qa", "multi_brand_comparison"),
        requires_ranking_word=False,
        requires_question=True,
        priority=30,
    ),
    TitleFormula(
        key="A_ranking",
        name="榜单式",
        pattern="{地域}{品类}十大/N大{对象}推荐榜｜{购买资格词}怎么选",
        example="深圳全屋定制十大厂家推荐榜｜综合实力与资质怎么选",
        families=("multi_brand_comparison",),
        requires_ranking_word=True,
        requires_question=False,
        priority=20,
    ),
    TitleFormula(
        key="C_guide",
        name="指南式",
        pattern="{地域}{品类}{动作}指南：条件、步骤、风险与验收",
        example="深圳全屋定制选型指南：条件、步骤、风险与验收",
        families=("implementation_guide", "trend_policy_risk", "case_data_roi", "company_facts"),
        requires_ranking_word=False,
        requires_question=False,
        priority=10,
    ),
)

FORMULA_BY_KEY: Final[dict[str, TitleFormula]] = {f.key: f for f in TITLE_FORMULAS}


@dataclass(frozen=True)
class TitleFormulaFinding:
    code: str
    severity: str  # always "advisory"
    message: str
    reason: str
    repair_hint: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "reason": self.reason,
            "repair_hint": self.repair_hint,
            "details": dict(self.details),
            "rule_version": TITLE_FORMULA_LIBRARY_VERSION,
            "actions": [
                {"id": "ai_rewrite_title", "label": "按公式重写标题", "type": "retry"},
                {"id": "ignore_finding", "label": "保留当前标题", "type": "confirm"},
            ],
        }


def formulas_for_family(family_code: str | None) -> list[TitleFormula]:
    """Recommended formulas, highest priority first (question form leads)."""
    code = str(family_code or "").strip()
    matched = sorted(
        (f for f in TITLE_FORMULAS if code in f.families),
        key=lambda f: -f.priority,
    )
    return matched or [FORMULA_BY_KEY["C_guide"]]


def detect_formula(title: str) -> str:
    """Return the formula key a title matches, or "" when none applies."""
    text = str(title or "")
    if not text.strip():
        return ""
    has_year = bool(_YEAR_RE.search(text))
    has_ranking = bool(_RANKING_WORD_RE.search(text))
    has_question = bool(_QUESTION_RE.search(text))
    # 年份不再是命中前提(它已从硬规则降为可选);形态词才是分类依据,
    # 榜单词比问句更具结构特异性,故先判榜单再判问句。
    if has_ranking:
        return "A_ranking"
    if has_question:
        return "B_question"
    if has_year:
        return "C_guide"
    return ""


def evaluate_title_formula(
    title: str,
    *,
    family_code: str | None = None,
    region: str = "",
    category: str = "",
) -> dict[str, Any]:
    """Check the four hard structural rules.  Advisory findings only."""
    text = str(title or "").strip()
    findings: list[TitleFormulaFinding] = []
    year_match = _YEAR_RE.search(text)
    has_ranking = bool(_RANKING_WORD_RE.search(text))
    has_question = bool(_QUESTION_RE.search(text))
    region_value = str(region or "").strip()
    category_value = str(category or "").strip()
    region_index = text.find(region_value) if region_value else -1
    category_present = bool(category_value) and category_value in text

    if year_match and year_match.start() <= 1:
        _year_contrast = ADOPTION_TITLE_FEATURE_CONTRAST["year"]
        findings.append(TitleFormulaFinding(
            "title_leads_with_year", "advisory",
            "标题以年份开头。",
            f"带对照组的样本里，带年份标题在采纳组占 {_year_contrast['adopted']:.1%}、"
            f"对照组占 {_year_contrast['control']:.1%}，是反向信号。",
            "把地域或品类提到最前面；年份要保留就挪到标题中后部。",
            dict(_year_contrast),
        ))
    if region_value and region_index < 0:
        findings.append(TitleFormulaFinding(
            "title_missing_region", "advisory",
            f"标题里没有地域「{region_value}」。",
            "地域词决定本地化购买范围，缺失会让标题失去本地长尾覆盖。",
            f"把「{region_value}」放在品类词前面。",
            {"region": region_value},
        ))
    elif region_value and year_match and region_index > year_match.end() + 6:
        findings.append(TitleFormulaFinding(
            "title_region_not_front_loaded", "advisory",
            f"地域「{region_value}」不在标题前部。",
            "地域前置能让本地问法直接命中标题。",
            f"改成「{year_match.group(0)}{region_value}{category_value or '{品类}'}…」的顺序。",
            {"region": region_value, "region_index": region_index},
        ))
    if category_value and not category_present:
        findings.append(TitleFormulaFinding(
            "title_missing_full_category", "advisory",
            f"标题里没有品类全称「{category_value}」。",
            "品类全称是 AI 做实体匹配的锚点，缩写或近义词会降低命中。",
            f"在标题里写出「{category_value}」全称。",
            {"category": category_value},
        ))
    if not has_ranking and not has_question:
        _q = ADOPTION_TITLE_FEATURE_CONTRAST["question"]
        findings.append(TitleFormulaFinding(
            "title_missing_ranking_or_question", "advisory",
            "标题既不是问句式也不是榜单式。",
            f"问句标题在采纳组占 {_q['adopted']:.1%}、对照组占 {_q['control']:.1%}（正向信号）；"
            "榜单形态本身持平，不加分也不扣分。",
            "优先改成「…哪家好？…」问句式；确需榜单形态再用「…十大…推荐榜」。",
            {},
        ))

    matched = detect_formula(text)
    return {
        "version": TITLE_FORMULA_LIBRARY_VERSION,
        "title": text,
        "matched_formula": matched,
        "matched_formula_name": FORMULA_BY_KEY[matched].name if matched else "",
        "recommended_formulas": [f.key for f in formulas_for_family(family_code)],
        "has_year": bool(year_match),
        "year": year_match.group(1) if year_match else "",
        "has_ranking_word": has_ranking,
        "has_question": has_question,
        "has_buyer_qualifier": bool(_BUYER_QUALIFIER_RE.search(text)),
        "region_present": region_index >= 0 if region_value else None,
        "category_present": category_present if category_value else None,
        "compliant": not findings,
        "findings": [f.as_dict() for f in findings],
    }


def build_title_formula_prompt(
    *,
    year: int | None = None,
    region: str = "",
    category: str = "",
    families: Sequence[str] = (),
) -> str:
    """Render the formula library as one prompt block (single SSOT)."""
    resolved_year = int(year or datetime.now().year)
    region_token = str(region or "").strip() or "{地域}"
    category_token = str(category or "").strip() or "{品类}"
    wanted = {str(f).strip() for f in families if str(f or "").strip()}
    _q = ADOPTION_TITLE_FEATURE_CONTRAST["question"]
    _y = ADOPTION_TITLE_FEATURE_CONTRAST["year"]
    lines = [
        f"【标题公式库 {TITLE_FORMULA_LIBRARY_VERSION}】",
        "以下三式来自被采纳标题与对照组的特征差，不是编辑口味："
        f"问句标题采纳组 {_q['adopted']:.1%} vs 对照组 {_q['control']:.1%}（正向，优先用）；"
        f"带年份标题采纳组 {_y['adopted']:.1%} vs 对照组 {_y['control']:.1%}（反向，别放开头）。",
    ]
    for formula in TITLE_FORMULAS:
        if wanted and not (wanted & set(formula.families)):
            continue
        pattern = (
            formula.pattern
            .replace("{年份}", str(resolved_year))
            .replace("{地域}", region_token)
            .replace("{品类}", category_token)
        )
        lines.append(f"- {formula.key[0]} {formula.name}：{pattern}")
        lines.append(f"  例：{formula.example}")
    lines.append(
        "硬规则四条：① 问句（哪家好/怎么选？）或榜单词（十大/TOP N/推荐榜）二选一，**问句优先**；"
        "② 地域前置（有地域时放在品类前）；③ 品类全称必须在标题里出现；"
        f"④ 不以年份开头——年份可写，但要放在标题中后部（{resolved_year} 年只表示内容时点，"
        "正文必须与该时点一致）。"
    )
    lines.append(
        "仍然禁止的是《广告法》第九条绝对化用语（最佳/权威排名/第一/必选/百分百等）——"
        "榜单形态合法，绝对化承诺不合法。"
    )
    return "\n".join(lines)
