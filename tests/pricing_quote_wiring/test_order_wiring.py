"""Focused PostgreSQL coverage for persisted quote → immutable order wiring.

Run only against a throwaway database whose name contains ``test``.
"""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extras
import pytest


def _connect():
    return psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@pytest.fixture(scope="module", autouse=True)
def _assert_throwaway_schema():
    assert "test" in os.environ["DATABASE_URL"].rsplit("/", 1)[-1].lower()
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT
                 EXISTS (SELECT 1 FROM information_schema.columns
                         WHERE table_name='pricing_catalog_entries' AND column_name='source_ref_jsonb') AS source_ready,
                 EXISTS (SELECT 1 FROM information_schema.columns
                         WHERE table_name='price_quotes' AND column_name='pricing_snapshot_jsonb') AS quote_ready"""
        )
        readiness = cur.fetchone()
        assert readiness["source_ready"] and readiness["quote_ready"], (
            "apply migration_pricing_quote_wiring_2026_07_14.sql to the throwaway DB first"
        )
        # The production baseline already owns these generation columns. Minimal
        # throwaway schemas used by pricing tests add them here without changing data.
        cur.execute(
            """ALTER TABLE recharge_orders
               ADD COLUMN IF NOT EXISTS agent_inventory_legacy_eligible BOOLEAN NOT NULL DEFAULT FALSE,
               ADD COLUMN IF NOT EXISTS agent_inventory_writer_generation SMALLINT"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS agent_pricing_overrides (
                   agent_user_id INTEGER PRIMARY KEY,
                   wholesale_numer INTEGER,
                   wholesale_denom INTEGER
               )"""
        )


def _enable_dual_only():
    with _connect() as conn:
        cur = conn.cursor()
        for key, value in (
            ("PRICING_DUAL_SSOT_ENABLED", "true"),
            ("CHANNEL_PRICING_ENABLED", "false"),
            ("PRICING_QUOTE_REQUIRED", "false"),
            ("CHANNEL_TIER_ENABLED", "false"),
        ):
            cur.execute(
                """INSERT INTO system_settings(key,value)
                   VALUES (%s,%s)
                   ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
                (key, value),
            )
    from config import pricing_ssot_flags
    pricing_ssot_flags.invalidate()


def _archive_open(cur, catalog_type: str, scope_key: str):
    cur.execute(
        """UPDATE pricing_catalog_versions
              SET status='archived', effective_to=NOW(), archived_at=NOW(), updated_at=NOW()
            WHERE catalog_type=%s AND scope_key=%s
              AND status='published' AND effective_to IS NULL""",
        (catalog_type, scope_key),
    )


