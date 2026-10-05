"""Discriminating tests for amount-anchored provider procurement."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import psycopg2
import psycopg2.extras
from fastapi import HTTPException
from starlette.requests import Request

from db.connection import get_db
from db.wallet_db import complete_recharge, create_recharge_order
from services import account_codes, channel_pricing, price_quote
from services.procurement_cash_anchor import CashAnchorError, calculate_cash_anchor
from services.pricing_catalog import apply_multiplier_cents

from helpers import PLATFORM_PRODUCT, publish_procurement, scalar, set_flags


def _path(*multipliers: int):
    actors = [900, 300, 200, 100]
    return [
        {
            "seller_user_id": actors[index],
            "buyer_user_id": actors[index + 1],
            "relationship_id": index + 1,
            "relationship_version": f"rel-v{index + 1}",
            "source_kind": "test",
            "multiplier_bps": multiplier,
        }
        for index, multiplier in enumerate(multipliers)
    ]


@pytest.mark.parametrize(
    ("multipliers", "expected_points"),
    [
        ((), 130),
        ((12000,), 108),
        ((13000,), 100),
        ((12000, 12000), 90),
    ],
)
def test_one_yuan_cash_is_immutable_and_only_points_change(multipliers, expected_points):
    result = calculate_cash_anchor(
        buyer_user_id=100,
        payable_amount_cents=100,
        platform_points_numer=130,
        platform_points_denom=100,
        catalog_version="proc-cash-v1",
        relationship_path=_path(*multipliers),
    )

    assert result["payable_amount_cents"] == 100
    assert result["paid_inventory_points"] == expected_points
    assert result["covered_path_cost_cents"] <= 100
    assert result["rounding_remainder_cents"] >= 0
    assert all(
        int(hop["sale_reference_cents"]) >= int(hop["standard_reference_cents"])
        for hop in result["hop_costs"]
    )


def test_fixed_five_hundred_yuan_stays_five_hundred_yuan():
    result = calculate_cash_anchor(
        buyer_user_id=100,
        payable_amount_cents=50_000,
        platform_points_numer=130,
        platform_points_denom=100,
        catalog_version="proc-cash-v1",
        relationship_path=_path(12000, 12000),
    )
    assert result["payable_amount_cents"] == 50_000
    assert result["paid_inventory_points"] == 45_138


def test_persisted_fixed_five_hundred_yuan_quote_keeps_cash_with_two_levels():
    publish_procurement(
        version_code="proc-cash-fixed-500-v1",
        amount_cents=50_000,
        points=65_000,
    )
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    channel_pricing.save_relationships([{
        "buyer_dealer_id": 200, "upstream_channel_account_id": 300,
        "expected_relationship_version": None,
        "new_relationship_version": "rel-fixed-root-v1",
        "cost_multiplier_bps": 12000, "reason": "fixed cash anchor root fixture",
    }], approved_by=900, created_by=900)
    _save_relationship(version="rel-fixed-buyer-v1", multiplier=12000)
    set_flags(dual=True, quote_required=True, channel=True)

    quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-fixed-500",
    )
    assert quote["base_price_cents"] == quote["final_price_cents"] == 50_000
    assert quote["points_granted"] == 45_138


def test_amount_too_small_for_one_point_fails_before_any_persistence():
    with pytest.raises(CashAnchorError) as caught:
        calculate_cash_anchor(
            buyer_user_id=100,
            payable_amount_cents=1,
            platform_points_numer=1,
            platform_points_denom=100,
            catalog_version="proc-cash-v1",
            relationship_path=_path(13000),
        )
    assert caught.value.code == "CASH_ANCHOR_POINTS_TOO_SMALL"


def test_tiny_amount_api_returns_422_with_zero_quote_and_zero_order():
    from api.agent_workbench_api import (
        AgentPurchasePreviewWiringRequest,
        agent_inventory_purchase_preview,
    )

    publish_procurement(
        version_code="proc-cash-tiny-v1", amount_cents=100, points=1,
    )
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    _save_relationship(version="rel-cash-tiny-v1", multiplier=13000)
    set_flags(dual=True, quote_required=True, channel=True)
    request = Request({
        "type": "http", "method": "POST",
        "path": "/api/agent/inventory/purchase-preview", "headers": [],
        "query_string": b"", "scheme": "http", "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })
    request.state.user = {"user_id": 100, "is_admin": False}

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(agent_inventory_purchase_preview(
            AgentPurchasePreviewWiringRequest(
                amount_cents=1, idempotency_key="cash-anchor-tiny",
            ),
            request,
        ))
    assert rejected.value.status_code == 422
    assert scalar("SELECT COUNT(*) FROM price_quotes") == 0
    assert scalar("SELECT COUNT(*) FROM recharge_orders") == 0


def test_old_cash_multiplication_is_the_red_discriminator():
    assert apply_multiplier_cents(100, 13000) == 130
    result = calculate_cash_anchor(
        buyer_user_id=100,
        payable_amount_cents=100,
        platform_points_numer=130,
        platform_points_denom=100,
        catalog_version="proc-cash-v1",
        relationship_path=_path(13000),
    )
    assert result["payable_amount_cents"] == 100
    assert result["payable_amount_cents"] != apply_multiplier_cents(100, 13000)


def test_calculator_output_is_integer_only_and_fingerprint_changes_with_relationship():
    first = calculate_cash_anchor(
        buyer_user_id=100,
        payable_amount_cents=123_457,
        platform_points_numer=195_000,
        platform_points_denom=120_000,
        catalog_version="proc-cash-v1",
        relationship_path=_path(12000),
    )
    second = calculate_cash_anchor(
        buyer_user_id=100,
        payable_amount_cents=123_457,
        platform_points_numer=195_000,
        platform_points_denom=120_000,
        catalog_version="proc-cash-v1",
        relationship_path=_path(13000),
    )

    def assert_no_float(value):
        if isinstance(value, dict):
            for child in value.values():
                assert_no_float(child)
        elif isinstance(value, list):
            for child in value:
                assert_no_float(child)
        else:
            assert not isinstance(value, float)

    assert_no_float(first)
    assert first["cash_anchor_fingerprint"] != second["cash_anchor_fingerprint"]


def _save_relationship(*, version: str, multiplier: int, expected: str | None = None):
    return channel_pricing.save_relationships([{
        "buyer_dealer_id": 100,
        "upstream_channel_account_id": 200,
        "expected_relationship_version": expected,
        "new_relationship_version": version,
        "cost_multiplier_bps": multiplier,
        "reason": "cash-anchor discriminating fixture",
    }], approved_by=900, created_by=900)


def _set_wholesale_override(
    *, agent_user_id: int = 100, numer: int = 100, denom: int = 200,
) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO agent_pricing_overrides(
                     agent_user_id,wholesale_numer,wholesale_denom,updated_at
                   ) VALUES (%s,%s,%s,NOW())
                   ON CONFLICT(agent_user_id) DO UPDATE SET
                     wholesale_numer=EXCLUDED.wholesale_numer,
                     wholesale_denom=EXCLUDED.wholesale_denom,
                     updated_at=NOW()""",
            (int(agent_user_id), int(numer), int(denom)),
        )


