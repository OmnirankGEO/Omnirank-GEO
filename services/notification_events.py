"""关键业务通知事件目录（用户可见通知的唯一契约 SSOT）。"""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping


class NotificationLevel(str, Enum):
    IMPORTANT = "important"
    GENTLE = "gentle"
    LIGHT = "light"
    SILENT = "silent"


class RecipientKind(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    ADMIN = "admin"
    TRADE_BUYER = "trade_buyer"
    TRADE_SELLER = "trade_seller"
    USER = "user"


class NotificationEventType(str, Enum):
    RECHARGE_CREDITED = "recharge.credited"
    RECHARGE_REVIEW_REQUIRED = "recharge.review_required"
    AGENT_INVENTORY_CREDITED = "agent_inventory.credited"
    INVENTORY_ALLOCATION_COMPLETED = "inventory_allocation.completed"
    RESELL_COMPLETED = "resale.completed"
    REFUND_COMPLETED = "refund.completed"
    REFUND_FAILED = "refund.failed"
    REFUND_MANUAL_REQUIRED = "refund.manual_required"
    DISPUTE_FROZEN = "dispute.frozen"
    DISPUTE_RESOLVED = "dispute.resolved"
    AGENT_SETTLEMENT_SUBMITTED = "agent_settlement.submitted"
    AGENT_SETTLEMENT_APPROVED = "agent_settlement.approved"
    AGENT_SETTLEMENT_REJECTED = "agent_settlement.rejected"
    AGENT_SETTLEMENT_PAID = "agent_settlement.paid"
    # [防御型 GEO WP2 · 2026-08-21] confirm 落 outbox 需要一个**非终态**事件。
    # 曾经想直接复用 DIAGNOSIS_COMPLETED —— 那会在用户按下"确认"的**那一刻**
    # 推一条「品牌体检已完成」,而体检根本还没开始。这正是 R6(2026-08-17)在
    # MONITORING_ENABLED_BY_OTHER 上记过的同一个坑:通知的价值全在标题,
    # 标题说反了比不发更糟。所以另立事件,不蹭终态。
    DIAGNOSIS_CONFIRMED = "diagnosis.confirmed"
    DIAGNOSIS_COMPLETED = "diagnosis.completed"
    DIAGNOSIS_PARTIAL = "diagnosis.partial_success"
    DIAGNOSIS_FAILED = "diagnosis.failed"
    DIAGNOSIS_CANCELLED = "diagnosis.cancelled"
    DIAGNOSIS_REFUNDED = "diagnosis.refunded"
    DIAGNOSIS_MANUAL_REQUIRED = "diagnosis.manual_required"
    GEO_PLAN_COMPLETED = "geo_plan.completed"
    GEO_PLAN_FAILED = "geo_plan.failed"
    GEO_PLAN_CANCELLED = "geo_plan.cancelled"
    GEO_PLAN_REFUNDED = "geo_plan.refunded"
    GEO_PLAN_MANUAL_REQUIRED = "geo_plan.manual_required"
    MONITORING_COMPLETED = "monitoring.completed"
    MONITORING_PARTIAL = "monitoring.partial_success"
    MONITORING_FAILED = "monitoring.failed"
    MONITORING_PAUSED = "monitoring.paused"
    # [R6 2026-08-17] N1 开通告知必须有**自己的**事件类型:
    #   原来复用 MONITORING_COMPLETED,标题渲染成「效果监测已完成」——
    #   给钱包主人推一条"已完成",而实际发生的是"有人替你开通了、要开始扣钱了",
    #   标题语义完全相反。通知的价值全在标题(用户多半只看那一行)。
    MONITORING_ENABLED_BY_OTHER = "monitoring.enabled_by_other"
    WRITING_COMPLETED = "writing.completed"
    WRITING_PARTIAL = "writing.partial_success"
    WRITING_FAILED = "writing.failed"
    WRITING_CANCELLED = "writing.cancelled"
    RESEARCH_COMPLETED = "research.completed"
    RESEARCH_PARTIAL = "research.partial_success"
    RESEARCH_FAILED = "research.failed"
    RESEARCH_CANCELLED = "research.cancelled"
    RESEARCH_REFUNDED = "research.refunded"
    RESEARCH_MANUAL_REQUIRED = "research.manual_required"
    PUBLICATION_COMPLETED = "publication.completed"
    PUBLICATION_PARTIAL = "publication.partial_success"
    PUBLICATION_REJECTED = "publication.rejected"
    PUBLICATION_WITHDRAWN = "publication.withdrawn"
    PUBLICATION_REFUNDED = "publication.refunded"
    PUBLICATION_MANUAL_REQUIRED = "publication.manual_required"
    MANAGED_CAMPAIGN_REFUNDED = "managed_campaign.refunded"
    MANAGED_CAMPAIGN_MANUAL_REQUIRED = "managed_campaign.manual_required"
    ASSET_COMPLETED = "asset.completed"
    ASSET_FAILED = "asset.failed"
    REPORT_COMPLETED = "report.completed"
    REPORT_FAILED = "report.failed"
    AGREEMENT_ACCEPTED = "account.agreement_accepted"
    PASSWORD_CHANGED = "account.password_changed"
    PHONE_CHANGED = "account.phone_changed"
    IDENTITY_CHANGED = "account.identity_changed"
    PERMISSIONS_CHANGED = "account.permissions_changed"
    TRIAL_REVIEW_REQUIRED = "account.trial_review_required"
    TRIAL_ACTIVATED = "account.trial_activated"
    TRIAL_REJECTED = "account.trial_rejected"
    TRIAL_EXPIRED = "account.trial_expired"
    SUBSCRIPTION_RENEWAL_FAILED = "account.subscription_renewal_failed"
    ACCOUNT_REGISTERED = "account.registered"
    BUSINESS_ACTION_REQUIRED = "business.action_required"
    # [#178 P0 · 2026-09-12] 「客户选中的问法全部不可交付」必须有**自己的**事件:
    #   曾想复用 BUSINESS_ACTION_REQUIRED(标题「有新的业务事项待处理」· level=LIGHT)——
    #   那条标题说不出「客户卡住了、要你补词」,而这正是本单唯一要传达的动作;
    #   同一坑 R6 在 MONITORING_ENABLED_BY_OTHER 上记过:通知的价值全在标题。
    SELECTION_NO_DELIVERABLE_KEYWORDS = "selection.no_deliverable_keywords"
    PAYMENT_REMINDER = "business.payment_reminder"
    PAYMENT_RECEIVED = "business.payment_received"
    SERVICE_ACTIVATED = "business.service_activated"
    RENEWAL_REQUESTED = "business.renewal_requested"
    SERVICE_EXPIRING = "business.service_expiring"
    ASSET_REVIEW_CONFIRMED = "asset.review_confirmed"
    ASSET_FEEDBACK_RECEIVED = "asset.feedback_received"
    REFERRAL_REGISTERED = "referral.registered"
    REFERRAL_REWARD_REVERSED = "referral.reward_reversed"
    PUBLICATION_FIRST_RECORDED = "publication.first_recorded"
    SYSTEM_JOB_FAILED = "system.job_failed"
    EXTERNAL_CHANNEL_FAILED = "system.external_channel_failed"
    EXTERNAL_CHANNEL_RECOVERED = "system.external_channel_recovered"
    FUND_RECOVERY_MANUAL_REQUIRED = "system.fund_recovery_manual_required"
    NOTIFICATION_DISPATCH_FAILED = "system.notification_dispatch_failed"


@dataclass(frozen=True)
class Presentation:
    title: str
    route: str


@dataclass(frozen=True)
class NotificationEventSpec:
    event_type: NotificationEventType
    level: NotificationLevel
    presentations: Mapping[RecipientKind, Presentation]
    content_template: str
    required_fields: FrozenSet[str]
    allowed_fields: FrozenSet[str]
    privacy_policy: str


@dataclass(frozen=True)
class RefundNotificationContext:
    """Strong input accepted by transactional point-refund paths."""

    event_type: NotificationEventType
    business_id: str
    terminal_state: str
    recipient_kind: RecipientKind
    business_no: str
    status: str
    summary: str = ""

    def __post_init__(self) -> None:
        allowed = {
            NotificationEventType.PUBLICATION_REFUNDED,
            NotificationEventType.MANAGED_CAMPAIGN_REFUNDED,
        }
        if self.event_type not in allowed:
            raise ValueError(f"event {self.event_type.value} is not a point-refund notification")
        for field_name in ("business_id", "terminal_state", "business_no", "status"):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"refund notification {field_name} is required")


