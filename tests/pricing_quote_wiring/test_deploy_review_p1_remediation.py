"""Regression proof for the two Deploy-review P1 findings."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from config import pricing_ssot_flags
from db.connection import get_db
from db.wallet_db import complete_recharge
from services import account_codes, channel_pricing, price_quote
from services.agent_inventory_pricing import FREE_AMOUNT_OPTION_ID, points_for_amount
from services.pricing_readiness import _check_entry_wiring

from helpers import publish_procurement, scalar, set_flags


def _agent_request() -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/agent/inventory/purchase-preview",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })
    request.state.user = {"user_id": 100, "is_admin": False}
    return request


def _customer_request() -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/wallet/recharge",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50001),
    })
    request.state.user = {"user_id": 400, "is_admin": False}
    return request


def _save_relationship() -> None:
    channel_pricing.save_relationships([{
        "buyer_dealer_id": 100,
        "upstream_channel_account_id": 200,
        "expected_relationship_version": None,
        "new_relationship_version": "custom-rel-v1",
        "cost_multiplier_bps": 11000,
        "reason": "explicit owner-approved custom amount fixture",
    }], approved_by=900, created_by=900)


def test_readiness_uses_active_geo_behavior_contract_and_social_is_warning_only(monkeypatch):
    original_read_text = Path.read_text
    paths_read: list[Path] = []

    def guarded_read_text(path: Path, *args, **kwargs):
        paths_read.append(path)
        if path.suffix.lower() in {".tsx", ".ts"}:
            raise AssertionError("readiness must not parse frontend source text")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    result = _check_entry_wiring()

    assert result["ready"] is True
    assert result["problems"] == []
    assert {entry["entry_id"] for entry in result["active_geo_entries"]} == {
        "customer-recharge",
        "agent-inventory-purchase",
    }
    assert all(entry["submits_persisted_price_quote_id"] for entry in result["active_geo_entries"])
    assert any("社媒" in warning and "不纳入" in warning for warning in result["warnings"])
    assert result["active_legacy_bypasses"] == []
    assert any(path.name == "pricing-entry-contracts.json" for path in paths_read)


def test_readiness_rejects_structured_geo_contract_tamper(monkeypatch):
    contract_path = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/contracts/pricing-entry-contracts.json"
    )
    document = json.loads(contract_path.read_text(encoding="utf-8"))
    document["entries"][0]["required_order_field"] = "sku_template_id"
    tampered = json.dumps(document, ensure_ascii=False)
    original_read_text = Path.read_text

    def read_tampered(path: Path, *args, **kwargs):
        if path.name == "pricing-entry-contracts.json":
            return tampered
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_tampered)
    result = _check_entry_wiring()
    assert result["ready"] is False
    assert any("required_order_field" in problem for problem in result["problems"])


def test_quote_required_blocks_real_geo_requests_before_writers():
    from api.agent_workbench_api import (
        AgentPurchaseWiringRequest,
        agent_inventory_purchase,
    )
    from api.wallet_api import RechargeRequest, create_recharge

    set_flags(dual=True, quote_required=True, channel=False)
    with pytest.raises(HTTPException) as customer_error:
        asyncio.run(create_recharge(
            RechargeRequest(sku_template_id=1, channel="auto"),
            _customer_request(),
        ))
    assert customer_error.value.status_code == 422
    assert customer_error.value.detail["code"] == "QUOTE_REQUIRED"

    with pytest.raises(HTTPException) as agent_error:
        asyncio.run(agent_inventory_purchase(
            AgentPurchaseWiringRequest(amount_cents=123457, channel="auto"),
            _agent_request(),
        ))
    assert agent_error.value.status_code == 422
    assert agent_error.value.detail["code"] == "QUOTE_REQUIRED"
    assert scalar("SELECT COUNT(*) FROM recharge_orders") == 0


def test_dual_preview_accepts_free_amount_and_persists_procurement_quote():
    from api.agent_workbench_api import (
        AgentPurchasePreviewWiringRequest,
        agent_inventory_purchase_preview,
    )

    publish_procurement()
    set_flags(dual=True, quote_required=False, channel=False)

    preview = asyncio.run(agent_inventory_purchase_preview(
        AgentPurchasePreviewWiringRequest(
            amount_cents=123457,
            idempotency_key="custom-preview-123457",
        ),
        _agent_request(),
    ))

    assert preview.price_quote_id
    assert preview.option_id == FREE_AMOUNT_OPTION_ID
    assert preview.product_code == FREE_AMOUNT_OPTION_ID
    assert preview.amount_cents == 123457
    assert preview.base_points == points_for_amount(123457, 120000, 195000)
    assert scalar(
        "SELECT COUNT(*) FROM price_quotes WHERE quote_id=%s AND quote_type='procurement'",
        (preview.price_quote_id,),
    ) == 1


def test_free_amount_quote_order_callback_is_snapshot_only(monkeypatch):
    from api.agent_workbench_api import _create_quoted_inventory_order
    from services import agent_inventory_pricing

    requested_base_cents = 123457
    publish_procurement()
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    _save_relationship()
    set_flags(dual=True, quote_required=True, channel=True)

    quote = price_quote.issue_procurement_custom_amount_quote(
        dealer_id=100,
        requested_amount_cents=requested_base_cents,
        idempotency_key="custom-quote-snapshot",
    )
    expected_points = requested_base_cents * 195000 * 10000 // (120000 * 11000)
    expected_upstream_cost = (expected_points * 120000 + 195000 - 1) // 195000
    assert quote["product_code"] == FREE_AMOUNT_OPTION_ID
    assert quote["base_price_cents"] == requested_base_cents
    assert quote["final_price_cents"] == requested_base_cents
    assert quote["points_granted"] == expected_points
    assert quote["catalog_version_id"] is not None
    retry = price_quote.issue_procurement_custom_amount_quote(
        dealer_id=100,
        requested_amount_cents=requested_base_cents,
        idempotency_key="custom-quote-snapshot",
    )
    assert retry["quote_id"] == quote["quote_id"]

    monkeypatch.setattr(
        agent_inventory_pricing,
        "inventory_snapshot_activation_state",
        lambda _cur: {"ready": True},
    )
    order_id, snapshot, reused = _create_quoted_inventory_order(
        agent_user_id=100,
        quote_id=str(quote["quote_id"]),
        order_id="custom-amount-order",
        idempotency_key="custom-amount-order-idem",
    )
    assert reused is False
    assert snapshot["option_source"] == "custom_amount"
    assert snapshot["cash_anchor_semantic_version"] == "procurement-cash-anchor-v2"
    assert snapshot["payable_amount_cents"] == requested_base_cents
    assert snapshot["amount_cents"] == requested_base_cents
    assert snapshot["channel_beneficiary_user_id"] == 200
    assert snapshot["upstream_cost_basis_cents"] == expected_upstream_cost

    # Later catalog, flag and relationship changes cannot affect callback settlement.
    publish_procurement(version_code="proc-v2", amount_cents=130000, points=210000)
    set_flags(dual=True, quote_required=True, channel=False)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE channel_pricing_relationships SET status='archived', effective_to=NOW() "
            "WHERE buyer_dealer_id=100"
        )

    complete_recharge(order_id, "wx-custom-amount")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM recharge_orders WHERE id=%s", (order_id,))
        stored_order = dict(cur.fetchone())
        cur.execute("SELECT * FROM agent_inventory_wallets WHERE agent_user_id=100")
        inventory = dict(cur.fetchone())
        cur.execute(
            "SELECT * FROM channel_revenue_ledger WHERE recharge_order_id=%s",
            (order_id,),
        )
        revenue = dict(cur.fetchone())

    assert stored_order["pricing_snapshot_jsonb"] == snapshot
    assert inventory["paid_inventory_points"] == expected_points
    assert revenue["channel_beneficiary_user_id"] == 200
    assert revenue["upstream_cost_basis_cents"] == expected_upstream_cost
    assert revenue["buyer_paid_cents"] == requested_base_cents
    assert revenue["channel_revenue_cents"] == requested_base_cents - expected_upstream_cost
