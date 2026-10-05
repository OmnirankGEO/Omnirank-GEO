from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from services import dealer_inventory_resale as resale

from .conftest import JIT_MIGRATION, JIT_ROLLBACK, MINTING_MIGRATION, TEST_URL

from .test_resale_funds import (
    _issue_manufacturer,
    _pay_order,
    _persist_trusted_b2b_refund_terminal,
    _prepare_order,
    _seed_users,
    _wallet,
)


def _seed_full_jit_chain(cur) -> None:
    _seed_users(cur, 1, 10, 20, 30)
    cur.execute(
        "UPDATE dealer_resale_global_settings SET platform_seller_user_id=1 WHERE singleton_id=1"
    )
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="jit-root")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'jit-20-10-v1',12000),(30,20,'jit-30-20-v1',12000)"""
    )


def _insert_promotion(
    cur, *, campaign_id: str, funding_scope: str, discount_bps: int,
    seller_user_id: int | None = None,
) -> None:
    cur.execute(
        """INSERT INTO dealer_resale_promotions
           (campaign_id,funding_scope,sponsor_user_id,seller_user_id,product_code,
            root_catalog_version,promotion_discount_bps,status,campaign_version,
            started_at,expires_at)
           VALUES (%s,%s,%s,%s,'WATER','proc-test-v1',%s,'active','v1',
                   NOW()-INTERVAL '1 hour',NOW()+INTERVAL '1 hour')""",
        (
            campaign_id, funding_scope, seller_user_id, seller_user_id,
            int(discount_bps),
        ),
    )


def test_full_jit_chain_reserves_root_and_settles_every_hop(db_conn):
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    plan = terms["fulfillment_plan"]
    assert [
        (hop["seller_user_id"], hop["buyer_user_id"], hop["sale_amount_cents"])
        for hop in plan["hops"]
    ] == [(1, 10, 50), (10, 20, 60), (20, 30, 72)]
    assert terms["sale_amount_cents"] == 72
    snapshot = _prepare_order(
        cur, order_id="O-JIT-ROOT-L2", quote_id="Q-JIT-ROOT-L2", terms=terms,
    )
    assert snapshot["resale_snapshot_version"] == 2
    # 按需铸造:平台跳不再预占任何存量批次 —— 预占阶段平台钱包完全不动
    assert (_wallet(cur, 1)["paid_inventory_points"], _wallet(cur, 1)["frozen_inventory_points"]) == (1, 0)
    assert _wallet(cur, 10)["paid_inventory_points"] == 0
    assert _wallet(cur, 20)["paid_inventory_points"] == 0
    cur.execute(
        "SELECT COALESCE(SUM(points),0) AS n FROM agent_inventory_transactions "
        "WHERE type='manufacturer_origin_in' AND related_order_id='O-JIT-ROOT-L2'"
    )
    assert int(cur.fetchone()["n"]) == 0, "未支付不得铸造"

    result = _pay_order(cur, order_id="O-JIT-ROOT-L2", buyer_id=30, points=1)
    assert result["jit_fulfillment_settled"] is True
    # 平台铸出 1 后当场卖出 → 净变化 0,原有 1 点存量原封不动
    assert [_wallet(cur, user_id)["paid_inventory_points"] for user_id in (1, 10, 20, 30)] == [1, 0, 0, 1]
    cur.execute(
        "SELECT points,pool,related_order_id,cost_basis_cents FROM agent_inventory_transactions "
        "WHERE type='manufacturer_origin_in' AND related_order_id='O-JIT-ROOT-L2'"
    )
    minted = cur.fetchall()
    assert [(int(r["points"]), r["pool"], r["related_order_id"], int(r["cost_basis_cents"]))
            for r in minted] == [(1, "paid", "O-JIT-ROOT-L2", 50)]
    cur.execute(
        """SELECT owner_agent_user_id,remaining_points,acquisition_cost_cents,status
           FROM dealer_inventory_lots WHERE root_order_id='O-JIT-ROOT-L2'
           ORDER BY source_hop_seq"""
    )
    assert [tuple(row.values()) for row in cur.fetchall()] == [
        (10, 0, 50, "consumed"),
        (20, 0, 60, "consumed"),
        (30, 1, 72, "active"),
    ]
    cur.execute(
        """SELECT seller_user_id,margin_cents,status FROM dealer_resale_hop_profit_ledger
           WHERE root_order_id='O-JIT-ROOT-L2' ORDER BY hop_seq"""
    )
    assert [tuple(row.values()) for row in cur.fetchall()] == [
        (1, 0, "pending"), (10, 10, "pending"), (20, 12, "pending"),
    ]
    cur.execute(
        "SELECT residual_cents FROM dealer_resale_fulfillment_plans, "
        "LATERAL jsonb_to_record(funds_conservation_jsonb) AS x(residual_cents bigint) "
        "WHERE root_order_id='O-JIT-ROOT-L2'"
    )
    assert int(cur.fetchone()["residual_cents"]) == 0
    cur.execute(
        "UPDATE dealer_resale_hop_profit_ledger "
        "SET available_at=NOW()-INTERVAL '1 second' "
        "WHERE root_order_id='O-JIT-ROOT-L2'"
    )
    matured = resale.mature_profits(cur=cur)
    assert matured["jit_matured_count"] == 3
    cur.execute(
        """SELECT agent_user_id,agent_margin_before_tax_cents,agent_settlement_cents
           FROM agent_revenue_ledger WHERE recharge_order_id='O-JIT-ROOT-L2'
           ORDER BY agent_user_id"""
    )
    assert [tuple(row.values()) for row in cur.fetchall()] == [
        (10, 10, 10), (20, 12, 12),
    ]
    actual = resale.verify_persisted_funds_ledger(
        cur, root_order_id="O-JIT-ROOT-L2", require_withdrawable=True,
    )
    assert actual == {
        "final_paid_cents": 72,
        "platform_root_revenue_cents": 50,
        "hop_agent_payable_cents": 22,
        "consumer_agent_payable_cents": 0,
        "durable_assigned_cents": 72,
        "actual_agent_ledger_cents": 22,
        "residual_cents": 0,
    }


def test_historical_principal_and_margin_enter_real_withdrawable_ledger(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="principal-root")

    root_terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-PRINCIPAL-ROOT", quote_id="Q-PRINCIPAL-ROOT", terms=root_terms)
    _pay_order(cur, order_id="O-PRINCIPAL-ROOT", buyer_id=10, points=1)
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'principal-edge-v1',12000)"""
    )
    resale_terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    assert (
        resale_terms["seller_cost_basis_cents"],
        resale_terms["principal_recovery_cents"],
        resale_terms["margin_cents"],
        resale_terms["agent_payable_cents"],
        resale_terms["sale_amount_cents"],
    ) == (50, 50, 10, 60, 60)
    _prepare_order(
        cur, order_id="O-PRINCIPAL-RESALE", quote_id="Q-PRINCIPAL-RESALE",
        terms=resale_terms,
    )
    _pay_order(cur, order_id="O-PRINCIPAL-RESALE", buyer_id=20, points=1)
    cur.execute(
        "UPDATE dealer_resale_hop_profit_ledger SET available_at=NOW()-INTERVAL '1 second'"
    )
    result = resale.mature_profits(cur=cur)
    assert result["jit_matured_count"] == 2
    cur.execute(
        """SELECT agent_user_id,agent_margin_before_tax_cents,agent_settlement_cents
           FROM agent_revenue_ledger WHERE recharge_order_id='O-PRINCIPAL-RESALE'"""
    )
    payable = cur.fetchone()
    assert tuple(payable.values()) == (10, 10, 60)
    cur.execute(
        "SELECT COUNT(*) AS n FROM agent_revenue_ledger WHERE recharge_order_id='O-PRINCIPAL-ROOT'"
    )
    assert int(cur.fetchone()["n"]) == 0
    cur.execute(
        """SELECT platform_root_revenue_cents,agent_payable_cents,revenue_ledger_id
           FROM dealer_resale_hop_profit_ledger WHERE root_order_id='O-PRINCIPAL-ROOT'"""
    )
    root_ledger = cur.fetchone()
    assert tuple(root_ledger.values()) == (50, 0, None)
    actual = resale.verify_persisted_funds_ledger(
        cur, root_order_id="O-PRINCIPAL-RESALE", require_withdrawable=True,
    )
    assert actual["actual_agent_ledger_cents"] == 60
    assert actual["residual_cents"] == 0


