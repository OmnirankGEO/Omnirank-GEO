from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Callable

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from db.connection import get_db
from db.wallet_db import create_recharge_order
from services import account_codes, commercial_service_routing, customer_binding, price_quote

from helpers import PLATFORM_PRODUCT, publish_retail


ROOT = Path(__file__).resolve().parents[2]


def _request() -> Request:
    request = Request({
        "type": "http", "method": "PATCH", "path": "/api/admin/test",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("testclient", 50000),
    })
    request.state.user = {
        "user_id": 900, "id": 900, "username": "admin-900", "is_admin": True,
    }
    return request


def _ensure_governance_schema() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_agent_id INTEGER")
        cur.execute("ALTER TABLE referral_links ADD COLUMN IF NOT EXISTS commission_rate NUMERIC DEFAULT 0")
        cur.execute("ALTER TABLE referral_links ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_test_referral_pair "
            "ON referral_links(referrer_id,referred_id)"
        )
        cur.execute("ALTER TABLE customer_agent_bindings ADD COLUMN IF NOT EXISTS admin_override_user_id INTEGER")
        cur.execute("ALTER TABLE customer_agent_bindings ADD COLUMN IF NOT EXISTS admin_override_at TIMESTAMPTZ")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
              id SERIAL PRIMARY KEY,
              user_id INTEGER,
              username TEXT,
              action TEXT NOT NULL,
              module TEXT,
              entity_type TEXT,
              entity_id INTEGER,
              summary TEXT,
              before_snapshot TEXT,
              after_snapshot TEXT,
              ip_address TEXT,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_agent_binding_disputes (
              id BIGSERIAL PRIMARY KEY,
              customer_user_id INTEGER NOT NULL,
              old_agent_user_id INTEGER NOT NULL,
              new_agent_user_id INTEGER NOT NULL,
              binding_source TEXT,
              source_token TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              admin_decision TEXT,
              admin_user_id INTEGER,
              note TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              resolved_at TIMESTAMPTZ
            )
        """)
        governance_migration = (
            ROOT / "scripts" / "migration_admin_user_governance_2026_07_15.sql"
        ).read_text(encoding="utf-8")
        cur.execute(governance_migration)
        cur.execute(governance_migration)


def _seed_customer(customer_id: int, *, bound_agent_id: int | None = None) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users(id,username,is_active) VALUES (%s,%s,1)",
            (customer_id, f"customer-{customer_id}"),
        )
        cur.execute("INSERT INTO user_wallets(user_id,agent_level) VALUES (%s,0)", (customer_id,))
        if bound_agent_id is not None:
            cur.execute(
                """INSERT INTO customer_agent_bindings
                   (customer_user_id,agent_user_id,binding_source,bound_at)
                   VALUES (%s,%s,'admin_manual',NOW())""",
                (customer_id, bound_agent_id),
            )
            cur.execute(
                "INSERT INTO referral_links(referrer_id,referred_id,level) VALUES (%s,%s,1)",
                (bound_agent_id, customer_id),
            )


def _quote(customer_id: int, version: str) -> dict:
    prepared = account_codes.prepare_account_codes(
        service_user_ids=[200], channel_user_ids=[],
    )
    scope = next(
        str(row["code"]) for row in prepared["prepared"] if row["kind"] == "service"
    )
    publish_retail(scope, version_code=version)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM customer_agent_bindings WHERE customer_user_id=%s",
            (int(customer_id),),
        )
        route_source = "explicit_binding" if cur.fetchone() else "platform_direct"
    return price_quote.issue_quote(
        quote_type="retail", scope_key=scope, product_code=PLATFORM_PRODUCT,
        buyer_user_id=customer_id,
        commercial_service_source=route_source,
    )


def _create_order(customer_id: int, quote: dict, order_id: str) -> dict:
    route_source = price_quote.quote_order_pricing_snapshot(quote).get(
        "commercial_service_source"
    )
    return create_recharge_order(
        user_id=customer_id, order_id=order_id,
        amount_cents=int(quote["final_price_cents"]),
        base_points=int(quote["points_granted"]),
        bonus_points=int(quote["bonus_points"]), payment_method="wechat_native",
        order_type="customer_recharge", agent_user_id=200,
        sku_template_id=1, override_id=701,
        binding_source=route_source,
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]), quote_type="retail",
        expected_product_code=PLATFORM_PRODUCT,
    )


def _install_paused_order_resolver(monkeypatch, customer_id: int):
    resolved = threading.Event()
    release = threading.Event()
    original = commercial_service_routing.resolve_commercial_relationship

    def paused(cursor, subject_id, *, for_update=False):
        relationship = original(cursor, subject_id, for_update=for_update)
        if int(subject_id) == int(customer_id) and for_update:
            resolved.set()
            if not release.wait(5):
                raise AssertionError("test did not release order transaction")
        return relationship

    monkeypatch.setattr(commercial_service_routing, "resolve_commercial_relationship", paused)
    return resolved, release


def _thread_call(name: str, fn: Callable[[], object]):
    finished = threading.Event()
    errors: list[BaseException] = []
    results: list[object] = []

    def run() -> None:
        try:
            results.append(fn())
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(name=name, target=run)
    thread.start()
    return thread, finished, errors, results


def _patch_admin_dependencies(monkeypatch) -> None:
    from api import admin_api
    from services import admin_business_profile

    monkeypatch.setattr(
        admin_api, "get_user",
        lambda user_id: {"id": int(user_id), "username": f"user-{user_id}", "is_active": 1},
    )
    monkeypatch.setattr(admin_api, "create_audit_log", lambda **kwargs: None)
    monkeypatch.setattr(admin_business_profile, "detect_referral_cycle", lambda *args: False)


def test_retired_mixed_admin_rebind_writer_returns_410_without_writing(monkeypatch):
    from api import admin_api

    _ensure_governance_schema()
    _seed_customer(520)
    _patch_admin_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.admin_set_upstream_service_provider(
            520,
            _request(),
        ))
    assert exc.value.status_code == 410
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_bindings WHERE customer_user_id=520")
        assert int(cur.fetchone()["c"]) == 0


def test_retired_mixed_admin_unbind_writer_returns_410_without_writing(monkeypatch):
    from api import admin_api

    _ensure_governance_schema()
    _seed_customer(521, bound_agent_id=200)
    _patch_admin_dependencies(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.admin_set_upstream_service_provider(
            521,
            _request(),
        ))
    assert exc.value.status_code == 410
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=521")
        assert int(cur.fetchone()["agent_user_id"]) == 200


def test_dispute_resolution_and_new_binding_request_have_one_lock_order(monkeypatch):
    from api import admin_w4_api
    from db import dispute_escrow_db

    _ensure_governance_schema()
    _seed_customer(530, bound_agent_id=200)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE customer_agent_bindings SET dispute_status='pending' WHERE customer_user_id=530"
        )
        cur.execute(
            """INSERT INTO customer_agent_binding_disputes
               (customer_user_id,old_agent_user_id,new_agent_user_id,binding_source,status)
               VALUES (530,200,300,'admin_manual','pending') RETURNING id"""
        )
        dispute_id = int(cur.fetchone()["id"])
    monkeypatch.setattr(dispute_escrow_db, "list_held_for_dispute", lambda *args: [])
    monkeypatch.setattr(dispute_escrow_db, "settle_escrow", lambda *args: True)
    monkeypatch.setattr(dispute_escrow_db, "refund_escrow", lambda *args: True)
    # This test isolates the subject-lock ordering. Provider readiness is covered
    # by the governance/readiness suites and would require a full resale catalog.
    monkeypatch.setattr(
        admin_w4_api, "lock_commercial_provider_for_assignment", lambda *args: {},
    )

    w4_locked = threading.Event()
    release_w4 = threading.Event()
    original_lock = commercial_service_routing.lock_commercial_binding_subject

    def paused_lock(cursor, customer_user_id):
        original_lock(cursor, customer_user_id)
        if threading.current_thread().name == "w4-resolve-530":
            w4_locked.set()
            if not release_w4.wait(5):
                raise AssertionError("test did not release W4 transaction")

    monkeypatch.setattr(commercial_service_routing, "lock_commercial_binding_subject", paused_lock)
    w4_thread, _, w4_errors, _ = _thread_call(
        "w4-resolve-530",
        lambda: asyncio.run(admin_w4_api.resolve_dispute(
            dispute_id,
            admin_w4_api.DisputePatchRequest(
                action="keep_old", note="并发锁序判别：保留原商业服务归属",
            ),
            _request(),
        )),
    )
    assert w4_locked.wait(5), f"W4 writer failed before subject lock: {w4_errors!r}"
    bind_thread, bind_finished, bind_errors, _ = _thread_call(
        "new-binding-530",
        lambda: _write_binding(530, 300, "after-w4"),
    )
    try:
        assert not bind_finished.wait(0.75)
    finally:
        release_w4.set()
        w4_thread.join(5)
        bind_thread.join(5)
    assert w4_errors == [] and bind_errors == []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM customer_agent_binding_disputes WHERE id=%s", (dispute_id,))
        assert cur.fetchone()["status"] == "resolved_keep_old"
        cur.execute("SELECT agent_user_id,dispute_status FROM customer_agent_bindings WHERE customer_user_id=530")
        row = dict(cur.fetchone())
        assert row == {"agent_user_id": 200, "dispute_status": "pending"}


def _write_binding(customer_id: int, agent_id: int, token: str) -> dict:
    with get_db() as conn:
        return customer_binding.upsert_customer_agent_binding(
            conn.cursor(), customer_user_id=customer_id, agent_user_id=agent_id,
            binding_source="admin_manual", source_token=token,
        )


def test_negative_control_detects_an_intentionally_unlocked_writer(monkeypatch):
    """Prove the concurrency harness fails closed if one writer skips the shared lock."""

    _ensure_governance_schema()
    _seed_customer(540)
    quote = _quote(540, "retail-v540")
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    resolved, release = _install_paused_order_resolver(monkeypatch, 540)
    original_lock = commercial_service_routing.lock_commercial_binding_subject

    def deliberately_broken_lock(cursor, customer_user_id):
        if threading.current_thread().name == "intentionally-unlocked-writer":
            return None
        return original_lock(cursor, customer_user_id)

    monkeypatch.setattr(
        commercial_service_routing, "lock_commercial_binding_subject", deliberately_broken_lock,
    )
    order_thread, _, order_errors, _ = _thread_call(
        "order-540", lambda: _create_order(540, quote, "order-negative-lock-control"),
    )
    assert resolved.wait(5)
    bind_thread, bind_finished, bind_errors, _ = _thread_call(
        "intentionally-unlocked-writer", lambda: _write_binding(540, 300, "negative-control"),
    )
    try:
        assert bind_finished.wait(0.75), "negative control did not reproduce the no-row race"
    finally:
        release.set()
        order_thread.join(5)
        bind_thread.join(5)
    assert order_errors == [] and bind_errors == []
