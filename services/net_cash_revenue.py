"""
V3.3.1 净现金收入(net cash revenue)计算核心

公式(决策书 §3.1.1):
    net_cash_revenue =
        支付到账(paid_amount_yuan)
      - 已退款(refund_amount)
      - 媒体外采(media_cost · 平台无毛利硬成本)
      - 优惠券(coupon_discount)
      - bonus_points 抵扣换算(bonus_points_used / 130)
      - 赠送积分抵扣换算(granted_points_deducted)
      - 支付通道手续费(payment_gateway_fee · ~0.6% 微信)

设计原则:
- 纯函数 · 无副作用 · 不读 DB
- 返回 Decimal + 明细(便于审计)
- 入参缺失 → fallback 0(防 None 撞算式)
- 负数允许返回 · 调用方自行决定 skip / 0 / 报警

调用方:
- services/service_fee_calculator.py(写 service_fee_records 前算 net_cash 入参)
- api/referral_api.py(写 pending_bonus 前算 net_cash 入参)
- services/refund_processor.py(退款时重算 net_cash)

关联:
- 决策书 §3.1.1 / §3.1.2
- IDENTITY_DECISIONS_LOCK Q31-Q32
"""

import logging
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Dict, Optional

logger = logging.getLogger("GEO-NetCash")

POINTS_PER_YUAN = Decimal("130")
DEFAULT_GATEWAY_FEE_RATE = Decimal("0.006")  # 微信 V3 ~0.6%


def _to_decimal(value, default: str = "0") -> Decimal:
    """容错转 Decimal · None / "" / 异常都 fallback default"""
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        logger.warning("net_cash._to_decimal: bad value %r · fallback %s", value, default)
        return Decimal(default)


@dataclass(frozen=True)
class NetCashBreakdown:
    """净现金计算的完整明细 · 便于审计 / DB 落库"""

    gross_amount_yuan: Decimal = field(default_factory=lambda: Decimal("0"))
    refund_amount_yuan: Decimal = field(default_factory=lambda: Decimal("0"))
    media_cost_yuan: Decimal = field(default_factory=lambda: Decimal("0"))
    coupon_yuan: Decimal = field(default_factory=lambda: Decimal("0"))
    bonus_deducted_yuan: Decimal = field(default_factory=lambda: Decimal("0"))
    granted_points_deducted_yuan: Decimal = field(default_factory=lambda: Decimal("0"))
    gateway_fee_yuan: Decimal = field(default_factory=lambda: Decimal("0"))
    net_cash_revenue_yuan: Decimal = field(default_factory=lambda: Decimal("0"))

    def is_positive(self) -> bool:
        """net_cash > 0 才能触发服务费/推荐奖励"""
        return self.net_cash_revenue_yuan > Decimal("0")

    def as_dict_yuan(self) -> Dict[str, float]:
        """转 float dict · 便于 DB 写 DECIMAL 字段"""
        return {k: float(v) for k, v in asdict(self).items()}


def compute_net_cash(
    paid_amount_yuan,
    *,
    refund_amount_yuan=None,
    media_cost_yuan=None,
    coupon_yuan=None,
    bonus_points_used=None,
    granted_points_deducted_yuan=None,
    gateway_fee_yuan=None,
    auto_gateway_fee: bool = True,
) -> NetCashBreakdown:
    """计算净现金收入

    Args:
        paid_amount_yuan:    支付到账金额(元)· 必传
        refund_amount_yuan:  已退款金额(元)· 默认 0
        media_cost_yuan:     媒体外采(元)· 默认 0
        coupon_yuan:         优惠券抵扣(元)· 默认 0
        bonus_points_used:   消费时使用的 bonus_points · 默认 0(将按 1元=130积分换算)
        granted_points_deducted_yuan: 赠送积分抵扣换算(元)· 默认 0
        gateway_fee_yuan:    支付通道手续费(元)· 默认按 auto_gateway_fee 自动 0.6%
        auto_gateway_fee:    True 时 gateway_fee 未传则自动按 0.6% 计算

    Returns:
        NetCashBreakdown · 完整明细 + net_cash_revenue_yuan(可能为负)

    Examples:
        >>> r = compute_net_cash(100)
        >>> float(r.net_cash_revenue_yuan)
        99.4  # 100 - 0.6% 手续费

        >>> r = compute_net_cash(100, media_cost_yuan=30)
        >>> float(r.net_cash_revenue_yuan)
        69.4

        >>> r = compute_net_cash(100, refund_amount_yuan=100)
        >>> r.is_positive()
        False
    """
    gross = _to_decimal(paid_amount_yuan)
    refund = _to_decimal(refund_amount_yuan)
    media = _to_decimal(media_cost_yuan)
    coupon = _to_decimal(coupon_yuan)
    granted_deducted = _to_decimal(granted_points_deducted_yuan)

    bonus_pts = _to_decimal(bonus_points_used)
    bonus_yuan = bonus_pts / POINTS_PER_YUAN if bonus_pts > 0 else Decimal("0")

    if gateway_fee_yuan is None and auto_gateway_fee:
        gateway = (gross * DEFAULT_GATEWAY_FEE_RATE).quantize(Decimal("0.01"))
    else:
        gateway = _to_decimal(gateway_fee_yuan)

    net = (gross - refund - media - coupon - bonus_yuan - granted_deducted - gateway).quantize(
        Decimal("0.01")
    )

    return NetCashBreakdown(
        gross_amount_yuan=gross.quantize(Decimal("0.01")),
        refund_amount_yuan=refund.quantize(Decimal("0.01")),
        media_cost_yuan=media.quantize(Decimal("0.01")),
        coupon_yuan=coupon.quantize(Decimal("0.01")),
        bonus_deducted_yuan=bonus_yuan.quantize(Decimal("0.01")),
        granted_points_deducted_yuan=granted_deducted.quantize(Decimal("0.01")),
        gateway_fee_yuan=gateway.quantize(Decimal("0.01")),
        net_cash_revenue_yuan=net,
    )


