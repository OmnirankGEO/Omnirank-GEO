"""Behavior locks for JIT paid/bonus pools, tier persistence, and refund proof."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import psycopg2
import psycopg2.extras


def _connect():
    return psycopg2.connect(
        os.environ["TEST_DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _row(sql: str, params=()):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            value = cur.fetchone()
            return dict(value) if value else None


def _tier_snapshot() -> dict:
    from services.agent_inventory_pricing import finalize_quote_snapshot

    return finalize_quote_snapshot(
        {
            "catalog_version": "agent-purchase-v1",
            "option_id": "apo-jit-tier",
            "amount_cents": 100000,
            "base_points": 144444,
            "discount_source": "dealer_inventory_resale_quote",
            "discount_numer": 225,
            "discount_denom": 325,
            "channel_tier_enabled": True,
            "tier_at_order": "preferred",
            "tier_source": "automatic",
            "tier_bonus_rate_bps": 1500,
            "tier_bonus_points": 21667,
            "founder_eligibility_source": "existing_atomic_gate",
            "founder_seat_policy": "payment_time_atomic_remaining_seat",
            "founder_cap_snapshot": 10,
            "founder_min_first_order_yuan_snapshot": "500",
            "founder_bonus_rate_bps_snapshot": 1000,
            "founder_bonus_points_if_eligible": 14444,
            "bonus_validity_months": 12,
            "bonus_rate_bps": 1500,
            "bonus_points": 21667,
            "total_points": 166111,
            "option_source": "catalog",
            "reward_eligible": True,
            "rolling_before_yuan_snapshot": "0",
            "projected_rolling_12m_yuan_snapshot": "1000",
            "tier_override_until_snapshot": None,
        },
        agent_user_id=7,
    )


def test_jit_callback_settles_base_once_and_persists_tier_founder_once(monkeypatch):
    """Twenty callback attempts must not merge rewards into paid inventory."""
    import db.wallet_db as wallet_db
    import services.dealer_inventory_resale as resale
    from services.agent_inventory import get_or_create_inventory_wallet

    snapshot = _tier_snapshot()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_TIER_ENABLED'")
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,amount_cents,base_points,bonus_points,payment_status,
                       order_type,pricing_snapshot_jsonb,pricing_catalog_version)
                     VALUES ('jit-tier-order',7,100000,144444,21667,'pending',
                             'agent_inventory_purchase',%s,'agent-purchase-v1')""",
                (json.dumps(snapshot),),
            )

    settled_points: list[int] = []

    def settle_base(cur, *, buyer_user_id, total_points, order_id):
        assert buyer_user_id == 7
        assert order_id == "jit-tier-order"
        assert total_points == 144444
        wallet = get_or_create_inventory_wallet(cur, buyer_user_id)
        cur.execute(
            """UPDATE agent_inventory_wallets
                  SET paid_inventory_points=paid_inventory_points+%s,
                      total_purchased_points=total_purchased_points+%s
                WHERE agent_user_id=%s""",
            (total_points, total_points, buyer_user_id),
        )
        settled_points.append(total_points)
        return {
            "paid_inventory_points": int(wallet["paid_inventory_points"]) + total_points,
            "bonus_inventory_points": int(wallet["bonus_inventory_points"]),
            "resale_settled": True,
        }

    monkeypatch.setattr(wallet_db, "_record_channel_revenue_if_applicable", lambda *_args: None)
    monkeypatch.setattr(resale, "has_resale_order", lambda _cur, order_id: order_id == "jit-tier-order")
    monkeypatch.setattr(resale, "settle_reserved_order", settle_base)

    def assert_bonus_order(_cur, *, order_id, buyer_user_id):
        assert (order_id, buyer_user_id) == ("jit-tier-order", 7)

    monkeypatch.setattr(resale, "assert_resale_bonus_credit_allowed", assert_bonus_order)
    # Production initializes the process pool before request fan-out. Mirror that
    # lifecycle so this test measures callback serialization, not pool construction.
    from db.connection import get_connection

    warm = get_connection()
    warm.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(
            pool.map(
                lambda index: wallet_db.complete_recharge(
                    "jit-tier-order", f"jit-tier-payment-{index}"
                ),
                range(20),
            )
        )

    assert settled_points == [144444]
    wallet = _row(
        "SELECT paid_inventory_points,bonus_inventory_points,total_purchased_points "
        "FROM agent_inventory_wallets WHERE agent_user_id=7"
    )
    assert wallet == {
        "paid_inventory_points": 144444,
        "bonus_inventory_points": 36111,
        "total_purchased_points": 180555,
    }
    grants = _row(
        "SELECT COUNT(*) AS count,COALESCE(SUM(granted_points),0) AS points "
        "FROM bonus_grants WHERE related_order_id='jit-tier-order'"
    )
    assert grants == {"count": 2, "points": 36111}
    state = _row(
        "SELECT channel_tier,is_founder,founder_rank,first_order_done "
        "FROM agent_channel_tier_state WHERE agent_user_id=7"
    )
    assert state == {
        "channel_tier": "certified",
        "is_founder": True,
        "founder_rank": 1,
        "first_order_done": True,
    }
    assert _row("SELECT COUNT(*) AS count FROM agent_tier_change_log")["count"] == 1
    assert _row("SELECT used FROM founder_seats WHERE id=1")["used"] == 1