_COMMON = frozenset({
    "business_no", "status", "occurred_at", "amount", "points", "reason", "summary",
    "fee_amount", "net_amount",
})
_FINANCE_REQUIRED = frozenset({"business_no", "status", "occurred_at"})
_TASK_REQUIRED = frozenset({"business_no", "status", "occurred_at"})


def _p(title: str, route: str) -> Presentation:
    return Presentation(title=title, route=route)


def _spec(
    event_type: NotificationEventType,
    level: NotificationLevel,
    presentations: Mapping[RecipientKind, Presentation],
    content_template: str,
    required: FrozenSet[str] = _TASK_REQUIRED,
    allowed: FrozenSet[str] = _COMMON,
    privacy: str = "no_sensitive_identity_or_internal_state",
) -> NotificationEventSpec:
    return NotificationEventSpec(event_type, level, presentations, content_template, required, allowed, privacy)


CATALOG: Dict[NotificationEventType, NotificationEventSpec] = {
    NotificationEventType.RECHARGE_CREDITED: _spec(
        NotificationEventType.RECHARGE_CREDITED, NotificationLevel.IMPORTANT,
        {RecipientKind.CUSTOMER: _p("充值已到账", "/customer/wallet")},
        "业务单号 {business_no}；金额 {amount}；到账算力 {points}；最终状态：{status}；时间：{occurred_at}。",
        _FINANCE_REQUIRED | {"amount", "points"},
    ),
    NotificationEventType.RECHARGE_REVIEW_REQUIRED: _spec(
        NotificationEventType.RECHARGE_REVIEW_REQUIRED, NotificationLevel.IMPORTANT,
        {
            RecipientKind.CUSTOMER: _p("充值正在人工核验", "/customer/wallet"),
            RecipientKind.ADMIN: _p("有充值需要人工核验", "/admin/finance"),
        },
        "业务单号 {business_no}；金额 {amount}；最终状态：{status}；时间：{occurred_at}。平台正在核验，请勿重复支付。",
        _FINANCE_REQUIRED | {"amount"},
    ),
    NotificationEventType.AGENT_INVENTORY_CREDITED: _spec(
        NotificationEventType.AGENT_INVENTORY_CREDITED, NotificationLevel.IMPORTANT,
        {RecipientKind.AGENT: _p("进货算力已到账", "/agent/inventory")},
        "业务单号 {business_no}；金额 {amount}；到账算力 {points}；最终状态：{status}；时间：{occurred_at}。",
        _FINANCE_REQUIRED | {"amount", "points"},
    ),
    NotificationEventType.INVENTORY_ALLOCATION_COMPLETED: _spec(
        NotificationEventType.INVENTORY_ALLOCATION_COMPLETED, NotificationLevel.IMPORTANT,
        {
            RecipientKind.AGENT: _p("库存划拨已完成", "/agent/inventory"),
            RecipientKind.CUSTOMER: _p("算力已到账", "/customer/wallet"),
        },
        "业务单号 {business_no}；算力 {points}；最终状态：{status}；时间：{occurred_at}。",
        _FINANCE_REQUIRED | {"points"},
    ),
    NotificationEventType.RESELL_COMPLETED: _spec(
        NotificationEventType.RESELL_COMPLETED, NotificationLevel.IMPORTANT,
        {
            RecipientKind.TRADE_BUYER: _p("进货交易已完成", "/agent/inventory"),
            RecipientKind.TRADE_SELLER: _p("库存交易已完成", "/agent/inventory"),
        },
        "业务单号 {business_no}；交易算力 {points}；最终状态：{status}；时间：{occurred_at}。",
        _FINANCE_REQUIRED | {"points"},
        privacy="current_trade_parties_only_no_chain_or_margin",
    ),
    NotificationEventType.REFUND_COMPLETED: _spec(
        NotificationEventType.REFUND_COMPLETED, NotificationLevel.IMPORTANT,
        {
            RecipientKind.CUSTOMER: _p("退款已完成", "/customer/wallet"),
            RecipientKind.AGENT: _p("退款已完成", "/agent/inventory"),
            RecipientKind.TRADE_BUYER: _p("进货退款已完成", "/agent/inventory"),
            RecipientKind.TRADE_SELLER: _p("库存退款已完成", "/agent/inventory"),
        },
        "业务单号 {business_no}；退款金额 {amount}；最终状态：{status}；时间：{occurred_at}。",
        _FINANCE_REQUIRED | {"amount"},
    ),
    NotificationEventType.REFUND_FAILED: _spec(
        NotificationEventType.REFUND_FAILED, NotificationLevel.IMPORTANT,
        {
            RecipientKind.CUSTOMER: _p("退款处理未完成", "/customer/wallet"),
            RecipientKind.AGENT: _p("退款处理未完成", "/agent/inventory"),
            RecipientKind.TRADE_BUYER: _p("退款处理未完成", "/agent/inventory"),
            RecipientKind.TRADE_SELLER: _p("退款处理未完成", "/agent/inventory"),
            RecipientKind.ADMIN: _p("退款处理失败", "/admin/refunds"),
        },
        "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。{reason}",
        _FINANCE_REQUIRED | {"reason"},
    ),
    NotificationEventType.REFUND_MANUAL_REQUIRED: _spec(
        NotificationEventType.REFUND_MANUAL_REQUIRED, NotificationLevel.IMPORTANT,
        {
            RecipientKind.CUSTOMER: _p("退款正在人工处理", "/customer/wallet"),
            RecipientKind.AGENT: _p("退款正在人工处理", "/agent/inventory"),
            RecipientKind.TRADE_BUYER: _p("退款正在人工处理", "/agent/inventory"),
            RecipientKind.TRADE_SELLER: _p("退款正在人工处理", "/agent/inventory"),
            RecipientKind.ADMIN: _p("退款需要人工处理", "/admin/refunds"),
        },
        "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。请等待平台核验。",
    ),
    NotificationEventType.DISPUTE_FROZEN: _spec(
        NotificationEventType.DISPUTE_FROZEN, NotificationLevel.IMPORTANT,
        {RecipientKind.CUSTOMER: _p("订单正在人工核验", "/customer/wallet"), RecipientKind.ADMIN: _p("有订单进入争议核验", "/admin/binding-disputes")},
        "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。核验完成前相关额度不可使用。",
    ),
    NotificationEventType.DISPUTE_RESOLVED: _spec(
        NotificationEventType.DISPUTE_RESOLVED, NotificationLevel.IMPORTANT,
        {RecipientKind.CUSTOMER: _p("订单核验已完成", "/customer/wallet"), RecipientKind.ADMIN: _p("订单争议已处理", "/admin/binding-disputes")},
        "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。",
    ),
    NotificationEventType.AGENT_SETTLEMENT_SUBMITTED: _spec(
        NotificationEventType.AGENT_SETTLEMENT_SUBMITTED, NotificationLevel.IMPORTANT,
        {RecipientKind.AGENT: _p("提现申请已提交", "/agent/settlement"), RecipientKind.ADMIN: _p("有新的提现申请待审核", "/admin/settlements")},
        "业务单号 {business_no}；申请金额 {amount}；最终状态：{status}；时间：{occurred_at}。",
        _FINANCE_REQUIRED | {"amount"},
    ),
    NotificationEventType.AGENT_SETTLEMENT_APPROVED: _spec(
        NotificationEventType.AGENT_SETTLEMENT_APPROVED, NotificationLevel.IMPORTANT,
        {RecipientKind.AGENT: _p("提现申请审核通过，等待打款", "/agent/settlement")},
        "业务单号 {business_no}；申请金额 {amount}；最终状态：{status}；时间：{occurred_at}。",
        _FINANCE_REQUIRED | {"amount"},
    ),
    NotificationEventType.AGENT_SETTLEMENT_REJECTED: _spec(
        NotificationEventType.AGENT_SETTLEMENT_REJECTED, NotificationLevel.IMPORTANT,
        {RecipientKind.AGENT: _p("提现申请未通过", "/agent/settlement")},
        "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。处理说明：{reason}",
        _FINANCE_REQUIRED | {"reason"},
    ),
    NotificationEventType.AGENT_SETTLEMENT_PAID: _spec(
        NotificationEventType.AGENT_SETTLEMENT_PAID, NotificationLevel.IMPORTANT,
        {RecipientKind.AGENT: _p("提现已打款", "/agent/settlement")},
        "业务单号 {business_no}；申请金额 {amount}；扣费 {fee_amount}；实际到账 {net_amount}；最终状态：{status}；时间：{occurred_at}。",
        _FINANCE_REQUIRED | {"amount", "fee_amount", "net_amount"},
    ),
}