def test_agent_override_replaces_published_root_ratio_without_changing_cash():
    publish_procurement(version_code="proc-override-root-v1", amount_cents=100, points=130)
    _set_wholesale_override(numer=100, denom=200)
    set_flags(dual=True, quote_required=True, channel=False)

    quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-agent-override-root",
    )
    snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    discount = snapshot["purchase_discount_snapshot"]
    assert quote["base_price_cents"] == quote["final_price_cents"] == 100
    assert quote["points_granted"] == 200
    assert snapshot["discount_source"] == "agent_override"
    assert (snapshot["discount_numer"], snapshot["discount_denom"]) == (100, 200)
    assert (discount["numer"], discount["denom"]) == (100, 200)
    assert snapshot["purchase_discount_version"] == discount["discount_version"]
    assert discount["source"] == "agent_override"
    assert discount["override_updated_at"]
    assert snapshot["cash_anchor_semantic_version"] == "procurement-cash-anchor-v2"

    global_quote = price_quote.issue_procurement_quote(
        dealer_id=200,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-global-peer",
    )
    global_snapshot = price_quote.quote_order_pricing_snapshot(global_quote, required=True)
    assert global_quote["final_price_cents"] == 100
    assert global_quote["points_granted"] == 130
    assert global_snapshot["purchase_discount_snapshot"]["source"] == "published_global"


