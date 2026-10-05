"""V3.3.1 净现金计算 单元测试

不依赖 DB · 纯函数测试
跑:python -m pytest tests/test_v3_3_1_net_cash.py -v
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.net_cash_revenue import (  # noqa: E402
    compute_net_cash,
    compute_service_fee,
    compute_referral_bonus_points,
    extract_breakdown_from_order,
    compute_from_order,
    POINTS_PER_YUAN,
    DEFAULT_GATEWAY_FEE_RATE,
)


def test_pure_recharge_no_deduction():
    """纯充值 · 仅扣手续费 · 净现金 = paid × (1 - 0.6%)"""
    r = compute_net_cash(100)
    assert r.gross_amount_yuan == Decimal("100")
    assert r.gateway_fee_yuan == Decimal("0.60")
    assert r.net_cash_revenue_yuan == Decimal("99.40")
    assert r.is_positive()


def test_with_refund():
    r = compute_net_cash(100, refund_amount_yuan=30, auto_gateway_fee=False)
    assert r.net_cash_revenue_yuan == Decimal("70.00")


def test_with_media_cost():
    r = compute_net_cash(100, media_cost_yuan=40, auto_gateway_fee=False)
    assert r.net_cash_revenue_yuan == Decimal("60.00")


def test_with_all_deductions():
    r = compute_net_cash(
        500,
        refund_amount_yuan=50,
        media_cost_yuan=100,
        coupon_yuan=10,
        bonus_points_used=130,  # = 1 元
        granted_points_deducted_yuan=5,
        gateway_fee_yuan=3,
    )
    # 500 - 50 - 100 - 10 - 1 - 5 - 3 = 331
    assert r.net_cash_revenue_yuan == Decimal("331.00")


def test_negative_net_skips():
    """全额退款 · 净现金 <= 0 · is_positive=False"""
    r = compute_net_cash(100, refund_amount_yuan=100, auto_gateway_fee=False)
    assert r.net_cash_revenue_yuan == Decimal("0.00")
    assert not r.is_positive()


def test_negative_net_with_media_cost_higher_than_paid():
    """媒体外采 > 支付 · net_cash 负数"""
    r = compute_net_cash(50, media_cost_yuan=100, auto_gateway_fee=False)
    assert r.net_cash_revenue_yuan == Decimal("-50.00")
    assert not r.is_positive()


def test_service_fee_28_percent():
    """按服务费率计算"""
    r = compute_net_cash(100, auto_gateway_fee=False)  # net = 100
    fee = compute_service_fee(r, rate=Decimal("0.28"))
    assert fee == Decimal("28.00")


def test_service_fee_skips_negative_net():
    """净现金 <= 0 时 service_fee = 0"""
    r = compute_net_cash(100, refund_amount_yuan=100, auto_gateway_fee=False)
    fee = compute_service_fee(r, rate=Decimal("0.28"))
    assert fee == Decimal("0")


def test_referral_bonus_points_15_percent():
    """15% 推荐奖励 · 转积分"""
    r = compute_net_cash(100, auto_gateway_fee=False)  # net = 100
    pts = compute_referral_bonus_points(r, rate=Decimal("0.15"))
    # 100 × 0.15 × 130 = 1950
    assert pts == 1950


def test_referral_bonus_points_skips_negative():
    r = compute_net_cash(100, refund_amount_yuan=100, auto_gateway_fee=False)
    pts = compute_referral_bonus_points(r, rate=Decimal("0.15"))
    assert pts == 0


def test_extract_from_order_with_aliases():
    """订单字段多重 fallback"""
    order = {
        "id": "ORDER-X", "user_id": 1,
        "paid_amount_cents": 9900,  # → 99 元
        "refund_amount_cents": 0,
    }
    args = extract_breakdown_from_order(order)
    assert args["paid_amount_yuan"] == 99.0
    assert args["refund_amount_yuan"] == 0


def test_compute_from_order():
    order = {
        "id": "ORDER-Y", "user_id": 1,
        "paid_amount_yuan": 200,
        "refund_amount_yuan": 30,
        "media_cost_yuan": 20,
    }
    r = compute_from_order(order, auto_gateway_fee=False)
    # 200 - 30 - 20 = 150
    assert r.net_cash_revenue_yuan == Decimal("150.00")


def test_extract_handles_missing_fields():
    order = {"id": "X", "user_id": 1, "paid_amount_yuan": 50}
    args = extract_breakdown_from_order(order)
    assert args["paid_amount_yuan"] == 50
    assert args["refund_amount_yuan"] is None
    assert args["media_cost_yuan"] is None


def test_bonus_points_conversion():
    """bonus_points_used 自动按 1元=130积分 换算"""
    r = compute_net_cash(100, bonus_points_used=260, auto_gateway_fee=False)  # 260 = 2 元
    assert r.bonus_deducted_yuan == Decimal("2.00")
    assert r.net_cash_revenue_yuan == Decimal("98.00")


def test_zero_paid_returns_zero():
    r = compute_net_cash(0)
    assert r.net_cash_revenue_yuan == Decimal("0.00")
    assert not r.is_positive()


def test_none_inputs_handled():
    r = compute_net_cash(None)
    assert r.gross_amount_yuan == Decimal("0")
    assert not r.is_positive()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