def _add_task_specs() -> None:
    definitions = (
        (NotificationEventType.DIAGNOSIS_COMPLETED, "品牌体检已完成", "/history", NotificationLevel.GENTLE),
        (NotificationEventType.DIAGNOSIS_PARTIAL, "品牌体检部分完成", "/history", NotificationLevel.GENTLE),
        (NotificationEventType.DIAGNOSIS_FAILED, "品牌体检未完成", "/history", NotificationLevel.GENTLE),
        (NotificationEventType.DIAGNOSIS_CANCELLED, "品牌体检已取消", "/history", NotificationLevel.GENTLE),
        (NotificationEventType.DIAGNOSIS_REFUNDED, "品牌体检费用已退回", "/wallet", NotificationLevel.IMPORTANT),
        (NotificationEventType.DIAGNOSIS_MANUAL_REQUIRED, "品牌体检需要人工处理", "/admin/diagnosis-fund-exceptions", NotificationLevel.IMPORTANT),
        (NotificationEventType.GEO_PLAN_COMPLETED, "方案已生成", "/pricing", NotificationLevel.GENTLE),
        (NotificationEventType.GEO_PLAN_FAILED, "方案生成未完成", "/pricing", NotificationLevel.GENTLE),
        (NotificationEventType.GEO_PLAN_CANCELLED, "方案任务已取消", "/pricing", NotificationLevel.GENTLE),
        (NotificationEventType.GEO_PLAN_REFUNDED, "方案任务未完成，费用已退回", "/pricing", NotificationLevel.IMPORTANT),
        (NotificationEventType.GEO_PLAN_MANUAL_REQUIRED, "方案任务需要平台处理", "/pricing", NotificationLevel.IMPORTANT),
        (NotificationEventType.MONITORING_COMPLETED, "效果监测已完成", "/monitoring", NotificationLevel.GENTLE),
        (NotificationEventType.MONITORING_PARTIAL, "效果监测部分完成", "/monitoring", NotificationLevel.GENTLE),
        (NotificationEventType.MONITORING_FAILED, "效果监测未完成", "/monitoring", NotificationLevel.GENTLE),
        (NotificationEventType.MONITORING_PAUSED, "效果监测已暂停", "/monitoring", NotificationLevel.IMPORTANT),
        (NotificationEventType.MONITORING_ENABLED_BY_OTHER, "有人为你的账户开通了效果监测", "/monitoring", NotificationLevel.IMPORTANT),
        (NotificationEventType.WRITING_COMPLETED, "文章任务已完成", "/writing", NotificationLevel.GENTLE),
        (NotificationEventType.WRITING_PARTIAL, "文章任务部分完成", "/writing", NotificationLevel.GENTLE),
        (NotificationEventType.WRITING_FAILED, "文章任务未完成", "/writing", NotificationLevel.GENTLE),
        (NotificationEventType.WRITING_CANCELLED, "文章任务已取消", "/writing", NotificationLevel.GENTLE),
        (NotificationEventType.RESEARCH_COMPLETED, "调研任务已完成", "/geo-research", NotificationLevel.GENTLE),
        (NotificationEventType.RESEARCH_PARTIAL, "调研任务部分完成", "/geo-research", NotificationLevel.GENTLE),
        (NotificationEventType.RESEARCH_FAILED, "调研任务未完成", "/geo-research", NotificationLevel.GENTLE),
        (NotificationEventType.RESEARCH_CANCELLED, "调研任务已取消", "/geo-research", NotificationLevel.GENTLE),
        (NotificationEventType.RESEARCH_REFUNDED, "调研任务未完成，费用已退回", "/geo-research", NotificationLevel.IMPORTANT),
        (NotificationEventType.RESEARCH_MANUAL_REQUIRED, "调研任务需要平台处理", "/geo-research", NotificationLevel.IMPORTANT),
        (NotificationEventType.PUBLICATION_COMPLETED, "发布任务已完成", "/publish", NotificationLevel.GENTLE),
        (NotificationEventType.PUBLICATION_PARTIAL, "发布任务部分完成", "/publish", NotificationLevel.GENTLE),
        (NotificationEventType.PUBLICATION_REJECTED, "稿件未通过", "/publish", NotificationLevel.GENTLE),
        (NotificationEventType.PUBLICATION_WITHDRAWN, "稿件已撤回", "/publish", NotificationLevel.GENTLE),
        (NotificationEventType.PUBLICATION_REFUNDED, "发布费用已退回", "/wallet", NotificationLevel.IMPORTANT),
        (NotificationEventType.PUBLICATION_MANUAL_REQUIRED, "发布任务需要人工同步", "/publish", NotificationLevel.IMPORTANT),
        (NotificationEventType.ASSET_COMPLETED, "营销物料已生成", "/marketing-materials", NotificationLevel.GENTLE),
        (NotificationEventType.ASSET_FAILED, "营销物料生成未完成", "/marketing-materials", NotificationLevel.GENTLE),
        (NotificationEventType.REPORT_COMPLETED, "报告已生成", "/reports", NotificationLevel.GENTLE),
        (NotificationEventType.REPORT_FAILED, "报告生成未完成", "/reports", NotificationLevel.GENTLE),
    )
    for event_type, title, route, level in definitions:
        CATALOG[event_type] = _spec(
            event_type, level,
            {RecipientKind.USER: _p(title, route), RecipientKind.AGENT: _p(title, route), RecipientKind.ADMIN: _p(title, route)},
            "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。{summary}",
        )


