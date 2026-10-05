from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import psycopg2
import pytest

from services import dealer_inventory_resale as resale
from services.agent_inventory_pricing import finalize_quote_snapshot
from services.price_quote import _neutral_procurement_snapshot

from .conftest import MIGRATION, REFUND_AGREEMENT_MIGRATION, ROLLBACK, ROOT, connect


def _seed_users(cur, *user_ids: int) -> None:
    for user_id in user_ids:
        cur.execute(
            "INSERT INTO users(id,username,is_active) VALUES (%s,%s,1)",
            (user_id, f"user-{user_id}"),
        )
        cur.execute("INSERT INTO user_wallets(user_id,agent_level) VALUES (%s,1)", (user_id,))
        cur.execute(
            """INSERT INTO public_account_codes
               (user_id,service_account_code,channel_account_code)
               VALUES (%s,%s,%s)""",
            (user_id, f"SV-TEST{user_id:04d}", f"CH-TEST{user_id:04d}"),
        )


def _wallet(cur, user_id: int) -> dict:
    cur.execute("SELECT * FROM agent_inventory_wallets WHERE agent_user_id=%s", (user_id,))
    return dict(cur.fetchone())


def _persist_trusted_b2b_refund_terminal(
    cur, *, order_id: str, amount_cents: int, provider_refund_id: str,
) -> None:
    """Test fixture for the persisted result of a signed provider callback/query."""

    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=%s,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || %s::jsonb
           WHERE id=%s""",
        (
            int(amount_cents),
            json.dumps({
                "refund_provider": "wechat",
                "provider_refund_id": str(provider_refund_id),
                "refund_evidence_kind": "signed_wechat_callback",
            }),
            str(order_id),
        ),
    )
    assert cur.rowcount == 1


def _issue_manufacturer(
    cur, *, points: int, acquisition_cost_cents: int, key: str,
    pricing_version: str = "factory-cost-v1",
    promotion_campaign_id: str | None = None,
) -> dict:
    # 显式发行厂家批次 = 这套夹具对「平台单位账面成本」的声明。
    # 按需铸造用同一个单价铸出(生产里就是 100,000,000 分 / 130,000,000 算力),
    # 因此这里把护栏配置对齐到本次发行的单价 —— 平台跳毛利在改造前后逐笔不变。
    cur.execute(
        """INSERT INTO system_settings(key,value,value_type)
           VALUES ('inventory_minting_guard_config',%s,'json')
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,value_type='json'""",
        (
            json.dumps({
                "mint_cost_numerator_cents": int(acquisition_cost_cents),
                "mint_cost_denominator_points": int(points),
            }),
        ),
    )
    return resale.issue_manufacturer_lot(
        cur, points=points, acquisition_cost_cents=acquisition_cost_cents,
        pricing_version=pricing_version, idempotency_key=key,
        evidence={"fixture": "explicit manufacturer origin", "invoice": key},
        issued_by=1, promotion_campaign_id=promotion_campaign_id,
    )


def _make_snapshot(terms: dict, quote_id: str) -> dict:
    snapshot = _neutral_procurement_snapshot(
        dealer_id=int(terms["buyer_user_id"]),
        catalog_version=str(terms["pricing_version"]),
        product_code="WATER",
        final_cents=int(terms["sale_amount_cents"]),
        points_granted=int(terms["points"]),
        bonus_points=0,
    )
    snapshot.update(terms)
    snapshot.update(
        {
            "price_quote_id": quote_id,
            "quote_type": "procurement",
            "catalog_version_id": 1,
            "catalog_entry_id": 1,
            "product_code": "WATER",
            "buyer_paid_cents": int(terms["sale_amount_cents"]),
            "buyer_user_id": int(terms["buyer_user_id"]),
            "amount_cents": int(terms["sale_amount_cents"]),
            "discount_source": "dealer_inventory_resale_quote",
            "discount_numer": int(terms["sale_amount_cents"]),
            "discount_denom": int(terms["points"]),
            "channel_account_code": None,
            "channel_beneficiary_user_id": None,
            "upstream_cost_basis_cents": None,
        }
    )
    return finalize_quote_snapshot(snapshot, agent_user_id=int(terms["buyer_user_id"]))


def _prepare_order(cur, *, order_id: str, quote_id: str, terms: dict) -> dict:
    snapshot = _make_snapshot(terms, quote_id)
    cur.execute(
        """INSERT INTO price_quotes
           (quote_id,quote_type,catalog_version,product_code,buyer_user_id,
            points_granted,bonus_points,base_price_cents,effective_multiplier_bps,
            final_price_cents,expires_at,status,pricing_snapshot_jsonb)
           VALUES (%s,'procurement',%s,'WATER',%s,%s,0,%s,%s,%s,
                   NOW()+INTERVAL '15 minutes','issued',%s::jsonb)""",
        (
            quote_id, terms["pricing_version"], terms["buyer_user_id"], terms["points"],
            terms["seller_cost_basis_cents"], terms["downstream_markup_bps"],
            terms["sale_amount_cents"], json.dumps({"order_pricing_snapshot": snapshot}),
        ),
    )
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,amount_cents,base_points,bonus_points,payment_method,
            payment_status,order_type,pricing_snapshot_jsonb,pricing_catalog_version,
            price_quote_id,agent_inventory_writer_generation)
           VALUES (%s,%s,%s,%s,0,'wechat','pending','agent_inventory_purchase',
                   %s::jsonb,%s,%s,2)""",
        (
            order_id, terms["buyer_user_id"], terms["sale_amount_cents"], terms["points"],
            json.dumps(snapshot), terms["pricing_version"], quote_id,
        ),
    )
    resale.reserve_order(cur, order_id=order_id, snapshot=snapshot)
    return snapshot


def _pay_order(cur, *, order_id: str, buyer_id: int, points: int) -> dict:
    cur.execute(
        "UPDATE recharge_orders SET payment_status='paid',payment_id=%s,paid_at=NOW() WHERE id=%s",
        (f"PAY-{order_id}", order_id),
    )
    return resale.settle_reserved_order(
        cur, buyer_user_id=buyer_id, total_points=points, order_id=order_id
    )


def _setup_water_chain(conn):
    cur = conn.cursor()
    _seed_users(cur, 1, 10, 20, 30, 40)
    cur.execute(
        "UPDATE dealer_resale_global_settings SET platform_seller_user_id=1 WHERE singleton_id=1"
    )
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="water-origin")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'rel-20-10-v1',12000),(30,20,'rel-30-20-v1',11666)"""
    )
    cur.execute(
        """INSERT INTO dealer_resale_policies
           (seller_user_id,downstream_markup_bps,authorized_min_markup_bps,
            authorized_max_markup_bps,policy_version)
           VALUES (10,12000,10000,20000,'p10-v1'),
                  (20,11666,10000,20000,'p20-v1'),
                  (30,11428,10000,20000,'p30-v1')"""
    )

    terms_1 = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    assert terms_1["sale_amount_cents"] == 50
    _prepare_order(cur, order_id="O-PLATFORM-L1", quote_id="Q-PLATFORM-L1", terms=terms_1)
    _pay_order(cur, order_id="O-PLATFORM-L1", buyer_id=10, points=1)

    terms_2 = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    assert (terms_2["seller_cost_basis_cents"], terms_2["sale_amount_cents"], terms_2["margin_cents"]) == (50, 60, 10)
    _prepare_order(cur, order_id="O-L1-L2", quote_id="Q-L1-L2", terms=terms_2)
    _pay_order(cur, order_id="O-L1-L2", buyer_id=20, points=1)

    terms_3 = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    assert (terms_3["seller_cost_basis_cents"], terms_3["sale_amount_cents"], terms_3["margin_cents"]) == (60, 70, 10)
    _prepare_order(cur, order_id="O-L2-SVC", quote_id="Q-L2-SVC", terms=terms_3)
    _pay_order(cur, order_id="O-L2-SVC", buyer_id=30, points=1)
    conn.commit()
    return cur


def _sell_water_to_consumer(
    conn, *, paid_at_sql: str = "NOW()",
    purchase_terms_version: str | None = None,
    purchase_terms_hash: str | None = None,
) -> dict:
    cur = _setup_water_chain(conn)
    purchase_evidence = {}
    if purchase_terms_version is not None:
        from services.legal_agreements import record_purchase_acceptance

        acceptance = record_purchase_acceptance(
            cur,
            user_id=40,
            ip_address="203.0.113.40",
            user_agent="pytest-historical-terms",
            surface="customer-recharge",
        )
        cur.execute(
            """UPDATE purchase_agreement_acceptances
               SET agreement_version=%s,content_hash=%s
               WHERE acceptance_id=%s""",
            (
                purchase_terms_version,
                purchase_terms_hash,
                acceptance["acceptance_id"],
            ),
        )
        purchase_evidence = {
            "terms_acceptance_id": acceptance["acceptance_id"],
            "terms_version": purchase_terms_version,
            "terms_content_hash": purchase_terms_hash,
        }
    terms = resale.build_consumer_quote_terms(
        cur, seller_user_id=30, consumer_user_id=40, points=1,
        catalog_reference_amount_cents=80,
        pricing_version="retail-sv30-v1", digital_goods_acknowledged=True,
    )
    assert (
        terms["seller_cost_basis_cents"], terms["sale_amount_cents"], terms["margin_cents"]
    ) == (70, 80, 10)
    snapshot = {
        **terms,
        "price_quote_id": "Q-SVC-CUSTOMER",
        "quote_type": "retail",
        "buyer_user_id": 40,
        "buyer_paid_cents": 80,
        "catalog_version": "retail-sv30-v1",
        "points_granted": 1,
        "tool_points": 1,
        "publish_points": 0,
        "bonus_points": 0,
        **purchase_evidence,
    }
    cur.execute(
        """INSERT INTO price_quotes
           (quote_id,quote_type,catalog_version,product_code,buyer_user_id,
            points_granted,bonus_points,base_price_cents,effective_multiplier_bps,
            final_price_cents,expires_at,status,used_order_id,pricing_snapshot_jsonb)
           VALUES ('Q-SVC-CUSTOMER','retail','retail-sv30-v1','WATER',40,1,0,70,11428,80,
                   NOW()+INTERVAL '15 minutes','consumed','O-SVC-CUSTOMER',%s::jsonb)""",
        (json.dumps({"order_pricing_snapshot": snapshot}),),
    )
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,agent_user_id,amount_cents,base_points,bonus_points,payment_method,
             actual_payment_channel,payment_status,order_type,pricing_snapshot_jsonb,
             pricing_catalog_version,price_quote_id)
           VALUES ('O-SVC-CUSTOMER',40,30,80,1,0,'wechat',%s,'pending','customer_recharge',
                    %s::jsonb,'retail-sv30-v1','Q-SVC-CUSTOMER')""",
        ("wechat_native" if purchase_evidence else None, json.dumps(snapshot)),
    )
    resale.reserve_consumer_sale(cur, order_id="O-SVC-CUSTOMER", snapshot=snapshot)
    cur.execute(
        f"""UPDATE recharge_orders SET payment_status='paid',payment_id='PAY-CUSTOMER',
                   paid_at={paid_at_sql} WHERE id='O-SVC-CUSTOMER'"""
    )
    cur.execute(
        "UPDATE user_wallets SET paid_points=paid_points+1,total_recharged=total_recharged+80 WHERE user_id=40"
    )
    result = resale.settle_consumer_sale(
        cur, order_id="O-SVC-CUSTOMER", consumer_user_id=40, pricing_snapshot=snapshot,
    )
    cur.execute(
        "UPDATE recharge_orders SET settlement_mode='dealer_consumer_resale' WHERE id='O-SVC-CUSTOMER'"
    )
    conn.commit()
    return result


def test_mineral_water_chain_conserves_inventory_and_direct_margin(db_conn):
    cur = _setup_water_chain(db_conn)
    # 按需铸造:平台跳的 1 点是当场铸出的,夹具预铸的那 1 点原封未动
    assert _wallet(cur, 1)["paid_inventory_points"] == 1
    assert _wallet(cur, 10)["paid_inventory_points"] == 0
    assert _wallet(cur, 20)["paid_inventory_points"] == 0
    assert _wallet(cur, 30)["paid_inventory_points"] == 1
    cur.execute("SELECT COALESCE(SUM(remaining_points+reserved_points),0) AS p FROM dealer_inventory_lots WHERE status IN ('active','refund_pending')")
    assert int(cur.fetchone()["p"]) == 2
    cur.execute(
        "SELECT (SELECT COALESCE(SUM(paid_inventory_points+bonus_inventory_points"
        "+frozen_inventory_points),0) FROM agent_inventory_wallets)"
        " - (SELECT COALESCE(SUM(points),0) FROM agent_inventory_transactions) AS diff"
    )
    assert int(cur.fetchone()["diff"]) == 0
    cur.execute("SELECT root_order_id AS order_id,margin_cents FROM dealer_resale_hop_profit_ledger ORDER BY root_order_id")
    margins = {row["order_id"]: int(row["margin_cents"]) for row in cur.fetchall()}
    assert margins == {"O-L1-L2": 10, "O-L2-SVC": 10, "O-PLATFORM-L1": 0}
    cur.execute("SELECT transfer_id,COUNT(*) AS c,SUM(points_delta) AS net FROM dealer_inventory_transfer_entries GROUP BY transfer_id")
    assert all(int(row["c"]) == 2 and int(row["net"]) == 0 for row in cur.fetchall())


def test_mineral_water_full_chain_050_060_070_080_uses_real_service_inventory(db_conn):
    result = _sell_water_to_consumer(db_conn)
    assert result["seller_margin_cents"] == 10
    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 0
    assert _wallet(cur, 30)["frozen_inventory_points"] == 0
    cur.execute(
        "SELECT tool_credit_points,publish_credit_points,bonus_credit_points "
        "FROM customer_agent_credit_wallets WHERE customer_user_id=40"
    )
    credit = cur.fetchone()
    assert (int(credit["tool_credit_points"]), int(credit["publish_credit_points"]), int(credit["bonus_credit_points"])) == (1, 0, 0)
    cur.execute("SELECT COUNT(*) AS c FROM dealer_inventory_lots WHERE owner_agent_user_id=40")
    assert int(cur.fetchone()["c"]) == 0, "普通客户不得获得可继续转售的经销商批次"
    cur.execute("SELECT seller_cost_basis_cents,sale_amount_cents,margin_cents FROM dealer_consumer_sales")
    sale = cur.fetchone()
    assert (int(sale["seller_cost_basis_cents"]), int(sale["sale_amount_cents"]), int(sale["margin_cents"])) == (70, 80, 10)
    cur.execute("SELECT agent_user_id,agent_settlement_cents,status FROM agent_revenue_ledger")
    revenue = cur.fetchone()
    assert (int(revenue["agent_user_id"]), int(revenue["agent_settlement_cents"]), revenue["status"]) == (30, 80, "frozen")
    cur.execute(
        "SELECT COALESCE(SUM(remaining_points+reserved_points),0) AS lots FROM dealer_inventory_lots "
        "WHERE status IN ('active','refund_pending')"
    )
    live_resale_lots = int(cur.fetchone()["lots"])
    assert live_resale_lots == 0
    cur.execute(
        "SELECT COALESCE(SUM(tool_credit_points+publish_credit_points+bonus_credit_points),0) AS credits "
        "FROM customer_agent_credit_wallets"
    )
    customer_entitlement = int(cur.fetchone()["credits"])
    cur.execute("SELECT COALESCE(SUM(points),0) AS issued FROM dealer_manufacturer_lot_issuances")
    manufacturer_origin = int(cur.fetchone()["issued"])
    assert live_resale_lots + customer_entitlement == manufacturer_origin == 1


def test_platform_seller_with_zero_balance_mints_on_demand(db_conn):
    """判别锁 #2(工单 §6):平台账号余额为 0、一个批次都没有时,下单仍然成功。

    前身是 test_platform_seller_without_real_origin_lot_cannot_quote
    (「平台没货就不能报价」)。按需铸造把这条业务规则整个反过来:
    平台缺货是人为制造的故障模式,业务上不该存在。
    """
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    cur.execute(
        "INSERT INTO system_settings(key,value,value_type) VALUES "
        "('inventory_minting_guard_config',"
        "'{\"mint_cost_numerator_cents\":50,\"mint_cost_denominator_points\":1}','json') "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,value_type='json'"
    )
    cur.execute("SELECT COUNT(*) AS c FROM dealer_inventory_lots")
    assert int(cur.fetchone()["c"]) == 0

    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    hops = terms["fulfillment_plan"]["hops"]
    assert [(h["seller_user_id"], h["mint_points"], h["existing_inventory_points"])
            for h in hops] == [(1, 1, 0)]
    cur.execute("SELECT COUNT(*) AS c FROM dealer_inventory_lots")
    assert int(cur.fetchone()["c"]) == 0, "报价阶段一行都不许写"

    _prepare_order(cur, order_id="O-ZERO-MINT", quote_id="Q-ZERO-MINT", terms=terms)
    assert _wallet(cur, 1)["paid_inventory_points"] == 0
    _pay_order(cur, order_id="O-ZERO-MINT", buyer_id=10, points=1)

    assert _wallet(cur, 10)["paid_inventory_points"] == 1
    assert _wallet(cur, 1)["paid_inventory_points"] == 0
    cur.execute(
        "SELECT points,pool,related_order_id FROM agent_inventory_transactions "
        "WHERE type='manufacturer_origin_in'"
    )
    minted = [(int(r["points"]), r["pool"], r["related_order_id"]) for r in cur.fetchall()]
    assert minted == [(1, "paid", "O-ZERO-MINT")]
    cur.execute(
        "SELECT (SELECT COALESCE(SUM(paid_inventory_points+bonus_inventory_points"
        "+frozen_inventory_points),0) FROM agent_inventory_wallets)"
        " - (SELECT COALESCE(SUM(points),0) FROM agent_inventory_transactions) AS diff"
    )
    assert int(cur.fetchone()["diff"]) == 0


