"""§7.3 场景覆盖 —— planned / measured / covered **三个**分母。

规格 §7.3 逐字:「不能用本次碰巧采到多少 distinct questions 作为分母,
也不能因一个平台一次提及就宣布题族已全面覆盖。」

这两句话对应两个**相反**方向的作弊,所以要两道门:

* 用实采题数当分母 ⇒ 平台挂了分母就变小 ⇒ 覆盖率反而涨。
  :func:`assert_planned_is_frozen` 拦这个:planned 只能来自冻结题单。
* 一个平台提一次就算覆盖 ⇒ 覆盖率虚高。
  :func:`covered_families` 按签发 policy 同时要求 count / rate / platform 三条,
  拦这个。

🔴 认知覆盖与推荐覆盖是**两个** definitionKey(MET-38 逐字「场景认知/推荐……
必须使用不同 definitionKey,不能共用一个模糊指标再由前端猜」)
------------------------------------------------------------------------
所以 :data:`COVERAGE_VARIANTS` 有两项,各自有自己的 positive outcome 集合。
推荐侧的 positive set **只能**由 ``recommended | conditionally_recommended``
组成 —— MET-38 逐字禁止「临时把 mentioned/candidate 塞入」,
:func:`assert_positive_set_signed` 把这条写成运行时门。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

from services.defensive_geo.monitoring import denominators as _den

COVERAGE_POLICY_VERSION = "defgeo-scenario-coverage-policy-v1"

#: 两个 variant = 两个 definitionKey。**不共用**。
COVERAGE_VARIANTS: tuple[str, ...] = ("awareness", "recommendation")

#: 各 variant 的**签发** positive outcome 集合。
#: 🔴 recommendation 侧逐字只有两项(MET-38)。把 mentioned_only / candidate_only
#:    塞进来是本规格点名的作弊形态 —— 判据有一发变异专打这里。
_SIGNED_POSITIVE_OUTCOMES: dict[str, frozenset[str]] = {
    "awareness": frozenset({
        "recommended", "conditionally_recommended", "candidate_only",
        "mentioned_only", "criteria_only",
    }),
    "recommendation": frozenset({"recommended", "conditionally_recommended"}),
}

#: 绝不允许进入 recommendation positive set 的 outcome。负向锁,判据直接遍历。
FORBIDDEN_IN_RECOMMENDATION_POSITIVE: frozenset[str] = frozenset({
    "mentioned_only", "candidate_only", "criteria_only",
    "not_mentioned", "misidentified",
    "refused_no_evidence", "refused_risk",
})


class CoveragePolicyError(ValueError):
    """policy 未签或被现场改动。**只能 not_measured**,不能出一个覆盖率。"""


class CoveragePolicy(NamedTuple):
    """§7.3 逐字要求「至少声明」的五项,全部必填 —— 缺一项就不是签发过的 policy。"""

    variant: str
    required_platforms: tuple[str, ...]
    family_valid_minimum: int      # 每个题族至少多少有效 cell 才可判断
    minimum_positive_count: int
    minimum_positive_rate: float
    positive_outcomes: frozenset[str]
    policy_version: str = COVERAGE_POLICY_VERSION


class CoverageResult(NamedTuple):
    variant: str
    planned_families: int
    measured_families: int
    covered_families: int
    status: str                    # 'measured' | 'not_measured'
    per_family: tuple[Mapping[str, Any], ...]


def assert_positive_set_signed(variant: str, positive: frozenset[str]) -> None:
    if variant not in COVERAGE_VARIANTS:
        raise CoveragePolicyError(f"未知覆盖 variant {variant!r}")
    signed = _SIGNED_POSITIVE_OUTCOMES[variant]
    if positive != signed:
        raise CoveragePolicyError(
            f"{variant} 的 positive outcome 集合与签发值不符。"
            f"签发={sorted(signed)} 收到={sorted(positive)}。"
            "改 outcome set 必须升 policy version(MET-38)。"
        )
    if variant == "recommendation":
        leaked = positive & FORBIDDEN_IN_RECOMMENDATION_POSITIVE
        if leaked:
            raise CoveragePolicyError(
                f"推荐覆盖的 positive set 混入了 {sorted(leaked)} —— "
                "「只是提到」和「列为候选」不是推荐。把它们算进推荐覆盖率,"
                "客户看到的推荐率就是虚的(MET-02 / MET-38)。"
            )


def signed_policy(variant: str, *, required_platforms: Sequence[str],
                  family_valid_minimum: int, minimum_positive_count: int,
                  minimum_positive_rate: float) -> CoveragePolicy:
    positive = _SIGNED_POSITIVE_OUTCOMES.get(variant)
    if positive is None:
        raise CoveragePolicyError(f"未知覆盖 variant {variant!r}")
    if family_valid_minimum < 1 or minimum_positive_count < 1:
        raise CoveragePolicyError("family_valid_minimum 与 minimum_positive_count 必须 >=1")
    if not (0.0 < minimum_positive_rate <= 1.0):
        raise CoveragePolicyError("minimum_positive_rate 必须落在 (0,1]")
    if not required_platforms:
        raise CoveragePolicyError(
            "required_platforms 为空 —— 「哪些平台属于本次 required set」是 §7.3 "
            "明令必须声明的一项,空集会让平台条件恒真"
        )
    assert_positive_set_signed(variant, positive)
    return CoveragePolicy(
        variant=variant,
        required_platforms=tuple(sorted(set(required_platforms))),
        family_valid_minimum=int(family_valid_minimum),
        minimum_positive_count=int(minimum_positive_count),
        minimum_positive_rate=float(minimum_positive_rate),
        positive_outcomes=positive,
    )


def assert_planned_is_frozen(planned_families: int, frozen_family_keys: Sequence[str]) -> None:
    """§7.3:planned 分母**只能**来自冻结题单,不能是本次实采到的题族数。

    这是「不得因平台失败或样本不足静默缩小计划分母」(§6.3)的执行点。
    """
    frozen = len(set(frozen_family_keys))
    if planned_families != frozen:
        raise CoveragePolicyError(
            f"plannedFamilies={planned_families} 与冻结题单的题族数 {frozen} 不等。"
            "计划分母只能来自冻结题单 —— 用实采数当分母时,平台挂了分母变小、"
            "覆盖率反而上涨(§7.3 / §6.3)。"
        )


def evaluate(
    *,
    policy: CoveragePolicy,
    frozen_family_keys: Sequence[str],
    per_family_cells: Mapping[str, Sequence[Mapping[str, Any]]],
) -> CoverageResult:
    """按签发 policy 算 planned / measured / covered。

    ``per_family_cells[family] = [{'platform':..., 'outcome':..., 'valid':bool}, ...]``
    —— 逐格,不是逐题:同一题族在多个平台各有格。

    🔴 三分母的语义**互不相同**,任何一个都不能拿另一个顶:
       planned = 应测(冻结题单)
       measured = 有效 cell 数达到 family_valid_minimum、**可以做判断**的题族
       covered = 在 measured 之上再满足 count/rate/platform 三条的题族
       所以恒有 ``0 <= covered <= measured <= planned``(MET-38 逐字)。
    """
    planned = len(set(frozen_family_keys))
    assert_planned_is_frozen(planned, frozen_family_keys)
    assert_positive_set_signed(policy.variant, policy.positive_outcomes)

    measured = 0
    covered = 0
    detail: list[Mapping[str, Any]] = []

    for family in sorted(set(frozen_family_keys)):
        cells = list(per_family_cells.get(family, ()))
        valid_cells = [c for c in cells if c.get("valid")]
        valid_count = len(valid_cells)
        is_measured = valid_count >= policy.family_valid_minimum
        positives = [c for c in valid_cells
                     if str(c.get("outcome")) in policy.positive_outcomes]
        positive_count = len(positives)
        positive_rate = (positive_count / valid_count) if valid_count else 0.0
        platforms_hit = {str(c.get("platform")) for c in positives}
        platform_ok = set(policy.required_platforms).issubset(platforms_hit)

        is_covered = bool(
            is_measured
            and positive_count >= policy.minimum_positive_count
            and positive_rate >= policy.minimum_positive_rate
            and platform_ok
        )
        if is_measured:
            measured += 1
        if is_covered:
            covered += 1
        detail.append({
            "familyKey": family,
            "validCells": valid_count,
            "positiveCells": positive_count,
            "positiveRate": positive_rate,
            "platformsWithPositive": sorted(platforms_hit),
            "isMeasured": is_measured,
            "isCovered": is_covered,
        })

    if not (0 <= covered <= measured <= planned):
        raise CoveragePolicyError(
            f"覆盖三分母不成立:covered={covered} measured={measured} planned={planned}"
        )

    # 一个 measured 都没有 ⇒ 不出覆盖率(§6.3「分母为 0 显示暂无结论,绝不显示 0%」)。
    status = "measured" if measured > 0 else "not_measured"
    return CoverageResult(
        variant=policy.variant, planned_families=planned,
        measured_families=measured, covered_families=covered,
        status=status, per_family=tuple(detail),
    )


def as_ratio(result: CoverageResult) -> _den.Ratio:
    """转成带单位、带分母的比值。单位 = question_family,与 cell 混算会被拒。"""
    return _den.cohort_ratio(
        numerator_key="covered_families",
        denominator_key="measured_families",
        numerator=result.covered_families,
        denominator=result.measured_families,
    )


def census() -> dict[str, Any]:
    return {
        "policyVersion": COVERAGE_POLICY_VERSION,
        "variants": list(COVERAGE_VARIANTS),
        "signedPositiveOutcomes": {
            k: sorted(v) for k, v in _SIGNED_POSITIVE_OUTCOMES.items()
        },
        "forbiddenInRecommendationPositive":
            sorted(FORBIDDEN_IN_RECOMMENDATION_POSITIVE),
    }
