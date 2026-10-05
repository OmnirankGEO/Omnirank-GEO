"""Discriminator tests for JIT edge pricing and promotion ownership."""

from __future__ import annotations

import pytest

from services import dealer_inventory_resale as resale


def test_historical_nine_tenths_cost_does_not_lower_current_edge_price():
    sale = resale.calculate_hop_sale(100, 12000)
    assert sale == 120
    assert sale - 90 == 30


def test_platform_promotion_is_applied_once_then_only_edges_compound():
    edges = [
        {
            "seller_user_id": 1,
            "buyer_user_id": 2,
            "edge_multiplier_bps": 12000,
            "relationship_version": "r1",
            "source_kind": "platform_root",
        },
        {
            "seller_user_id": 2,
            "buyer_user_id": 3,
            "edge_multiplier_bps": 12000,
            "relationship_version": "r2",
            "source_kind": "dealer_resale",
        },
    ]
    priced = resale.price_reference_path(
        platform_reference_amount_cents=100,
        edges=edges,
        promotion={
            "campaign_id": "P90",
            "funding_scope": "PLATFORM",
            "promotion_discount_bps": 9000,
        },
    )
    assert [hop["sale_reference_cents"] for hop in priced] == [108, 130]
    assert [hop["promotion_discount_bps"] for hop in priced] == [10000, 10000]


def test_seller_promotion_reduces_only_the_final_edge():
    edges = [
        {
            "seller_user_id": 1,
            "buyer_user_id": 2,
            "edge_multiplier_bps": 12000,
            "relationship_version": "r1",
            "source_kind": "platform_root",
        },
        {
            "seller_user_id": 2,
            "buyer_user_id": 3,
            "edge_multiplier_bps": 12000,
            "relationship_version": "r2",
            "source_kind": "dealer_resale",
        },
    ]
    priced = resale.price_reference_path(
        platform_reference_amount_cents=100,
        edges=edges,
        promotion={
            "campaign_id": "S90",
            "funding_scope": "SELLER",
            "seller_user_id": 2,
            "promotion_discount_bps": 9000,
        },
    )
    assert priced[0]["sale_reference_cents"] == 120
    assert priced[0]["promotion_discount_bps"] == 10000
    assert priced[1]["sale_reference_cents"] == 130
    assert priced[1]["normal_sale_reference_cents"] == 144
    assert priced[1]["promotion_discount_bps"] == 9000


def test_funds_conservation_rejects_any_unassigned_cent():
    proof = resale.verify_funds_conservation(
        final_paid_cents=130,
        platform_root_revenue_cents=100,
        dealer_margin_cents=20,
        final_seller_margin_cents=10,
    )
    assert proof["residual_cents"] == 0
    with pytest.raises(resale.ResaleError, match="差额 1"):
        resale.verify_funds_conservation(
            final_paid_cents=131,
            platform_root_revenue_cents=100,
            dealer_margin_cents=20,
            final_seller_margin_cents=10,
        )


def test_real_133_to_29_edge_uses_relationship_12000_not_policy_default(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO users(id,username) VALUES (1,'platform'),(29,'upstream'),(133,'buyer')"
    )
    cur.execute(
        "INSERT INTO user_wallets(user_id,agent_level) VALUES (1,1),(29,1),(133,1)"
    )
    cur.execute(
        """UPDATE dealer_resale_global_settings
           SET platform_seller_user_id=1,default_downstream_markup_bps=10000"""
    )
    cur.execute(
        """INSERT INTO dealer_resale_policies
           (seller_user_id,downstream_markup_bps,authorized_min_markup_bps,
            authorized_max_markup_bps,policy_version)
           VALUES (29,10000,10000,20000,'policy-default')"""
    )
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,
            cost_multiplier_bps,status)
           VALUES (133,29,'real-133-29-v1',12000,'active')"""
    )
    edges = resale.resolve_fulfillment_edges(
        cur, final_buyer_user_id=133, platform_seller_user_id=1
    )
    assert [(e["seller_user_id"], e["buyer_user_id"], e["edge_multiplier_bps"])
            for e in edges] == [(1, 29, 10000), (29, 133, 12000)]
    assert edges[-1]["relationship_version"] == "real-133-29-v1"
