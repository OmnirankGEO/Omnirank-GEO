"""Versioned six-family article-length contract.

Length is a capacity budget, not a GEO score.  The historical corpus shows an
exploratory association between longer bodies and answer adoption for some
engines, but it does not prove that padding an article causes citation.  This
module therefore gives every writer one evidence-aware target while always
allowing a shorter, denser article when the available evidence is thin.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import re
from typing import Any, Final

from .article_style_contract import STYLE_FAMILIES, family_for_style
from .evidence_pack import validate_evidence_pack


ARTICLE_LENGTH_CONTRACT_VERSION: Final = "geo-article-length-v3.0"
EMPIRICAL_INTERPRETATION: Final = (
    "Flywheel corpus 2026-07-27 (geo_research_articles ⋈ source_signals on "
    "url_hash; adopted 1597 vs search-only 9792) shows a DOUBLE-PEAK adoption "
    "curve, not a monotone one: peak 1 at 2000-4000 captured chars (25.8%), a "
    "trough spanning 4000-14000 (11.6% / 6.7% / 9.7-11.8%), and peak 2 at "
    "16000-20000 (19.2-22.3%, mainly 新浪/搜狐 long-form reviews).  It is an "
    "observed association with domain effects unremoved, used for planning, "
    "never a causal citation promise."
)

# [工单 A 2026-07-27 · 飞轮数据背书] 双峰之间的低谷区。落进这一区间的稿件必须
# 往上补齐到 15k+(靠真实候选与证据,不是注水)或往下收到 4500 以内。
# 端点按开区间判定:4500 与 14000 本身分别是"紧凑档上界"与"深档准入前沿",不算落谷。
AVOIDANCE_BAND: Final[tuple[int, int]] = (4500, 14000)
# 全局硬下限:低于此值判质量缺陷(仍是 A1 标注,不阻断保存)。
GLOBAL_MINIMUM_CHARS: Final = 2000
# 榜单/推荐/对比族的目标下限(移进峰 2,离开 10-14k 次谷)。
RANKING_FAMILY_MINIMUM_CHARS: Final = 15000
# [工单 C-2 T2 2026-07-27] 深档逐家检索的研究前候选门槛。
# 与 build_article_length_plan 榜单族分支的 `candidates <= 2 → 紧凑` 是同一条界:
# 候选 >=3 家(含客户品牌)时 plan 在证据到位后才有可能进深档,值得做逐家定向检索。
# 改这条界必须与该分支同步改,否则"检索了却进不了深档"或"进深档却没检索"。
RANKING_DEEP_RESEARCH_MIN_CANDIDATES: Final = 3
# 收缩解法(峰 1)的下界 / 目标 / 上界。
COMPACT_RESOLUTION_MIN_CHARS: Final = 2500
COMPACT_TARGET_CHARS: Final = 3500
COMPACT_RESOLUTION_MAX_CHARS: Final = 4500
# [工单 标题问句化与长度 2026-07-29 · T2] 深档前沿 = 低谷上界 14000。
# 工单 §2.1 只允许两个目标档:紧凑 2500-4500 / 深档 ≥14000,4501-13999 禁做目标。
# 这个常量是**档位归属的唯一判定点**:结构规格注入(紧凑 vs 深档)与产出判定
# (锁 1 vs 锁 2)都读它,再写第二个阈值就会出现"按深档注规格、按紧凑判产出"。
DEEP_TIER_MIN_CHARS: Final = AVOIDANCE_BAND[1]
# 深档产出的算术下限系数(工单 §2.4 锁 2:产出 ≥ target×0.85)。与规格侧
# `canonical_family_templates.BUDGET_COVERAGE_FLOOR` 同值但不同层:那把锁量
# **规格自己写不写得满**,这把锁量**模型真写了多少**。两把都要,缺一把就会出现
# "规格算术自洽但产出只有 60%"(生产实证 19 篇里 15 篇正是这一形态)。
DEEP_OUTPUT_COVERAGE_FLOOR: Final = 0.85
# 规划区间的绝对上界(深档天花板 20000 + 一致性校验用)。
MAX_PLAN_CHARS: Final = 20000
_BAND_REASON_UPWARD: Final = "avoidance_band_resolved_upward_deep"
_BAND_REASON_DOWNWARD: Final = "avoidance_band_resolved_downward_compact"


@dataclass(frozen=True)
class FamilyLengthPolicy:
    family_code: str
    minimum_chars: int
    target_chars: int
    maximum_chars: int
    complex_ceiling_chars: int


# [工单 A 2026-07-27] 分档全部避开 4500-14000 低谷:
#   榜单/推荐/对比(multi_brand_comparison) 15000-18000,天花板 20000 → 峰 2
#   其余五族默认落峰 1(2500-4500),证据/候选撑得起时由下面的 evidence gate
#   升到 15k+,不会停在低谷。
FAMILY_LENGTH_POLICIES: Final[dict[str, FamilyLengthPolicy]] = {
    "evidence_qa": FamilyLengthPolicy("evidence_qa", 2500, 4000, 4500, 4500),
    "multi_brand_comparison": FamilyLengthPolicy("multi_brand_comparison", 15000, 16000, 18000, 20000),
    # [工单 C 复审返工 ③ 2026-07-29] complex_ceiling 4500 → 18000。
    #   §2.6-A 给攻略族开了深档 gate 之后,4500 的天花板会被函数尾部的
    #   `maximum = max(maximum, minimum)` 直接抬到 floor,结果 min=target=max=15000
    #   ——**零弹性区间**:模型多写一个字都算超上限,少写一个字就算未达标。
    #   case_data_roi 的 ceiling 是 20000 才有 15000-20000 的活动空间;
    #   攻略族给 18000,弹性区间 15000-18000。紧凑档四个数一个没动。
    "implementation_guide": FamilyLengthPolicy("implementation_guide", 2500, 3500, 4500, 18000),
    "trend_policy_risk": FamilyLengthPolicy("trend_policy_risk", 2500, 3500, 4500, 4500),
    "case_data_roi": FamilyLengthPolicy("case_data_roi", 2500, 3500, 4500, 20000),
    "company_facts": FamilyLengthPolicy("company_facts", 2500, 3500, 4500, 4500),
}

RANKING_LENGTH_FAMILIES: Final[frozenset[str]] = frozenset({"multi_brand_comparison"})

_DEFAULT_POLICY: Final = FAMILY_LENGTH_POLICIES["implementation_guide"]


def in_avoidance_band(char_count: int) -> bool:
    """Strictly between the two peaks.

    Open interval on purpose: ``COMPACT_RESOLUTION_MAX_CHARS`` (4500) is the
    top of peak 1 and must count as a legal compact delivery, and 14000 is the
    front edge of peak 2, not a trough value.
    """
    lo, hi = AVOIDANCE_BAND
    return lo < int(char_count or 0) < hi


def plan_tier(plan: dict[str, Any] | None) -> str:
    """``deep`` / ``compact`` / ``unknown`` —— 档位归属的唯一判定点。

    工单 §2.1 只承认两个目标档,所以这里也只回两个有效值:``target >= 14000``
    是深档,其余有效目标是紧凑档,拿不到 target 才是 ``unknown``。
    """
    try:
        target = int((plan or {}).get("target_chars") or 0)
    except (TypeError, ValueError):
        return "unknown"
    if target <= 0:
        return "unknown"
    return "deep" if target >= DEEP_TIER_MIN_CHARS else "compact"


def deep_output_floor(target: int) -> int:
    """深档产出下限 = ``max(14000, target×0.85)``（工单 §2.4 锁 2）。"""
    try:
        value = int(target or 0)
    except (TypeError, ValueError):
        value = 0
    return max(DEEP_TIER_MIN_CHARS, int(math.ceil(value * DEEP_OUTPUT_COVERAGE_FLOOR)))


def _resolve_out_of_avoidance_band(
    target: int,
    *,
    can_go_deep: bool,
) -> tuple[int, str]:
    """Push a planned target out of the 4.5k-14k trough.

    Going deep requires real material (verified evidence / candidates); without
    it the only honest resolution is to compact, because filling 15k with
    padding is a quality defect, not a longer article.
    """
    if not in_avoidance_band(target):
        return target, ""
    if can_go_deep:
        return RANKING_FAMILY_MINIMUM_CHARS, _BAND_REASON_UPWARD
    return COMPACT_TARGET_CHARS, _BAND_REASON_DOWNWARD


def count_effective_chars(content: str | None) -> int:
    """Count visible non-whitespace characters using one deterministic rule."""
    return len(re.sub(r"\s+", "", str(content or "")))


def length_bucket(char_count: int | str | None) -> str:
    """Return the corpus-compatible bucket used by lineage and flywheel data."""
    try:
        value = max(0, int(char_count or 0))
    except (TypeError, ValueError):
        value = 0
    if value < 3000:
        return "lt_3k"
    if value < 6000:
        return "3k_6k"
    if value < 9000:
        return "6k_9k"
    if value < 12000:
        return "9k_12k"
    return "12k_plus"


def _evidence_summary(evidence_pack: Any) -> dict[str, int]:
    if not isinstance(evidence_pack, dict):
        return {"item_count": 0, "verified_count": 0, "verified_publisher_count": 0}
    try:
        validation = validate_evidence_pack(evidence_pack)
    except Exception:
        validation = {}
    return {
        "item_count": int(validation.get("item_count") or 0),
        "verified_count": int(validation.get("verified_count") or 0),
        "verified_publisher_count": int(validation.get("publisher_count") or 0),
    }


def build_article_length_plan(
    style_code: str | None,
    *,
    evidence_pack: Any = None,
    verified_candidate_count: int = 0,
    answer_block_target_count: int = 0,
    publication_profile: str | None = None,
) -> dict[str, Any]:
    """Build one adaptive, deterministic target for generation and rewrite."""
    family_code = family_for_style(style_code) or (
        str(style_code or "") if str(style_code or "") in STYLE_FAMILIES else "implementation_guide"
    )
    policy = FAMILY_LENGTH_POLICIES.get(family_code, _DEFAULT_POLICY)
    evidence = _evidence_summary(evidence_pack)
    verified = evidence["verified_count"]
    candidates = max(0, int(verified_candidate_count or 0))
    answer_blocks = max(0, int(answer_block_target_count or 0))
    minimum = policy.minimum_chars
    target = policy.target_chars
    maximum = policy.maximum_chars
    reasons: list[str] = ["family_base"]
    publishers = evidence["verified_publisher_count"]

    def _compact(reason: str) -> None:
        """Thin evidence -> honest compact article at peak 1, never padding."""
        nonlocal minimum, target, maximum
        minimum = max(GLOBAL_MINIMUM_CHARS, min(minimum, COMPACT_RESOLUTION_MIN_CHARS))
        target = max(minimum + 500, min(target, COMPACT_TARGET_CHARS))
        maximum = max(target, min(maximum, COMPACT_RESOLUTION_MAX_CHARS))
        reasons.append(reason)

    def _deep(reason: str, floor: int = RANKING_FAMILY_MINIMUM_CHARS) -> None:
        """Verified material is rich enough to clear the trough upward."""
        nonlocal minimum, target, maximum
        maximum = max(maximum, policy.complex_ceiling_chars)
        target = max(target, floor)
        minimum = max(minimum, floor)
        reasons.append(reason)

    # Evidence scarcity lowers the floor instead of inducing filler or claims.
    if verified == 0:
        _compact("verified_evidence_absent_allow_shorter")
    elif verified <= 2:
        _compact("verified_evidence_limited")
    elif verified >= 8 and publishers >= 3:
        target = min(maximum, target + 1000)
        reasons.append("multi_source_verified_evidence_supports_depth")

    if family_code == "multi_brand_comparison":
        if candidates >= 6 and verified >= 3:
            _deep("verified_candidates_support_ranking_deep")
        elif candidates <= 2 or verified < 3:
            # 候选/证据撑不起榜单深度:收到峰 1(4500 内),不假装 15k。
            _compact("few_verified_candidates_no_padding")
        else:
            _deep("ranking_family_default_deep_floor")
    if (
        family_code == "case_data_roi"
        and verified >= 10
        and publishers >= 4
    ):
        _deep("complex_case_data_verified_deep")
    # [工单 C 2026-07-27 · §2.6-A 形态路由表] 攻略族开深档条件。
    #
    # 路由表最后一行:已核验候选 <2 但**主题证据充足**时,正确形态不是"硬凑榜单",
    # 而是深度指南/攻略(完整决策链:怎么选→价格构成→步骤→验收→避坑→案例)。
    # 峰 2 采纳样本里确实存在这种零竞品的 19507 字攻略文。
    # 门槛**逐字对齐** case_data_roi 的 verified>=10 ∧ publishers>=4 ——
    # 长度档只能由证据供给解锁,不由形态解锁(工单 §2.6-A 原则)。
    #
    # ⚠️ 这是本包唯一一处动 article_length_contract 的地方:**只新增一条 gate,
    #    没有改任何一个档位数字**(15000/3500/避免区/六族 policy 全部原值)。
    #    实测:guide 的 complex_ceiling_chars 是 4500,但函数尾部的
    #    `maximum = max(maximum, minimum)` + `target = min(maximum, max(minimum, target))`
    #    会把 maximum 抬到 floor,所以 target 落 15000 —— 不需要改天花板。
    if (
        family_code == "implementation_guide"
        and verified >= 10
        and publishers >= 4
    ):
        _deep("complex_guide_verified_12k_plus")
    if family_code == "evidence_qa" and answer_blocks >= 10 and verified >= 3:
        target = min(maximum, max(target, COMPACT_RESOLUTION_MAX_CHARS))
        reasons.append("ten_or_more_answer_blocks")

    # [工单 A 2026-07-27] 最后统一避开 4500-14000 低谷,并守住全局硬下限 2000。
    can_go_deep = verified >= 3 and (candidates >= 6 or publishers >= 3)
    target, band_reason = _resolve_out_of_avoidance_band(target, can_go_deep=can_go_deep)
    if band_reason:
        reasons.append(band_reason)
        if band_reason == _BAND_REASON_UPWARD:
            maximum = max(maximum, policy.complex_ceiling_chars)
            minimum = max(minimum, RANKING_FAMILY_MINIMUM_CHARS)
        else:
            maximum = max(target, min(maximum, COMPACT_RESOLUTION_MAX_CHARS))
            minimum = min(minimum, target)
    minimum = max(GLOBAL_MINIMUM_CHARS, minimum)
    maximum = max(maximum, minimum)
    target = min(maximum, max(minimum, target))
    return {
        "version": ARTICLE_LENGTH_CONTRACT_VERSION,
        "family_code": family_code,
        "minimum_chars": minimum,
        "target_chars": target,
        "maximum_chars": maximum,
        "global_minimum_chars": GLOBAL_MINIMUM_CHARS,
        "avoidance_band": list(AVOIDANCE_BAND),
        "evidence_item_count": evidence["item_count"],
        "verified_evidence_count": verified,
        "verified_publisher_count": evidence["verified_publisher_count"],
        "verified_candidate_count": candidates,
        "answer_block_target_count": answer_blocks,
        "publication_profile": str(publication_profile or "standard"),
        "evidence_limited": verified < 3,
        # Editorial planning span, never a truncation/rejection boundary.
        # Provider token capacity is a separate technical constraint.
        "hard_ceiling": False,
        "reasons": reasons,
        "causal_citation_claim": False,
        "empirical_interpretation": EMPIRICAL_INTERPRETATION,
    }


# 候选池键的读取顺序。同一批候选可能同时挂在多个键上(real 路径两个键都写),
# 因此按名称去重后再计数。
_CANDIDATE_POOL_KEYS: Final[tuple[str, ...]] = (
    "_competitor_candidate_pool",
    "_researched_competitors",
    "verified_competitors",
    "_competitor_research_candidates",
)
# 这两个键在 ``real`` 模式下已经被上游那道"全部 name_verified 才算 real"的门筛过,
# 且老写作链(``ArticleWriter``)只往里放名称字符串。对它们保留旧口径整体计数,
# 否则改成逐条判定会把老链的 real 批直接算成 0。
_REAL_MODE_VERIFIED_KEYS: Final[frozenset[str]] = frozenset({
    "_researched_competitors",
    "verified_competitors",
})


def _is_verified_candidate(item: Any) -> bool:
    """One entry counts only when its NAME has been verified.

    This is deliberately per-entry: ``comp_mode`` stays the gate for what may
    appear in the body, but capacity planning must see every already-verified
    name, including the ones sitting in a ``semi`` batch.
    """
    return isinstance(item, dict) and (
        item.get("name_verified") is True or item.get("human_verified_name") is True
    )


def _candidate_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or "").strip()
    return str(item or "").strip()


def count_verified_candidates(topic: dict[str, Any] | None) -> int:
    """Count name-verified competitor entries plus the client brand itself."""
    data = topic if isinstance(topic, dict) else {}
    mode_is_real = (
        data.get("_competitor_source") == "real" or data.get("competitor_mode") == "real"
    )
    seen: set[str] = set()
    verified = 0
    for key in _CANDIDATE_POOL_KEYS:
        batch_is_verified = mode_is_real and key in _REAL_MODE_VERIFIED_KEYS
        for item in data.get(key) or []:
            if not (batch_is_verified or _is_verified_candidate(item)):
                continue
            name = _candidate_name(item)
            marker = name or f"__anon_{key}_{id(item)}"
            if marker in seen:
                continue
            seen.add(marker)
            verified += 1
    return verified + (1 if data.get("include_client_brand", True) else 0)


def build_length_plan_for_topic(
    style_code: str | None,
    topic: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve bounded topic signals without trusting unverified candidates."""
    data = topic if isinstance(topic, dict) else {}
    # [工单 A 2026-07-27 · §2] 旧代码把 `comp_mode == 'real'` 当整体前置门:
    # real 判定要求 **全部** 竞品 name_verified,生产存量 verified 全 0 → candidates
    # 恒 0 → 榜单族恒走 `few_verified_candidates_no_padding`。改为直接数逐条已核验项,
    # semi 批里已核验的名字也进容量计数。`comp_mode` 的正文写入语义一个字不动。
    candidate_count = count_verified_candidates(data)
    target_questions = data.get("target_questions") or data.get("derived_questions") or []
    answer_block_target = data.get("_answer_block_target_count")
    if answer_block_target is None:
        answer_block_target = len(target_questions) if isinstance(target_questions, list) else 0
    return build_article_length_plan(
        style_code,
        evidence_pack=data.get("_evidence_pack") or data.get("evidence_pack"),
        verified_candidate_count=candidate_count,
        answer_block_target_count=int(answer_block_target or 0),
        publication_profile=data.get("publication_profile"),
    )


