"""Acceptance coverage for template-independent provider retail SKUs.

These cases intentionally cross API, publication, quote, order and callback
boundaries.  A green model-only test would not prove the money/inventory chain.
"""

from __future__ import annotations

from tests.pricing_quote_wiring._single_ledger import (
    assert_single_ledger_settlement,
    read_settlement_facts,
)
import asyncio
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extras
import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from api.agent_workbench_api import (
    agent_pricing_sku_create,
    agent_pricing_sku_delete,
    agent_pricing_sku_save,
)
from api.customer_workbench_api import customer_recharge_skus
from api.pricing_ssot_api import retail_catalog
from db.connection import get_db
from db.wallet_db import complete_recharge, create_recharge_order
from schemas.v35_w2_dto import AgentSKUCreateRequest, AgentSKUUpdateRequest
from services import account_codes, agent_pricing, price_quote, pricing_catalog
from services.agent_retail_sku_schema import schema_status

from helpers import set_flags


def _request(user_id: int, *, is_admin: bool = False) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.user = {"user_id": user_id, "is_admin": is_admin}
    return request


def _create_payload(*, request_id: str = "retail-create-12000", points: int = 12000,
                    cents: int = 10000) -> AgentSKUCreateRequest:
    return AgentSKUCreateRequest(
        client_request_id=request_id,
        display_name="自定义 12000 算力包",
        subtitle="独立零售规格",
        extra_promo_text="按需购买，不依附平台进货档位",
        scene="GEO 内容生产",
        points_granted=points,
        retail_cents=cents,
        is_active=True,
        sort_order=7,
    )


def _create_service_sku(agent_user_id: int, *, request_id: str, points: int = 12000,
                        cents: int = 10000) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        result = agent_pricing.create_agent_sku_override(
            cur,
            agent_user_id=agent_user_id,
            points_granted=points,
            retail_cents=cents,
            client_request_id=request_id,
            custom_name=f"自定义 {points} 算力包",
            custom_subtitle="独立零售规格",
            custom_sales_pitch="模板不是身份",
            custom_scene="GEO",
            is_active=True,
            sort_order=7,
        )
        conn.commit()
        return result