@pytest.mark.parametrize(
    ("account_update", "wallet_update"),
    [
        ("UPDATE users SET is_active=0 WHERE id=10", None),
        (None, "UPDATE user_wallets SET agent_level=0 WHERE user_id=10"),
    ],
)
def test_direct_seller_is_revalidated_before_quote(
    db_conn, account_update: str | None, wallet_update: str | None,
):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="seller-runtime-quote")
    upstream_terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-RUNTIME-UPSTREAM", quote_id="Q-RUNTIME-UPSTREAM", terms=upstream_terms)
    _pay_order(cur, order_id="O-RUNTIME-UPSTREAM", buyer_id=10, points=1)
    cur.execute(
        "INSERT INTO channel_pricing_relationships "
        "(buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps) "
        "VALUES (20,10,'rel-runtime-v1',12000)"
    )
    if account_update:
        cur.execute(account_update)
    if wallet_update:
        cur.execute(wallet_update)

    with pytest.raises(resale.ResaleError) as denied:
        resale.build_quote_terms(
            cur, buyer_user_id=20, points=1, platform_reference_amount_cents=50,
            pricing_version="proc-test-v1",
        )
    assert denied.value.code == "SELLER_UNAVAILABLE"


def test_direct_seller_is_revalidated_again_at_order_reservation(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="seller-runtime-reserve")
    upstream_terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-RESERVE-UPSTREAM", quote_id="Q-RESERVE-UPSTREAM", terms=upstream_terms)
    _pay_order(cur, order_id="O-RESERVE-UPSTREAM", buyer_id=10, points=1)
    cur.execute(
        "INSERT INTO channel_pricing_relationships "
        "(buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps) "
        "VALUES (20,10,'rel-reserve-v1',12000)"
    )
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    cur.execute("UPDATE user_wallets SET agent_level=0 WHERE user_id=10")

    with pytest.raises(resale.ResaleError) as denied:
        _prepare_order(cur, order_id="O-RESERVE-DENIED", quote_id="Q-RESERVE-DENIED", terms=terms)
    assert denied.value.code == "SELLER_UNAVAILABLE"
    cur.execute("SELECT COUNT(*) AS c FROM dealer_resale_orders WHERE order_id='O-RESERVE-DENIED'")
    assert int(cur.fetchone()["c"]) == 0


def test_retail_quote_requires_digital_ack_and_consumption_atomically_reserves_service_lot(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO pricing_catalog_versions
           (catalog_type,scope_key,version_code,status,published_at,calc_meta_jsonb)
           VALUES ('retail','SV-TEST0030','retail-sv30-v1','published',NOW(),'{}')
           RETURNING id"""
    )
    version_id = int(cur.fetchone()["id"])
    source_ref = {
        "agent_user_id": 30, "sku_template_id": 1, "template_code": "WATER",
        "sku_type": "credit_pack", "wholesale_cents": 70, "retail_cents": 80,
        "points_granted": 1, "tool_points": 1, "publish_points": 0,
    }
    cur.execute(
        """INSERT INTO pricing_catalog_entries
           (version_id,product_code,base_price_cents,final_price_cents,paid_points,bonus_points,source_ref_jsonb)
           VALUES (%s,'WATER',80,80,1,0,%s::jsonb)""",
        (version_id, json.dumps(source_ref)),
    )
    db_conn.commit()
    from services import price_quote
    with pytest.raises(price_quote.QuoteError, match="确认|数字商品"):
        price_quote.issue_quote(
            quote_type="retail", scope_key="SV-TEST0030", product_code="WATER",
            buyer_user_id=40, retail_seller_user_id=30,
            consumer_policy_acknowledged=False,
        )
    quote = price_quote.issue_quote(
        quote_type="retail", scope_key="SV-TEST0030", product_code="WATER",
        buyer_user_id=40, retail_seller_user_id=30,
        consumer_policy_acknowledged=True,
    )
    assert int(quote["final_price_cents"]) == 80
    snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    assert snapshot["consumer_resale_mode"] is True
    assert snapshot["digital_goods_acknowledged"] is True
    cur = db_conn.cursor()
    price_quote.lock_and_validate(
        cur, quote["quote_id"], buyer_user_id=40, quote_type="retail",
        expected_final_cents=80, expected_points=1, expected_bonus_points=0,
    )
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,agent_user_id,amount_cents,base_points,bonus_points,payment_status,
            order_type,pricing_snapshot_jsonb,pricing_catalog_version,price_quote_id)
           VALUES ('O-ATOMIC-RETAIL',40,30,80,1,0,'pending','customer_recharge',
                   %s::jsonb,'retail-sv30-v1',%s)""",
        (json.dumps(snapshot), quote["quote_id"]),
    )
    assert price_quote.consume_quote(cur, quote["quote_id"], "O-ATOMIC-RETAIL") is True
    db_conn.commit()
    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 0
    assert _wallet(cur, 30)["frozen_inventory_points"] == 1
    cur.execute("SELECT state FROM dealer_consumer_sales WHERE order_id='O-ATOMIC-RETAIL'")
    assert cur.fetchone()["state"] == "reserved"


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_consumer_refund_is_not_b2b_72h_and_restores_only_direct_service(db_conn):
    _sell_water_to_consumer(db_conn, paid_at_sql="NOW()-INTERVAL '10 days'")
    cur = db_conn.cursor()
    case = resale.request_consumer_refund_case(
        cur, order_id="O-SVC-CUSTOMER", consumer_user_id=40, reason="unused digital credit",
    )
    assert case["status"] == "requested"
    reviewed = resale.review_consumer_refund_case(
        cur, case_id=case["case_id"], actor_user_id=30, approve=True, note="confirmed unused",
    )
    assert reviewed["status"] == "platform_execution"
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import revoke_credit 已删除。
    revoked = revoke_credit(
        cur, customer_user_id=40, tool_points=1, related_order_id="O-SVC-CUSTOMER",
        description="consumer refund",
    )
    assert revoked["actually_revoked"]["tool"] == 1
    cur.execute(
        "UPDATE agent_revenue_ledger SET status='cancelled',reversed_at=NOW() "
        "WHERE recharge_order_id='O-SVC-CUSTOMER'"
    )
    cur.execute(
        "UPDATE recharge_orders SET refund_status='processed',refund_completed_at=NULL "
        "WHERE id='O-SVC-CUSTOMER'"
    )
    assert resale.sync_consumer_refund_from_recharge(cur, "O-SVC-CUSTOMER") is True
    assert _wallet(cur, 30)["paid_inventory_points"] == 0
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=80,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || '{"refund_provider":"wechat","provider_refund_id":"WX-LEGACY-1",
                      "refund_evidence_kind":"signed_wechat_callback"}'::jsonb
           WHERE id='O-SVC-CUSTOMER'"""
    )
    assert resale.sync_consumer_refund_from_recharge(cur, "O-SVC-CUSTOMER") is True
    db_conn.commit()
    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 1
    assert _wallet(cur, 20)["paid_inventory_points"] == 0
    assert _wallet(cur, 10)["paid_inventory_points"] == 0
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["state"] == "paid"
    cur.execute("SELECT status FROM dealer_resale_hop_profit_ledger WHERE root_order_id='O-L2-SVC'")
    assert cur.fetchone()["status"] == "pending"
    cur.execute("SELECT state FROM dealer_consumer_sales WHERE order_id='O-SVC-CUSTOMER'")
    assert cur.fetchone()["state"] == "refunded"
    cur.execute("SELECT status FROM consumer_refund_cases WHERE source_order_id='O-SVC-CUSTOMER'")
    assert cur.fetchone()["status"] == "completed"
    cur.execute(
        """SELECT refund_scope,local_ref FROM external_refund_proof_registry
           WHERE provider='wechat' AND external_refund_id='WX-LEGACY-1'"""
    )
    proof = cur.fetchone()
    assert proof["refund_scope"] == "consumer" and proof["local_ref"] == case["case_id"]


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_statutory_seven_day_consumed_order_requires_manual_review_and_moves_no_funds(db_conn):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import consume_credit 已删除。

    consume_credit(
        cur,
        customer_user_id=40,
        feature_code="diagnosis_run",
        cost_points=1,
        related_order_id="USE-AFTER-O-SVC-CUSTOMER",
        description="prove seven-day order was consumed",
    )
    case = resale.request_consumer_refund_case(
        cur,
        order_id="O-SVC-CUSTOMER",
        consumer_user_id=40,
        reason="seven-day request after consuming the digital credit",
        reason_category="statutory_seven_day",
        evidence={"customer_request": "consumed-order"},
    )

    assert case["decision_kind"] == "mandatory"
    assert case["mandatory_evidence_status"] == "claimed"
    assert case["status"] == "manual_review"
    assert case["eligibility_jsonb"]["seven_day_eligible"] is False
    assert case["eligibility_jsonb"]["order_unspent"]["tool_unspent"] == 0
    assert int(_wallet(cur, 30)["paid_inventory_points"]) == 0
    cur.execute(
        "SELECT status,reversed_at FROM agent_revenue_ledger "
        "WHERE recharge_order_id='O-SVC-CUSTOMER'"
    )
    revenue = cur.fetchone()
    assert revenue["status"] == "frozen" and revenue["reversed_at"] is None
    cur.execute("SELECT COUNT(*) AS c FROM service_refund_cash_jobs")
    assert int(cur.fetchone()["c"]) == 0


def test_consumer_profit_maturity_waits_for_refund_case_terminal_rejection(db_conn):
    _sell_water_to_consumer(db_conn, paid_at_sql="NOW()-INTERVAL '10 days'")
    cur = db_conn.cursor()
    case = resale.request_consumer_refund_case(
        cur, order_id="O-SVC-CUSTOMER", consumer_user_id=40, reason="needs review",
    )
    db_conn.commit()

    from services.agent_revenue import settle_frozen_to_settled

    cur = db_conn.cursor()
    assert settle_frozen_to_settled(cur, batch_size=100) == 0
    result = resale.mature_profits(cur=cur)
    assert result["consumer_matured_count"] == 0
    cur.execute(
        "SELECT status FROM agent_revenue_ledger WHERE recharge_order_id='O-SVC-CUSTOMER'"
    )
    assert cur.fetchone()["status"] == "frozen"

    resale.review_consumer_refund_case(
        cur, case_id=case["case_id"], actor_user_id=30, approve=False,
        note="service rejected after review",
    )
    result = resale.mature_profits(cur=cur)
    assert result["consumer_matured_count"] == 1
    cur.execute(
        "SELECT status FROM agent_revenue_ledger WHERE recharge_order_id='O-SVC-CUSTOMER'"
    )
    assert cur.fetchone()["status"] == "settled"


def test_partial_frozen_margin_keeps_only_residual_then_matures_once(db_conn):
    _sell_water_to_consumer(db_conn, paid_at_sql="NOW()-INTERVAL '10 days'")
    cur = db_conn.cursor()
    cur.execute("SELECT * FROM dealer_consumer_sales WHERE order_id='O-SVC-CUSTOMER'")
    sale = dict(cur.fetchone())
    assert resale._reverse_direct_service_revenue(
        cur, sale=sale, refund_amount_cents=40,
    ) == 40
    cur.execute(
        """UPDATE agent_revenue_ledger
           SET settle_at=clock_timestamp()-INTERVAL '1 second'
           WHERE recharge_order_id='O-SVC-CUSTOMER'"""
    )
    cur.execute(
        """INSERT INTO consumer_refund_cases
             (case_id,consumer_user_id,responsible_service_user_id,source_order_id,
              policy_code,digital_goods_acknowledged_at,status,decision_kind,cash_status,
              refund_amount_cents,revenue_reversed_cents,mandatory_evidence_status)
           VALUES ('CRF-PARTIAL-MARGIN',40,30,'O-SVC-CUSTOMER',%s,NOW(),'completed',
                   'negotiated','completed',40,40,'not_applicable')""",
        (resale.DIGITAL_GOODS_POLICY_CODE,),
    )
    cur.execute(
        "UPDATE dealer_consumer_sales SET state='refunded',refunded_at=NOW() WHERE order_id='O-SVC-CUSTOMER'"
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=40
           WHERE id='O-SVC-CUSTOMER'"""
    )
    result = resale.mature_consumer_profits(cur=cur)
    assert result["matured_count"] == 1
    cur.execute(
        """SELECT status,agent_margin_before_tax_cents,agent_settlement_cents
           FROM agent_revenue_ledger WHERE recharge_order_id='O-SVC-CUSTOMER'"""
    )
    ledger = cur.fetchone()
    assert (ledger["status"], int(ledger["agent_margin_before_tax_cents"]),
            int(ledger["agent_settlement_cents"])) == ("settled", 5, 40)
    assert resale.mature_consumer_profits(cur=cur)["matured_count"] == 0


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_consumer_refund_callback_concurrency_restores_inventory_once(db_conn):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import revoke_credit 已删除。
    revoke_credit(
        cur, customer_user_id=40, tool_points=1, related_order_id="O-SVC-CUSTOMER",
        description="consumer refund concurrency",
    )
    cur.execute(
        "UPDATE agent_revenue_ledger SET status='cancelled',reversed_at=NOW() "
        "WHERE recharge_order_id='O-SVC-CUSTOMER'"
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=80,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || '{"refund_provider":"wechat","provider_refund_id":"WX-CONCURRENT-1",
                      "refund_evidence_kind":"signed_wechat_callback"}'::jsonb
           WHERE id='O-SVC-CUSTOMER'"""
    )
    db_conn.commit()

    def sync_once(_: int) -> bool:
        conn = connect()
        try:
            result = resale.sync_consumer_refund_from_recharge(conn.cursor(), "O-SVC-CUSTOMER")
            conn.commit()
            return result
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert all(pool.map(sync_once, range(20)))
    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 1
    cur.execute(
        "SELECT COUNT(*) AS c FROM dealer_consumer_transfer_entries "
        "WHERE order_id='O-SVC-CUSTOMER'"
    )
    assert int(cur.fetchone()["c"]) == 4
    cur.execute(
        "SELECT COUNT(*) AS c FROM agent_inventory_transactions "
        "WHERE related_order_id='O-SVC-CUSTOMER' AND type='consumer_refund_in'"
    )
    assert int(cur.fetchone()["c"]) == 1


def test_mandatory_consumer_refund_cannot_be_rejected_and_four_ledgers_conserve(db_conn):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    _seed_users(cur, 999)
    case = resale.request_consumer_refund_case(
        cur, order_id="O-SVC-CUSTOMER", consumer_user_id=40,
        reason="system did not deliver", reason_category="system_failure",
        evidence={"incident": "INC-1"},
    )
    assert case["decision_kind"] == "mandatory"
    assert case["status"] == "manual_review"
    assert case["mandatory_evidence_status"] == "claimed"
    with pytest.raises(resale.ResaleError) as denied:
        resale.review_consumer_refund_case(
            cur, case_id=case["case_id"], actor_user_id=30,
            approve=False, note="service refuses",
        )
    assert denied.value.code == "MANDATORY_REFUND_CANNOT_BE_REJECTED"
    with pytest.raises(resale.ResaleError) as provider_cannot_verify:
        resale.review_consumer_refund_case(
            cur, case_id=case["case_id"], actor_user_id=30,
            approve=True, note="service tries to self-verify",
        )
    assert provider_cannot_verify.value.code == "MANDATORY_REFUND_PLATFORM_REVIEW_REQUIRED"
    approved = resale.review_consumer_refund_case(
        cur, case_id=case["case_id"], actor_user_id=999,
        is_admin=True, approve=True, note="incident evidence verified",
    )
    assert approved["status"] == "platform_execution"
    assert approved["mandatory_evidence_status"] == "verified"

    settled = resale.settle_consumer_refund_case(cur, case_id=case["case_id"])
    assert settled["cash_status"] == "queued"
    assert int(settled["refund_amount_cents"]) == 80
    assert int(settled["revoked_paid_points"]) == 1
    assert int(settled["restored_inventory_points"]) == 1
    assert int(settled["revenue_reversed_cents"]) == 80
    assert _wallet(cur, 30)["paid_inventory_points"] == 1
    assert _wallet(cur, 20)["paid_inventory_points"] == 0
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["state"] == "paid"
    cur.execute("SELECT status FROM dealer_resale_hop_profit_ledger WHERE root_order_id='O-L2-SVC'")
    assert cur.fetchone()["status"] == "pending"
    cur.execute("SELECT negative_settlement_cents FROM service_refund_liability_ledger")
    assert int(cur.fetchone()["negative_settlement_cents"]) == 0
    cur.execute("SELECT COUNT(*) AS n FROM service_refund_funding_work_orders")
    assert int(cur.fetchone()["n"]) == 0
    db_conn.commit()

    # Repeated internal settlement and provider completion are idempotent.
    cur = db_conn.cursor()
    resale.settle_consumer_refund_case(cur, case_id=case["case_id"])
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=80,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || %s::jsonb
           WHERE id='O-SVC-CUSTOMER'""",
        (json.dumps({
            "refund_provider": "wechat",
            "provider_refund_id": "WX-MANDATORY-1",
            "refund_evidence_kind": "signed_wechat_callback",
        }),),
    )
    completed = resale.complete_consumer_refund_cash(
        cur, case_id=case["case_id"], amount_cents=80, provider="wechat",
        external_refund_id="WX-MANDATORY-1", evidence={"signed_callback": True},
    )
    assert completed["status"] == "completed"
    repeated = resale.complete_consumer_refund_cash(
        cur, case_id=case["case_id"], amount_cents=80, provider="wechat_pay",
        external_refund_id="WX-MANDATORY-1", evidence={"signed_callback": True},
    )
    assert repeated["cash_status"] == "completed"
    with pytest.raises(resale.ResaleError) as conflicting_cash_replay:
        resale.complete_consumer_refund_cash(
            cur, case_id=case["case_id"], amount_cents=80, provider="wechat",
            external_refund_id="WX-DIFFERENT", evidence={"signed_callback": True},
        )
    assert conflicting_cash_replay.value.code == "CONSUMER_REFUND_IDEMPOTENCY_CONFLICT"
    cur.execute("SELECT COUNT(*) AS c FROM consumer_refund_lot_restorations")
    assert int(cur.fetchone()["c"]) == 1