def test_agent_override_is_applied_before_one_and_two_level_channel_paths():
    publish_procurement(version_code="proc-override-chain-v1", amount_cents=100, points=130)
    _set_wholesale_override(numer=100, denom=200)
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200, 300])
    channel_pricing.save_relationships([{
        "buyer_dealer_id": 200,
        "upstream_channel_account_id": 300,
        "expected_relationship_version": None,
        "new_relationship_version": "override-chain-root-v1",
        "cost_multiplier_bps": 12000,
        "reason": "dedicated discount chain fixture",
    }], approved_by=900, created_by=900)
    _save_relationship(version="override-chain-buyer-v1", multiplier=12000)
    set_flags(dual=True, quote_required=True, channel=True)

    two_level = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-agent-override-two-level",
    )
    assert two_level["final_price_cents"] == 100
    assert two_level["points_granted"] == 138  # floor(100 * 2 / 1.44)

    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE channel_pricing_relationships SET status='archived',effective_to=NOW() "
            "WHERE buyer_dealer_id=200"
        )
    one_level = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-agent-override-one-level",
    )
    assert one_level["final_price_cents"] == 100
    assert one_level["points_granted"] == 166  # floor(100 * 2 / 1.20)


def test_fixed_five_hundred_uses_agent_override_then_two_level_channel_path():
    publish_procurement(
        version_code="proc-override-fixed-500-v1", amount_cents=50_000, points=65_000,
    )
    _set_wholesale_override(numer=100, denom=200)
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200, 300])
    channel_pricing.save_relationships([{
        "buyer_dealer_id": 200,
        "upstream_channel_account_id": 300,
        "expected_relationship_version": None,
        "new_relationship_version": "override-fixed-root-v1",
        "cost_multiplier_bps": 12000,
        "reason": "fixed dedicated discount chain fixture",
    }], approved_by=900, created_by=900)
    _save_relationship(version="override-fixed-buyer-v1", multiplier=12000)
    set_flags(dual=True, quote_required=True, channel=True)

    quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-agent-override-fixed-500",
    )
    assert quote["base_price_cents"] == quote["final_price_cents"] == 50_000
    assert quote["points_granted"] == 69_444  # floor(50000 * 2 / 1.44)


def test_override_change_expires_idempotent_quote_and_requotes_new_points():
    publish_procurement(version_code="proc-override-stale-v1", amount_cents=100, points=130)
    _set_wholesale_override(numer=100, denom=200)
    set_flags(dual=True, quote_required=True, channel=False)
    first = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-agent-override-stale",
    )
    assert first["points_granted"] == 200

    with get_db() as conn:
        conn.cursor().execute(
            """UPDATE agent_pricing_overrides
                  SET wholesale_numer=110,wholesale_denom=200,
                      updated_at=updated_at + INTERVAL '1 second'
                WHERE agent_user_id=100"""
        )
    with get_db() as conn:
        with pytest.raises(price_quote.QuoteError, match="专属进货折扣已变化"):
            price_quote.lock_and_validate(
                conn.cursor(), str(first["quote_id"]),
                buyer_user_id=100, quote_type="procurement",
            )

    second = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-agent-override-stale",
    )
    assert second["quote_id"] != first["quote_id"]
    assert second["final_price_cents"] == 100
    assert second["points_granted"] == 181
    assert scalar(
        "SELECT status FROM price_quotes WHERE quote_id=%s", (first["quote_id"],)
    ) == "expired"