_add_task_specs()

# [防御型 GEO WP2 · 2026-08-21] 诊断"已开始"是**非终态**事实,所以它:
#   1. 不走 `_add_task_specs` —— 那批共用的正文模板写死「最终状态:{status}」,
#      对一个刚开始的任务说"最终状态"是自相矛盾的;这里用「当前状态」。
#   2. **不进** `TERMINAL_SUPERSEDE_GROUPS['diagnosis']` —— 那个组的语义是
#      "同组终态互相覆盖"。把非终态塞进去,会让后到的"已开始"把真正的
#      "已完成/已退款"盖掉,用户最后只看得见一条"开始了"。
#      判据 `test_confirmed_event_is_not_in_the_terminal_supersede_group` 钉住这条。
CATALOG[NotificationEventType.DIAGNOSIS_CONFIRMED] = _spec(
    NotificationEventType.DIAGNOSIS_CONFIRMED,
    NotificationLevel.GENTLE,
    {
        RecipientKind.USER: _p("品牌体检已开始", "/history"),
        RecipientKind.AGENT: _p("品牌体检已开始", "/history"),
        RecipientKind.ADMIN: _p("品牌体检已开始", "/history"),
    },
    "业务单号 {business_no}；当前状态：{status}；时间：{occurred_at}。{summary}",
)

