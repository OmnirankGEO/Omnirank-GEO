"""
V3.5 W2 · pydantic Response DTO 严格分离

铁律(基于 v6 终稿章九 W2 边界 + Codex 7 轮 review 收敛):
- AgentXxxResponse:**只**含代理可见字段
- AdminXxxResponse:含 admin 全字段(包括 raw cost / tax 规则 / gateway/service bps)

禁止字段(Agent 路径):
- platform_cost_cents
- raw_cost / raw_llm_cost / media_raw_cost
- gateway_fee_bps / settlement_service_fee_bps
- tax_rate_bps / tax_mode

允许字段(Agent 路径 · 财务透明):
- wholesale_cents / retail_cents / points_granted
- margin_label / margin_warning / agent_settlement_cents
- tax_withholding_cents(汇总金额 · 不暴露规则)

W2 验收 gate #1-4 守护此隔离 · 严禁在 api/agent_*.py 直接复用 Admin schema 然后手动 pop。

memory feedback_v35_factory_inventory_model_v6
"""

from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from datetime import datetime
from services.agent_inventory_pricing import MAX_AGENT_PURCHASE_AMOUNT_CENTS


# ============================================================
# 代理库存
# ============================================================

class AgentInventoryBalanceResponse(BaseModel):
    """代理库存余额 + 预警级别"""
    paid_inventory_points: int = Field(..., description="paid 库存(全功能)")
    bonus_inventory_points: int = Field(..., description="bonus 库存(禁发布)")
    frozen_inventory_points: int = Field(0, description="冻结库存")
    total_purchased_points: int = Field(0, description="累计进货")
    total_allocated_points: int = Field(0, description="累计划拨给客户")
    alert_level: str = Field("ok", description="ok / warn_30 / warn_10 / empty")


class AgentInventoryTransactionItem(BaseModel):
    id: int
    type: str
    pool: str
    points: int
    balance_paid_after: int
    balance_bonus_after: int
    related_customer_user_id: Optional[int] = None
    related_order_id: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime


class AgentInventoryTransactionsResponse(BaseModel):
    items: List[AgentInventoryTransactionItem]
    total: int


class AgentPurchaseOption(BaseModel):
    """进货档位(预付充值)"""
    option_id: str
    amount_cents: int
    base_points: int
    bonus_points: int = 0
    label: str
    is_first_month_bonus: bool = False
    total_points: int = 0
    reward_description: str = ""
    sort_order: int = 0
    quote_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class AgentPurchaseOptionsResponse(BaseModel):
    catalog_version: str = "agent-purchase-v1"
    options: List[AgentPurchaseOption]


class AgentPurchaseRequest(BaseModel):
    """代理预付充值订单创建"""
    model_config = ConfigDict(extra="forbid")
    amount_cents: int = Field(..., gt=0, le=MAX_AGENT_PURCHASE_AMOUNT_CENTS)
    option_id: Optional[str] = None
    expected_catalog_version: Optional[str] = Field(
        default=None, pattern=r"^agent-purchase-v[1-9][0-9]*$"
    )
    expected_quote_fingerprint: Optional[str] = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    channel: Optional[str] = Field(
        "auto",
        description="auto / wechat_native / wechat_h5 / wechat_jsapi · 默认 auto 按 UA 路由(代理 PC 多 · 通常落 native)"
    )


class AgentPurchaseCreateResponse(BaseModel):
    order_id: str
    amount_cents: int
    base_points: int
    bonus_points: int
    quote_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    actual_channel: str = Field("wechat_native", description="实际下单渠道")
    code_url: Optional[str] = Field(None, description="Native weixin:// URL · 前端自渲二维码")
    payment_url_qrcode: Optional[str] = Field(None, description="兜底二维码 image URL")
    payment_url_mobile: Optional[str] = Field(None, description="H5 跳转 URL")
    needs_openid: bool = Field(False, description="JSAPI 渠道 · 前端需先跳 oauth 拿 openid")


class AgentPurchasePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: int = Field(..., gt=0, le=MAX_AGENT_PURCHASE_AMOUNT_CENTS)
    option_id: Optional[str] = None