@pytest.mark.skip(reason="[单账本接线 2026-08-17 · R4] 本用例测的是 customer_agent_credit_wallets 三池语义 / 已删除的 customer_credit 函数。该行为于 2026-07-29 单账本收敛时被**有意移除**(客户算力只剩 user_wallets 一处),用例随之退役。替代守卫见 tests/v35_refund_single_ledger_2026_08_17/。")
def test_mandatory_full_cash_refund_does_not_restore_consumed_inventory(db_conn):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    _seed_users(cur, 999)
    # [单账本接线 2026-08-17 · R4] 原 from services.customer_credit import consume_credit 已删除。

    consume_credit(
        cur,
        customer_user_id=40,
        feature_code="diagnosis_run",
        cost_points=1,
        related_order_id="USE-CONSUMED-MANDATORY",
        description="consume the purchased credit before cash remedy",
    )
    case = resale.request_consumer_refund_case(
        cur,
        order_id="O-SVC-CUSTOMER",
        consumer_user_id=40,
        reason="service failed after the credit was consumed",
        reason_category="system_failure",
        evidence={"incident": "INC-CONSUMED"},
    )
    resale.review_consumer_refund_case(
        cur,
        case_id=case["case_id"],
        actor_user_id=999,
        is_admin=True,
        approve=True,
        note="cash remedy verified; consumed credits remain consumed",
    )
    before_wallet = int(_wallet(cur, 30)["paid_inventory_points"])
    settled = resale.settle_consumer_refund_case(cur, case_id=case["case_id"])
    assert int(settled["refund_amount_cents"]) == 80
    assert int(settled["revoked_paid_points"]) == 0
    assert int(settled["restored_inventory_points"]) == 0
    assert int(_wallet(cur, 30)["paid_inventory_points"]) == before_wallet
    cur.execute(
        "SELECT COUNT(*) AS c FROM consumer_refund_lot_restorations WHERE case_id=%s",
        (case["case_id"],),
    )
    assert int(cur.fetchone()["c"]) == 0
    cur.execute("SELECT COUNT(*) AS c FROM service_refund_liability_ledger")
    assert int(cur.fetchone()["c"]) == 1


def test_negotiated_rejection_moves_no_cash_credit_inventory_or_revenue(db_conn):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    before_service = int(_wallet(cur, 30)["paid_inventory_points"])
    cur.execute("SELECT tool_credit_points FROM customer_agent_credit_wallets WHERE customer_user_id=40")
    before_credit = int(cur.fetchone()["tool_credit_points"])
    case = resale.request_consumer_refund_case(
        cur, order_id="O-SVC-CUSTOMER", consumer_user_id=40,
        reason="changed mind", reason_category="change_of_mind",
    )
    rejected = resale.review_consumer_refund_case(
        cur, case_id=case["case_id"], actor_user_id=30,
        approve=False, note="negotiation not agreed",
    )
    assert rejected["status"] == "rejected"
    assert int(_wallet(cur, 30)["paid_inventory_points"]) == before_service
    cur.execute("SELECT tool_credit_points FROM customer_agent_credit_wallets WHERE customer_user_id=40")
    assert int(cur.fetchone()["tool_credit_points"]) == before_credit
    cur.execute("SELECT status,reversed_at FROM agent_revenue_ledger WHERE recharge_order_id='O-SVC-CUSTOMER'")
    revenue = cur.fetchone()
    assert revenue["status"] == "frozen" and revenue["reversed_at"] is None
    cur.execute("SELECT COUNT(*) AS c FROM service_refund_cash_jobs")
    assert int(cur.fetchone()["c"]) == 0


def test_concurrent_mandatory_refund_settlement_restores_once(db_conn):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    _seed_users(cur, 999)
    case = resale.request_consumer_refund_case(
        cur, order_id="O-SVC-CUSTOMER", consumer_user_id=40,
        reason="duplicate", reason_category="duplicate_charge",
    )
    resale.review_consumer_refund_case(
        cur, case_id=case["case_id"], actor_user_id=999,
        is_admin=True, approve=True, note="duplicate charge verified",
    )
    db_conn.commit()

    def settle_once(_: int) -> str:
        conn = connect()
        try:
            row = resale.settle_consumer_refund_case(conn.cursor(), case_id=case["case_id"])
            conn.commit()
            return str(row["cash_status"])
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert set(pool.map(settle_once, range(20))) == {"queued"}
    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 1
    cur.execute("SELECT COUNT(*) AS c FROM consumer_refund_lot_restorations")
    assert int(cur.fetchone()["c"]) == 1
    cur.execute("SELECT COUNT(*) AS c FROM service_refund_cash_jobs")
    assert int(cur.fetchone()["c"]) == 1
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=80,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || %s::jsonb
           WHERE id='O-SVC-CUSTOMER'""",
        (json.dumps({
            "refund_provider": "wechat",
            "provider_refund_id": "WX-CONCURRENT-REFUND",
            "refund_evidence_kind": "signed_wechat_callback",
        }),),
    )
    db_conn.commit()

    def complete_cash_once(_: int) -> str:
        conn = connect()
        try:
            row = resale.complete_consumer_refund_cash(
                conn.cursor(), case_id=case["case_id"], amount_cents=80,
                provider="wechat", external_refund_id="WX-CONCURRENT-REFUND",
                evidence={"reconciliation": "persisted callback"},
            )
            conn.commit()
            return str(row["cash_status"])
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert set(pool.map(complete_cash_once, range(20))) == {"completed"}
    cur = db_conn.cursor()
    cur.execute("SELECT status,attempt_count FROM service_refund_cash_jobs")
    cash_job = cur.fetchone()
    assert (cash_job["status"], int(cash_job["attempt_count"])) == ("completed", 1)


def test_downsold_lot_blocks_refund_then_each_hop_refunds_independently(db_conn):
    cur = _setup_water_chain(db_conn)
    with pytest.raises(resale.ResaleError, match="已消费|继续向下转售"):
        resale.request_refund(
            cur, order_id="O-L1-L2", requested_by_user_id=20, is_admin=False
        )
    db_conn.rollback()

    cur = db_conn.cursor()
    refund = resale.request_refund(
        cur, order_id="O-L2-SVC", requested_by_user_id=30, is_admin=False
    )
    assert refund["status"] == "requested"
    _persist_trusted_b2b_refund_terminal(
        cur, order_id="O-L2-SVC", amount_cents=70, provider_refund_id="WX-O-L2-SVC",
    )
    assert resale.sync_refund_from_recharge(cur, "O-L2-SVC") is True
    db_conn.commit()

    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 0
    assert _wallet(cur, 20)["paid_inventory_points"] == 1
    cur.execute("SELECT status FROM dealer_resale_hop_profit_ledger WHERE root_order_id='O-L2-SVC'")
    assert cur.fetchone()["status"] == "reversed"
    cur.execute("SELECT status FROM dealer_resale_hop_profit_ledger WHERE root_order_id='O-L1-L2'")
    assert cur.fetchone()["status"] == "pending"
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-PLATFORM-L1'")
    assert cur.fetchone()["state"] == "paid"

    resale.request_refund(cur, order_id="O-L1-L2", requested_by_user_id=20)
    _persist_trusted_b2b_refund_terminal(
        cur, order_id="O-L1-L2", amount_cents=60, provider_refund_id="WX-O-L1-L2",
    )
    resale.sync_refund_from_recharge(cur, "O-L1-L2")
    db_conn.commit()
    cur = db_conn.cursor()
    assert _wallet(cur, 20)["paid_inventory_points"] == 0
    assert _wallet(cur, 10)["paid_inventory_points"] == 1
    cur.execute("SELECT COUNT(*) AS c FROM dealer_inventory_transfer_entries WHERE transfer_id=(SELECT transfer_id FROM dealer_resale_orders WHERE order_id='O-L2-SVC')")
    assert int(cur.fetchone()["c"]) == 4

    # The first-level dealer must separately refund its own manufacturer order.
    # Only then does the manufacturer origin lot return; no prior refund recursed.
    resale.request_refund(cur, order_id="O-PLATFORM-L1", requested_by_user_id=10)
    _persist_trusted_b2b_refund_terminal(
        cur, order_id="O-PLATFORM-L1", amount_cents=50,
        provider_refund_id="WX-O-PLATFORM-L1",
    )
    resale.sync_refund_from_recharge(cur, "O-PLATFORM-L1")
    db_conn.commit()
    cur = db_conn.cursor()
    assert _wallet(cur, 10)["paid_inventory_points"] == 0
    # 退款把平台跳那 1 点还回平台;夹具预铸的 1 点始终未被动过 → 合计 2
    assert _wallet(cur, 1)["paid_inventory_points"] == 2
    cur.execute(
        "SELECT remaining_points,status FROM dealer_inventory_lots "
        "WHERE source_kind='manufacturer_origin' AND lot_id LIKE 'DML%'"
    )
    origin = cur.fetchone()
    assert (int(origin["remaining_points"]), origin["status"]) == (1, "active")


def test_b2b_voluntary_return_cost_requires_evidence_and_is_capped_at_five_percent(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    # Service's own purchase is still unused; its paid amount is 70 cents, so
    # the provable voluntary-return cost cap is floor(70 * 5%) = 3 cents.
    with pytest.raises(resale.ResaleError) as too_high:
        resale.request_refund(
            cur, order_id="O-L2-SVC", requested_by_user_id=30,
            processing_cost_cents=4, processing_cost_evidence={"gateway": 4},
        )
    assert too_high.value.code == "B2B_REFUND_COST_CAP_EXCEEDED"
    db_conn.rollback()
    cur = db_conn.cursor()
    with pytest.raises(resale.ResaleError) as no_evidence:
        resale.request_refund(
            cur, order_id="O-L2-SVC", requested_by_user_id=30,
            processing_cost_cents=3,
        )
    assert no_evidence.value.code == "B2B_REFUND_COST_EVIDENCE_REQUIRED"
    db_conn.rollback()
    refund = resale.request_refund(
        db_conn.cursor(), order_id="O-L2-SVC", requested_by_user_id=30,
        processing_cost_cents=3,
        processing_cost_evidence={"gateway_statement": "GW-COST-3"},
    )
    assert int(refund["processing_cost_cents"]) == 3


def test_b2b_platform_fault_has_no_fixed_window_or_processing_fee(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    _seed_users(cur, 999)
    cur.execute(
        "UPDATE dealer_resale_orders SET refund_deadline=NOW()-INTERVAL '1 day' WHERE order_id='O-L2-SVC'"
    )
    with pytest.raises(resale.ResaleError) as fee_denied:
        resale.request_refund(
            cur, order_id="O-L2-SVC", requested_by_user_id=30,
            reason_category="system_failure", processing_cost_cents=1,
            processing_cost_evidence={"cost": 1},
        )
    assert fee_denied.value.code == "REFUND_WINDOW_EXPIRED"
    db_conn.rollback()
    cur = db_conn.cursor()
    _seed_users(cur, 999)
    cur.execute(
        "UPDATE dealer_resale_orders SET refund_deadline=NOW()-INTERVAL '1 day' WHERE order_id='O-L2-SVC'"
    )
    with pytest.raises(resale.ResaleError) as unverified_claim:
        resale.request_refund(
            cur, order_id="O-L2-SVC", requested_by_user_id=30,
            reason_category="system_failure",
        )
    assert unverified_claim.value.code == "REFUND_WINDOW_EXPIRED"
    with pytest.raises(resale.ResaleError) as admin_without_evidence:
        resale.request_refund(
            cur, order_id="O-L2-SVC", requested_by_user_id=999,
            is_admin=True, reason_category="system_failure",
        )
    assert admin_without_evidence.value.code == "B2B_MANDATORY_EVIDENCE_REQUIRED"
    refund = resale.request_refund(
        cur, order_id="O-L2-SVC", requested_by_user_id=999,
        is_admin=True, reason_category="system_failure",
        mandatory_evidence={"incident_id": "INC-B2B-1"},
    )
    assert refund["reason_category"] == "system_failure"
    assert int(refund["processing_cost_cents"]) == 0
    db_conn.rollback()
    cur = db_conn.cursor()
    cur.execute(
        "UPDATE dealer_resale_orders SET refund_deadline=NOW()-INTERVAL '1 day' WHERE order_id='O-L2-SVC'"
    )
    with pytest.raises(resale.ResaleError) as mandatory_fee:
        resale.request_refund(
            cur, order_id="O-L2-SVC", requested_by_user_id=999,
            is_admin=True, reason_category="system_failure",
            mandatory_evidence={"incident_id": "INC-B2B-2"},
            processing_cost_cents=1, processing_cost_evidence={"cost": 1},
        )
    assert mandatory_fee.value.code == "B2B_REFUND_COST_FORBIDDEN"


def test_duplicate_callback_settlement_is_idempotent(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=5, acquisition_cost_cents=50, key="callback-origin")
    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=5, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-CALLBACK", quote_id="Q-CALLBACK", terms=terms)
    db_conn.commit()

    from db.wallet_db import complete_recharge

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(
            lambda i: complete_recharge("O-CALLBACK", f"PAY-{i}"),
            range(20),
        ))
    assert all(result is not None for result in results)
    cur = db_conn.cursor()
    assert _wallet(cur, 10)["paid_inventory_points"] == 5
    cur.execute("SELECT COUNT(*) AS c FROM dealer_inventory_lots WHERE source_order_id='O-CALLBACK'")
    assert int(cur.fetchone()["c"]) == 1
    cur.execute("SELECT COUNT(*) AS c FROM dealer_resale_hop_profit_ledger WHERE root_order_id='O-CALLBACK'")
    assert int(cur.fetchone()["c"]) == 1
    cur.execute(
        "SELECT COUNT(*) AS c FROM dealer_inventory_transfer_entries "
        "WHERE transfer_id=(SELECT transfer_id FROM dealer_resale_orders WHERE order_id='O-CALLBACK')"
    )
    assert int(cur.fetchone()["c"]) == 2


def test_b2b_refund_terminal_callback_concurrency_restores_once(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    resale.request_refund(cur, order_id="O-L2-SVC", requested_by_user_id=30)
    _persist_trusted_b2b_refund_terminal(
        cur, order_id="O-L2-SVC", amount_cents=70,
        provider_refund_id="WX-O-L2-SVC-CONCURRENT",
    )
    db_conn.commit()

    def sync_once(_: int) -> bool:
        conn = connect()
        try:
            value = resale.sync_refund_from_recharge(conn.cursor(), "O-L2-SVC")
            conn.commit()
            return value
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert all(pool.map(sync_once, range(20)))
    cur = db_conn.cursor()
    assert _wallet(cur, 20)["paid_inventory_points"] == 1
    assert _wallet(cur, 30)["paid_inventory_points"] == 0
    cur.execute(
        "SELECT COUNT(*) AS c FROM agent_inventory_transactions "
        "WHERE related_order_id='O-L2-SVC' AND type='resale_refund_in'"
    )
    assert int(cur.fetchone()["c"]) == 1
    cur.execute(
        "SELECT COUNT(*) AS c FROM dealer_inventory_transfer_entries "
        "WHERE transfer_id=(SELECT transfer_id FROM dealer_resale_orders WHERE order_id='O-L2-SVC')"
    )
    assert int(cur.fetchone()["c"]) == 4
    cur.execute(
        """SELECT refund_scope,local_ref,amount_cents
           FROM external_refund_proof_registry
           WHERE provider='wechat' AND external_refund_id='WX-O-L2-SVC-CONCURRENT'"""
    )
    proof = cur.fetchone()
    assert (proof["refund_scope"], proof["local_ref"], int(proof["amount_cents"])) == (
        "dealer_b2b", "O-L2-SVC", 70,
    )
    with pytest.raises(resale.ResaleError) as conflict:
        resale._claim_external_refund_proof(
            cur,
            provider="wechat_pay",
            external_refund_id="WX-O-L2-SVC-CONCURRENT",
            refund_scope="consumer",
            local_ref="CRF-CROSS-SCOPE-AFTER-CALLBACK",
            amount_cents=70,
            evidence={"signed_callback": True},
        )
    assert conflict.value.code == "REFUND_IDEMPOTENCY_CONFLICT"


def test_completed_b2b_refund_replay_rejects_untrusted_persisted_audit(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    resale.request_refund(cur, order_id="O-L2-SVC", requested_by_user_id=30)
    _persist_trusted_b2b_refund_terminal(
        cur,
        order_id="O-L2-SVC",
        amount_cents=70,
        provider_refund_id="WX-B2B-AUDIT-TAMPER",
    )
    assert resale.sync_refund_from_recharge(cur, "O-L2-SVC") is True
    cur.execute(
        "DELETE FROM external_refund_proof_registry "
        "WHERE provider='wechat' AND external_refund_id='WX-B2B-AUDIT-TAMPER'"
    )
    cur.execute(
        """UPDATE dealer_resale_refunds
           SET audit_jsonb=jsonb_build_object(
               'verified_cash_terminal',jsonb_build_object(
                   'provider','wechat',
                   'provider_refund_id','WX-B2B-AUDIT-TAMPER',
                   'refunded_amount_cents',70,
                   'refund_evidence_kind','operator_material'
               )
           )
           WHERE order_id='O-L2-SVC'"""
    )
    with pytest.raises(resale.ResaleError) as exc:
        resale.sync_refund_from_recharge(cur, "O-L2-SVC")
    assert exc.value.code == "EXTERNAL_REFUND_EVIDENCE_REQUIRED"
    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id='WX-B2B-AUDIT-TAMPER'"
    )
    assert int(cur.fetchone()["c"]) == 0


def test_xunhupay_pending_review_is_a_proven_b2b_cash_terminal(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    resale.request_refund(cur, order_id="O-L2-SVC", requested_by_user_id=30)
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='pending_review',refund_completed_at=NOW(),
               refunded_amount_cents=70,payment_method='xunhupay',
               actual_payment_channel='xunhupay',
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || '{"refund_provider":"xunhupay",
                      "provider_refund_id":"XHP-B2B-PENDING-REVIEW",
                      "refund_evidence_kind":"signed_xunhupay_callback",
                      "provider_refund_reference_kind":"signed_xunhupay_refund_response"}'::jsonb
           WHERE id='O-L2-SVC'"""
    )
    assert resale.sync_refund_from_recharge(cur, "O-L2-SVC") is True
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["state"] == "refunded"
    cur.execute(
        "SELECT status FROM dealer_resale_hop_profit_ledger "
        "WHERE root_order_id='O-L2-SVC' ORDER BY hop_seq DESC LIMIT 1"
    )
    assert cur.fetchone()["status"] == "reversed"
    cur.execute(
        """SELECT refund_scope,local_ref,amount_cents
           FROM external_refund_proof_registry
           WHERE provider='xunhupay'
             AND external_refund_id='XHP-B2B-PENDING-REVIEW'"""
    )
    proof = cur.fetchone()
    assert (proof["refund_scope"], proof["local_ref"], int(proof["amount_cents"])) == (
        "dealer_b2b", "O-L2-SVC", 70,
    )


