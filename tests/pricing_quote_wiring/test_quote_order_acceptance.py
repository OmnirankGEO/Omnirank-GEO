"""Hard acceptance for persisted quotes, atomic orders, and callback idempotency."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import psycopg2
import pytest
from fastapi import HTTPException, Request

from api.agent_workbench_api import _lookup_agent_customers, agent_allocate_offline
from api.pricing_ssot_api import (
    ProcurementQuoteRequest,
    RetailQuoteRequest,
    procurement_catalog,
    procurement_quote,
    retail_catalog,
    retail_quote,
)
from schemas.v35_w2_dto import AllocateOfflineRequest
import threading

from db.connection import get_db
from db.wallet_db import complete_recharge, create_recharge_order
from tests.pricing_quote_wiring._single_ledger import (
    assert_single_ledger_settlement,
    read_settlement_facts,
)
from services import account_codes, price_quote
from services.legal_agreements import record_purchase_acceptance
from services.price_quote import QuoteError

from helpers import PLATFORM_PRODUCT, publish_procurement, publish_retail, scalar, set_flags


def _request(user: dict) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.user = user
    return request


def _service_scope() -> str:
    result = account_codes.prepare_account_codes(service_user_ids=[200], channel_user_ids=[])
    row = next(item for item in result["prepared"] if item["kind"] == "service")
    return str(row["code"])


def _retail_quote(*, buyer: int = 400, ttl_seconds: int = 900):
    scope = _service_scope()
    publish_retail(scope)
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO customer_agent_bindings(
                 customer_user_id,agent_user_id,binding_source,bound_at
               ) VALUES (%s,200,'admin_manual',NOW())
               ON CONFLICT(customer_user_id) DO NOTHING""",
            (buyer,),
        )
    set_flags(dual=True, quote_required=False, channel=False)
    return price_quote.issue_quote(
        quote_type="retail",
        scope_key=scope,
        product_code=PLATFORM_PRODUCT,
        buyer_user_id=buyer,
        ttl_seconds=ttl_seconds,
    )


def test_unbound_customer_offline_allocation_is_409_and_zero_write():
    request = _request({"user_id": 100, "is_admin": False})
    payload = AllocateOfflineRequest(customer_user_id=500, tool_points=100)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_bindings WHERE customer_user_id=500")
        before_bindings = int(cur.fetchone()["c"])
        cur.execute("SELECT COUNT(*) AS c FROM agent_inventory_transactions")
        before_inventory_tx = int(cur.fetchone()["c"])
        cur.execute("SELECT COUNT(*) AS c FROM customer_credit_transactions")
        before_customer_tx = int(cur.fetchone()["c"])

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(agent_allocate_offline(payload, request))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "COMMERCIAL_BINDING_REQUIRED"

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_bindings WHERE customer_user_id=500")
        assert int(cur.fetchone()["c"]) == before_bindings == 0
        cur.execute("SELECT COUNT(*) AS c FROM agent_inventory_transactions")
        assert int(cur.fetchone()["c"]) == before_inventory_tx
        cur.execute("SELECT COUNT(*) AS c FROM customer_credit_transactions")
        assert int(cur.fetchone()["c"]) == before_customer_tx
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_credit_wallets WHERE customer_user_id=500")
        assert int(cur.fetchone()["c"]) == 0


def test_exact_phone_lookup_never_discovers_unbound_customer():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET phone='13800138000',display_name='隐私客户' WHERE id=500"
        )
        assert _lookup_agent_customers(cur, 100, "13800138000", False, 8) == []
        cur.execute(
            """INSERT INTO customer_agent_bindings(
                   customer_user_id,agent_user_id,binding_source,bound_at)
               VALUES (500,100,'admin_manual',NOW())"""
        )
        owned = _lookup_agent_customers(cur, 100, "13800138000", False, 8)
    assert len(owned) == 1
    assert owned[0]["customer_user_id"] == 500
    assert owned[0]["binding_status"] == "owned"
    assert owned[0]["phone_masked"] == "138****8000"