def test_bonus_only_resale_credit_requires_paid_base_transfer(monkeypatch):
    import pytest

    import services.dealer_inventory_resale as resale
    from services.agent_inventory import purchase_inventory_prepay

    monkeypatch.setattr(resale, "has_resale_order", lambda *_args: True)

    def reject_pending(*_args, **_kwargs):
        raise resale.ResaleError("RESALE_BONUS_BEFORE_PAID", "pending")

    monkeypatch.setattr(resale, "assert_resale_bonus_credit_allowed", reject_pending)
    with _connect() as conn:
        with conn.cursor() as cur:
            with pytest.raises(resale.ResaleError) as exc:
                purchase_inventory_prepay(
                    cur, 7, paid_points=0, bonus_points=50,
                    related_order_id="pending-resale",
                )
            assert exc.value.code == "RESALE_BONUS_BEFORE_PAID"
            cur.execute("SELECT COUNT(*) AS n FROM agent_inventory_wallets")
            assert int(cur.fetchone()["n"]) == 0


def test_bonus_refund_reserve_release_revoke_and_replay_are_pool_safe():
    from services.bonus_grants import (
        release_agent_order_bonus_refund,
        reserve_agent_order_bonus_for_refund,
        revoke_agent_order_bonus_for_refund,
    )

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_inventory_wallets(
                       agent_user_id,bonus_inventory_points,total_purchased_points)
                     VALUES (7,50,50)"""
            )
            cur.execute(
                """INSERT INTO bonus_grants(
                       grant_key,owner_type,owner_id,granted_points,grant_type,
                       related_order_id,expires_at)
                     VALUES
                       ('tier:refund','agent',7,30,'tier_purchase','bonus-refund',NOW()+INTERVAL '1 month'),
                       ('founder:refund','agent',7,20,'founder_first_order','bonus-refund',NOW()+INTERVAL '1 month')"""
            )
            first = reserve_agent_order_bonus_for_refund(cur, 7, "bonus-refund")
            assert first["total_reserved_points"] == 50
            cur.execute(
                "SELECT bonus_inventory_points,frozen_inventory_points FROM agent_inventory_wallets WHERE agent_user_id=7"
            )
            assert dict(cur.fetchone()) == {"bonus_inventory_points": 0, "frozen_inventory_points": 50}

            released = release_agent_order_bonus_refund(cur, 7, "bonus-refund", first)
            assert released == {"released_points": 50, "grant_count": 2}
            cur.execute(
                "SELECT bonus_inventory_points,frozen_inventory_points FROM agent_inventory_wallets WHERE agent_user_id=7"
            )
            assert dict(cur.fetchone()) == {"bonus_inventory_points": 50, "frozen_inventory_points": 0}

            second = reserve_agent_order_bonus_for_refund(cur, 7, "bonus-refund")
            revoked = revoke_agent_order_bonus_for_refund(
                cur, 7, "bonus-refund", reservation=second
            )
            replay = revoke_agent_order_bonus_for_refund(
                cur, 7, "bonus-refund", reservation=second
            )
            assert revoked == {"revoked_points": 50, "grant_count": 2, "idempotent": False}
            assert replay == {"revoked_points": 50, "grant_count": 2, "idempotent": True}

    wallet = _row(
        "SELECT paid_inventory_points,bonus_inventory_points,frozen_inventory_points,total_purchased_points "
        "FROM agent_inventory_wallets WHERE agent_user_id=7"
    )
    assert wallet == {
        "paid_inventory_points": 0,
        "bonus_inventory_points": 0,
        "frozen_inventory_points": 0,
        "total_purchased_points": 0,
    }
    assert _row(
        "SELECT COUNT(*) AS count FROM agent_inventory_transactions "
        "WHERE related_order_id='bonus-refund' AND pool='bonus' AND points=-50"
    )["count"] == 1


def test_bonus_refund_lock_order_does_not_deadlock_expiry_worker():
    """An expiry worker holding the grant must still be able to lock the wallet."""
    from services.bonus_grants import reserve_agent_order_bonus_for_refund

    with _connect() as seed:
        with seed.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_inventory_wallets(
                       agent_user_id,bonus_inventory_points,total_purchased_points)
                     VALUES (7,50,50)"""
            )
            cur.execute(
                """INSERT INTO bonus_grants(
                       grant_key,owner_type,owner_id,granted_points,grant_type,
                       related_order_id,expires_at)
                     VALUES ('tier:lock-order','agent',7,50,'tier_purchase',
                             'lock-order-refund',NOW()+INTERVAL '1 month')"""
            )

    expiry = _connect()
    try:
        with expiry.cursor() as expiry_cur:
            expiry_cur.execute("SET LOCAL lock_timeout='750ms'")
            expiry_cur.execute(
                "SELECT id FROM bonus_grants WHERE grant_key='tier:lock-order' FOR UPDATE"
            )

            def reserve_in_parallel():
                with _connect() as refund_conn:
                    with refund_conn.cursor() as refund_cur:
                        refund_cur.execute("SET LOCAL lock_timeout='3s'")
                        return reserve_agent_order_bonus_for_refund(
                            refund_cur, 7, "lock-order-refund"
                        )

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(reserve_in_parallel)
                time.sleep(0.25)
                expiry_cur.execute(
                    "SELECT bonus_inventory_points FROM agent_inventory_wallets "
                    "WHERE agent_user_id=7 FOR UPDATE"
                )
                assert int(expiry_cur.fetchone()["bonus_inventory_points"]) == 50
                expiry.commit()
                assert future.result(timeout=5)["total_reserved_points"] == 50
    finally:
        expiry.close()


