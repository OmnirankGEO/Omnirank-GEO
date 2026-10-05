"""``metric_definition_registry_v1`` + 互斥 exclusion precedence —— MET-38 / MET-27。

MET-38 逐字要求两件事:

1. registry「key 集合与逐 key metric/unit/denominator/value/bucket/numerator/
   rounding/minimum/coverage/exclusion policy **exact**」;
2. 「每个 candidate **恰**落 denominator 或一个按签发 precedence 选出的
   **互斥** primary exclusion,``exclusionCandidateCount = denominator + distinct
   exclusions``,少/多解释、重复 code、双算或输入换序漂移拒绝」。

🔴 第 2 条的关键词是「**恰**」和「**互斥**」
--------------------------------------------
一个 candidate 可能同时满足多条排除规则(既"平台报错"又"未采集")。
若允许它落进两个 exclusion,``denominator + distinct exclusions`` 就会
**大于** candidate 总数 —— 这就是 MET-38 说的"双算"。
所以排除规则必须有**全序 precedence**,命中多条时只记 precedence 最高的那一条
(primary exclusion)。precedence 写成数据并带 ``exclusion_precedence_version``,
改顺序必须升版本 —— 否则同一批数据在两次发布之间会给出不同分母而无人察觉。

🔴 「输入换序漂移拒绝」
----------------------
:func:`classify_candidates` 的结果**不依赖输入顺序**:它对每个 candidate 独立
按 precedence 求最小值。判据里用打乱顺序的同一批输入验证结果逐值相等。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, NamedTuple, Sequence

REGISTRY_VERSION = "metric_definition_registry_v1"
DENOMINATOR_REGISTRY_VERSION = "denominator_registry_v1"
EXCLUSION_PRECEDENCE_VERSION = "metric_exclusion_precedence_v1"


class MetricDefinition(NamedTuple):
    """一行 registry。字段集合即 MET-38 点名的那十项。"""

    metric_definition_key: str
    metric: str
    unit: str                       # ratio | count | distribution | evidence_level
    denominator: str
    numerator: str
    value_shape: str                # ratio | count | distribution | level
    bucket_policy: str
    rounding: str
    minimum_sample_policy: str
    coverage_policy: str
    exclusion_policy: str
    definition_version: str
    calculation_version: str

    def as_row(self) -> dict[str, Any]:
        """给 slice commitment 用的 **exact row**。字段顺序无关(JCS 会排序),
        但**内容必须逐字**来自本 registry —— 就地拼一个近似 row 会算出
        一个"看起来对"的 commitment。"""
        return dict(self._asdict())


def _d(key: str, metric: str, unit: str, denominator: str, numerator: str,
       value_shape: str, bucket: str, rounding: str, minimum: str,
       coverage: str, exclusion: str) -> MetricDefinition:
    return MetricDefinition(
        metric_definition_key=key, metric=metric, unit=unit,
        denominator=denominator, numerator=numerator, value_shape=value_shape,
        bucket_policy=bucket, rounding=rounding, minimum_sample_policy=minimum,
        coverage_policy=coverage, exclusion_policy=exclusion,
        definition_version="defensive-metric-def-v1",
        calculation_version="defensive-metric-calc-v1",
    )


#: 🔴 registry 全集。判据把它当分母**机械遍历**,不手抄 key 列表。
_REGISTRY: dict[str, MetricDefinition] = {
    d.metric_definition_key: d for d in (
        _d("brand_mention_rate", "品牌提及率", "ratio",
           "valid_answers", "answers_mentioning_brand", "ratio",
           "none", "half_up_1dp", "min_valid_5", "per_scope", "standard_v1"),
        # 🔴 [#233] 显示名收回到判定真正测到的东西(见 copy_registry 抬头)。
        #    契约键 explicit_recommendation_rate **不改** —— 改键是另一回事。
        _d("explicit_recommendation_rate", "提及率(正面措辞)", "ratio",
           "valid_answers", "answers_explicitly_recommending", "ratio",
           "none", "half_up_1dp", "min_valid_5", "per_scope", "standard_v1"),
        _d("conditional_recommendation_rate", "提及率(带条件措辞)", "ratio",
           "valid_answers", "answers_conditionally_recommending", "ratio",
           "none", "half_up_1dp", "min_valid_5", "per_scope", "standard_v1"),
        _d("scenario_coverage", "场景覆盖率", "ratio",
           "planned_question_families", "families_with_valid_answer", "ratio",
           "none", "half_up_1dp", "min_valid_3", "per_side", "standard_v1"),
        _d("answer_gap_count", "还答不上来的问题数", "count",
           "planned_question_families", "families_without_valid_answer", "count",
           "none", "integer", "none", "per_side", "standard_v1"),
        _d("competitor_cooccurrence", "竞品共现分布", "distribution",
           "valid_answers", "answers_naming_each_competitor", "distribution",
           "multi_label_v1", "integer", "min_valid_5", "per_scope", "standard_v1"),
        _d("source_consistency", "信源一致性", "ratio",
           "source_attribution_eligible_claims", "claims_with_consistent_sources",
           "ratio", "none", "half_up_1dp", "min_valid_3", "per_scope", "standard_v1"),
        _d("explicit_source_coverage", "显式来源覆盖率", "ratio",
           "source_attribution_eligible_claims", "claims_with_explicit_mapping",
           "ratio", "none", "half_up_1dp", "min_valid_3", "per_scope", "standard_v1"),
        _d("attribution_proof_rate", "归因证据有效率", "ratio",
           "claims_with_explicit_mapping", "claims_with_url_and_body_proof",
           "ratio", "none", "half_up_1dp", "min_valid_1", "per_scope", "standard_v1"),
        _d("entity_identification", "实体识别状态", "evidence_level",
           "valid_answers", "answers_with_confirmed_entity", "level",
           "entity_state_v1", "integer", "min_valid_3", "per_scope", "standard_v1"),
    )
}

METRIC_KEYS: tuple[str, ...] = tuple(_REGISTRY)

#: MET-38 点名必须 exact 的十个字段。判据拿它遍历,不手抄。
REQUIRED_DEFINITION_FIELDS: tuple[str, ...] = (
    "metric", "unit", "denominator", "numerator", "value_shape",
    "bucket_policy", "rounding", "minimum_sample_policy",
    "coverage_policy", "exclusion_policy",
)


def definition(key: str) -> MetricDefinition:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise ValueError(
            f"未知 metricDefinitionKey {key!r};合法 = {list(METRIC_KEYS)}。"
            "MET-38「未知/缺 definition……拒绝」——不设兜底。"
        ) from None


def registry_rows() -> Mapping[str, MetricDefinition]:
    return dict(_REGISTRY)


# ════════════════════════════════════════════════════════════════════
# 互斥 exclusion precedence
# ════════════════════════════════════════════════════════════════════

class ExclusionRule(NamedTuple):
    code: str
    precedence: int          # 越小越优先
    public_reason_key: str


#: 🔴 全序 precedence。数字**互不相同**(判据会验),否则"互斥"无从谈起。
_EXCLUSIONS: tuple[ExclusionRule, ...] = (
    ExclusionRule("engine_error", 10, "excluded_engine_error"),
    ExclusionRule("entity_ambiguous", 20, "excluded_entity_ambiguous"),
    ExclusionRule("policy_skipped", 30, "excluded_policy_skipped"),
    ExclusionRule("not_attempted", 40, "excluded_not_attempted"),
    ExclusionRule("out_of_scope", 50, "excluded_out_of_scope"),
)

EXCLUSION_CODES: tuple[str, ...] = tuple(r.code for r in _EXCLUSIONS)
_BY_CODE: dict[str, ExclusionRule] = {r.code: r for r in _EXCLUSIONS}


class Classification(NamedTuple):
    denominator: int
    #: code → count。只记 **primary** exclusion,一个 candidate 只出现一次。
    exclusions: dict[str, int]
    exclusion_candidate_count: int

    def conserves(self) -> bool:
        """MET-38 的守恒式:``exclusionCandidateCount = denominator + Σ exclusions``。"""
        return self.exclusion_candidate_count == self.denominator + sum(
            self.exclusions.values()
        )


class ClassificationError(ValueError):
    pass


def classify_candidates(
    candidates: Sequence[Mapping[str, Any]]
) -> Classification:
    """把 candidate 逐个分到 denominator 或**唯一一个** primary exclusion。

    每个 candidate 形如 ``{"id": "...", "exclusion_codes": [...]}``。
    命中多条排除规则时,只记 precedence 最小(最优先)的那一条 ——
    这就是"互斥 primary exclusion",也是不双算的全部原因。

    结果与输入顺序无关(MET-38「输入换序漂移拒绝」)。
    """
    seen_ids: set[str] = set()
    denominator = 0
    exclusions: dict[str, int] = {}

    for candidate in candidates:
        cid = candidate.get("id")
        if not isinstance(cid, str) or not cid:
            raise ClassificationError("candidate 缺少稳定 id,无法验重")
        if cid in seen_ids:
            raise ClassificationError(
                f"candidate id 重复:{cid!r} —— 重复即双算,分母不可复算"
            )
        seen_ids.add(cid)

        codes = candidate.get("exclusion_codes") or []
        if not isinstance(codes, (list, tuple)):
            raise ClassificationError(f"{cid}: exclusion_codes 必须是列表")
        unknown = [c for c in codes if c not in _BY_CODE]
        if unknown:
            raise ClassificationError(
                f"{cid}: 未知 exclusion code {unknown!r};"
                f"合法 = {list(EXCLUSION_CODES)}(MET-38「重复 code……拒绝」)"
            )
        if len(set(codes)) != len(codes):
            raise ClassificationError(f"{cid}: exclusion code 自身重复")

        if not codes:
            denominator += 1
            continue
        primary = min((_BY_CODE[c] for c in codes), key=lambda r: r.precedence)
        exclusions[primary.code] = exclusions.get(primary.code, 0) + 1

    result = Classification(
        denominator=denominator,
        exclusions=exclusions,
        exclusion_candidate_count=len(seen_ids),
    )
    if not result.conserves():                   # pragma: no cover - 结构性保证
        raise ClassificationError(
            f"守恒被破:candidates={result.exclusion_candidate_count} "
            f"denominator={result.denominator} exclusions={result.exclusions}"
        )
    return result


def registry_census() -> dict[str, Any]:
    """POR-20 用的机械分母。"""
    return {
        "registry_version": REGISTRY_VERSION,
        "metric_keys": len(_REGISTRY),
        "exclusion_codes": len(_EXCLUSIONS),
        "precedence_version": EXCLUSION_PRECEDENCE_VERSION,
    }