def test_disputed_customer_offline_allocation_is_409_and_zero_write():
    request = _request({"user_id": 100, "is_admin": False})
    payload = AllocateOfflineRequest(customer_user_id=500, tool_points=100)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO customer_agent_bindings(
                   customer_user_id,agent_user_id,binding_source,bound_at,dispute_status)
               VALUES (500,100,'admin_manual',NOW(),'pending')"""
        )
        cur.execute("SELECT COUNT(*) AS c FROM agent_inventory_transactions")
        before_inventory = int(cur.fetchone()["c"])
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(agent_allocate_offline(payload, request))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "COMMERCIAL_BINDING_DISPUTED"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM agent_inventory_transactions")
        assert int(cur.fetchone()["c"]) == before_inventory
        cur.execute("SELECT COUNT(*) AS c FROM customer_credit_transactions")
        assert int(cur.fetchone()["c"]) == 0


def test_offline_allocation_serializes_with_admin_rebind_and_old_provider_writes_nothing():
    request = _request({"user_id": 100, "is_admin": False})
    payload = AllocateOfflineRequest(customer_user_id=500, tool_points=100)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO customer_agent_bindings(
                   customer_user_id,agent_user_id,binding_source,bound_at)
               VALUES (500,100,'admin_manual',NOW())"""
        )
        cur.execute(
            """INSERT INTO agent_inventory_wallets(agent_user_id,paid_inventory_points)
               VALUES (100,1000)
               ON CONFLICT(agent_user_id) DO UPDATE
               SET paid_inventory_points=EXCLUDED.paid_inventory_points"""
        )
        conn.commit()

    blocker = psycopg2.connect(str(__import__('os').environ['DATABASE_URL']))
    blocker.autocommit = False
    try:
        blocker_cur = blocker.cursor()
        from services.commercial_service_routing import lock_commercial_binding_subject

        lock_commercial_binding_subject(blocker_cur, 500)
        blocker_cur.execute(
            "UPDATE customer_agent_bindings SET agent_user_id=200 WHERE customer_user_id=500"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: asyncio.run(agent_allocate_offline(payload, request)))
            assert future.done() is False
            blocker.commit()
            with pytest.raises(HTTPException) as exc_info:
                future.result(timeout=10)
        assert exc_info.value.status_code == 403
    finally:
        blocker.close()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT paid_inventory_points FROM agent_inventory_wallets WHERE agent_user_id=100")
        assert int(cur.fetchone()["paid_inventory_points"]) == 1000
        cur.execute("SELECT COUNT(*) AS c FROM customer_credit_transactions")
        assert int(cur.fetchone()["c"]) == 0


def _create_retail_order(quote: dict, order_id: str):
    return create_recharge_order(
        user_id=int(quote["buyer_user_id"]),
        order_id=order_id,
        amount_cents=int(quote["final_price_cents"]),
        base_points=int(quote["points_granted"]),
        bonus_points=int(quote["bonus_points"]),
        payment_method="wechat_native",
        order_type="customer_recharge_direct",
        agent_user_id=200,
        sku_template_id=1,
        override_id=701,
        binding_source="ref_link",
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]),
        quote_type="retail",
        expected_product_code=PLATFORM_PRODUCT,
    )