def compute_service_fee(
    net_cash_breakdown: NetCashBreakdown,
    rate=None,
) -> Decimal:
    """服务费金额 = net_cash × rate(默认 28% · 可传 0.0 表示禁用)"""
    if not net_cash_breakdown.is_positive():
        return Decimal("0")
    from config.v3_3_1_flags import get_service_fee_rate

    r = _to_decimal(rate) if rate is not None else _to_decimal(get_service_fee_rate())
    return (net_cash_breakdown.net_cash_revenue_yuan * r).quantize(Decimal("0.01"))


def compute_referral_bonus_points(
    net_cash_breakdown: NetCashBreakdown,
    rate=None,
) -> int:
    """推荐奖励 bonus_points 数 = net_cash × rate × 130

    Returns:
        int · 直接入 pending_bonus_records.bonus_points
    """
    if not net_cash_breakdown.is_positive():
        return 0
    from config.v3_3_1_flags import get_referral_bonus_rate

    r = _to_decimal(rate) if rate is not None else _to_decimal(get_referral_bonus_rate())
    yuan = net_cash_breakdown.net_cash_revenue_yuan * r
    points = (yuan * POINTS_PER_YUAN).quantize(Decimal("1"))
    return int(points)


def extract_breakdown_from_order(order: dict) -> Dict[str, Optional[float]]:
    """从订单 dict 抽出 net_cash 入参 · 容错老数据(字段缺失 → None)

    支持的字段名(多重 fallback · 兼容老订单 schema):
        paid: paid_amount_yuan / amount_yuan / paid_amount_cents(自动 / 100)
        refund: refund_amount_yuan / refunded_amount / refund_amount_cents
        media: media_cost_yuan / external_media_cost / media_purchase_cost
        coupon: coupon_yuan / coupon_discount / coupon_amount
        bonus_used: bonus_points_used / bonus_deducted
        gateway: gateway_fee_yuan / payment_gateway_fee
    """

    def first_present(*keys):
        for k in keys:
            if k in order and order[k] is not None:
                return order[k]
        return None

    paid = first_present("paid_amount_yuan", "amount_yuan")
    if paid is None:
        cents = first_present("paid_amount_cents", "amount_cents")
        paid = (cents / 100.0) if cents is not None else None

    refund = first_present("refund_amount_yuan", "refunded_amount")
    if refund is None:
        cents = first_present("refund_amount_cents")
        refund = (cents / 100.0) if cents is not None else None

    return {
        "paid_amount_yuan": paid,
        "refund_amount_yuan": refund,
        "media_cost_yuan": first_present("media_cost_yuan", "external_media_cost", "media_purchase_cost"),
        "coupon_yuan": first_present("coupon_yuan", "coupon_discount", "coupon_amount"),
        "bonus_points_used": first_present("bonus_points_used", "bonus_deducted"),
        "granted_points_deducted_yuan": first_present("granted_points_deducted_yuan", "granted_deducted"),
        "gateway_fee_yuan": first_present("gateway_fee_yuan", "payment_gateway_fee"),
    }


def compute_from_order(order: dict, *, auto_gateway_fee: bool = True) -> NetCashBreakdown:
    """便捷入口:从订单 dict 直接算"""
    args = extract_breakdown_from_order(order)
    return compute_net_cash(
        args["paid_amount_yuan"] or 0,
        refund_amount_yuan=args["refund_amount_yuan"],
        media_cost_yuan=args["media_cost_yuan"],
        coupon_yuan=args["coupon_yuan"],
        bonus_points_used=args["bonus_points_used"],
        granted_points_deducted_yuan=args["granted_points_deducted_yuan"],
        gateway_fee_yuan=args["gateway_fee_yuan"],
        auto_gateway_fee=auto_gateway_fee,
    )