def test_jit_quote_does_not_write_or_reserve_inventory(db_conn):
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    assert (_wallet(cur, 1)["paid_inventory_points"], _wallet(cur, 1)["frozen_inventory_points"]) == (1, 0)
    cur.execute("SELECT COUNT(*) AS n FROM dealer_resale_fulfillment_plans")
    assert int(cur.fetchone()["n"]) == 0


def test_bonus_only_credit_is_blocked_until_base_resale_is_paid(db_conn):
    from services.agent_inventory import purchase_inventory_prepay

    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(
        cur, order_id="O-JIT-BONUS-GATE", quote_id="Q-JIT-BONUS-GATE", terms=terms,
    )
    # 赠送必须由订单**独立落库**地声明,否则新铸造护栏(一致性锁)会先一步拒绝,
    # 本用例要验的是 RESALE_BONUS_BEFORE_PAID 这道时序闸。
    cur.execute(
        "UPDATE recharge_orders SET bonus_points=5 WHERE id='O-JIT-BONUS-GATE'"
    )
    with pytest.raises(resale.ResaleError) as pending:
        purchase_inventory_prepay(
            cur, 30, paid_points=0, bonus_points=5,
            related_order_id="O-JIT-BONUS-GATE",
        )
    assert pending.value.code == "RESALE_BONUS_BEFORE_PAID"
    cur.execute(
        "SELECT bonus_inventory_points FROM agent_inventory_wallets WHERE agent_user_id=%s",
        (30,),
    )
    pending_wallet = cur.fetchone()
    assert pending_wallet is None or pending_wallet["bonus_inventory_points"] == 0

    _pay_order(cur, order_id="O-JIT-BONUS-GATE", buyer_id=30, points=1)
    purchase_inventory_prepay(
        cur, 30, paid_points=0, bonus_points=5,
        related_order_id="O-JIT-BONUS-GATE",
    )
    assert _wallet(cur, 30)["bonus_inventory_points"] == 5


def test_readiness_detects_payment_plan_and_hop_state_drift(db_conn):
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-JIT-READINESS", quote_id="Q-JIT-READINESS", terms=terms)
    cur.execute(
        """UPDATE dealer_resale_fulfillment_plans
           SET state='settled' WHERE root_order_id='O-JIT-READINESS'"""
    )
    result = resale.readiness(cur=cur)
    assert any(
        "JIT 履约计划/逐跳流水/资金守恒不一致: O-JIT-READINESS" in blocker
        for blocker in result["blockers"]
    )


