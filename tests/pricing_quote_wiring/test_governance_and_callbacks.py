"""Acceptance for admin-only code governance, channel OCC, and procurement settlement."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from config import pricing_ssot_flags
from db.connection import get_db
from db.wallet_db import complete_recharge, create_recharge_order
from services import account_codes, channel_pricing, price_quote
from services.channel_pricing import ChannelError

from helpers import PLATFORM_PRODUCT, publish_procurement, scalar, set_flags


def _save_relationship(
    buyer: int,
    upstream: int,
    version: str,
    *,
    expected: str | None = None,
    multiplier: int = 11000,
):
    return channel_pricing.save_relationships([{
        "buyer_dealer_id": buyer,
        "upstream_channel_account_id": upstream,
        "expected_relationship_version": expected,
        "new_relationship_version": version,
        "cost_multiplier_bps": multiplier,
        "reason": "explicit owner-approved test fixture",
    }], approved_by=900, created_by=900)


def test_public_code_reads_and_admin_dry_run_are_zero_write():
    assert scalar("SELECT COUNT(*) FROM public_account_codes") == 0
    assert account_codes.get_service_code(200) is None
    assert account_codes.get_channel_code(200) is None
    dry_run = account_codes.account_code_dry_run(
        service_user_ids=[200], channel_user_ids=[300]
    )
    assert dry_run["missing_service_count"] == 1
    assert dry_run["missing_channel_count"] == 1
    assert scalar("SELECT COUNT(*) FROM public_account_codes") == 0


def test_account_code_prepare_twenty_way_concurrent_is_stable_and_idempotent():
    def prepare(_index: int):
        return account_codes.prepare_account_codes(
            service_user_ids=[200], channel_user_ids=[200]
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(prepare, range(20)))

    service_codes = {
        item["code"]
        for result in results
        for item in result["prepared"]
        if item["kind"] == "service"
    }
    channel_codes = {
        item["code"]
        for result in results
        for item in result["prepared"]
        if item["kind"] == "channel"
    }
    assert len(service_codes) == len(channel_codes) == 1
    service_code = next(iter(service_codes))
    channel_code = next(iter(channel_codes))
    assert service_code.startswith("SV-") and "200" not in service_code
    assert channel_code.startswith("CH-") and "200" not in channel_code
    assert sum(result["created_count"] for result in results) == 2
    assert scalar("SELECT COUNT(*) FROM public_account_codes WHERE user_id=200") == 1

    retry = account_codes.prepare_account_codes(
        service_user_ids=[200], channel_user_ids=[200]
    )
    assert retry["created_count"] == 0
    assert account_codes.get_service_code(200) == service_code
    assert account_codes.get_channel_code(200) == channel_code


def test_channel_dry_run_self_cycle_duplicate_upstream_and_stale_version_rejected():
    change = {
        "buyer_dealer_id": 100,
        "upstream_channel_account_id": 200,
        "expected_relationship_version": None,
        "new_relationship_version": "rel-v1",
        "cost_multiplier_bps": 11000,
        "reason": "explicit list",
    }
    dry_run = channel_pricing.dry_run_relationships([change])
    assert dry_run["valid"] is True
    assert scalar("SELECT COUNT(*) FROM channel_pricing_relationships") == 0
    _save_relationship(100, 200, "rel-v1")

    exact_retry = _save_relationship(100, 200, "rel-v1", multiplier=11000)
    assert exact_retry["changed_count"] == 0
    assert exact_retry["relationships"][0]["idempotent"] is True

    with pytest.raises(ChannelError, match="版本已变化"):
        _save_relationship(100, 300, "rel-v2", expected="stale-v0")

    with pytest.raises(ChannelError, match="同一批次"):
        channel_pricing.save_relationships([
            {
                "buyer_dealer_id": 300,
                "upstream_channel_account_id": 100,
                "new_relationship_version": "double-a",
                "cost_multiplier_bps": 10000,
            },
            {
                "buyer_dealer_id": 300,
                "upstream_channel_account_id": 200,
                "new_relationship_version": "double-b",
                "cost_multiplier_bps": 10000,
            },
        ])

    with pytest.raises(ChannelError, match="自己归属自己"):
        _save_relationship(300, 300, "self")

    with pytest.raises(ChannelError, match="成环"):
        _save_relationship(200, 100, "cycle")

    status = channel_pricing.relationship_status()
    assert status["ready"] is True
    assert status["active_relationship_count"] == 1


def test_procurement_quote_order_twenty_callbacks_use_old_snapshot_once():
    """Relationship/catalog/flag changes after order creation cannot reprice callback."""

    publish_procurement(version_code="proc-v1")
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    _save_relationship(100, 200, "rel-v1", multiplier=11000)
    set_flags(dual=True, quote_required=True, channel=True)

    old_quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="proc-old",
    )
    assert old_quote["final_price_cents"] == 120000
    assert old_quote["points_granted"] == 177272
    assert old_quote["upstream_cost_basis_cents"] == 109091
    assert old_quote["channel_beneficiary_user_id"] == 200

    order = create_recharge_order(
        user_id=100,
        order_id="procurement-old-order",
        amount_cents=int(old_quote["final_price_cents"]),
        base_points=int(old_quote["points_granted"]),
        bonus_points=int(old_quote["bonus_points"]),
        payment_method="wechat_native",
        order_type="agent_inventory_purchase",
        price_quote_id=str(old_quote["quote_id"]),
        pricing_catalog_version=str(old_quote["catalog_version"]),
        idempotency_key="procurement-old-order",
        quote_type="procurement",
        expected_product_code=PLATFORM_PRODUCT,
    )
    old_snapshot = order["pricing_snapshot_jsonb"]
    assert old_snapshot["channel_relationship_version"] == "rel-v1"
    assert old_snapshot["channel_beneficiary_user_id"] == 200
    assert old_snapshot["buyer_paid_cents"] == 120000

    # Explicit admin change affects only later quotes.  Also publish a new base
    # catalog and close channel pricing before the old payment callback.
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[300])
    _save_relationship(100, 300, "rel-v2", expected="rel-v1", multiplier=15000)
    publish_procurement(version_code="proc-v2", amount_cents=130000, points=195000)
    new_quote = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code=PLATFORM_PRODUCT,
        idempotency_key="proc-new",
    )
    assert new_quote["channel_beneficiary_user_id"] == 300
    assert new_quote["channel_relationship_version"] == "rel-v2"
    assert new_quote["final_price_cents"] == 130000
    assert new_quote["points_granted"] == 130000

    set_flags(dual=True, quote_required=True, channel=False)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(
            pool.map(
                lambda idx: complete_recharge("procurement-old-order", f"wx-proc-{idx}"),
                range(20),
            )
        )
    assert all(result is not None for result in results)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM recharge_orders WHERE id='procurement-old-order'")
        paid_order = dict(cur.fetchone())
        cur.execute("SELECT * FROM agent_inventory_wallets WHERE agent_user_id=100")
        inventory = dict(cur.fetchone())
        cur.execute(
            "SELECT COUNT(*) AS c FROM agent_inventory_transactions "
            "WHERE related_order_id='procurement-old-order'"
        )
        inventory_tx_count = int(cur.fetchone()["c"])
        cur.execute(
            "SELECT * FROM channel_revenue_ledger WHERE recharge_order_id='procurement-old-order'"
        )
        ledger = dict(cur.fetchone())
    assert paid_order["payment_status"] == "paid"
    assert paid_order["settlement_mode"] == "agent_inventory_prepay"
    assert paid_order["pricing_snapshot_jsonb"] == old_snapshot
    assert inventory["paid_inventory_points"] == 177272
    assert inventory["bonus_inventory_points"] == 0
    assert inventory_tx_count == 1
    assert ledger["channel_beneficiary_user_id"] == 200
    assert ledger["relationship_version"] == "rel-v1"
    assert ledger["upstream_cost_basis_cents"] == 109091
    assert ledger["buyer_paid_cents"] == 120000
    assert ledger["channel_revenue_cents"] == 10909
    assert scalar(
        "SELECT COUNT(*) FROM channel_revenue_ledger "
        "WHERE recharge_order_id='procurement-old-order'"
    ) == 1


def test_flag_state_matrix_and_invalid_ordering_readiness_contract():
    publish_procurement()
    account_codes.prepare_account_codes(service_user_ids=[], channel_user_ids=[200])
    _save_relationship(100, 200, "rel-v1", multiplier=11000)

    matrix = [
        ((False, False, False), (False, False, False), 120000, 195000),
        ((True, False, False), (True, False, False), 120000, 195000),
        ((True, True, False), (True, True, False), 120000, 195000),
        ((True, True, True), (True, True, True), 120000, 177272),
    ]
    for index, ((dual, required, channel), expected_flags, expected_cents, expected_points) in enumerate(matrix):
        set_flags(dual=dual, quote_required=required, channel=channel)
        observed = (
            pricing_ssot_flags.dual_ssot_enabled(),
            pricing_ssot_flags.quote_required(),
            pricing_ssot_flags.channel_pricing_enabled(),
        )
        assert observed == expected_flags
        quote = price_quote.issue_procurement_quote(
            dealer_id=100,
            product_code=PLATFORM_PRODUCT,
            idempotency_key=f"matrix-{index}",
        )
        assert quote["final_price_cents"] == expected_cents
        assert quote["points_granted"] == expected_points

    from services.pricing_readiness import _read_flags

    set_flags(dual=False, quote_required=True, channel=False)
    with get_db() as conn:
        invalid = _read_flags(conn.cursor())
    assert invalid["ready"] is False
    assert any("不能先于" in reason for reason in invalid["problems"])
