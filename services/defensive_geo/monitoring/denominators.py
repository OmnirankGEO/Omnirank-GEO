"""§6.3 版本化 denominator registry。

规格 §6.3 逐字:「分母定义必须由服务端 ``denominator_registry_version`` 统一签发。
每项同时**冻结计量单位**,禁止把 cell、题族、事实字段和陈述混算。」

🔴 为什么"计量单位"是承重的,不是装饰
------------------------------------
表里四种单位:``plan cell`` / ``attempt`` / ``question family`` /
``canonical fact field`` / ``answer claim``。把"归因覆盖率"的分母
(单位 = answer claim)错配成"品牌提及率"的分母(单位 = plan cell)时,
两个数都还是"整数比整数",肉眼、类型检查、甚至 ``分子<=分母`` 断言
**全都不会红**。唯一能抓住的是逐项冻结单位并在算之前核对。
:func:`assert_same_unit` 就是那道门。

🔴 ``attempt_records`` 的特例(§6.3 逐字「不直接作为品牌率分母」)
--------------------------------------------------------------
它的单位是 attempt 而不是 plan cell,一个 plan cell 可以有 N 个 attempt。
拿它当品牌率分母 = 重试越多分母越大 = 品牌率被稀释。所以它在
:data:`FORBIDDEN_AS_BRAND_RATE_DENOMINATOR` 里,:func:`assert_usable_as_rate`
拒绝。
"""

from __future__ import annotations

from typing import Any, NamedTuple

DENOMINATOR_REGISTRY_VERSION = "denominator_registry_v1"

#: 五种计量单位。混算 = 判据必须转红。
Unit = str
UNIT_PLAN_CELL: Unit = "plan_cell"
UNIT_ATTEMPT: Unit = "attempt"
UNIT_QUESTION_FAMILY: Unit = "question_family"
UNIT_CANONICAL_FACT_FIELD: Unit = "canonical_fact_field"
UNIT_ANSWER_CLAIM: Unit = "answer_claim"

UNITS: tuple[Unit, ...] = (
    UNIT_PLAN_CELL,
    UNIT_ATTEMPT,
    UNIT_QUESTION_FAMILY,
    UNIT_CANONICAL_FACT_FIELD,
    UNIT_ANSWER_CLAIM,
)


class DenominatorDefinition(NamedTuple):
    key: str
    unit: Unit
    definition: str


def _d(key: str, unit: Unit, definition: str) -> DenominatorDefinition:
    return DenominatorDefinition(key=key, unit=unit, definition=definition)


