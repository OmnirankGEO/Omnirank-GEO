"""§13.4 续费建议 —— 可解释,且**不自动扣费**。

规格 §13.4 逐字:「续费**不以**『发了多少篇』或单次推荐判断」,
并给了六个输入维度;末句是承重的:

    「续费仍是**新商业快照**,**不自动扣费**。」

🔴 「不自动扣费」在代码里长什么样
--------------------------------
不是"我们没写扣费代码"(那只证明这一版没写),而是:

1. 本模块**不 import** 任何资金模块(``middleware.billing`` / ``db.wallet_db`` /
   freeze/commit/release 一个都不碰)。判据用 AST census 遍历本文件的
   import 集合,出现任何资金 sink 即红 —— 这条锁**能拆红**:把一行
   ``from middleware import billing`` 加进来,判据必须立刻转红。
2. 产出物是一份 :class:`RenewalRecommendation`,它的
   ``next_action_kind`` 恒为 ``new_customer_snapshot`` ——
   也就是「重新出一份报价给客户确认」,而不是任何形式的"直接续"。
   :func:`assert_no_auto_charge` 把这一位钉死。

🔴 六个维度里有**两个反方向**的信号,不能只报好消息
--------------------------------------------------
「问题空洞是否减少」「冲突是否修复」「可观察来源是否扩大」是正向;
「链接下架、信息过时」是负向;「套餐服务期和剩余预算」是约束。
只报正向的续费建议在销售嘴里会变成"效果很好所以续" ——
客户下一次自己看到下架链接时,信任就没了。
:func:`build` 因此要求正负两侧都显式给出(可以为空,但必须是**算过**的空)。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

RENEWAL_VERSION = "defgeo-renewal-recommendation-v1"

TABLE = "defgeo_renewal_recommendations"

#: §13.4 逐字六个输入维度。判据拿它当分母:少算一个维度必红。
RENEWAL_SIGNALS: tuple[str, ...] = (
    "answer_gap_change",            # 问题空洞是否减少
    "identity_and_fact_conflict_repair",  # 身份与事实冲突是否修复
    "observable_source_growth",     # 可观察来源是否扩大
    "same_scope_outcome_change",    # 同口径提及/推荐/场景覆盖的变化
    "link_retraction_and_staleness",  # 链接下架、信息过时与新增业务
    "service_period_and_budget",    # 套餐服务期和剩余预算
)

#: 负向信号 —— 必须与正向一起下发,不许只报好消息。
NEGATIVE_SIGNALS: frozenset[str] = frozenset({
    "link_retraction_and_staleness",
})

#: 🔴 恒定的下一步。续费 = 新商业快照,不是"继续扣"。
NEXT_ACTION_KIND = "new_customer_snapshot"

#: 判据的负向锁:这些词出现在建议的**任何**字段里都说明有人把续费
#: 做成了自动动作。锁能拆红:把其中一个词写进 summary,判据必须转红。
FORBIDDEN_AUTO_CHARGE_MARKERS: tuple[str, ...] = (
    "auto_charge", "auto_renew", "autoRenew", "autoCharge",
    "charge_now", "freeze_now", "commit_points",
)

#: 建议强度三档。**没有**"必须续"这一档 —— 续不续是客户的商业决定,
#: 我们只负责把事实讲清楚。
RECOMMENDATION_LEVELS: tuple[str, ...] = (
    "renew_recommended", "renew_with_adjustment", "insufficient_basis",
)


class RenewalError(ValueError):
    """建议形态不合法。**不下发**,而不是下发一份只讲好消息的续费理由。"""


class SignalReading(NamedTuple):
    signal: str
    #: 'improved' | 'worsened' | 'unchanged' | 'unknown'
    #: 🔴 ``unknown`` 是**一等公民**:没测到就是没测到,不许当成 unchanged。
    direction: str
    evidence_refs: tuple[str, ...]
    explanation_key: str


class RenewalRecommendation(NamedTuple):
    level: str
    signals: tuple[SignalReading, ...]
    next_action_kind: str
    recommendation_version: str
    #: §13.4:续费是**新商业快照**。这里只放"要基于哪份报告去出新报价",
    #: 不放任何金额 —— 价格一律服务端实时签发(G-6)。
    basis_report_snapshot_id: str
    comparability_level: str


DIRECTIONS: tuple[str, ...] = ("improved", "worsened", "unchanged", "unknown")


def assert_no_auto_charge(payload: Mapping[str, Any]) -> None:
    """§13.4 末句的可执行形式。

    遍历建议的全部字符串叶子;出现任何自动扣费标记即抛。
    """
    for value in _walk_strings(payload):
        for marker in FORBIDDEN_AUTO_CHARGE_MARKERS:
            if marker in value:
                raise RenewalError(
                    f"续费建议里出现自动扣费标记「{marker}」。"
                    "§13.4 逐字:续费仍是**新商业快照**,不自动扣费 —— "
                    "客户必须重新看到一份报价并确认。"
                )


def _walk_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for k, v in node.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk_strings(v)


def build(
    *,
    signals: Sequence[SignalReading],
    basis_report_snapshot_id: str,
    comparability_level: str,
) -> RenewalRecommendation:
    """按六维读数出建议。缺任一维、或只报正向,都抛。"""
    seen = {s.signal for s in signals}
    missing = [s for s in RENEWAL_SIGNALS if s not in seen]
    if missing:
        raise RenewalError(
            f"续费建议缺维度 {missing} —— §13.4 逐字六维,少一维就是"
            "挑着说。判据拿 RENEWAL_SIGNALS 当分母。"
        )
    extra = [s for s in seen if s not in RENEWAL_SIGNALS]
    if extra:
        raise RenewalError(f"续费建议出现未登记维度 {extra}")
    for s in signals:
        if s.direction not in DIRECTIONS:
            raise RenewalError(
                f"{s.signal} 的 direction={s.direction!r} 非法;合法 = {list(DIRECTIONS)}")
        if s.direction in ("improved", "worsened") and not s.evidence_refs:
            raise RenewalError(
                f"{s.signal} 报了 {s.direction} 却没有证据 ref —— "
                "没有证据的「变好了」是销售话术,不是续费依据(§13.4「可解释」)"
            )

    # 🔴 不可比时不出"效果变好/变差"的结论。
    #    §13.2:「不可比时不算涨跌」——续费建议是涨跌的下游,同一条铁律。
    if comparability_level == "none":
        level = "insufficient_basis"
    else:
        worsened = [s for s in signals if s.direction == "worsened"]
        improved = [s for s in signals if s.direction == "improved"]
        if not improved and not worsened:
            level = "insufficient_basis"
        elif worsened:
            level = "renew_with_adjustment"
        else:
            level = "renew_recommended"

    rec = RenewalRecommendation(
        level=level,
        signals=tuple(signals),
        next_action_kind=NEXT_ACTION_KIND,
        recommendation_version=RENEWAL_VERSION,
        basis_report_snapshot_id=basis_report_snapshot_id,
        comparability_level=comparability_level,
    )
    assert_no_auto_charge(as_payload(rec))
    return rec


def as_payload(rec: RenewalRecommendation) -> dict[str, Any]:
    return {
        "level": rec.level,
        "nextActionKind": rec.next_action_kind,
        "recommendationVersion": rec.recommendation_version,
        "basisReportSnapshotId": rec.basis_report_snapshot_id,
        "comparabilityLevel": rec.comparability_level,
        "signals": [
            {
                "signal": s.signal,
                "direction": s.direction,
                "evidenceRefs": list(s.evidence_refs),
                "explanationKey": s.explanation_key,
                "isNegative": s.signal in NEGATIVE_SIGNALS,
            }
            for s in rec.signals
        ],
    }


def census() -> dict[str, Any]:
    return {
        "renewalVersion": RENEWAL_VERSION,
        "table": TABLE,
        "signals": list(RENEWAL_SIGNALS),
        "negativeSignals": sorted(NEGATIVE_SIGNALS),
        "directions": list(DIRECTIONS),
        "levels": list(RECOMMENDATION_LEVELS),
        "nextActionKind": NEXT_ACTION_KIND,
        "forbiddenAutoChargeMarkers": list(FORBIDDEN_AUTO_CHARGE_MARKERS),
    }