@pytest.mark.parametrize("provider", ["xhp", "xunhupay-manual", "unknown-gateway"])
def test_unknown_provider_alias_cannot_open_a_second_proof_namespace(db_conn, provider):
    cur = db_conn.cursor()
    with pytest.raises(resale.ResaleError) as exc:
        resale._claim_external_refund_proof(
            cur,
            provider=provider,
            external_refund_id="SAME-PROVIDER-REF",
            refund_scope="dealer_b2b",
            local_ref="O-UNSUPPORTED-PROVIDER",
            amount_cents=70,
            evidence={"operator_proof": True},
        )
    assert exc.value.code == "EXTERNAL_REFUND_PROVIDER_UNSUPPORTED"
    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id='SAME-PROVIDER-REF'"
    )
    assert int(cur.fetchone()["c"]) == 0


def test_external_refund_proof_cannot_be_replayed_across_b2b_orders(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    resale._claim_external_refund_proof(
        cur,
        provider="wechat",
        external_refund_id="GLOBAL-PROOF-1",
        refund_scope="dealer_b2b",
        local_ref="O-L1-L2",
        amount_cents=80,
        evidence={"refund_evidence_kind": "wechat_refund_query"},
    )
    resale.request_refund(cur, order_id="O-L2-SVC", requested_by_user_id=30)
    before = _wallet(cur, 20)["paid_inventory_points"]
    with pytest.raises(resale.ResaleError) as exc:
        resale._claim_external_refund_proof(
            cur,
            provider="WeChat",
            external_refund_id="GLOBAL-PROOF-1",
            refund_scope="dealer_b2b",
            local_ref="O-L2-SVC",
            amount_cents=70,
            evidence={"refund_evidence_kind": "wechat_refund_query"},
        )
    assert exc.value.code == "REFUND_IDEMPOTENCY_CONFLICT"
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["state"] == "refund_pending"
    assert _wallet(cur, 20)["paid_inventory_points"] == before


@pytest.mark.parametrize(
    ("registry_provider", "b2b_provider", "external_id"),
    [
        ("wechat", "wechat_pay", "CROSS-SCOPE-WECHAT-1"),
        ("xunhupay", "xunhupay_manual", "CROSS-SCOPE-XUNHUPAY-1"),
    ],
)
def test_consumer_refund_proof_cannot_be_replayed_into_b2b_refund(
    db_conn, registry_provider, b2b_provider, external_id,
):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO external_refund_proof_registry(
               provider,external_refund_id,refund_scope,local_ref,amount_cents,evidence_jsonb)
           VALUES (%s,%s,'consumer','CRF-ALREADY-CLOSED',80,
                   '{"signed_callback":true}'::jsonb)""",
        (registry_provider, external_id),
    )
    resale.request_refund(cur, order_id="O-L2-SVC", requested_by_user_id=30)
    before_wallet = int(_wallet(cur, 20)["paid_inventory_points"])
    with pytest.raises(resale.ResaleError) as exc:
        resale._claim_external_refund_proof(
            cur,
            provider=b2b_provider,
            external_refund_id=external_id,
            refund_scope="dealer_b2b",
            local_ref="O-L2-SVC",
            amount_cents=70,
            evidence={"refund_evidence_kind": "provider_verified_terminal"},
        )
    assert exc.value.code == "REFUND_IDEMPOTENCY_CONFLICT"
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["state"] == "refund_pending"
    assert int(_wallet(cur, 20)["paid_inventory_points"]) == before_wallet
    cur.execute(
        """SELECT refund_scope,local_ref,amount_cents
           FROM external_refund_proof_registry
           WHERE provider=%s AND external_refund_id=%s""",
        (registry_provider, external_id),
    )
    owner = cur.fetchone()
    assert (owner["refund_scope"], owner["local_ref"], int(owner["amount_cents"])) == (
        "consumer", "CRF-ALREADY-CLOSED", 80,
    )


def test_direct_migration_backfills_callback_b2b_and_legacy_consumer_proofs(db_conn):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO dealer_resale_refunds(
               refund_id,order_id,requested_by_user_id,responsible_seller_user_id,
               status,reason,deadline_at,completed_at)
           VALUES ('DRF-HIST-CALLBACK','O-L2-SVC',30,20,'completed','historic callback',
                   NOW(),NOW())"""
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=70,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || '{"refund_provider":"wechat","provider_refund_id":"WX-HIST-B2B",
                      "refund_evidence_kind":"signed_wechat_callback"}'::jsonb
           WHERE id='O-L2-SVC'"""
    )
    cur.execute(
        "UPDATE dealer_resale_orders SET state='refunded',refunded_at=NOW() "
        "WHERE order_id='O-L2-SVC'"
    )
    cur.execute(
        "UPDATE dealer_resale_hop_profit_ledger SET status='reversed',reversed_at=NOW() "
        "WHERE root_order_id='O-L2-SVC' AND hop_seq=("
        "SELECT MAX(hop_seq) FROM dealer_resale_hop_profit_ledger "
        "WHERE root_order_id='O-L2-SVC')"
    )
    cur.execute(
        """INSERT INTO consumer_refund_cases(
               case_id,consumer_user_id,responsible_service_user_id,source_order_id,
               policy_code,digital_goods_acknowledged_at,status,decision_kind,cash_status,
               refund_amount_cents,mandatory_evidence_status)
           VALUES ('CRF-HIST-LEGACY',40,30,'O-SVC-CUSTOMER',%s,NOW(),'completed',
                   'negotiated','completed',0,'not_applicable')""",
        (resale.DIGITAL_GOODS_POLICY_CODE,),
    )
    cur.execute(
        "UPDATE dealer_consumer_sales SET state='refunded',refunded_at=NOW() "
        "WHERE order_id='O-SVC-CUSTOMER'"
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=80,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || '{"refund_provider":"xunhupay","provider_refund_id":"XHP-HIST-CONSUMER",
                      "refund_evidence_kind":"signed_xunhupay_callback",
                      "provider_refund_reference_kind":"signed_xunhupay_refund_response"}'::jsonb,
               payment_method='xunhupay'
           WHERE id='O-SVC-CUSTOMER'"""
    )
    cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    cur.execute(
        """SELECT provider,external_refund_id,refund_scope,local_ref,amount_cents
           FROM external_refund_proof_registry
           WHERE external_refund_id IN ('WX-HIST-B2B','XHP-HIST-CONSUMER')
           ORDER BY external_refund_id"""
    )
    rows = cur.fetchall()
    assert [
        (row["provider"], row["external_refund_id"], row["refund_scope"],
         row["local_ref"], int(row["amount_cents"]))
        for row in rows
    ] == [
        ("wechat", "WX-HIST-B2B", "dealer_b2b", "O-L2-SVC", 70),
        ("xunhupay", "XHP-HIST-CONSUMER", "consumer", "CRF-HIST-LEGACY", 80),
    ]


@pytest.mark.parametrize(
    ("payment_method", "provider", "evidence_kind", "refunded_amount", "internal_terminal"),
    [
        ("wechat", "xunhupay", "signed_xunhupay_callback", 70, True),
        ("xunhupay", "xunhupay", "signed_wechat_callback", 70, True),
        ("wechat", "wechat", "signed_wechat_callback", 69, True),
        ("wechat", "wechat", "signed_wechat_callback", 70, False),
    ],
)
def test_direct_migration_rejects_callback_proof_weaker_than_runtime(
    db_conn, payment_method, provider, evidence_kind, refunded_amount, internal_terminal,
):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO dealer_resale_refunds(
               refund_id,order_id,requested_by_user_id,responsible_seller_user_id,
               status,reason,deadline_at,completed_at)
           VALUES ('DRF-BAD-CALLBACK','O-L2-SVC',30,20,'completed','invalid historic proof',
                   NOW(),NOW())"""
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=%s,
               payment_method=%s,actual_payment_channel=NULL,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || jsonb_build_object(
                        'refund_provider',%s,'provider_refund_id','BAD-HIST-B2B',
                        'refund_evidence_kind',%s)
           WHERE id='O-L2-SVC'""",
        (refunded_amount, payment_method, provider, evidence_kind),
    )
    if internal_terminal:
        cur.execute(
            "UPDATE dealer_resale_orders SET state='refunded',refunded_at=NOW() "
            "WHERE order_id='O-L2-SVC'"
        )
        cur.execute(
            "UPDATE dealer_resale_hop_profit_ledger SET status='reversed',reversed_at=NOW() "
            "WHERE root_order_id='O-L2-SVC' AND hop_seq=("
            "SELECT MAX(hop_seq) FROM dealer_resale_hop_profit_ledger "
            "WHERE root_order_id='O-L2-SVC')"
        )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.RaiseException, match="runtime-equivalent proof"):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()
    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id='BAD-HIST-B2B'"
    )
    assert int(cur.fetchone()["c"]) == 0