# Manual diagnosis needs different user/admin destinations; never send an
# administrator route to an ordinary account.
CATALOG[NotificationEventType.DIAGNOSIS_MANUAL_REQUIRED] = _spec(
    NotificationEventType.DIAGNOSIS_MANUAL_REQUIRED,
    NotificationLevel.IMPORTANT,
    {
        RecipientKind.USER: _p("品牌体检需要平台处理", "/history"),
        RecipientKind.ADMIN: _p("品牌体检需要人工处理", "/admin/diagnosis-fund-exceptions"),
    },
    "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。{summary}",
)

CATALOG[NotificationEventType.GEO_PLAN_MANUAL_REQUIRED] = _spec(
    NotificationEventType.GEO_PLAN_MANUAL_REQUIRED,
    NotificationLevel.IMPORTANT,
    {
        RecipientKind.USER: _p("方案任务需要平台处理", "/pricing"),
        RecipientKind.ADMIN: _p("方案任务需要人工处理", "/admin/diagnosis-fund-exceptions"),
    },
    "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。{summary}",
)

CATALOG[NotificationEventType.MONITORING_FAILED] = _spec(
    NotificationEventType.MONITORING_FAILED,
    NotificationLevel.GENTLE,
    {
        RecipientKind.USER: _p("效果监测未完成", "/monitoring"),
        RecipientKind.ADMIN: _p("效果监测任务失败", "/admin/ai-ops"),
    },
    "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。{summary}",
)

CATALOG[NotificationEventType.PUBLICATION_REFUNDED] = _spec(
    NotificationEventType.PUBLICATION_REFUNDED,
    NotificationLevel.IMPORTANT,
    {RecipientKind.USER: _p("发布费用已退回", "/publish")},
    "业务单号 {business_no}；退回算力 {points}；最终状态：{status}；时间：{occurred_at}。",
    _FINANCE_REQUIRED | {"points"},
)

CATALOG[NotificationEventType.PUBLICATION_MANUAL_REQUIRED] = _spec(
    NotificationEventType.PUBLICATION_MANUAL_REQUIRED,
    NotificationLevel.IMPORTANT,
    {
        RecipientKind.USER: _p("发布任务需要平台处理", "/publish"),
        RecipientKind.ADMIN: _p("发布任务需要人工处理", "/admin/ai-ops"),
    },
    "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。{summary}",
)

CATALOG[NotificationEventType.MANAGED_CAMPAIGN_REFUNDED] = _spec(
    NotificationEventType.MANAGED_CAMPAIGN_REFUNDED,
    NotificationLevel.IMPORTANT,
    {RecipientKind.USER: _p("托管任务费用已退回", "/managed")},
    "业务单号 {business_no}；退回算力 {points}；最终状态：{status}；时间：{occurred_at}。",
    _FINANCE_REQUIRED | {"points"},
)

