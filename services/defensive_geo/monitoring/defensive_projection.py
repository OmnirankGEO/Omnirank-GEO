"""§13.1 DefensiveOutcomeProjection —— 额外的、版本化的防御事实投影。

🔴 第一条铁律:**不覆盖现役 outcome**
------------------------------------
规格 §13.1 逐字:「保留现役 raw result 与 ``target_outcome``。防御事实准确性是
额外版本化投影,**不能**把现役提及/推荐枚举改成另一套含义。」

所以本模块:

* 只**读** ``monitoring_results.target_outcome`` 等现役列,一次 UPDATE 都不发;
* 产出物存进独立表 ``defgeo_monitoring_defensive_projections``,
  以 ``projection_id``(§6.1)为主键 —— 分类器升级 = 新 projection 行,
  历史那一行原封不动(MON-04)。

🔴 第二条:``supportLevel`` 与 ``hasConflict`` **正交**
------------------------------------------------------
§7.5 / MET-31 逐字:「``corroborated + hasConflict=true`` 可合法并存;
冲突**不强制降** unsupported」。合并成一个 ``unsupported_or_conflicting`` 态
是本规格点名禁止的复活项(§13.1)。:func:`assert_orthogonal` 把它写死。

现实里这一条护的是客户:有一条公开资料支持"在深圳提供定制服务",另一条给出
不同服务范围 —— 正确的话是"有资料支持,但信源存在冲突,需要先统一",
不是"查无此事"。后者会让销售把一条真事实从稿子里删掉。

🔴 第三条(R-1 · §0.5.2):低置信冲突**默认交 AI 复判**,不默认转人工
-------------------------------------------------------------------
原文 ``needsHumanReview`` 已按 §0.5.6 Z-5 就地改写。落地形态是四件套:

1. **留痕** ``ai:<model>@<version>`` 写进 ``adjudicated_by``;
2. **fail-closed** —— AI 不可用/返回不可解析时**不猜**,停在 ``pending_review``;
3. **幂等** —— 同一 ``projection_id`` 重复复判返回同一结论,不重复扣算力;
4. **不阻断** —— 复判在跑的时候,这一格照常带着 ``pending_review`` 进报告,
   不拦住整份诊断。

只有**资金/身份类**冲突才保留人工确认(§0.5.2 R-1 逐字)。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple, Sequence

from services.defensive_geo.monitoring import lineage as _lin

PROJECTION_VERSION = "defgeo-defensive-outcome-v1"

TABLE = "defgeo_monitoring_defensive_projections"

#: §13.1 逐字五态。``misidentified`` 是 §7.4 的承重点 ——
#: 「AI 认成了别家」既不是正常未提及也不是引擎失败。
IDENTITY_STATES: tuple[str, ...] = (
    "confirmed", "ambiguous", "misidentified", "not_evaluated",
)

COMPLETENESS_STATES: tuple[str, ...] = ("complete", "partial", "not_evaluable")

#: §7.5 四档。**没有** ``unsupported_or_conflicting`` —— 那是被点名禁止复活的合并态。
SUPPORT_LEVELS: tuple[str, ...] = ("corroborated", "inferred", "unsupported", "unknown")

#: 被点名禁止的合并态。判据拿它当负向锁:出现即红。
FORBIDDEN_MERGED_SUPPORT_LEVELS: frozenset[str] = frozenset({
    "unsupported_or_conflicting",
    "conflicting",
    "unsupported_or_conflict",
})

#: R-1:只有这两类冲突保留**人工**确认,其余默认交 AI。
HUMAN_ONLY_CONFLICT_KINDS: tuple[str, ...] = ("funding", "identity_ownership")

#: 复判裁决方留痕格式。判据钉这个形态 ——
#: 留痕写成 "ai" 或 "auto" 这种没有 model/version 的串等于没留痕。
_ADJUDICATOR_SHAPE = re.compile(r"^(ai:[A-Za-z0-9._\-]+@[A-Za-z0-9._\-]+|human:\d+)$")

ADJUDICATION_STATES: tuple[str, ...] = (
    "not_required",      # 无冲突或高置信,不需要复判
    "pending_review",    # fail-closed 停在这里(AI 不可用 / 返回不可解析)
    "ai_adjudicated",    # AI 复判已出结论,留痕带 model@version
    "human_adjudicated",  # 资金/身份类,人工确认过
)


class DefensiveProjectionError(ValueError):
    """投影形态不合法。**不下发**,而不是下发一份看起来合理的结论。"""


class DefensiveOutcomeProjection(NamedTuple):
    """§13.1 的 TS interface 逐字对应(camelCase 在 API 层转)。"""

    monitoring_result_id: int
    plan_item_key: str
    question_revision: int
    projection_version: str
    identity_state: str
    completeness: str
    support_level: str
    has_conflict: bool
    matched_facts: tuple[str, ...]
    missing_facts: tuple[str, ...]
    conflicting_facts: tuple[str, ...]
    evidence_manifest_revision: str
    confidence: float | None
    adjudication_state: str
    adjudicated_by: str | None
    projection_id: str


def assert_orthogonal(support_level: str, has_conflict: bool) -> None:
    """MET-31 / §7.5:两条轴正交。

    这个函数**不做任何降级** —— 它只拒绝把冲突塞进 supportLevel 的写法。
    调用方若传了合并态,这里抛;传 ``corroborated + True`` 则放行。
    """
    if support_level in FORBIDDEN_MERGED_SUPPORT_LEVELS:
        raise DefensiveProjectionError(
            f"supportLevel={support_level!r} 是被点名禁止复活的合并态。"
            "「有没有资料支持」与「资料之间有没有冲突」是两件事:"
            "corroborated + hasConflict=true 合法且必须能同时下发"
            "(§7.5 / §13.1 / MET-31)。"
        )
    if support_level not in SUPPORT_LEVELS:
        raise DefensiveProjectionError(
            f"未知 supportLevel {support_level!r};合法四档 = {list(SUPPORT_LEVELS)}"
        )
    if not isinstance(has_conflict, bool):
        raise DefensiveProjectionError("hasConflict 必须是布尔")


def assert_conflict_facts_consistent(
    *, has_conflict: bool, conflicting_facts: Sequence[str]
) -> None:
    """MET-23 逐字:「hasConflict=true 必有 conflict source、false 必为空」。"""
    if has_conflict and not conflicting_facts:
        raise DefensiveProjectionError(
            "hasConflict=true 但 conflictingFacts 为空 —— 说不出冲突在哪一条,"
            "客户就没法「先统一口径」(§7.5 的整条修复动作会落空)"
        )
    if not has_conflict and conflicting_facts:
        raise DefensiveProjectionError(
            "hasConflict=false 却带着 conflictingFacts —— 两者必须一致"
        )


def assert_adjudication_traceable(state: str, adjudicated_by: str | None) -> None:
    """R-1 四件套之「留痕」。

    ``ai_adjudicated`` 必须带 ``ai:<model>@<version>``;
    ``human_adjudicated`` 必须带 ``human:<user_id>``。
    没有版本的留痕在模型换代之后无法复现当时的判断 —— 等于没留。
    """
    if state not in ADJUDICATION_STATES:
        raise DefensiveProjectionError(f"未知复判状态 {state!r}")
    if state in ("not_required", "pending_review"):
        if adjudicated_by is not None:
            raise DefensiveProjectionError(
                f"{state} 不应带裁决方留痕(收到 {adjudicated_by!r})")
        return
    if not adjudicated_by or not _ADJUDICATOR_SHAPE.match(adjudicated_by):
        raise DefensiveProjectionError(
            f"{state} 的留痕 {adjudicated_by!r} 形态不合法。"
            "必须是 ai:<model>@<version> 或 human:<user_id> —— "
            "没有版本号的留痕在模型换代后无法复现当时的判断(R-1)。"
        )
    if state == "ai_adjudicated" and not adjudicated_by.startswith("ai:"):
        raise DefensiveProjectionError("ai_adjudicated 的留痕必须以 ai: 开头")
    if state == "human_adjudicated" and not adjudicated_by.startswith("human:"):
        raise DefensiveProjectionError("human_adjudicated 的留痕必须以 human: 开头")


def route_low_confidence(
    *,
    conflict_kind: str | None,
    ai_available: bool,
) -> str:
    """R-1 的分诊器:低置信冲突该走 AI 还是走人。

    §0.5.2 R-1 逐字:「内容/判断类低置信冲突**默认交 AI 评估**……
    人工确认只保留**资金/身份类**」。

    🔴 fail-closed 的方向是「停在待审」,不是「猜一个」。AI 不可用时返回
       ``pending_review`` —— 这一格照常进报告(不阻断),但它的结论是
       "还没判",不是"没问题"。
    """
    if conflict_kind in HUMAN_ONLY_CONFLICT_KINDS:
        # 资金/身份类:分诊器**只能**把它挂起等人。它自己不能宣布"人已经判了" ——
        # 那一位只有真的有人按下按钮时才由 :func:`assert_adjudication_traceable`
        # 带着 human:<user_id> 写进去。
        return "pending_review"
    if not ai_available:
        # fail-closed:不猜。
        return "pending_review"
    return "ai_adjudicated"


def build(
    *,
    monitoring_result_id: int,
    observation_cell_id: str,
    plan_item_key: str,
    question_revision: int,
    identity_state: str,
    completeness: str,
    support_level: str,
    has_conflict: bool,
    matched_facts: Sequence[str],
    missing_facts: Sequence[str],
    conflicting_facts: Sequence[str],
    evidence_manifest_revision: str,
    confidence: float | None,
    adjudication_state: str,
    adjudicated_by: str | None,
    resolver_version: str,
    classifier_version: str,
    evidence_extractor_version: str,
) -> DefensiveOutcomeProjection:
    """构造一条投影。任何一条形态不合法就抛,**不下发**。"""
    if identity_state not in IDENTITY_STATES:
        raise DefensiveProjectionError(
            f"未知 identityState {identity_state!r};合法 = {list(IDENTITY_STATES)}")
    if completeness not in COMPLETENESS_STATES:
        raise DefensiveProjectionError(f"未知 completeness {completeness!r}")
    assert_orthogonal(support_level, has_conflict)
    assert_conflict_facts_consistent(
        has_conflict=has_conflict, conflicting_facts=conflicting_facts)
    assert_adjudication_traceable(adjudication_state, adjudicated_by)
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        raise DefensiveProjectionError(f"confidence 必须在 [0,1] 或为 null,收到 {confidence}")
    if identity_state == "not_evaluated" and confidence is not None:
        raise DefensiveProjectionError(
            "not_evaluated 不应带 confidence —— 没评估过就没有置信度可言")

    pid = _lin.projection_id(
        observation_cell_id=observation_cell_id,
        resolver_version=resolver_version,
        classifier_version=classifier_version,
        evidence_extractor_version=evidence_extractor_version,
    )
    return DefensiveOutcomeProjection(
        monitoring_result_id=int(monitoring_result_id),
        plan_item_key=plan_item_key,
        question_revision=int(question_revision),
        projection_version=PROJECTION_VERSION,
        identity_state=identity_state,
        completeness=completeness,
        support_level=support_level,
        has_conflict=bool(has_conflict),
        matched_facts=tuple(matched_facts),
        missing_facts=tuple(missing_facts),
        conflicting_facts=tuple(conflicting_facts),
        evidence_manifest_revision=evidence_manifest_revision,
        confidence=(None if confidence is None else float(confidence)),
        adjudication_state=adjudication_state,
        adjudicated_by=adjudicated_by,
        projection_id=pid,
    )


def insert(cur, projection: DefensiveOutcomeProjection, *,
           tenant_owner_user_id: int, brand_id: int) -> bool:
    """写入投影。返回是否**新建**(False = 同 projection_id 已存在,幂等重放)。

    🔴 ``ON CONFLICT DO NOTHING`` 而不是 ``DO UPDATE``:MON-04 逐字
       「classifier 升级**不改历史 outcome**,生成新 projection version」。
       projection_id 已经吃了三个 version,所以升级必然换 id ——
       同 id 还想改内容,那就是在改写历史。
    """
    cur.execute(
        f"""
        INSERT INTO public.{TABLE}
            (projection_id, monitoring_result_id, tenant_owner_user_id, brand_id,
             plan_item_key, question_revision, projection_version,
             identity_state, completeness, support_level, has_conflict,
             matched_facts, missing_facts, conflicting_facts,
             evidence_manifest_revision, confidence,
             adjudication_state, adjudicated_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (projection_id) DO NOTHING
        """,
        (projection.projection_id, projection.monitoring_result_id,
         int(tenant_owner_user_id), int(brand_id),
         projection.plan_item_key, projection.question_revision,
         projection.projection_version,
         projection.identity_state, projection.completeness,
         projection.support_level, projection.has_conflict,
         list(projection.matched_facts), list(projection.missing_facts),
         list(projection.conflicting_facts),
         projection.evidence_manifest_revision, projection.confidence,
         projection.adjudication_state, projection.adjudicated_by),
    )
    return int(cur.rowcount) == 1


def legacy_outcome_is_untouched(cur, *, monitoring_result_id: int,
                                expected_target_outcome: str) -> bool:
    """§13.1「不覆盖现役 outcome」的**可执行**自证。

    判据用它:建完投影之后回读现役那一行,``target_outcome`` 必须逐字未变。
    这条比"我们没写 UPDATE"硬 —— 后者只证明这一版没写。
    """
    cur.execute(
        "SELECT target_outcome FROM public.monitoring_results WHERE id=%s",
        (int(monitoring_result_id),),
    )
    row = cur.fetchone()
    if not row:
        return False
    actual = row["target_outcome"] if isinstance(row, Mapping) else row[0]
    return str(actual) == str(expected_target_outcome)


def census() -> dict[str, Any]:
    return {
        "projectionVersion": PROJECTION_VERSION,
        "table": TABLE,
        "identityStates": list(IDENTITY_STATES),
        "completenessStates": list(COMPLETENESS_STATES),
        "supportLevels": list(SUPPORT_LEVELS),
        "forbiddenMergedSupportLevels": sorted(FORBIDDEN_MERGED_SUPPORT_LEVELS),
        "adjudicationStates": list(ADJUDICATION_STATES),
        "humanOnlyConflictKinds": list(HUMAN_ONLY_CONFLICT_KINDS),
    }