def test_provider_creates_12000_point_package_without_platform_template():
    response = asyncio.run(agent_pricing_sku_create(_create_payload(), _request(100)))
    assert response["success"] is True
    assert response["points_granted"] == 12000

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT retail_sku_id,sku_template_id,source_template_id,points_granted,
                      retail_cents,custom_name,version
                 FROM agent_sku_overrides WHERE id=%s""",
            (response["id"],),
        )
        row = dict(cur.fetchone())
    assert row["retail_sku_id"] == response["retail_sku_id"]
    assert row["sku_template_id"] is None
    assert row["source_template_id"] is None
    assert row["points_granted"] == 12000
    assert row["retail_cents"] == 10000
    assert row["custom_name"] == "自定义 12000 算力包"
    assert row["version"] == 1


def test_non_provider_cannot_create_and_cross_provider_update_is_not_found():
    with pytest.raises(HTTPException) as denied:
        asyncio.run(agent_pricing_sku_create(_create_payload(), _request(400)))
    assert denied.value.status_code == 403

    created = _create_service_sku(200, request_id="owner-200-create")
    update = AgentSKUUpdateRequest(
        version=created["version"],
        display_name="越权修改",
        points_granted=12000,
        retail_cents=11000,
    )
    with pytest.raises(HTTPException) as hidden:
        asyncio.run(agent_pricing_sku_save(created["id"], update, _request(100)))
    assert hidden.value.status_code == 404


@pytest.mark.parametrize("bad_points", [0, -1, 1.5, True, 9_000_000_000_000_001])
def test_invalid_points_are_rejected_before_any_write(bad_points):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM agent_sku_overrides")
        before = int(cur.fetchone()["n"])
    with pytest.raises(ValidationError):
        AgentSKUCreateRequest(
            client_request_id="invalid-points-request",
            display_name="非法算力",
            points_granted=bad_points,
            retail_cents=10000,
        )
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM agent_sku_overrides")
        assert int(cur.fetchone()["n"]) == before


def test_money_is_strict_integer_cents_and_internal_fields_are_forbidden():
    with pytest.raises(ValidationError):
        AgentSKUCreateRequest(
            client_request_id="float-money-request",
            display_name="浮点金额",
            points_granted=12000,
            retail_cents=100.5,
        )
    with pytest.raises(ValidationError):
        AgentSKUCreateRequest(
            client_request_id="forged-cost-request",
            display_name="伪造成本",
            points_granted=12000,
            retail_cents=10000,
            acquisition_cost_cents=1,
            upstream_user_id=999,
            settlement_cents=999999,
        )


def test_twenty_concurrent_replays_create_one_canonical_row():
    def create_once(_index: int) -> dict:
        return _create_service_sku(100, request_id="same-client-request-20x")

    with ThreadPoolExecutor(max_workers=20) as pool:
        rows = list(pool.map(create_once, range(20)))
    assert len({int(row["id"]) for row in rows}) == 1
    assert sum(bool(row.get("idempotent_replay")) for row in rows) == 19
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM agent_sku_overrides "
            "WHERE agent_user_id=100 AND client_request_id='same-client-request-20x'"
        )
        assert int(cur.fetchone()["n"]) == 1


def test_stale_version_update_returns_409_and_does_not_overwrite_winner():
    created = _create_service_sku(100, request_id="occ-create-request")
    first = AgentSKUUpdateRequest(
        version=1, display_name="赢家", points_granted=7150, retail_cents=7000,
    )
    winner = asyncio.run(agent_pricing_sku_save(created["id"], first, _request(100)))
    assert winner["version"] == 2
    stale = AgentSKUUpdateRequest(
        version=1, display_name="输家", points_granted=130, retail_cents=1000,
    )
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(agent_pricing_sku_save(created["id"], stale, _request(100)))
    assert conflict.value.status_code == 409
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT custom_name,points_granted,retail_cents,version "
            "FROM agent_sku_overrides WHERE id=%s", (created["id"],),
        )
        assert dict(cur.fetchone()) == {
            "custom_name": "赢家", "points_granted": 7150,
            "retail_cents": 7000, "version": 2,
        }


def test_tombstone_hides_package_and_never_falls_back_to_platform_template():
    created = _create_service_sku(100, request_id="delete-no-fallback")
    account_codes.prepare_account_codes(service_user_ids=[100], channel_user_ids=[])
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO customer_agent_bindings
                   (customer_user_id,agent_user_id,binding_source,bound_at)
               VALUES (400,100,'admin_manual',NOW())"""
        )
    before = asyncio.run(customer_recharge_skus(_request(400)))
    assert [item["retail_sku_id"] for item in before["items"]] == [created["retail_sku_id"]]

    deleted = asyncio.run(
        agent_pricing_sku_delete(created["id"], _request(100), version=created["version"])
    )
    assert deleted["action"] == "soft_deleted"
    after = asyncio.run(customer_recharge_skus(_request(400)))
    assert after["items"] == []
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT deleted_at,is_active FROM agent_sku_overrides WHERE id=%s", (created["id"],))
        tombstone = dict(cur.fetchone())
    assert tombstone["deleted_at"] is not None
    assert tombstone["is_active"] is False


def test_legacy_template_package_is_losslessly_materialized_once():
    with get_db() as conn:
        cur = conn.cursor()
        result = agent_pricing.create_agent_sku_override(
            cur,
            agent_user_id=100,
            sku_template_id=1,
            retail_cents=180000,
            client_request_id="legacy-template-create",
        )
        conn.commit()
        cur.execute(
            """SELECT sku_template_id,source_template_id,points_granted,custom_name,
                      retail_cents,is_active,deleted_at
                 FROM agent_sku_overrides WHERE id=%s""", (result["id"],),
        )
        row = dict(cur.fetchone())
    assert row == {
        "sku_template_id": 1,
        "source_template_id": 1,
        "points_granted": 195000,
        "custom_name": "基础算力包",
        "retail_cents": 180000,
        "is_active": True,
        "deleted_at": None,
    }