CATALOG[NotificationEventType.MANAGED_CAMPAIGN_MANUAL_REQUIRED] = _spec(
    NotificationEventType.MANAGED_CAMPAIGN_MANUAL_REQUIRED,
    NotificationLevel.IMPORTANT,
    {
        RecipientKind.USER: _p("托管任务需要平台处理", "/managed"),
        RecipientKind.ADMIN: _p("托管任务退款需要人工处理", "/admin/managed"),
    },
    "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。{summary}",
)

for _event, _title, _route, _level, _recipients in (
    (NotificationEventType.AGREEMENT_ACCEPTED, "协议已确认", "/account/profile", NotificationLevel.IMPORTANT, (RecipientKind.USER, RecipientKind.AGENT)),
    (NotificationEventType.PASSWORD_CHANGED, "密码已修改", "/account/profile", NotificationLevel.IMPORTANT, (RecipientKind.USER, RecipientKind.AGENT)),
    (NotificationEventType.PHONE_CHANGED, "手机号已修改", "/account/profile", NotificationLevel.IMPORTANT, (RecipientKind.USER, RecipientKind.AGENT)),
    (NotificationEventType.IDENTITY_CHANGED, "账号身份已变更", "/account/profile", NotificationLevel.IMPORTANT, (RecipientKind.USER, RecipientKind.AGENT)),
    (NotificationEventType.PERMISSIONS_CHANGED, "账号权限已变更", "/account/profile", NotificationLevel.IMPORTANT, (RecipientKind.USER, RecipientKind.AGENT)),
    (NotificationEventType.TRIAL_REVIEW_REQUIRED, "有新的工作台试用申请待审批", "/agent/trial-pass-review", NotificationLevel.IMPORTANT, (RecipientKind.AGENT,)),
    (NotificationEventType.TRIAL_ACTIVATED, "工作台试用已开通", "/dashboard", NotificationLevel.IMPORTANT, (RecipientKind.USER,)),
    (NotificationEventType.TRIAL_REJECTED, "工作台试用申请未通过", "/dashboard", NotificationLevel.IMPORTANT, (RecipientKind.USER,)),
    (NotificationEventType.TRIAL_EXPIRED, "工作台试用已结束", "/dashboard", NotificationLevel.IMPORTANT, (RecipientKind.USER,)),
    (NotificationEventType.SUBSCRIPTION_RENEWAL_FAILED, "自动续费未完成", "/subscription/manage", NotificationLevel.IMPORTANT, (RecipientKind.USER,)),
    (NotificationEventType.BUSINESS_ACTION_REQUIRED, "有新的业务事项待处理", "/dashboard/today", NotificationLevel.LIGHT, (RecipientKind.AGENT,)),
    # [#178] route 必须是 App.tsx 里真声明过的路径 —— test_every_catalog_route_exists_in_active_app_router
    #   会对整张 CATALOG 做差集。"/online-quote" = 报价方的选词/报价台(OnlineQuoteFlow),
    #   即「去补充商业选型问题」的落点。
    #   🔴 不下发深链 token:_FORBIDDEN_WORDS 里就有 "token",带 token 的 content 会被
    #      render_notification 直接判违规抛错;而且 GEO-R1-CAN-133 明令不落库明文 selection token。
    #      定位靠既有约定 business_no=QUOTE-<id>。
    (NotificationEventType.SELECTION_NO_DELIVERABLE_KEYWORDS, "客户暂无可交付问法 · 待补充选型问题", "/online-quote", NotificationLevel.IMPORTANT, (RecipientKind.AGENT,)),
    (NotificationEventType.PAYMENT_REMINDER, "有订单等待收款确认", "/pricing", NotificationLevel.LIGHT, (RecipientKind.AGENT,)),
    (NotificationEventType.PAYMENT_RECEIVED, "订单已确认收款", "/writing", NotificationLevel.IMPORTANT, (RecipientKind.AGENT,)),
    (NotificationEventType.SERVICE_ACTIVATED, "客户服务已激活", "/writing", NotificationLevel.LIGHT, (RecipientKind.AGENT,)),
    (NotificationEventType.RENEWAL_REQUESTED, "收到客户续费申请", "/pricing", NotificationLevel.IMPORTANT, (RecipientKind.AGENT,)),
    (NotificationEventType.SERVICE_EXPIRING, "客户服务即将到期", "/pricing", NotificationLevel.LIGHT, (RecipientKind.AGENT,)),
    (NotificationEventType.ASSET_REVIEW_CONFIRMED, "营销资料已确认", "/marketing-materials", NotificationLevel.GENTLE, (RecipientKind.AGENT,)),
    (NotificationEventType.ASSET_FEEDBACK_RECEIVED, "收到营销资料修改意见", "/marketing-materials", NotificationLevel.GENTLE, (RecipientKind.AGENT,)),
    (NotificationEventType.ACCOUNT_REGISTERED, "有新用户完成注册", "/admin/users", NotificationLevel.LIGHT, (RecipientKind.ADMIN,)),
    (NotificationEventType.REFERRAL_REGISTERED, "有新用户通过推荐注册", "/referral", NotificationLevel.LIGHT, (RecipientKind.USER,)),
    (NotificationEventType.REFERRAL_REWARD_REVERSED, "推荐奖励已取消", "/referral", NotificationLevel.IMPORTANT, (RecipientKind.USER,)),
    (NotificationEventType.PUBLICATION_FIRST_RECORDED, "首次发布已记录", "/monitoring", NotificationLevel.GENTLE, (RecipientKind.AGENT,)),
    (NotificationEventType.SYSTEM_JOB_FAILED, "后台任务需要处理", "/admin/ai-ops", NotificationLevel.IMPORTANT, (RecipientKind.ADMIN,)),
    (NotificationEventType.EXTERNAL_CHANNEL_FAILED, "外部通道任务需要处理", "/admin/ai-ops", NotificationLevel.IMPORTANT, (RecipientKind.ADMIN,)),
    (NotificationEventType.EXTERNAL_CHANNEL_RECOVERED, "外部通道已恢复", "/admin/ai-ops", NotificationLevel.GENTLE, (RecipientKind.ADMIN,)),
    (NotificationEventType.FUND_RECOVERY_MANUAL_REQUIRED, "资金恢复任务需要人工处理", "/admin/finance", NotificationLevel.IMPORTANT, (RecipientKind.ADMIN,)),
    (NotificationEventType.NOTIFICATION_DISPATCH_FAILED, "通知派发需要人工处理", "/admin/ai-ops", NotificationLevel.IMPORTANT, (RecipientKind.ADMIN,)),
):
    CATALOG[_event] = _spec(
        _event, _level, {r: _p(_title, _route) for r in _recipients},
        "业务单号 {business_no}；最终状态：{status}；时间：{occurred_at}。{summary}",
    )

