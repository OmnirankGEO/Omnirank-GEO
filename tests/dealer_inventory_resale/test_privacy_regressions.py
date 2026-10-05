from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_PUBLIC_KEYS = {
    "agent_user_id",
    "upstream_user_id",
    "service_account_code",
    "channel_account_code",
    "seller_account_code",
    "resolved_user_id",
    "relationship_id",
    "relationship_version",
    "cost_multiplier",
    "cost_multiplier_bps",
    "counterparty_account_code",
    "responsible_service_account_code",
    "seller_user_id",
    "buyer_user_id",
    "seller_cost_basis_cents",
    "margin_cents",
    "downstream_markup_bps",
    "edge_multiplier_bps",
    "standard_reference_cents",
    "fulfillment_plan",
    "hops",
    "promotion_sponsor_user_id",
    "platform_seller_user_id",
}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def test_non_admin_order_and_refund_contracts_never_expose_upstream_identity(db_conn):
    from services import dealer_inventory_resale as resale
    from .test_resale_funds import _sell_water_to_consumer

    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    cur.execute("SELECT * FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    order = resale._serialize_order(
        cur, dict(cur.fetchone()), actor_user_id=30, is_admin=False,
    )
    case = resale.request_consumer_refund_case(
        cur, order_id="O-SVC-CUSTOMER", consumer_user_id=40, reason="unused",
    )
    refund = resale.get_consumer_refund_visible(
        cur, case_id=case["case_id"], actor_user_id=40, is_admin=False,
    )

    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(set(_walk_keys(order)))
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(set(_walk_keys(refund)))
    assert order["refund_handling"] == "PLATFORM_MANAGED"
    assert refund["refund_handling"] == "PLATFORM_MANAGED"
    cur.execute(
        "SELECT description FROM customer_credit_transactions "
        "WHERE customer_user_id=40 AND related_order_id='O-SVC-CUSTOMER'"
    )
    customer_descriptions = [str(row["description"] or "") for row in cur.fetchall()]
    assert customer_descriptions
    assert all(
        token not in description
        for description in customer_descriptions
        for token in ("直属服务方", "直属卖方", "上级服务商")
    )


def test_non_admin_pricing_frontends_have_no_upstream_contract_fields():
    paths = [
        "frontend/src/pages/Wallet/BuyCreditsSSOT.tsx",
        "frontend/src/pages/Customer/BuyCredit.tsx",
        "frontend/src/pages/Customer/CreditWallet.tsx",
        "frontend/src/pages/Agent/InventoryCenter.tsx",
        "frontend/src/pages/Agent/ProcurementSSOT.tsx",
        "frontend/src/hooks/usePricingSSOT.ts",
        "frontend/src/lib/v35w3Api.ts",
        "frontend/src/context/WalletContext.tsx",
    ]
    forbidden_tokens = (
        "service_account_code",
        "channel_account_code",
        "直属卖方",
        "直属服务方",
        "绑定服务方",
        "联系服务方",
        "上级服务商",
    )
    leaked = {}
    for relative_path in paths:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        found = [token for token in forbidden_tokens if token in source]
        if found:
            leaked[relative_path] = found
    assert leaked == {}