def test_consumer_retail_price_jit_replenishes_direct_service_only(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20, 40)
    cur.execute(
        "UPDATE dealer_resale_global_settings SET platform_seller_user_id=1 WHERE singleton_id=1"
    )
    _issue_manufacturer(cur, points=12000, acquisition_cost_cents=5000, key="retail-jit-root")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'retail-20-10-v1',12000)"""
    )
    terms = resale.build_consumer_quote_terms(
        cur, seller_user_id=20, consumer_user_id=40, points=12000,
        catalog_reference_amount_cents=8000, platform_reference_amount_cents=5000,
        pricing_version="retail-sv20-v1", product_code="WATER",
        digital_goods_acknowledged=True,
    )
    assert (
        terms["seller_cost_basis_cents"], terms["sale_amount_cents"], terms["margin_cents"]
    ) == (6000, 8000, 2000)
    snapshot = {
        **terms, "price_quote_id": "Q-RETAIL-JIT", "quote_type": "retail",
        "buyer_user_id": 40, "buyer_paid_cents": 8000,
        "catalog_version": "retail-sv20-v1", "points_granted": 12000,
        "tool_points": 12000, "publish_points": 0, "bonus_points": 0,
    }
    cur.execute(
        """INSERT INTO price_quotes
           (quote_id,quote_type,catalog_version,product_code,buyer_user_id,
            points_granted,bonus_points,base_price_cents,effective_multiplier_bps,
            final_price_cents,expires_at,status,used_order_id,pricing_snapshot_jsonb)
           VALUES ('Q-RETAIL-JIT','retail','retail-sv20-v1','WATER',40,12000,0,5000,16000,8000,
                   NOW()+INTERVAL '15 minutes','consumed','O-RETAIL-JIT',%s::jsonb)""",
        (json.dumps({"order_pricing_snapshot": snapshot}),),
    )
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,agent_user_id,amount_cents,base_points,bonus_points,payment_method,
            payment_status,order_type,pricing_snapshot_jsonb,pricing_catalog_version,price_quote_id)
           VALUES ('O-RETAIL-JIT',40,20,8000,12000,0,'wechat','pending','customer_recharge',
                   %s::jsonb,'retail-sv20-v1','Q-RETAIL-JIT')""", (json.dumps(snapshot),),
    )
    resale.reserve_consumer_sale(cur, order_id="O-RETAIL-JIT", snapshot=snapshot)
    cur.execute(
        """UPDATE recharge_orders SET payment_status='paid',payment_id='PAY-RETAIL-JIT',
                   paid_at=NOW() WHERE id='O-RETAIL-JIT'"""
    )
    cur.execute("UPDATE user_wallets SET paid_points=12000,total_recharged=8000 WHERE user_id=40")
    result = resale.settle_consumer_sale(
        cur, order_id="O-RETAIL-JIT", consumer_user_id=40, pricing_snapshot=snapshot,
    )
    assert result["seller_margin_cents"] == 2000
    # 平台按需铸造 → 预铸的 12000 原封不动留在平台钱包
    assert [_wallet(cur, user_id)["paid_inventory_points"] for user_id in (1, 10, 20)] == [12000, 0, 0]
    cur.execute(
        """SELECT owner_agent_user_id,remaining_points,status FROM dealer_inventory_lots
           WHERE root_order_id='O-RETAIL-JIT' ORDER BY source_hop_seq"""
    )
    assert [tuple(row.values()) for row in cur.fetchall()] == [
        (10, 0, "consumed"), (20, 0, "consumed"),
    ]
    cur.execute(
        "SELECT tool_credit_points FROM customer_agent_credit_wallets WHERE customer_user_id=40"
    )
    assert int(cur.fetchone()["tool_credit_points"]) == 12000
    cur.execute(
        "SELECT agent_user_id,agent_margin_before_tax_cents FROM agent_revenue_ledger "
        "WHERE recharge_order_id='O-RETAIL-JIT'"
    )
    assert tuple(cur.fetchone().values()) == (20, 2000)
    cur.execute("SELECT COUNT(*) AS n FROM recharge_orders WHERE id='O-RETAIL-JIT'")
    assert int(cur.fetchone()["n"]) == 1, "JIT 中间 hop 不得创建或重复扣外部支付订单"

    case = resale.request_consumer_refund_case(
        cur, order_id="O-RETAIL-JIT", consumer_user_id=40,
        reason="unused", reason_category="change_of_mind",
    )
    resale.review_consumer_refund_case(
        cur, case_id=case["case_id"], actor_user_id=20,
        approve=True, note="unused credit confirmed",
    )
    refunded = resale.settle_consumer_refund_case(cur, case_id=case["case_id"])
    assert int(refunded["restored_inventory_points"]) == 12000
    assert _wallet(cur, 20)["paid_inventory_points"] == 12000
    cur.execute(
        "SELECT COUNT(*) AS n FROM dealer_consumer_jit_refund_lots WHERE source_order_id='O-RETAIL-JIT'"
    )
    assert int(cur.fetchone()["n"]) == 1
    cur.execute(
        """SELECT COUNT(*) AS n FROM dealer_resale_hop_profit_ledger
           WHERE root_order_id='O-RETAIL-JIT' AND status='pending'"""
    )
    assert int(cur.fetchone()["n"]) == 2, "客户退款不得冲销任一祖先采购 hop"


def test_partial_inventory_replenishes_only_shortfall(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=2, acquisition_cost_cents=100, key="partial-root")
    seed = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-PARTIAL-SEED", quote_id="Q-PARTIAL-SEED", terms=seed)
    _pay_order(cur, order_id="O-PARTIAL-SEED", buyer_id=10, points=1)
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'partial-v1',12000)"""
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    hops = terms["fulfillment_plan"]["hops"]
    # 平台跳(hop0)整段按需铸造:existing/shortfall 归 0,量落在 mint_points
    assert [(h["points"], h["existing_inventory_points"], h["jit_shortfall_points"],
             h["mint_points"]) for h in hops] == [(1, 0, 0, 1), (2, 1, 1, 0)]
    assert terms["seller_cost_basis_cents"] == 100
    assert terms["sale_amount_cents"] == 120


def test_sufficient_direct_seller_inventory_skips_platform(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=4, acquisition_cost_cents=200, key="sufficient-root")
    seed = resale.build_quote_terms(
        cur, buyer_user_id=10, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-SUFFICIENT-SEED", quote_id="Q-SUFFICIENT-SEED", terms=seed)
    _pay_order(cur, order_id="O-SUFFICIENT-SEED", buyer_id=10, points=2)
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'sufficient-v1',12000)"""
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    assert [(h["seller_user_id"], h["buyer_user_id"]) for h in terms["fulfillment_plan"]["hops"]] == [(10, 20)]
    # 平台从不消耗预铸存量(seed 单也是铸出来的),4 点原封不动
    assert _wallet(cur, 1)["paid_inventory_points"] == 4
    cur.execute(
        "SELECT remaining_points FROM dealer_inventory_lots "
        "WHERE owner_agent_user_id=1 AND source_kind='manufacturer_origin' "
        "AND lot_id LIKE 'DML%'"
    )
    assert [int(r["remaining_points"]) for r in cur.fetchall()] == [4]


