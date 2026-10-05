from types import SimpleNamespace

import pytest


def _admin_request():
    return SimpleNamespace(state=SimpleNamespace(user={"id": 1, "user_id": 1, "is_admin": True}))


def test_recharge_path_label_priority():
    from api.finance_api import _classify_recharge_path

    assert _classify_recharge_path("agent_inventory_purchase", None, None) == "服务商预付进货"
    assert _classify_recharge_path("recharge", "v32_legacy", None) == "老分润路径"
    assert _classify_recharge_path("customer_recharge", "v35_inventory_settlement", 49) == "服务商客户充值"
    assert _classify_recharge_path("recharge", "v35_inventory_settlement", 49) == "服务商客户充值"
    assert _classify_recharge_path("recharge", None, None) == "平台直营"


def test_recharge_limit_is_clamped():
    from api.finance_api import _clamp_recharge_limit

    assert _clamp_recharge_limit(0) == 1
    assert _clamp_recharge_limit(50) == 50
    assert _clamp_recharge_limit(500) == 200


@pytest.mark.asyncio
async def test_recharge_orders_returns_cents_and_exact_total(monkeypatch):
    from api import finance_api

    def fake_query(sql, params=()):
        if "COUNT(*) AS v" in sql:
            return [{"v": 1}]
        if "SUM(ro.amount_cents)" in sql:
            return [{"amount_cents": 24000, "points_granted": 24375}]
        if "FROM recharge_orders ro" in sql and "ORDER BY" in sql:
            return [{
                "id": "order-1",
                "user_id": 103,
                "user_display_name": "澄远文化",
                "user_username": "13000000000",
                "user_agent_level": 0,
                "amount_cents": 24000,
                "base_points": 24375,
                "bonus_points": 0,
                "payment_status": "paid",
                "refund_status": None,
                "refunded_amount_cents": 0,
                "payment_method": "wechat",
                "order_type": "customer_recharge",
                "settlement_mode": "v35_inventory_settlement",
                "agent_user_id": 49,
                "agent_display_name": "又是个zoomba",
                "service_revenue_cents": 0,
                "created_at": None,
                "paid_at": None,
            }]
        raise AssertionError(f"unexpected SQL: {sql[:120]}")

    monkeypatch.setattr(finance_api, "_query_db", fake_query)

    result = await finance_api.admin_recharge_orders(
        _admin_request(), period="month", status="paid", limit=500
    )

    assert result["limit"] == 200
    assert result["total"] == 1
    assert result["totals"]["amount_cents"] == 24000
    assert result["items"][0]["amount_cents"] == 24000
    assert "amount_yuan" not in result["items"][0]
    assert result["items"][0]["path_label"] == "服务商客户充值"


@pytest.mark.asyncio
async def test_inventory_purchase_flow_does_not_warn_missing_wallet_transaction(monkeypatch):
    from api import finance_api

    def fake_query_db(sql, params=()):
        if "FROM recharge_orders ro" in sql:
            return [{
                "id": "inv-1",
                "user_id": 49,
                "user_display_name": "服务商A",
                "user_username": "sp-a",
                "user_agent_level": 1,
                "amount_cents": 100000,
                "base_points": 100000,
                "bonus_points": 0,
                "payment_status": "paid",
                "refund_status": None,
                "refunded_amount_cents": 0,
                "payment_method": "wechat",
                "order_type": "agent_inventory_purchase",
                "settlement_mode": None,
                "agent_user_id": None,
                "agent_display_name": None,
                "created_at": None,
                "paid_at": None,
            }]
        raise AssertionError(f"unexpected strict SQL: {sql[:120]}")

    def fake_safe_rows(sql, params=()):
        return []

    monkeypatch.setattr(finance_api, "_query_db", fake_query_db)
    monkeypatch.setattr(finance_api, "_safe_rows", fake_safe_rows)

    result = await finance_api.admin_recharge_order_flow("inv-1", _admin_request())

    assert result["order"]["path_label"] == "服务商预付进货"
    assert "订单已支付,但未找到普通钱包充值流水" not in result["warnings"]


@pytest.mark.asyncio
async def test_recharge_flow_order_lookup_query_failure_is_not_turned_into_404(monkeypatch):
    from api import finance_api

    def broken_query(sql, params=()):
        raise RuntimeError("schema drift")

    monkeypatch.setattr(finance_api, "_query_db", broken_query)

    with pytest.raises(RuntimeError, match="schema drift"):
        await finance_api.admin_recharge_order_flow("order-1", _admin_request())


def test_dashboard_cash_revenue_uses_paid_time_basis():
    import inspect
    from api import dashboard_api

    source = inspect.getsource(dashboard_api)
    assert "COALESCE(paid_at, created_at) >= %s" in source


def test_admin_business_profile_includes_customer_credit(monkeypatch):
    import services.admin_business_profile as profile

    class FakeCursor:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql, params=()):
            self.last_sql = sql

        def fetchone(self):
            if "SUM(ABS(amount))" in self.last_sql:
                return {"consumed": 0}
            if "SUM(agent_settlement_cents)" in self.last_sql:
                return {"settled": 0}
            return None

        def fetchall(self):
            if "FROM customer_credit_transactions" in self.last_sql:
                return [{
                    "id": 10,
                    "agent_user_id": 49,
                    "agent_display_name": "服务商A",
                    "type": "allocate",
                    "pool": "tool",
                    "points": 24375,
                    "balance_tool_after": 24375,
                    "balance_publish_after": 0,
                    "balance_bonus_after": 0,
                    "feature_code": None,
                    "related_order_id": "order-1",
                    "source": "online_payment",
                    "description": "平台代收",
                    "created_at": None,
                }]
            return []

    monkeypatch.setattr(
        "db.wallet_db.get_wallet_balance",
        lambda _user_id: {
            "paid_points": 0,
            "bonus_points": 968,
            "total": 3893,
            "customer_credit": {
                "tool_credit_points": 2925,
                "publish_credit_points": 0,
                "bonus_credit_points": 0,
                "total_purchased_points": 24375,
                "total_consumed_points": 21450,
                "agent_user_id": 49,
            },
        },
    )

    result = profile._build_wallet_billing(FakeCursor(), 103)

    credit = result["customer_credit"]
    assert credit["total_remaining_points"] == 2925
    assert credit["total_purchased_points"] == 24375
    assert result["customer_credit_transactions"][0]["related_order_id"] == "order-1"