CATALOG[NotificationEventType.REFERRAL_REWARD_REVERSED] = _spec(
    NotificationEventType.REFERRAL_REWARD_REVERSED,
    NotificationLevel.IMPORTANT,
    {RecipientKind.USER: _p("推荐奖励已取消", "/referral")},
    "业务单号 {business_no}；取消金额 {amount}；最终状态：{status}；时间：{occurred_at}。{summary}",
    _FINANCE_REQUIRED | {"amount"},
)

CATALOG[NotificationEventType.PAYMENT_RECEIVED] = _spec(
    NotificationEventType.PAYMENT_RECEIVED,
    NotificationLevel.IMPORTANT,
    {RecipientKind.AGENT: _p("订单已确认收款", "/writing")},
    "业务单号 {business_no}；订单金额 {amount}；最终状态：{status}；时间：{occurred_at}。{summary}",
    _FINANCE_REQUIRED | {"amount"},
)

CATALOG[NotificationEventType.SERVICE_ACTIVATED] = _spec(
    NotificationEventType.SERVICE_ACTIVATED,
    NotificationLevel.IMPORTANT,
    {RecipientKind.AGENT: _p("客户服务已激活", "/writing")},
    "业务单号 {business_no}；订单金额 {amount}；最终状态：{status}；时间：{occurred_at}。{summary}",
    _FINANCE_REQUIRED | {"amount"},
)


# ---------------------------------------------------------------------------
# [工单 2026-07-29 T3] 通知分类 + 同业务单号终态去重
# ---------------------------------------------------------------------------


class NotificationCategory(str, Enum):
    """通知中心两类分组(Owner 指定)。

    SYSTEM  = 产品更新 / 公告 / 业务状态(如"效果监测已完成")→ 跳对应业务页
    BILLING = 每笔扣费 / 退费 / 冻结释放 → 跳账单或消费明细页
    """

    SYSTEM = "system"
    BILLING = "billing"


# 资金类事件白名单。**只增不猜**:没列进来的一律算系统消息,
# 免得新加事件因为标题里带个"费"字就被误分到扣费页。
BILLING_EVENT_TYPES: FrozenSet[NotificationEventType] = frozenset({
    NotificationEventType.RECHARGE_CREDITED,
    NotificationEventType.RECHARGE_REVIEW_REQUIRED,
    NotificationEventType.AGENT_INVENTORY_CREDITED,
    NotificationEventType.INVENTORY_ALLOCATION_COMPLETED,
    NotificationEventType.RESELL_COMPLETED,
    NotificationEventType.REFUND_COMPLETED,
    NotificationEventType.REFUND_FAILED,
    NotificationEventType.REFUND_MANUAL_REQUIRED,
    NotificationEventType.DISPUTE_FROZEN,
    NotificationEventType.DISPUTE_RESOLVED,
    NotificationEventType.AGENT_SETTLEMENT_SUBMITTED,
    NotificationEventType.AGENT_SETTLEMENT_APPROVED,
    NotificationEventType.AGENT_SETTLEMENT_REJECTED,
    NotificationEventType.AGENT_SETTLEMENT_PAID,
    NotificationEventType.DIAGNOSIS_REFUNDED,
    NotificationEventType.GEO_PLAN_REFUNDED,
    NotificationEventType.RESEARCH_REFUNDED,
    NotificationEventType.PUBLICATION_REFUNDED,
    NotificationEventType.MANAGED_CAMPAIGN_REFUNDED,
    NotificationEventType.REFERRAL_REWARD_REVERSED,
})


