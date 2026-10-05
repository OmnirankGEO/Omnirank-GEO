"""C-end custom recharge keeps cash fixed and inherits provider pricing."""

from __future__ import annotations

from tests.pricing_quote_wiring._single_ledger import (
    assert_single_ledger_settlement,
    read_settlement_facts,
)
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from db.connection import get_db
from db.wallet_db import complete_recharge, create_recharge_order
from services import account_codes, channel_pricing, price_quote
from services.legal_agreements import record_purchase_acceptance
from services.price_quote import QuoteError

from helpers import publish_retail, scalar, set_flags


CUSTOM_PRODUCT = "RETAIL_CUSTOM_AMOUNT"


def _set_root_ratio(*, markup_bps: int) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO system_settings(key,value,value_type,description)
               VALUES ('pricing_config',%s,'json','retail custom cash test')
               ON CONFLICT (key) DO UPDATE
               SET value=EXCLUDED.value,updated_at=NOW()""",
            (json.dumps({
                "wholesale_numer": 100,
                "wholesale_denom": 130,
                "agent_purchase_catalog_version": "procurement-test-v1",
            }),),
        )
        cur.execute(
            "UPDATE users SET agent_sku_markup_ratio=%s WHERE id=100",
            (markup_bps / 10000,),
        )


def _prepare_customer_route(*, chain_multipliers: tuple[int, ...]) -> tuple[str, str]:
    prepared = account_codes.prepare_account_codes(
        service_user_ids=[100], channel_user_ids=[]
    )
    scope_key = str(next(
        row["code"] for row in prepared["prepared"] if row["kind"] == "service"
    ))
    # The custom amount quote uses the current retail publication as its version
    # boundary, but does not borrow a fixed package's price or points.
    publish_retail(
        scope_key,
        version_code="retail-custom-v1",
        retail_cents=1_000_000,
        points=195_000,
        agent_user_id=100,
    )
    if len(chain_multipliers) >= 1:
        channel_pricing.create_relationship(
            buyer_dealer_id=100,
            upstream_channel_account_id=200,
            relationship_version="custom-chain-100-v1",
            cost_multiplier_bps=chain_multipliers[0],
        )
    if len(chain_multipliers) >= 2:
        channel_pricing.create_relationship(
            buyer_dealer_id=200,
            upstream_channel_account_id=300,
            relationship_version="custom-chain-200-v1",
            cost_multiplier_bps=chain_multipliers[1],
        )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO customer_agent_bindings(
                   customer_user_id,agent_user_id,binding_source,bound_at)
               VALUES (400,100,'admin_manual',NOW())"""
        )
        acceptance = record_purchase_acceptance(
            cur,
            user_id=400,
            ip_address="127.0.0.1",
            user_agent="pricing-wiring-test",
            surface="customer-recharge",
            evidence={"test_case": "retail-custom-cash-anchor"},
        )
    set_flags(dual=True, quote_required=True, channel=True)
    return scope_key, str(acceptance["acceptance_id"])


def _issue(
    *,
    markup_bps: int,
    chain_multipliers: tuple[int, ...] = (),
    idempotency_key: str,
) -> dict:
    _set_root_ratio(markup_bps=markup_bps)
    scope_key, acceptance_id = _prepare_customer_route(
        chain_multipliers=chain_multipliers
    )
    return price_quote.issue_retail_cash_quote(
        scope_key=scope_key,
        buyer_user_id=400,
        retail_seller_user_id=100,
        amount_cents=100,
        commercial_service_source="explicit_binding",
        consumer_policy_acknowledged=True,
        purchase_terms_acceptance_id=acceptance_id,
        idempotency_key=idempotency_key,
    )


@pytest.mark.parametrize(
    ("markup_bps", "chain_multipliers", "expected_points"),
    [
        (10_000, (), 130),
        (13_000, (), 100),
        (10_000, (12_000,), 108),
        (12_000, (12_000,), 90),
    ],
)
def test_customer_cash_is_fixed_while_effective_coefficients_change_points(
    markup_bps: int,
    chain_multipliers: tuple[int, ...],
    expected_points: int,
):
    quote = _issue(
        markup_bps=markup_bps,
        chain_multipliers=chain_multipliers,
        idempotency_key=f"retail-cash-{markup_bps}-{'-'.join(map(str, chain_multipliers))}",
    )

    assert int(quote["base_price_cents"]) <= 100
    assert int(quote["final_price_cents"]) == 100
    assert int(quote["points_granted"]) == expected_points
    assert int(quote["bonus_points"]) == 0
    assert quote["product_code"] == CUSTOM_PRODUCT
    snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    assert snapshot["retail_pricing_mode"] == "custom_amount"
    assert snapshot["customer_paid_cents"] == 100
    assert snapshot["points_granted"] == expected_points
    assert snapshot["sku_template_id"] is None
    assert snapshot["override_id"] is None
    assert snapshot["cash_anchor_semantic_version"] == "retail-cash-anchor-v1"
    assert snapshot["effective_cost_version"]
    assert snapshot["markup_version"]