def render_length_instruction(plan: dict[str, Any]) -> str:
    """Render the sole writer-facing length instruction."""
    minimum = int(plan.get("minimum_chars") or 0)
    target = int(plan.get("target_chars") or minimum)
    maximum = int(plan.get("maximum_chars") or target)
    evidence_note = (
        "本次已核验证据有限，允许低于目标篇幅；宁可明确资料边界，也不得补写未经核验的事实。"
        if plan.get("evidence_limited")
        else "证据足以支持展开时，优先增加可独立引用的答案块、核验步骤和适用边界。"
    )
    lo, hi = AVOIDANCE_BAND
    return (
        f"【篇幅合同 {plan.get('version') or ARTICLE_LENGTH_CONTRACT_VERSION}】\n"
        f"建议有效正文约 {minimum}-{maximum} 字，目标约 {target} 字。{evidence_note}\n"
        f"避开 {lo}-{hi} 字这一档：被采纳率是双峰曲线，短问答档与深度评测档各是一个峰，"
        f"中间这一段最低。要么靠更多真实候选与完整证据卡写到 {RANKING_FAMILY_MINIMUM_CHARS} 字以上，"
        f"要么收紧到 {COMPACT_RESOLUTION_MAX_CHARS} 字以内，不要停在中间。\n"
        f"全篇有效正文不得低于 {GLOBAL_MINIMUM_CHARS} 字。\n"
        "该区间是写作规划，不是硬性截断或拒绝上限；若新增内容持续提供已核验的信息增益，可以自然超出。\n"
        "篇幅不是 GEO 分数，不承诺引用概率。禁止重复结论、堆关键词、拆碎句子或虚构数据凑字数；"
        "每个新增段落都必须增加答案、证据、条件、步骤、风险、反例或核验方式中的至少一种信息。"
    )