def test_payment_setup_failure_releases_every_hop_reservation(db_conn):
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-JIT-CANCEL", quote_id="Q-JIT-CANCEL", terms=terms)
    # 平台跳按需铸造 → 预占阶段没有任何冻结可释放,也没有任何铸造发生
    assert _wallet(cur, 1)["frozen_inventory_points"] == 0
    cur.execute(
        "SELECT COUNT(*) AS c FROM agent_inventory_transactions "
        "WHERE type='manufacturer_origin_in' AND related_order_id='O-JIT-CANCEL'"
    )
    assert int(cur.fetchone()["c"]) == 0
    assert resale.cancel_reservation(cur, "O-JIT-CANCEL") is True
    assert (_wallet(cur, 1)["paid_inventory_points"], _wallet(cur, 1)["frozen_inventory_points"]) == (1, 0)
    cur.execute(
        "SELECT COUNT(*) AS c FROM agent_inventory_transactions "
        "WHERE type='manufacturer_origin_in' AND related_order_id='O-JIT-CANCEL'"
    )
    assert int(cur.fetchone()["c"]) == 0, "取消的订单绝不能留下铸造"
    cur.execute("SELECT state FROM dealer_resale_fulfillment_plans WHERE root_order_id='O-JIT-CANCEL'")
    assert cur.fetchone()["state"] == "cancelled"


def test_relationship_change_between_quote_and_order_is_409_style_stale(db_conn):
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    cur.execute(
        """UPDATE channel_pricing_relationships
           SET cost_multiplier_bps=13000,relationship_version='jit-30-20-v2'
           WHERE buyer_dealer_id=30"""
    )
    with pytest.raises(resale.ResaleError) as stale:
        _prepare_order(cur, order_id="O-JIT-STALE", quote_id="Q-JIT-STALE", terms=terms)
    assert stale.value.code == "JIT_QUOTE_STALE"
    assert _wallet(cur, 1)["frozen_inventory_points"] == 0