class AgentPurchasePreviewResponse(BaseModel):
    catalog_version: str
    option_id: str
    amount_cents: int
    base_points: int
    bonus_points: int
    total_points: int
    reward_description: str
    quote_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    tier_at_order: Optional[str] = None
    tier_bonus_rate_bps: Optional[int] = None
    crosses_tier_threshold: Optional[bool] = None
    projected_rolling_12m_yuan: Optional[str] = None


# ============================================================
# 线下划拨 / revoke
# ============================================================

class AllocateOfflineRequest(BaseModel):
    customer_user_id: int
    tool_points: int = 0
    publish_points: int = 0
    bonus_points: int = 0
    description: Optional[str] = None


class SupplyDownstreamRequest(BaseModel):
    """上游线下供货给下线服务商(P0 热修 §2)。

    与 `AllocateOfflineRequest` 的区别是**账本落点**:这里进对方的库存算力,
    因此没有 tool/publish 之分 —— 库存只有充值(paid)与赠送(bonus)两格。
    """
    downstream_user_id: int
    paid_points: int = Field(0, ge=0)
    bonus_points: int = Field(0, ge=0)
    description: Optional[str] = None


class SupplyDownstreamResponse(BaseModel):
    """🔴 面向非 admin,**不含**任何关系反推字段(R5)——
    尤其不回 `cost_multiplier*`:加动作时最容易顺手把系数带出来。"""
    success: bool
    downstream_user_id: int
    supplied_paid: int
    supplied_bonus: int
    agent_paid_inventory_after: int
    agent_bonus_inventory_after: int
    downstream_paid_inventory_after: int
    downstream_bonus_inventory_after: int


class RevokeOfflineRequest(BaseModel):
    customer_user_id: int
    tool_points: int = 0
    publish_points: int = 0
    bonus_points: int = 0
    reason: Optional[str] = None


class AllocateRevokeResultResponse(BaseModel):
    success: bool
    customer_user_id: int
    new_tool_credit: int
    new_publish_credit: int
    new_bonus_credit: int
    agent_paid_inventory_after: int
    agent_bonus_inventory_after: int


class RelationActionDTO(BaseModel):
    """一个真实可点的出口(工单 §0.3:禁止只有解释、没有动作)。"""
    label: str
    action: str
    route: Optional[str] = None


class AgentCustomerLookupItem(BaseModel):
    """服务商搜索结果 · **含关系态与出口**(工单 v3 §P0-2)。

    🔴 本 DTO 面向非 admin,因此**刻意不含**任何关系反推字段:
       `cost_multiplier*` / `relationship_id` / `relationship_version` / `upstream_*` /
       `service_account_code` / `channel_account_code`(工单 §0.1 R5、§9 完成定义 5b)。
       上游要看"按什么价"→ 读 `effect_note` 的人话,不给原始系数。
       计价用的已绑定系数只在后端流转(`resolve_bound_cost_multiplier_bps`)。
    """
    customer_user_id: int
    display_name: Optional[str] = None
    phone_masked: Optional[str] = None
    brand_name: Optional[str] = None
    binding_status: str = Field(
        "owned",
        description="owned=我的客户 · downstream_partner=我的下线服务商 · both=两者都有",
    )
    tool_credit_points: int = 0
    publish_credit_points: int = 0
    bonus_credit_points: int = 0
    # —— 关系态与出口(与 `services.channel_partner_requests.resolve_relationship` 同源)——
    relation: Optional[str] = Field(
        None, description="customer / downstream_partner / both(**没有** upstream 态:R5)",
    )
    target_identity: Optional[str] = Field(None, description="level0 / service_provider")
    headline: Optional[str] = None
    effect_note: Optional[str] = Field(
        None, description="这次划拨会发生什么 —— 进哪个钱包、按什么价(提交前必须展示)",
    )
    primary_action: Optional[RelationActionDTO] = None
    secondary_actions: List[RelationActionDTO] = Field(default_factory=list)
    allowed_actions: List[str] = Field(default_factory=list)
    ledger_note: Optional[str] = Field(
        None, description="inventory_wallet=进库存算力 / available_wallet=进可用算力",
    )


class AgentCustomerLookupResponse(BaseModel):
    items: List[AgentCustomerLookupItem]
    total: int


# ============================================================
# 客户额度(代理视角)
# ============================================================