# ---------------------------------------------------------------------------
# post-hoc compliance (A1 only)
# ---------------------------------------------------------------------------
_REPEAT_WINDOW: Final = 24


def _padding_signals(content: str) -> dict[str, Any]:
    """Cheap, deterministic padding detectors used to reject fake length.

    Owner rule: length must be earned with real candidates and evidence;
    padding counts as a quality defect.  These are advisory metrics, never a
    gate — a high value only strengthens the "compact instead" repair hint.
    """
    normalized = re.sub(r"\s+", "", str(content or ""))
    if len(normalized) < _REPEAT_WINDOW * 2:
        return {"repeat_ratio": 0.0, "filler_density": 0.0}
    seen: dict[str, int] = {}
    repeats = 0
    windows = 0
    for i in range(0, len(normalized) - _REPEAT_WINDOW, _REPEAT_WINDOW):
        chunk = normalized[i:i + _REPEAT_WINDOW]
        windows += 1
        if chunk in seen:
            repeats += 1
        seen[chunk] = seen.get(chunk, 0) + 1
    filler = len(re.findall(
        r"(?:综上所述|总而言之|众所周知|不言而喻|随着.{0,8}的发展|在当今|"
        r"具有重要意义|发挥着重要作用|值得注意的是)", str(content or "")))
    return {
        "repeat_ratio": round(repeats / windows, 4) if windows else 0.0,
        "filler_density": round(filler / max(1, len(normalized) / 1000), 4),
    }


