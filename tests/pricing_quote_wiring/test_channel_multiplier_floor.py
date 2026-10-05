"""Regression proof for the channel multiplier/revenue floor review finding."""

from __future__ import annotations

import os

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest
from pydantic import ValidationError

from api.pricing_ssot_api import ChannelRelChange
from db.connection import get_db
from services import account_codes, channel_pricing, price_quote
from services.channel_pricing import ChannelError

from helpers import PLATFORM_PRODUCT, publish_procurement, set_flags


def _relationship(multiplier: int, version: str = "floor-v1") -> dict:
    return {
        "buyer_dealer_id": 100,
        "upstream_channel_account_id": 200,
        "expected_relationship_version": None,
        "new_relationship_version": version,
        "cost_multiplier_bps": multiplier,
        "reason": "explicit owner-approved floor fixture",
    }


def test_api_and_service_reject_multiplier_below_one():
    with pytest.raises(ValidationError):
        ChannelRelChange(**_relationship(9999))

    with pytest.raises(ChannelError, match="10000"):
        channel_pricing.save_relationships(
            [_relationship(9999)], approved_by=900, created_by=900
        )


def test_database_rejects_relationship_and_ledger_below_cost():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SAVEPOINT multiplier_floor")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """INSERT INTO channel_pricing_relationships
                   (buyer_dealer_id, upstream_channel_account_id,
                    relationship_version, cost_multiplier_bps)
                   VALUES (100, 200, 'direct-below-floor', 9999)"""
            )
        cur.execute("ROLLBACK TO SAVEPOINT multiplier_floor")

        cur.execute("SAVEPOINT revenue_floor")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                """INSERT INTO channel_revenue_ledger
                   (channel_beneficiary_user_id, buyer_dealer_id, recharge_order_id,
                    upstream_cost_basis_cents, buyer_paid_cents, channel_revenue_cents)
                   VALUES (200, 100, 'direct-negative-revenue', 120000, 119999, -1)"""
            )
        cur.execute("ROLLBACK TO SAVEPOINT revenue_floor")


def test_existing_below_floor_relationship_fails_readiness_closed():
    conn = psycopg2.connect(
        os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor
    )
    try:
        cur = conn.cursor()
        # Simulate a pre-constraint legacy row inside a rollback-only transaction.
        cur.execute(
            "ALTER TABLE channel_pricing_relationships "
            "DROP CONSTRAINT chk_channel_relationship_multiplier_floor, "
            "DROP CONSTRAINT channel_pricing_relationships_cost_multiplier_bps_check"
        )
        cur.execute(
            """INSERT INTO channel_pricing_relationships
               (buyer_dealer_id, upstream_channel_account_id,
                relationship_version, cost_multiplier_bps)
               VALUES (100, 200, 'legacy-below-floor', 9999)"""
        )
        status = channel_pricing.relationship_status(cur=cur)
        assert status["ready"] is False
        assert status["invalid_multiplier_relationship_ids"]
        assert any("10000" in blocker for blocker in status["blockers"])
        with pytest.raises(ChannelError, match="低于 10000"):
            channel_pricing.resolve_effective_cost_basis(100, 120000, cur=cur)
        conn.rollback()
    finally:
        conn.close()


def test_equal_one_records_zero_revenue_and_markup_records_positive_revenue():
    publish_procurement(version_code="proc-floor-v1")
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    set_flags(dual=True, quote_required=True, channel=True)

    channel_pricing.save_relationships(
        [_relationship(10000, "floor-v1")], approved_by=900, created_by=900
    )
    zero_quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="floor-zero-quote",
    )
    assert zero_quote["final_price_cents"] == 120000
    assert zero_quote["upstream_cost_basis_cents"] == 120000

    with get_db() as conn:
        cur = conn.cursor()
        zero_id = channel_pricing.record_channel_revenue(
            cur,
            recharge_order_id="floor-zero-order",
            buyer_dealer_id=100,
            beneficiary_user_id=200,
            upstream_cost_basis_cents=120000,
            buyer_paid_cents=120000,
            relationship_version="floor-v1",
            price_quote_id=str(zero_quote["quote_id"]),
            catalog_version=str(zero_quote["catalog_version"]),
        )
        assert zero_id is not None
        cur.execute(
            "SELECT channel_revenue_cents FROM channel_revenue_ledger "
            "WHERE recharge_order_id='floor-zero-order'"
        )
        assert int(cur.fetchone()["channel_revenue_cents"]) == 0

    channel_pricing.save_relationships(
        [{
            **_relationship(11000, "floor-v2"),
            "expected_relationship_version": "floor-v1",
        }],
        approved_by=900,
        created_by=900,
    )
    positive_quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="floor-positive-quote",
    )
    expected_points = 120000 * 195000 * 10000 // (120000 * 11000)
    expected_cost = (expected_points * 120000 + 195000 - 1) // 195000
    assert positive_quote["final_price_cents"] == 120000
    assert positive_quote["points_granted"] == expected_points
    assert positive_quote["upstream_cost_basis_cents"] == expected_cost

    with get_db() as conn:
        cur = conn.cursor()
        positive_id = channel_pricing.record_channel_revenue(
            cur,
            recharge_order_id="floor-positive-order",
            buyer_dealer_id=100,
            beneficiary_user_id=200,
            upstream_cost_basis_cents=expected_cost,
            buyer_paid_cents=120000,
            relationship_version="floor-v2",
            price_quote_id=str(positive_quote["quote_id"]),
            catalog_version=str(positive_quote["catalog_version"]),
        )
        assert positive_id is not None
        cur.execute(
            "SELECT channel_revenue_cents FROM channel_revenue_ledger "
            "WHERE recharge_order_id='floor-positive-order'"
        )
        assert int(cur.fetchone()["channel_revenue_cents"]) == 120000 - expected_cost

    with get_db() as conn:
        with pytest.raises(ChannelError, match="负渠道收益"):
            channel_pricing.record_channel_revenue(
                conn.cursor(),
                recharge_order_id="floor-negative-service-order",
                buyer_dealer_id=100,
                beneficiary_user_id=200,
                upstream_cost_basis_cents=120000,
                buyer_paid_cents=119999,
            )
