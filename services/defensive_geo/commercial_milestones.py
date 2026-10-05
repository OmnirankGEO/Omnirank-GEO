"""五个商业里程碑 —— **只读投影**,不是第二套状态机。

规格 §3.1(五里程碑与现役接缝)/§3.2(不可变确认合同)/§0.5.5 U-3(对客进度条)。
判据 = ACT-01(legacy 不漂)/ACT-03(v2 确认只写审计)/ACT-14(现役 route 逐值绑定)。

🔴 为什么这里**没有** VALID_TRANSITIONS 的副本
------------------------------------------------
现役商业状态机是 ``api/selection_api.py`` 的 ``VALID_TRANSITIONS``
(``selecting → … → quoted → confirmed → pending_payment → active``)。
§3.2 L304 逐字划死:五阶段**只适用于**明确 enrolled、带 ``geo-delivery-plan-v1``
的 v2 snapshot;legacy ``/confirm-quote`` **保持现役行为**,
「本包不得为了新语义删除或移动旧调用」。

所以本模块是**投影**不是**状态机**:输入现役 session 状态 + 已落库的激活事实,
输出五里程碑视图。改这里**不会**、也**不许**改变任何 legacy 请求的行为。
反过来说 —— 如果有人日后在这里加了写操作,ACT-01 会转红。

一个现役与规格的真实落差(必须记在这里,不能靠记忆)
--------------------------------------------------
现役只有一个 ``active`` 态,规格要求 ``commercial_basis_established``
(已收款)与 ``service_activated``(已物化)**分开**,中间还有
``activation_pending``(§3.1 L288-290 的 mermaid)。现役 ``active`` 因此
**不足以**判断到底是「收了钱还没开工」还是「已经开工」。
本模块不猜:没有耐久激活事实时,``active`` 只投影到
``commercial_basis_established``,并把 ``service_activated`` 留给
activation outbox 的物化事实(见 ``activation_outbox``)。
把 ``active`` 直接读成 ``service_activated`` 会让 ACT-06/ACT-13 静默变绿 ——
那正是「用一个复合态代表其中任一条件」的老毛病。
"""

from __future__ import annotations

from typing import Literal, Mapping, NamedTuple

PROJECTION_VERSION = "defensive-geo-commercial-milestone-projection-v1"

Milestone = Literal[
    "draft",
    "offer_sent",
    "customer_accepted",
    "sales_validated",
    "commercial_basis_established",
    "activation_pending",
    "service_activated",
]

#: §3.1 L281-291 的状态机节点全序。判据拿它当分母,不手抄。
MILESTONE_ORDER: tuple[Milestone, ...] = (
    "draft",
    "offer_sent",
    "customer_accepted",
    "sales_validated",
    "commercial_basis_established",
    "activation_pending",
    "service_activated",
)

#: §3.1 L281-291 的合法迁移。``activation_pending`` 是
#: ``commercial_basis_established`` 的**旁路**,不是它和 ``service_activated``
#: 之间的必经站 —— 物化一次成功就直接到 ``service_activated``。
ALLOWED_TRANSITIONS: dict[Milestone, frozenset[Milestone]] = {
    "draft": frozenset({"offer_sent"}),
    "offer_sent": frozenset({"customer_accepted"}),
    "customer_accepted": frozenset({"sales_validated"}),
    "sales_validated": frozenset({"commercial_basis_established"}),
    "commercial_basis_established": frozenset(
        {"service_activated", "activation_pending"}
    ),
    "activation_pending": frozenset({"service_activated"}),
    "service_activated": frozenset(),
}

#: 现役 ``sessions.status`` → 里程碑。**只映射,不改写**。
#: 未列出的现役状态一律投影为 ``draft``:报价还没签发出去,五里程碑尚未开始。
#: 🔴 ``active`` 只到 ``commercial_basis_established`` —— 理由见模块 docstring。
_SESSION_STATUS_TO_MILESTONE: dict[str, Milestone] = {
    "quoted": "offer_sent",
    "confirmed": "customer_accepted",
    "pending_payment": "sales_validated",
    "payment_overdue": "sales_validated",
    "active": "commercial_basis_established",
}

#: §0.5.5 U-3 L131 逐字的五格进度条。**内部名禁上屏**(U-3 L132)。
#: 注意这里只有 5 格而里程碑有 7 个:``draft`` 尚未进条,
#: ``activation_pending`` 与 ``commercial_basis_established`` 合并显示为「已收款」
#: —— 对销售而言这两态要做的事完全相同(等系统开工),分开显示只会制造焦虑。
_PROGRESS_STEPS: tuple[tuple[str, tuple[Milestone, ...]], ...] = (
    ("报价已发", ("offer_sent",)),
    ("客户已同意", ("customer_accepted",)),
    ("待核单", ("sales_validated",)),
    ("已收款", ("commercial_basis_established", "activation_pending")),
    ("服务中", ("service_activated",)),
)

#: §0.5.5 U-2 的 ``userLabel``:服务端下发,**前端不得自造**。
_USER_LABELS: dict[Milestone, str] = {
    "draft": "草稿",
    "offer_sent": "报价已发",
    "customer_accepted": "客户已同意",
    "sales_validated": "待核单",
    "commercial_basis_established": "已收款",
    "activation_pending": "已收款,系统准备开工中",
    "service_activated": "服务中",
}