def test_platform_promotion_applies_once_and_pending_order_keeps_snapshot(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _insert_promotion(cur, campaign_id="P90", funding_scope="PLATFORM", discount_bps=9000)
    _issue_manufacturer(
        cur, points=1, acquisition_cost_cents=90, key="promo-root",
        pricing_version="proc-test-v1", promotion_campaign_id="P90",
    )
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (10,1,'promo-10-1-v1',12000),(20,10,'promo-20-10-v1',12000)"""
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    assert [h["sale_amount_cents"] for h in terms["fulfillment_plan"]["hops"]] == [108, 130]
    _prepare_order(cur, order_id="O-PROMO-PENDING", quote_id="Q-PROMO-PENDING", terms=terms)
    cur.execute("UPDATE dealer_resale_promotions SET status='ended' WHERE campaign_id='P90'")
    result = _pay_order(cur, order_id="O-PROMO-PENDING", buyer_id=20, points=1)
    assert result["jit_fulfillment_settled"] is True
    cur.execute("SELECT amount_cents FROM recharge_orders WHERE id='O-PROMO-PENDING'")
    assert int(cur.fetchone()["amount_cents"]) == 130


def test_seller_funded_discount_only_reduces_final_margin_and_rejects_negative(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=100, key="seller-promo-seed")
    seed = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-SELLER-PROMO-SEED", quote_id="Q-SELLER-PROMO-SEED", terms=seed)
    _pay_order(cur, order_id="O-SELLER-PROMO-SEED", buyer_id=10, points=1)
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'seller-promo-edge-v1',12000)"""
    )
    _insert_promotion(
        cur, campaign_id="S90", funding_scope="SELLER", discount_bps=9000,
        seller_user_id=10,
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    assert (terms["sale_amount_cents"], terms["margin_cents"]) == (108, 8)
    cur.execute("UPDATE dealer_inventory_lots SET remaining_cost_cents=110,acquisition_cost_cents=110 WHERE owner_agent_user_id=10")
    with pytest.raises(resale.ResaleError) as negative:
        resale.build_quote_terms(
            cur, buyer_user_id=20, points=1, platform_reference_amount_cents=100,
            pricing_version="proc-test-v1", product_code="WATER",
        )
    assert negative.value.code == "NEGATIVE_MARGIN"


def test_promotion_eligibility_is_enforced_and_unknown_predicates_fail_closed(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _insert_promotion(cur, campaign_id="ELIGIBLE-OTHER", funding_scope="PLATFORM", discount_bps=9000)
    cur.execute(
        "UPDATE dealer_resale_promotions SET eligibility_jsonb=%s::jsonb WHERE campaign_id='ELIGIBLE-OTHER'",
        (json.dumps({"buyer_user_ids": [999], "order_kinds": ["B2B"]}),),
    )
    lot = _issue_manufacturer(cur, points=1, acquisition_cost_cents=100, key="eligibility-root")
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    assert terms["sale_amount_cents"] == 100
    assert terms["fulfillment_plan"]["promotion"] == {}
    cur.execute(
        "UPDATE dealer_resale_promotions SET eligibility_jsonb='{\"region\":\"CN\"}'::jsonb "
        "WHERE campaign_id='ELIGIBLE-OTHER'"
    )
    with pytest.raises(resale.ResaleError) as unsupported:
        resale.build_quote_terms(
            cur, buyer_user_id=10, points=1, platform_reference_amount_cents=100,
            pricing_version="proc-test-v1", product_code="WATER",
        )
    assert unsupported.value.code == "PROMOTION_ELIGIBILITY_UNSUPPORTED"
    assert lot["lot_id"]


def test_promotion_end_between_quote_and_order_requires_requote(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _insert_promotion(cur, campaign_id="END-BEFORE-ORDER", funding_scope="PLATFORM", discount_bps=9000)
    _issue_manufacturer(
        cur, points=1, acquisition_cost_cents=90, key="end-before-root",
        pricing_version="proc-test-v1", promotion_campaign_id="END-BEFORE-ORDER",
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    cur.execute("UPDATE dealer_resale_promotions SET status='ended' WHERE campaign_id='END-BEFORE-ORDER'")
    with pytest.raises(resale.ResaleError) as stale:
        _prepare_order(cur, order_id="O-PROMO-ENDED", quote_id="Q-PROMO-ENDED", terms=terms)
    assert stale.value.code == "JIT_QUOTE_STALE"
    assert _wallet(cur, 1)["frozen_inventory_points"] == 0


def test_promotion_end_waits_for_order_revalidation_transaction(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _insert_promotion(
        cur, campaign_id="END-CONCURRENT", funding_scope="PLATFORM", discount_bps=9000,
    )
    _issue_manufacturer(
        cur, points=1, acquisition_cost_cents=90, key="end-concurrent-root",
        pricing_version="proc-test-v1", promotion_campaign_id="END-CONCURRENT",
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    db_conn.commit()
    order_ready = Event()
    allow_order_commit = Event()

    def reserve_and_hold() -> str:
        conn = psycopg2.connect(TEST_URL, cursor_factory=RealDictCursor)
        try:
            _prepare_order(
                conn.cursor(), order_id="O-PROMO-CONCURRENT",
                quote_id="Q-PROMO-CONCURRENT", terms=terms,
            )
            order_ready.set()
            if not allow_order_commit.wait(timeout=5):
                raise AssertionError("test did not release order transaction")
            conn.commit()
            return "reserved"
        finally:
            conn.close()

    def end_campaign() -> str:
        conn = psycopg2.connect(TEST_URL, cursor_factory=RealDictCursor)
        try:
            resale.end_promotion_admin(
                conn.cursor(), campaign_id="END-CONCURRENT",
                expected_row_version=1, updated_by=1,
            )
            conn.commit()
            return "ended"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        order_future = pool.submit(reserve_and_hold)
        assert order_ready.wait(timeout=5)
        end_future = pool.submit(end_campaign)
        time.sleep(0.25)
        assert end_future.done() is False
        allow_order_commit.set()
        assert order_future.result(timeout=5) == "reserved"
        assert end_future.result(timeout=5) == "ended"

    cur = db_conn.cursor()
    cur.execute(
        """SELECT o.payment_status,p.state,c.status
           FROM recharge_orders o
           JOIN dealer_resale_fulfillment_plans p ON p.root_order_id=o.id
           JOIN dealer_resale_promotions c ON c.campaign_id='END-CONCURRENT'
           WHERE o.id='O-PROMO-CONCURRENT'"""
    )
    assert tuple(cur.fetchone().values()) == ("pending", "reserved", "ended")


def test_promotion_activation_waits_for_no_campaign_order_revalidation(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(
        cur, points=1, acquisition_cost_cents=100, key="activate-concurrent-root",
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    assert terms["fulfillment_plan"]["promotion"] == {}
    db_conn.commit()
    order_ready = Event()
    allow_order_commit = Event()

    def reserve_and_hold() -> str:
        conn = psycopg2.connect(TEST_URL, cursor_factory=RealDictCursor)
        try:
            _prepare_order(
                conn.cursor(), order_id="O-PROMO-ACTIVATE-CONCURRENT",
                quote_id="Q-PROMO-ACTIVATE-CONCURRENT", terms=terms,
            )
            order_ready.set()
            if not allow_order_commit.wait(timeout=5):
                raise AssertionError("test did not release order transaction")
            conn.commit()
            return "reserved"
        finally:
            conn.close()

    def activate_campaign() -> str:
        conn = psycopg2.connect(TEST_URL, cursor_factory=RealDictCursor)
        try:
            now = datetime.now(timezone.utc)
            row = resale.save_promotion_admin(
                conn.cursor(), campaign_id="ACTIVATE-CONCURRENT",
                funding_scope="PLATFORM", seller_user_id=None,
                product_code="WATER", root_catalog_version="proc-test-v1",
                promotion_discount_bps=9000, eligibility={}, status="active",
                campaign_version="v1", started_at=now-timedelta(hours=1),
                expires_at=now+timedelta(hours=1), expected_row_version=None,
                updated_by=1,
            )
            conn.commit()
            return str(row["status"])
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        order_future = pool.submit(reserve_and_hold)
        assert order_ready.wait(timeout=5)
        activation_future = pool.submit(activate_campaign)
        time.sleep(0.25)
        assert activation_future.done() is False
        allow_order_commit.set()
        assert order_future.result(timeout=5) == "reserved"
        assert activation_future.result(timeout=5) == "active"

    cur = db_conn.cursor()
    cur.execute(
        """SELECT o.payment_status,p.state,c.status
           FROM recharge_orders o
           JOIN dealer_resale_fulfillment_plans p ON p.root_order_id=o.id
           JOIN dealer_resale_promotions c ON c.campaign_id='ACTIVATE-CONCURRENT'
           WHERE o.id='O-PROMO-ACTIVATE-CONCURRENT'"""
    )
    assert tuple(cur.fetchone().values()) == ("pending", "reserved", "active")


def test_seller_disabled_between_quote_and_order_fails_closed(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=100, key="disabled-root")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'disabled-edge-v1',12000)"""
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    cur.execute("UPDATE users SET is_active=0 WHERE id=10")
    with pytest.raises(resale.ResaleError) as unavailable:
        _prepare_order(cur, order_id="O-SELLER-DISABLED", quote_id="Q-SELLER-DISABLED", terms=terms)
    assert unavailable.value.code == "SELLER_UNAVAILABLE"
    assert _wallet(cur, 1)["frozen_inventory_points"] == 0


def test_mixed_historical_campaign_and_normal_lots_keep_cost_lineage(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _insert_promotion(cur, campaign_id="HIST-P90", funding_scope="PLATFORM", discount_bps=9000)
    _issue_manufacturer(
        cur, points=1, acquisition_cost_cents=90, key="mixed-promo-root",
        pricing_version="proc-test-v1", promotion_campaign_id="HIST-P90",
    )
    discounted = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-MIX-PROMO", quote_id="Q-MIX-PROMO", terms=discounted)
    _pay_order(cur, order_id="O-MIX-PROMO", buyer_id=10, points=1)
    cur.execute("UPDATE dealer_resale_promotions SET status='ended' WHERE campaign_id='HIST-P90'")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=100, key="mixed-normal-root")
    normal = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-MIX-NORMAL", quote_id="Q-MIX-NORMAL", terms=normal)
    _pay_order(cur, order_id="O-MIX-NORMAL", buyer_id=10, points=1)
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'mixed-edge-v1',12000)"""
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=2, platform_reference_amount_cents=200,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    allocations = terms["fulfillment_plan"]["hops"][-1]["allocations"]
    assert sorted(item["cost_basis_cents"] for item in allocations) == [90, 100]
    assert terms["seller_cost_basis_cents"] == 190
    assert (terms["sale_amount_cents"], terms["margin_cents"]) == (240, 50)


def test_v2_b2b_refund_reverses_only_final_hop(db_conn):
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-JIT-REFUND", quote_id="Q-JIT-REFUND", terms=terms)
    _pay_order(cur, order_id="O-JIT-REFUND", buyer_id=30, points=1)
    refund = resale.request_refund(
        cur, order_id="O-JIT-REFUND", requested_by_user_id=30,
    )
    assert refund["status"] == "requested"
    _persist_trusted_b2b_refund_terminal(
        cur, order_id="O-JIT-REFUND", amount_cents=72,
        provider_refund_id="WX-O-JIT-REFUND",
    )
    assert resale.sync_refund_from_recharge(cur, "O-JIT-REFUND") is True
    assert _wallet(cur, 30)["paid_inventory_points"] == 0
    assert _wallet(cur, 20)["paid_inventory_points"] == 1
    # 平台仍持有它那 1 点未被消费的预铸存量(按需铸造不吃它)
    assert [_wallet(cur, user_id)["paid_inventory_points"] for user_id in (1, 10)] == [1, 0]
    cur.execute(
        """SELECT hop_seq,status FROM dealer_resale_hop_profit_ledger
           WHERE root_order_id='O-JIT-REFUND' ORDER BY hop_seq"""
    )
    assert [tuple(row.values()) for row in cur.fetchall()] == [
        (0, "pending"), (1, "pending"), (2, "reversed"),
    ]
    readiness = resale.readiness(cur=cur)
    assert not any(
        blocker.startswith("JIT 履约计划/逐跳流水/资金守恒不一致: O-JIT-REFUND")
        for blocker in readiness["blockers"]
    )
    cur.execute("SAVEPOINT partial_refund_allocation_tamper")
    cur.execute(
        """UPDATE dealer_resale_fulfillment_allocations SET status='consumed'
           WHERE plan_id=(SELECT plan_id FROM dealer_resale_fulfillment_plans
                          WHERE root_order_id='O-JIT-REFUND')
             AND hop_seq=(SELECT MAX(hop_seq) FROM dealer_resale_fulfillment_hops
                          WHERE root_order_id='O-JIT-REFUND')
             AND allocation_seq=0"""
    )
    tampered = resale.readiness(cur=cur)
    assert any("O-JIT-REFUND" in blocker for blocker in tampered["blockers"])
    cur.execute("ROLLBACK TO SAVEPOINT partial_refund_allocation_tamper")


def test_v2_b2b_refund_claws_back_tier_and_founder_bonus_once(db_conn):
    """A cash refund must reverse the final paid lot and its restricted rewards."""
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    snapshot = _prepare_order(
        cur, order_id="O-JIT-BONUS-REFUND", quote_id="Q-JIT-BONUS-REFUND", terms=terms,
    )
    snapshot.update({
        "channel_tier_enabled": True,
        "tier_bonus_points": 30,
        "bonus_points": 30,
        "total_points": 31,
    })
    cur.execute(
        "UPDATE recharge_orders SET bonus_points=30,pricing_snapshot_jsonb=%s::jsonb WHERE id=%s",
        (json.dumps(snapshot), "O-JIT-BONUS-REFUND"),
    )
    _pay_order(cur, order_id="O-JIT-BONUS-REFUND", buyer_id=30, points=1)
    cur.execute(
        """UPDATE agent_inventory_wallets
              SET bonus_inventory_points=bonus_inventory_points+50,
                  total_purchased_points=total_purchased_points+50
            WHERE agent_user_id=30"""
    )
    cur.execute(
        """INSERT INTO bonus_grants(
               grant_key,owner_type,owner_id,granted_points,grant_type,
               related_order_id,expires_at)
             VALUES
               ('tier:jit-refund','agent',30,30,'tier_purchase',%s,NOW()+INTERVAL '1 month'),
               ('founder:jit-refund','agent',30,20,'founder_first_order',%s,NOW()+INTERVAL '1 month')""",
        ("O-JIT-BONUS-REFUND", "O-JIT-BONUS-REFUND"),
    )

    refund = resale.request_refund(
        cur, order_id="O-JIT-BONUS-REFUND", requested_by_user_id=30,
    )
    assert refund["status"] == "requested"
    reserved_wallet = _wallet(cur, 30)
    assert (
        reserved_wallet["paid_inventory_points"],
        reserved_wallet["bonus_inventory_points"],
        reserved_wallet["frozen_inventory_points"],
    ) == (0, 0, 51)
    _persist_trusted_b2b_refund_terminal(
        cur, order_id="O-JIT-BONUS-REFUND", amount_cents=72,
        provider_refund_id="WX-O-JIT-BONUS-REFUND",
    )
    assert resale.sync_refund_from_recharge(cur, "O-JIT-BONUS-REFUND") is True
    assert resale.sync_refund_from_recharge(cur, "O-JIT-BONUS-REFUND") is True
    wallet = _wallet(cur, 30)
    assert (
        wallet["paid_inventory_points"], wallet["bonus_inventory_points"],
        wallet["frozen_inventory_points"], wallet["total_purchased_points"],
    ) == (0, 0, 0, 0)
    cur.execute(
        "SELECT COUNT(*) AS n FROM agent_inventory_transactions "
        "WHERE related_order_id='O-JIT-BONUS-REFUND' AND pool='bonus' AND points=-50"
    )
    assert int(cur.fetchone()["n"]) == 1
    cur.execute(
        "SELECT COUNT(*) AS n FROM bonus_grants WHERE related_order_id=%s "
        "AND status='fully_consumed' AND consumed_points=granted_points",
        ("O-JIT-BONUS-REFUND",),
    )
    assert int(cur.fetchone()["n"]) == 2


def test_campaign_lot_rejects_wrong_catalog_and_readiness_detects_wrong_product(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _insert_promotion(
        cur, campaign_id="LINEAGE-P90", funding_scope="PLATFORM", discount_bps=9000,
    )
    with pytest.raises(resale.ResaleError) as wrong_catalog:
        _issue_manufacturer(
            cur, points=1, acquisition_cost_cents=90, key="wrong-catalog-root",
            pricing_version="wrong-catalog-v999", promotion_campaign_id="LINEAGE-P90",
        )
    assert wrong_catalog.value.code == "PROMOTION_ORIGIN_CATALOG_MISMATCH"

    lot = _issue_manufacturer(
        cur, points=1, acquisition_cost_cents=90, key="wrong-product-root",
        pricing_version="proc-test-v1",
    )
    cur.execute(
        """UPDATE dealer_inventory_lots
           SET promotion_campaign_id='LINEAGE-P90',promotion_funding_scope='PLATFORM',
               promotion_product_code='WRONG-PRODUCT',
               promotion_root_catalog_version='proc-test-v1',
               promotion_snapshot_jsonb='{"campaign_id":"LINEAGE-P90"}'::jsonb
           WHERE lot_id=%s""",
        (lot["lot_id"],),
    )
    status = resale.readiness(cur=cur)
    assert status["ready"] is False
    assert any("活动库存产品/根目录版本血缘错误" in item for item in status["blockers"])


def test_admin_sees_full_jit_chain_but_buyer_sees_only_current_order(db_conn):
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-JIT-VIS", quote_id="Q-JIT-VIS", terms=terms)
    _pay_order(cur, order_id="O-JIT-VIS", buyer_id=30, points=1)
    chain = resale.admin_order_chain(cur, "O-JIT-VIS")
    assert chain["count"] == 3
    assert [(hop["seller_user_id"], hop["buyer_user_id"]) for hop in chain["hops"]] == [
        (1, 10), (10, 20), (20, 30),
    ]
    assert all(hop["profit"] for hop in chain["hops"])
    buyer = resale.get_order_visible(
        cur, order_id="O-JIT-VIS", actor_user_id=30, is_admin=False,
    )
    forbidden = {
        "seller_user_id", "buyer_user_id", "seller_cost_basis_cents", "margin_cents",
        "downstream_markup_bps", "relationship_version", "seller_lot_allocations",
        "fulfillment_plan", "hops",
    }
    assert forbidden.isdisjoint(buyer)


def test_v2_inventory_already_resold_blocks_original_order_refund(db_conn):
    cur = db_conn.cursor()
    _seed_full_jit_chain(cur)
    _seed_users(cur, 40)
    first = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-JIT-BEFORE-DOWNSELL", quote_id="Q-JIT-BEFORE-DOWNSELL", terms=first)
    _pay_order(cur, order_id="O-JIT-BEFORE-DOWNSELL", buyer_id=30, points=1)
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (40,30,'downsold-edge-v1',12000)"""
    )
    second = resale.build_quote_terms(
        cur, buyer_user_id=40, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-JIT-DOWNSOLD", quote_id="Q-JIT-DOWNSOLD", terms=second)
    _pay_order(cur, order_id="O-JIT-DOWNSOLD", buyer_id=40, points=1)
    with pytest.raises(resale.ResaleError) as used:
        resale.request_refund(
            cur, order_id="O-JIT-BEFORE-DOWNSELL", requested_by_user_id=30,
        )
    assert used.value.code == "REFUND_INVENTORY_ALREADY_USED"


def test_jit_migration_twice_rollback_forward_and_tamper_detection(db_conn):
    cur = db_conn.cursor()
    cur.execute(JIT_ROLLBACK.read_text(encoding="utf-8"))
    cur.execute(JIT_MIGRATION.read_text(encoding="utf-8"))
    cur.execute(JIT_MIGRATION.read_text(encoding="utf-8"))
    # 按需铸造迁移建在 JIT 表之上(mint_points 列 + hop_values 等式),
    # JIT 表被 rollback→forward 重建后必须跟着复跑,否则 schema_status 契约当场缺列。
    cur.execute(MINTING_MIGRATION.read_text(encoding="utf-8"))
    cur.execute(MINTING_MIGRATION.read_text(encoding="utf-8"))
    db_conn.commit()
    cur = db_conn.cursor()
    assert resale.schema_status(cur)["ready"] is True

    cur.execute("SAVEPOINT tamper_jit_constraint")
    cur.execute(
        "ALTER TABLE dealer_resale_fulfillment_hops "
        "DROP CONSTRAINT chk_dealer_fulfillment_hop_money"
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "chk_dealer_fulfillment_hop_money" in status["invalid_constraints"]
    cur.execute("ROLLBACK TO SAVEPOINT tamper_jit_constraint")

    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="immutable-proof-root")
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1", product_code="WATER",
    )
    _prepare_order(cur, order_id="O-JIT-IMMUTABLE", quote_id="Q-JIT-IMMUTABLE", terms=terms)
    cur.execute("SAVEPOINT tamper_jit_proof")
    with pytest.raises(psycopg2.errors.RaiseException):
        cur.execute(
            "UPDATE dealer_resale_fulfillment_plans "
            "SET funds_conservation_jsonb='{\"residual_cents\":1}'::jsonb "
            "WHERE root_order_id='O-JIT-IMMUTABLE'"
        )
    cur.execute("ROLLBACK TO SAVEPOINT tamper_jit_proof")


def test_schema_contract_rejects_type_null_check_and_index_tampering(db_conn):
    cur = db_conn.cursor()
    assert resale.schema_status(cur)["ready"] is True

    cur.execute("SAVEPOINT tamper_money_type")
    cur.execute(
        "ALTER TABLE dealer_resale_hop_profit_ledger "
        "DROP CONSTRAINT chk_dealer_hop_profit_money, "
        "DROP CONSTRAINT chk_dealer_hop_profit_payout_v2"
    )
    cur.execute(
        "ALTER TABLE dealer_resale_hop_profit_ledger "
        "ALTER COLUMN principal_recovery_cents TYPE INTEGER"
    )
    cur.execute(
        """ALTER TABLE dealer_resale_hop_profit_ledger
           ADD CONSTRAINT chk_dealer_hop_profit_money CHECK (
             seller_cost_basis_cents>0 AND sale_amount_cents>0
             AND principal_recovery_cents>=0 AND margin_cents>=0
             AND platform_root_revenue_cents>=0
             AND sale_amount_cents=seller_cost_basis_cents+margin_cents
             AND principal_recovery_cents<=seller_cost_basis_cents
             AND platform_root_revenue_cents<=sale_amount_cents),
           ADD CONSTRAINT chk_dealer_hop_profit_payout_v2 CHECK (
             (platform_root_revenue_cents=sale_amount_cents
              AND principal_recovery_cents=0 AND agent_payable_cents=0)
             OR (platform_root_revenue_cents=0
                 AND agent_payable_cents=principal_recovery_cents+margin_cents))"""
    )
    status = resale.schema_status(cur)
    assert "dealer_resale_hop_profit_ledger.principal_recovery_cents" in status[
        "invalid_column_contracts"
    ]
    cur.execute("ROLLBACK TO SAVEPOINT tamper_money_type")

    cur.execute("SAVEPOINT tamper_money_check")
    cur.execute(
        "ALTER TABLE dealer_resale_hop_profit_ledger "
        "DROP CONSTRAINT chk_dealer_hop_profit_money"
    )
    cur.execute(
        "ALTER TABLE dealer_resale_hop_profit_ledger "
        "ADD CONSTRAINT chk_dealer_hop_profit_money CHECK (margin_cents>=-999999999)"
    )
    status = resale.schema_status(cur)
    assert "chk_dealer_hop_profit_money" not in status["invalid_constraints"]
    assert "chk_dealer_hop_profit_money" in status["invalid_constraint_definitions"]
    cur.execute("ROLLBACK TO SAVEPOINT tamper_money_check")

    cur.execute("SAVEPOINT tamper_constraint_owner")
    cur.execute(
        "ALTER TABLE dealer_resale_hop_profit_ledger "
        "DROP CONSTRAINT chk_dealer_hop_profit_money"
    )
    cur.execute(
        """CREATE TABLE dealer_hop_profit_constraint_decoy (
             seller_cost_basis_cents BIGINT NOT NULL,
             sale_amount_cents BIGINT NOT NULL,
             principal_recovery_cents BIGINT NOT NULL,
             margin_cents BIGINT NOT NULL,
             platform_root_revenue_cents BIGINT NOT NULL,
             CONSTRAINT chk_dealer_hop_profit_money CHECK (
               seller_cost_basis_cents>0 AND sale_amount_cents>0
               AND principal_recovery_cents>=0 AND margin_cents>=0
               AND platform_root_revenue_cents>=0
               AND sale_amount_cents=seller_cost_basis_cents+margin_cents
               AND principal_recovery_cents<=seller_cost_basis_cents
               AND platform_root_revenue_cents<=sale_amount_cents
             )
           )"""
    )
    status = resale.schema_status(cur)
    assert "chk_dealer_hop_profit_money" in status["invalid_constraint_definitions"]
    cur.execute("ROLLBACK TO SAVEPOINT tamper_constraint_owner")

    cur.execute("SAVEPOINT tamper_money_null")
    cur.execute(
        "ALTER TABLE dealer_resale_hop_profit_ledger "
        "ALTER COLUMN agent_payable_cents DROP NOT NULL"
    )
    status = resale.schema_status(cur)
    assert "dealer_resale_hop_profit_ledger.agent_payable_cents" in status[
        "invalid_column_contracts"
    ]
    cur.execute("ROLLBACK TO SAVEPOINT tamper_money_null")

    cur.execute("SAVEPOINT tamper_promotion_index")
    cur.execute("DROP INDEX idx_dealer_lot_promotion_fifo_v2")
    cur.execute(
        """CREATE INDEX idx_dealer_lot_promotion_fifo_v2
           ON dealer_inventory_lots(owner_agent_user_id)
           WHERE status='active'"""
    )
    status = resale.schema_status(cur)
    assert "idx_dealer_lot_promotion_fifo_v2" in status["invalid_index_definitions"]
    cur.execute("ROLLBACK TO SAVEPOINT tamper_promotion_index")
    assert resale.schema_status(cur)["ready"] is True


def test_schema_contract_rejects_same_name_constraint_on_wrong_table(db_conn):
    cur = db_conn.cursor()
    assert resale.schema_status(cur)["ready"] is True
    cur.execute(
        "ALTER TABLE dealer_resale_profit_ledger "
        "DROP CONSTRAINT chk_dealer_profit_money"
    )
    cur.execute(
        """CREATE TABLE dealer_profit_constraint_decoy (
             margin_cents BIGINT,
             CONSTRAINT chk_dealer_profit_money CHECK (margin_cents>=0)
           )"""
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "chk_dealer_profit_money" in status["invalid_constraint_objects"]


def test_schema_contract_rejects_same_name_trigger_on_wrong_table(db_conn):
    cur = db_conn.cursor()
    assert resale.schema_status(cur)["ready"] is True
    cur.execute("DROP TRIGGER trg_dealer_resale_order_immutable ON dealer_resale_orders")
    cur.execute("CREATE TABLE dealer_order_trigger_decoy (id BIGINT)")
    cur.execute(
        """CREATE TRIGGER trg_dealer_resale_order_immutable
           BEFORE UPDATE ON dealer_order_trigger_decoy
           FOR EACH ROW EXECUTE FUNCTION dealer_resale_order_immutable_guard()"""
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "trg_dealer_resale_order_immutable" in status["invalid_trigger_definitions"]


def test_schema_contract_rejects_trigger_timing_and_function_tampering(db_conn):
    cur = db_conn.cursor()
    assert resale.schema_status(cur)["ready"] is True

    cur.execute("SAVEPOINT trigger_timing_tamper")
    cur.execute("DROP TRIGGER trg_dealer_resale_order_immutable ON dealer_resale_orders")
    cur.execute(
        """CREATE TRIGGER trg_dealer_resale_order_immutable
           AFTER UPDATE ON dealer_resale_orders
           FOR EACH ROW EXECUTE FUNCTION dealer_resale_order_immutable_guard()"""
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "trg_dealer_resale_order_immutable" in status["invalid_trigger_definitions"]
    cur.execute("ROLLBACK TO SAVEPOINT trigger_timing_tamper")

    cur.execute("SAVEPOINT trigger_function_tamper")
    cur.execute("DROP TRIGGER trg_dealer_resale_order_immutable ON dealer_resale_orders")
    cur.execute(
        """CREATE TRIGGER trg_dealer_resale_order_immutable
           BEFORE UPDATE ON dealer_resale_orders
           FOR EACH ROW EXECUTE FUNCTION dealer_consumer_sale_immutable_guard()"""
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "trg_dealer_resale_order_immutable" in status["invalid_trigger_definitions"]


def test_schema_contract_rejects_same_name_unique_index_on_wrong_columns(db_conn):
    cur = db_conn.cursor()
    assert resale.schema_status(cur)["ready"] is True
    cur.execute("DROP INDEX ux_dealer_lot_source_order")
    cur.execute(
        """CREATE UNIQUE INDEX ux_dealer_lot_source_order
           ON dealer_inventory_lots(owner_agent_user_id)"""
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "ux_dealer_lot_source_order" in status["invalid_index_definitions"]