#: 🔴 §6.3 表格逐行。**判据拿 ``REGISTRY`` 当分母机械遍历,不手抄 key 列表**
#:    (G-2:「本文内的手写枚举只是示例,不是分母」)。
_REGISTRY: dict[str, DenominatorDefinition] = {
    d.key: d for d in (
        _d("planned_cells", UNIT_PLAN_CELL,
           "冻结题单 x 本次计划 public platform/surface x 轮次的全部格"),
        _d("attempt_records", UNIT_ATTEMPT,
           "对 plan cell 的全部真实调用、fallback 与 retry;不直接作为品牌率分母"),
        _d("attempted_cells", UNIT_PLAN_CELL,
           "至少生成一条耐久 attempt 的 distinct plan cell,失败也保留"),
        _d("response_cells", UNIT_PLAN_CELL,
           "canonical selection policy 选出的 attempt 成功返回且原文可解析"),
        _d("valid_samples", UNIT_PLAN_CELL,
           "canonical projection 完成实体判断并落入业务 outcome;"
           "不含最终 engine error 和 entity ambiguous"),
        _d("mention_eligible_samples", UNIT_PLAN_CELL,
           "brand_exposure=unnamed 且属于 valid_samples"),
        _d("identity_evaluable_samples", UNIT_PLAN_CELL,
           "身份/事实核验题中 resolver 已完成判断;ambiguous 单独展示"),
        _d("recommendation_eligible_samples", UNIT_PLAN_CELL,
           "冻结为选择、比较或推荐意图且属于 valid_samples"),
        _d("comparison_eligible_samples", UNIT_PLAN_CELL,
           "brand_exposure=comparison、实体均已消歧且属于 valid_samples"),
        _d("position_eligible_samples", UNIT_PLAN_CELL,
           "回答明确给出有序候选列表,且可稳定解析 rank"),
        _d("source_observable_samples", UNIT_PLAN_CELL,
           "该 platform/surface/collector 合同能够观察 source 字段的有效回答;"
           "字段为空仍属于可观察样本,只有平台/接口本身不暴露来源时才为 not-applicable"),
        _d("target_present_samples", UNIT_PLAN_CELL,
           "正确消歧且目标品牌确实出现的有效回答"),
        _d("planned_families", UNIT_QUESTION_FAMILY, "accepted plan 中应测题族"),
        _d("measured_families", UNIT_QUESTION_FAMILY,
           "达到冻结 minimum-valid policy、可做覆盖判断的题族"),
        _d("covered_families", UNIT_QUESTION_FAMILY,
           "达到该题族冻结 coverage policy 的题族"),
        _d("comparable_fact_fields", UNIT_CANONICAL_FACT_FIELD,
           "同一事实字段存在至少两个独立、可比较来源"),
        _d("source_attribution_eligible_claims", UNIT_ANSWER_CLAIM,
           "source_observable_samples 中可稳定切分、可判断是否存在来源映射的"
           "全部回答陈述;没有映射的陈述也必须进入分母"),
        _d("explicit_mapped_claims", UNIT_ANSWER_CLAIM,
           "平台显式返回了来源映射的陈述;只作为「映射后 proof 有效率」的分母,"
           "不能替代来源归因覆盖率分母"),
        _d("answer_claims", UNIT_ANSWER_CLAIM,
           "valid_samples 经 versioned claim splitter 稳定切分的全部客户可见回答陈述"),
    )
}

REGISTRY_KEYS: tuple[str, ...] = tuple(_REGISTRY)

#: §6.3 逐字「不直接作为品牌率分母」。
FORBIDDEN_AS_BRAND_RATE_DENOMINATOR: frozenset[str] = frozenset({"attempt_records"})


class DenominatorError(ValueError):
    """分母用错了。**不出数**,而不是出一个单位错配的百分比。"""


def definition(key: str) -> DenominatorDefinition:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise DenominatorError(
            f"未登记的 denominator {key!r}(registry={DENOMINATOR_REGISTRY_VERSION})。"
            "新分母必须先进 registry 并升 version —— 现场造一个等于没人验它的单位。"
        ) from None


def unit_of(key: str) -> Unit:
    return definition(key).unit


def assert_same_unit(numerator_key: str, denominator_key: str) -> None:
    """§6.3「禁止把 cell、题族、事实字段和陈述混算」的可执行形式。

    分子与分母的计量单位必须**相同**。这是唯一能抓住"归因 claim 数
    除以 plan cell 数"这类错配的门 —— 两边都是整数,别的检查全绿。
    """
    nu, du = unit_of(numerator_key), unit_of(denominator_key)
    if nu != du:
        raise DenominatorError(
            f"计量单位混算:分子 {numerator_key}({nu}) / 分母 {denominator_key}({du})。"
            "§6.3 禁止把 cell、题族、事实字段和陈述混算。"
        )


def assert_usable_as_rate(denominator_key: str) -> None:
    if denominator_key in FORBIDDEN_AS_BRAND_RATE_DENOMINATOR:
        raise DenominatorError(
            f"{denominator_key} 的单位是 attempt,一个 plan cell 可有多个 attempt。"
            "拿它当品牌率分母 = 重试越多品牌率越低(§6.3 逐字「不直接作为品牌率分母」)。"
        )


class Ratio(NamedTuple):
    """一个**带分母、带单位、带取数出处**的比值。零分母不是 0%。"""

    numerator_key: str
    denominator_key: str
    unit: Unit
    numerator: int
    denominator: int
    value: float | None
    status: str  # 'measured' | 'no_denominator'