#: 每态**唯一**的「下一步」(U-3 L132「每态配唯一『下一步』按钮」)。
#: 恒非空 —— 没有下一步的态只有终态 ``service_activated``,它给的是去看服务。
_NEXT_ACTION: dict[Milestone, tuple[str, str]] = {
    "draft": ("send_offer", "把报价发给客户"),
    "offer_sent": ("await_customer", "等客户确认(可重发链接)"),
    "customer_accepted": ("sales_validate", "核对价格与交付内容"),
    "sales_validated": ("collect_payment", "与客户完成收款对账"),
    "commercial_basis_established": ("await_activation", "系统正在准备开工"),
    "activation_pending": ("await_activation", "系统正在准备开工"),
    "service_activated": ("open_service", "查看服务进度"),
}


class MilestoneView(NamedTuple):
    """一条可直接下发给前端的里程碑投影。"""

    milestone: Milestone
    #: 人话状态词。内部枚举名永不上屏。
    user_label: str
    #: 五格进度条的当前格(1-based);``draft`` 时为 0。
    step_index: int
    step_total: int
    #: 逐格标签,给前端画条用 —— 前端不得自己写死这五个词。
    steps: tuple[str, ...]
    next_action_kind: str
    next_action_label: str
    projection_version: str


class NotEnrolled(NamedTuple):
    """未 enrolled 的 legacy 报价。**没有里程碑视图** —— 这不是错误,是边界。

    ACT-01 要求 legacy 行为零漂移;给 legacy 编一个五里程碑视图就是漂移。
    """

    reason: str


def is_v2_enrolled(pricing_snapshot: Mapping[str, object] | None) -> bool:
    """分流键 —— §3.2 L306:**必须来自服务端 snapshot schema,禁信客户端 mode**。

    只有 ``pricing_snapshot.delivery_plan.schema_version == 'geo-delivery-plan-v1'``
    才算 enrolled。请求体里的 ``mode`` 字段在这里一个字都不读。
    """
    if not isinstance(pricing_snapshot, Mapping):
        return False
    plan = pricing_snapshot.get("delivery_plan")
    if not isinstance(plan, Mapping):
        return False
    from services.defensive_geo.delivery_plan import SCHEMA_VERSION

    return plan.get("schema_version") == SCHEMA_VERSION


def project(
    *,
    session_status: str | None,
    pricing_snapshot: Mapping[str, object] | None,
    has_durable_activation: bool = False,
    activation_materialized: bool = False,
) -> MilestoneView | NotEnrolled:
    """把现役事实投影成五里程碑视图。

    :param session_status: 现役 ``sessions.status`` 原值。
    :param pricing_snapshot: 现役 ``quote_pricing_snapshots.pricing_snapshot``。
    :param has_durable_activation: 是否已落耐久 activation event(收款/线下授权)。
    :param activation_materialized: 物化是否**已完成**(问题、交付格、服务期都已幂等物化)。

    ``has_durable_activation`` 与 ``activation_materialized`` 分开两个参数,
    不合并成一个三值枚举:合并会让「已收款未物化」和「已物化」共用一个入口,
    调用方少传一个就静默滑到终态。
    """
    if not is_v2_enrolled(pricing_snapshot):
        return NotEnrolled(
            "该报价不是本版交付计划,按现役流程处理(五里程碑不适用)。"
        )

    base: Milestone = _SESSION_STATUS_TO_MILESTONE.get(
        (session_status or "").strip(), "draft"
    )

    # 只有在现役已到 active(即 commercial basis 成立)之后,物化事实才有意义。
    # 把物化事实用在更早的态上会让「还没收款就服务中」成为可能。
    if base == "commercial_basis_established":
        if activation_materialized:
            base = "service_activated"
        elif has_durable_activation:
            base = "activation_pending"

    step_index = 0
    for position, (_label, members) in enumerate(_PROGRESS_STEPS, start=1):
        if base in members:
            step_index = position
            break

    kind, label = _NEXT_ACTION[base]
    return MilestoneView(
        milestone=base,
        user_label=_USER_LABELS[base],
        step_index=step_index,
        step_total=len(_PROGRESS_STEPS),
        steps=tuple(s for s, _ in _PROGRESS_STEPS),
        next_action_kind=kind,
        next_action_label=label,
        projection_version=PROJECTION_VERSION,
    )


def user_label(milestone: str) -> str:
    """状态词查表。未知值**抛错**,不返回原串 —— 返回原串就是内部枚举上屏。"""
    try:
        return _USER_LABELS[milestone]  # type: ignore[index]
    except KeyError:
        raise ValueError(
            f"未知里程碑 {milestone!r};合法值 = {list(MILESTONE_ORDER)}。"
            "不回退成原串:回退会让内部枚举裸串上屏(§0.5.5 U-1 = 验收红)。"
        ) from None


def progress_steps() -> tuple[str, ...]:
    """五格进度条标签。前端从这里取,不自己写死。"""
    return tuple(s for s, _ in _PROGRESS_STEPS)


def customer_confirm_notice() -> str:
    """§0.5.5 U-3 L133 逐字要求的客户确认成功页文案。

    「替销售挡住『我确认了然后呢』」—— 这句话是产品承诺的一部分,不是装饰。
    """
    return "已确认。您的服务商将与您完成收款对账后开始服务"