class AgentCustomerCreditResponse(BaseModel):
    """代理视角看客户授权额度三池余额 + 累计已用"""
    customer_user_id: int
    customer_display_name: Optional[str] = None
    phone_masked: Optional[str] = None
    tool_credit_points: int
    publish_credit_points: int
    bonus_credit_points: int
    total_allocated: int
    total_consumed: int
    binding_source: Optional[str] = None
    bound_at: Optional[datetime] = None
    dispute_status: Optional[str] = None


class AgentCustomerCreditTxItem(BaseModel):
    id: int
    type: str
    pool: str
    points: int
    related_order_id: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime


class AgentCustomerCreditTxResponse(BaseModel):
    items: List[AgentCustomerCreditTxItem]
    total: int
    online_fifo_available: int = Field(
        0, description="线上订单可分批 revoke 总和(FIFO 未消费)"
    )
    offline_total_remaining: int = Field(
        0, description="线下划拨总余额(W2 仅总余额 cap revoke · 分批留 W4)"
    )


# ============================================================
# 白标定价(代理视角 · raw cost 永不外露)
# ============================================================

class AgentSKUItem(BaseModel):
    retail_sku_id: str
    version: int
    sku_template_id: Optional[int] = Field(None, description="历史模板引用；新自定义包为空")
    source_template_id: Optional[int] = Field(None, description="可选预填来源，不参与身份/结算")
    sku_key: str
    category: str
    display_name: str
    subtitle: Optional[str] = None
    extra_promo_text: Optional[str] = None
    points_granted: int
    wholesale_cents: int = Field(..., description="当前有效进货成本(仅本服务商可见)")
    retail_cents: int = Field(..., description="代理设置的零售价")
    is_active: bool = True
    margin_label: Optional[str] = None
    override_id: int = Field(..., description="canonical 服务商零售包内部 id")
    sort_order: int = 0
    scene: Optional[str] = Field(None, description="适合场景(代理自定文案)")
    margin_warning: Optional[str] = None
    # ⚠️ 严禁加 platform_cost_cents / raw_cost / tax_rate_bps / *_fee_bps


class AgentSKUListResponse(BaseModel):
    items: List[AgentSKUItem]


class AgentSKUUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt = Field(..., ge=1)
    display_name: str = Field(..., min_length=1, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=240)
    extra_promo_text: Optional[str] = Field(None, max_length=1000)
    scene: Optional[str] = Field(None, max_length=500)
    points_granted: StrictInt = Field(..., gt=0, le=9_000_000_000_000_000)
    retail_cents: StrictInt = Field(..., gt=0, le=2_000_000_000)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class AgentSKUCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(..., min_length=8, max_length=80)
    source_template_id: Optional[StrictInt] = Field(None, gt=0)
    # Deprecated request alias retained only for older clients. It is converted
    # to source_template_id and never becomes the new SKU's identity.
    sku_template_id: Optional[StrictInt] = Field(None, gt=0)
    display_name: str = Field(..., min_length=1, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=240)
    extra_promo_text: Optional[str] = Field(None, max_length=1000)
    scene: Optional[str] = Field(None, max_length=500)
    points_granted: StrictInt = Field(..., gt=0, le=9_000_000_000_000_000)
    retail_cents: StrictInt = Field(..., gt=0, le=2_000_000_000)
    is_active: bool = True
    sort_order: int = 0


class AgentSKUPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points_granted: StrictInt = Field(..., gt=0, le=9_000_000_000_000_000)
    retail_cents: StrictInt = Field(..., gt=0, le=2_000_000_000)


# ============================================================
# 工厂结算(代理视角)
# ============================================================

class AgentSettlementBalanceResponse(BaseModel):
    """5 池余额 + 代理本人税预扣汇总(透明)"""
    frozen_cents: int = Field(0, description="T+3 内冻结")
    available_cents: int = Field(0, description="可提现(settled · 未锁定)")
    pending_cents: int = Field(0, description="申请中(已提交 · 未打款)")
    paid_cents: int = Field(0, description="已打款累计")
    clawback_pending_cents: int = Field(0, description="待抵扣(负数 clawback)")
    tax_withholding_total_cents: int = Field(
        0, description="代理本人累计预扣税(汇总 · 不暴露税率/模式)"
    )
    # ⚠️ 严禁加 tax_rate_bps / tax_mode / gateway_fee_bps / settlement_service_fee_bps


