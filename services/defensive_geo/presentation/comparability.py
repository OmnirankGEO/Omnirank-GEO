"""可比性投影(3×3 逐态闭合)—— MET-43 / Z-2.3。

规格 §15.6 L2743-2893。三档 ``level`` × 单侧/hybrid 的 section aggregate。

七维(§15.6 L2743 逐字,顺序即 precedence 之外的展示序):
``sameMode`` / ``sameResolvedEntity`` / ``sameQuestionSetVersion`` /
``samePlatformSet`` / ``sameModelRevisions`` / ``sameSurfaceAndSearchMode`` /
``sameResolverAndClassifierVersions``。

🔴 两个维度是「**致命**」的,不能降级成 partial
--------------------------------------------
``level='partial'`` 的 reason 类型逐字 ``Exclude<..., 'no_baseline' |
'entity_identity_changed' | 'mode_changed'>``:换了主体、换了目标模式,
两次测的**根本不是同一件事**,并列展示都算误导,只能 ``level='none'``。
本模块把这两条写成 :data:`FATAL_DIMENSIONS`,并在构造时强制 ——
不是靠调用方自觉。

🔴 Z-2.3:``no_comparable_baseline`` 分支**不许**是死路
------------------------------------------------------
§0.5.6 Z-2.3 明令把该分支的 ``nextAction: null`` 改为指向「建立同口径复测计划」。
所以 :func:`section_state_for` 对每一档都返回**非空**动作,
并且 ``baseline_not_comparable`` 那档额外带 ``contact_provider``(MET-43 逐字)。
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, NamedTuple, Sequence

REASON_POLICY_VERSION = "comparability_reason_priority_v1"
COMMITMENT_POLICY_VERSION = "public_comparison_commitment_v1"

Level = Literal["none", "full", "partial"]

#: §15.6 L2743 七维,**顺序固定**。判据拿它当分母,不手抄。
DIMENSION_KEYS: tuple[str, ...] = (
    "sameMode",
    "sameResolvedEntity",
    "sameQuestionSetVersion",
    "samePlatformSet",
    "sameModelRevisions",
    "sameSurfaceAndSearchMode",
    "sameResolverAndClassifierVersions",
)

#: §15.6 L2753 十个 reason code。
REASON_CODES: tuple[str, ...] = (
    "no_baseline",
    "entity_identity_changed",
    "mode_changed",
    "question_set_changed",
    "platform_set_changed",
    "model_revision_changed",
    "surface_or_search_mode_changed",
    "resolver_or_classifier_changed",
    "partial_overlap",
    "metric_shape_changed",
)

#: 维度 → 它失配时产生的 reason。**双射**(判据会验:七维七 reason,无重无漏)。
DIMENSION_TO_REASON: dict[str, str] = {
    "sameMode": "mode_changed",
    "sameResolvedEntity": "entity_identity_changed",
    "sameQuestionSetVersion": "question_set_changed",
    "samePlatformSet": "platform_set_changed",
    "sameModelRevisions": "model_revision_changed",
    "sameSurfaceAndSearchMode": "surface_or_search_mode_changed",
    "sameResolverAndClassifierVersions": "resolver_or_classifier_changed",
}

#: 🔴 换主体 / 换模式 ⇒ 强制 ``level='none'``,不得降级成 partial。
FATAL_DIMENSIONS: frozenset[str] = frozenset({"sameResolvedEntity", "sameMode"})

#: 有限 precedence(MET-43「finite precedence/dimensions exact」)。
#: 数字互不相同 —— 相同就无法确定"主 reason",而 DTO 只允许一个。
REASON_PRECEDENCE: dict[str, int] = {
    "no_baseline": 0,
    "entity_identity_changed": 10,
    "mode_changed": 20,
    "question_set_changed": 30,
    "platform_set_changed": 40,
    "model_revision_changed": 50,
    "surface_or_search_mode_changed": 60,
    "resolver_or_classifier_changed": 70,
    "metric_shape_changed": 80,
    "partial_overlap": 90,
}

#: partial 不允许承载的 reason(§15.6 L2881 的 Exclude 逐字)。
REASONS_FORBIDDEN_IN_PARTIAL: frozenset[str] = frozenset(
    {"no_baseline", "entity_identity_changed", "mode_changed"}
)


class ComparabilityError(ValueError):
    """形态不合法 ⇒ 投影失败,不下发一个"看起来合理"的对比。"""


class Reason(NamedTuple):
    code: str
    public_explanation_key: str
    reason_policy_version: str = REASON_POLICY_VERSION


class Projection(NamedTuple):
    level: Level
    reason: Reason | None
    baseline_snapshot_ref: str | None
    current_snapshot_ref: str
    baseline_as_of: str | None
    current_as_of: str
    matched_cells: int
    baseline_only_cells: int
    current_only_cells: int
    dimensions: dict[str, bool] | None
    metric_comparisons: tuple[Mapping[str, Any], ...]
    reason_policy_version: str = REASON_POLICY_VERSION
    commitment_policy_version: str = COMMITMENT_POLICY_VERSION


def _reason(code: str) -> Reason:
    if code not in REASON_PRECEDENCE:
        raise ComparabilityError(f"未知 comparability reason {code!r}")
    return Reason(code=code, public_explanation_key=f"comparability_{code}")


def primary_reason(dimensions: Mapping[str, bool]) -> str | None:
    """按 precedence 选**唯一**主 reason;全同则 None。"""
    failed = [
        DIMENSION_TO_REASON[k] for k in DIMENSION_KEYS if not dimensions.get(k, False)
    ]
    if not failed:
        return None
    return min(failed, key=lambda c: REASON_PRECEDENCE[c])


def _validate_dimensions(dimensions: Mapping[str, bool]) -> dict[str, bool]:
    missing = [k for k in DIMENSION_KEYS if k not in dimensions]
    if missing:
        raise ComparabilityError(f"缺少可比性维度:{missing}(七维必须齐全)")
    extra = [k for k in dimensions if k not in DIMENSION_KEYS]
    if extra:
        raise ComparabilityError(f"未知可比性维度:{extra}")
    for k in DIMENSION_KEYS:
        if not isinstance(dimensions[k], bool):
            raise ComparabilityError(f"维度 {k} 必须是布尔")
    return {k: bool(dimensions[k]) for k in DIMENSION_KEYS}


def _assert_instants_increase(baseline_as_of: str, current_as_of: str) -> None:
    """MET-43「baseline≠current 且 RFC3339 instant 递增」。

    比较用字符串序:RFC3339 UTC(``...Z``)在同一格式下字典序与时间序一致。
    这里不解析成 datetime —— 解析会引入时区歧义,而判据要的是"严格递增"。
    """
    if baseline_as_of == current_as_of:
        raise ComparabilityError("baselineAsOf 与 currentAsOf 相同,不构成对比")
    if not (baseline_as_of < current_as_of):
        raise ComparabilityError(
            f"currentAsOf({current_as_of}) 必须晚于 baselineAsOf({baseline_as_of})"
        )


def project(
    *,
    baseline_snapshot_ref: str | None,
    current_snapshot_ref: str,
    baseline_as_of: str | None,
    current_as_of: str,
    dimensions: Mapping[str, bool] | None,
    matched_cells: int,
    baseline_only_cells: int,
    current_only_cells: int,
    metric_comparisons: Sequence[Mapping[str, Any]] = (),
) -> Projection:
    """三档逐态闭合。每一档的 shape 都由 §15.6 逐字规定,这里逐条强制。"""
    if not current_snapshot_ref or not current_as_of:
        raise ComparabilityError("current root/asOf 必须存在(逐值等于 Core)")

    # ---- level='none' 且 reason='no_baseline':根本没有基线 ----
    if baseline_snapshot_ref is None:
        if baseline_as_of is not None or dimensions is not None:
            raise ComparabilityError(
                "no_baseline 档必须 baselineAsOf=null、dimensions=null"
            )
        if matched_cells != 0 or metric_comparisons:
            raise ComparabilityError("no_baseline 档不得带 matchedCells 或 comparison")
        return Projection(
            level="none", reason=_reason("no_baseline"),
            baseline_snapshot_ref=None, current_snapshot_ref=current_snapshot_ref,
            baseline_as_of=None, current_as_of=current_as_of,
            matched_cells=0, baseline_only_cells=baseline_only_cells,
            current_only_cells=current_only_cells,
            dimensions=None, metric_comparisons=(),
        )

    if baseline_as_of is None or dimensions is None:
        raise ComparabilityError("有基线时 baselineAsOf 与 dimensions 都必须存在")
    _assert_instants_increase(baseline_as_of, current_as_of)
    dims = _validate_dimensions(dimensions)
    code = primary_reason(dims)

    # ---- 致命维度 ⇒ 强制 none(不得降级成 partial)----
    #
    # 🔴 **纵深防御 · 非承重**(2026-08-21 变异实测记录)
    #    把下面这行改成 ``fatal = []`` 后,判据**全绿**(MUT-14 存活)。
    #    穷举 2^7 维度组合 × 4 种 cell 形态实测:仍有 **0** 个致命组合能到达
    #    ``level='partial'``。真正承重的是 REASONS_FORBIDDEN_IN_PARTIAL ——
    #    entity(10)/mode(20) 是 precedence 最低的两个,任一失配都会成为主 reason,
    #    而 partial 拒绝它们。
    #    这一段保留的价值是**错误信息更准**(直接说"换主体/换模式"而不是
    #    "partial 不得承载该 reason"),不是保证 level 正确。
    #    承重判据 = test_fatal_dimension_can_never_reach_partial(穷举不变式)。
    fatal = [k for k in FATAL_DIMENSIONS if not dims[k]]
    if fatal:
        if matched_cells != 0 or metric_comparisons:
            raise ComparabilityError(
                "换主体/换模式时不得下发任何 comparison(两次测的不是同一件事)"
            )
        return Projection(
            level="none", reason=_reason(code or "entity_identity_changed"),
            baseline_snapshot_ref=baseline_snapshot_ref,
            current_snapshot_ref=current_snapshot_ref,
            baseline_as_of=baseline_as_of, current_as_of=current_as_of,
            matched_cells=0, baseline_only_cells=baseline_only_cells,
            current_only_cells=current_only_cells,
            dimensions=dims, metric_comparisons=(),
        )

    # ---- level='full':七维全同、无单侧 cell、comparison 非空 ----
    if code is None and baseline_only_cells == 0 and current_only_cells == 0:
        if not metric_comparisons:
            raise ComparabilityError("full 档不得为空(MET-43「空 full……拒绝」)")
        for c in metric_comparisons:
            if c.get("comparable") is not True:
                raise ComparabilityError("full 档的每项 comparison 都必须 comparable")
        return Projection(
            level="full", reason=None,
            baseline_snapshot_ref=baseline_snapshot_ref,
            current_snapshot_ref=current_snapshot_ref,
            baseline_as_of=baseline_as_of, current_as_of=current_as_of,
            matched_cells=matched_cells, baseline_only_cells=0, current_only_cells=0,
            dimensions=dims, metric_comparisons=tuple(metric_comparisons),
        )

    # ---- level='none'(非 no_baseline):有基线但完全不可比 ----
    if matched_cells == 0:
        if metric_comparisons:
            raise ComparabilityError("none 档不得带 comparison/delta")
        return Projection(
            level="none", reason=_reason(code or "partial_overlap"),
            baseline_snapshot_ref=baseline_snapshot_ref,
            current_snapshot_ref=current_snapshot_ref,
            baseline_as_of=baseline_as_of, current_as_of=current_as_of,
            matched_cells=0, baseline_only_cells=baseline_only_cells,
            current_only_cells=current_only_cells,
            dimensions=dims, metric_comparisons=(),
        )

    # ---- level='partial' ----
    if not metric_comparisons:
        raise ComparabilityError("partial 档不得为空")
    partial_code = code or "partial_overlap"
    if partial_code in REASONS_FORBIDDEN_IN_PARTIAL:
        raise ComparabilityError(
            f"partial 档不得承载 reason {partial_code}(§15.6 Exclude 逐字)"
        )
    if code is None and baseline_only_cells == 0 and current_only_cells == 0:
        raise ComparabilityError("partial 档必须有真实 mismatch")
    return Projection(
        level="partial", reason=_reason(partial_code),
        baseline_snapshot_ref=baseline_snapshot_ref,
        current_snapshot_ref=current_snapshot_ref,
        baseline_as_of=baseline_as_of, current_as_of=current_as_of,
        matched_cells=matched_cells, baseline_only_cells=baseline_only_cells,
        current_only_cells=current_only_cells,
        dimensions=dims, metric_comparisons=tuple(metric_comparisons),
    )


# ════════════════════════════════════════════════════════════════════
# 3×3 section aggregate(单侧 / hybrid × 三档)
# ════════════════════════════════════════════════════════════════════

SectionState = Literal["ready", "partial_observation", "no_conclusion"]

#: 每档的下一步。**恒非空** —— Z-2.3 要求 no-baseline 分支不得是死路。
_SECTION_ACTIONS: dict[str, tuple[str, ...]] = {
    "ready": ("view_evidence",),
    "partial_observation": ("view_evidence", "build_comparable_retest_plan"),
    "no_conclusion": ("build_comparable_retest_plan",),
}

#: MET-43:``baseline_not_comparable`` 额外必须给 contact_provider。
_CONTACT_PROVIDER_REASONS: frozenset[str] = frozenset(REASON_CODES) - {"no_baseline"}


def section_state_for(projection: Projection) -> SectionState:
    """单侧 section state。``full`` 含 zero delta 也固定 ``ready``(MET-43 逐字)。"""
    if projection.level == "full":
        return "ready"
    if projection.level == "partial":
        return "partial_observation"
    return "no_conclusion"


def section_actions(projection: Projection) -> tuple[str, ...]:
    state = section_state_for(projection)
    actions = _SECTION_ACTIONS[state]
    if (
        projection.level == "none"
        and projection.reason is not None
        and projection.reason.code in _CONTACT_PROVIDER_REASONS
    ):
        # 「后者必须 contact-provider 并有同侧 gap/action/handoff」
        actions = actions + ("contact_provider",)
    if not actions:                              # pragma: no cover - 结构性保证
        raise AssertionError("comparability section 没有下一步 —— §0.5.6 禁死路")
    return actions


def aggregate_hybrid(sides: Mapping[str, Projection]) -> SectionState:
    """hybrid 双侧聚合。**取更差的那一侧** —— 一侧全绿不能掩盖另一侧无结论。

    MET-43「一侧全绿另一侧 all-error 时顶层 partial」:
    ready + no_conclusion 聚合为 partial_observation,而不是 ready。
    """
    if not sides:
        raise ComparabilityError("hybrid 聚合至少需要一侧")
    states = {section_state_for(p) for p in sides.values()}
    if states == {"ready"}:
        return "ready"
    if states == {"no_conclusion"}:
        return "no_conclusion"
    return "partial_observation"


def registry_census() -> dict[str, int]:
    return {
        "dimensions": len(DIMENSION_KEYS),
        "reason_codes": len(REASON_CODES),
        "precedence_entries": len(REASON_PRECEDENCE),
        "section_states": len(_SECTION_ACTIONS),
    }