def test_retail_catalog_quote_order_callback_credits_wallet_once():
    """Customer chain: catalog -> quote -> order -> 20 callbacks -> one credit."""

    quote = _retail_quote()
    order = _create_retail_order(quote, "retail-callback-once")
    assert order["price_quote_id"] == quote["quote_id"]
    assert order["pricing_catalog_version"] == quote["catalog_version"]
    assert order["pricing_snapshot_jsonb"]["buyer_paid_cents"] == quote["final_price_cents"]

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(
            pool.map(
                lambda idx: complete_recharge("retail-callback-once", f"wx-{idx}"),
                range(20),
            )
        )
    assert all(result is not None for result in results)

    # 🔴 [#118 1-8] 这段原来钉的是 **2026-07-27 单账本收敛之前**的双账本模型:
    #    期望客户钱包 paid=0(钱进退役账本)、`customer_credit_transactions` 有 1 行、
    #    `point_transactions` 2 行。收敛后现实是 paid=195000 / 退役 0 行 / point_tx 1 行。
    #    改成守**不变量**而不是守实现 —— 见 `_single_ledger` 模块 docstring。
    with get_db() as conn:
        cur = conn.cursor()
        facts = read_settlement_facts(
            cur, order_id="retail-callback-once", customer_user_id=400)
        cur.execute("SELECT * FROM recharge_orders WHERE id='retail-callback-once'")
        paid_order = dict(cur.fetchone())
    assert_single_ledger_settlement(facts)
    assert paid_order["payment_status"] == "paid"
    assert paid_order["settlement_mode"] == "v35_inventory_settlement"
    assert facts["order_base"] + facts["order_bonus"] > 0, "订单快照应给为 0 —— 守恒会被空值满足"

    # 🔴 幂等臂:再重放一次,① ③ 读数必须**一字不差**。
    #    20 并发已跑过一轮;这一发是**串行**重放,盖的是另一格
    #    (并发靠锁挡住 vs 事后再来一次靠幂等键挡住 —— 两条路)。
    complete_recharge("retail-callback-once", "wx-replay")
    with get_db() as conn:
        after = read_settlement_facts(
            conn.cursor(), order_id="retail-callback-once", customer_user_id=400)
    assert_single_ledger_settlement(after)
    assert after == facts, "重放改变了读数:%s -> %s" % (facts, after)


def test_retail_quote_customer_factory_callback_moves_funds_once():
    """Bound customer quote settles through the real V3.5 inventory/credit ledger path."""

    quote = _retail_quote()
    order = create_recharge_order(
        user_id=400,
        order_id="retail-v35-callback-once",
        amount_cents=int(quote["final_price_cents"]),
        base_points=int(quote["points_granted"]),
        bonus_points=int(quote["bonus_points"]),
        payment_method="wechat_native",
        order_type="customer_recharge",
        agent_user_id=200,
        sku_template_id=1,
        override_id=701,
        binding_source="admin_manual",
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]),
        quote_type="retail",
        expected_product_code=PLATFORM_PRODUCT,
    )
    assert order["pricing_snapshot_jsonb"]["wholesale_cents"] == 120000

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(
            pool.map(
                lambda index: complete_recharge(
                    "retail-v35-callback-once", f"wx-v35-{index}"
                ),
                range(20),
            )
        )
    assert all(result is not None for result in results)

    # 🔴 [#118 1-8] 原来这里读**退役账本** `customer_agent_credit_wallets`,
    #    并用它断言三件事:归属(agent_user_id=200)、算力拆分(tool/bonus)、
    #    以及客户钱包**必须是 0**(钱不在这本账上)。2026-07-27 单账本收敛后
    #    那张表不再有行 => 三件事都得换到**还活着的权威源**上,不是删掉:
    #      归属      -> agent_revenue_ledger.agent_user_id
    #      算力拆分  -> user_wallets == 订单快照(不变量 ①)
    #      不复制    -> 退役账本 0 行(不变量 ②,否定臂)
    with get_db() as conn:
        cur = conn.cursor()
        facts = read_settlement_facts(
            cur, order_id="retail-v35-callback-once", customer_user_id=400)
        cur.execute("SELECT * FROM agent_inventory_wallets WHERE agent_user_id=200")
        inventory = dict(cur.fetchone())
        cur.execute(
            "SELECT * FROM agent_revenue_ledger "
            "WHERE recharge_order_id='retail-v35-callback-once'"
        )
        revenue = dict(cur.fetchone())
        cur.execute(
            "SELECT * FROM recharge_orders WHERE id='retail-v35-callback-once'"
        )
        paid_order = dict(cur.fetchone())
        cur.execute(
            "SELECT COUNT(*) AS c FROM agent_inventory_transactions "
            "WHERE related_order_id='retail-v35-callback-once'"
        )
        inventory_tx_count = int(cur.fetchone()["c"])

    assert_single_ledger_settlement(facts)
    # 报价 -> 订单快照这一跳(原先由退役账本的 tool/bonus 断言承担)
    assert facts["order_base"] == int(quote["points_granted"])
    assert facts["order_bonus"] == int(quote["bonus_points"])
    # 归属(原先读退役账本的 agent_user_id)
    assert int(revenue["agent_user_id"]) == 200
    assert inventory["paid_inventory_points"] == 0
    assert inventory["bonus_inventory_points"] == 0
    assert inventory["total_purchased_points"] == int(quote["points_granted"])
    assert inventory["total_allocated_points"] == int(quote["points_granted"])
    assert revenue["customer_paid_cents"] == int(quote["final_price_cents"])
    assert revenue["factory_cents"] == 120000
    assert revenue["agent_settlement_cents"] == 60000
    assert paid_order["payment_status"] == "paid"
    assert paid_order["settlement_mode"] == "v35_inventory_settlement"
    assert inventory_tx_count == 2