def test_direct_migration_rejects_null_manual_refund_status(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    proof = {
        "provider": "wechat",
        "external_refund_id": "WX-NULL-STATUS",
        "amount_cents": 70,
        "external_completed_at": "2026-07-15T12:00:00Z",
        "evidence": {"signed_callback": True},
    }
    cur.execute(
        """INSERT INTO dealer_resale_refunds(
               refund_id,order_id,requested_by_user_id,responsible_seller_user_id,
               status,reason,deadline_at,external_channel,external_refund_id,
               completed_at,audit_jsonb)
           VALUES ('DRF-NULL-STATUS','O-L2-SVC',30,20,'completed','invalid null terminal',
                   NOW(),'wechat','WX-NULL-STATUS',NOW(),%s::jsonb)""",
        (json.dumps({"external_cash_refund": proof}),),
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status=NULL,refund_completed_at=NOW(),refunded_amount_cents=70
           WHERE id='O-L2-SVC'"""
    )
    cur.execute(
        "UPDATE dealer_resale_orders SET state='refunded',refunded_at=NOW() "
        "WHERE order_id='O-L2-SVC'"
    )
    cur.execute(
        "UPDATE dealer_resale_hop_profit_ledger SET status='reversed',reversed_at=NOW() "
        "WHERE root_order_id='O-L2-SVC' AND hop_seq=("
        "SELECT MAX(hop_seq) FROM dealer_resale_hop_profit_ledger "
        "WHERE root_order_id='O-L2-SVC')"
    )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.RaiseException, match="trusted immutable external proof"):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()
    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id='WX-NULL-STATUS'"
    )
    assert int(cur.fetchone()["c"]) == 0


@pytest.mark.parametrize(
    ("payment_method", "provider", "evidence_kind", "refunded_amount"),
    [
        ("wechat", "wechat", "signed_xunhupay_callback", 80),
        ("xunhupay", "wechat", "signed_wechat_callback", 80),
        ("wechat", "wechat", "signed_wechat_callback", 79),
        ("wechat", "wechat", None, 80),
    ],
)
def test_direct_migration_rejects_consumer_proof_weaker_than_runtime(
    db_conn, payment_method, provider, evidence_kind, refunded_amount,
):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO consumer_refund_cases(
               case_id,consumer_user_id,responsible_service_user_id,source_order_id,
               policy_code,digital_goods_acknowledged_at,status,decision_kind,cash_status,
               refund_amount_cents,mandatory_evidence_status)
           VALUES ('CRF-BAD-CONSUMER',40,30,'O-SVC-CUSTOMER',%s,NOW(),'completed',
                   'negotiated','completed',0,'not_applicable')""",
        (resale.DIGITAL_GOODS_POLICY_CODE,),
    )
    cur.execute(
        "UPDATE dealer_consumer_sales SET state='refunded',refunded_at=NOW() "
        "WHERE order_id='O-SVC-CUSTOMER'"
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=%s,
               payment_method=%s,actual_payment_channel=NULL,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || jsonb_build_object(
                        'refund_provider',%s,'provider_refund_id','BAD-HIST-CONSUMER',
                        'refund_evidence_kind',%s)
           WHERE id='O-SVC-CUSTOMER'""",
        (refunded_amount, payment_method, provider, evidence_kind),
    )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.RaiseException, match="completed consumer refund"):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()
    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id='BAD-HIST-CONSUMER'"
    )
    assert int(cur.fetchone()["c"]) == 0


@pytest.mark.parametrize(
    (
        "job_route", "job_provider", "job_ref", "job_amount", "job_kind",
        "job_consumer_user_id", "job_service_user_id",
    ),
    [
        ("wechat", "xunhupay", "WX-CASH-JOB", 80, "signed_xunhupay_callback", 40, 30),
        ("wechat", "wechat", "WX-CASH-JOB-DRIFT", 80, "signed_wechat_callback", 40, 30),
        ("xunhupay", "wechat", "WX-CASH-JOB", 80, "signed_wechat_callback", 40, 30),
        ("wechat", "wechat", "WX-CASH-JOB", 79, "signed_wechat_callback", 40, 30),
        ("wechat", "wechat", "WX-CASH-JOB", 80, "signed_xunhupay_callback", 40, 30),
        ("wechat", "wechat", "WX-CASH-JOB", 80, "signed_wechat_callback", 30, 30),
        ("wechat", "wechat", "WX-CASH-JOB", 80, "signed_wechat_callback", 40, 20),
    ],
)
def test_direct_migration_rejects_completed_cash_job_projection_drift(
    db_conn, job_route, job_provider, job_ref, job_amount, job_kind,
    job_consumer_user_id, job_service_user_id,
):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO consumer_refund_cases(
               case_id,consumer_user_id,responsible_service_user_id,source_order_id,
               policy_code,digital_goods_acknowledged_at,status,decision_kind,cash_status,
               refund_amount_cents,mandatory_evidence_status,internal_settled_at)
           VALUES ('CRF-BAD-CASH-JOB',40,30,'O-SVC-CUSTOMER',%s,NOW(),'completed',
                   'negotiated','completed',80,'not_applicable',NOW())""",
        (resale.DIGITAL_GOODS_POLICY_CODE,),
    )
    cur.execute(
        "UPDATE dealer_consumer_sales SET state='refunded',refunded_at=NOW() "
        "WHERE order_id='O-SVC-CUSTOMER'"
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=80,
               payment_method='wechat',actual_payment_channel=NULL,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || '{"refund_provider":"wechat","provider_refund_id":"WX-CASH-JOB",
                      "refund_evidence_kind":"signed_wechat_callback"}'::jsonb
           WHERE id='O-SVC-CUSTOMER'"""
    )
    evidence = {
        "provider": job_provider,
        "provider_refund_id": job_ref,
        "refund_evidence_kind": job_kind,
        "refunded_amount_cents": job_amount,
        "original_payment_route": job_route,
        "source": "recharge_orders.settlement_snapshot_jsonb",
    }
    cur.execute(
        """INSERT INTO service_refund_cash_jobs(
               cash_job_id,case_id,source_order_id,consumer_user_id,
               responsible_service_user_id,amount_cents,original_payment_route,
               status,attempt_count,idempotency_key,provider_refund_id,
               provider_evidence_jsonb,completed_at)
           VALUES ('SCJ-BAD-PROJECTION','CRF-BAD-CASH-JOB','O-SVC-CUSTOMER',%s,%s,
                   %s,%s,'completed',1,'bad-cash-job-projection',%s,%s::jsonb,NOW())""",
        (
            job_consumer_user_id, job_service_user_id, job_amount, job_route, job_ref,
            json.dumps(evidence),
        ),
    )
    db_conn.commit()
    with pytest.raises(psycopg2.errors.RaiseException, match="completed consumer cash job"):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()
    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id IN ('WX-CASH-JOB','WX-CASH-JOB-DRIFT')"
    )
    assert int(cur.fetchone()["c"]) == 0


def _seed_valid_completed_cash_job(db_conn) -> None:
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO consumer_refund_cases(
               case_id,consumer_user_id,responsible_service_user_id,source_order_id,
               policy_code,digital_goods_acknowledged_at,status,decision_kind,cash_status,
               refund_amount_cents,mandatory_evidence_status,internal_settled_at)
           VALUES ('CRF-VALID-CASH-JOB',40,30,'O-SVC-CUSTOMER',%s,NOW(),'completed',
                   'negotiated','completed',80,'not_applicable',NOW())""",
        (resale.DIGITAL_GOODS_POLICY_CODE,),
    )
    cur.execute(
        "UPDATE dealer_consumer_sales SET state='refunded',refunded_at=NOW() "
        "WHERE order_id='O-SVC-CUSTOMER'"
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=80,
               payment_method='wechat',actual_payment_channel=NULL,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || '{"refund_provider":"wechat","provider_refund_id":"WX-CASH-JOB",
                      "refund_evidence_kind":"signed_wechat_callback"}'::jsonb
           WHERE id='O-SVC-CUSTOMER'"""
    )
    evidence = {
        "provider": "wechat",
        "provider_refund_id": "WX-CASH-JOB",
        "refund_evidence_kind": "signed_wechat_callback",
        "refunded_amount_cents": 80,
        "original_payment_route": "wechat",
        "source": "recharge_orders.settlement_snapshot_jsonb",
    }
    cur.execute(
        """INSERT INTO service_refund_cash_jobs(
               cash_job_id,case_id,source_order_id,consumer_user_id,
               responsible_service_user_id,amount_cents,original_payment_route,
               status,attempt_count,idempotency_key,provider_refund_id,
               provider_evidence_jsonb,completed_at)
           VALUES ('SCJ-VALID-PROJECTION','CRF-VALID-CASH-JOB','O-SVC-CUSTOMER',40,30,
                   80,'wechat','completed',1,'valid-cash-job-projection','WX-CASH-JOB',
                   %s::jsonb,NOW())""",
        (json.dumps(evidence),),
    )
    db_conn.commit()


def test_direct_migration_rechecks_cash_job_after_preflight_and_rolls_back_registry(db_conn):
    _seed_valid_completed_cash_job(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """CREATE FUNCTION test_drift_cash_job_after_registry_insert()
           RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
               UPDATE service_refund_cash_jobs
                  SET provider_evidence_jsonb=provider_evidence_jsonb
                      || '{"refund_evidence_kind":"signed_xunhupay_callback"}'::jsonb
                WHERE cash_job_id='SCJ-VALID-PROJECTION';
               RETURN NEW;
           END $$"""
    )
    cur.execute(
        """CREATE TRIGGER test_drift_cash_job_after_registry_insert
           AFTER INSERT ON external_refund_proof_registry
           FOR EACH ROW WHEN (NEW.external_refund_id='WX-CASH-JOB')
           EXECUTE FUNCTION test_drift_cash_job_after_registry_insert()"""
    )
    db_conn.commit()

    try:
        with pytest.raises(psycopg2.errors.RaiseException, match="changed during proof migration"):
            cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
        db_conn.rollback()
        cur.execute(
            "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
            "WHERE external_refund_id='WX-CASH-JOB'"
        )
        assert int(cur.fetchone()["c"]) == 0
        cur.execute(
            "SELECT provider_evidence_jsonb->>'refund_evidence_kind' AS kind "
            "FROM service_refund_cash_jobs WHERE cash_job_id='SCJ-VALID-PROJECTION'"
        )
        assert cur.fetchone()["kind"] == "signed_wechat_callback"
    finally:
        db_conn.rollback()
        cur.execute(
            "DROP TRIGGER IF EXISTS test_drift_cash_job_after_registry_insert "
            "ON external_refund_proof_registry"
        )
        cur.execute("DROP FUNCTION IF EXISTS test_drift_cash_job_after_registry_insert()")
        db_conn.commit()


def test_direct_migration_fences_external_cash_job_writer_until_commit(db_conn):
    _seed_valid_completed_cash_job(db_conn)
    cur = db_conn.cursor()
    lock_key = 92071542
    cur.execute(
        f"""CREATE FUNCTION test_hold_refund_registry_insert()
           RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN
               PERFORM pg_advisory_lock({lock_key});
               PERFORM pg_sleep(3);
               PERFORM pg_advisory_unlock({lock_key});
               RETURN NEW;
           END $$"""
    )
    cur.execute(
        """CREATE TRIGGER test_hold_refund_registry_insert
           AFTER INSERT ON external_refund_proof_registry
           FOR EACH ROW WHEN (NEW.external_refund_id='WX-CASH-JOB')
           EXECUTE FUNCTION test_hold_refund_registry_insert()"""
    )
    db_conn.commit()

    def run_migration() -> None:
        conn = connect()
        conn.autocommit = True
        try:
            with conn.cursor() as migration_cur:
                migration_cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
        finally:
            conn.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_migration)
            probe = connect()
            probe.autocommit = True
            try:
                deadline = time.monotonic() + 10
                migration_in_registry_insert = False
                while time.monotonic() < deadline:
                    with probe.cursor() as probe_cur:
                        probe_cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,))
                        acquired = bool(probe_cur.fetchone()["acquired"])
                        if acquired:
                            probe_cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                        else:
                            migration_in_registry_insert = True
                            break
                    time.sleep(0.05)
                assert migration_in_registry_insert, "migration never reached the fenced registry phase"

                writer = connect()
                try:
                    with writer.cursor() as writer_cur:
                        writer_cur.execute("SET LOCAL statement_timeout='300ms'")
                        with pytest.raises(psycopg2.errors.QueryCanceled):
                            writer_cur.execute(
                                "UPDATE service_refund_cash_jobs SET last_error='concurrent drift' "
                                "WHERE cash_job_id='SCJ-VALID-PROJECTION'"
                            )
                    writer.rollback()
                finally:
                    writer.close()
            finally:
                probe.close()
            future.result(timeout=15)
    finally:
        cleanup = connect()
        cleanup.autocommit = True
        try:
            with cleanup.cursor() as cleanup_cur:
                cleanup_cur.execute(
                    "DROP TRIGGER IF EXISTS test_hold_refund_registry_insert "
                    "ON external_refund_proof_registry"
                )
                cleanup_cur.execute("DROP FUNCTION IF EXISTS test_hold_refund_registry_insert()")
        finally:
            cleanup.close()

    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id='WX-CASH-JOB'"
    )
    assert int(cur.fetchone()["c"]) == 1


def test_direct_migration_rejects_cross_order_cash_job_ownership(db_conn):
    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO price_quotes(
               quote_id,quote_type,catalog_version,product_code,buyer_user_id,
               points_granted,bonus_points,base_price_cents,effective_multiplier_bps,
               final_price_cents,expires_at,status,used_order_id,pricing_snapshot_jsonb)
           VALUES ('Q-SVC-CUSTOMER-B','retail','retail-sv30-v1','WATER',40,1,0,70,11428,80,
                   NOW()+INTERVAL '15 minutes','consumed','O-SVC-CUSTOMER-B','{}'::jsonb)"""
    )
    cur.execute(
        """INSERT INTO recharge_orders(
               id,user_id,agent_user_id,amount_cents,base_points,bonus_points,payment_method,
               actual_payment_channel,payment_status,payment_id,paid_at,order_type,
               pricing_snapshot_jsonb,pricing_catalog_version,price_quote_id,refund_status,
               refund_completed_at,refunded_amount_cents,settlement_snapshot_jsonb)
           VALUES ('O-SVC-CUSTOMER-B',40,30,80,1,0,'wechat',NULL,'paid','PAY-CUSTOMER-B',
                   NOW(),'customer_recharge','{}'::jsonb,'retail-sv30-v1','Q-SVC-CUSTOMER-B',
                   'completed',NOW(),80,
                   '{"refund_provider":"wechat","provider_refund_id":"WX-CASH-JOB-B",
                     "refund_evidence_kind":"signed_wechat_callback"}'::jsonb)"""
    )
    cur.execute(
        """INSERT INTO agent_revenue_ledger(
               agent_user_id,source,recharge_order_id,customer_user_id,customer_paid_cents,
               factory_cents,agent_margin_before_tax_cents,agent_settlement_cents,
               status,settle_at,reversed_at)
           VALUES (30,'recharge','O-SVC-CUSTOMER-B',40,80,70,10,10,'cancelled',NOW(),NOW())
           RETURNING id"""
    )
    revenue_id = int(cur.fetchone()["id"])
    cur.execute(
        """INSERT INTO dealer_consumer_sales(
               order_id,seller_user_id,consumer_user_id,points,seller_lot_allocations,
               seller_cost_basis_cents,sale_amount_cents,margin_cents,downstream_markup_bps,
               pricing_version,quote_id,refund_responsible_user_id,digital_goods_policy_code,
               digital_goods_acknowledged_at,state,transfer_id,revenue_ledger_id,
               paid_at,refunded_at)
           VALUES ('O-SVC-CUSTOMER-B',30,40,1,'[{"lot_id":"TEST-B","points":1}]'::jsonb,
                   70,80,10,11428,'retail-sv30-v1','Q-SVC-CUSTOMER-B',30,%s,NOW(),
                   'refunded','TR-SVC-CUSTOMER-B',%s,NOW(),NOW())""",
        (resale.DIGITAL_GOODS_POLICY_CODE, revenue_id),
    )
    cur.execute(
        """INSERT INTO consumer_refund_cases(
               case_id,consumer_user_id,responsible_service_user_id,source_order_id,
               policy_code,digital_goods_acknowledged_at,status,decision_kind,cash_status,
               refund_amount_cents,mandatory_evidence_status,internal_settled_at)
           VALUES ('CRF-CROSS-ORDER',40,30,'O-SVC-CUSTOMER',%s,NOW(),'completed',
                   'negotiated','completed',80,'not_applicable',NOW())""",
        (resale.DIGITAL_GOODS_POLICY_CODE,),
    )
    evidence = {
        "provider": "wechat",
        "provider_refund_id": "WX-CASH-JOB-B",
        "refund_evidence_kind": "signed_wechat_callback",
        "refunded_amount_cents": 80,
        "original_payment_route": "wechat",
        "source": "recharge_orders.settlement_snapshot_jsonb",
    }
    cur.execute(
        """INSERT INTO service_refund_cash_jobs(
               cash_job_id,case_id,source_order_id,consumer_user_id,
               responsible_service_user_id,amount_cents,original_payment_route,status,
               attempt_count,idempotency_key,provider_refund_id,provider_evidence_jsonb,completed_at)
           VALUES ('SCJ-CROSS-ORDER','CRF-CROSS-ORDER','O-SVC-CUSTOMER-B',40,30,80,
                   'wechat','completed',1,'cross-order-cash-job','WX-CASH-JOB-B',%s::jsonb,NOW())""",
        (json.dumps(evidence),),
    )
    db_conn.commit()

    with pytest.raises(psycopg2.errors.RaiseException, match="completed consumer cash job"):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()
    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id='WX-CASH-JOB-B'"
    )
    assert int(cur.fetchone()["c"]) == 0


def test_b2b_completed_status_without_provider_proof_moves_nothing(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    resale.request_refund(cur, order_id="O-L2-SVC", requested_by_user_id=30)
    cur.execute(
        "UPDATE recharge_orders SET refund_status='completed',refund_completed_at=NOW(),"
        "refunded_amount_cents=70 WHERE id='O-L2-SVC'"
    )
    with pytest.raises(resale.ResaleError) as missing_proof:
        resale.sync_refund_from_recharge(cur, "O-L2-SVC")
    assert missing_proof.value.code == "B2B_REFUND_PROVIDER_PROOF_MISSING"
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["state"] == "refund_pending"
    assert _wallet(cur, 20)["paid_inventory_points"] == 0
    assert _wallet(cur, 30)["frozen_inventory_points"] == 1


def test_two_buyers_racing_one_lot_only_one_reserves(db_conn):
    cur = db_conn.cursor()
    buyer_ids = list(range(20, 40))
    _seed_users(cur, 1, 10, *buyer_ids)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="race-origin")
    for buyer_id in buyer_ids:
        cur.execute(
            """INSERT INTO channel_pricing_relationships
               (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
               VALUES (%s,10,%s,12000)""",
            (buyer_id, f"r{buyer_id}"),
        )
    cur.execute(
        """INSERT INTO dealer_resale_policies
           (seller_user_id,downstream_markup_bps,authorized_min_markup_bps,
            authorized_max_markup_bps,policy_version)
           VALUES (10,12000,10000,20000,'p10')"""
    )
    first = resale.build_quote_terms(cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50, pricing_version="proc-test-v1")
    _prepare_order(cur, order_id="O-SEED", quote_id="Q-SEED", terms=first)
    _pay_order(cur, order_id="O-SEED", buyer_id=10, points=1)
    race_rows = []
    for buyer_id in buyer_ids:
        terms = resale.build_quote_terms(
            cur, buyer_user_id=buyer_id, points=1,
            platform_reference_amount_cents=50, pricing_version="proc-test-v1",
        )
        order_id = f"O-RACE-{buyer_id}"
        quote_id = f"Q-RACE-{buyer_id}"
        snapshot = _make_snapshot(terms, quote_id)
        race_rows.append((order_id, snapshot))
        cur.execute(
            """INSERT INTO price_quotes
               (quote_id,quote_type,catalog_version,product_code,buyer_user_id,points_granted,
                bonus_points,base_price_cents,effective_multiplier_bps,final_price_cents,
                expires_at,status,pricing_snapshot_jsonb)
               VALUES (%s,'procurement','proc-test-v1','WATER',%s,1,0,50,12000,60,
                       NOW()+INTERVAL '15 min','issued',%s::jsonb)""",
            (quote_id, terms["buyer_user_id"], json.dumps({"order_pricing_snapshot": snapshot})),
        )
        cur.execute(
            """INSERT INTO recharge_orders
               (id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,
                pricing_snapshot_jsonb,pricing_catalog_version,price_quote_id)
               VALUES (%s,%s,60,1,0,'pending','agent_inventory_purchase',%s::jsonb,'proc-test-v1',%s)""",
            (order_id, terms["buyer_user_id"], json.dumps(snapshot), quote_id),
        )
    db_conn.commit()

    def reserve_one(order_id: str, snapshot: dict) -> str:
        conn = connect()
        try:
            resale.reserve_order(conn.cursor(), order_id=order_id, snapshot=snapshot)
            conn.commit()
            return "ok"
        except resale.ResaleError as exc:
            conn.rollback()
            return exc.code
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda args: reserve_one(*args), race_rows))
    assert results.count("ok") == 1
    assert any(code in {"JIT_QUOTE_STALE", "MANUFACTURER_INVENTORY_INSUFFICIENT"} for code in results if code != "ok")
    cur = db_conn.cursor()
    assert _wallet(cur, 10)["paid_inventory_points"] == 0
    assert _wallet(cur, 10)["frozen_inventory_points"] == 1