def test_missing_override_row_is_locked_against_concurrent_first_insert():
    from services.agent_inventory_pricing import resolve_procurement_discount_snapshot

    published = {
        "wholesale_numer": 100,
        "wholesale_denom": 130,
    }
    conn = psycopg2.connect(
        os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor,
    )
    cur = conn.cursor()
    before = resolve_procurement_discount_snapshot(
        cur, 100, published,
        published_catalog_version="proc-first-insert-v1",
        require_override_schema=True,
    )
    assert before["source"] == "published_global"

    writer_started = threading.Event()

    def insert_override() -> None:
        import psycopg2

        with psycopg2.connect(os.environ["DATABASE_URL"]) as writer:
            writer_cur = writer.cursor()
            writer_started.set()
            writer_cur.execute("SELECT pg_advisory_xact_lock(920713, 1)")
            writer_cur.execute(
                "INSERT INTO agent_pricing_overrides"
                "(agent_user_id,wholesale_numer,wholesale_denom,updated_at) "
                "VALUES (100,100,200,NOW())"
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(insert_override)
        assert writer_started.wait(timeout=2)
        time.sleep(0.2)
        assert future.done() is False
        conn.commit()
        future.result(timeout=5)
    conn.close()

    with get_db() as verify_conn:
        after = resolve_procurement_discount_snapshot(
            verify_conn.cursor(), 100, published,
            published_catalog_version="proc-first-insert-v1",
            require_override_schema=True,
        )
    assert after["source"] == "agent_override"
    assert (after["numer"], after["denom"]) == (100, 200)


def test_pending_and_paid_order_keep_original_override_snapshot_after_change():
    publish_procurement(version_code="proc-override-order-v1", amount_cents=100, points=130)
    _set_wholesale_override(numer=100, denom=200)
    set_flags(dual=True, quote_required=True, channel=False)
    quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-agent-override-order",
    )
    snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    order = create_recharge_order(
        user_id=100,
        order_id="cash-anchor-agent-override-order",
        amount_cents=100,
        base_points=200,
        bonus_points=0,
        payment_method="wechat_native",
        order_type="agent_inventory_purchase",
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]),
        idempotency_key="cash-anchor-agent-override-order",
        quote_type="procurement",
        expected_product_code=PLATFORM_PRODUCT,
    )
    assert order["amount_cents"] == 100
    assert order["base_points"] == 200

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE agent_pricing_overrides
                  SET wholesale_numer=150,wholesale_denom=200,
                      updated_at=updated_at + INTERVAL '1 second'
                WHERE agent_user_id=100"""
        )
        cur.execute(
            "SELECT amount_cents,base_points,pricing_snapshot_jsonb "
            "FROM recharge_orders WHERE id='cash-anchor-agent-override-order'"
        )
        pending = dict(cur.fetchone())
    pending_snapshot = pending["pricing_snapshot_jsonb"]
    assert (pending["amount_cents"], pending["base_points"]) == (100, 200)
    assert pending_snapshot["purchase_discount_version"] == snapshot["purchase_discount_version"]

    complete_recharge("cash-anchor-agent-override-order", "wx-agent-override-order")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT paid_inventory_points,bonus_inventory_points "
            "FROM agent_inventory_wallets WHERE agent_user_id=100"
        )
        inventory = dict(cur.fetchone())
        cur.execute(
            "SELECT amount_cents,base_points,pricing_snapshot_jsonb "
            "FROM recharge_orders WHERE id='cash-anchor-agent-override-order'"
        )
        paid = dict(cur.fetchone())
    assert inventory == {"paid_inventory_points": 200, "bonus_inventory_points": 0}
    assert (paid["amount_cents"], paid["base_points"]) == (100, 200)
    assert paid["pricing_snapshot_jsonb"] == pending_snapshot


def test_readiness_blocks_partial_or_unversioned_procurement_override():
    from services import pricing_readiness

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO agent_pricing_overrides(
                     agent_user_id,wholesale_numer,wholesale_denom,updated_at
                   ) VALUES (100,100,NULL,NOW())"""
        )
        status = pricing_readiness._check_schema(cur)
    assert status["ready"] is False
    assert status["invalid_procurement_overrides"] == 1
    assert any("专属进货折扣" in item for item in status["problems"])