def test_custom_catalog_quote_pending_order_and_callback_keep_original_snapshot():
    prepared = account_codes.prepare_account_codes(service_user_ids=[100], channel_user_ids=[])
    scope = next(
        row["code"] for row in prepared["prepared"]
        if row["kind"] == "service" and int(row["user_id"]) == 100
    )
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO customer_agent_bindings
                   (customer_user_id,agent_user_id,binding_source,bound_at)
               VALUES (400,100,'admin_manual',NOW())"""
        )
    set_flags(dual=True, quote_required=False, channel=False)
    created = asyncio.run(agent_pricing_sku_create(_create_payload(), _request(100)))

    published = pricing_catalog.get_published_catalog("retail", scope)
    assert published is not None and len(published["items"]) == 1
    entry = published["items"][0]
    assert entry["paid_points"] == 12000
    assert entry["final_price_cents"] == 10000
    source = entry["source_ref_jsonb"]
    if isinstance(source, str):
        source = json.loads(source)
    assert source["retail_sku_id"] == created["retail_sku_id"]
    assert source["retail_sku_version"] == 1
    assert source["source_template_id"] is None

    quote = price_quote.issue_quote(
        quote_type="retail", scope_key=scope,
        product_code=entry["product_code"], buyer_user_id=400,
    )
    order = create_recharge_order(
        user_id=400,
        order_id="custom-retail-12000-order",
        amount_cents=10000,
        base_points=12000,
        bonus_points=0,
        payment_method="wechat_native",
        order_type="customer_recharge",
        agent_user_id=100,
        sku_template_id=None,
        override_id=created["id"],
        binding_source="admin_manual",
        price_quote_id=quote["quote_id"],
        pricing_catalog_version=quote["catalog_version"],
        quote_type="retail",
        expected_product_code=entry["product_code"],
    )
    locked = order["pricing_snapshot_jsonb"]
    assert locked["retail_sku_id"] == created["retail_sku_id"]
    assert locked["retail_sku_version"] == 1
    assert locked["points_granted"] == 12000
    assert locked["buyer_paid_cents"] == 10000
    assert locked["sku_template_id"] is None

    update = AgentSKUUpdateRequest(
        version=1,
        display_name="改价后的 7150 包",
        points_granted=7150,
        retail_cents=8000,
        is_active=True,
    )
    assert asyncio.run(agent_pricing_sku_save(created["id"], update, _request(100)))["version"] == 2

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT pricing_snapshot_jsonb FROM recharge_orders WHERE id='custom-retail-12000-order'"
        )
        persisted = cur.fetchone()["pricing_snapshot_jsonb"]
    assert persisted["retail_sku_version"] == 1
    assert persisted["points_granted"] == 12000
    assert persisted["buyer_paid_cents"] == 10000

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(
            lambda i: complete_recharge("custom-retail-12000-order", f"wx-custom-{i}"),
            range(20),
        ))
    assert all(result is not None for result in results)
    with get_db() as conn:
        cur = conn.cursor()
        # 🔴 [#118 1-8] 继任 `2aef3b59b`(2026-07-27 单账本收敛):
        #    客户算力**留在 user_wallets**,不再复制进 customer_agent_credit_wallets。
        #    原断言钉的是收敛前的双账本模型(本文件最后改动 2026-07-18,收敛后 0 笔)。
        #    三条不变量在 `_single_ledger` **一处实现** —— 八个调用点各写一遍必有几处漂。
        facts = read_settlement_facts(
            cur, order_id='custom-retail-12000-order', customer_user_id=400)
        assert_single_ledger_settlement(facts)
        cur.execute(
            "SELECT COUNT(*) AS n FROM agent_revenue_ledger "
            "WHERE recharge_order_id='custom-retail-12000-order'"
        )
        assert int(cur.fetchone()["n"]) == 1

    public = asyncio.run(retail_catalog(_request(400)))
    public_json = json.dumps(public, ensure_ascii=False, default=str)
    for forbidden in (
        "source_template_id", "sku_template_id", "upstream", "cost_floor",
        "wholesale", "margin", "multiplier", "relationship", "channel_account",
    ):
        assert forbidden not in public_json.lower()


def test_schema_readiness_rejects_weakened_constraint_wrong_index_and_type(raw_conn):
    cur = raw_conn.cursor()
    assert schema_status(cur) == {"ready": True, "blockers": []}

    cur.execute("ALTER TABLE agent_sku_overrides DROP CONSTRAINT chk_agent_retail_sku_money")
    cur.execute(
        "ALTER TABLE agent_sku_overrides ADD CONSTRAINT chk_agent_retail_sku_money "
        "CHECK (retail_cents > 0)"
    )
    weakened = schema_status(cur)
    assert weakened["ready"] is False
    assert "约束 chk_agent_retail_sku_money 定义不匹配" in weakened["blockers"]
    raw_conn.rollback()

    cur = raw_conn.cursor()
    cur.execute("DROP INDEX ux_agent_retail_sku_client_request")
    cur.execute(
        "CREATE UNIQUE INDEX ux_agent_retail_sku_client_request "
        "ON agent_sku_overrides (client_request_id, agent_user_id) "
        "WHERE client_request_id IS NOT NULL"
    )
    wrong_index = schema_status(cur)
    assert wrong_index["ready"] is False
    assert "索引 ux_agent_retail_sku_client_request 列或顺序错误" in wrong_index["blockers"]
    raw_conn.rollback()

    cur = raw_conn.cursor()
    cur.execute("ALTER TABLE agent_sku_overrides ALTER COLUMN points_granted TYPE INTEGER")
    wrong_type = schema_status(cur)
    assert wrong_type["ready"] is False
    assert "agent_sku_overrides.points_granted 类型应为 bigint" in wrong_type["blockers"]
    raw_conn.rollback()

    cur = raw_conn.cursor()
    cur.execute(
        "INSERT INTO agent_sku_overrides "
        "(agent_user_id, retail_sku_id, points_granted, custom_name, retail_cents, "
        "is_active, sort_order, version) "
        "VALUES (100, 'RSKU-NULLSTATE-12345678', 12000, 'null-state', 10000, NULL, 0, 1)"
    )
    null_state = schema_status(cur)
    assert null_state["ready"] is False
    assert "agent_sku_overrides 存在非法 canonical 零售 SKU" in null_state["blockers"]
    raw_conn.rollback()


def test_migration_twice_rollback_forward_and_template_free_rollback_guard():
    root = Path(__file__).resolve().parents[2]
    migration = (root / "scripts" / "migration_agent_retail_sku_decoupling_2026_07_17.sql").read_text(
        encoding="utf-8"
    )
    rollback = (root / "scripts" / "rollback_agent_retail_sku_decoupling_2026_07_17.sql").read_text(
        encoding="utf-8"
    )
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(rollback)
        cur.execute(migration)
        cur.execute(migration)
        assert schema_status(cur) == {"ready": True, "blockers": []}
    finally:
        conn.close()

    created = _create_service_sku(100, request_id="rollback-guard-custom")
    assert created["retail_sku_id"].startswith("RSKU-")
    guard_conn = psycopg2.connect(os.environ["DATABASE_URL"])
    guard_conn.autocommit = True
    try:
        with pytest.raises(psycopg2.Error, match="rollback blocked"):
            guard_conn.cursor().execute(rollback)
        guard_conn.cursor().execute("ROLLBACK")
        cur = guard_conn.cursor()
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='agent_sku_overrides' "
            "AND column_name='sku_template_id'"
        )
        assert cur.fetchone()[0] == "YES"
    finally:
        guard_conn.close()