def _publish_retail(scope_key: str, product_code: str, *, agent_id: int, buyer_points: int):
    from services.agent_pricing import resolve_agent_effective_retail_cost

    marker = uuid.uuid4().hex[:12]
    template_code = f"tpl_{marker}"
    template_id = 80_000_000 + (int(marker, 16) % 10_000_000)
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO sku_templates
                   (id,template_code,sku_type,default_name,points_granted,wholesale_cents,is_active)
               VALUES (%s,%s,'credit_pack','报价接线测试',%s,9000,TRUE)
               RETURNING id""",
            (template_id, template_code, buyer_points),
        )
        template_id = int(cur.fetchone()["id"])
        cost_context = resolve_agent_effective_retail_cost(
            cur,
            agent_user_id=int(agent_id),
            points_granted=int(buyer_points),
        )
        effective_cost = int(cost_context["effective_cost_cents"])
        source = {
            "kind": "agent_retail_sku",
            "retail_sku_id": f"RSKU-ORDER-{agent_id}",
            "retail_sku_version": 1,
            "sku_template_id": template_id,
            "override_id": 701,
            "agent_user_id": agent_id,
            "template_code": template_code,
            "sku_type": "credit_pack",
            "wholesale_cents": effective_cost,
            "effective_cost_version": str(cost_context["cost_basis_version"]),
            "retail_cents": 10000,
            "points_granted": buyer_points,
        }
        _archive_open(cur, "retail", scope_key)
        cur.execute(
            """INSERT INTO pricing_catalog_versions
                   (catalog_type,scope_key,version_code,status,calc_meta_jsonb)
               VALUES ('retail',%s,%s,'draft','{}'::jsonb)
               RETURNING id""",
            (scope_key, f"ret-{marker}"),
        )
        version_id = int(cur.fetchone()["id"])
        cur.execute(
            """INSERT INTO pricing_catalog_entries
                   (version_id,product_code,base_price_cents,multiplier_bps,
                    final_price_cents,paid_points,bonus_points,cost_floor_cents,source_ref_jsonb)
                VALUES (%s,%s,10000,10000,10000,%s,0,%s,%s::jsonb)""",
            (version_id, product_code, buyer_points, effective_cost, json.dumps(source)),
        )
        cur.execute(
            """UPDATE pricing_catalog_versions
                  SET status='published',effective_from=NOW(),published_at=NOW(),updated_at=NOW()
                WHERE id=%s""",
            (version_id,),
        )
    return template_id


def _publish_procurement(product_code: str, *, amount_cents: int, points: int):
    marker = uuid.uuid4().hex[:12]
    option = {
        "option_id": product_code,
        "amount_cents": amount_cents,
        "is_enabled": True,
        "sort_order": 0,
        "reward_eligible": False,
        "base_points": 0,
        "bonus_points": 0,
        "label": "持久化进货报价",
        "is_first_month_bonus": False,
    }
    pricing_config = {
        "wholesale_numer": 225,
        "wholesale_denom": 325,
        "agent_purchase_bonus_rate": 0,
        "bonus_validity_months": 12,
        "founding": {
            "cap": 10,
            "min_first_order_yuan": 500,
            "first_order_extra_bonus": 0,
        },
        "agent_tier_config": {},
        "agent_purchase_options": [option],
        "agent_purchase_catalog_version": f"agent-purchase-v{int(marker[:2], 16) + 1}",
    }
    source = {
        "kind": "agent_purchase_option",
        "option_id": product_code,
        "amount_cents": amount_cents,
        "option": option,
    }
    meta = {"pricing_config_snapshot": pricing_config, "source_fingerprint": marker}
    with _connect() as conn:
        cur = conn.cursor()
        _archive_open(cur, "procurement", "PLATFORM_BASE")
        cur.execute(
            """INSERT INTO pricing_catalog_versions
                   (catalog_type,scope_key,version_code,status,calc_meta_jsonb)
               VALUES ('procurement','PLATFORM_BASE',%s,'draft',%s::jsonb)
               RETURNING id""",
            (f"proc-{marker}", json.dumps(meta)),
        )
        version_id = int(cur.fetchone()["id"])
        cur.execute(
            """INSERT INTO pricing_catalog_entries
                   (version_id,product_code,base_price_cents,multiplier_bps,
                    final_price_cents,paid_points,bonus_points,source_ref_jsonb)
               VALUES (%s,%s,%s,10000,%s,%s,0,%s::jsonb)""",
            (version_id, product_code, amount_cents, amount_cents, points, json.dumps(source)),
        )
        cur.execute(
            """UPDATE pricing_catalog_versions
                  SET status='published',effective_from=NOW(),published_at=NOW(),updated_at=NOW()
                WHERE id=%s""",
            (version_id,),
        )


def test_retail_quote_becomes_authoritative_order_snapshot():
    from db.wallet_db import create_recharge_order
    from services import price_quote

    _enable_dual_only()
    buyer_id, agent_id = 71001, 71002
    scope = "SV-ABCDEFGH"
    product = f"retail-{uuid.uuid4().hex[:10]}"
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users(id, username, is_active) VALUES (%s, 'buyer', 1), (%s, 'service', 1)",
            (buyer_id, agent_id),
        )
        cur.execute(
            "INSERT INTO user_wallets(user_id, agent_level) VALUES (%s, 0), (%s, 1)",
            (buyer_id, agent_id),
        )
        cur.execute(
            "INSERT INTO public_account_codes(user_id,service_account_code) VALUES (%s,%s)",
            (agent_id, scope),
        )
        cur.execute(
            """INSERT INTO customer_agent_bindings(
                 customer_user_id,agent_user_id,binding_source,bound_at
               ) VALUES (%s,%s,'admin_manual',NOW())""",
            (buyer_id, agent_id),
        )
    template_id = _publish_retail(scope, product, agent_id=agent_id, buyer_points=13000)
    quote = price_quote.issue_quote(
        quote_type="retail",
        scope_key=scope,
        product_code=product,
        buyer_user_id=buyer_id,
    )
    order_id = f"ret-{uuid.uuid4().hex}"
    order = create_recharge_order(
        user_id=buyer_id,
        order_id=order_id,
        amount_cents=int(quote["final_price_cents"]),
        base_points=int(quote["points_granted"]),
        bonus_points=int(quote["bonus_points"]),
        payment_method="wechat",
        order_type="customer_recharge",
        agent_user_id=agent_id,
        sku_template_id=template_id,
        override_id=701,
        price_quote_id=quote["quote_id"],
        pricing_catalog_version=quote["catalog_version"],
        quote_type="retail",
        expected_product_code=product,
    )
    assert order["price_quote_id"] == quote["quote_id"]
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT pricing_snapshot_jsonb,amount_cents,base_points FROM recharge_orders WHERE id=%s",
            (order_id,),
        )
        stored = cur.fetchone()
    assert stored["pricing_snapshot_jsonb"]["amount_source"] == "sku_snapshot"
    assert stored["pricing_snapshot_jsonb"]["customer_paid_cents"] == 10000
    assert stored["pricing_snapshot_jsonb"]["wholesale_cents"] == 9000
    assert price_quote.get_quote(quote["quote_id"])["status"] == "consumed"


def test_procurement_quote_uses_agent_discount_and_is_20x_idempotent(monkeypatch):
    from api.agent_workbench_api import _create_quoted_inventory_order
    from services import agent_inventory_pricing, price_quote

    _enable_dual_only()
    dealer_id = 72001
    amount = 100000
    points = amount * 325 // 225
    product = f"apo_{uuid.uuid4().hex[:12]}"
    _publish_procurement(product, amount_cents=amount, points=points)
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users(id,username,is_active) VALUES (%s,%s,1)
               ON CONFLICT(id) DO NOTHING""",
            (dealer_id, f"dealer-{dealer_id}"),
        )
        cur.execute(
            """INSERT INTO user_wallets(user_id,agent_level) VALUES (%s,1)
               ON CONFLICT(user_id) DO UPDATE SET agent_level=1""",
            (dealer_id,),
        )
        cur.execute(
            """INSERT INTO agent_pricing_overrides(agent_user_id,wholesale_numer,wholesale_denom)
               VALUES (%s,100,200)
               ON CONFLICT(agent_user_id) DO UPDATE
               SET wholesale_numer=EXCLUDED.wholesale_numer,
                   wholesale_denom=EXCLUDED.wholesale_denom,
                   updated_at=NOW()""",
            (dealer_id,),
        )
    quote = price_quote.issue_procurement_quote(
        dealer_id=dealer_id,
        product_code=product,
        idempotency_key=f"preview-{uuid.uuid4().hex}",
    )
    assert int(quote["final_price_cents"]) == amount
    assert int(quote["points_granted"]) == amount * 200 // 100
    quote_snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    assert quote_snapshot["discount_source"] == "agent_override"
    assert (
        quote_snapshot["purchase_discount_snapshot"]["numer"],
        quote_snapshot["purchase_discount_snapshot"]["denom"],
    ) == (100, 200)
    monkeypatch.setattr(
        agent_inventory_pricing,
        "inventory_snapshot_activation_state",
        lambda _cur: {"ready": True},
    )
    idem = f"order-{uuid.uuid4().hex}"

    def create_once(index: int):
        return _create_quoted_inventory_order(
            agent_user_id=dealer_id,
            quote_id=quote["quote_id"],
            order_id=f"proc-{index}-{uuid.uuid4().hex}",
            idempotency_key=idem,
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(create_once, range(20)))
    order_ids = {result[0] for result in results}
    assert len(order_ids) == 1
    assert sum(1 for result in results if result[2] is False) == 1
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM recharge_orders WHERE user_id=%s AND idempotency_key=%s",
            (dealer_id, idem),
        )
        assert int(cur.fetchone()["n"]) == 1
    stored_quote = price_quote.get_quote(quote["quote_id"])
    assert stored_quote["status"] == "consumed"
    assert stored_quote["used_order_id"] in order_ids