def test_readiness_rejects_override_table_without_per_agent_uniqueness():
    from services import pricing_readiness

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "ALTER TABLE agent_pricing_overrides "
            "DROP CONSTRAINT agent_pricing_overrides_pkey"
        )
        status = pricing_readiness._check_schema(cur)
        conn.rollback()
    assert status["ready"] is False
    assert any("专属折扣单服务商唯一" in item for item in status["problems"])


def test_readiness_rejects_partial_override_index_disguised_as_uniqueness():
    from services import pricing_readiness

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "ALTER TABLE agent_pricing_overrides "
            "DROP CONSTRAINT agent_pricing_overrides_pkey"
        )
        cur.execute(
            "CREATE UNIQUE INDEX fake_agent_pricing_override_unique "
            "ON agent_pricing_overrides(agent_user_id) WHERE agent_user_id < 0"
        )
        status = pricing_readiness._check_schema(cur)
        conn.rollback()
    assert status["ready"] is False
    assert any("专属折扣单服务商唯一" in item for item in status["problems"])


def test_corrupted_extreme_override_fails_closed_without_quote_fallback():
    from services import pricing_readiness

    publish_procurement(version_code="proc-override-corrupt-v1", amount_cents=100, points=130)
    _set_wholesale_override(numer=1, denom=9999)
    set_flags(dual=True, quote_required=True, channel=False)
    with pytest.raises(price_quote.QuoteError, match="专属进货折扣无法确认"):
        price_quote.issue_procurement_quote(
            dealer_id=100,
            product_code=PLATFORM_PRODUCT,
            idempotency_key="cash-anchor-corrupt-override",
        )
    assert scalar(
        "SELECT COUNT(*) FROM price_quotes WHERE buyer_user_id=100"
    ) == 0
    with get_db() as conn:
        status = pricing_readiness._check_schema(conn.cursor())
    assert status["ready"] is False
    assert status["invalid_procurement_overrides"] == 1


def test_relationship_change_invalidates_unconsumed_cash_anchor_quote():
    publish_procurement(version_code="proc-cash-rel-v1")
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    _save_relationship(version="rel-cash-v1", multiplier=12000)
    set_flags(dual=True, quote_required=True, channel=True)
    issued = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-stale",
    )

    _save_relationship(version="rel-cash-v2", multiplier=13000, expected="rel-cash-v1")
    with get_db() as conn:
        with pytest.raises(price_quote.QuoteError, match="关系已变化"):
            price_quote.lock_and_validate(
                conn.cursor(),
                str(issued["quote_id"]),
                buyer_user_id=100,
                quote_type="procurement",
            )
    assert scalar(
        "SELECT COUNT(*) FROM price_quotes WHERE quote_id=%s AND status='issued'",
        (issued["quote_id"],),
    ) == 1


