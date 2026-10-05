"""Strong admin-only contracts for user identity and relationship governance.

These DTOs are intentionally isolated from customer and service-provider APIs.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictAdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: 可以**写入**的业务身份。`ChangeBusinessIdentityRequest` 用它 ——
#: 🔴 不许把 "unverified" 加进来:那是「还没核出来」,不是一个可以把人
#:    设过去的目标身份。客户端 POST business_identity="unverified" 必须被拒。
BusinessIdentity = Literal["ordinary_user", "service_provider"]

#: 🔴 [#139 · 2026-09-07] **读**出来的业务身份可以是「未知」。
#:
#:    列表/详情对没有 `user_wallets` 行的用户返回 "unverified"(按行降级)。
#:    这个 Literal 与上面那个**必须分开**:读可以是未知,写不能以未知为目标。
#:    合成一个的话,请求体也会开始接受 "unverified" —— 那是把「查不出来」
#:    变成一个可以主动设置的状态,方向完全相反。
#:
#:    ⚠️ 不加这个值的后果不是「显示不对」,是 **HTTP 500**:
#:    `GET /users` 带 `response_model`,返回值过不了 Literal 校验 ⇒ 整页 500。
#:    也就是说旧现象只是换了个状态码回来。
BusinessIdentityView = Literal["ordinary_user", "service_provider", "unverified"]
GovernanceScope = Literal[
    "business_identity", "commercial_binding", "channel_relationship",
    "platform_access", "password_security", "wallet_adjustment"
]


class AdminActorRef(StrictAdminModel):
    user_id: int
    username: str
    display_name: str
    is_active: bool
    company: Optional[str] = None
    business_identity: BusinessIdentity
    service_code: Optional[str] = None
    channel_code: Optional[str] = None


class GovernanceVersions(StrictAdminModel):
    business_identity: int = 1
    commercial_binding: int = 1
    channel_relationship: int = 1
    platform_access: int = 1
    password_security: int = 1
    wallet_adjustment: int = 1
    account_status: int = 1


class AdminUserListItem(StrictAdminModel):
    user_id: int
    username: str
    display_name: str
    phone: Optional[str] = None
    is_active: bool
    business_identity: BusinessIdentityView
    platform_access: Literal["administrator", "standard"]
    total_points: int
    customer_count: int
    brand_count: int
    service_mode: Literal["service_provider", "platform_direct"]
    # [补充工单 2026-08-06] 账号来源要在**列表行**上也看得出来。
    # 🔴 DTO 是 extra="forbid",不在这里声明 = 列表接口整个 500(不是「字段看不见」)。
    account_origin: Literal["self_signup", "organization_member"] = "self_signup"
    organization_name: Optional[str] = None
    needs_attention: bool
    attention_label: Optional[str] = None
    created_at: Optional[datetime] = None
    last_active_at: Optional[datetime] = None
    versions: GovernanceVersions


class AdminUserListResponse(StrictAdminModel):
    success: bool = True
    users: List[AdminUserListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class UserOverview(StrictAdminModel):
    user_id: int
    username: str
    display_name: str
    phone: Optional[str] = None
    company: Optional[str] = None
    is_active: bool
    business_identity: BusinessIdentityView
    business_identity_label: str
    platform_access: Literal["administrator", "standard"]
    platform_access_label: str
    total_points: int
    paid_points: int
    bonus_points: int
    total_recharged_points: int
    customer_count: int
    brand_count: int
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    last_active_at: Optional[datetime] = None
    versions: GovernanceVersions
    # [P0-C · WO_INVREL_P0_HOTFIX §9] 详情页也要知道这条归属在不在亮灯 ——
    # 「补录凭证」入口的显示条件就是它(列表侧 `UserListItem` 早就有同名字段)。
    # 🔴 与上面 `account_origin` 那条注释同一个道理:本类是 extra="forbid",
    #    服务层往 overview 塞了却不在这里声明 = **详情接口整个 500**,不是"字段看不见"。
    #    2026-08-13 本包第一版就是这么挂的(Deploy NO-GO),同型第二次 ——
    #    判据见 `tests/inventory_relation_model_2026_08_13/test_detail_dto_outlet.py`:
    #    打**真出口**做响应校验,并配反向对照(塞未声明键必须转红)。
    needs_attention: bool = False
    attention_label: Optional[str] = None


class EvidenceSummary(StrictAdminModel):
    status: Literal["complete", "related", "incomplete", "not_required"]
    label: str
    operator_user_id: Optional[int] = None
    operator_name: Optional[str] = None
    reason: Optional[str] = None
    request_id: Optional[str] = None
    happened_at: Optional[datetime] = None


class OrganizationOrigin(StrictAdminModel):
    """账号由哪个团队邀请创建(仅组织操作员账号才有)。"""
    organization_id: Optional[int] = None
    name: Optional[str] = None
    owner: Optional[AdminActorRef] = None
    membership_status: Optional[str] = None
    role_id: Optional[int] = None
    joined_at: Optional[datetime] = None


class RegistrationAttribution(StrictAdminModel):
    present: bool
    inviter: Optional[AdminActorRef] = None
    # 🔴🔴 `organization_invite` 这一项是**必须**的,不是锦上添花:
    #   87c29cdf 让 _relationships() 对组织操作员账号返回 source="organization_invite"
    #   与两个新键(account_origin / organization),但**没同步改这里** ——
    #   StrictAdminModel 是 extra="forbid",于是 #161 / #159(Owner 报的那两个账号)
    #   点开详情页会直接 500,比原来那句误导性的「无邀请记录」更糟。
    #   实测三条校验错:source 的 Literal 不含 organization_invite,
    #   account_origin 与 organization 都是 extra_forbidden。
    #   教训:锁打在 _relationships() 函数层是不够的,**要打在真出口 DTO 上**。
    source: Literal["referral_links", "legacy_user_pointer", "organization_invite", "none"]
    registered_at: Optional[datetime] = None
    legacy_pointer_user_id: Optional[int] = None
    evidence: EvidenceSummary
    account_origin: Literal["self_signup", "organization_member"] = "self_signup"
    organization: Optional[OrganizationOrigin] = None


class CommercialServiceBinding(StrictAdminModel):
    mode: Literal["service_provider", "platform_direct"]
    provider: Optional[AdminActorRef] = None
    binding_id: Optional[int] = None
    binding_source: Optional[str] = None
    binding_source_label: str
    bound_at: Optional[datetime] = None
    relationship_version: int
    dispute_status: Optional[str] = None
    evidence: EvidenceSummary


class ChannelHierarchy(StrictAdminModel):
    mode: Literal["upstream_channel", "platform_root", "not_applicable"]
    upstream: Optional[AdminActorRef] = None
    relationship_version: Optional[str] = None
    cost_multiplier_bps: Optional[int] = None
    effective_from: Optional[datetime] = None
    reason: Optional[str] = None


class RelationshipNotice(StrictAdminModel):
    code: str
    severity: Literal["info", "warning", "critical"]
    title: str
    detail: str


class ServiceRelationships(StrictAdminModel):
    registration: RegistrationAttribution
    commercial: CommercialServiceBinding
    channel: ChannelHierarchy
    dual_relationships_present: bool
    dual_relationships_label: Optional[str] = None
    notices: List[RelationshipNotice] = Field(default_factory=list)


class PricingSettlementSummary(StrictAdminModel):
    customer_pricing_route: str
    procurement_pricing_route: str
    settlement_route: str
    pricing_source: str
    special_pricing_note: Optional[str] = None


class ClientSummary(StrictAdminModel):
    customer_user_id: int
    username: str
    display_name: str
    bound_at: Optional[datetime] = None


class BrandSummary(StrictAdminModel):
    brand_id: int
    name: str
    industry: Optional[str] = None
    status: Optional[str] = None


class ClientsBrandsSummary(StrictAdminModel):
    clients: List[ClientSummary] = Field(default_factory=list)
    brands: List[BrandSummary] = Field(default_factory=list)


class RechargeOrderSummary(StrictAdminModel):
    order_id: str
    amount_yuan: str
    status_label: str
    created_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None


class PointTransactionSummary(StrictAdminModel):
    transaction_id: int
    direction_label: str
    points: int
    description: Optional[str] = None
    created_at: Optional[datetime] = None


class WalletBillingSummary(StrictAdminModel):
    paid_points: int
    bonus_points: int
    total_points: int
    total_recharged_points: int
    recent_orders: List[RechargeOrderSummary] = Field(default_factory=list)
    recent_transactions: List[PointTransactionSummary] = Field(default_factory=list)


class LegacyRoleInfo(StrictAdminModel):
    role_id: int
    internal_name: str
    historical_label: str
    compatibility_status: Literal["active_compatibility", "read_only_legacy"]


class PermissionsSecuritySummary(StrictAdminModel):
    platform_access: Literal["administrator", "standard"]
    account_status_label: str
    must_change_password: bool
    permission_version: int
    legacy_roles: List[LegacyRoleInfo] = Field(default_factory=list)


class GovernanceAuditEntry(StrictAdminModel):
    audit_id: int
    scope: GovernanceScope
    action_label: str
    operator_user_id: int
    operator_name: Optional[str] = None
    request_id: str
    reason: str
    before_summary: str
    after_summary: str
    version_before: int
    version_after: int
    created_at: datetime


class AdminUserDetailResponse(StrictAdminModel):
    success: bool = True
    overview: UserOverview
    relationships: ServiceRelationships
    pricing_and_settlement: PricingSettlementSummary
    clients_and_brands: ClientsBrandsSummary
    wallet_and_billing: WalletBillingSummary
    permissions_and_security: PermissionsSecuritySummary
    operation_logs: List[GovernanceAuditEntry] = Field(default_factory=list)


class EmptyRetailCatalogScope(StrictAdminModel):
    """一个"已发布但零条目"的零售目录 scope —— 该服务方名下客户当前买不了任何东西。"""

    version_id: int
    scope_key: str
    version_code: str = ""
    effective_from: Optional[str] = None
    reason: str = ""


class PlatformDirectReadiness(StrictAdminModel):
    configured: bool
    ready: bool
    status: Literal["missing", "invalid", "ready"]
    label: str
    service_user: Optional[AdminActorRef] = None
    checks: List[str] = Field(default_factory=list)
    # 全平台(不只平台直营)当前空掉的零售目录 —— 断供必须在管理端看得见。
    empty_retail_scopes: List[EmptyRetailCatalogScope] = Field(default_factory=list)
    empty_retail_alert: Optional[Dict[str, Any]] = None


class PlatformDirectReadinessResponse(StrictAdminModel):
    success: bool = True
    readiness: PlatformDirectReadiness


class AgreementGateDailyCount(StrictAdminModel):
    day: str
    triggers: int
    users: int


class AgreementGateMetrics(StrictAdminModel):
    """协议门禁把人挡在门外的计数。

    判读口径:正常形态是"触发 → 几秒内补签成功 → 不再触发"。
    `consecutive_days_with_triggers` 连续多天非零 = 有人正卡在门外出不来。
    """

    available: bool
    days: int
    total: int
    distinct_users: int
    daily: List[AgreementGateDailyCount] = Field(default_factory=list)
    consecutive_days_with_triggers: int = 0


class AgreementGateMetricsResponse(StrictAdminModel):
    success: bool = True
    metrics: AgreementGateMetrics


class GovernanceMutationBase(StrictAdminModel):
    expected_version: int = Field(..., ge=1)
    reason: str = Field(..., min_length=2, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_must_be_meaningful(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("必须填写明确的操作原因")
        return value


class ChangeBusinessIdentityRequest(GovernanceMutationBase):
    business_identity: BusinessIdentity


class ChangeCommercialBindingRequest(GovernanceMutationBase):
    provider_user_id: Optional[int] = Field(
        default=None,
        ge=1,
        description="null 表示解除商业绑定并转为平台直营",
    )


class BackfillCommercialBindingAuditRequest(GovernanceMutationBase):
    """补录历史 admin_manual 归属的审计凭证(工单 §P1-5 方案 a)。

    刻意**没有** `provider_user_id` 字段:补录不改变归属,承接方取自当前绑定行。
    多一个字段就意味着多一条改归属的路,那正是本工单禁止的。
    """


class ChangePlatformAccessRequest(GovernanceMutationBase):
    administrator: bool


class ChangeChannelRelationshipRequest(GovernanceMutationBase):
    upstream_user_id: Optional[int] = Field(
        default=None, ge=1, description="null 表示改为平台根渠道"
    )
    cost_multiplier_bps: int = Field(default=10000, ge=10000, le=100000)


class ResetUserPasswordRequest(GovernanceMutationBase):
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_must_not_be_blank(cls, value: str) -> str:
        if value != value.strip() or not value.strip():
            raise ValueError("密码首尾不能包含空格")
        if len(value.encode("utf-8")) > 72:
            raise ValueError("密码 UTF-8 编码后不能超过 72 字节")
        return value


class AdjustUserWalletRequest(GovernanceMutationBase):
    point_type: Literal["paid", "bonus"]
    operation: Literal["add", "deduct", "set"]
    amount: int = Field(..., ge=0, le=1_000_000_000_000_000)

    @field_validator("amount")
    @classmethod
    def delta_operations_require_positive_amount(cls, value: int, info):
        operation = info.data.get("operation")
        if operation in {"add", "deduct"} and value <= 0:
            raise ValueError("增加或扣减的算力必须大于 0")
        return value


class GovernanceStateSnapshot(StrictAdminModel):
    business_identity: Optional[BusinessIdentity] = None
    commercial_provider_user_id: Optional[int] = None
    commercial_mode: Optional[Literal["service_provider", "platform_direct"]] = None
    channel_upstream_user_id: Optional[int] = None
    channel_mode: Optional[Literal["upstream_channel", "platform_root"]] = None
    cost_multiplier_bps: Optional[int] = None
    platform_access: Optional[Literal["administrator", "standard"]] = None
    password_state: Optional[Literal["active", "reset_required"]] = None
    paid_points: Optional[int] = None
    bonus_points: Optional[int] = None


class GovernanceMutationResponse(StrictAdminModel):
    success: bool = True
    scope: GovernanceScope
    version: int
    request_id: str
    before: GovernanceStateSnapshot
    after: GovernanceStateSnapshot
