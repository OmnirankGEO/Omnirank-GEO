"""[V3.5 v7 · 2026-06-08 老板拍板 A/B/C] 资金链根治测试

批 1A · calc_settlement 新公式:结算 = R - factory(纯 markup)· fees/税挪提现端。
后续批次(1B/1C/1D/2/3)测试随各批补入本文件。
"""
from services.agent_pricing import calc_settlement, calc_factory_cents


# ---------- 批 1A · calc_settlement v7 ----------

def test_calc_settlement_v7_pure_markup():
    """老板新模型铁证:R=200/points=130/factory=95 → settlement=105(纯 markup·无 fees/税)。"""
    r = calc_settlement(200, 130, factory_cents_override=95)
    assert r["agent_settlement_cents"] == 105
    assert r["agent_margin_before_tax_cents"] == 105
    assert r["factory_cents"] == 95
    assert r["customer_paid_cents"] == 200
    # fees/税全 0(挪提现端)· 字段保留向后兼容
    assert r["gateway_fee_cents"] == 0
    assert r["settlement_service_fee_cents"] == 0
    assert r["collection_fee_cents"] == 0
    assert r["tax_withholding_cents"] == 0
    assert r["gateway_fee_bps"] == 0
    assert r["settlement_service_fee_bps"] == 0
    assert r["tax_rate_bps"] == 0
    # 返回 dict 字段集不变(下游 finance/referral 读取兼容)
    assert set(r.keys()) == {
        "customer_paid_cents", "factory_cents", "gateway_fee_bps", "gateway_fee_cents",
        "settlement_service_fee_bps", "settlement_service_fee_cents", "collection_fee_cents",
        "agent_margin_before_tax_cents", "tax_rate_bps", "tax_withholding_cents",
        "agent_settlement_cents",
    }


def test_calc_settlement_v7_zero_markup():
    """0 markup(基础体验包·R=factory):settlement=0·不亏不赚。"""
    r = calc_settlement(95, 130, factory_cents_override=95)
    assert r["agent_settlement_cents"] == 0
    assert r["agent_margin_before_tax_cents"] == 0


def test_calc_settlement_v7_large_markup_no_tax_at_settlement():
    """大 markup:R=1000/factory=95 → settlement=905;大利润也不在结算扣税(挪提现)。"""
    r = calc_settlement(1000, 130, factory_cents_override=95)
    assert r["agent_settlement_cents"] == 905
    assert r["tax_withholding_cents"] == 0


def test_calc_settlement_v7_fees_zero_regardless_of_payment_method():
    """无论支付方式·结算端 fees 恒 0(挪提现端)。"""
    for pm in ("wechat_pay", "alipay", "huipi", "manual"):
        r = calc_settlement(200, 130, payment_method=pm, factory_cents_override=95)
        assert r["gateway_fee_cents"] == 0
        assert r["agent_settlement_cents"] == 105


def test_calc_settlement_v7_tax_rate_bps_arg_ignored():
    """老 tax_rate_bps 入参保留(签名兼容)但结算端不再使用 → 恒 0。"""
    r = calc_settlement(200, 130, tax_rate_bps=600, factory_cents_override=95)
    assert r["tax_rate_bps"] == 0
    assert r["tax_withholding_cents"] == 0
    assert r["agent_settlement_cents"] == 105


def test_calc_settlement_v7_factory_from_config_when_no_override():
    """无 override 时 factory 走 calc_factory_cents(当前配置)· settlement = R - factory。"""
    factory = calc_factory_cents(130)
    r = calc_settlement(200, 130)
    assert r["factory_cents"] == factory
    assert r["agent_settlement_cents"] == 200 - factory
    assert r["agent_margin_before_tax_cents"] == 200 - factory


def test_calc_factory_cents_unchanged_regression():
    """calc_factory_cents 未改(ceil 整数防累计误差)· 防回归。"""
    assert calc_factory_cents(130, 225, 325) == 90     # 9 折:ceil(90.0)=90
    assert calc_factory_cents(130, 9500, 13000) == 95  # 95 折:ceil(95.0)=95
    assert calc_factory_cents(0) == 0