def test_old_issued_procurement_quote_is_not_reinterpreted_as_cash_anchor():
    version_id = publish_procurement(version_code="proc-legacy-v0")
    with get_db() as conn:
        cur = conn.cursor()
        legacy_snapshot = price_quote._neutral_procurement_snapshot(
            dealer_id=100,
            catalog_version="proc-legacy-v0",
            product_code=PLATFORM_PRODUCT,
            final_cents=130,
            points_granted=130,
            bonus_points=0,
        )
        legacy = price_quote._insert_quote(
            cur,
            quote_type="procurement",
            scope_key="PLATFORM_BASE",
            product_code=PLATFORM_PRODUCT,
            buyer_user_id=100,
            quantity=1,
            catalog_version="proc-legacy-v0",
            catalog_version_id=version_id,
            base_cents=100,
            mult_bps=13000,
            final_cents=130,
            points_granted=130,
            bonus_points=0,
            order_pricing_snapshot=legacy_snapshot,
        )
        from services import pricing_readiness

        readiness = pricing_readiness._check_order_snapshots(cur)
        assert readiness["ready"] is False
        assert readiness["active_legacy_procurement_quotes"] == 1
        with pytest.raises(price_quote.QuoteError, match="旧版进货报价已失效"):
            price_quote.lock_and_validate(
                cur,
                str(legacy["quote_id"]),
                buyer_user_id=100,
                quote_type="procurement",
            )