def test_pending_bound_order_settles_to_immutable_provider_after_later_rebind():
    quote = _retail_quote()
    create_recharge_order(
        user_id=400,
        order_id="retail-bound-before-rebind",
        amount_cents=int(quote["final_price_cents"]),
        base_points=int(quote["points_granted"]),
        bonus_points=int(quote["bonus_points"]),
        payment_method="wechat_native",
        order_type="customer_recharge",
        agent_user_id=200,
        sku_template_id=1,
        override_id=701,
        binding_source="admin_manual",
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]),
        quote_type="retail",
        expected_product_code=PLATFORM_PRODUCT,
    )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE customer_agent_bindings SET agent_user_id=300 "
            "WHERE customer_user_id=400"
        )
        conn.commit()

    assert complete_recharge("retail-bound-before-rebind", "wx-bound-before-rebind") is not None

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT settlement_mode,agent_user_id FROM recharge_orders "
            "WHERE id='retail-bound-before-rebind'"
        )
        order = dict(cur.fetchone())
        # 🔴 [#118 1-8] 继任 `2aef3b59b`(2026-07-27 单账本收敛):退役账本不再有行,归属只认 customer_agent_bindings / agent_revenue_ledger。
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_credit_wallets"
                    " WHERE customer_user_id=400")
        assert int(cur.fetchone()["c"]) == 0, "退役账本又长出行 —— 两本账复活了"
        cur.execute(
            "SELECT agent_user_id FROM agent_revenue_ledger "
            "WHERE recharge_order_id='retail-bound-before-rebind'"
        )
        revenue_owner = int(cur.fetchone()["agent_user_id"])
    assert order == {"settlement_mode": "v35_inventory_settlement", "agent_user_id": 200}
    # 🔴 [#118 1-8] 原来这里用**退役账本的 agent_user_id** 断言归属。
    #    单账本收敛后那张表不再有行,归属唯一权威是下面的 `revenue_owner`
    #    (`agent_revenue_ledger`)—— 同一件事,换了个还活着的权威源。
    assert revenue_owner == 200


def test_platform_direct_quote_order_callback_uses_ready_provider_without_creating_binding(monkeypatch):
    """A ready platform provider routes the order without inventing a binding."""
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    scope = _service_scope()
    publish_retail(scope)
    set_flags(dual=True, quote_required=True, channel=False)
    quote = price_quote.issue_quote(
        quote_type="retail",
        scope_key=scope,
        product_code=PLATFORM_PRODUCT,
        buyer_user_id=500,
        commercial_service_source="platform_direct",
    )
    order = create_recharge_order(
        user_id=500,
        order_id="platform-direct-retail",
        amount_cents=int(quote["final_price_cents"]),
        base_points=int(quote["points_granted"]),
        bonus_points=int(quote["bonus_points"]),
        payment_method="wechat_native",
        order_type="customer_recharge",
        agent_user_id=200,
        sku_template_id=1,
        override_id=701,
        binding_source="platform_direct",
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]),
        quote_type="retail",
        expected_product_code=PLATFORM_PRODUCT,
    )
    assert order["pricing_snapshot_jsonb"]["commercial_service_source"] == "platform_direct"

    complete_recharge("platform-direct-retail", "wx-platform-direct")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_bindings WHERE customer_user_id=500")
        assert int(cur.fetchone()["c"]) == 0
        cur.execute("SELECT settlement_mode FROM recharge_orders WHERE id='platform-direct-retail'")
        assert cur.fetchone()["settlement_mode"] == "v35_platform_direct_settlement"
        # 🔴 [#118 1-8] 继任 `2aef3b59b`(2026-07-27 单账本收敛):退役账本不再有行,归属只认 customer_agent_bindings / agent_revenue_ledger。
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_credit_wallets"
                    " WHERE customer_user_id=500")
        assert int(cur.fetchone()["c"]) == 0, "退役账本又长出行 —— 两本账复活了"


