"""Presentation 投影 —— SampleSummary 重建 + 五卡 + 受众裁剪。

规格 §8(报告 IA)/§15.6(Presentation DTO)/§9.4(同一 snapshot 三个出口)。
判据 = MET-18 / MET-22 / MET-29 / MET-35 / MET-36 / MET-39 / UI-23。

🔴 受众差异**只能由服务端裁剪**(§9.4 逐字)
--------------------------------------------
「客户版不接收内部 evidence id、供应商、成本、模型代号或 mutation capability」。
前端"不渲染"不算裁剪 —— 不渲染只是没画出来,接口照样把内容发出去了。
现役 ``services/gap_operation_plan.present_snapshot`` 已经是这个形态
(``受众硬编码 customer:actions 恒空``),本模块沿用同一套路,不另造第二套哲学。

🔴 canonical 与 audience-bound 分开
-----------------------------------
WP3 出口条件:「同一 snapshot 的网页、内部页、PDF **canonical business metric
payload 逐值一致**,而 audience-bound ref/commitment/content hash 必须各自签发」。
所以 :func:`project` 先算一份 canonical,再按受众裁剪 —— 裁剪只做减法,
不重算任何业务数字。重算就会出现"网页 7/9、PDF 6/9"这种事。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, NamedTuple, Sequence

from services.defensive_geo.presentation import copy_registry, registries

PROJECTION_VERSION = "defensive-geo-presentation-v2"

#: §15.6 ``PublicEvidenceCell.status`` 闭集。判据分母从这里取。
CELL_STATUSES: tuple[str, ...] = (
    "answered", "entity_ambiguous", "engine_error", "policy_skipped", "not_attempted",
)

#: MET-22:客户面与 PDF **任意嵌套层**都不许出现的键。
FORBIDDEN_CUSTOMER_KEYS: frozenset[str] = frozenset({
    "planTrace", "observationTrace", "attemptTrace", "projectionTrace",
    "articleRevisionId", "scoringModel", "scoringModelVersion",
    "providerName", "providerId", "modelCode", "costPoints", "rawAnswer",
    "internalRuleId", "capabilities",
})


class SampleSummary(NamedTuple):
    """§15.6 逐字十字段。每一个都必须能从 ledger 重建(MET-18/35)。"""

    planned: int
    attempted: int
    terminal: int
    policySkipped: int
    attemptRecords: int
    attemptErrors: int
    response: int
    valid: int
    identityAmbiguous: int
    engineErrors: int


class SummaryInconsistent(ValueError):
    """汇总与 ledger 对不上。**投影失败**,不下发半真半假的数字。

    MET-18 逐字「删除失败格或伪造任一汇总字段转红」——
    实现方式就是让不守恒的 summary 根本投影不出来。
    """


def build_sample_summary(
    *,
    plan_cells: Sequence[Mapping[str, Any]],
    evidence_cells: Sequence[Mapping[str, Any]],
    attempt_ledger: Sequence[Mapping[str, Any]],
) -> SampleSummary:
    """从 plan / evidence / attempt 三个源**逐值重建**汇总。

    公式取自 §15.6 L3479(逐字):

    - ``planned = count(plan cells)``
    - ``valid = count(status='answered')`` —— 🔴 **含 misidentified**
      (它是 entityState 不是 status;认错了也是"拿到了回答")
    - ``identityAmbiguous = count(status='entity_ambiguous')``
    - ``response = valid + identityAmbiguous``
    - ``engineErrors = count(final engine_error)``
    - ``policySkipped = count(final policy_skipped with zero attempt)``
    - ``terminal = response + engineErrors + policySkipped``,终态报告须 ``= planned``
    - ``attempted = response + engineErrors``
    - ``engineErrors <= attemptErrors <= attemptRecords``

    不接受调用方直接传 summary —— G-3 明令「禁止夹具直造 DTO 层」。
    """
    planned = len(plan_cells)
    by_status: dict[str, int] = {s: 0 for s in CELL_STATUSES}
    for cell in evidence_cells:
        status = cell.get("status")
        if status not in by_status:
            raise SummaryInconsistent(
                f"未知 evidence cell status {status!r};合法 = {list(CELL_STATUSES)}"
            )
        by_status[status] += 1

    valid = by_status["answered"]
    identity_ambiguous = by_status["entity_ambiguous"]
    engine_errors = by_status["engine_error"]
    policy_skipped = by_status["policy_skipped"]
    response = valid + identity_ambiguous
    attempted = response + engine_errors
    terminal = response + engine_errors + policy_skipped

    attempt_records = len(attempt_ledger)
    attempt_errors = sum(1 for a in attempt_ledger if a.get("error"))

    if len(evidence_cells) > planned:
        raise SummaryInconsistent(
            f"evidence cell 数({len(evidence_cells)})多于计划格({planned})"
        )
    if not (engine_errors <= attempt_errors <= attempt_records):
        raise SummaryInconsistent(
            f"attempt 守恒被破:engineErrors={engine_errors} "
            f"attemptErrors={attempt_errors} attemptRecords={attempt_records}"
        )
    if attempt_records < attempted:
        raise SummaryInconsistent(
            f"attemptRecords({attempt_records}) < attempted({attempted})"
        )

    return SampleSummary(
        planned=planned, attempted=attempted, terminal=terminal,
        policySkipped=policy_skipped, attemptRecords=attempt_records,
        attemptErrors=attempt_errors, response=response, valid=valid,
        identityAmbiguous=identity_ambiguous, engineErrors=engine_errors,
    )


def assert_terminal_conservation(summary: SampleSummary) -> None:
    """终态报告的额外守恒:``terminal == planned``(MET-35)。

    单独一个函数而不是塞进 build:运行中的报告 terminal < planned 是**合法**的,
    只有终态才要求相等。合并会把"还在跑"误判成"数据坏了"。
    """
    if summary.terminal != summary.planned:
        raise SummaryInconsistent(
            f"终态 terminal({summary.terminal}) != planned({summary.planned})"
        )


class CardView(NamedTuple):
    key: str
    question: str
    ordinal: int
    level_key: str
    level_label: str
    level_tone: str
    state: str
    actions: tuple[str, ...]
    summary: SampleSummary


def project_cards(
    *,
    summary_by_card: Mapping[str, SampleSummary],
    level_by_card: Mapping[str, str],
) -> tuple[CardView, ...]:
    """五卡投影 —— MET-29「恰各一次,删卡/重复/换序均拒绝」。

    卡的集合与顺序来自 registry,**不来自入参**:入参只提供数据。
    这样"少给一张卡"会以 KeyError 的形式当场炸,而不是静默少画一张。
    """
    out: list[CardView] = []
    for spec in registries.cards():
        if spec.key not in summary_by_card:
            raise ValueError(
                f"缺少客户卡 {spec.key} 的汇总;五卡必须恰各一次(MET-29)"
            )
        summary = summary_by_card[spec.key]
        state = registries.derive_section_state(
            valid=summary.valid, planned=summary.planned
        )
        # no_conclusion 时等级必须是 unknown —— 没有有效样本却给等级 = 编数字。
        level_key = "unknown" if state == "no_conclusion" else level_by_card[spec.key]
        meta = registries.level_meta(level_key)
        out.append(CardView(
            key=spec.key, question=spec.question, ordinal=spec.ordinal,
            level_key=meta.key, level_label=meta.label, level_tone=meta.tone,
            state=state, actions=registries.state_actions(state), summary=summary,
        ))
    return tuple(out)


def _strip_forbidden(node: Any) -> Any:
    """递归剥掉客户面禁键。**任意嵌套层**(MET-22 逐字)。"""
    if isinstance(node, Mapping):
        return {
            k: _strip_forbidden(v)
            for k, v in node.items()
            if k not in FORBIDDEN_CUSTOMER_KEYS
        }
    if isinstance(node, (list, tuple)):
        return [_strip_forbidden(v) for v in node]
    return node


def crop_for_audience(payload: Mapping[str, Any], audience: str) -> dict[str, Any]:
    """按受众裁剪。**只做减法**,不重算任何业务数字。

    - 服务商 / 服务商 demo:全量(含 trace 与 capability);
    - 客户 / 客户 demo / 客户 PDF:剥内部键,并过对客签发闸。

    §9.4「差异只能由服务端字段裁剪」;MET-22「customer/PDF schema 嵌套任意层
    均无这些 key」。
    """
    if audience not in registries.ALL_AUDIENCES:
        raise ValueError(
            f"未知受众 {audience!r};合法 = {list(registries.ALL_AUDIENCES)}"
        )
    if audience in registries.CUSTOMER_FACING_AUDIENCES:
        registries.require_signed_for_customer(audience)
        cropped = _strip_forbidden(payload)
        if audience == "customer_public_demo":
            cropped["demoNotice"] = copy_registry.SENTENCES["demo_read_only"]
        return cropped
    out = dict(payload)
    if audience == "service_provider_demo":
        # 服务商 demo「不缩水」但副作用恒零(§9.7 末行)。
        out["demoNotice"] = copy_registry.SENTENCES["demo_read_only"]
        out["sideEffectsDisabled"] = True
    return out


def find_internal_leaks(payload: Any) -> list[str]:
    """扫描将要下发给客户的 payload 里有没有内部枚举裸串上屏(U-1 = 红)。

    只扫**值**不扫键:键名 ``levelKey`` 是给机器的,值 ``needs_strengthening``
    才是会被画到屏幕上的那一个。这个区分很重要 —— 不分的话
    整个 DTO 都会被判红,判据就失去区分力了。
    """
    leaks: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and copy_registry.looks_like_internal_enum(node):
            leaks.append(f"{path} = {node!r}")

    walk(payload, "$")
    return leaks