def test_changed_markup_invalidates_unconsumed_quote_without_creating_order():
    quote = _issue(
        markup_bps=13_000,
        idempotency_key="retail-cash-stale-before-order",
    )
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE users SET agent_sku_markup_ratio=1.40 WHERE id=100"
        )

    with pytest.raises(QuoteError, match="价格配置已变化"):
        create_recharge_order(
            user_id=400,
            order_id="retail-cash-stale-order",
            amount_cents=100,
            base_points=100,
            bonus_points=0,
            payment_method="wechat_native",
            order_type="customer_recharge",
            agent_user_id=100,
            sku_template_id=None,
            override_id=None,
            binding_source="explicit_binding",
            price_quote_id=str(quote["quote_id"]),
            pricing_catalog_version=str(quote["catalog_version"]),
            quote_type="retail",
            expected_product_code=CUSTOM_PRODUCT,
        )

    assert scalar(
        "SELECT COUNT(*) FROM recharge_orders WHERE id='retail-cash-stale-order'"
    ) == 0
    assert scalar(
        "SELECT status FROM price_quotes WHERE quote_id=%s",
        (quote["quote_id"],),
    ) == "issued"


def test_digital_goods_acknowledgement_is_required_even_when_resale_is_disabled():
    _set_root_ratio(markup_bps=13_000)
    scope_key, acceptance_id = _prepare_customer_route(chain_multipliers=())

    with pytest.raises(QuoteError, match="请先确认数字商品交付与退款规则"):
        price_quote.issue_retail_cash_quote(
            scope_key=scope_key,
            buyer_user_id=400,
            retail_seller_user_id=100,
            amount_cents=100,
            commercial_service_source="explicit_binding",
            consumer_policy_acknowledged=False,
            purchase_terms_acceptance_id=acceptance_id,
            idempotency_key="retail-cash-policy-required",
        )

    assert scalar(
        """SELECT COUNT(*) FROM price_quotes
             WHERE buyer_user_id=400
               AND idempotency_key='retail-cash-policy-required'"""
    ) == 0


def test_resale_seam_receives_fixed_cash_and_already_converted_points(monkeypatch):
    from config import dealer_inventory_resale_flags
    from services import dealer_inventory_resale

    _set_root_ratio(markup_bps=12_000)
    scope_key, acceptance_id = _prepare_customer_route(
        chain_multipliers=(12_000,),
    )
    observed: dict = {}

    monkeypatch.setattr(dealer_inventory_resale_flags, "enabled", lambda _cur=None: True)

    def build_terms(_cur, **kwargs):
        observed.update(kwargs)
        return {
            "consumer_resale_mode": True,
            "sale_amount_cents": kwargs["catalog_reference_amount_cents"],
            "seller_policy_version": "jit-consumer-v1",
            "fulfillment_plan": {
                "plan_version": 2,
                "order_kind": "CONSUMER",
                "final_sale_amount_cents": kwargs["catalog_reference_amount_cents"],
            },
        }

    monkeypatch.setattr(
        dealer_inventory_resale,
        "build_consumer_quote_terms",
        build_terms,
    )
    quote = price_quote.issue_retail_cash_quote(
        scope_key=scope_key,
        buyer_user_id=400,
        retail_seller_user_id=100,
        amount_cents=100,
        commercial_service_source="explicit_binding",
        consumer_policy_acknowledged=True,
        purchase_terms_acceptance_id=acceptance_id,
        idempotency_key="retail-cash-resale-seam",
    )

    assert int(quote["final_price_cents"]) == 100
    assert int(quote["points_granted"]) == 90
    assert observed["catalog_reference_amount_cents"] == 100
    assert observed["points"] == 90
    assert observed["digital_goods_acknowledged"] is True
    snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    assert snapshot["customer_paid_cents"] == 100
    assert snapshot["consumer_resale_mode"] is True
    assert snapshot["fulfillment_plan"]["final_sale_amount_cents"] == 100