def test_platform_direct_readiness_uses_quote_ssot_without_creating_quote(monkeypatch):
    from services.commercial_service_routing import platform_direct_readiness

    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    scope = _service_scope()
    publish_retail(scope)
    set_flags(dual=True, quote_required=True, channel=False)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM price_quotes")
        before = int(cur.fetchone()["c"])

    ready = platform_direct_readiness()
    assert ready["ready"] is True
    assert any("可生成报价" in check for check in ready["checks"])

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM price_quotes")
        assert int(cur.fetchone()["c"]) == before


def test_platform_direct_pending_order_keeps_snapshot_when_later_binding_is_created(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    scope = _service_scope()
    publish_retail(scope)
    set_flags(dual=True, quote_required=True, channel=False)
    quote = price_quote.issue_quote(
        quote_type="retail", scope_key=scope, product_code=PLATFORM_PRODUCT,
        buyer_user_id=500, commercial_service_source="platform_direct",
    )
    create_recharge_order(
        user_id=500, order_id="platform-direct-before-binding",
        amount_cents=int(quote["final_price_cents"]),
        base_points=int(quote["points_granted"]), bonus_points=int(quote["bonus_points"]),
        payment_method="wechat_native", order_type="customer_recharge",
        agent_user_id=200, sku_template_id=1, override_id=701,
        binding_source="platform_direct", price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]), quote_type="retail",
        expected_product_code=PLATFORM_PRODUCT,
    )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO customer_agent_bindings(
                   customer_user_id,agent_user_id,binding_source,bound_at)
               VALUES (500,100,'admin_manual',NOW())"""
        )

    complete_recharge("platform-direct-before-binding", "wx-platform-direct-late-binding")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=500")
        assert int(cur.fetchone()["agent_user_id"]) == 100
        # 🔴 [#118 1-8] 继任 `2aef3b59b`(2026-07-27 单账本收敛):退役账本不再有行,归属只认 customer_agent_bindings / agent_revenue_ledger。
        cur.execute("SELECT COUNT(*) AS c FROM customer_agent_credit_wallets"
                    " WHERE customer_user_id=500")
        assert int(cur.fetchone()["c"]) == 0, "退役账本又长出行 —— 两本账复活了"
        cur.execute(
            "SELECT settlement_mode FROM recharge_orders WHERE id='platform-direct-before-binding'"
        )
        assert cur.fetchone()["settlement_mode"] == "v35_platform_direct_settlement"


def test_platform_direct_marker_without_quote_snapshot_fails_closed_before_wallet_write():
    create_recharge_order(
        user_id=500, order_id="forged-platform-direct-marker",
        amount_cents=120000, base_points=13000, bonus_points=0,
        payment_method="wechat_native", order_type="customer_recharge",
        agent_user_id=200, sku_template_id=1, override_id=701,
        binding_source="platform_direct",
    )
    with pytest.raises(ValueError, match="不可变商业服务路由快照"):
        complete_recharge("forged-platform-direct-marker", "wx-forged-platform-direct")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT payment_status FROM recharge_orders WHERE id='forged-platform-direct-marker'"
        )
        assert cur.fetchone()["payment_status"] == "pending"
        cur.execute("SELECT paid_points,bonus_points FROM user_wallets WHERE user_id=500")
        wallet = cur.fetchone()
        assert int(wallet["paid_points"]) == 0
        assert int(wallet["bonus_points"]) == 0


def test_public_catalogs_hide_internal_account_codes_and_agent_auth_reads_wallet_ssot(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    scope = _service_scope()
    publish_retail(scope)
    publish_procurement()
    set_flags(dual=True, quote_required=True, channel=False)

    customer_request = _request({"user_id": 500, "is_admin": False})
    catalog = asyncio.run(retail_catalog(customer_request))["data"]
    with get_db() as conn:
        acceptance = record_purchase_acceptance(
            conn.cursor(),
            user_id=500,
            ip_address="127.0.0.1",
            user_agent="pricing-wiring-test",
            surface="customer-recharge",
        )
    quote = asyncio.run(retail_quote(
        RetailQuoteRequest(
            product_code=PLATFORM_PRODUCT,
            terms_acceptance_id=acceptance["acceptance_id"],
        ),
        customer_request,
    ))["data"]
    assert "service_account_code" not in catalog
    assert "service_account_code" not in quote

    # JWT intentionally has no agent_level.  Authorization must still accept the
    # service provider because user_wallets.agent_level is the business SSOT.
    provider_request = _request({"user_id": 100, "is_admin": False})
    procurement = asyncio.run(procurement_catalog(provider_request))["data"]
    proc_quote = asyncio.run(procurement_quote(
        ProcurementQuoteRequest(product_code=PLATFORM_PRODUCT), provider_request
    ))["data"]
    assert "channel_account_code" not in procurement
    assert "channel_account_code" not in proc_quote


def test_unbound_customer_platform_direct_quote_order_and_callbacks_are_atomic(monkeypatch):
    """Unbound customers use the configured internal principal without a lazy binding."""

    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    scope = _service_scope()
    publish_retail(scope)
    set_flags(dual=True, quote_required=False, channel=False)
    quote = price_quote.issue_quote(
        quote_type="retail",
        scope_key=scope,
        product_code=PLATFORM_PRODUCT,
        buyer_user_id=500,
        commercial_service_source="platform_direct",
    )
    order = create_recharge_order(
        user_id=500,
        order_id="retail-platform-direct-callback-once",
        amount_cents=int(quote["final_price_cents"]),
        base_points=int(quote["points_granted"]),
        bonus_points=int(quote["bonus_points"]),
        payment_method="wechat_native",
        order_type="customer_recharge",
        agent_user_id=200,
        sku_template_id=1,
        override_id=701,
        binding_source="platform_direct",
        price_quote_id=str(quote["quote_id"]),
        pricing_catalog_version=str(quote["catalog_version"]),
        quote_type="retail",
        expected_product_code=PLATFORM_PRODUCT,
    )
    assert order["pricing_snapshot_jsonb"]["commercial_resolution"] == "PLATFORM_DIRECT"

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(
            lambda index: complete_recharge(
                "retail-platform-direct-callback-once", f"wx-direct-{index}",
            ),
            range(20),
        ))
    assert all(result is not None for result in results)

    with get_db() as conn:
        cur = conn.cursor()
        facts = read_settlement_facts(
            cur, order_id="retail-platform-direct-callback-once", customer_user_id=500)
        cur.execute(
            "SELECT settlement_mode,payment_status FROM recharge_orders WHERE id=%s",
            ("retail-platform-direct-callback-once",),
        )
        paid_order = dict(cur.fetchone())
        cur.execute(
            "SELECT COUNT(*) AS c FROM customer_agent_bindings WHERE customer_user_id=500"
        )
        binding_count = int(cur.fetchone()["c"])
        cur.execute(
            "SELECT COUNT(*) AS c FROM agent_revenue_ledger WHERE recharge_order_id=%s",
            ("retail-platform-direct-callback-once",),
        )
        revenue_count = int(cur.fetchone()["c"])
    # 🔴 [#118 1-8] 原来这条断言 `customer_credit_transactions` 恰 1 行 ——
    #    那是 2026-07-27 单账本收敛**之前**的模型。收敛后该表 0 行,
    #    「入账恰一次」由 `point_transactions` 承担(不变量 ③),
    #    「不复制到第二本账」由不变量 ② 的否定臂承担。
    assert_single_ledger_settlement(facts)
    assert paid_order == {
        "settlement_mode": "v35_platform_direct_settlement",
        "payment_status": "paid",
    }
    assert binding_count == 0, "平台直营不该造绑定"
    assert revenue_count == 1


def test_unbound_order_resolution_serializes_concurrent_first_binding(monkeypatch):
    """A no-row relationship read must still serialize a concurrent first insert."""

    from services import commercial_service_routing, customer_binding

    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "200")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users(id,username,is_active) VALUES (510,'customer-510',1)")
        cur.execute("INSERT INTO user_wallets(user_id,agent_level) VALUES (510,0)")
    scope = _service_scope()
    publish_retail(scope, version_code="retail-v510")
    quote = price_quote.issue_quote(
        quote_type="retail",
        scope_key=scope,
        product_code=PLATFORM_PRODUCT,
        buyer_user_id=510,
        commercial_service_source="platform_direct",
    )

    resolved = threading.Event()
    allow_order_to_finish = threading.Event()
    binding_finished = threading.Event()
    order_errors: list[BaseException] = []
    binding_errors: list[BaseException] = []
    original_resolver = commercial_service_routing.resolve_commercial_relationship

    def paused_resolver(cursor, customer_user_id, *, for_update=False):
        relationship = original_resolver(
            cursor, customer_user_id, for_update=for_update,
        )
        if int(customer_user_id) == 510 and for_update:
            resolved.set()
            if not allow_order_to_finish.wait(5):
                raise AssertionError("test did not release order transaction")
        return relationship

    monkeypatch.setattr(
        commercial_service_routing, "resolve_commercial_relationship", paused_resolver,
    )

    def create_order() -> None:
        try:
            create_recharge_order(
                user_id=510,
                order_id="retail-platform-direct-binding-race",
                amount_cents=int(quote["final_price_cents"]),
                base_points=int(quote["points_granted"]),
                bonus_points=int(quote["bonus_points"]),
                payment_method="wechat_native",
                order_type="customer_recharge",
                agent_user_id=200,
                sku_template_id=1,
                override_id=701,
                binding_source="platform_direct",
                price_quote_id=str(quote["quote_id"]),
                pricing_catalog_version=str(quote["catalog_version"]),
                quote_type="retail",
                expected_product_code=PLATFORM_PRODUCT,
            )
        except BaseException as exc:  # captured for the parent assertion
            order_errors.append(exc)

    def insert_binding() -> None:
        try:
            with get_db() as conn:
                customer_binding.upsert_customer_agent_binding(
                    conn.cursor(), customer_user_id=510, agent_user_id=300,
                    binding_source="admin_manual", source_token="race-proof",
                )
        except BaseException as exc:  # captured for the parent assertion
            binding_errors.append(exc)
        finally:
            binding_finished.set()

    order_thread = threading.Thread(target=create_order)
    binding_thread = threading.Thread(target=insert_binding)
    order_thread.start()
    assert resolved.wait(5), "order did not reach the locked relationship resolution"
    binding_thread.start()
    try:
        assert not binding_finished.wait(0.75), (
            "concurrent first binding committed while the order transaction held its "
            "commercial relationship decision"
        )
    finally:
        allow_order_to_finish.set()
        order_thread.join(5)
        binding_thread.join(5)
    assert not order_thread.is_alive() and not binding_thread.is_alive()
    assert order_errors == []
    assert binding_errors == []


def test_cross_user_expired_replay_wrong_type_product_points_and_amount_rejected():
    quote = _retail_quote()

    cases = [
        ({"buyer_user_id": 500, "quote_type": "retail"}, "买方"),
        ({"buyer_user_id": 400, "quote_type": "procurement"}, "用途"),
        (
            {
                "buyer_user_id": 400,
                "quote_type": "retail",
                "expected_final_cents": 1,
            },
            "金额",
        ),
        (
            {
                "buyer_user_id": 400,
                "quote_type": "retail",
                "expected_product_code": "wrong-product",
            },
            "商品",
        ),
        (
            {
                "buyer_user_id": 400,
                "quote_type": "retail",
                "expected_points": 1,
            },
            "算力",
        ),
    ]
    for kwargs, message in cases:
        with get_db() as conn:
            with pytest.raises(QuoteError, match=message):
                price_quote.lock_and_validate(conn.cursor(), quote["quote_id"], **kwargs)

    expired = price_quote.issue_quote(
        quote_type="retail",
        scope_key=quote["pricing_snapshot_jsonb"]["catalog_scope_key"],
        product_code=PLATFORM_PRODUCT,
        buyer_user_id=400,
        ttl_seconds=-1,
    )
    with get_db() as conn:
        with pytest.raises(QuoteError, match="过期"):
            price_quote.lock_and_validate(
                conn.cursor(), expired["quote_id"], buyer_user_id=400, quote_type="retail"
            )

    _create_retail_order(quote, "retail-replay")
    with get_db() as conn:
        with pytest.raises(QuoteError, match="已被使用"):
            price_quote.lock_and_validate(
                conn.cursor(), quote["quote_id"], buyer_user_id=400, quote_type="retail"
            )


def test_same_quote_twenty_concurrent_orders_consume_exactly_once():
    quote = _retail_quote()

    def attempt(index: int):
        try:
            _create_retail_order(quote, f"race-order-{index}")
            return "created"
        except Exception as exc:  # expected losers cross the row lock after winner commits
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = list(pool.map(attempt, range(20)))

    assert outcomes.count("created") == 1, outcomes
    assert scalar("SELECT COUNT(*) FROM recharge_orders") == 1
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, used_order_id FROM price_quotes WHERE quote_id=%s", (quote["quote_id"],))
        persisted = dict(cur.fetchone())
    assert persisted["status"] == "consumed"
    assert persisted["used_order_id"].startswith("race-order-")


def test_retail_quote_rejected_after_customer_binding_changes():
    quote = _retail_quote()
    account_codes.prepare_account_codes(service_user_ids=[300], channel_user_ids=[])
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE customer_agent_bindings SET agent_user_id=300 WHERE customer_user_id=400"
        )

    with pytest.raises(QuoteError, match="relationship"):
        _create_retail_order(quote, "cross-binding-must-fail")
    assert scalar("SELECT COUNT(*) FROM recharge_orders") == 0
    assert scalar(
        "SELECT COUNT(*) FROM price_quotes WHERE quote_id=%s AND status='issued'",
        (quote["quote_id"],),
    ) == 1


def test_new_catalog_does_not_change_old_quote_or_pending_order():
    scope = _service_scope()
    old_version_id = publish_retail(scope, version_code="retail-v1", retail_cents=180000)
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO customer_agent_bindings(
                 customer_user_id,agent_user_id,binding_source,bound_at
               ) VALUES (400,200,'admin_manual',NOW())"""
        )
    set_flags(dual=True, quote_required=False, channel=False)
    old_quote = price_quote.issue_quote(
        quote_type="retail",
        scope_key=scope,
        product_code=PLATFORM_PRODUCT,
        buyer_user_id=400,
    )

    publish_retail(scope, version_code="retail-v2", retail_cents=210000)
    new_quote = price_quote.issue_quote(
        quote_type="retail",
        scope_key=scope,
        product_code=PLATFORM_PRODUCT,
        buyer_user_id=500,
    )
    assert old_quote["final_price_cents"] == 180000
    assert new_quote["final_price_cents"] == 210000

    pending = _create_retail_order(old_quote, "old-price-pending")
    assert pending["amount_cents"] == 180000
    assert pending["pricing_snapshot_jsonb"]["retail_cents"] == 180000
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM pricing_catalog_versions WHERE id=%s", (old_version_id,))
        assert cur.fetchone()["status"] == "archived"


def test_published_catalog_quote_and_order_pricing_anchors_are_db_immutable():
    quote = _retail_quote()
    order = _create_retail_order(quote, "immutable-order")

    with pytest.raises(psycopg2.DatabaseError):
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE pricing_catalog_entries SET final_price_cents=1 WHERE id=%s",
                (quote["pricing_snapshot_jsonb"]["catalog_entry_id"],),
            )

    with pytest.raises(psycopg2.DatabaseError):
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE price_quotes SET final_price_cents=1 WHERE quote_id=%s",
                (quote["quote_id"],),
            )

    with pytest.raises(psycopg2.DatabaseError):
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE recharge_orders SET pricing_snapshot_jsonb='{}'::jsonb WHERE id=%s",
                (order["id"],),
            )

    # Failure transactions rolled back; immutable truth is unchanged.
    assert scalar("SELECT final_price_cents FROM price_quotes WHERE quote_id=%s", (quote["quote_id"],)) == 180000
    assert scalar("SELECT amount_cents FROM recharge_orders WHERE id='immutable-order'") == 180000