def test_pre_snapshot_issued_procurement_quote_is_explicitly_expired_at_order_gate():
    legacy_hash = price_quote.compute_calculation_hash(price_quote._hash_inputs(
        quote_type="procurement",
        product_code=PLATFORM_PRODUCT,
        quantity=1,
        buyer_user_id=100,
        catalog_version="proc-pre-snapshot-v0",
        base_price_cents=100,
        multiplier_bps=13000,
        final_price_cents=130,
        points_granted=130,
        channel_relationship_version=None,
        upstream_cost_basis_cents=None,
    ))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO price_quotes(
                   quote_id,quote_type,catalog_version,product_code,quantity,
                   buyer_user_id,points_granted,bonus_points,base_price_cents,
                   effective_multiplier_bps,final_price_cents,expires_at,
                   calculation_hash,status,pricing_snapshot_jsonb)
               VALUES ('pq_pre_snapshot_cash_anchor','procurement','proc-pre-snapshot-v0',
                       %s,1,100,130,0,100,13000,130,NOW()+INTERVAL '15 minutes',
                       %s,'issued',NULL)""",
            (PLATFORM_PRODUCT, legacy_hash),
        )
        with pytest.raises(price_quote.QuoteError, match="旧版进货报价已失效"):
            price_quote.lock_and_validate(
                cur,
                "pq_pre_snapshot_cash_anchor",
                buyer_user_id=100,
                quote_type="procurement",
            )


def test_twenty_concurrent_identical_quotes_persist_once():
    publish_procurement(version_code="proc-cash-concurrent-v1")
    set_flags(dual=True, quote_required=True, channel=False)

    def issue(_index: int) -> str:
        quote = price_quote.issue_procurement_custom_amount_quote(
            dealer_id=100,
            requested_amount_cents=100,
            idempotency_key="cash-anchor-concurrent-20",
        )
        return str(quote["quote_id"])

    with ThreadPoolExecutor(max_workers=20) as pool:
        quote_ids = list(pool.map(issue, range(20)))

    assert len(set(quote_ids)) == 1
    assert scalar(
        "SELECT COUNT(*) FROM price_quotes WHERE buyer_user_id=100 "
        "AND idempotency_key='cash-anchor-concurrent-20' AND status='issued'"
    ) == 1


def test_procurement_idempotency_key_cannot_be_reused_for_different_cash_request():
    publish_procurement(version_code="proc-cash-idem-mismatch-v1")
    set_flags(dual=True, quote_required=True, channel=False)
    first = price_quote.issue_procurement_custom_amount_quote(
        dealer_id=100,
        requested_amount_cents=100,
        idempotency_key="cash-anchor-idem-mismatch",
    )
    with pytest.raises(price_quote.QuoteError, match="幂等键已用于不同"):
        price_quote.issue_procurement_custom_amount_quote(
            dealer_id=100,
            requested_amount_cents=101,
            idempotency_key="cash-anchor-idem-mismatch",
        )
    assert int(first["final_price_cents"]) == 100
    assert scalar(
        "SELECT COUNT(*) FROM price_quotes WHERE buyer_user_id=100 "
        "AND idempotency_key='cash-anchor-idem-mismatch'"
    ) == 1


def test_tier_reward_is_derived_after_paid_points_and_settles_to_separate_pool():
    version_id = publish_procurement(
        version_code="proc-cash-bonus-v1",
        amount_cents=100,
        points=130,
        bonus_points=1,
        reward_rate=0.1,
    )
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    _save_relationship(version="rel-cash-bonus-v1", multiplier=12000)
    set_flags(dual=True, quote_required=True, channel=True)

    quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="cash-anchor-bonus",
    )
    assert quote["final_price_cents"] == 100
    assert quote["points_granted"] == 108
    assert quote["bonus_points"] == 11
    order = create_recharge_order(
        user_id=100,
        order_id="cash-anchor-bonus-order",
        amount_cents=100,
        base_points=108,
        bonus_points=11,
        payment_method="wechat_native",
        order_type="agent_inventory_purchase",
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]),
        idempotency_key="cash-anchor-bonus-order",
        quote_type="procurement",
        expected_product_code=PLATFORM_PRODUCT,
    )
    assert order["base_points"] == 108
    assert order["bonus_points"] == 11
    complete_recharge("cash-anchor-bonus-order", "wx-cash-anchor-bonus")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT paid_inventory_points,bonus_inventory_points "
            "FROM agent_inventory_wallets WHERE agent_user_id=100"
        )
        inventory = dict(cur.fetchone())
    assert inventory == {
        "paid_inventory_points": 108,
        "bonus_inventory_points": 11,
    }


def test_provider_page_dto_and_network_contract_hide_internal_procurement_terms():
    from api import pricing_ssot_api
    from api.agent_workbench_api import AgentPurchasePreviewWiringResponse

    publish_procurement(version_code="proc-cash-privacy-v1", amount_cents=100, points=130)
    _set_wholesale_override(numer=100, denom=200)
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    _save_relationship(version="rel-cash-privacy-v1", multiplier=12000)
    set_flags(dual=True, quote_required=True, channel=True)
    request = Request({
        "type": "http", "method": "GET", "path": "/api/pricing/procurement/catalog",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })
    request.state.user = {"user_id": 100, "is_admin": False}
    catalog = asyncio.run(pricing_ssot_api.procurement_catalog(request))
    quote = asyncio.run(pricing_ssot_api.procurement_quote(
        pricing_ssot_api.ProcurementQuoteRequest(product_code=PLATFORM_PRODUCT), request,
    ))

    forbidden = (
        "platform_base", "upstream", "multiplier", "cost_basis", "b2b",
        "discount_numer", "discount_denom", "purchase_discount_version",
        "purchase_discount_snapshot", "override_updated_at", "agent_override",
        "平台库存转售", "倍率", "上游", "底价", "逐级利润",
    )
    network = json.dumps({"catalog": catalog, "quote": quote}, ensure_ascii=False).lower()
    assert not [term for term in forbidden if term.lower() in network]
    assert quote["data"]["cash_price_cents"] == 100
    assert quote["data"]["paid_inventory_points"] == 166

    # 🔴🔴 [#118-9] 泄漏检测**打去注释后的源码**。
    #
    #    实测:`upstream` / `multiplier` 只出现在 `v35w2Api.ts:237` 的**一行注释**里,
    #    而那句注释写的正是「这里**刻意没有** cost_multiplier / upstream_* 等字段」——
    #    **记录「我们没泄漏」的那句话触发了泄漏检测**。
    #    于是「解释清楚自己没做什么」本身会让判据变红,
    #    而真要泄漏的人顺手写句注释就能把红转移到别人头上。
    #
    #    复用既有剥注释器,**不写第二个** —— 同一谓词两处实现,漂开那天不会有东西报错。
    #    ⚠️ 它是**行级**的(匹配行首 `*` 或 `//`):剥不掉**行尾**注释。
    #       这一格目前没有实例;写在这里是因为「这把尺子量不到哪儿」
    #       应当写在尺子旁边,而不是等下一个人重新发现。
    root = Path(__file__).resolve().parents[2]
    provider_sources = "\n".join(
        _strip_frontend_comments((root / relative).read_text(encoding="utf-8")).lower()
        for relative in (
            "frontend/src/pages/Agent/InventoryCenter.tsx",
            "frontend/src/lib/v35w2Api.ts",
        )
    )
    assert not [term for term in forbidden if term.lower() in provider_sources]
    assert "resale_mode" not in AgentPurchasePreviewWiringResponse.model_fields


def _strip_frontend_comments(text: str) -> str:
    """剥前端源码的整行注释 —— **复用**既有实现,不写第二个。

    🔴 Review 点名的坐标 `tests/v35_visible_copy.strip_comments` 在树上**不存在**。
    仓里真有两个剥注释器:
      · `selftest_qgate_rework_mutations.strip_comments_and_docstrings`
        —— 基于 Python `tokenize`,**对 .tsx/.ts 无效**;
      · `defensive_geo_w4…test_frontend_backend_action_parity._strip_comments`
        —— 行级正则,前端源码适用。这里用后者。
    """
    from tests.defensive_geo_w4_2026_08_22.test_frontend_backend_action_parity import (
        _strip_comments,
    )

    return _strip_comments(text)


def test_the_leak_detector_still_sees_a_real_leak():
    """🔴 正样本自证:剥了注释之后,**真泄漏必须仍然被抓到**。

    只把注释剥掉、不验这一条的话,「不再误报」与「什么都不再报」同形 ——
    而后者正是把检测器悄悄关掉。
    """
    real_leak = "const x = { cost_multiplier: 1.3, upstream_price: 42 };"
    only_comment = "// 这里刻意没有 cost_multiplier / upstream_* 等字段"
    leak = _strip_frontend_comments(real_leak).lower()
    note = _strip_frontend_comments(only_comment).lower()
    assert "multiplier" in leak and "upstream" in leak, (
        "剥注释把真代码也剥掉了 —— 检测器被关掉了")
    assert "multiplier" not in note and "upstream" not in note, (
        "注释没被剥掉 —— 误报还在")


def test_provider_pricing_prerequisite_error_hides_internal_fulfillment_model():
    from api.agent_workbench_api import (
        AgentPurchasePreviewWiringRequest,
        agent_inventory_purchase_preview,
    )

    set_flags(dual=False, quote_required=False, channel=False)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO system_settings(key,value) VALUES
                   ('DEALER_INVENTORY_RESALE_ENABLED','true')
               ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"""
        )
    request = Request({
        "type": "http", "method": "POST",
        "path": "/api/agent/inventory/purchase-preview", "headers": [],
        "query_string": b"", "scheme": "http", "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })
    request.state.user = {"user_id": 100, "is_admin": False}

    try:
        with pytest.raises(HTTPException) as rejected:
            asyncio.run(agent_inventory_purchase_preview(
                AgentPurchasePreviewWiringRequest(amount_cents=100), request,
            ))
        assert rejected.value.status_code == 503
        public_error = json.dumps(rejected.value.detail, ensure_ascii=False).lower()
        forbidden = (
            "dealer_resale", "platform_base", "upstream", "multiplier", "cost_basis",
            "b2b", "平台库存转售", "逐级", "倍率", "上游", "底价", "逐级利润",
        )
        assert not [term for term in forbidden if term in public_error]
    finally:
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE system_settings SET value='false' "
                "WHERE key='DEALER_INVENTORY_RESALE_ENABLED'"
            )


def test_provider_reward_copy_never_reflects_administrative_pricing_text():
    malicious = {
        "bonus_points": 9,
        "reward_description": "platform_base upstream multiplier cost_basis B2B 上游 倍率 底价",
    }
    assert price_quote.public_procurement_reward_description(malicious) == "本档含赠送库存"
