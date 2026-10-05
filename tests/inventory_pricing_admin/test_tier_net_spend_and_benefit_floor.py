"""Service-provider tier progress and procurement-package guard regressions."""

import os
from decimal import Decimal

import psycopg2
import psycopg2.extras
import pytest
from pydantic import ValidationError

from api.admin_factory_api import InventoryCatalogPutRequest
from services.channel_tier import (
    compute_purchase_bonus_projection,
    compute_rolling_12m_yuan,
    get_agent_channel_tier_state,
)
from services.agent_inventory_pricing import build_purchase_snapshot
from services.pricing_publication import _build_procurement_plan


TIER_CONFIG = {
    "certified": {"is_enabled": True, "min_yuan": 500, "bonus_rate": 0.10},
    "preferred": {"is_enabled": True, "min_yuan": 30_000, "bonus_rate": 0.15},
    "strategic": {"is_enabled": True, "min_yuan": 100_000, "bonus_rate": 0.30},
}


def _connect():
    return psycopg2.connect(
        os.environ["TEST_DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def test_rolling_progress_uses_net_paid_cash_after_effective_refunds():
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO recharge_orders(
                id,user_id,amount_cents,payment_status,paid_at,created_at,order_type,
                refund_status,refunded_amount_cents
            ) VALUES
                ('paid-partial-refund',7,40000,'paid',NOW(),NOW(),'agent_inventory_purchase','completed',10000),
                ('paid-pending-refund',7,20000,'paid',NOW(),NOW(),'agent_inventory_purchase','pending',20000),
                ('unpaid',7,90000,'pending',NULL,NOW(),'agent_inventory_purchase',NULL,NULL),
                ('old-paid',7,100000,'paid',NOW()-INTERVAL '13 months',NOW()-INTERVAL '13 months','agent_inventory_purchase',NULL,NULL)
            """
        )
        assert compute_rolling_12m_yuan(cur, 7) == Decimal("500")


def test_benefit_floor_never_caps_natural_upgrade_and_crossing_order_uses_new_rate(monkeypatch):
    monkeypatch.setattr("config.pricing_config.get_agent_tier_config", lambda: TIER_CONFIG)
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_TIER_ENABLED'")
        cur.execute(
            """
            INSERT INTO agent_channel_tier_state(
                agent_user_id,channel_tier,rolling_12m_yuan,tier_override
            ) VALUES (7,'preferred',99900,'preferred')
            """
        )
        cur.execute(
            """
            INSERT INTO recharge_orders(
                id,user_id,amount_cents,payment_status,paid_at,created_at,order_type
            ) VALUES ('natural-progress',7,9990000,'paid',NOW(),NOW(),'agent_inventory_purchase')
            """
        )

        state = get_agent_channel_tier_state(cur, 7)
        assert state["natural_tier"] == "preferred"
        assert state["effective_tier"] == "preferred"
        assert state["benefit_floor_tier"] == "preferred"
        assert state["next_tier"] == "strategic"
        assert state["gap_to_next_yuan"] == 100

        projection = compute_purchase_bonus_projection(
            cur,
            7,
            10_000,
            rolling_before_yuan=Decimal("99900"),
            pricing_config={"agent_tier_config": TIER_CONFIG},
            base_points=1000,
        )
        assert projection["effective_before_tier"] == "preferred"
        assert projection["projected_tier"] == "strategic"
        assert projection["crosses_tier_threshold"] is True
        assert projection["bonus_rate_bps"] == 3000
        assert projection["bonus_points"] == 300

        cur.execute(
            "UPDATE recharge_orders SET amount_cents=10000000 WHERE id='natural-progress'"
        )
        upgraded = get_agent_channel_tier_state(cur, 7)
        assert upgraded["natural_tier"] == "strategic"
        assert upgraded["effective_tier"] == "strategic"
        assert upgraded["benefit_floor_tier"] == "preferred"
        assert upgraded["effective_bonus_rate"] == 0.30


def test_crossing_projection_uses_actual_buyer_cash_not_platform_reference(monkeypatch):
    monkeypatch.setattr("config.pricing_config.get_agent_tier_config", lambda: TIER_CONFIG)
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_TIER_ENABLED'")
        cur.execute(
            """
            INSERT INTO recharge_orders(
                id,user_id,amount_cents,payment_status,paid_at,created_at,order_type
            ) VALUES ('before-crossing',7,49000,'paid',NOW(),NOW(),'agent_inventory_purchase')
            """
        )
        snapshot = build_purchase_snapshot(
            cur,
            config={
                "wholesale_numer": 225,
                "wholesale_denom": 325,
                "agent_purchase_bonus_rate": 0,
                "agent_tier_config": TIER_CONFIG,
                "founding": {},
                "bonus_validity_months": 12,
            },
            catalog_version="test",
            agent_user_id=7,
            amount_cents=500,
            progress_amount_cents=1200,
            option=None,
        )
        assert snapshot["base_points"] == 722
        assert snapshot["tier_progress_amount_cents"] == 1200
        assert snapshot["projected_rolling_12m_yuan_snapshot"] == "502"
        assert snapshot["tier_at_order"] == "certified"
        assert snapshot["crosses_tier_threshold"] is True
        assert snapshot["tier_bonus_rate_bps"] == 1000


def test_more_than_three_enabled_fixed_procurement_options_fail_new_admin_write():
    options = [
        {
            "option_id": f"apo_fixed_{index}",
            "amount_cents": index * 10_000,
            "is_enabled": True,
            "sort_order": index,
        }
        for index in range(1, 5)
    ]
    with pytest.raises(ValidationError, match="最多启用 3 个固定进货档位"):
        InventoryCatalogPutRequest(
            expected_catalog_version="agent-purchase-v1",
            options=options,
        )

    legacy_config = {
        "agent_purchase_catalog_version": "agent-purchase-v1",
        "agent_purchase_options": options,
        "wholesale_numer": 225,
        "wholesale_denom": 325,
        "agent_purchase_bonus_rate": 0,
        "agent_tier_config": TIER_CONFIG,
    }
    # A pre-existing catalog remains publishable until an operator replaces it.
    # This keeps a control-plane cleanup from becoming a deployment outage.
    assert len(_build_procurement_plan(legacy_config, 1)["entries"]) == 4


def test_disabled_historical_options_do_not_count_toward_three_fixed_slots():
    options = [
        {
            "option_id": f"apo_slot_{index}",
            "amount_cents": index * 10_000,
            "is_enabled": index <= 3,
            "sort_order": index,
        }
        for index in range(1, 6)
    ]
    request = InventoryCatalogPutRequest(
        expected_catalog_version="agent-purchase-v1",
        options=options,
    )
    assert sum(1 for row in request.options if row.is_enabled) == 3