# 同一业务单号的**互斥终态**分组。同组内后到的终态覆盖先到的(不并列展示)。
#
# 🔴 刻意不收进来的:
#   · *_REFUNDED / *_MANUAL_REQUIRED —— 资金与人工处理事实,任何情况下都不许被后续
#     终态盖掉(盖掉 = 客户看不到退款/待人工,比看到两条矛盾更糟);
#   · MONITORING_PAUSED —— 暂停不是"完成/失败"的同类项,是另一个事实。
TERMINAL_SUPERSEDE_GROUPS: Dict[str, FrozenSet[NotificationEventType]] = {
    "diagnosis": frozenset({
        NotificationEventType.DIAGNOSIS_COMPLETED,
        NotificationEventType.DIAGNOSIS_PARTIAL,
        NotificationEventType.DIAGNOSIS_FAILED,
        NotificationEventType.DIAGNOSIS_CANCELLED,
    }),
    "geo_plan": frozenset({
        NotificationEventType.GEO_PLAN_COMPLETED,
        NotificationEventType.GEO_PLAN_FAILED,
        NotificationEventType.GEO_PLAN_CANCELLED,
    }),
    "monitoring": frozenset({
        NotificationEventType.MONITORING_COMPLETED,
        NotificationEventType.MONITORING_PARTIAL,
        NotificationEventType.MONITORING_FAILED,
    }),
    "writing": frozenset({
        NotificationEventType.WRITING_COMPLETED,
        NotificationEventType.WRITING_PARTIAL,
        NotificationEventType.WRITING_FAILED,
        NotificationEventType.WRITING_CANCELLED,
    }),
    "research": frozenset({
        NotificationEventType.RESEARCH_COMPLETED,
        NotificationEventType.RESEARCH_PARTIAL,
        NotificationEventType.RESEARCH_FAILED,
        NotificationEventType.RESEARCH_CANCELLED,
    }),
    "publication": frozenset({
        NotificationEventType.PUBLICATION_COMPLETED,
        NotificationEventType.PUBLICATION_PARTIAL,
        NotificationEventType.PUBLICATION_REJECTED,
        NotificationEventType.PUBLICATION_WITHDRAWN,
    }),
    "report": frozenset({
        NotificationEventType.REPORT_COMPLETED,
        NotificationEventType.REPORT_FAILED,
    }),
    "asset": frozenset({
        NotificationEventType.ASSET_COMPLETED,
        NotificationEventType.ASSET_FAILED,
    }),
}


def event_category(event_type: Any) -> NotificationCategory:
    """事件 → 两类分组之一。未知事件一律 SYSTEM(fail-safe:不误报成扣费)。"""
    try:
        event = NotificationEventType(event_type)
    except (ValueError, TypeError):
        return NotificationCategory.SYSTEM
    return (
        NotificationCategory.BILLING if event in BILLING_EVENT_TYPES
        else NotificationCategory.SYSTEM
    )


def terminal_supersede_pairs() -> tuple:
    """展开成 (event_type_value, group_name) 二元组,供 SQL 侧 unnest 成映射表。"""
    return tuple(
        (event.value, group)
        for group, events in TERMINAL_SUPERSEDE_GROUPS.items()
        for event in sorted(events, key=lambda item: item.value)
    )


_REDACT_LONG_NUMBER = re.compile(r"(?<!\d)\d{6,}(?!\d)")
_FORBIDDEN_WORDS = re.compile(
    r"(?i)(银行卡号|卡号|token|secret|密钥|freeze_id|表名|上游|下级|成本|倍率|差价|relationship)"
)


def safe_reason(value: Any) -> str:
    text = " ".join(str(value or "").split())[:160]
    text = _REDACT_LONG_NUMBER.sub("[已隐藏]", text)
    text = _FORBIDDEN_WORDS.sub("相关敏感信息", text)
    return text or "请在对应页面查看处理建议。"


def render_notification(
    event_type: NotificationEventType | str,
    recipient_kind: RecipientKind | str,
    facts: Mapping[str, Any],
) -> Dict[str, Any]:
    event = NotificationEventType(event_type)
    recipient = RecipientKind(recipient_kind)
    spec = CATALOG[event]
    presentation = spec.presentations.get(recipient)
    if presentation is None:
        raise ValueError(f"recipient {recipient.value} is not allowed for {event.value}")
    unknown = set(facts).difference(spec.allowed_fields)
    missing = set(spec.required_fields).difference(facts)
    if unknown or missing:
        raise ValueError(f"invalid notification facts unknown={sorted(unknown)} missing={sorted(missing)}")
    values = {key: str(value if value is not None else "") for key, value in facts.items()}
    values["reason"] = safe_reason(values.get("reason"))
    values["summary"] = safe_reason(values.get("summary")) if values.get("summary") else ""
    content = spec.content_template.format(**values).strip()
    if _FORBIDDEN_WORDS.search(content):
        raise ValueError("notification content violates privacy policy")
    return {
        "event_type": event.value,
        "recipient_kind": recipient.value,
        "level": spec.level.value,
        "title": presentation.title,
        "content": content,
        "route": presentation.route,
        "privacy_policy": spec.privacy_policy,
        "payload": dict(values),
    }


def catalog_routes() -> FrozenSet[str]:
    return frozenset(p.route for spec in CATALOG.values() for p in spec.presentations.values())


def publication_refund_context(business_id: str) -> RefundNotificationContext:
    """Build a stable public-safe reference without exposing an internal ledger id."""
    identity = str(business_id or "").strip()
    if not identity:
        raise ValueError("publication refund business_id is required")
    public_ref = hashlib.sha256(f"publication-refund:{identity}".encode("utf-8")).hexdigest()[:10].upper()
    return RefundNotificationContext(
        event_type=NotificationEventType.PUBLICATION_REFUNDED,
        business_id=identity,
        terminal_state="refunded",
        recipient_kind=RecipientKind.USER,
        business_no=f"PUB-{public_ref}",
        status="已退回",
        summary="发布费用已按真实退款流水退回。",
    )