def test_profit_matures_only_after_db_deadline_and_refund_window_expires(db_conn):
    cur = _setup_water_chain(db_conn)
    cur.execute(
        """UPDATE dealer_resale_hop_profit_ledger SET available_at=clock_timestamp()-INTERVAL '1 second'
           WHERE root_order_id='O-L1-L2'"""
    )
    cur.execute(
        "UPDATE dealer_resale_orders SET refund_deadline=clock_timestamp()-INTERVAL '1 second' WHERE order_id='O-L1-L2'"
    )
    with pytest.raises(resale.ResaleError, match="72"):
        resale.request_refund(cur, order_id="O-L1-L2", requested_by_user_id=20)
    db_conn.rollback()
    cur = db_conn.cursor()
    cur.execute(
        """UPDATE dealer_resale_hop_profit_ledger SET available_at=clock_timestamp()-INTERVAL '1 second'
           WHERE root_order_id='O-L1-L2'"""
    )
    result = resale.mature_profits(cur=cur)
    assert result["matured_count"] == 1
    cur.execute("SELECT status FROM dealer_resale_hop_profit_ledger WHERE root_order_id='O-L1-L2'")
    assert cur.fetchone()["status"] == "available"


def test_visibility_is_exactly_one_hop_and_admin_only_full_chain(db_conn):
    cur = _setup_water_chain(db_conn)
    buyer_view = resale.get_order_visible(cur, order_id="O-L2-SVC", actor_user_id=30)
    assert "seller_user_id" not in buyer_view and "seller_cost_basis_cents" not in buyer_view
    assert buyer_view["direction"] == "purchase"
    assert buyer_view["refund_handling"] == "PLATFORM_MANAGED"
    assert "counterparty_account_code" not in buyer_view
    seller_view = resale.get_order_visible(cur, order_id="O-L2-SVC", actor_user_id=20)
    assert "margin_cents" not in seller_view and "buyer_user_id" not in seller_view
    assert "seller_cost_basis_cents" not in seller_view
    assert "downstream_markup_bps" not in seller_view
    with pytest.raises(resale.ResaleError) as forbidden:
        resale.get_order_visible(cur, order_id="O-L2-SVC", actor_user_id=10)
    assert forbidden.value.code == "FORBIDDEN"
    admin_view = resale.get_order_visible(cur, order_id="O-L2-SVC", actor_user_id=999, is_admin=True)
    assert admin_view["seller_user_id"] == 20 and admin_view["buyer_user_id"] == 30
    chain = resale.admin_order_chain(cur, "O-L2-SVC")
    assert {row["order_id"] for row in chain["orders"]} == {"O-PLATFORM-L1", "O-L1-L2", "O-L2-SVC"}


def test_migration_rollback_forward_and_constraint_tamper_detection(db_conn):
    cur = db_conn.cursor()
    cur.execute(ROLLBACK.read_text(encoding="utf-8"))
    cur.execute(MIGRATION.read_text(encoding="utf-8"))
    cur.execute("SELECT value FROM system_settings WHERE key='DEALER_INVENTORY_RESALE_ENABLED'")
    assert cur.fetchone()["value"] == "false"
    db_conn.commit()

    cur = db_conn.cursor()
    assert resale.schema_status(cur)["ready"] is True
    cur.execute("ALTER TABLE dealer_resale_orders DROP CONSTRAINT chk_dealer_resale_order_money")
    tampered = resale.schema_status(cur)
    assert tampered["ready"] is False
    assert "chk_dealer_resale_order_money" in tampered["invalid_constraints"]
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute("ALTER TABLE dealer_consumer_sales DISABLE TRIGGER trg_dealer_consumer_sale_immutable")
    tampered = resale.schema_status(cur)
    assert tampered["ready"] is False
    assert "trg_dealer_consumer_sale_immutable" in tampered["invalid_triggers"]
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE agent_inventory_transactions "
        "DROP CONSTRAINT agent_inventory_transactions_type_check"
    )
    cur.execute(
        "ALTER TABLE agent_inventory_transactions ADD CONSTRAINT "
        "agent_inventory_transactions_type_check CHECK (type IN ('purchase_prepay'))"
    )
    tampered = resale.schema_status(cur)
    assert tampered["ready"] is False
    assert "agent_inventory_transactions_type_check" in tampered["invalid_legacy_check_values"]
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE customer_credit_transactions "
        "DROP CONSTRAINT customer_credit_transactions_source_check"
    )
    cur.execute(
        "ALTER TABLE customer_credit_transactions ADD CONSTRAINT "
        "customer_credit_transactions_source_check CHECK (source IN ("
        "'online_payment','offline_allocation','admin_adjust','tool_consume',"
        "'refund_revoke','agent_rebate','tool_fail_refund','diagnosis_delivery_refund',"
        "'direct_service_refund','unexpected_source'))"
    )
    tampered = resale.schema_status(cur)
    assert tampered["ready"] is False
    assert "customer_credit_transactions_source_check" in tampered["invalid_legacy_check_values"]
    db_conn.rollback()


def test_production_legacy_transaction_checks_allow_every_resale_value(db_conn):
    expected_inventory_types = {
        "manufacturer_origin_in",
        "resale_transfer_out",
        "resale_transfer_in",
        "consumer_sale_out",
        "consumer_refund_in",
        "resale_refund_out",
        "resale_refund_in",
    }
    cur = db_conn.cursor()
    cur.execute(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conname='agent_inventory_transactions_type_check'"
    )
    inventory_definition = str(cur.fetchone()["definition"])
    assert all(value in inventory_definition for value in expected_inventory_types)
    cur.execute(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conname='customer_credit_transactions_source_check'"
    )
    credit_definition = str(cur.fetchone()["definition"])
    assert "direct_service_refund" in credit_definition
    status = resale.schema_status(cur)
    assert status["ready"] is True
    assert status.get("invalid_legacy_check_values") == []


def test_later_diagnosis_migration_preserves_direct_service_refund_source():
    sql = (ROOT / "scripts" / "migration_diagnosis_runs_2026_07_13.sql").read_text(
        encoding="utf-8"
    )
    check_block = sql.split(
        "ALTER TABLE customer_credit_transactions ADD CONSTRAINT "
        "customer_credit_transactions_source_check",
        1,
    )[1].split("RAISE NOTICE", 1)[0]
    assert "diagnosis_delivery_refund" in check_block
    assert "direct_service_refund" in check_block


def test_schema_gate_rejects_wrong_column_shape_and_same_name_on_wrong_table(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE dealer_inventory_lots ALTER COLUMN remaining_points TYPE INTEGER"
    )
    shaped = resale.schema_status(cur)
    assert shaped["ready"] is False
    assert "dealer_inventory_lots.remaining_points" in shaped["invalid_column_shapes"]
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE dealer_resale_orders DROP CONSTRAINT chk_dealer_resale_order_money"
    )
    cur.execute(
        "ALTER TABLE user_wallets ADD CONSTRAINT chk_dealer_resale_order_money CHECK (paid_points >= 0)"
    )
    misplaced = resale.schema_status(cur)
    assert misplaced["ready"] is False
    assert "chk_dealer_resale_order_money" in misplaced["invalid_constraints"]
    db_conn.rollback()


def test_schema_gate_rejects_integer_transfer_points_and_prestart_fails(db_conn):
    cur = db_conn.cursor()
    cur.execute("ALTER TABLE dealer_inventory_transfers ALTER COLUMN points TYPE INTEGER")
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "dealer_inventory_transfers.points" in status["invalid_column_shapes"]
    with pytest.raises(psycopg2.Error):
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


def test_schema_gate_rejects_same_name_fake_check_and_unique_index(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE dealer_inventory_transfers DROP CONSTRAINT chk_dealer_transfer_points"
    )
    cur.execute(
        "ALTER TABLE dealer_inventory_transfers "
        "ADD CONSTRAINT chk_dealer_transfer_points CHECK (TRUE)"
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "chk_dealer_transfer_points" in status["invalid_constraint_definitions"]
    with pytest.raises(psycopg2.Error):
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute("DROP INDEX ux_dealer_resale_external_ref_global")
    cur.execute(
        "CREATE UNIQUE INDEX ux_dealer_resale_external_ref_global "
        "ON dealer_resale_refunds(refund_id)"
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "ux_dealer_resale_external_ref_global" in status["invalid_index_definitions"]
    with pytest.raises(psycopg2.Error):
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE purchase_agreement_acceptances "
        "DROP CONSTRAINT chk_purchase_acceptance_evidence"
    )
    cur.execute(
        "ALTER TABLE purchase_agreement_acceptances "
        "ADD CONSTRAINT chk_purchase_acceptance_evidence CHECK (TRUE)"
    )
    status = resale.purchase_agreement_schema_status(cur)
    assert status["ready"] is False
    assert any("chk_purchase_acceptance_evidence" in item for item in status["blockers"])
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute("DROP INDEX ux_recharge_terms_acceptance_once")
    cur.execute(
        "CREATE UNIQUE INDEX ux_recharge_terms_acceptance_once ON recharge_orders(id)"
    )
    status = resale.purchase_agreement_schema_status(cur)
    assert status["ready"] is False
    assert any("ux_recharge_terms_acceptance_once" in item for item in status["blockers"])
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


def test_exact_schema_contract_rejects_semantic_lookalikes_and_extra_keys(db_conn):
    cur = db_conn.cursor()
    cur.execute("ALTER TABLE dealer_inventory_transfers DROP CONSTRAINT chk_dealer_transfer_points")
    cur.execute(
        "ALTER TABLE dealer_inventory_transfers "
        "ADD CONSTRAINT chk_dealer_transfer_points CHECK (points > 0 OR TRUE)"
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "schema_contract_digest" in status["invalid_constraints"]
    with pytest.raises(psycopg2.Error):
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE service_refund_liability_ledger "
        "DROP CONSTRAINT chk_service_refund_liability_money"
    )
    cur.execute(
        "ALTER TABLE service_refund_liability_ledger "
        "ADD CONSTRAINT chk_service_refund_liability_money CHECK (TRUE)"
    )
    assert resale.schema_status(cur)["ready"] is False
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


@pytest.mark.parametrize(
    ("index_name", "create_sql", "migration_path", "purchase_only"),
    [
        (
            "ux_dealer_lot_source_order",
            """CREATE UNIQUE INDEX ux_dealer_lot_source_order
               ON dealer_inventory_lots(source_order_id)
               WHERE source_order_id IS NOT NULL AND FALSE""",
            MIGRATION,
            False,
        ),
        (
            "ux_dealer_resale_external_ref_global",
            """CREATE UNIQUE INDEX ux_dealer_resale_external_ref_global
               ON dealer_resale_refunds(lower(btrim(external_channel)),btrim(external_refund_id))
               WHERE external_channel IS NOT NULL AND external_refund_id IS NOT NULL AND FALSE""",
            MIGRATION,
            False,
        ),
        (
            "ux_recharge_terms_acceptance_once",
            """CREATE UNIQUE INDEX ux_recharge_terms_acceptance_once
               ON recharge_orders((pricing_snapshot_jsonb->>'terms_acceptance_id'))
               WHERE pricing_snapshot_jsonb ? 'terms_acceptance_id' AND FALSE""",
            REFUND_AGREEMENT_MIGRATION,
            True,
        ),
        (
            "ux_service_refund_provider_ref_global",
            """CREATE UNIQUE INDEX ux_service_refund_provider_ref_global
               ON service_refund_cash_jobs(provider_refund_id)
               WHERE provider_refund_id IS NOT NULL AND FALSE""",
            REFUND_AGREEMENT_MIGRATION,
            False,
        ),
    ],
)
def test_partial_unique_index_and_false_cannot_fake_readiness_or_prestart(
    db_conn, index_name, create_sql, migration_path, purchase_only,
):
    cur = db_conn.cursor()
    cur.execute(f"DROP INDEX {index_name}")
    cur.execute(create_sql)
    status = (
        resale.purchase_agreement_schema_status(cur)
        if purchase_only
        else resale.schema_status(cur)
    )
    assert status["ready"] is False
    with pytest.raises(psycopg2.Error):
        cur.execute(migration_path.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute("ALTER TABLE consumer_refund_cases DROP CONSTRAINT ux_consumer_refund_source")
    cur.execute(
        "ALTER TABLE consumer_refund_cases "
        "ADD CONSTRAINT ux_consumer_refund_source UNIQUE (case_id)"
    )
    assert resale.schema_status(cur)["ready"] is False
    with pytest.raises(psycopg2.Error):
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute("DROP INDEX ux_recharge_terms_acceptance_once")
    cur.execute(
        """CREATE UNIQUE INDEX ux_recharge_terms_acceptance_once
           ON recharge_orders ((pricing_snapshot_jsonb->>'terms_acceptance_id'),id)
           WHERE pricing_snapshot_jsonb ? 'terms_acceptance_id'"""
    )
    status = resale.purchase_agreement_schema_status(cur)
    assert status["ready"] is False
    assert any("ux_recharge_terms_acceptance_once" in item for item in status["blockers"])
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


def test_migrations_reject_every_critical_money_shape_not_just_representatives(db_conn):
    cur = db_conn.cursor()
    cur.execute("ALTER TABLE dealer_inventory_lots ALTER COLUMN reserved_cost_cents TYPE INTEGER")
    with pytest.raises(psycopg2.Error):
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute("ALTER TABLE consumer_refund_cases ALTER COLUMN revoked_paid_points TYPE INTEGER")
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("service_refund_cash_jobs", "status"),
        ("service_refund_cash_jobs", "attempt_count"),
        ("external_refund_proof_registry", "provider"),
        ("external_refund_proof_registry", "external_refund_id"),
        ("external_refund_proof_registry", "refund_scope"),
        ("external_refund_proof_registry", "local_ref"),
    ],
)
def test_control_and_proof_identity_columns_must_remain_not_null(db_conn, table, column):
    cur = db_conn.cursor()
    cur.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert f"{table}.{column}" in status["invalid_column_shapes"]
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


def test_schema_gate_rejects_replica_only_and_wrong_event_purchase_trigger(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE recharge_orders ENABLE REPLICA TRIGGER trg_recharge_purchase_acceptance_use"
    )
    status = resale.purchase_agreement_schema_status(cur)
    assert status["ready"] is False
    assert any("trg_recharge_purchase_acceptance_use" in item for item in status["blockers"])
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute("CREATE SCHEMA evil")
    cur.execute(
        """CREATE FUNCTION evil.consume_purchase_acceptance_use()
           RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"""
    )
    cur.execute("DROP TRIGGER trg_recharge_purchase_acceptance_use ON recharge_orders")
    cur.execute(
        """CREATE TRIGGER trg_recharge_purchase_acceptance_use
           AFTER INSERT OR UPDATE OF pricing_snapshot_jsonb ON recharge_orders
           FOR EACH ROW EXECUTE FUNCTION evil.consume_purchase_acceptance_use()"""
    )
    status = resale.purchase_agreement_schema_status(cur)
    assert status["ready"] is False
    assert any("trg_recharge_purchase_acceptance_use" in item for item in status["blockers"])
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute(
        """CREATE OR REPLACE FUNCTION consume_purchase_acceptance_use()
           RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"""
    )
    status = resale.purchase_agreement_schema_status(cur)
    assert status["ready"] is False
    assert "购买协议触发函数定义不合格" in status["blockers"]
    # The idempotent migration repairs an in-schema function body, then the gate
    # proves the real owner/surface/one-use logic has returned.
    cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    assert resale.purchase_agreement_schema_status(cur)["ready"] is True
    db_conn.rollback()

    cur = db_conn.cursor()
    cur.execute("DROP TRIGGER trg_recharge_purchase_acceptance_use ON recharge_orders")
    cur.execute(
        """CREATE TRIGGER trg_recharge_purchase_acceptance_use
           AFTER DELETE ON recharge_orders
           FOR EACH ROW EXECUTE FUNCTION consume_purchase_acceptance_use()"""
    )
    status = resale.purchase_agreement_schema_status(cur)
    assert status["ready"] is False
    assert any("trg_recharge_purchase_acceptance_use" in item for item in status["blockers"])
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()

def test_not_valid_money_constraint_blocks_schema_and_prestart_migration(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE dealer_resale_global_settings "
        "DROP CONSTRAINT chk_dealer_resale_settings_row_version"
    )
    cur.execute(
        "UPDATE dealer_resale_global_settings SET row_version=0 WHERE singleton_id=1"
    )
    cur.execute(
        "ALTER TABLE dealer_resale_global_settings "
        "ADD CONSTRAINT chk_dealer_resale_settings_row_version "
        "CHECK (row_version >= 1) NOT VALID"
    )
    status = resale.schema_status(cur)
    assert status["ready"] is False
    assert "chk_dealer_resale_settings_row_version" in status["invalid_constraints"]
    with pytest.raises(psycopg2.Error):
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