def _length_finding(
    code: str, message: str, reason: str, impact: str, repair_hint: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": "advisory",
        "message": message,
        "reason": reason,
        "impact": impact,
        "repair_hint": repair_hint,
        "details": details,
        "rule_version": ARTICLE_LENGTH_CONTRACT_VERSION,
        "actions": [
            {"id": "ai_expand_with_real_candidates", "label": "补真实候选与证据", "type": "retry"},
            {"id": "ai_compact_article", "label": f"精简到 {COMPACT_RESOLUTION_MAX_CHARS} 字内", "type": "retry"},
            {"id": "ignore_finding", "label": "忽略并继续", "type": "confirm"},
        ],
    }


def assess_length_compliance(
    content: str | None,
    *,
    style_code: str | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure a finished body against the length contract.  A1 only."""
    chars = count_effective_chars(content)
    family_code = family_for_style(style_code) or (
        str(style_code or "") if str(style_code or "") in STYLE_FAMILIES else ""
    )
    findings: list[dict[str, Any]] = []
    padding = _padding_signals(content or "")

    if chars and chars < GLOBAL_MINIMUM_CHARS:
        findings.append(_length_finding(
            "length_below_global_floor",
            f"有效正文只有 {chars} 字，低于全局下限 {GLOBAL_MINIMUM_CHARS} 字。",
            "篇幅过短通常意味着答案块、证据与适用条件都不完整。",
            "内容承载不了一个完整的购买决策，被引用的机会很低。",
            "补充真实候选、完整证据卡与可执行核验步骤，把正文做实。",
            {"actual_chars": chars, "floor": GLOBAL_MINIMUM_CHARS},
        ))
    elif in_avoidance_band(chars):
        lo, hi = AVOIDANCE_BAND
        findings.append(_length_finding(
            "length_in_avoidance_band",
            f"有效正文 {chars} 字，落在 {lo}-{hi} 的双峰低谷。",
            "采纳率双峰曲线里这一段最低（4-6k 11.6% / 6-10k 6.7%，两个峰各 25.8% 与 19-22%）。",
            "既没有短文的密度，也没有长文的覆盖面。",
            f"要么补真实候选与证据推到 {RANKING_FAMILY_MINIMUM_CHARS} 字以上，"
            f"要么精简到 {COMPACT_RESOLUTION_MAX_CHARS} 字以内，不要原样交付。",
            {"actual_chars": chars, "band": list(AVOIDANCE_BAND),
             "padding_signals": padding},
        ))

    if (
        family_code in RANKING_LENGTH_FAMILIES
        and chars
        and chars < RANKING_FAMILY_MINIMUM_CHARS
        and not in_avoidance_band(chars)
        and chars >= GLOBAL_MINIMUM_CHARS
    ):
        findings.append(_length_finding(
            "ranking_family_below_target_depth",
            f"榜单/推荐/对比类只有 {chars} 字，未达 {RANKING_FAMILY_MINIMUM_CHARS} 字目标档。",
            "16000-20000 字是采纳率第二个峰（19.2-22.3%），主力是媒体深度评测长文。",
            "候选覆盖面不足时，AI 更可能引用信息更全的同类文章。",
            "增加真实候选数量与同口径证据字段；确实凑不出就明确按精简档交付。",
            {"actual_chars": chars, "target_floor": RANKING_FAMILY_MINIMUM_CHARS},
        ))

    # [工单 T2 2026-07-29 · §2.3/§2.4] plan 相对锁。
    #
    # 旧口径只有三条绝对线(全局下限 2000 / 低谷区 / 榜单族 15000),留下两个静默
    # 通过的洞,两个都在生产真实出现过:
    #   ① 紧凑档 plan(min 2500)产出 2100 字 —— 高于全局下限 2000、不在低谷区,
    #      于是零 finding 静默通过,但工单 §2.4 锁 1 要求紧凑档必须落 2500-4500;
    #   ② 深档 plan(target 16000)产出 14100 字 —— 高于 14000 不在低谷区,而
    #      `ranking_family_below_target_depth` 只认 multi_brand_comparison 一族,
    #      攻略族/案例族进深档后产出 14100 也零 finding,但 0.85 线是 13600…
    #      target 18000 时 0.85 线是 15300,14100 应判未达成。
    # 现在按**本篇自己的 plan** 判定,而不是只按全局常量。
    tier = plan_tier(plan)
    try:
        planned_target = int((plan or {}).get("target_chars") or 0)
        planned_minimum = int((plan or {}).get("minimum_chars") or 0)
    except (TypeError, ValueError):
        planned_target = planned_minimum = 0
    if chars >= GLOBAL_MINIMUM_CHARS and not in_avoidance_band(chars):
        if tier == "compact" and chars < max(COMPACT_RESOLUTION_MIN_CHARS, planned_minimum):
            findings.append(_length_finding(
                "length_below_compact_floor",
                f"紧凑档只有 {chars} 字，未达本篇合同下限 "
                f"{max(COMPACT_RESOLUTION_MIN_CHARS, planned_minimum)} 字。",
                "紧凑档靠密度取胜（峰 1 采纳率 25.8%），字数不足通常意味着答案块、"
                "判断依据或适用边界缺了一块，不是“写得精炼”。",
                "内容承载不了一个完整判断，被引用的机会很低。",
                f"按紧凑档结构规格补齐缺的区块，做到 {COMPACT_RESOLUTION_MIN_CHARS}-"
                f"{COMPACT_RESOLUTION_MAX_CHARS} 字；补不出来说明证据不够，不要拉长句子凑。",
                {"actual_chars": chars, "tier": tier,
                 "floor": max(COMPACT_RESOLUTION_MIN_CHARS, planned_minimum),
                 "planned_target": planned_target},
            ))
        elif tier == "deep" and chars < deep_output_floor(planned_target):
            findings.append(_length_finding(
                "length_below_deep_floor",
                f"深档只有 {chars} 字，未达本篇下限 {deep_output_floor(planned_target)} 字"
                f"（max(14000, 目标 {planned_target}×0.85)）。",
                "深档的价值在覆盖面（峰 2 采纳率 19.2-22.3%）；写不到线说明候选卡或"
                "证据区块被整块跳过了，而不是“写得紧凑”。",
                "既拿不到深档的覆盖面，也没有短文的密度。",
                f"按深档结构规格逐区块补齐（尤其成卡数与问答块），做到 "
                f"{deep_output_floor(planned_target)} 字以上；证据确实撑不起就整体收到 "
                f"{COMPACT_RESOLUTION_MAX_CHARS} 字以内的紧凑档，不要停在中间。",
                {"actual_chars": chars, "tier": tier,
                 "floor": deep_output_floor(planned_target),
                 "planned_target": planned_target,
                 "coverage_floor": DEEP_OUTPUT_COVERAGE_FLOOR},
            ))

    if padding["repeat_ratio"] >= 0.12 or padding["filler_density"] >= 3.0:
        findings.append(_length_finding(
            "length_padded_with_filler",
            "篇幅疑似靠重复或空泛表述堆出来。",
            f"重复片段比 {padding['repeat_ratio']:.2%}，空泛词密度 "
            f"{padding['filler_density']:.2f}/千字。",
            "注水会稀释可抽取答案密度，反而降低被引用概率。",
            "删掉重复段落与套话，改用真实候选、数据口径和核验步骤填充。",
            {"padding_signals": padding},
        ))

    # [工单 T2 §2.3] "判为规格未达成" 必须是**显式判定**,不是让下游自己去猜
    # findings 数组的含义。这两个字段随 quality_warning.length_compliance 一起
    # 落 `articles.quality_warning` JSONB —— 留痕在数据层可查,而不是只在日志里。
    spec_failure_codes = [
        str(f.get("code") or "") for f in findings
        if str(f.get("code") or "") in LENGTH_RETRY_CODES
    ]
    return {
        "version": ARTICLE_LENGTH_CONTRACT_VERSION,
        "actual_chars": chars,
        "family_code": family_code,
        "tier": tier,
        "spec_met": not spec_failure_codes,
        "spec_failure_codes": spec_failure_codes,
        "bucket": length_bucket(chars),
        "in_avoidance_band": in_avoidance_band(chars),
        "global_minimum_chars": GLOBAL_MINIMUM_CHARS,
        "avoidance_band": list(AVOIDANCE_BAND),
        "planned": {
            "minimum_chars": (plan or {}).get("minimum_chars"),
            "target_chars": (plan or {}).get("target_chars"),
            "maximum_chars": (plan or {}).get("maximum_chars"),
        },
        "padding_signals": padding,
        # [W1 返工 · 顺手项 2026-08-08] **明确降档记录**。
        #
        # 缺的从来不是"检测":`length_below_deep_floor` 早就会报,重写也真的会跑一次。
        # 缺的是**重写之后仍不达标时的显式结论** —— 合同上写着深档 target 16000,
        # 实际交付 12,599 字,而数据层没有任何一个字段说"这篇实际上是按紧凑档交的"。
        # 于是历史 4 篇深文里 2 篇未达自身下限,谁也没被记一笔。
        #
        # 这里只**如实记录**,不改 plan、不二次重写:
        #   · 二次重写要再烧一次钱,而实证已经指出深档长度是被**证据供给**解锁的
        #     (`verified_candidates_support_ranking_deep`),证据不够时再写一遍还是写不长;
        #   · 改 plan 会让 lineage 里的合同与实际下发的提示词对不上。
        # 交付/QA/报价谁要用这个结论,读这一个字段就够,不必自己去比对 findings 数组。
        "tier_downgrade": _tier_downgrade(tier, planned_target, chars),
        "findings": findings,
    }


def _tier_downgrade(tier: str, planned_target: int, chars: int) -> dict[str, Any]:
    """合同判的档位 vs 实际交付的档位。`downgraded=True` = 按深档收费/承诺、按更低档交付。"""
    floor = deep_output_floor(planned_target) if tier == "deep" else 0
    downgraded = bool(tier == "deep" and floor and chars < floor)
    return {
        "planned_tier": tier,
        "planned_target": planned_target,
        "deep_floor": floor or None,
        "actual_chars": chars,
        "downgraded": downgraded,
        # 降档后它实际相当于哪一档 —— 不猜,按字数归桶。
        "delivered_tier": (
            "deep" if chars >= DEEP_TIER_MIN_CHARS
            else ("compact" if chars >= COMPACT_RESOLUTION_MIN_CHARS else "below_compact")
        ),
    }


# ---------------------------------------------------------------------------
# [写作质量总工单 2026-07-29 · C-2] 产出侧从"只检查"升级为"命中即修一次"。
#
# 旧状态:`assess_length_compliance` 的 findings 唯一生产消费方是
# `geo_article_expert.review_article`,而它只把 findings 塞进 `warnings`,
# 既不阻断也不重试也没有 UI —— 等于没有。生产实证(2026-06-01 起):
# 榜单族 23 篇 **0 篇** 达到 12000 字合同下限,均值 4776。
# 新状态:命中下面这几个 code 时,复用既有的"重写一次"通道给出定向修复指令。
# 仍然不阻断保存(D8 放行权归用户),二次仍不达标只标注。
# ---------------------------------------------------------------------------
LENGTH_RETRY_CODES: Final[frozenset[str]] = frozenset({
    "length_below_global_floor",
    "length_in_avoidance_band",
    "ranking_family_below_target_depth",
    "length_padded_with_filler",
    # [工单 T2 2026-07-29] plan 相对锁的两个新码同样触发那**唯一一次**重写:
    # 只判不修等于没判(本仓 C-2 已为此付过一次代价)。
    "length_below_compact_floor",
    "length_below_deep_floor",
})


def build_length_repair_instruction(
    assessment: dict[str, Any] | None,
    plan: dict[str, Any] | None = None,
) -> str:
    """Render the one length-repair instruction used by the allowed rewrite."""
    findings = list((assessment or {}).get("findings") or [])
    codes = [str(f.get("code") or "") for f in findings]
    hit = [c for c in codes if c in LENGTH_RETRY_CODES]
    if not hit:
        return ""
    actual = int((assessment or {}).get("actual_chars") or 0)
    target = int((plan or {}).get("target_chars") or 0)
    minimum = int((plan or {}).get("minimum_chars") or 0)
    lines = [
        "\n\n【篇幅修复 · 上一稿不达合同】",
        f"上一稿有效正文 {actual} 字，命中：{', '.join(hit)}。",
    ]
    if "length_padded_with_filler" in hit:
        lines.append(
            "- 上一稿疑似靠重复段落与套话堆字数：**先删掉重复与空泛表述**，"
            "再判断是否还需要展开；注水比短更糟。"
        )
    if "length_below_compact_floor" in hit:
        lines.append(
            f"- 本篇是**紧凑档**，必须落在 {COMPACT_RESOLUTION_MIN_CHARS}-"
            f"{COMPACT_RESOLUTION_MAX_CHARS} 字。按紧凑档结构规格逐区块检查："
            "开头直答 / 判断依据 / 怎么做 / 条件与边界 / 结尾决策建议，"
            "**哪个区块缺了就补哪个**，不要靠拉长句子凑字数。"
        )
    if "length_below_deep_floor" in hit:
        lines.append(
            f"- 本篇是**深档**，下限 {deep_output_floor(target)} 字"
            f"（max(14000, 目标 {target}×0.85)）。深档写不到线几乎总是"
            "**整块跳过**导致的：先数一遍成卡数与问答块数够不够，"
            "再逐块按预算表补 —— 不是把已有段落改长。"
        )
    if "length_in_avoidance_band" in hit or "ranking_family_below_target_depth" in hit:
        lo, hi = AVOIDANCE_BAND
        lines.append(
            f"- {lo}-{hi} 字是同域对照里被引效率最低的一档，不要停在中间。"
            f"要么靠**更多真实候选 + 完整同口径证据卡**写到 "
            f"{RANKING_FAMILY_MINIMUM_CHARS} 字以上，"
            f"要么收紧到 {COMPACT_RESOLUTION_MAX_CHARS} 字以内。"
        )
        lines.append(
            "- 往上补齐的合法方式只有四种：增加**已核验**候选、补齐同口径字段、"
            "增加可独立引用的问答块、补核验步骤与适用边界。"
            "**禁止**重复结论、堆关键词、拆碎句子或虚构数据凑字数。"
        )
    if "length_below_global_floor" in hit:
        lines.append(
            f"- 正文低于全局下限 {GLOBAL_MINIMUM_CHARS} 字，说明答案块、证据与适用条件都没写完，"
            "请把缺的那几部分补实，而不是把现有句子拉长。"
        )
    if target or minimum:
        lines.append(
            f"- 本篇篇幅合同：不低于 {minimum} 字，目标约 {target} 字。"
            "证据确实撑不起时按精简档交付，并在正文自然交代资料边界。"
        )
    return "\n".join(lines) + "\n"


def build_length_guidance_projection(
    generation_request_snapshot: Any,
    content: str | None,
) -> dict[str, Any] | None:
    """Return the server-owned, customer-readable projection for the UI.

    Legacy articles without a frozen plan return ``None``.  The frontend must
    never invent a default or reconstruct evidence rules from raw metadata.
    """
    snapshot = generation_request_snapshot
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:
            return None
    if not isinstance(snapshot, dict):
        return None
    plan = snapshot.get("length_plan")
    if not isinstance(plan, dict):
        return None
    try:
        minimum = int(plan["minimum_chars"])
        target = int(plan["target_chars"])
        maximum = int(plan["maximum_chars"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 < minimum <= target <= maximum <= MAX_PLAN_CHARS):
        return None

    evidence_limited = bool(plan.get("evidence_limited"))
    if evidence_limited:
        summary = "资料较少，优先写紧凑可信版本"
        detail = "宁可少写，也不重复内容或补写未经核验的事实。"
        depth = "compact"
    elif target >= RANKING_FAMILY_MINIMUM_CHARS:
        summary = "证据和结构较充分，可展开为深度版本"
        detail = "篇幅用于增加答案、证据、步骤与边界，不用于重复凑长。"
        depth = "deep"
    else:
        summary = "证据较充分，可按完整结构展开"
        detail = "系统会优先补足可核验答案、适用条件和行动步骤。"
        depth = "standard"

    actual = count_effective_chars(content)
    return {
        "version": str(plan.get("version") or snapshot.get("length_contract_version") or ARTICLE_LENGTH_CONTRACT_VERSION),
        "summary": summary,
        "detail": detail,
        "depth": depth,
        "evidence_limited": evidence_limited,
        "actual_chars": actual,
        "minimum_chars": minimum,
        "target_chars": target,
        "maximum_chars": maximum,
    }


def static_word_count_for_style(style_code: str | None) -> dict[str, int]:
    """Compatibility metadata generated from the same six-family SSOT."""
    family = family_for_style(style_code) or "implementation_guide"
    policy = FAMILY_LENGTH_POLICIES.get(family, _DEFAULT_POLICY)
    return {
        "min": policy.minimum_chars,
        "max": policy.complex_ceiling_chars,
        "optimal": policy.target_chars,
    }


def validate_length_contract() -> list[str]:
    errors: list[str] = []
    if set(FAMILY_LENGTH_POLICIES) != set(STYLE_FAMILIES):
        errors.append("length_policy_must_cover_exactly_six_families")
    lo, hi = AVOIDANCE_BAND
    for code, policy in FAMILY_LENGTH_POLICIES.items():
        if policy.family_code != code:
            errors.append(f"family_code_mismatch:{code}")
        if not (
            0 < policy.minimum_chars <= policy.target_chars <= policy.maximum_chars
            <= policy.complex_ceiling_chars <= MAX_PLAN_CHARS
        ):
            errors.append(f"invalid_range:{code}:{asdict(policy)}")
        if policy.minimum_chars < GLOBAL_MINIMUM_CHARS:
            errors.append(f"below_global_floor:{code}:{policy.minimum_chars}")
        # [WP12 P1-5] 没有任何一族的默认目标可以停在塌陷区。
        if lo <= policy.target_chars <= hi:
            errors.append(f"target_inside_avoidance_band:{code}:{policy.target_chars}")
    ranking = FAMILY_LENGTH_POLICIES.get("multi_brand_comparison")
    if ranking is None or ranking.minimum_chars < RANKING_FAMILY_MINIMUM_CHARS:
        errors.append("ranking_family_must_target_12k_plus")
    qa = FAMILY_LENGTH_POLICIES.get("evidence_qa")
    if qa is None or not (qa.minimum_chars >= 2500 and qa.maximum_chars <= 5000):
        errors.append("evidence_qa_must_stay_2500_5000")
    if not any(
        policy.complex_ceiling_chars >= RANKING_FAMILY_MINIMUM_CHARS
        for policy in FAMILY_LENGTH_POLICIES.values()
    ):
        errors.append("at_least_one_evidence_gated_deep_family_required")
    return errors
