"""§7.4 实体消歧与信源一致性 —— canonical facts 第一期。

🔴 R-3(§0.5.2)先说清边界,免得这个模块被当成写作侧的闸
--------------------------------------------------------
§7.4 那句「核心卖点和能力边界(**仅在有证据时**)」的证据门,
**只作用于信源一致性这条测量轴**。裁定原文:「不得外溢到写作/交付链 ——
客户资料 = SSOT,写作侧使用客户卖点不设证据前置」。

所以本模块的一切"有没有证据"判断只回答一个问题:
**这条事实在公开信源上能不能被独立核对**。
它不回答"这条事实能不能写进稿子" —— 后者的答案永远是"能"(客户资料是 SSOT)。
:func:`assert_not_used_as_writing_gate` 把这条写成显式的调用方契约。

🔴 misidentified 是**第三种**东西,不是未提及也不是引擎失败(§7.4 / MET-26)
----------------------------------------------------------------------------
「回答里出现了相同或相近字符串,却把它解释为另一家公司/地区主体/产品」。
它十态 outcome 可以保持 ``not_mentioned``,但:

* **绝不进入**正确提及/推荐分子;
* 报告必须**单独展示**「AI 认错了谁」+ 证据 + 校准动作;
* **不能**降成普通 nonmention,也不能升成 engine_error。

把它压成 nonmention 的后果:客户看到"AI 没提到你",于是买更多曝光;
真相是 AI 提到了但认成了同名的另一家 —— 该做的是校准身份,不是加投放。

🔴 「缺失不等于冲突」(§7.4 逐字 / MET-10)
------------------------------------------
查不到 = ``unknown``,不是 ``conflict``。把 unknown 算成冲突,报告会写成
"你们的地址和公开资料对不上",而事实是公开资料里根本没有地址。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

CANONICAL_FACTS_VERSION = "defgeo-canonical-facts-v1"
ENTITY_RESOLVER_VERSION = "defgeo-entity-resolver-v1"

#: §7.4 第一期 canonical facts 逐字九项。判据拿它当分母机械遍历。
CANONICAL_FACT_FIELDS: tuple[str, ...] = (
    "legal_entity_name",
    "brand_name_and_aliases",
    "primary_category",
    "service_regions",
    "official_site",
    "address",
    "phone",
    "founded_at_and_credentials",
    "core_selling_points_and_boundaries",
)

#: §7.4 唯一带证据门的那一项(R-3:门只在这条**测量**轴上)。
EVIDENCE_GATED_FACT_FIELDS: frozenset[str] = frozenset({
    "core_selling_points_and_boundaries",
})

#: §7.4 实体判定五分。**misidentified 与 ambiguous 各自独立**。
ENTITY_STATES: tuple[str, ...] = (
    "confirmed", "nonmention", "misidentified", "ambiguous", "not_evaluated",
)

#: MET-26 / MET-07 的负向锁:这两个状态**不许**被折叠进 nonmention/engine_error。
MUST_NOT_COLLAPSE_INTO_NONMENTION: frozenset[str] = frozenset({
    "misidentified", "ambiguous",
})

#: 每条事实必须随身携带的溯源字段(§7.4 逐字「保存 source URL、抓取快照、
#: as-of、normalizer 和 extractor version」)。缺一项 = 这条事实无法复核。
REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "source_url", "captured_snapshot_ref", "as_of",
    "normalizer_version", "extractor_version",
)

#: 一致性判定三态。**缺失单独一态**,不并进冲突(§7.4「缺失不等于冲突」)。
CONSISTENCY_STATES: tuple[str, ...] = ("consistent", "conflicting", "unknown")


class SourceConsistencyError(ValueError):
    """事实/信源形态不合法。**不判**,而不是判一个"看起来像冲突"的结论。"""


class FactObservation(NamedTuple):
    """一条来自某个公开信源的事实观察。"""

    fact_field: str
    value: str
    source_url: str
    captured_snapshot_ref: str
    as_of: str
    normalizer_version: str
    extractor_version: str
    #: §7.4 逐字「同一软文转载不算多个独立来源」。转载指纹相同的两条只算一条。
    syndication_fingerprint: str


def assert_not_used_as_writing_gate(call_site: str) -> None:
    """R-3 的显式契约:本模块**不得**被写作/交付链当作证据前置。

    调用方在写作链里 import 本模块时必须先经过这个函数并给出自己的坐标;
    坐标落在写作链里就直接抛。这比在文档里写"请勿用于写作侧"硬 ——
    后者拦不住任何人。
    """
    writing_chain_markers = (
        "writing/", "writing_", "article_", "geo_writer", "ranking_prompt",
    )
    lowered = call_site.replace("\\", "/").lower()
    for marker in writing_chain_markers:
        if marker in lowered:
            raise SourceConsistencyError(
                f"{call_site} 属于写作/交付链,不得把信源一致性当作写作证据前置。"
                "客户资料 = SSOT,写作侧使用客户卖点**不设**证据前置"
                "(§0.5.2 R-3 逐字:证据门仅限信源一致性测量轴)。"
            )


def assert_provenance_complete(obs: FactObservation) -> None:
    """§7.4:五项溯源缺一不可。

    缺 ``as_of`` 的事实无法判"过时";缺 ``extractor_version`` 的事实在抽取器
    升级后无法复现 —— 两者都会让日后的复核变成猜。
    """
    missing = [
        f for f in REQUIRED_PROVENANCE_FIELDS if not getattr(obs, f, None)
    ]
    if missing:
        raise SourceConsistencyError(
            f"{obs.fact_field} 的观察缺溯源字段 {missing}(§7.4 逐字五项)"
        )
    if obs.fact_field not in CANONICAL_FACT_FIELDS:
        raise SourceConsistencyError(
            f"未登记的 canonical fact 字段 {obs.fact_field!r};"
            f"第一期九项 = {list(CANONICAL_FACT_FIELDS)}"
        )


def independent_sources(observations: Sequence[FactObservation]) -> list[FactObservation]:
    """§7.4 / MET-16:「同一软文转载**不算**多个独立来源」。

    按 ``syndication_fingerprint`` 归并 —— 同一原稿转载到十个站点仍是一条。
    不做这一步的后果是"两个独立来源可比"这个条件被转载量刷出来,
    ``comparable_fact_fields`` 分母虚高。
    """
    seen: set[str] = set()
    out: list[FactObservation] = []
    for obs in observations:
        assert_provenance_complete(obs)
        if obs.syndication_fingerprint in seen:
            continue
        seen.add(obs.syndication_fingerprint)
        out.append(obs)
    return out


def consistency_for_field(
    *,
    fact_field: str,
    accepted_value: str | None,
    observations: Sequence[FactObservation],
) -> dict[str, Any]:
    """一条事实字段的一致性判定。

    ``accepted_value`` = 客户确认过的值(``accepted_brand_fact``)。
    🔴 R-4:客户确认过的事实在 ``hasConflict=true`` 时**仍然可用**,
       冲突信息随行下发 —— 本函数因此永远把 ``accepted_value`` 原样带出,
       从不因为发现冲突就把它抹掉。
    """
    if fact_field not in CANONICAL_FACT_FIELDS:
        raise SourceConsistencyError(f"未登记的 canonical fact 字段 {fact_field!r}")

    indep = independent_sources(observations)
    values = {o.value for o in indep}

    if not indep:
        # 🔴 缺失 != 冲突(§7.4 / MET-10「unknown 不等于错误」)。
        state = "unknown"
        conflicting: list[Mapping[str, Any]] = []
    elif accepted_value is not None and values - {accepted_value}:
        state = "conflicting"
        conflicting = [
            {"value": o.value, "sourceUrl": o.source_url, "asOf": o.as_of}
            for o in indep if o.value != accepted_value
        ]
    elif accepted_value is None and len(values) > 1:
        state = "conflicting"
        conflicting = [
            {"value": o.value, "sourceUrl": o.source_url, "asOf": o.as_of}
            for o in indep
        ]
    else:
        state = "consistent"
        conflicting = []

    return {
        "factField": fact_field,
        # R-4:永远原样带出,不因冲突消失。
        "acceptedValue": accepted_value,
        "state": state,
        "independentSourceCount": len(indep),
        # §6.3 ``comparable_fact_fields``:「至少两个独立、可比较来源」。
        "isComparable": len(indep) >= 2,
        "conflictingSources": conflicting,
        "evidenceGated": fact_field in EVIDENCE_GATED_FACT_FIELDS,
        "resolverVersion": ENTITY_RESOLVER_VERSION,
        "factsVersion": CANONICAL_FACTS_VERSION,
    }


def assert_entity_state_not_collapsed(
    *, entity_state: str, projected_outcome: str
) -> None:
    """MET-07 / MET-26:``misidentified`` / ``ambiguous`` 不得被吞。

    十态 outcome 允许 ``misidentified`` 落在 ``not_mentioned``(§7.4 明说可以),
    但那只是 outcome 那一列;``entity_state`` 这一列必须原样保留,报告才有
    「AI 认错了谁」那张卡。所以这里拦的是**把 entity_state 本身改掉**。
    """
    if entity_state not in ENTITY_STATES:
        raise SourceConsistencyError(
            f"未知 entityState {entity_state!r};五分 = {list(ENTITY_STATES)}")
    if entity_state in MUST_NOT_COLLAPSE_INTO_NONMENTION:
        if projected_outcome in ("engine_error",):
            raise SourceConsistencyError(
                f"entityState={entity_state} 被投影成 engine_error —— "
                "认错主体/同名难辨不是引擎故障,压成故障会让这一格从品牌分母里"
                "整个消失(MET-07 / MET-26)。"
            )


def misidentification_card(
    *, matched_string: str, resolved_as: str, evidence_excerpt: str,
) -> dict[str, Any]:
    """§7.4 要求的「AI 认错了谁」单独展示 + 证据 + 校准动作。

    🔴 Z-3.2(§0.5.6):身份校准**默认给选择题**,不做成纯手工输入框。
       所以这里下发的 nextAction 是 ``review_identity``,由前端渲染候选身份
       一键确认;AI 不可用时才 fail-closed 落到人工兜底。
    """
    if not matched_string or not resolved_as:
        raise SourceConsistencyError(
            "认错主体卡必须同时给出「命中的字符串」与「AI 认成了谁」——"
            "少任何一半,销售都无法向客户解释这一格"
        )
    return {
        "kind": "misidentified",
        "matchedString": matched_string,
        "resolvedAs": resolved_as,
        "evidenceExcerpt": evidence_excerpt,
        "nextActionKind": "review_identity",
    }


def census() -> dict[str, Any]:
    return {
        "factsVersion": CANONICAL_FACTS_VERSION,
        "resolverVersion": ENTITY_RESOLVER_VERSION,
        "canonicalFactFields": list(CANONICAL_FACT_FIELDS),
        "evidenceGatedFactFields": sorted(EVIDENCE_GATED_FACT_FIELDS),
        "entityStates": list(ENTITY_STATES),
        "mustNotCollapseIntoNonmention": sorted(MUST_NOT_COLLAPSE_INTO_NONMENTION),
        "requiredProvenanceFields": list(REQUIRED_PROVENANCE_FIELDS),
        "consistencyStates": list(CONSISTENCY_STATES),
    }