def test_not_valid_purchase_evidence_constraint_blocks_flag_off_startup_schema(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1)
    cur.execute(
        "ALTER TABLE purchase_agreement_acceptances "
        "DROP CONSTRAINT chk_purchase_acceptance_evidence"
    )
    cur.execute(
        """INSERT INTO purchase_agreement_acceptances(
               acceptance_id,user_id,agreement_type,agreement_version,content_hash,
               surface,evidence_jsonb)
           VALUES ('BAD-EVIDENCE',1,'customer-credit-purchase','v1','hash','checkout','[]')"""
    )
    cur.execute(
        "ALTER TABLE purchase_agreement_acceptances "
        "ADD CONSTRAINT chk_purchase_acceptance_evidence "
        "CHECK (jsonb_typeof(evidence_jsonb)='object') NOT VALID"
    )
    status = resale.purchase_agreement_schema_status(cur)
    assert status["ready"] is False
    assert any("chk_purchase_acceptance_evidence" in item for item in status["blockers"])
    with pytest.raises(psycopg2.Error):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


def test_valid_data_does_not_allow_not_valid_constraint_to_self_heal(db_conn):
    cur = db_conn.cursor()
    cur.execute(
        "ALTER TABLE service_refund_cash_jobs DROP CONSTRAINT chk_service_refund_cash_amount"
    )
    cur.execute(
        "ALTER TABLE service_refund_cash_jobs ADD CONSTRAINT chk_service_refund_cash_amount "
        "CHECK (amount_cents > 0 AND attempt_count >= 0) NOT VALID"
    )
    assert resale.schema_status(cur)["ready"] is False
    with pytest.raises(psycopg2.Error, match="NOT VALID"):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()


def test_migration_rejects_completed_b2b_row_with_empty_manual_proof(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO dealer_resale_refunds(
               refund_id,order_id,requested_by_user_id,responsible_seller_user_id,
               status,reason,deadline_at,completed_at,external_channel,
               external_refund_id,audit_jsonb)
           VALUES ('DRF-EMPTY-PROOF','O-L2-SVC',30,20,'completed','historic empty proof',
                   NOW(),NOW(),'wechat','WX-EMPTY-PROOF','{}'::jsonb)"""
    )
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=70
           WHERE id='O-L2-SVC'"""
    )
    db_conn.commit()
    cur = db_conn.cursor()
    with pytest.raises(psycopg2.Error, match="trusted immutable external proof"):
        cur.execute(REFUND_AGREEMENT_MIGRATION.read_text(encoding="utf-8"))
    db_conn.rollback()
    cur = db_conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS c FROM external_refund_proof_registry "
        "WHERE external_refund_id='WX-EMPTY-PROOF'"
    )
    assert int(cur.fetchone()["c"]) == 0


def test_persisted_quote_locks_cash_and_direct_seller_plan_and_rejects_wrong_buyer_replay(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20, 99)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=100, acquisition_cost_cents=50, key="quote-origin")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'rel-v1',12000)"""
    )
    cur.execute(
        """INSERT INTO dealer_resale_policies
           (seller_user_id,downstream_markup_bps,authorized_min_markup_bps,
            authorized_max_markup_bps,policy_version)
           VALUES (10,12000,10000,20000,'seller10-v1')"""
    )
    seed_terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=100, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-Q-SEED", quote_id="Q-Q-SEED", terms=seed_terms)
    _pay_order(cur, order_id="O-Q-SEED", buyer_id=10, points=100)
    db_conn.commit()

    from services import price_quote

    quote = price_quote.issue_procurement_quote(
        dealer_id=20, product_code="WATER", idempotency_key="quote-lock-test"
    )
    assert int(quote["final_price_cents"]) == 50
    assert int(quote["points_granted"]) == 83
    assert quote["seller_legal_entity_id"] == "USER:10"
    order_snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    assert order_snapshot["seller_user_id"] == 10
    assert order_snapshot["seller_cost_basis_cents"] == 41
    assert order_snapshot["margin_cents"] == 9
    assert order_snapshot["cash_anchor_semantic_version"] == "procurement-cash-anchor-v2"
    assert order_snapshot["channel_beneficiary_user_id"] is None

    conn = connect()
    try:
        cur = conn.cursor()
        with pytest.raises(price_quote.QuoteError, match="买方不一致"):
            price_quote.lock_and_validate(
                cur, quote["quote_id"], buyer_user_id=99, quote_type="procurement"
            )
        conn.rollback()
        cur = conn.cursor()
        price_quote.lock_and_validate(
            cur, quote["quote_id"], buyer_user_id=20, quote_type="procurement"
        )
        assert price_quote.consume_quote(cur, quote["quote_id"], "ORDER-ONCE") is True
        assert price_quote.consume_quote(cur, quote["quote_id"], "ORDER-TWICE") is False
        conn.commit()
    finally:
        conn.close()


def test_cash_anchor_delivers_same_points_with_existing_seller_stock_or_full_jit(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20, 30)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1000, acquisition_cost_cents=500, key="cash-anchor-origin")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'cash-20-v1',12000),(30,20,'cash-30-v1',12000)"""
    )
    db_conn.commit()

    from services import price_quote

    jit_quote = price_quote.issue_procurement_quote(
        dealer_id=30, product_code="WATER", idempotency_key="cash-anchor-jit"
    )
    jit_snapshot = price_quote.quote_order_pricing_snapshot(jit_quote, required=True)
    assert int(jit_quote["final_price_cents"]) == 50
    assert int(jit_quote["points_granted"]) == 69
    assert len(jit_snapshot["fulfillment_plan"]["hops"]) == 3
    assert jit_snapshot["fulfillment_plan"]["funds_conservation"]["residual_cents"] == 0

    # Give the direct seller the exact inventory through its own independent order.
    cur = db_conn.cursor()
    seller_terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=69, platform_reference_amount_cents=35,
        pricing_version="proc-test-v1",
    )
    _prepare_order(
        cur, order_id="O-CASH-SELLER-STOCK", quote_id="Q-CASH-SELLER-STOCK",
        terms=seller_terms,
    )
    _pay_order(cur, order_id="O-CASH-SELLER-STOCK", buyer_id=20, points=69)
    db_conn.commit()

    stock_quote = price_quote.issue_procurement_quote(
        dealer_id=30, product_code="WATER", idempotency_key="cash-anchor-stock"
    )
    stock_snapshot = price_quote.quote_order_pricing_snapshot(stock_quote, required=True)
    assert int(stock_quote["final_price_cents"]) == 50
    assert int(stock_quote["points_granted"]) == 69
    assert len(stock_snapshot["fulfillment_plan"]["hops"]) == 1
    assert stock_snapshot["seller_cost_basis_cents"] == 42
    assert stock_snapshot["margin_cents"] == 8
    proof = stock_snapshot["fulfillment_plan"]["funds_conservation"]
    assert proof["historical_principal_cents"] == 42
    assert proof["final_seller_margin_cents"] == 8
    assert proof["assigned_cents"] == proof["final_paid_cents"] == 50
    assert proof["residual_cents"] == 0


def test_cash_anchor_order_callback_settles_each_hop_once_from_frozen_snapshot(
    db_conn, monkeypatch,
):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20, 30)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1000, acquisition_cost_cents=500, key="cash-callback-origin")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'cash-callback-20-v1',12000),
                  (30,20,'cash-callback-30-v1',12000)"""
    )
    db_conn.commit()

    from api.agent_workbench_api import _create_quoted_inventory_order
    from db.wallet_db import complete_recharge
    from services import agent_inventory_pricing, price_quote

    monkeypatch.setattr(
        agent_inventory_pricing,
        "inventory_snapshot_activation_state",
        lambda _cur: {"ready": True},
    )

    quote = price_quote.issue_procurement_quote(
        dealer_id=30, product_code="WATER", idempotency_key="cash-callback-quote",
    )
    order_id, snapshot, reused = _create_quoted_inventory_order(
        agent_user_id=30,
        quote_id=str(quote["quote_id"]),
        order_id="O-CASH-ANCHOR-CALLBACK",
        idempotency_key="cash-callback-order",
    )
    assert reused is False
    assert order_id == "O-CASH-ANCHOR-CALLBACK"
    assert int(snapshot["amount_cents"]) == 50
    assert int(snapshot["base_points"]) == 69
    frozen_plan = snapshot["fulfillment_plan"]
    assert frozen_plan["funds_conservation"]["residual_cents"] == 0

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(
            lambda index: complete_recharge(order_id, f"PAY-CASH-ANCHOR-{index}"),
            range(20),
        ))
    assert all(result is not None for result in results)

    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 69
    cur.execute(
        "SELECT COUNT(*) AS c FROM dealer_inventory_lots "
        "WHERE owner_agent_user_id=30 AND source_order_id=%s",
        (order_id,),
    )
    assert int(cur.fetchone()["c"]) == 1
    cur.execute(
        """SELECT hop_seq,seller_user_id,margin_cents,principal_recovery_cents,
                  agent_payable_cents
           FROM dealer_resale_hop_profit_ledger
           WHERE root_order_id=%s ORDER BY hop_seq""",
        (order_id,),
    )
    ledgers = [dict(row) for row in cur.fetchall()]
    assert len(ledgers) == len(frozen_plan["hops"]) == 3
    assert all(int(row["margin_cents"]) >= 0 for row in ledgers)
    for ledger, hop in zip(ledgers, frozen_plan["hops"]):
        assert int(ledger["seller_user_id"]) == int(hop["seller_user_id"])
        assert int(ledger["margin_cents"]) == int(hop["margin_cents"])
        assert int(ledger["principal_recovery_cents"]) == int(hop["principal_recovery_cents"])
        assert int(ledger["agent_payable_cents"]) == int(hop["agent_payable_cents"])
    cur.execute(
        """SELECT transfer_id,COUNT(*) AS c,SUM(points_delta) AS net
           FROM dealer_resale_hop_transfer_entries
           WHERE root_order_id=%s GROUP BY transfer_id""",
        (order_id,),
    )
    transfers = cur.fetchall()
    assert len(transfers) == 3
    assert all(int(row["c"]) == 2 and int(row["net"]) == 0 for row in transfers)
    cur.execute(
        "SELECT payment_status,amount_cents,base_points FROM recharge_orders WHERE id=%s",
        (order_id,),
    )
    settled = dict(cur.fetchone())
    assert settled == {"payment_status": "paid", "amount_cents": 50, "base_points": 69}


def test_kill_before_commit_rolls_back_seller_reservation(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="kill-origin")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version)
           VALUES (20,10,'rel-kill-v1')"""
    )
    first = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-KILL-SEED", quote_id="Q-KILL-SEED", terms=first)
    _pay_order(cur, order_id="O-KILL-SEED", buyer_id=10, points=1)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    snapshot = _make_snapshot(terms, "Q-KILL")
    cur.execute(
        """INSERT INTO price_quotes
           (quote_id,quote_type,catalog_version,product_code,buyer_user_id,points_granted,
            bonus_points,base_price_cents,effective_multiplier_bps,final_price_cents,
            expires_at,status,pricing_snapshot_jsonb)
           VALUES ('Q-KILL','procurement','proc-test-v1','WATER',20,1,0,%s,%s,%s,
                   NOW()+INTERVAL '15 min','issued',%s::jsonb)""",
        (
            terms["seller_cost_basis_cents"], terms["downstream_markup_bps"],
            terms["sale_amount_cents"], json.dumps({"order_pricing_snapshot": snapshot}),
        ),
    )
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,
            pricing_snapshot_jsonb,pricing_catalog_version,price_quote_id)
           VALUES ('O-KILL',20,%s,1,0,'pending','agent_inventory_purchase',
                   %s::jsonb,'proc-test-v1','Q-KILL')""",
        (terms["sale_amount_cents"], json.dumps(snapshot)),
    )
    db_conn.commit()

    killed = connect()
    resale.reserve_order(killed.cursor(), order_id="O-KILL", snapshot=snapshot)
    killed.close()  # server process dies before commit

    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM dealer_resale_orders WHERE order_id='O-KILL'")
    assert int(cur.fetchone()["c"]) == 0
    assert _wallet(cur, 10)["paid_inventory_points"] == 1
    assert _wallet(cur, 10)["frozen_inventory_points"] == 0
    cur.execute("SELECT remaining_points,reserved_points FROM dealer_inventory_lots WHERE owner_agent_user_id=10")
    lot = cur.fetchone()
    assert (int(lot["remaining_points"]), int(lot["reserved_points"])) == (1, 0)


def test_hard_process_termination_rolls_back_uncommitted_reservation(db_conn, tmp_path):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="hard-kill-origin")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version)
           VALUES (20,10,'rel-hard-kill-v1')"""
    )
    seed = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-HARD-SEED", quote_id="Q-HARD-SEED", terms=seed)
    _pay_order(cur, order_id="O-HARD-SEED", buyer_id=10, points=1)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    snapshot = _make_snapshot(terms, "Q-HARD-KILL")
    cur.execute(
        """INSERT INTO price_quotes
           (quote_id,quote_type,catalog_version,product_code,buyer_user_id,points_granted,
            bonus_points,base_price_cents,effective_multiplier_bps,final_price_cents,
            expires_at,status,pricing_snapshot_jsonb)
           VALUES ('Q-HARD-KILL','procurement','proc-test-v1','WATER',20,1,0,%s,%s,%s,
                   NOW()+INTERVAL '15 min','issued',%s::jsonb)""",
        (
            terms["seller_cost_basis_cents"], terms["downstream_markup_bps"],
            terms["sale_amount_cents"], json.dumps({"order_pricing_snapshot": snapshot}),
        ),
    )
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,
            pricing_snapshot_jsonb,pricing_catalog_version,price_quote_id)
           VALUES ('O-HARD-KILL',20,%s,1,0,'pending','agent_inventory_purchase',
                   %s::jsonb,'proc-test-v1','Q-HARD-KILL')""",
        (terms["sale_amount_cents"], json.dumps(snapshot)),
    )
    db_conn.commit()

    from .conftest import TEST_URL
    ready_file = tmp_path / "reservation-ready"
    child_env = os.environ.copy()
    child_env.update(
        {
            "TEST_CHILD_DATABASE_URL": TEST_URL,
            "TEST_CHILD_ORDER_ID": "O-HARD-KILL",
            "TEST_CHILD_SNAPSHOT": json.dumps(snapshot),
            "TEST_CHILD_READY_FILE": str(ready_file),
        }
    )
    child_code = """
import json, os, time
import psycopg2
from psycopg2.extras import RealDictCursor
from services import dealer_inventory_resale as resale

conn = psycopg2.connect(os.environ['TEST_CHILD_DATABASE_URL'], cursor_factory=RealDictCursor)
resale.reserve_order(
    conn.cursor(),
    order_id=os.environ['TEST_CHILD_ORDER_ID'],
    snapshot=json.loads(os.environ['TEST_CHILD_SNAPSHOT']),
)
with open(os.environ['TEST_CHILD_READY_FILE'], 'w', encoding='utf-8') as marker:
    marker.write('ready')
time.sleep(60)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        cwd=str(ROOT),
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while not ready_file.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready_file.exists():
        stderr = proc.communicate(timeout=5)[1] if proc.poll() is not None else ""
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail(f"child failed to reach the uncommitted reservation point: {stderr}")
    proc.terminate()
    proc.wait(timeout=15)
    assert proc.returncode not in (None, 0)

    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM dealer_resale_orders WHERE order_id='O-HARD-KILL'")
    assert int(cur.fetchone()["c"]) == 0
    assert _wallet(cur, 10)["paid_inventory_points"] == 1
    assert _wallet(cur, 10)["frozen_inventory_points"] == 0


def test_four_processes_read_same_committed_pricing_version(db_conn):
    _setup_water_chain(db_conn)
    from .conftest import TEST_URL

    child_code = """
import json, os
import psycopg2
from psycopg2.extras import RealDictCursor

conn = psycopg2.connect(os.environ['TEST_CHILD_DATABASE_URL'], cursor_factory=RealDictCursor)
try:
    cur = conn.cursor()
    cur.execute(
        'SELECT pricing_version,quote_id,points FROM dealer_resale_orders WHERE order_id=%s',
        (os.environ['TEST_CHILD_ORDER_ID'],),
    )
    row = cur.fetchone()
    print(json.dumps([row['pricing_version'], row['quote_id'], int(row['points'])]))
finally:
    conn.close()
"""
    child_env = os.environ.copy()
    child_env.update(
        {
            "TEST_CHILD_DATABASE_URL": TEST_URL,
            "TEST_CHILD_ORDER_ID": "O-L2-SVC",
        }
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child_code],
            cwd=str(ROOT),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    values = []
    for proc in processes:
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, stderr
        values.append(tuple(json.loads(stdout)))
    assert values == [("proc-test-v1", "Q-L2-SVC", 1)] * 4


