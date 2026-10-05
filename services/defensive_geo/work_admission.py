"""付费 work command 的准入闸 —— ACT-04 / ACT-05 / ACT-10 / ACT-11 / ACT-15。

规格 §3.1(只有 service_activated 才允许创建付费命令)/§3.3(组织与席位)/
§10.3 已被 §0.5.5 U-7 更正(出口以 §15.7 为准)。

本模块只回答一个问题:**这条付费命令现在能不能建?**
它**不扣钱、不冻结、不入队、不调 provider** —— 一个 import 都没有指向钱包。
ACT-11/15 逐字要求"零 freeze、零 outbox、零 provider",最稳的实现方式是
让这一层根本没有能力做那些事。

🔴 为什么不返回 bool
--------------------
§0.5.6 铁律「任何阻塞与错误必须自带解决方案」。返回 ``False`` 的闸是死路:
调用方只知道不行,不知道差什么、该点哪。所以恒返回
:class:`AdmissionVerdict`,拒绝时必带 typed action(ACT-15「有 typed action」)。
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from services.defensive_geo import funding_projection

ADMISSION_VERSION = "defensive-geo-work-admission-v1"

#: 付费 work command 的三条腿(§3.1「才允许创建付费写作、监测、发布命令」)。
WorkKind = Literal["content_generation", "continuous_monitoring", "media_publication"]
WORK_KINDS: tuple[WorkKind, ...] = (
    "content_generation", "continuous_monitoring", "media_publication",
)

#: 🔴 只有这一个里程碑放行付费命令。§3.1 逐字。
#: 写成集合而不是 ``>= 某序号`` —— 序号比较会让日后插入新态时静默放行。
ADMITTING_MILESTONES: frozenset[str] = frozenset({"service_activated"})

#: ACT-11 点名的两个**必须零副作用**的态。单列出来是为了判据能直接拿它当分母。
ZERO_EFFECT_MILESTONES: frozenset[str] = frozenset(
    {"customer_accepted", "activation_pending"}
)

BlockReason = Literal[
    "service_not_activated",      # ACT-11
    "execution_funding_pending",  # ACT-04 / ACT-15
    "approval_required",          # ACT-05
    "scope_cap_exhausted",        # §4.1 scope_caps_points
    "capability_unavailable",     # ACT-10(O1)
]


class AdmissionAction(NamedTuple):
    """一条可执行出口。``kind`` 给机器,``label`` 给人 —— label 里不许有内部枚举。"""

    kind: str
    label: str


class AdmissionVerdict(NamedTuple):
    admitted: bool
    #: 拒绝原因;放行时为 None。
    reason: BlockReason | None
    #: 人话说明(服务商面)。放行时为空串。
    message: str
    #: 出口。拒绝时**恒非空**(§0.5.6);放行时为空。
    actions: tuple[AdmissionAction, ...]
    #: ACT-11/15:本次是否产生任何副作用。恒为 0 —— 本层没有能力产生副作用。
    freeze_count: int = 0
    outbox_count: int = 0
    provider_calls: int = 0
    admission_version: str = ADMISSION_VERSION


def _blocked(
    reason: BlockReason, message: str, actions: tuple[AdmissionAction, ...]
) -> AdmissionVerdict:
    if not actions:                              # 防止有人日后加一条无出口的拒绝
        raise ValueError(f"拒绝原因 {reason} 没有出口 —— §0.5.6 不允许死路")
    return AdmissionVerdict(
        admitted=False, reason=reason, message=message, actions=actions
    )


#: ACT-04 / ACT-15 的常驻文案。§0.5.5 U-4 逐字:
#: 「客户货款已收妥;开始交付需消耗你的算力,还差 X,充值后自动继续」。
def funding_pending_message(shortfall_points: int) -> str:
    return (
        f"客户货款已收妥;开始交付需消耗你的算力,还差 {shortfall_points},充值后自动继续"
    )


#: 四格 funding policy → 余额不足时允许的出口。**复用窗A 的矩阵,不复述**。
_ACTION_LABELS: dict[str, str] = {
    "top_up": "去充值",
    "reduce_plan": "减少本次交付量",
    "request_budget_approval": "向组织申请预算审批",
}


def _insufficient_actions(funding_policy: str) -> tuple[AdmissionAction, ...]:
    kinds = funding_projection.allowed_insufficient_actions(funding_policy)
    if not kinds:
        # 平台账不存在"余额不足",只可能是 policy 不可用 → 转支持。
        return (AdmissionAction("contact_support", "联系平台支持"),)
    return tuple(
        AdmissionAction(k, _ACTION_LABELS.get(k, k)) for k in kinds
    )


def admit(
    *,
    work_kind: str,
    milestone: str,
    funding_policy: str,
    balance_points: int,
    required_points: int,
    scope_remaining_points: int | None = None,
    approval_state: str = "not_required",
    capability_available: bool = True,
) -> AdmissionVerdict:
    """判定一条付费 work command 能否创建。

    检查顺序是**有意的**:先里程碑(ACT-11)、再能力(ACT-10)、再审批(ACT-05)、
    最后才是钱(ACT-04/15)。理由 = ACT-04「客户确认不查询执行钱包」:
    还没到该看钱的时候就去看钱,本身就是缺陷。顺序反了会让"服务没激活"
    被报成"余额不足",给销售一个错误的出口(去充值也没用)。
    """
    if work_kind not in WORK_KINDS:
        raise ValueError(f"未知 work kind {work_kind!r};合法 = {list(WORK_KINDS)}")

    # ---- ACT-11:里程碑闸 ----------------------------------------------
    if milestone not in ADMITTING_MILESTONES:
        return _blocked(
            "service_not_activated",
            "服务还没正式开工,现在还不能创建这项交付任务。",
            (AdmissionAction("view_milestone", "查看当前进度"),),
        )

    # ---- ACT-10(O1):单项能力不可用**不阻塞其它**能力 ------------------
    # 注意这条只否掉**本条** command,不是整包 —— 调用方对其它 work_kind
    # 再调一次本函数仍可放行。这就是"不阻塞其它套餐能力"的实现形态。
    if not capability_available:
        return _blocked(
            "capability_unavailable",
            "发布渠道未接入或未就绪,这一项做不了(其它交付项不受影响,可照常进行)。",
            (AdmissionAction("skip_capability", "先跳过这一项"),
             AdmissionAction("contact_support", "联系平台支持")),
        )

    # ---- ACT-05:审批闸(必须早于扣钱判断)------------------------------
    # 「普通成员无审批时得到可执行 403 handoff;**不扣个人钱包**」——
    # 把审批放在余额之前,保证组织成员永远不会因为"个人余额够"而被静默扣。
    if approval_state in ("required", "rejected"):
        return _blocked(
            "approval_required",
            "这项支出需要先获得组织审批。",
            (AdmissionAction("request_approval", "请求审批"),
             AdmissionAction("view_approver", "查看该找谁批")),
        )

    # ---- §4.1:scope cap 不得跨 scope 挪用 -------------------------------
    if scope_remaining_points is not None and required_points > scope_remaining_points:
        return _blocked(
            "scope_cap_exhausted",
            "这一类交付的预算额度已经用完了(其它类别的额度不能挪过来用)。",
            (AdmissionAction("raise_scope_cap", "调高这一类的预算"),
             AdmissionAction("reduce_plan", "减少本次交付量")),
        )

    # ---- ACT-04 / ACT-15:余额闸 ----------------------------------------
    if required_points > balance_points:
        shortfall = required_points - balance_points
        return _blocked(
            "execution_funding_pending",
            funding_pending_message(shortfall),
            _insufficient_actions(funding_policy),
        )

    return AdmissionVerdict(
        admitted=True, reason=None, message="", actions=()
    )


def zero_effect_milestones() -> frozenset[str]:
    """ACT-11 判据的分母。前端/判据从这里取,不手抄。"""
    return ZERO_EFFECT_MILESTONES
