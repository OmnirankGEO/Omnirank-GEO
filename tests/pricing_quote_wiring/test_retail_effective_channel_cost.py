"""Discriminating PostgreSQL tests for provider retail effective-channel cost SSOT."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi import HTTPException, Request

from api.agent_workbench_api import agent_pricing_skus
from api.pricing_ssot_api import retail_catalog
from db.connection import get_db
from services import (
    account_codes,
    agent_pricing,
    channel_pricing,
    price_quote,
    pricing_publication,
)
from services.price_quote import QuoteError

from helpers import publish_procurement, set_flags


def _request(user_id: int, *, is_admin: bool = False) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.user = {"user_id": int(user_id), "is_admin": is_admin}
    return request


def _set_unit_platform_cost() -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO system_settings(key,value,value_type,description)
               VALUES ('pricing_config',%s,'json','unit cost test')
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()""",
            (json.dumps({"wholesale_numer": 1, "wholesale_denom": 1}),),
        )
        conn.commit()


@pytest.mark.parametrize("admin_agent_level", [0, 1])
def test_admin_patrol_uses_platform_direct_subject_not_personal_account(
    monkeypatch,
    admin_agent_level,
):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "100")
    _set_unit_platform_cost()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_wallets SET agent_level=%s WHERE user_id=400",
            (admin_agent_level,),
        )
        cur.execute(
            "INSERT INTO public_account_codes(user_id,service_account_code) "
            "VALUES (100,'SV-ABCDEFGH')",
        )
        agent_pricing.create_agent_sku_override(
            cur,
            agent_user_id=100,
            points_granted=100,
            retail_cents=200,
            custom_name="平台直营巡检包",
            client_request_id="admin-platform-direct-patrol",
        )
        conn.commit()

    payload = asyncio.run(agent_pricing_skus(_request(400, is_admin=True))).model_dump()
    assert [item["display_name"] for item in payload["items"]] == ["平台直营巡检包"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM agent_sku_overrides WHERE agent_user_id=400")
        assert int(cur.fetchone()["c"]) == 0


def test_admin_patrol_fails_closed_when_platform_direct_identity_is_unavailable(monkeypatch):
    monkeypatch.delenv("PLATFORM_DIRECT_SERVICE_USER_ID", raising=False)
    with pytest.raises(HTTPException) as denied:
        asyncio.run(agent_pricing_skus(_request(400, is_admin=True)))
    assert denied.value.status_code == 503
    assert denied.value.detail["code"] == "ADMIN_OPERATING_CONTEXT_UNAVAILABLE"


def test_ordinary_account_still_cannot_enter_provider_workbench():
    with pytest.raises(HTTPException) as denied:
        asyncio.run(agent_pricing_skus(_request(400)))
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "AGENT_IDENTITY_REQUIRED"


def test_admin_retail_publication_keeps_subject_and_operator_distinct(monkeypatch):
    from api import agent_workbench_api
    from config import pricing_ssot_flags

    observed = {}
    monkeypatch.setattr(
        pricing_ssot_flags,
        "pricing_flags_snapshot",
        lambda cur: {"PRICING_DUAL_SSOT_ENABLED": True},
    )

    def _capture(cur, *, agent_user_id, actor_id, reason):
        observed.update(
            agent_user_id=agent_user_id,
            actor_id=actor_id,
            reason=reason,
        )
        return {"version_code": "retail-platform-direct-v1"}

    monkeypatch.setattr(pricing_publication, "materialize_retail_catalog", _capture)
    result = agent_workbench_api._publish_retail_after_agent_mutation(
        None,
        agent_user_id=100,
        actor_id=400,
        reason="admin patrol update",
    )

    assert result["version_code"] == "retail-platform-direct-v1"
    assert observed == {
        "agent_user_id": 100,
        "actor_id": 400,
        "reason": "admin patrol update",
    }


def _seed_chain(*, depth: int) -> None:
    _set_unit_platform_cost()
    if depth >= 1:
        channel_pricing.create_relationship(
            buyer_dealer_id=100,
            upstream_channel_account_id=200,
            relationship_version="cost-100-v1",
            cost_multiplier_bps=12000,
        )
    if depth >= 2:
        channel_pricing.create_relationship(
            buyer_dealer_id=200,
            upstream_channel_account_id=300,
            relationship_version="cost-200-v1",
            cost_multiplier_bps=12000,
        )


def _preview(*, retail_cents: int = 200) -> dict:
    with get_db() as conn:
        return agent_pricing.preview_agent_retail_sku(
            conn.cursor(),
            agent_user_id=100,
            points_granted=100,
            retail_cents=int(retail_cents),
        )


def test_one_and_two_level_effective_cost_use_channel_resolver():
    _seed_chain(depth=1)
    one = _preview()
    assert one["estimated_cost_cents"] == 120
    assert one["estimated_profit_cents"] == 80

    channel_pricing.create_relationship(
        buyer_dealer_id=200,
        upstream_channel_account_id=300,
        relationship_version="cost-200-v1",
        cost_multiplier_bps=12000,
    )
    two = _preview()
    assert two["estimated_cost_cents"] == 144
    assert two["estimated_profit_cents"] == 56
    assert set(two) == {
        "points_granted", "retail_cents", "estimated_cost_cents",
        "estimated_profit_cents", "margin_label", "margin_action",
        "is_loss", "publishable", "estimate_basis",
    }


def test_each_channel_hop_rounds_up_in_integer_cents():
    _set_unit_platform_cost()
    channel_pricing.create_relationship(
        buyer_dealer_id=100,
        upstream_channel_account_id=200,
        relationship_version="round-near-v1",
        cost_multiplier_bps=12000,
    )
    channel_pricing.create_relationship(
        buyer_dealer_id=200,
        upstream_channel_account_id=300,
        relationship_version="round-far-v1",
        cost_multiplier_bps=12000,
    )
    # ceil(101 * 1.20) = 122; ceil(122 * 1.20) = 147.
    with get_db() as conn:
        result = agent_pricing.resolve_agent_effective_retail_cost(
            conn.cursor(), agent_user_id=100, points_granted=101,
        )
    assert result["effective_cost_cents"] == 147


@pytest.mark.parametrize("retail_cents", [119, 120])
def test_create_at_or_below_effective_cost_rejects_with_zero_write(retail_cents: int):
    _seed_chain(depth=1)
    with get_db() as conn:
        cur = conn.cursor()
        with pytest.raises(ValueError, match="必须高于当前有效成本"):
            agent_pricing.create_agent_sku_override(
                cur,
                agent_user_id=100,
                points_granted=100,
                retail_cents=retail_cents,
                custom_name="不得写入",
                client_request_id=f"floor-reject-{retail_cents}",
            )
        cur.execute("SELECT COUNT(*) AS c FROM agent_sku_overrides WHERE agent_user_id=100")
        assert int(cur.fetchone()["c"]) == 0


def test_apply_markup_uses_effective_cost_not_platform_base():
    _seed_chain(depth=1)
    with get_db() as conn:
        cur = conn.cursor()
        created = agent_pricing.create_agent_sku_override(
            cur,
            agent_user_id=100,
            points_granted=100,
            retail_cents=200,
            custom_name="渠道成本加价包",
            client_request_id="effective-markup-create",
        )
        result = agent_pricing.apply_markup_for_agent(cur, 100, 15000)
        cur.execute("SELECT retail_cents FROM agent_sku_overrides WHERE id=%s", (created["id"],))
        assert int(cur.fetchone()["retail_cents"]) == 180
        assert result["updated"][0]["retail_cents"] == 180


@pytest.mark.parametrize("retail_cents", [119, 120])
def test_edit_at_or_below_effective_cost_rejects_without_mutation(retail_cents: int):
    _seed_chain(depth=1)
    with get_db() as conn:
        cur = conn.cursor()
        created = agent_pricing.create_agent_sku_override(
            cur,
            agent_user_id=100,
            points_granted=100,
            retail_cents=200,
            custom_name="不可被亏本编辑覆盖",
            client_request_id=f"edit-floor-{retail_cents}",
        )
        conn.commit()
        with pytest.raises(ValueError, match="必须高于当前有效成本"):
            agent_pricing.update_agent_sku_override(
                cur,
                agent_user_id=100,
                override_id=int(created["id"]),
                retail_cents=retail_cents,
                points_granted=100,
                expected_version=int(created["version"]),
            )
        conn.rollback()
        cur.execute(
            "SELECT retail_cents,version FROM agent_sku_overrides WHERE id=%s",
            (created["id"],),
        )
        assert dict(cur.fetchone()) == {"retail_cents": 200, "version": 1}


def test_retail_publication_rejects_zero_margin_without_new_catalog():
    _seed_chain(depth=1)
    account_codes.prepare_account_codes(service_user_ids=[100], channel_user_ids=[])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO agent_sku_overrides
                   (agent_user_id,retail_sku_id,points_granted,custom_name,
                    retail_cents,is_active,sort_order,version,client_request_id)
               VALUES (100,'RSKU-PUBLISHFLOOR1',100,'零利润禁止发布',
                       120,TRUE,0,1,'publish-floor-zero')"""
        )
        with pytest.raises(
            pricing_publication.PricingPublicationError,
            match="售价必须高于当前有效成本",
        ):
            pricing_publication.materialize_retail_catalog(
                cur, agent_user_id=100, actor_id=100, reason="reject zero margin",
            )
        conn.rollback()
        cur.execute(
            "SELECT COUNT(*) AS c FROM pricing_catalog_versions WHERE catalog_type='retail'"
        )
        assert int(cur.fetchone()["c"]) == 0


def test_cost_resolution_holds_graph_lock_until_transaction_commit():
    _seed_chain(depth=1)
    reader_ready = threading.Event()
    allow_reader_commit = threading.Event()
    writer_done = threading.Event()
    failure: list[BaseException] = []

    def hold_cost_snapshot() -> None:
        try:
            with get_db() as conn:
                resolved = agent_pricing.resolve_agent_effective_retail_cost(
                    conn.cursor(), agent_user_id=100, points_granted=100,
                )
                assert resolved["effective_cost_cents"] == 120
                reader_ready.set()
                assert allow_reader_commit.wait(timeout=5)
                conn.commit()
        except BaseException as exc:  # pragma: no cover - thread relay
            failure.append(exc)
            reader_ready.set()

    def change_relationship() -> None:
        try:
            assert reader_ready.wait(timeout=5)
            channel_pricing.save_relationships([{
                "buyer_dealer_id": 100,
                "upstream_channel_account_id": 200,
                "expected_relationship_version": "cost-100-v1",
                "new_relationship_version": "cost-100-v2",
                "cost_multiplier_bps": 13000,
                "reason": "concurrency discriminator",
            }], validate_nodes=False)
            writer_done.set()
        except BaseException as exc:  # pragma: no cover - thread relay
            failure.append(exc)
            writer_done.set()

    reader = threading.Thread(target=hold_cost_snapshot, name="retail-cost-reader")
    writer = threading.Thread(target=change_relationship, name="channel-cost-writer")
    reader.start()
    writer.start()
    assert reader_ready.wait(timeout=5)
    time.sleep(0.2)
    assert not writer_done.is_set(), "relationship write crossed the quote cost snapshot"
    allow_reader_commit.set()
    reader.join(timeout=5)
    writer.join(timeout=5)
    assert not reader.is_alive() and not writer.is_alive()
    assert failure == []
    with get_db() as conn:
        current = agent_pricing.resolve_agent_effective_retail_cost(
            conn.cursor(), agent_user_id=100, points_granted=100,
        )
    assert current["effective_cost_cents"] == 130


def _publish_provider_retail() -> tuple[str, dict]:
    account_codes.prepare_account_codes(
        service_user_ids=[100], channel_user_ids=[]
    )
    service_code = account_codes.get_service_code(100)
    assert service_code
    with get_db() as conn:
        cur = conn.cursor()
        agent_pricing.create_agent_sku_override(
            cur,
            agent_user_id=100,
            points_granted=100,
            retail_cents=200,
            custom_name="不可过期成交包",
            client_request_id="stale-catalog-create",
        )
        published = pricing_publication.materialize_retail_catalog(
            cur,
            agent_user_id=100,
            actor_id=100,
            reason="effective channel cost acceptance",
        )
        conn.commit()
    return service_code, published


def _consume_into_order(quote: dict, *, order_id: str, payment_status: str) -> dict:
    order_snapshot = price_quote.quote_order_pricing_snapshot(quote, required=True)
    with get_db() as conn:
        cur = conn.cursor()
        locked = price_quote.lock_and_validate(
            cur,
            quote["quote_id"],
            buyer_user_id=400,
            quote_type="retail",
        )
        cur.execute(
            """INSERT INTO recharge_orders
                   (id,user_id,amount_cents,base_points,bonus_points,payment_status,
                    order_type,agent_user_id,pricing_snapshot_jsonb)
               VALUES (%s,400,%s,%s,%s,%s,'retail_sku',100,%s::jsonb)""",
            (
                order_id,
                int(locked["final_price_cents"]),
                int(locked["points_granted"]),
                int(locked["bonus_points"]),
                payment_status,
                json.dumps(order_snapshot),
            ),
        )
        assert price_quote.consume_quote(cur, quote["quote_id"], order_id) is True
        conn.commit()
    return order_snapshot


def test_relationship_change_invalidates_catalog_and_unconsumed_quote_only():
    _seed_chain(depth=2)
    service_code, _ = _publish_provider_retail()
    set_flags(dual=True, quote_required=False, channel=True)
    with get_db() as conn:
        conn.cursor().execute(
            """INSERT INTO customer_agent_bindings
                   (customer_user_id,agent_user_id,binding_source,bound_at)
               VALUES (400,100,'admin_manual',NOW())"""
        )
        conn.commit()

    # Product codes are opaque by design; read the published code internally.
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT e.product_code FROM pricing_catalog_entries e
               JOIN pricing_catalog_versions v ON v.id=e.version_id
               WHERE v.catalog_type='retail' AND v.scope_key=%s
                 AND v.status='published' AND v.effective_to IS NULL""",
            (service_code,),
        )
        product_code = str(cur.fetchone()["product_code"])

    stale_quote = price_quote.issue_quote(
        quote_type="retail", scope_key=service_code, product_code=product_code,
        buyer_user_id=400,
    )
    pending_quote = price_quote.issue_quote(
        quote_type="retail", scope_key=service_code, product_code=product_code,
        buyer_user_id=400, idempotency_key="pending-snapshot",
    )
    paid_quote = price_quote.issue_quote(
        quote_type="retail", scope_key=service_code, product_code=product_code,
        buyer_user_id=400, idempotency_key="paid-snapshot",
    )
    pending_snapshot = _consume_into_order(
        pending_quote, order_id="pending-effective-cost", payment_status="pending"
    )
    paid_snapshot = _consume_into_order(
        paid_quote, order_id="paid-effective-cost", payment_status="paid"
    )

    public_before = asyncio.run(retail_catalog(_request(400)))
    assert public_before["data"]["items"][0]["final_price_cents"] == 200
    encoded_before = json.dumps(public_before, ensure_ascii=False, default=str).lower()
    for forbidden in (
        "wholesale", "cost_floor", "platform_base", "upstream",
        "relationship_version", "cost_multiplier_bps", "channel_account",
    ):
        assert forbidden not in encoded_before

    channel_pricing.create_relationship(
        buyer_dealer_id=100,
        upstream_channel_account_id=200,
        relationship_version="cost-100-v2",
        cost_multiplier_bps=13000,
    )

    with pytest.raises(QuoteError, match="成本已变化"):
        price_quote.issue_quote(
            quote_type="retail", scope_key=service_code, product_code=product_code,
            buyer_user_id=400,
        )
    with get_db() as conn:
        with pytest.raises(QuoteError, match="成本已变化"):
            price_quote.lock_and_validate(
                conn.cursor(), stale_quote["quote_id"],
                buyer_user_id=400, quote_type="retail",
            )
        conn.rollback()

    with pytest.raises(HTTPException) as stale_catalog:
        asyncio.run(retail_catalog(_request(400)))
    assert stale_catalog.value.status_code == 409
    assert stale_catalog.value.detail == {
        "code": "PRICE_CONFIGURATION_UNAVAILABLE",
        "message": "当前账户价格配置暂不可用，请稍后重试",
    }

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,payment_status,pricing_snapshot_jsonb FROM recharge_orders "
            "WHERE id IN ('pending-effective-cost','paid-effective-cost') ORDER BY id"
        )
        rows = {row["id"]: dict(row) for row in cur.fetchall()}
    assert rows["pending-effective-cost"]["payment_status"] == "pending"
    assert rows["pending-effective-cost"]["pricing_snapshot_jsonb"] == pending_snapshot
    assert rows["paid-effective-cost"]["payment_status"] == "paid"
    assert rows["paid-effective-cost"]["pricing_snapshot_jsonb"] == paid_snapshot