def ratio(
    *,
    numerator_label: str,
    denominator_key: str,
    numerator: int,
    denominator: int,
) -> Ratio:
    """§6.3 三条守恒 + 「分母为 0 显示暂无结论/不适用,**绝不能显示 0%**」。

    🔴 ``numerator_label`` 是**自由标签**不是 registry key,这是刻意的:
       分子多数时候是分母 cohort 的一个**子集**(「这些格里被提及的那些」),
       子集当然与全集同单位,给它硬造一个 registry key 反而会让 registry
       里塞满不是分母的东西,分母普查就不再是分母普查了。
       两个 cohort 相除时用 :func:`cohort_ratio` —— 那种才需要核对单位。
    """
    assert_usable_as_rate(denominator_key)
    if not numerator_label:
        raise DenominatorError(
            "numerator_label 不能为空 —— 一个没有出处的分子在报告上无法解释"
        )
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        raise DenominatorError("分子分母必须是整数(MET-27「finite 非负整数」)")
    if numerator < 0 or denominator < 0:
        raise DenominatorError(
            f"分子/分母不得为负(收到 {numerator}/{denominator})")
    if numerator > denominator:
        raise DenominatorError(
            f"分子 {numerator} > 分母 {denominator} —— §6.3「分子 <= 对应分母」。"
        )
    if denominator == 0:
        # 🔴 这一格是 MET-13 / §6.3 的承重点:返回 None + no_denominator,
        #    **不返回 0.0**。返回 0.0 会在报告上写成 0%,而真相是「没测到」。
        return Ratio(numerator_label, denominator_key, unit_of(denominator_key),
                     0, 0, None, "no_denominator")
    return Ratio(numerator_label, denominator_key, unit_of(denominator_key),
                 numerator, denominator, numerator / denominator, "measured")


def cohort_ratio(
    *,
    numerator_key: str,
    denominator_key: str,
    numerator: int,
    denominator: int,
) -> Ratio:
    """两个 **registry cohort** 相除(如 covered_families / measured_families)。

    这一种才需要 :func:`assert_same_unit` —— 它是唯一会发生
    「answer_claim 数除以 plan_cell 数」那类错配的地方:两边都是整数,
    ``分子<=分母`` 也可能碰巧成立,只有单位对不上能抓住。
    """
    assert_same_unit(numerator_key, denominator_key)
    return ratio(numerator_label=numerator_key, denominator_key=denominator_key,
                 numerator=numerator, denominator=denominator)


def assert_attempted_conservation(
    *,
    attempted_cells: int,
    canonical_outcome_cells: int,
    ambiguous_cells: int,
    final_engine_error_cells: int,
) -> None:
    """§6.3 逐字:「每个 attempted plan cell 的 canonical outcome + ambiguous +
    final engine error 守恒」。

    MON-01/MON-02/MET-13 全部落在这一条上:失败格没被删、401/429/超时没被
    压成"未提及"、ambiguous 没被吞 —— 三件事任何一件发生,这个等式立刻不成立。
    """
    total = canonical_outcome_cells + ambiguous_cells + final_engine_error_cells
    if total != attempted_cells:
        raise DenominatorError(
            f"attempted 守恒破裂:attempted={attempted_cells} != "
            f"outcome({canonical_outcome_cells}) + ambiguous({ambiguous_cells}) + "
            f"engine_error({final_engine_error_cells}) = {total}。"
            "常见真因:失败格被删、401/429/超时被映射成未提及、"
            "或 ambiguous 被吞成 nonmention(§6.3 / MON-01 / MON-02)。"
        )


def census() -> dict[str, Any]:
    """G-2:机械导出,判据不手抄。"""
    return {
        "registryVersion": DENOMINATOR_REGISTRY_VERSION,
        "units": list(UNITS),
        "keys": list(REGISTRY_KEYS),
        "byKey": {
            k: {"unit": d.unit, "definition": d.definition}
            for k, d in _REGISTRY.items()
        },
        "forbiddenAsBrandRateDenominator": sorted(FORBIDDEN_AS_BRAND_RATE_DENOMINATOR),
    }