def test_pending_order_keeps_original_cash_and_points_after_markup_change():
    quote = _issue(
        markup_bps=13_000,
        idempotency_key="retail-cash-order-snapshot",
    )
    expected_snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    order = create_recharge_order(
        user_id=400,
        order_id="retail-cash-order-snapshot",
        amount_cents=100,
        base_points=100,
        bonus_points=0,
        payment_method="wechat_native",
        order_type="customer_recharge",
        agent_user_id=100,
        sku_template_id=None,
        override_id=None,
        binding_source="explicit_binding",
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]),
        quote_type="retail",
        expected_product_code=CUSTOM_PRODUCT,
    )
    assert order["amount_cents"] == 100
    assert order["base_points"] == 100
    expected_order_snapshot = {
        **expected_snapshot,
        "commercial_resolution": "BOUND",
    }
    assert order["pricing_snapshot_jsonb"] == expected_order_snapshot

    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE users SET agent_sku_markup_ratio=2.00 WHERE id=100"
        )
    assert complete_recharge("retail-cash-order-snapshot", "wx-retail-cash") is not None

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT amount_cents,base_points,bonus_points,payment_status,
                      pricing_snapshot_jsonb
                 FROM recharge_orders WHERE id='retail-cash-order-snapshot'"""
        )
        stored = dict(cur.fetchone())
        # 🔴 [#118 1-8] 继任 `2aef3b59b`(2026-07-27 单账本收敛):不再有第二本账。
        #    原断言读 customer_agent_credit_wallets(该路径已停写),改守三条不变量。
        facts = read_settlement_facts(
            cur, order_id='retail-cash-order-snapshot', customer_user_id=400)

    assert stored == {
        "amount_cents": 100,
        "base_points": 100,
        "bonus_points": 0,
        "payment_status": "paid",
        "pricing_snapshot_jsonb": expected_order_snapshot,
    }
    assert_single_ledger_settlement(facts)


def test_twenty_concurrent_requests_with_one_key_create_one_quote():
    _set_root_ratio(markup_bps=13_000)
    scope_key, acceptance_id = _prepare_customer_route(chain_multipliers=())

    def issue_once(_index: int) -> str:
        quote = price_quote.issue_retail_cash_quote(
            scope_key=scope_key,
            buyer_user_id=400,
            retail_seller_user_id=100,
            amount_cents=100,
            commercial_service_source="explicit_binding",
            consumer_policy_acknowledged=True,
            purchase_terms_acceptance_id=acceptance_id,
            idempotency_key="retail-cash-20-concurrent",
        )
        return str(quote["quote_id"])

    with ThreadPoolExecutor(max_workers=20) as pool:
        quote_ids = list(pool.map(issue_once, range(20)))

    assert len(set(quote_ids)) == 1
    assert scalar(
        """SELECT COUNT(*) FROM price_quotes
             WHERE buyer_user_id=400
               AND idempotency_key='retail-cash-20-concurrent'"""
    ) == 1


def test_public_custom_amount_quote_keeps_private_pricing_out_of_http_dto():
    from api import pricing_ssot_api

    _set_root_ratio(markup_bps=13_000)
    _scope_key, acceptance_id = _prepare_customer_route(chain_multipliers=(12_000,))
    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 400, "is_admin": False})
    )
    response = asyncio.run(
        pricing_ssot_api.retail_custom_amount_quote(
            pricing_ssot_api.RetailCustomAmountQuoteRequest(
                amount_cents=100,
                idempotency_key="retail-cash-public-dto",
                digital_goods_acknowledged=True,
                terms_acceptance_id=acceptance_id,
            ),
            request,
        )
    )

    assert response["success"] is True
    data = response["data"]
    assert data["final_price_cents"] == 100
    assert data["points_granted"] == 83
    assert data["bonus_points"] == 0
    assert data["amount_source"] == "customer_entered_cash"
    serialized = json.dumps(data, ensure_ascii=False).lower()
    for forbidden in (
        "base_price",
        "cost_basis",
        "multiplier",
        "markup",
        "margin",
        "profit",
        "upstream",
        "relationship_id",
        "relationship_fingerprint",
    ):
        assert forbidden not in serialized


def test_service_provider_cannot_call_customer_custom_recharge_endpoint():
    from api import pricing_ssot_api

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 100, "is_admin": False})
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            pricing_ssot_api.retail_custom_amount_quote(
                pricing_ssot_api.RetailCustomAmountQuoteRequest(
                    amount_cents=100,
                    idempotency_key="provider-must-use-inventory",
                    digital_goods_acknowledged=True,
                    terms_acceptance_id="acceptance-not-consumed",
                ),
                request,
            )
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {
        "code": "CUSTOMER_RECHARGE_ONLY",
        "message": "自由充值仅供普通客户使用，服务商请前往算力库存进货",
    }
