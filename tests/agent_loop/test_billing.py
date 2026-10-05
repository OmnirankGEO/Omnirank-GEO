import pytest


def _cleanup_audit():
    from db.social_agent_tool_audit import ensure_tables, get_connection

    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_agent_tool_audit WHERE turn_id LIKE 'pytest_billing_%'")
        conn.commit()
    finally:
        conn.close()


def test_cost_map_covers_all_declared_tools():
    from tools.agent_loop.billing.cost_map import TOOL_COST_POINTS, get_tool_cost
    from tools.agent_loop.tool_definitions import TOOL_SCHEMAS

    tool_names = {schema["name"] for schema in TOOL_SCHEMAS}

    assert set(TOOL_COST_POINTS) == tool_names
    assert get_tool_cost("metaso_web_search") == 5
    assert get_tool_cost("time_now") == 0
    assert all(isinstance(points, int) and points >= 0 for points in TOOL_COST_POINTS.values())


def test_estimator_uses_soft_and_hard_confirmation_lines():
    from tools.agent_loop.billing.estimator import estimate_turn_cost

    ok = estimate_turn_cost(["keyword_explore", "time_now"], user_is_admin=False)
    soft = estimate_turn_cost(["tikhub_parse_video"] * 8, user_is_admin=False)
    hard = estimate_turn_cost(["tikhub_parse_video"] * 13, user_is_admin=False)
    admin = estimate_turn_cost(["tikhub_parse_video"] * 13, user_is_admin=True)
    old_brand = estimate_turn_cost(["tikhub_parse_video"] * 13, grandfathered=True)

    assert ok["decision"] == "ok"
    assert ok["total_points"] == 2
    assert soft["decision"] == "soft_confirm"
    assert soft["requires_confirmation"] is True
    assert hard["decision"] == "hard_confirm"
    assert hard["requires_confirmation"] is True
    assert admin["decision"] == "admin_exempt"
    assert admin["total_points"] == 0
    assert old_brand["decision"] == "grandfathered"
    assert old_brand["total_points"] == 0


@pytest.mark.asyncio
async def test_wrapper_charges_and_refunds_with_injected_billing_functions():
    from tools.agent_loop.billing.wrapper import AgentBillingContext, charge_tool_call, refund_tool_call

    calls = []

    async def deduct(user_id, feature_code, extra_cost=0, brand_id=None):
        calls.append(("deduct", user_id, feature_code, extra_cost, brand_id))
        return {"success": True, "deducted": extra_cost}

    async def refund(user_id, feature_code, reason):
        calls.append(("refund", user_id, feature_code, reason))
        return {"success": True, "refunded": 5}

    charge = await charge_tool_call(
        "metaso_web_search",
        5,
        AgentBillingContext(user_id=201, brand_id=301),
        deduct_func=deduct,
    )
    refunded = await refund_tool_call(charge, "tool failed", refund_func=refund)

    assert charge.charged is True
    assert charge.deducted_points == 5
    assert refunded.refunded_points == 5
    assert calls == [
        ("deduct", 201, "agent_tool_call", 5, 301),
        ("refund", 201, "agent_tool_call", "tool failed"),
    ]


@pytest.mark.asyncio
async def test_wrapper_admin_is_exempt():
    from tools.agent_loop.billing.wrapper import AgentBillingContext, charge_tool_call

    async def deduct(*args, **kwargs):
        raise AssertionError("admin should not be billed")

    charge = await charge_tool_call(
        "metaso_web_search",
        5,
        AgentBillingContext(user_id=202, is_admin=True),
        deduct_func=deduct,
    )

    assert charge.charged is False
    assert charge.billing_status == "admin_exempt"


@pytest.mark.asyncio
async def test_router_charges_successful_tool_and_records_audit(monkeypatch):
    from db.social_agent_tool_audit import get_audit_by_turn_id
    from tools.agent_loop.adapters import keyword
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    _cleanup_audit()
    calls = []

    async def fake_expand(**kwargs):
        return {"success": True, "keywords": [{"keyword": "德国洗衣凝珠"}]}

    async def deduct(user_id, feature_code, extra_cost=0, brand_id=None):
        calls.append(("deduct", user_id, feature_code, extra_cost, brand_id))
        return {"success": True, "deducted": extra_cost}

    monkeypatch.setattr(keyword, "expand_keywords_for_client", fake_expand)
    ctx = AgentToolContext(
        user_id=203,
        profile_id="pytest_billing_profile",
        turn_id="pytest_billing_router_success",
        billing_enabled=True,
        brand_id=303,
        billing_deduct_func=deduct,
    )

    result = await execute_tool("keyword_explore", {"seed": "洗衣凝珠", "industry": "外贸"}, ctx)
    rows = get_audit_by_turn_id("pytest_billing_router_success")

    assert result["ok"] is True
    assert calls == [("deduct", 203, "agent_tool_call", 2, 303)]
    assert rows[0]["charged_points"] == 2
    assert rows[0]["refunded_points"] == 0
    assert rows[0]["billing_status"] == "charged"


@pytest.mark.asyncio
async def test_router_refunds_failed_tool_and_records_audit(monkeypatch):
    from db.social_agent_tool_audit import get_audit_by_turn_id
    from tools.agent_loop.adapters import search
    from tools.agent_loop.tool_router import AgentToolContext, execute_tool

    _cleanup_audit()
    calls = []

    async def boom(query, **kwargs):
        raise RuntimeError("provider down")

    async def deduct(user_id, feature_code, extra_cost=0, brand_id=None):
        calls.append(("deduct", user_id, feature_code, extra_cost, brand_id))
        return {"success": True, "deducted": extra_cost}

    async def refund(user_id, feature_code, reason):
        calls.append(("refund", user_id, feature_code, reason))
        return {"success": True, "refunded": 5}

    monkeypatch.setattr(search, "metaso_web_search", boom)
    ctx = AgentToolContext(
        user_id=204,
        profile_id="pytest_billing_profile",
        turn_id="pytest_billing_router_refund",
        billing_enabled=True,
        billing_deduct_func=deduct,
        billing_refund_func=refund,
    )

    result = await execute_tool("metaso_web_search", {"query": "德国超市洗衣凝珠"}, ctx)
    rows = get_audit_by_turn_id("pytest_billing_router_refund")

    assert result["ok"] is False
    assert calls[0] == ("deduct", 204, "agent_tool_call", 5, None)
    assert calls[1][0] == "refund"
    assert rows[0]["charged_points"] == 5
    assert rows[0]["refunded_points"] == 5
    assert rows[0]["billing_status"] == "refunded"
