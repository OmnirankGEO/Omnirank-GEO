"""§13.2 同口径 D0/D30 —— 把**真实 cell** 喂进窗B 的 comparability 引擎。

🔴 本模块**不重写** MET-43
--------------------------
窗B 已经交付 ``services/defensive_geo/presentation/comparability``:
三档 level x 七维 x 有限 precedence,连"致命维度不得降级成 partial"都在里面。
本模块的职责只有一件:**从 plan cell 事实算出它要的那些入参**,
然后调 :func:`presentation.comparability.project`。

多写一个平行实现的后果在本仓有先例:同一谓词写两处,必有一处没人验。

🔴 §13.2 的核心不是"比两份报告的元数据",是 **cell cohort 匹配**
----------------------------------------------------------------
逐字:「先按 ``question_key + question_revision + platform +
provider/model/revision + surface/search_mode + run_index`` 匹配 observation
cell,再对**相同 cell cohort** 比较。」

于是"某平台 D0 成功而 D30 失败"时,那一格**不进 matched cohort** ——
它既不算涨也不算跌,只进 ``baselineOnlyCells``。§13.2 逐字:
「不得用总体分母变化制造涨跌」。:func:`match_cohort` 就是这一条。

🔴 逐 metric 各自一枚 cohort commitment(§15.5 L2062 / MET-38 逐字)
--------------------------------------------------------------------
「不同 metric definition **不得共用**一枚 cohort commitment」。
原因很实在:``brand_mention_rate`` 与 ``scenario_coverage`` 的分母单位
一个是 plan cell 一个是 question family,共用一枚 hash 等于宣称两组
根本不同的东西"匹配范围相同"。:func:`metric_comparison` 因此逐项签发。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

from services.defensive_geo.presentation import comparability as _cmp
from services.defensive_geo.presentation import commitments as _commit

COMPARABILITY_FEED_VERSION = "defgeo-comparability-feed-v1"

#: §13.2 逐字的 matched cohort 匹配键。**顺序固定**,判据拿它当分母。
#: 注意 ``question_identity_key`` 就是规格里那个 ``question_key``
#: (§0.5.1 第 7 条去混淆后的名字)。
COHORT_MATCH_KEYS: tuple[str, ...] = (
    "question_identity_key",
    "question_revision",
    "public_platform",
    "actual_provider",
    "actual_model",
    "actual_model_revision",
    "planned_surface",
    "search_mode",
    "run_index",
)

#: §13.2「下列口径决定 cell 是否可进入 matched cohort」逐字七项 ——
#: 与窗B 的 ``DIMENSION_KEYS`` 一一对应。这里给的是**映射**,
#: 让「规格里的口径名」与「窗B 的维度名」不至于各说一套。
SCOPE_TO_DIMENSION: dict[str, str] = {
    "campaign_mode_and_side": "sameMode",
    "resolved_entity": "sameResolvedEntity",
    "question_set_revision_and_family_policy": "sameQuestionSetVersion",
    "platform_set": "samePlatformSet",
    "provider_model_and_revision": "sameModelRevisions",
    "surface_and_search_mode": "sameSurfaceAndSearchMode",
    "resolver_classifier_and_metric_definition_version":
        "sameResolverAndClassifierVersions",
}


class ComparabilityFeedError(ValueError):
    """入参算不出来。**不下发对比**,而不是拿"看起来像"的两个数画箭头。"""


#: [工单 E3-4 · P1-9b · Codex 二审] ``actual_model`` 的来源里,
#: **只有这一档**能拿去做同模型比较。
#:
#: ``planned_fallback``(供应商没回显、回落到计划值)进不了 cohort:
#: §13.2 的 ``sameModelRevisions`` 声称"两边跑的是同一个模型",而计划值
#: 只能证明"两边打算发同一个模型"。拿后者去说前者,就是把一个未经证实的
#: 相等当成证据 —— 客户看到的却是一句确定的"同口径对比"。
#: 宁可这一格不进分母(matched 变小、可比档位降级),也不许它以证据的身份进去。
MODEL_SOURCE_COMPARABLE = "provider_echo"


class CellIdentity(NamedTuple):
    """进入 cohort 匹配的一格。字段与 :data:`COHORT_MATCH_KEYS` 一一对应。

    ``model_source`` **不在** :data:`COHORT_MATCH_KEYS` 里 —— 它不是匹配键,
    是**准入**条件(见 :func:`comparable_cells`)。混进匹配键会让
    "两边都是 planned_fallback" 变成一次成功匹配,那正好是要挡的那件事。
    """

    question_identity_key: str
    question_revision: int
    public_platform: str
    actual_provider: str
    actual_model: str
    actual_model_revision: str | None
    planned_surface: str
    search_mode: str
    run_index: int
    #: 默认 ``planned_fallback``:**保守缺省**。没人显式说它是真回显时,
    #: 它就不是 —— 缺省成 comparable 会让所有还没接回显的调用方悄悄进 cohort。
    model_source: str = "planned_fallback"

    def match_key(self) -> tuple:
        return tuple(getattr(self, k) for k in COHORT_MATCH_KEYS)


def comparable_cells(cells: Sequence[CellIdentity]) -> tuple[CellIdentity, ...]:
    """滤掉不能用于同模型比较的格。"""
    return tuple(c for c in cells
                 if str(getattr(c, "model_source", "")) == MODEL_SOURCE_COMPARABLE)


class CohortMatch(NamedTuple):
    matched: tuple[tuple, ...]
    baseline_only: tuple[tuple, ...]
    current_only: tuple[tuple, ...]

    @property
    def matched_cells(self) -> int:
        return len(self.matched)

    @property
    def baseline_only_cells(self) -> int:
        return len(self.baseline_only)

    @property
    def current_only_cells(self) -> int:
        return len(self.current_only)


def match_cohort(
    baseline: Sequence[CellIdentity], current: Sequence[CellIdentity]
) -> CohortMatch:
    """§13.2 的 cell cohort 匹配。

    🔴 ``matched`` 按 **unique cell** 计(MET-43「aggregate matched 按 unique
       cells 重建」)。同一格出现两次(重复回调/镜像)不得让 matched 翻倍 ——
       MON-08「重复回调、重试、镜像不重复入分母」在这一层也要成立。
    """
    b = {c.match_key() for c in baseline}
    c = {x.match_key() for x in current}
    return CohortMatch(
        matched=tuple(sorted(b & c)),
        baseline_only=tuple(sorted(b - c)),
        current_only=tuple(sorted(c - b)),
    )


def dimensions_from_scope(scope: Mapping[str, bool]) -> dict[str, bool]:
    """把 §13.2 的七项口径翻成窗B 的七维。

    多一项少一项都抛 —— 漏一项时窗B 的 ``_validate_dimensions`` 会说"缺维度",
    但那时已经不知道是**哪一条口径**没算,排查要多绕一圈。
    """
    missing = [k for k in SCOPE_TO_DIMENSION if k not in scope]
    if missing:
        raise ComparabilityFeedError(
            f"缺 §13.2 口径 {missing}(七项决定 cell 能否进 matched cohort)")
    extra = [k for k in scope if k not in SCOPE_TO_DIMENSION]
    if extra:
        raise ComparabilityFeedError(f"出现未登记口径 {extra}")
    return {SCOPE_TO_DIMENSION[k]: bool(scope[k]) for k in SCOPE_TO_DIMENSION}


def metric_comparison(
    *,
    metric_definition_key: str,
    baseline_value: float | None,
    current_value: float | None,
    baseline_numerator: int,
    baseline_denominator: int,
    current_numerator: int,
    current_denominator: int,
    comparable: bool,
    cohort_commitment: str,
    baseline_scope_commitment: str,
    current_scope_commitment: str,
    baseline_slice_commitment: str,
    current_slice_commitment: str,
    matched_cell_count: int,
    reason_code: str | None = None,
) -> dict[str, Any]:
    """一项 metric 的对比。逐项自带 cohort commitment(MET-38/MET-43)。

    🔴 ``comparable=True`` 要求两侧 scope/slice commitment **相等** ——
       不等就说明两侧算的根本不是同一个定义/范围,这时给出 delta 是误导。
    🔴 ``comparable=False`` **保真两侧真实不同值**(MET-43 逐字),
       不许把它压成 0 或 null 来"看起来干净"。
    """
    if comparable:
        if baseline_scope_commitment != current_scope_commitment:
            raise ComparabilityFeedError(
                f"{metric_definition_key}: comparable 却两侧 scope commitment 不等 —— "
                "范围不同就不是同口径,画出来的涨跌是假的(MET-43)"
            )
        if baseline_slice_commitment != current_slice_commitment:
            raise ComparabilityFeedError(
                f"{metric_definition_key}: comparable 却两侧 slice commitment 不等 —— "
                "指标定义不同,两个数不可减(MET-43)"
            )
        if matched_cell_count < 1:
            raise ComparabilityFeedError(
                f"{metric_definition_key}: comparable 却 matched cell 为 0")
        if baseline_value is None or current_value is None:
            raise ComparabilityFeedError(
                f"{metric_definition_key}: comparable 两侧都必须有值")
        delta: float | None = float(current_value) - float(baseline_value)
    else:
        if reason_code is None:
            raise ComparabilityFeedError(
                f"{metric_definition_key}: non-comparable 必须带 reason —— "
                "不给原因的「不可比」在界面上是一句死话,销售没法向客户解释"
            )
        delta = None

    return {
        "metricDefinitionKey": metric_definition_key,
        "comparable": bool(comparable),
        "baselineValue": baseline_value,
        "currentValue": current_value,
        "delta": delta,
        "baselineNumerator": int(baseline_numerator),
        "baselineDenominator": int(baseline_denominator),
        "currentNumerator": int(current_numerator),
        "currentDenominator": int(current_denominator),
        "matchedCellCount": int(matched_cell_count),
        "cohortCommitment": cohort_commitment,
        "baselineScopeCommitment": baseline_scope_commitment,
        "currentScopeCommitment": current_scope_commitment,
        "baselineSliceCommitment": baseline_slice_commitment,
        "currentSliceCommitment": current_slice_commitment,
        "reasonCode": reason_code,
    }


def assert_no_cohort_reuse(comparisons: Sequence[Mapping[str, Any]]) -> None:
    """MET-38 逐字:「metric B **复用** metric A 的 cohort hash……拒绝」。

    两个不同 definitionKey 的 slice commitment 不同,所以 cohort commitment
    必然不同 —— 相同就说明某一处把 commitment 当常量写死了。
    """
    seen: dict[str, str] = {}
    for c in comparisons:
        key = str(c["metricDefinitionKey"])
        commitment = str(c["cohortCommitment"])
        if commitment in seen and seen[commitment] != key:
            raise ComparabilityFeedError(
                f"{key} 与 {seen[commitment]} 共用同一枚 cohort commitment "
                f"{commitment[:16]}… —— 不同 metric definition 不得共用"
                "(§15.5 L2062 / MET-38)"
            )
        seen[commitment] = key


def project(
    *,
    baseline_snapshot_ref: str | None,
    current_snapshot_ref: str,
    baseline_as_of: str | None,
    current_as_of: str,
    scope: Mapping[str, bool] | None,
    baseline_cells: Sequence[CellIdentity],
    current_cells: Sequence[CellIdentity],
    metric_comparisons: Sequence[Mapping[str, Any]] = (),
) -> _cmp.Projection:
    """算好入参,交给窗B 的引擎定档。**本函数不自己决定 level**。"""
    if baseline_snapshot_ref is None:
        # no_baseline 档:窗B 要求 dimensions/asOf 都是 null、零 comparison。
        return _cmp.project(
            baseline_snapshot_ref=None, current_snapshot_ref=current_snapshot_ref,
            baseline_as_of=None, current_as_of=current_as_of, dimensions=None,
            matched_cells=0,
            baseline_only_cells=len({c.match_key() for c in baseline_cells}),
            current_only_cells=len({c.match_key() for c in current_cells}),
            metric_comparisons=(),
        )

    if scope is None:
        raise ComparabilityFeedError("有基线时必须给出 §13.2 七项口径")
    dims = dimensions_from_scope(scope)
    # [工单 E3-4 · P1-9b] 计划值(``planned_fallback``)不进 matched cohort ——
    # 它证明不了"两边跑的是同一个模型",而 sameModelRevisions 正是这么说的。
    # 被滤掉的格仍然算在 baseline_only / current_only 里(它们**存在**,
    # 只是不可比),所以分母不会凭空缩小、可比档位会如实降级。
    cohort = match_cohort(comparable_cells(baseline_cells),
                          comparable_cells(current_cells))
    assert_no_cohort_reuse(metric_comparisons)

    # 🔴 ``*_only`` 必须按**全集**减 matched 算,不能只看过滤后的那部分。
    #    只看过滤后的话,被滤掉的格会从分母里整个消失 —— 显示成"两边加起来
    #    就这么几格,而且全都对上了",可比档位反而**被抬高**。
    #    那与本次修改的方向正好相反:这些格是存在的,只是不可比。
    matched = set(cohort.matched)
    baseline_only = {c.match_key() for c in baseline_cells} - matched
    current_only = {c.match_key() for c in current_cells} - matched

    return _cmp.project(
        baseline_snapshot_ref=baseline_snapshot_ref,
        current_snapshot_ref=current_snapshot_ref,
        baseline_as_of=baseline_as_of, current_as_of=current_as_of,
        dimensions=dims,
        matched_cells=cohort.matched_cells,
        baseline_only_cells=len(baseline_only),
        current_only_cells=len(current_only),
        metric_comparisons=metric_comparisons,
    )


def new_platform_forces_partial(
    baseline_cells: Sequence[CellIdentity], current_cells: Sequence[CellIdentity]
) -> bool:
    """§13.2 尾句:「**新增平台只能形成 partial comparison**」。

    调用方拿它去把 ``samePlatformSet`` 置 False —— 新增平台时七维里那一维
    就不能报 True,于是窗B 必然给出 partial 而不是 full。
    """
    b = {c.public_platform for c in baseline_cells}
    c = {x.public_platform for x in current_cells}
    return bool(c - b)


def census() -> dict[str, Any]:
    return {
        "feedVersion": COMPARABILITY_FEED_VERSION,
        "cohortMatchKeys": list(COHORT_MATCH_KEYS),
        "scopeToDimension": dict(SCOPE_TO_DIMENSION),
        "engineDimensionKeys": list(_cmp.DIMENSION_KEYS),
        "engineReasonCodes": list(_cmp.REASON_CODES),
        "commitmentKinds": list(_commit.COMMITMENT_KINDS),
    }