def test_bonus_refund_release_uses_grant_before_wallet_lock_order():
    from services.bonus_grants import (
        release_agent_order_bonus_refund,
        reserve_agent_order_bonus_for_refund,
    )

    with _connect() as seed:
        with seed.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_inventory_wallets(
                       agent_user_id,bonus_inventory_points,total_purchased_points)
                     VALUES (7,50,50)"""
            )
            cur.execute(
                """INSERT INTO bonus_grants(
                       grant_key,owner_type,owner_id,granted_points,grant_type,
                       related_order_id,expires_at)
                     VALUES ('tier:release-order','agent',7,50,'tier_purchase',
                             'release-order-refund',NOW()+INTERVAL '1 month')"""
            )
            reservation = reserve_agent_order_bonus_for_refund(
                cur, 7, "release-order-refund"
            )

    grant_holder = _connect()
    try:
        with grant_holder.cursor() as holder_cur:
            holder_cur.execute("SET LOCAL lock_timeout='750ms'")
            holder_cur.execute(
                "SELECT id FROM bonus_grants WHERE grant_key='tier:release-order' FOR UPDATE"
            )

            def release_in_parallel():
                with _connect() as refund_conn:
                    with refund_conn.cursor() as refund_cur:
                        refund_cur.execute("SET LOCAL lock_timeout='3s'")
                        return release_agent_order_bonus_refund(
                            refund_cur, 7, "release-order-refund", reservation
                        )

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(release_in_parallel)
                time.sleep(0.25)
                holder_cur.execute(
                    "SELECT frozen_inventory_points FROM agent_inventory_wallets "
                    "WHERE agent_user_id=7 FOR UPDATE"
                )
                assert int(holder_cur.fetchone()["frozen_inventory_points"]) == 50
                grant_holder.commit()
                assert future.result(timeout=5)["released_points"] == 50
    finally:
        grant_holder.close()


def test_pending_review_only_reduces_tier_progress_with_matching_cash_proof():
    from services.channel_tier import compute_rolling_12m_yuan
    from services.channel_tier_admin import list_channel_tier_agents

    def progress_and_admin(cur):
        progress = compute_rolling_12m_yuan(cur, 7)
        admin = next(row for row in list_channel_tier_agents(cur) if row["agent_user_id"] == 7)
        return progress, Decimal(str(admin["rolling_12m_yuan"]))

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO recharge_orders(
                       id,user_id,amount_cents,payment_status,paid_at,created_at,order_type,
                       refund_status,refunded_amount_cents)
                     VALUES ('proof-order',7,40000,'paid',NOW(),NOW(),
                             'agent_inventory_purchase','pending_review',10000)"""
            )
            assert progress_and_admin(cur) == (Decimal("400"), Decimal("400"))

            cur.execute("UPDATE recharge_orders SET refund_completed_at=NOW() WHERE id='proof-order'")
            assert progress_and_admin(cur) == (Decimal("300"), Decimal("300"))

            cur.execute(
                "UPDATE recharge_orders SET refund_completed_at=NULL,refunded_amount_cents=0 WHERE id='proof-order'"
            )
            assert progress_and_admin(cur) == (Decimal("400"), Decimal("400"))

            cur.execute("UPDATE recharge_orders SET refunded_amount_cents=50000 WHERE id='proof-order'")
            assert progress_and_admin(cur) == (Decimal("400"), Decimal("400"))

            cur.execute("UPDATE recharge_orders SET refunded_amount_cents=10000 WHERE id='proof-order'")
            cur.execute(
                "INSERT INTO refund_work_orders(source_order_id,status,requested_refund_cents) "
                "VALUES ('other-order','approved',10000) RETURNING id"
            )
            wrong_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO refund_work_order_attachments(work_order_id,evidence_type) VALUES (%s,'payout_proof')",
                (wrong_id,),
            )
            assert progress_and_admin(cur) == (Decimal("400"), Decimal("400"))

            cur.execute(
                "INSERT INTO refund_work_orders(source_order_id,status,requested_refund_cents) "
                "VALUES ('proof-order','approved',9000) RETURNING id"
            )
            wrong_amount_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO refund_work_order_attachments(work_order_id,evidence_type) VALUES (%s,'channel_refund_proof')",
                (wrong_amount_id,),
            )
            assert progress_and_admin(cur) == (Decimal("400"), Decimal("400"))

            cur.execute(
                "INSERT INTO refund_work_orders(source_order_id,status,requested_refund_cents) "
                "VALUES ('proof-order','approved',10000) RETURNING id"
            )
            correct_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO refund_work_order_attachments(work_order_id,evidence_type) VALUES (%s,'channel_refund_proof')",
                (correct_id,),
            )
            assert progress_and_admin(cur) == (Decimal("300"), Decimal("300"))

            cur.execute("DELETE FROM refund_work_order_attachments WHERE work_order_id=%s", (correct_id,))
            cur.execute(
                "UPDATE refund_work_orders SET payout_proof_url='https://proof.invalid/refund' WHERE id=%s",
                (correct_id,),
            )
            assert progress_and_admin(cur) == (Decimal("300"), Decimal("300"))