class AgentLedgerItem(BaseModel):
    id: int
    source: str
    recharge_order_id: Optional[str] = None
    customer_user_id: Optional[int] = None
    customer_paid_cents: int
    amount_cents: int = Field(..., description="应付代理款(已扣手续费+税)")
    tax_withholding_cents: int = 0
    status: str
    frozen_at: Optional[datetime] = None
    settle_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    manual_review_required: bool = False
    note: Optional[str] = None


class AgentAvailableItemsResponse(BaseModel):
    items: List[AgentLedgerItem]
    total_amount_cents: int


class AgentSettlementRequestCreate(BaseModel):
    """W1 service 内部走 FIFO 选 ledger items + 部分锁定 · 不传 ledger_ids"""
    amount_cents: int = Field(..., gt=0)
    bank_name: str = Field(..., min_length=1)
    bank_account: str = Field(..., min_length=1)
    account_holder: str = Field(..., min_length=1)
    invoice_required: bool = False


class AgentSettlementRequestResponse(BaseModel):
    id: int
    status: str
    amount_cents: int  # 提现申请额(gross · 锁 settled ledger 这么多)
    ledger_count: int
    # [V3.5 v7 批1D] 提现透明三段(金额 · 不露费率 *_bps · 见本文件 docstring 铁律)
    net_cents: int = Field(0, description="到账(gross − 总扣费)")
    total_fee_cents: int = Field(0, description="总扣费")
    platform_fee_cents: int = Field(0, description="平台服务费(通道+代收合并 · 不分项露费率)")
    tax_cents: int = Field(0, description="代扣税(金额汇总 · 不暴露税率/模式)")


# ============================================================
# 推广中心
# ============================================================

class AgentPromotionQRResponse(BaseModel):
    qr_code_url: str
    ref_link: str
    invite_code: str


class AgentPromotionCustomerItem(BaseModel):
    customer_user_id: int
    display_name: Optional[str] = None
    phone_masked: Optional[str] = None
    binding_source: str
    bound_at: datetime
    dispute_status: Optional[str] = None
    tool_credit: int = 0
    publish_credit: int = 0
    bonus_credit: int = 0
    # [客户线上购买门控 2026-07-29] 服务商侧字段(客户永远看不到这两个)。
    # online_purchase_override 三态:inherit(跟随默认)/ allow / offline_only。
    online_purchase_override: str = "inherit"
    # 该客户当前实际能否线上购买(= 两级判定后的结果 · 供列表直接显示状态)。
    can_purchase_online: bool = True


class AgentPromotionCustomersResponse(BaseModel):
    items: List[AgentPromotionCustomerItem]
    total: int
    # 主账号默认开关 · 列表页直接展示"跟随默认"到底是跟随成什么。
    default_allow_client_online_purchase: bool = True


class AgentClientPurchaseSettingsResponse(BaseModel):
    """服务商侧:名下客户线上购买总开关。文案面向服务商可以直说。"""
    allow_client_online_purchase: bool = True


class AgentClientPurchaseSettingsRequest(BaseModel):
    allow_client_online_purchase: bool


class AgentCustomerPurchaseOverrideRequest(BaseModel):
    """单客户三态覆盖:inherit / allow / offline_only。"""
    override: str


class AgentCustomerPurchaseOverrideResponse(BaseModel):
    customer_user_id: int
    online_purchase_override: str
    can_purchase_online: bool


# ============================================================
# Admin · 结算审批(全字段 · 可见 raw cost / tax 规则)
# ============================================================

class AdminSettlementRequestItem(BaseModel):
    id: int
    agent_user_id: int
    agent_display_name: Optional[str] = None
    agent_phone: Optional[str] = None
    amount_cents: int  # 提现申请额(gross · 锁 settled ledger 这么多)
    # [V3.5 v7 批1D] 财务按 net 实际打款(扣 fees 后到账)· admin 路径可见全字段(不受 W2 限制)
    # 历史申请(1A 前 ledger 已是净额 · net_amount_cents=0)回落 = gross · 全额打款
    net_amount_cents: int = 0   # 实际应打款额
    total_fee_cents: int = 0    # 总扣费(gross − net)
    payout_method: str
    payout_account: str
    status: str
    created_at: datetime
    ledger_count: int = 0