def test_stale_schedulers_never_release_before_delayed_success_callback(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="stale-origin")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version)
           VALUES (20,10,'rel-stale-v1')"""
    )
    seed = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-STALE-SEED", quote_id="Q-STALE-SEED", terms=seed)
    _pay_order(cur, order_id="O-STALE-SEED", buyer_id=10, points=1)
    terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-STALE", quote_id="Q-STALE", terms=terms)
    cur.execute(
        "UPDATE recharge_orders SET created_at=clock_timestamp()-INTERVAL '31 minutes' "
        "WHERE id='O-STALE'"
    )
    db_conn.commit()

    def scan_once() -> dict:
        conn = connect()
        try:
            result = resale.release_stale_reservations(
                limit=10, stale_after_minutes=30, cur=conn.cursor(),
            )
            conn.commit()
            return result
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        scans = list(pool.map(lambda _: scan_once(), range(2)))
    assert all(item["released_count"] == 0 for item in scans)
    assert all(item["automatic_release_disabled"] is True for item in scans)
    assert all("O-STALE" in item["reconciliation_order_ids"] for item in scans)
    cur = db_conn.cursor()
    assert _wallet(cur, 10)["paid_inventory_points"] == 0
    assert _wallet(cur, 10)["frozen_inventory_points"] == 1
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-STALE'")
    assert cur.fetchone()["state"] == "reserved"
    cur.execute("SELECT payment_status FROM recharge_orders WHERE id='O-STALE'")
    assert cur.fetchone()["payment_status"] == "pending"
    _pay_order(cur, order_id="O-STALE", buyer_id=20, points=1)
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-STALE'")
    assert cur.fetchone()["state"] == "paid"


def test_failed_consumer_payment_setup_releases_service_fifo_once(db_conn):
    _setup_water_chain(db_conn)
    cur = db_conn.cursor()
    terms = resale.build_consumer_quote_terms(
        cur, seller_user_id=30, consumer_user_id=40, points=1,
        catalog_reference_amount_cents=80, pricing_version="retail-sv30-v1",
        digital_goods_acknowledged=True,
    )
    snapshot = {
        **terms, "price_quote_id": "Q-FAIL-RETAIL", "quote_type": "retail",
        "buyer_user_id": 40, "buyer_paid_cents": 80, "catalog_version": "retail-sv30-v1",
        "points_granted": 1, "tool_points": 1, "publish_points": 0, "bonus_points": 0,
    }
    cur.execute(
        """INSERT INTO price_quotes
           (quote_id,quote_type,catalog_version,product_code,buyer_user_id,points_granted,
            bonus_points,base_price_cents,effective_multiplier_bps,final_price_cents,
            expires_at,status,used_order_id,pricing_snapshot_jsonb)
           VALUES ('Q-FAIL-RETAIL','retail','retail-sv30-v1','WATER',40,1,0,70,11428,80,
                   NOW()+INTERVAL '15 min','consumed','O-FAIL-RETAIL',%s::jsonb)""",
        (json.dumps({"order_pricing_snapshot": snapshot}),),
    )
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,agent_user_id,amount_cents,base_points,bonus_points,payment_status,
            order_type,pricing_snapshot_jsonb,pricing_catalog_version,price_quote_id)
           VALUES ('O-FAIL-RETAIL',40,30,80,1,0,'pending','customer_recharge',
                   %s::jsonb,'retail-sv30-v1','Q-FAIL-RETAIL')""",
        (json.dumps(snapshot),),
    )
    resale.reserve_consumer_sale(cur, order_id="O-FAIL-RETAIL", snapshot=snapshot)
    assert resale.cancel_consumer_reservation(cur, "O-FAIL-RETAIL") is True
    assert resale.cancel_consumer_reservation(cur, "O-FAIL-RETAIL") is True
    db_conn.commit()
    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 1
    assert _wallet(cur, 30)["frozen_inventory_points"] == 0
    cur.execute(
        "SELECT remaining_points,reserved_points FROM dealer_inventory_lots "
        "WHERE owner_agent_user_id=30"
    )
    lot = cur.fetchone()
    assert (int(lot["remaining_points"]), int(lot["reserved_points"])) == (1, 0)
    cur.execute("SELECT status FROM price_quotes WHERE quote_id='Q-FAIL-RETAIL'")
    assert cur.fetchone()["status"] == "consumed", "失败支付不得复活旧报价"


def test_duplicate_profit_schedulers_release_once(db_conn):
    cur = _setup_water_chain(db_conn)
    cur.execute(
        """UPDATE dealer_resale_hop_profit_ledger
           SET available_at=clock_timestamp()-INTERVAL '1 second'
           WHERE root_order_id='O-L1-L2'"""
    )
    db_conn.commit()

    def run_maturity() -> int:
        conn = connect()
        try:
            result = resale.mature_profits(cur=conn.cursor())
            conn.commit()
            return int(result["matured_count"])
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: run_maturity(), range(2)))
    assert sum(counts) == 1


def test_policy_change_does_not_override_relationship_or_old_order_cost(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10, 20, 30)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=2, acquisition_cost_cents=100, key="immutable-origin")
    cur.execute(
        """INSERT INTO channel_pricing_relationships
           (buyer_dealer_id,upstream_channel_account_id,relationship_version,cost_multiplier_bps)
           VALUES (20,10,'r20-v1',12000),(30,10,'r30-v1',12000)"""
    )
    cur.execute(
        """INSERT INTO dealer_resale_policies
           (seller_user_id,downstream_markup_bps,authorized_min_markup_bps,
            authorized_max_markup_bps,policy_version)
           VALUES (10,12000,10000,20000,'p10-v1')"""
    )
    seed = resale.build_quote_terms(
        cur, buyer_user_id=10, points=2, platform_reference_amount_cents=100,
        pricing_version="proc-test-v1",
    )
    _prepare_order(cur, order_id="O-IMM-SEED", quote_id="Q-IMM-SEED", terms=seed)
    _pay_order(cur, order_id="O-IMM-SEED", buyer_id=10, points=2)

    old_terms = resale.build_quote_terms(
        cur, buyer_user_id=20, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    assert old_terms["sale_amount_cents"] == 60
    old_snapshot = _make_snapshot(old_terms, "Q-IMM-OLD")
    cur.execute(
        """INSERT INTO price_quotes
           (quote_id,quote_type,catalog_version,product_code,buyer_user_id,points_granted,
            bonus_points,base_price_cents,effective_multiplier_bps,final_price_cents,
            expires_at,status,pricing_snapshot_jsonb)
           VALUES ('Q-IMM-OLD','procurement','proc-test-v1','WATER',20,1,0,50,12000,60,
                   NOW()+INTERVAL '15 min','issued',%s::jsonb)""",
        (json.dumps({"order_pricing_snapshot": old_snapshot}),),
    )
    cur.execute(
        """INSERT INTO recharge_orders
           (id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,
            pricing_snapshot_jsonb,pricing_catalog_version,price_quote_id)
           VALUES ('O-IMM-OLD',20,60,1,0,'pending','agent_inventory_purchase',
                   %s::jsonb,'proc-test-v1','Q-IMM-OLD')""",
        (json.dumps(old_snapshot),),
    )
    cur.execute(
        """UPDATE dealer_resale_policies
           SET downstream_markup_bps=15000,policy_version='p10-v2',row_version=row_version+1
           WHERE seller_user_id=10"""
    )
    resale.reserve_order(cur, order_id="O-IMM-OLD", snapshot=old_snapshot)
    new_terms = resale.build_quote_terms(
        cur, buyer_user_id=30, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    assert new_terms["sale_amount_cents"] == 60
    _pay_order(cur, order_id="O-IMM-OLD", buyer_id=20, points=1)
    cur.execute(
        "SELECT sale_amount_cents,downstream_markup_bps FROM dealer_resale_orders WHERE order_id='O-IMM-OLD'"
    )
    old_order = cur.fetchone()
    assert (int(old_order["sale_amount_cents"]), int(old_order["downstream_markup_bps"])) == (60, 12000)
    cur.execute(
        "SELECT acquisition_cost_cents FROM dealer_inventory_lots WHERE source_order_id='O-IMM-OLD'"
    )
    assert int(cur.fetchone()["acquisition_cost_cents"]) == 60


def test_independent_flag_off_creates_no_resale_quote_terms(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    cur.execute(
        "UPDATE system_settings SET value='false' WHERE key='DEALER_INVENTORY_RESALE_ENABLED'"
    )
    assert resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    ) is None
    cur.execute("SELECT COUNT(*) AS c FROM dealer_inventory_lots")
    assert int(cur.fetchone()["c"]) == 0


def test_readiness_requires_audited_sellable_manufacturer_inventory(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    before = resale.readiness(cur=cur)
    assert "平台厂家尚无经审计的原始库存发行记录" in before["blockers"]
    assert "平台厂家尚无可售真实库存批次" in before["blockers"]

    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="readiness-origin")
    after = resale.readiness(cur=cur)
    assert "平台厂家尚无经审计的原始库存发行记录" not in after["blockers"]
    assert "平台厂家尚无可售真实库存批次" not in after["blockers"]


def test_consumer_quote_preview_uses_real_fifo_policy_without_writes(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 30)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    cur.execute(
        """INSERT INTO dealer_resale_policies(
               seller_user_id,downstream_markup_bps,authorized_min_markup_bps,
               authorized_max_markup_bps,policy_version)
           VALUES (30,12000,10000,20000,'retail-policy-v1')"""
    )
    with pytest.raises(resale.ResaleError) as missing:
        resale.preview_consumer_quote_terms(
            cur, seller_user_id=30, points=2,
            catalog_reference_amount_cents=120, pricing_version="retail-v1",
        )
    assert missing.value.code == "SELLER_INVENTORY_INSUFFICIENT"

    _issue_manufacturer(cur, points=2, acquisition_cost_cents=100, key="retail-readiness")
    cur.execute(
        """UPDATE dealer_inventory_lots SET owner_agent_user_id=30
           WHERE source_kind='manufacturer_origin'"""
    )
    before = {}
    for table in ("price_quotes", "dealer_consumer_sales", "recharge_orders"):
        cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
        before[table] = int(cur.fetchone()["c"])

    terms = resale.preview_consumer_quote_terms(
        cur, seller_user_id=30, points=2,
        catalog_reference_amount_cents=120, pricing_version="retail-v1",
    )

    assert terms["sale_amount_cents"] == 120
    for table, expected in before.items():
        cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
        assert int(cur.fetchone()["c"]) == expected


def test_readiness_blocks_b2b_refund_without_persisted_amount_snapshot(db_conn):
    cur = _setup_water_chain(db_conn)
    resale.request_refund(
        cur,
        order_id="O-L2-SVC",
        requested_by_user_id=30,
        processing_cost_cents=3,
        processing_cost_evidence={"invoice": "actual-cost"},
    )
    cur.execute(
        "UPDATE dealer_resale_refunds SET refund_amount_cents=0 WHERE order_id='O-L2-SVC'"
    )
    status = resale.readiness(cur=cur)
    assert status["ready"] is False
    assert any("B2B 退款缺持久金额" in item for item in status["blockers"])


def test_readiness_requires_exact_service_v24_body_hash(db_conn):
    from services.agent_agreement import CURRENT_CONTENT_HASH, CURRENT_VERSION

    cur = db_conn.cursor()
    _seed_users(cur, 10)
    cur.execute(
        """INSERT INTO dealer_resale_policies
           (seller_user_id,downstream_markup_bps,authorized_min_markup_bps,
            authorized_max_markup_bps,policy_version)
           VALUES (10,10000,10000,20000,'policy-v1')"""
    )
    before = resale.readiness(cur=cur)
    assert any("v2.4 协议" in item and "10" in item for item in before["blockers"])
    cur.execute(
        """INSERT INTO agent_factory_agreements
           (agent_user_id,version,status,content_hash,signed_at)
           VALUES (10,%s,'signed','wrong-hash',NOW())""",
        (CURRENT_VERSION,),
    )
    wrong = resale.readiness(cur=cur)
    assert any("v2.4 协议" in item and "10" in item for item in wrong["blockers"])
    cur.execute(
        "UPDATE agent_factory_agreements SET content_hash=%s WHERE agent_user_id=10",
        (CURRENT_CONTENT_HASH,),
    )
    exact = resale.readiness(cur=cur)
    assert not any("v2.4 协议" in item and "10" in item for item in exact["blockers"])


def test_readiness_validates_historical_consumer_terms_against_order_snapshot(db_conn):
    _sell_water_to_consumer(
        db_conn,
        purchase_terms_version="user-v1.9",
        purchase_terms_hash="historical-user-terms-body-hash",
    )

    status = resale.readiness(cur=db_conn.cursor())

    assert not any(
        "消费者订单" in blocker and "O-SVC-CUSTOMER" in blocker
        for blocker in status["blockers"]
    )


def test_readiness_rejects_consumer_order_without_terms_snapshot(db_conn):
    _sell_water_to_consumer(db_conn)

    status = resale.readiness(cur=db_conn.cursor())

    assert any(
        "消费者订单" in blocker and "O-SVC-CUSTOMER" in blocker
        for blocker in status["blockers"]
    )


def test_readiness_rejects_consumer_order_without_unique_acceptance_use(db_conn):
    _sell_water_to_consumer(
        db_conn,
        purchase_terms_version="user-v1.9",
        purchase_terms_hash="historical-user-terms-body-hash",
    )
    cur = db_conn.cursor()
    cur.execute(
        "DELETE FROM purchase_agreement_acceptance_uses WHERE purchase_ref_id=%s",
        ("O-SVC-CUSTOMER",),
    )

    status = resale.readiness(cur=cur)

    assert any(
        "消费者订单" in blocker and "O-SVC-CUSTOMER" in blocker
        for blocker in status["blockers"]
    )


def test_opening_lot_prepare_is_concurrent_idempotent_and_evidence_bound(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 10)
    resale._inventory_wallet(cur, 10)
    cur.execute(
        "UPDATE agent_inventory_wallets SET paid_inventory_points=5 WHERE agent_user_id=10"
    )
    db_conn.commit()
    evidence = {"inventory_snapshot": "throwaway-2026-07-15", "approved_by": "admin-test"}

    def prepare(_: int) -> str:
        conn = connect()
        try:
            row = resale.prepare_opening_lot(
                conn.cursor(), owner_agent_user_id=10, points=5,
                acquisition_cost_cents=250, pricing_version="opening-v1",
                idempotency_key="opening-10-v1", evidence=evidence,
            )
            conn.commit()
            return str(row["lot_id"])
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        lot_ids = list(pool.map(prepare, range(20)))
    assert len(set(lot_ids)) == 1
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) AS c,SUM(remaining_points) AS p FROM dealer_inventory_lots")
    row = cur.fetchone()
    assert (int(row["c"]), int(row["p"])) == (1, 5)
    with pytest.raises(resale.ResaleError, match="幂等键"):
        resale.prepare_opening_lot(
            cur, owner_agent_user_id=10, points=5, acquisition_cost_cents=250,
            pricing_version="opening-v1", idempotency_key="opening-10-v1",
            evidence={"inventory_snapshot": "different"},
        )
    with pytest.raises(resale.ResaleError) as zero_cost:
        resale.prepare_opening_lot(
            cur, owner_agent_user_id=10, points=5, acquisition_cost_cents=0,
            pricing_version="opening-v2", idempotency_key="opening-zero-cost",
            evidence=evidence,
        )
    assert zero_cost.value.code == "OPENING_LOT_INPUT_INVALID"


def test_manufacturer_origin_issue_is_concurrent_idempotent(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    db_conn.commit()

    def issue(_: int) -> str:
        conn = connect()
        try:
            row = resale.issue_manufacturer_lot(
                conn.cursor(), points=20, acquisition_cost_cents=1000,
                pricing_version="factory-concurrent-v1", idempotency_key="factory-concurrent",
                evidence={"invoice": "FACTORY-CONCURRENT-1"}, issued_by=1,
            )
            conn.commit()
            return str(row["lot_id"])
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=20) as pool:
        lot_ids = list(pool.map(issue, range(20)))
    assert len(set(lot_ids)) == 1
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM dealer_manufacturer_lot_issuances")
    assert int(cur.fetchone()["c"]) == 1
    cur.execute(
        "SELECT paid_inventory_points FROM agent_inventory_wallets WHERE agent_user_id=1"
    )
    assert int(cur.fetchone()["paid_inventory_points"]) == 20


def test_redis_outage_cannot_change_pg_authoritative_quote(db_conn, monkeypatch):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 10)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    _issue_manufacturer(cur, points=1, acquisition_cost_cents=50, key="redis-outage-origin")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/15")

    terms = resale.build_quote_terms(
        cur, buyer_user_id=10, points=1, platform_reference_amount_cents=50,
        pricing_version="proc-test-v1",
    )
    assert terms is not None
    assert terms["seller_cost_basis_cents"] == 50
    assert terms["sale_amount_cents"] == 50