def test_actual_procurement_quote_and_provider_dto_share_effective_cost_without_leakage():
    _seed_chain(depth=2)
    account_codes.prepare_account_codes(
        service_user_ids=[100, 200, 300], channel_user_ids=[200]
    )
    publish_procurement(version_code="proc-v1", amount_cents=100, points=100)
    set_flags(dual=True, quote_required=False, channel=True)
    procurement = price_quote.issue_procurement_quote(
        dealer_id=100,
        product_code="credit_basic",
    )
    assert int(procurement["final_price_cents"]) == 100
    assert int(procurement["points_granted"]) == 69
    procurement_snapshot = price_quote.quote_order_pricing_snapshot(procurement, required=True)
    assert procurement_snapshot["cash_anchor_semantic_version"] == "procurement-cash-anchor-v2"

    with get_db() as conn:
        agent_pricing.create_agent_sku_override(
            conn.cursor(),
            agent_user_id=100,
            points_granted=100,
            retail_cents=200,
            custom_name="隐私成本包",
            client_request_id="provider-privacy-cost",
        )
        conn.commit()
    response = asyncio.run(agent_pricing_skus(_request(100)))
    payload = response.model_dump()
    assert payload["items"][0]["wholesale_cents"] == 144
    encoded = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in (
        "platform_base", "upstream", "relationship_version",
        "cost_multiplier_bps", "beneficiary", "channel_account",
        "suggested_retail_cents",
    ):
        assert forbidden not in encoded