class AdminSettlementListResponse(BaseModel):
    items: List[AdminSettlementRequestItem]
    total: int


class AdminSettlementPatchRequest(BaseModel):
    action: str = Field(..., description="approve / reject / mark_paid")
    transfer_proof_url: Optional[str] = None
    wire_transfer_no: Optional[str] = None
    admin_note: Optional[str] = None
    reject_reason: Optional[str] = None


# ============================================================
# Admin · 税务配置
# ============================================================

class AdminTaxProfileResponse(BaseModel):
    """对齐 W1 schema agent_tax_profiles"""
    agent_user_id: int
    entity_type: Optional[str] = Field(
        None, description="individual / individual_business / company / partnership"
    )
    default_tax_rate_bps: Optional[int] = None
    default_tax_mode: Optional[str] = Field(
        None, description="withheld / invoice_provided / exempt_manual"
    )
    tax_id: Optional[str] = None
    invoice_capability: Optional[str] = Field(
        None, description="none / general_invoice / special_invoice"
    )
    notes: Optional[str] = None
    updated_at: Optional[datetime] = None


class AdminTaxProfilePutRequest(BaseModel):
    entity_type: Optional[str] = None
    default_tax_rate_bps: Optional[int] = Field(None, ge=0, le=10000)
    default_tax_mode: Optional[str] = None
    tax_id: Optional[str] = None
    invoice_capability: Optional[str] = None
    notes: Optional[str] = None


# ============================================================
# Admin · 价格管理(可见全部 raw cost)
# ============================================================

class AdminSKUItem(BaseModel):
    """sku_templates 管理视图 · W1 schema 无 platform_cost_cents 列(在 feature_pricing/mhz_media)"""
    sku_template_id: int
    sku_key: str
    category: str
    display_name: str
    subtitle: Optional[str] = None
    points_granted: int
    wholesale_cents: int = Field(..., description="出厂价")
    retail_cents: int = Field(..., description="平台建议零售价(suggested_retail_cents)· 代理可覆盖")
    is_active: bool = True


class AdminSKUListResponse(BaseModel):
    items: List[AdminSKUItem]


class AdminSKUPutRequest(BaseModel):
    points_granted: Optional[int] = None
    wholesale_cents: Optional[int] = None
    retail_cents: Optional[int] = Field(None, description="写入 suggested_retail_cents")
    is_active: Optional[bool] = None
    display_name: Optional[str] = None
    subtitle: Optional[str] = None


# ============================================================
# Admin · 对账快照(只读 · W4 才做日报告警)
# ============================================================

class AdminInventoryAuditSummary(BaseModel):
    agent_total_points: int = Field(..., description="所有代理 paid+bonus 库存总和")
    # [2026-07-29 返修] 账实相符式的三个量 · frozen 必须计入(冻结是池内搬运 · 不写流水)
    agent_frozen_points: int = Field(0, description="所有代理 frozen 冻结库存总和")
    wallet_total_points: int = Field(0, description="账面【实】= paid + bonus + frozen")
    ledger_total_points: int = Field(0, description="账面【账】= 全量 agent_inventory_transactions.points 净额")
    # ↓ 以下四项为审计留痕分项 · 不参与 diff(枚举式残留 · 统计不全)
    customer_total_points: int = Field(..., description="所有客户 tool+publish+bonus 授权总和(留痕 · 不进 diff)")
    platform_consumed_points: int = Field(..., description="已消费总和(读已停写账本 · 留痕 · 不进 diff)")
    historical_purchased_points: int = Field(..., description="历史进货总和(枚举式分项 · 留痕 · 不进 diff)")
    historical_admin_adjust_points: int = Field(0, description="admin 调账总和(枚举式分项 · 留痕 · 不进 diff)")
    diff_points: int = Field(
        ..., description="对账等式差异 = wallet_total(paid+bonus+frozen) − ledger_total(全量流水净额)"
    )
    diff_status: str = Field(..., description="ok / drift_warning(W4 才做告警 · W2 只读展示)")
    snapshot_at: datetime
