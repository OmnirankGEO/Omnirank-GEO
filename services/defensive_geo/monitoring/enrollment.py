"""§15.9.3 持续监测 adapter 的 admission gate —— MON-12 / ACT-11 承重点。

规格 §15.9.3 逐字:

    「持续监测与套餐 D30 对 **server-enrolled v2** 必须先有 ``service_activated``
      和相应 monitoring/retest 私有预算;未满足时**零 freeze/outbox/provider**,
      并返回资金/审批/激活 action。**报价前 formal diagnosis 不走此 gate**,
      **legacy monitoring subscription/run 也保持现役 admission、计费和幂等,
      不被新状态阻断**。」

这段话有两个**方向相反**的要求,任何一个做漏都是 §19 变异 140 的形态:

    ① 新门没接上  ⇒ v2 未激活也能跑,钱先冻了再说;
    ② 新门接过头  ⇒ 老代理的现役监测被新状态挡死,线上当场炸。

所以本模块的入口 :func:`admit` 第一件事就是**分流**,而且分流事实
只来自服务端 enrollment(``is_v2_enrolled``),**一个字都不读请求体** ——
客户端带个 ``mode=defensive`` 就能进 v2 的话,这道门等于没有。

🔴 「零副作用」不是"我们没调" —— 是可被计数的
--------------------------------------------
:class:`AdmissionDecision` 明确带 ``freeze_count_expected=0`` /
``outbox_count_expected=0`` / ``provider_call_count_expected=0``,
判据据此**逐个计数**。这比"检查函数返回了 rejected"硬:
后者在有人日后把副作用挪到 gate 之前时不会红。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple

#: §15.9.3 逐字的四个不予放行原因,与 ``ExecutionFundingPendingProjection``
#: 的 ``reasonCode`` 联合逐值相同 —— 两处写不同名字就会有一处没人验。
ADMISSION_REASONS: tuple[str, ...] = (
    "service_not_active",
    "insufficient_points",
    "budget_cap_exceeded",
    "approval_required",
)

#: reason → 该给她点哪。§0.5.6 铁律:任何阻塞必须自带解决方案。
#: 判据遍历 ADMISSION_REASONS 核对这张表**无漏项** —— 少一项就有一条死路。
REASON_TO_ACTION: dict[str, str] = {
    "service_not_active": "view_activation_status",
    "insufficient_points": "top_up",
    "budget_cap_exceeded": "request_budget_approval",
    "approval_required": "request_approval",
}

#: 三条 legacy 路径,**不受**新门约束(§15.9.3 逐字)。
LEGACY_PATHS: tuple[str, ...] = (
    "legacy_monitoring_subscription",
    "legacy_monitoring_run",
    "pre_quote_formal_diagnosis",
)

WORK_KINDS: tuple[str, ...] = ("writing", "continuous_monitoring", "publication")


class AdmissionError(ValueError):
    """入参不足以做 admission 判断。**不放行**,而不是默认放行。"""


class AdmissionDecision(NamedTuple):
    admitted: bool
    path: str                      # 'v2_enrolled' | 三条 legacy 之一
    reason_code: str | None
    next_action_kind: str | None
    #: 判据逐个计数的三位。未放行时**必须**全为 0。
    freeze_count_expected: int
    outbox_count_expected: int
    provider_call_count_expected: int


def _rejected(reason: str) -> AdmissionDecision:
    if reason not in ADMISSION_REASONS:
        raise AdmissionError(f"未登记的 admission reason {reason!r}")
    return AdmissionDecision(
        admitted=False, path="v2_enrolled", reason_code=reason,
        next_action_kind=REASON_TO_ACTION[reason],
        freeze_count_expected=0, outbox_count_expected=0,
        provider_call_count_expected=0,
    )


def admit(
    *,
    is_v2_enrolled: bool,
    legacy_path: str | None = None,
    service_activated: bool = False,
    monitoring_budget_available: bool = False,
    budget_within_cap: bool = True,
    approval_satisfied: bool = True,
) -> AdmissionDecision:
    """§15.9.3 的 admission。

    🔴 ``is_v2_enrolled`` **只能**来自服务端 snapshot enrollment。
       调用方若从请求体推导它,这道门就被客户端接管了 ——
       §15.9.1 逐字:「分流事实仍来自服务端 snapshot enrollment,
       不能靠客户端带该字段进入 v2」。
    """
    if not is_v2_enrolled:
        # ── 方向② 的防线:legacy 一律原样放行,新状态一个都不检查 ──
        path = legacy_path or "legacy_monitoring_run"
        if path not in LEGACY_PATHS:
            raise AdmissionError(
                f"未登记的 legacy 路径 {path!r};合法 = {list(LEGACY_PATHS)}。"
                "乱起名字会让「新门有没有误伤 legacy」这件事失去分母。"
            )
        return AdmissionDecision(
            admitted=True, path=path, reason_code=None, next_action_kind=None,
            # legacy 的副作用由**现役**链路决定,本门不预言也不限制。
            freeze_count_expected=-1, outbox_count_expected=-1,
            provider_call_count_expected=-1,
        )

    # ── 方向① 的防线:v2 必须逐条满足,不满足零副作用 ──
    if not service_activated:
        return _rejected("service_not_active")
    if not approval_satisfied:
        return _rejected("approval_required")
    if not budget_within_cap:
        return _rejected("budget_cap_exceeded")
    if not monitoring_budget_available:
        return _rejected("insufficient_points")

    return AdmissionDecision(
        admitted=True, path="v2_enrolled", reason_code=None, next_action_kind=None,
        freeze_count_expected=-1, outbox_count_expected=-1,
        provider_call_count_expected=-1,
    )


def funding_pending_projection(
    decision: AdmissionDecision, *, work_kind: str, work_item_ref: str,
    preview_or_snapshot_ref: str, status_url: str,
) -> dict[str, Any]:
    """§15.9.3 的 ``ExecutionFundingPendingProjection``。

    🔴 ``commandId`` / ``freezeId`` **恒 null**(逐字)。这是"pre-command 状态"
       的定义:命令还没建,所以没有 id 可给。给了 id 就意味着某处已经建了东西,
       而那正是 §19 变异 142「资金不足时创建空 command/outbox 后再标 pending」。
    """
    if decision.admitted:
        raise AdmissionError("已放行的 admission 不应产出 funding-pending 投影")
    if work_kind not in WORK_KINDS:
        raise AdmissionError(f"未知 workKind {work_kind!r};合法 = {list(WORK_KINDS)}")
    return {
        "state": "execution_funding_pending",
        "workKind": work_kind,
        "workItemRef": work_item_ref,
        "previewOrSnapshotRef": preview_or_snapshot_ref,
        "reasonCode": decision.reason_code,
        "nextActionKind": decision.next_action_kind,
        "commandId": None,
        "freezeId": None,
        "statusUrl": status_url,
    }


def assert_zero_side_effects(
    decision: AdmissionDecision, *,
    observed_freezes: int, observed_outbox: int, observed_provider_calls: int,
) -> None:
    """判据逐个计数用。未放行时三位必须**实测**为 0。

    实测而不是断言"我们没调" —— 后者在有人把 provider 调用挪到 gate 之前时
    不会红,而这一条会。
    """
    if decision.admitted:
        return
    bad = []
    if observed_freezes:
        bad.append(f"freeze={observed_freezes}")
    if observed_outbox:
        bad.append(f"outbox={observed_outbox}")
    if observed_provider_calls:
        bad.append(f"providerCall={observed_provider_calls}")
    if bad:
        raise AdmissionError(
            f"未放行({decision.reason_code})却产生了副作用:{', '.join(bad)}。"
            "§15.9.3 逐字「未满足时零 freeze/outbox/provider」;"
            "先冻钱再说的写法会在用户还没被允许开工时就扣住她的算力。"
        )


def census() -> dict[str, Any]:
    return {
        "admissionReasons": list(ADMISSION_REASONS),
        "reasonToAction": dict(REASON_TO_ACTION),
        "legacyPaths": list(LEGACY_PATHS),
        "workKinds": list(WORK_KINDS),
    }
