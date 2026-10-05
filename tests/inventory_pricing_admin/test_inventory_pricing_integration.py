"""专用 admin API、下单快照、支付幂等与跨进程版本一致性。"""

import asyncio
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from api.admin_factory_api import (
    AgentPricingOverrideRequest,
    GlobalPricingConfigRequest,
    InventoryCatalogItemRequest,
    InventoryCatalogPutRequest,
    admin_get_inventory_purchase_catalog,
    admin_put_agent_pricing_override,
    admin_put_global_pricing_config,
    admin_put_inventory_purchase_catalog,
)
from api.admin_api import (
    ChannelTierConfigRequest,
    SetPurchasePricingOverrideRequest,
    admin_get_channel_tier_config,
    admin_put_channel_tier_config,
    admin_set_purchase_pricing_override,
)
from api.agent_workbench_api import agent_inventory_purchase
from api.agent_workbench_api import agent_inventory_purchase_preview
from api.agent_workbench_api import agent_inventory_purchase_options
from schemas.v35_w2_dto import AgentPurchasePreviewRequest, AgentPurchaseRequest


def request_for(user, *, method="GET", user_agent="MicroMessenger"):
    scope = {
        "type": "http", "method": method, "path": "/", "query_string": b"",
        "headers": [(b"user-agent", user_agent.encode())],
        "client": ("127.0.0.1", 43210), "server": ("test", 80), "scheme": "http",
    }
    request = Request(scope)
    request.state.user = user
    request.state.request_id = "req-inventory-test"
    return request


def db_row(sql, params=()):
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def activate_snapshot_cutover():
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "activate_agent_inventory_snapshot_cutover_2026_07_14.sql"
    ).read_text(encoding="utf-8")
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_settings(key,value,value_type,description,updated_at) "
                "SELECT 'AGENT_INVENTORY_SNAPSHOT_HOT_ROLLBACK_READY',"
                "'agent-inventory-snapshot-v3|sha256:test|sha256:test',"
                "'deploy_capability','test evidence',clock_timestamp() "
                "WHERE NOT EXISTS (SELECT 1 FROM system_settings "
                "WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT') "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at"
            )
            cur.execute(script)


def finalized_snapshot(snapshot):
    from services.agent_inventory_pricing import finalize_quote_snapshot

    result = dict(snapshot)
    result.setdefault("option_source", "catalog")
    result.setdefault("reward_eligible", True)
    result.setdefault("founder_seat_policy", "payment_time_atomic_remaining_seat")
    result.setdefault("founder_cap_snapshot", 10)
    result.setdefault("rolling_before_yuan_snapshot", "0")
    result.setdefault(
        "projected_rolling_12m_yuan_snapshot",
        str(Decimal(int(result["amount_cents"])) / Decimal(100)),
    )
    result.setdefault("tier_override_until_snapshot", None)
    return finalize_quote_snapshot(result, agent_user_id=7)


def put_payload(response, *, disable_first=False):
    options = []
    for index, row in enumerate(response["options"]):
        options.append({
            "option_id": row["option_id"],
            "amount_cents": row["amount_cents"],
            "is_enabled": False if disable_first and index == 0 else row["is_enabled"],
            "sort_order": 100 - index,
        })
    return InventoryCatalogPutRequest(
        expected_catalog_version=response["catalog_version"], options=options,
    )


def test_admin_rbac_validation_occ_soft_disable_and_audit():
    with pytest.raises(HTTPException) as denied:
        asyncio.run(admin_get_inventory_purchase_catalog(request_for({"user_id": 7, "is_admin": False}), None))
    assert denied.value.status_code == 403

    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True})
    before = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
    saved = asyncio.run(admin_put_inventory_purchase_catalog(put_payload(before, disable_first=True), admin))
    assert saved["catalog_version"] == "agent-purchase-v2"
    assert saved["options"][0]["is_enabled"] is True  # stable sort changed; inspect by id below
    disabled = next(row for row in saved["options"] if row["option_id"] == before["options"][0]["option_id"])
    assert disabled["is_enabled"] is False

    stale = put_payload(before)
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(admin_put_inventory_purchase_catalog(stale, admin))
    assert conflict.value.status_code == 409
    assert conflict.value.detail["current_catalog_version"] == "agent-purchase-v2"

    audit = db_row("SELECT username,before_snapshot,after_snapshot FROM audit_logs ORDER BY id DESC LIMIT 1")
    assert audit["username"] == "admin"
    before_audit = json.loads(audit["before_snapshot"])
    after_audit = json.loads(audit["after_snapshot"])
    assert before_audit["catalog_version"] == "agent-purchase-v1"
    assert after_audit["catalog_version"] == "agent-purchase-v2"
    assert before_audit["request_id"] == after_audit["request_id"] == "req-inventory-test"

    # 旧渠道奖励保存只能 patch 自己的字段，不能把停用档通过默认配置复活。
    from services.channel_tier_admin import channel_tier_config_snapshot, write_channel_tier_config
    from db.connection import get_db
    with get_db() as conn:
        write_channel_tier_config(
            conn.cursor(), channel_tier_config_snapshot(), expected_catalog_version="agent-purchase-v2"
        )
    persisted = json.loads(db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"])
    persisted_disabled = next(row for row in persisted["agent_purchase_options"] if row["option_id"] == disabled["option_id"])
    assert persisted_disabled["is_enabled"] is False

    with pytest.raises(ValidationError):
        InventoryCatalogPutRequest(expected_catalog_version="agent-purchase-v2", options=[
            {"amount_cents": 100, "is_enabled": True, "sort_order": 1},
            {"amount_cents": 100, "is_enabled": False, "sort_order": 2},
        ])
    assert json.loads(db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"])["agent_purchase_catalog_version"] == "agent-purchase-v3"


def test_invalid_catalog_shapes_and_environment_override_are_zero_write(monkeypatch):
    invalid_options = [
        [{"amount_cents": -1, "is_enabled": True, "sort_order": 0}],
        [{"amount_cents": 100, "is_enabled": False, "sort_order": 0}],
        [{"amount_cents": 100, "is_enabled": True, "sort_order": -1}],
    ]
    for options in invalid_options:
        with pytest.raises(ValidationError):
            InventoryCatalogPutRequest(expected_catalog_version="agent-purchase-v1", options=options)
    with pytest.raises(ValidationError):
        AgentPurchaseRequest(amount_cents=100, expected_catalog_version="")

    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True})
    writable = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
    before = db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"]
    monkeypatch.setenv("PRICING_CONFIG", json.dumps({"agent_purchase_options": []}))
    visible = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
    assert visible["environment_override"]["active"] is True
    with pytest.raises(HTTPException) as locked:
        asyncio.run(admin_put_inventory_purchase_catalog(put_payload(writable), admin))
    assert locked.value.status_code == 423
    monkeypatch.setenv("PRICING_CONFIG", json.dumps({"agent_purchase_bonus_rate": 0.25}))
    with pytest.raises(HTTPException) as rule_locked:
        asyncio.run(admin_put_global_pricing_config(
            GlobalPricingConfigRequest(
                expected_catalog_version="agent-purchase-v1",
                agent_purchase_bonus_rate=0.2,
            ), admin,
        ))
    assert rule_locked.value.status_code == 423
    monkeypatch.setenv("PRICING_CONFIG", json.dumps({"bonus_validity_months": 6}))
    channel_visible = asyncio.run(admin_get_channel_tier_config(admin))
    assert channel_visible["environment_override"]["active"] is True
    with pytest.raises(HTTPException) as channel_locked:
        asyncio.run(admin_put_channel_tier_config(
            ChannelTierConfigRequest(
                expected_catalog_version="agent-purchase-v1", bonus_validity_months=12
            ), admin,
        ))
    assert channel_locked.value.status_code == 423
    assert db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"] == before


def test_admin_preview_agent_card_and_order_snapshot_are_identical_with_override():
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_pricing_overrides(agent_user_id,wholesale_numer,wholesale_denom) "
                "VALUES (7,100,200)"
            )

    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True})
    agent = request_for({"user_id": 7, "is_admin": False}, method="POST")
    preview = asyncio.run(admin_get_inventory_purchase_catalog(admin, 7))
    cards = asyncio.run(agent_inventory_purchase_options(agent))
    preview_row = preview["options"][0]
    card = cards.options[0]
    assert preview["catalog_version"] == cards.catalog_version
    assert preview_row["preview"]["base_points"] == card.base_points == card.amount_cents * 2
    assert preview_row["preview"]["bonus_points"] == card.bonus_points
    assert preview_row["preview"]["total_points"] == card.total_points
    assert preview_row["preview"]["discount_source"] == "agent_override"
    assert preview_row["preview"]["quote_fingerprint"] == card.quote_fingerprint

    activate_snapshot_cutover()
    created = asyncio.run(agent_inventory_purchase(
        AgentPurchaseRequest(
            amount_cents=card.amount_cents,
            option_id=card.option_id,
            expected_catalog_version=cards.catalog_version,
            expected_quote_fingerprint=card.quote_fingerprint,
            channel="wechat_jsapi",
        ),
        agent,
    ))
    snapshot = db_row("SELECT pricing_snapshot_jsonb FROM recharge_orders WHERE id=%s", (created.order_id,))["pricing_snapshot_jsonb"]
    assert snapshot["base_points"] == card.base_points
    assert snapshot["bonus_points"] == card.bonus_points
    assert snapshot["total_points"] == card.total_points
    assert snapshot["discount_source"] == "agent_override"
    assert created.bonus_points == card.bonus_points


def test_bigint_migration_and_purchase_extreme_are_safe(monkeypatch):
    from pydantic import ValidationError
    from services.agent_inventory_pricing import MAX_AGENT_PURCHASE_AMOUNT_CENTS

    # 生产已有渠道奖励对账 view，直接 ALTER TYPE 会被 PostgreSQL 拒绝。把两列
    # 恢复为迁移前 INTEGER，建立真实依赖和显式 grant，再连续执行 migration 2×；
    # 必须扩宽成功并保留 view/权限。
    migration = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "migration_agent_inventory_bigint_cutover_2026_07_14.sql"
    ).read_text(encoding="utf-8")
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP VIEW IF EXISTS v_bonus_grant_reconcile")
            cur.execute(
                "ALTER TABLE agent_inventory_wallets "
                "ALTER COLUMN bonus_inventory_points TYPE INTEGER USING bonus_inventory_points::INTEGER"
            )
            cur.execute(
                "ALTER TABLE customer_agent_credit_wallets "
                "ALTER COLUMN bonus_credit_points TYPE INTEGER USING bonus_credit_points::INTEGER"
            )
            cur.execute("""
                CREATE VIEW v_bonus_grant_reconcile AS
                SELECT 'agent'::TEXT AS owner_type, agent_user_id AS owner_id,
                       bonus_inventory_points AS pool_balance
                  FROM agent_inventory_wallets
                UNION ALL
                SELECT 'customer'::TEXT AS owner_type, customer_user_id AS owner_id,
                       bonus_credit_points AS pool_balance
                  FROM customer_agent_credit_wallets
            """)
            cur.execute("GRANT SELECT ON v_bonus_grant_reconcile TO PUBLIC")
            cur.execute(migration)
            cur.execute(migration)

    view_state = db_row(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='v_bonus_grant_reconcile' AND column_name='pool_balance'"
    )
    assert view_state["data_type"] == "bigint"
    assert db_row(
        "SELECT COUNT(*) AS c FROM information_schema.role_table_grants "
        "WHERE table_name='v_bonus_grant_reconcile' AND grantee='PUBLIC' "
        "AND privilege_type='SELECT'"
    )["c"] == 1

    columns = db_row(
        """
        SELECT COUNT(*) AS c
          FROM information_schema.columns
         WHERE table_name IN ('agent_inventory_wallets','agent_inventory_transactions')
           AND column_name IN (
             'paid_inventory_points','bonus_inventory_points','frozen_inventory_points',
             'points','balance_paid_after','balance_bonus_after'
           )
           AND data_type='bigint'
        """
    )
    customer_columns = db_row(
        """
        SELECT COUNT(*) AS c
          FROM information_schema.columns
         WHERE table_name IN ('customer_agent_credit_wallets','customer_credit_transactions')
           AND column_name IN (
             'tool_credit_points','publish_credit_points','bonus_credit_points',
             'points','balance_tool_after','balance_publish_after','balance_bonus_after'
           )
           AND data_type='bigint'
        """
    )
    assert columns["c"] == 6
    assert customer_columns["c"] == 7
    assert db_row(
        "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
    ) is None

    with pytest.raises(ValidationError):
        AgentPurchaseRequest(amount_cents=MAX_AGENT_PURCHASE_AMOUNT_CENTS + 1)
    with pytest.raises(ValidationError):
        InventoryCatalogItemRequest(
            amount_cents=MAX_AGENT_PURCHASE_AMOUNT_CENTS + 1,
            is_enabled=True,
            sort_order=1,
        )

    row = db_row("SELECT value FROM system_settings WHERE key='pricing_config'")
    config = json.loads(row["value"])
    config["agent_purchase_bonus_rate"] = 1
    config["agent_purchase_options"][0]["amount_cents"] = MAX_AGENT_PURCHASE_AMOUNT_CENTS
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_settings SET value=%s WHERE key='pricing_config'", (json.dumps(config),))
            # 本用例考的是 **bigint 列宽**,不是业务策略:它刻意驱动
            # ¥20,000,000 → 3.25e9 算力的合成极值。按需铸造的单笔上限(默认 5,000 万)
            # 是一条**业务**防线,会先一步把这笔合成极值拒掉,
            # 于是"列宽够不够"这个契约就再也验不到了。
            # 故此处显式为极值场景抬高铸造上限 —— 两条契约各自成立,互不遮蔽。
            cur.execute(
                """INSERT INTO system_settings(key,value,value_type)
                   VALUES ('inventory_minting_guard_config',%s,'json')
                   ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,value_type='json'""",
                (json.dumps({"max_single_mint_points": 10_000_000_000}),),
            )

    agent = request_for({"user_id": 7, "is_admin": False}, method="POST")
    cards = asyncio.run(agent_inventory_purchase_options(agent))
    card = next(item for item in cards.options if item.amount_cents == MAX_AGENT_PURCHASE_AMOUNT_CENTS)
    assert card.base_points == 3_250_000_000
    assert card.bonus_points == 3_250_000_000
    activate_snapshot_cutover()
    created = asyncio.run(agent_inventory_purchase(
        AgentPurchaseRequest(
            amount_cents=card.amount_cents,
            option_id=card.option_id,
            expected_catalog_version=cards.catalog_version,
            expected_quote_fingerprint=card.quote_fingerprint,
            channel="wechat_jsapi",
        ),
        agent,
    ))
    assert created.bonus_points == 3_250_000_000

    import db.wallet_db as wallet_db
    monkeypatch.setattr(wallet_db, "_record_channel_revenue_if_applicable", lambda *_args: None)
    wallet_db.complete_recharge(created.order_id, "extreme-pay")
    wallet = db_row("SELECT * FROM agent_inventory_wallets WHERE agent_user_id=7")
    assert wallet["paid_inventory_points"] == 3_250_000_000
    assert wallet["bonus_inventory_points"] == 3_250_000_000
    tx = db_row(
        "SELECT MIN(points) AS min_points,MAX(balance_paid_after) AS paid_after,"
        "MAX(balance_bonus_after) AS bonus_after FROM agent_inventory_transactions WHERE related_order_id=%s",
        (created.order_id,),
    )
    assert tx == {
        "min_points": 3_250_000_000,
        "paid_after": 3_250_000_000,
        "bonus_after": 3_250_000_000,
    }

    from services.offline_allocation import allocate_offline_to_customer
    with psycopg2.connect(
        os.environ["TEST_DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor
    ) as conn:
        with conn.cursor() as cur:
            allocation = allocate_offline_to_customer(
                cur,
                agent_user_id=7,
                customer_user_id=7001,
                tool_points=3_250_000_000,
                bonus_points=3_250_000_000,
                description="极值资金链划拨",
            )
    assert allocation["customer_after"] == {
        "tool": 3_250_000_000,
        "publish": 0,
        "bonus": 3_250_000_000,
    }
    customer_wallet = db_row(
        "SELECT * FROM customer_agent_credit_wallets WHERE customer_user_id=7001"
    )
    assert customer_wallet["tool_credit_points"] == 3_250_000_000
    assert customer_wallet["bonus_credit_points"] == 3_250_000_000
    assert db_row(
        "SELECT COUNT(*) AS c FROM customer_credit_transactions WHERE customer_user_id=7001"
    )["c"] == 2


def test_channel_config_concurrent_occ_prevents_lost_update():
    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True}, method="PUT")
    before = asyncio.run(admin_get_channel_tier_config(admin))
    reward_rules = json.loads(json.dumps(before["config"]["agent_tier_config"], default=str))
    reward_rules["certified"]["bonus_rate"] = "0.5"

    requests = {
        "reward": ChannelTierConfigRequest(
            expected_catalog_version=before["catalog_version"],
            agent_tier_config=reward_rules,
        ),
        "validity": ChannelTierConfigRequest(
            expected_catalog_version=before["catalog_version"],
            bonus_validity_months=6,
        ),
    }

    def save(kind):
        try:
            response = asyncio.run(admin_put_channel_tier_config(requests[kind], admin))
            return kind, response["catalog_version"]
        except HTTPException as exc:
            return kind, exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(pool.map(save, ("reward", "validity")))
    assert sorted(results.values(), key=str) == sorted(["agent-purchase-v2", 409], key=str)

    stored = json.loads(db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"])
    if results["reward"] == "agent-purchase-v2":
        assert stored["agent_tier_config"]["certified"]["bonus_rate"] == "0.5"
        assert int(stored.get("bonus_validity_months", 12)) == 12
    else:
        assert str(stored["agent_tier_config"]["certified"]["bonus_rate"]) in {"0.1", "0.10"}
        assert stored["bonus_validity_months"] == 6
    audit = db_row("SELECT module,before_snapshot,after_snapshot FROM audit_logs ORDER BY id DESC LIMIT 1")
    assert audit["module"] == "channel_tier"
    assert json.loads(audit["before_snapshot"])["catalog_version"] == "agent-purchase-v1"
    assert json.loads(audit["after_snapshot"])["catalog_version"] == "agent-purchase-v2"
    assert db_row("SELECT COUNT(*) AS c FROM audit_logs")["c"] == 1


def test_legacy_partial_channel_config_is_compatible_on_get_and_put():
    partial = {
        "agent_purchase_catalog_version": "agent-purchase-v1",
        "agent_purchase_options": [
            {"option_id": "apo_seed1000", "amount_cents": 100000, "is_enabled": True, "sort_order": 10}
        ],
        "agent_tier_config": {"certified": {"min_yuan": 800, "bonus_rate": "0.25"}},
        "founding": {"cap": 5},
        "margin_label_thresholds": {"healthy_bps": 4000},
    }
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_settings SET value=%s WHERE key='pricing_config'", (json.dumps(partial),))

    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True}, method="PUT")
    visible = asyncio.run(admin_get_channel_tier_config(admin))
    assert visible["config"]["founding"] == {
        "cap": 5, "first_order_extra_bonus": "0.1", "min_first_order_yuan": "500"
    }
    assert visible["config"]["margin_label_thresholds"]["healthy_bps"] == 4000
    assert visible["config"]["margin_label_thresholds"]["loss_heavy_bps"] == -4000
    assert visible["config"]["agent_tier_config"]["preferred"]["is_enabled"] is True

    saved = asyncio.run(admin_put_channel_tier_config(
        ChannelTierConfigRequest(
            expected_catalog_version=visible["catalog_version"], bonus_validity_months=6
        ),
        admin,
    ))
    assert saved["catalog_version"] == "agent-purchase-v2"
    persisted = json.loads(db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"])
    assert persisted["founding"]["cap"] == 5
    assert persisted["founding"]["first_order_extra_bonus"] == "0.1"
    assert persisted["margin_label_thresholds"]["healthy_bps"] == 4000
    assert persisted["bonus_validity_months"] == 6


def test_bonus_validity_is_locked_for_pending_order(monkeypatch):
    import db.wallet_db as wallet_db

    snapshot = finalized_snapshot({
        "catalog_version": "agent-purchase-v1", "option_id": "apo_seed1000",
        "amount_cents": 100000, "base_points": 144444,
        "discount_source": "global", "discount_numer": 225, "discount_denom": 325,
        "channel_tier_enabled": True, "tier_at_order": "preferred", "tier_source": "automatic",
        "tier_bonus_rate_bps": 1500, "tier_bonus_points": 21667,
        "founder_eligibility_source": "existing_atomic_gate",
        "founder_min_first_order_yuan_snapshot": "500",
        "founder_bonus_rate_bps_snapshot": 1000,
        "founder_bonus_points_if_eligible": 14444,
        "bonus_validity_months": 12,
        "bonus_rate_bps": 1500, "bonus_points": 21667, "total_points": 166111,
    })
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_TIER_ENABLED'")
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,pricing_snapshot_jsonb,pricing_catalog_version) "
                "VALUES ('validity-snapshot',7,100000,144444,21667,'pending','agent_inventory_purchase',%s,'agent-purchase-v1')",
                (json.dumps(snapshot),),
            )
            current = json.loads(db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"])
            current["bonus_validity_months"] = 6
            cur.execute("UPDATE system_settings SET value=%s WHERE key='pricing_config'", (json.dumps(current),))
    monkeypatch.setattr(wallet_db, "_record_channel_revenue_if_applicable", lambda *_args: None)
    wallet_db.complete_recharge("validity-snapshot", "validity-pay")
    grants = db_row(
        "SELECT COUNT(*) AS c,MIN(EXTRACT(EPOCH FROM (expires_at-granted_at))/86400) AS min_days "
        "FROM bonus_grants WHERE related_order_id='validity-snapshot'"
    )
    assert grants["c"] >= 1
    assert float(grants["min_days"]) > 330


@pytest.mark.parametrize("bad_marker", [None, "", "not-a-timestamp", "2999-01-01 00:00:00"])
def test_cutover_activation_rejects_null_invalid_or_future_marker(bad_marker):
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_settings(key,value,value_type) VALUES "
                "('AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT',%s,'timestamp')",
                (bad_marker,),
            )
    with pytest.raises(psycopg2.Error, match="snapshot cutover refused"):
        activate_snapshot_cutover()
    stored = db_row(
        "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
    )
    assert stored["value"] == bad_marker


def test_first_activation_requires_fresh_same_image_hot_rollback_evidence():
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "activate_agent_inventory_snapshot_cutover_2026_07_14.sql"
    ).read_text(encoding="utf-8")
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_settings(key,value,value_type,updated_at) VALUES "
                "('AGENT_INVENTORY_SNAPSHOT_HOT_ROLLBACK_READY',"
                "'agent-inventory-snapshot-v3|sha256:a|sha256:b','deploy_capability',NOW())"
            )
            with pytest.raises(psycopg2.Error, match="fresh same-image hot rollback evidence is missing"):
                cur.execute(script)
        conn.rollback()
    assert db_row(
        "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
    ) is None
    assert db_row(
        "SELECT pg_sequence_last_value('agent_inventory_writer_generation_fence_seq'::regclass) AS generation"
    )["generation"] == 1


def test_cutover_lock_timeout_writes_no_marker_and_is_retryable():
    blocker = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    try:
        with blocker.cursor() as cur:
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                "VALUES ('activation-lock-blocker',7,1000,1625,0,'pending','agent_inventory_purchase')"
            )
        started = time.monotonic()
        with pytest.raises(psycopg2.Error, match="lock timeout"):
            activate_snapshot_cutover()
        assert time.monotonic() - started >= 4.5
        assert db_row(
            "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
        ) is None
        blocker.rollback()
    finally:
        blocker.close()

    activate_snapshot_cutover()
    assert db_row(
        "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
    )["value"]


def test_blue_green_overlap_order_remains_legacy_eligible_after_separate_cutover(monkeypatch):
    import db.wallet_db as wallet_db

    # prestart BIGINT 已完成，但尚未 cutover；旧 active 在重叠窗口创建空快照订单。
    assert db_row(
        "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
    ) is None
    old_writer = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    try:
        with old_writer.cursor() as cur:
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                "VALUES ('legacy-overlap',7,1000,1625,0,'pending','agent_inventory_purchase')"
            )
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                "VALUES ('legacy-already-paid',7,1000,1625,0,'paid','agent_inventory_purchase')"
            )
        # activation 必须等待旧 writer 的未提交 INSERT；排空后 marker 使用锁后时钟。
        with ThreadPoolExecutor(max_workers=1) as pool:
            activation = pool.submit(activate_snapshot_cutover)
            time.sleep(0.2)
            assert activation.done() is False
            old_writer.commit()
            activation.result(timeout=10)
    finally:
        old_writer.close()

    marker = db_row(
        "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
    )["value"]
    assert db_row(
        "SELECT agent_inventory_legacy_eligible AS allowed FROM recharge_orders WHERE id='legacy-overlap'"
    )["allowed"] is True
    assert db_row(
        "SELECT agent_inventory_legacy_eligible AS allowed FROM recharge_orders WHERE id='legacy-already-paid'"
    )["allowed"] is False

    # 连续激活必须保留首次 marker。
    activate_snapshot_cutover()
    assert db_row(
        "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
    )["value"] == marker
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation, match="writer generation rejected"):
                cur.execute(
                    "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                    "VALUES ('legacy-after-rejected',7,1000,1625,0,'pending','agent_inventory_purchase')"
                )
        conn.rollback()

    # 即使有人绕过/禁用 trigger 插入，重复 activation 也不得扩大 allowlist，且必须拒绝复核。
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE recharge_orders DISABLE TRIGGER trg_agent_inventory_snapshot_writer_generation")
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,agent_inventory_writer_generation) "
                "VALUES ('legacy-after',7,1000,1625,0,'pending','agent_inventory_purchase',1)"
            )
            cur.execute("ALTER TABLE recharge_orders ENABLE TRIGGER trg_agent_inventory_snapshot_writer_generation")
    with pytest.raises(psycopg2.Error, match="outside the legacy allowlist"):
        activate_snapshot_cutover()
    assert db_row(
        "SELECT value FROM system_settings WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
    )["value"] == marker
    monkeypatch.setattr(wallet_db, "_record_channel_revenue_if_applicable", lambda *_args: None)
    wallet_db.complete_recharge("legacy-overlap", "legacy-pay")
    assert db_row("SELECT payment_status FROM recharge_orders WHERE id='legacy-overlap'")["payment_status"] == "paid"

    with pytest.raises(ValueError, match="部署后代理进货订单缺少不可变快照"):
        wallet_db.complete_recharge("legacy-after", "must-fail")
    assert db_row("SELECT payment_status FROM recharge_orders WHERE id='legacy-after'")["payment_status"] == "pending"
    assert db_row(
        "SELECT COUNT(*) AS c FROM agent_inventory_transactions WHERE related_order_id='legacy-after'"
    )["c"] == 0


def test_phase_a_dual_read_settles_only_generationless_legacy(monkeypatch):
    import db.wallet_db as wallet_db

    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                "VALUES ('phase-a-legacy',7,1000,1625,0,'pending','agent_inventory_purchase')"
            )
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,agent_inventory_writer_generation) "
                "VALUES ('phase-a-broken-new',7,1000,1625,0,'pending','agent_inventory_purchase',2)"
            )

    monkeypatch.setattr(wallet_db, "_record_channel_revenue_if_applicable", lambda *_args: None)
    wallet_db.complete_recharge("phase-a-legacy", "phase-a-pay")
    assert db_row(
        "SELECT payment_status FROM recharge_orders WHERE id='phase-a-legacy'"
    )["payment_status"] == "paid"

    with pytest.raises(ValueError, match="Phase A 异常订单"):
        wallet_db.complete_recharge("phase-a-broken-new", "phase-a-must-fail")
    assert db_row(
        "SELECT payment_status FROM recharge_orders WHERE id='phase-a-broken-new'"
    )["payment_status"] == "pending"
    assert db_row(
        "SELECT COUNT(*) AS c FROM agent_inventory_transactions "
        "WHERE related_order_id='phase-a-broken-new'"
    )["c"] == 0


def test_phase_a_blocks_new_purchase_order_but_keeps_quotes_readable():
    agent = request_for({"user_id": 7, "is_admin": False}, method="POST")
    quoted = asyncio.run(agent_inventory_purchase_options(agent))
    card = quoted.options[0]

    with pytest.raises(HTTPException) as blocked:
        asyncio.run(agent_inventory_purchase(
            AgentPurchaseRequest(
                amount_cents=card.amount_cents,
                option_id=card.option_id,
                expected_catalog_version=quoted.catalog_version,
                expected_quote_fingerprint=card.quote_fingerprint,
                channel="wechat_jsapi",
            ),
            agent,
        ))

    assert blocked.value.status_code == 503
    assert blocked.value.detail["code"] == "INVENTORY_PURCHASE_ACTIVATION_PENDING"
    assert blocked.value.headers["Retry-After"] == "30"
    assert db_row("SELECT COUNT(*) AS c FROM recharge_orders")["c"] == 0
    state = db_row(
        "SELECT "
        "(SELECT COUNT(*) FROM system_settings "
        " WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT') AS marker_count, "
        "pg_sequence_last_value('agent_inventory_writer_generation_fence_seq'::regclass) AS generation"
    )
    assert state == {"marker_count": 0, "generation": 1}


@pytest.mark.parametrize("bad_marker", [None, "", "not-a-timestamp", "2999-01-01 00:00:00"])
def test_new_purchase_fail_closed_when_activation_marker_is_invalid(bad_marker):
    agent = request_for({"user_id": 7, "is_admin": False}, method="POST")
    quoted = asyncio.run(agent_inventory_purchase_options(agent))
    card = quoted.options[0]
    activate_snapshot_cutover()
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE system_settings SET value=%s "
                "WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'",
                (bad_marker,),
            )

    with pytest.raises(HTTPException) as blocked:
        asyncio.run(agent_inventory_purchase(
            AgentPurchaseRequest(
                amount_cents=card.amount_cents,
                option_id=card.option_id,
                expected_catalog_version=quoted.catalog_version,
                expected_quote_fingerprint=card.quote_fingerprint,
                channel="wechat_jsapi",
            ),
            agent,
        ))
    assert blocked.value.status_code == 503
    assert blocked.value.detail["code"] == "INVENTORY_PURCHASE_ACTIVATION_PENDING"
    assert db_row("SELECT COUNT(*) AS c FROM recharge_orders")["c"] == 0


def test_transaction_started_before_marker_but_delayed_insert_is_rejected():
    old_writer = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    try:
        with old_writer.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("SELECT CURRENT_TIMESTAMP")
            transaction_started_at = cur.fetchone()[0]

            # 该事务尚未触碰 recharge_orders，因此 activation 的表锁不会等待它。
            activate_snapshot_cutover()
            marker = db_row(
                "SELECT value::timestamp AS value FROM system_settings "
                "WHERE key='AGENT_INVENTORY_SNAPSHOT_CUTOVER_AT'"
            )["value"]
            assert transaction_started_at.replace(tzinfo=None) < marker

            # created_at 若被允许会沿用事务开始时间，但 generation trigger 必须按 INSERT 代际拒绝。
            with pytest.raises(psycopg2.errors.CheckViolation, match="writer generation rejected"):
                cur.execute(
                    "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                    "VALUES ('delayed-old-writer',7,1000,1625,0,'pending','agent_inventory_purchase')"
                )
        old_writer.rollback()
    finally:
        old_writer.close()
    assert db_row("SELECT COUNT(*) AS c FROM recharge_orders WHERE id='delayed-old-writer'")["c"] == 0


def test_active_generation_fence_rejects_update_into_legacy_allowlist():
    activate_snapshot_cutover()
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                "VALUES ('ordinary-before-forge',7,1000,0,0,'pending','user_recharge')"
            )
            with pytest.raises(psycopg2.errors.CheckViolation, match="writer generation rejected"):
                cur.execute(
                    "UPDATE recharge_orders SET order_type='agent_inventory_purchase', "
                    "agent_inventory_writer_generation=1, agent_inventory_legacy_eligible=TRUE "
                    "WHERE id='ordinary-before-forge'"
                )
        conn.rollback()


def test_all_snapshot_pricing_writers_advance_expected_version():
    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True})
    agent = request_for({"user_id": 7, "is_admin": False}, method="POST")
    old = asyncio.run(agent_inventory_purchase_options(agent))

    asyncio.run(admin_put_global_pricing_config(
        GlobalPricingConfigRequest(
            expected_catalog_version=old.catalog_version,
            agent_purchase_bonus_rate=0.2,
        ), admin,
    ))
    after_global = asyncio.run(agent_inventory_purchase_options(agent))
    assert after_global.catalog_version == "agent-purchase-v2"
    with pytest.raises(HTTPException) as stale:
        asyncio.run(agent_inventory_purchase(
            AgentPurchaseRequest(
                amount_cents=old.options[0].amount_cents,
                option_id=old.options[0].option_id,
                expected_catalog_version=old.catalog_version,
                expected_quote_fingerprint=old.options[0].quote_fingerprint,
                channel="wechat_jsapi",
            ), agent,
        ))
    assert stale.value.status_code == 409

    from services.agent_pricing_overrides import write_agent_pricing_override_atomic
    write_agent_pricing_override_atomic(
        7,
        wholesale_numer=100,
        wholesale_denom=200,
        expected_catalog_version="agent-purchase-v2",
        admin_user_id=99,
        admin_username="admin",
        request_id="req-direct-atomic",
        ip_address="127.0.0.1",
        audit_module="test_agent_pricing_override",
    )
    assert asyncio.run(agent_inventory_purchase_options(agent)).catalog_version == "agent-purchase-v3"

    from services.channel_tier_admin import channel_tier_config_snapshot, write_channel_tier_config
    from db.connection import get_db
    with get_db() as conn:
        write_channel_tier_config(
            conn.cursor(), channel_tier_config_snapshot(), expected_catalog_version="agent-purchase-v3"
        )
    assert asyncio.run(agent_inventory_purchase_options(agent)).catalog_version == "agent-purchase-v4"


def test_factory_agent_override_is_versioned_and_audited_in_one_transaction():
    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True}, method="PUT")
    result = asyncio.run(admin_put_agent_pricing_override(
        7,
        AgentPricingOverrideRequest(
            expected_catalog_version="agent-purchase-v1",
            wholesale_numer=100,
            wholesale_denom=200,
            note="factory atomic",
        ),
        admin,
    ))
    assert result["catalog_version"] == "agent-purchase-v2"
    override = db_row(
        "SELECT wholesale_numer,wholesale_denom,updated_by FROM agent_pricing_overrides WHERE agent_user_id=7"
    )
    assert override == {"wholesale_numer": 100, "wholesale_denom": 200, "updated_by": 99}
    audit = db_row(
        "SELECT module,entity_type,entity_id,before_snapshot,after_snapshot FROM audit_logs "
        "WHERE module='admin_pricing_agent_override'"
    )
    assert audit["entity_type"] == "agent_pricing_override"
    assert audit["entity_id"] == 7
    assert json.loads(audit["before_snapshot"])["catalog_version"] == "agent-purchase-v1"
    assert json.loads(audit["after_snapshot"])["catalog_version"] == "agent-purchase-v2"
    assert json.loads(audit["after_snapshot"])["request_id"] == "req-inventory-test"

    with pytest.raises(HTTPException) as stale:
        asyncio.run(admin_put_agent_pricing_override(
            7,
            AgentPricingOverrideRequest(
                expected_catalog_version="agent-purchase-v1",
                wholesale_numer=120,
                wholesale_denom=200,
            ),
            admin,
        ))
    assert stale.value.status_code == 409
    assert db_row(
        "SELECT wholesale_numer FROM agent_pricing_overrides WHERE agent_user_id=7"
    )["wholesale_numer"] == 100


def test_user_management_override_audit_failure_rolls_back_everything(monkeypatch):
    import api.admin_api as admin_api_module
    import services.agent_inventory_pricing as pricing_service

    monkeypatch.setattr(admin_api_module, "get_user", lambda user_id: {"id": user_id})
    monkeypatch.setattr(admin_api_module, "_assert_service_provider", lambda _user_id: None)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(pricing_service, "insert_pricing_audit", fail_audit)
    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True}, method="PUT")
    before_config = db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"]
    before_epoch = db_row("SELECT value FROM system_settings WHERE key='pricing_config_epoch'")["value"]
    with pytest.raises(RuntimeError, match="audit unavailable"):
        asyncio.run(admin_set_purchase_pricing_override(
            7,
            SetPurchasePricingOverrideRequest(
                expected_catalog_version="agent-purchase-v1",
                wholesale_numer=100,
                wholesale_denom=200,
                reason="must rollback",
            ),
            admin,
        ))

    assert db_row("SELECT COUNT(*) AS c FROM agent_pricing_overrides")["c"] == 0
    assert db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"] == before_config
    assert db_row("SELECT value FROM system_settings WHERE key='pricing_config_epoch'")["value"] == before_epoch
    assert db_row("SELECT COUNT(*) AS c FROM audit_logs")["c"] == 0


def test_environment_cannot_pin_db_controlled_revision(monkeypatch):
    monkeypatch.setenv("PRICING_CONFIG", json.dumps({"agent_purchase_catalog_version": "agent-purchase-v99"}))
    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True})
    agent = request_for({"user_id": 7, "is_admin": False})
    assert asyncio.run(agent_inventory_purchase_options(agent)).catalog_version == "agent-purchase-v1"
    catalog = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
    assert catalog["catalog_version"] == "agent-purchase-v1"
    assert catalog["environment_override"]["active"] is False
    asyncio.run(admin_put_global_pricing_config(
        GlobalPricingConfigRequest(
            expected_catalog_version="agent-purchase-v1",
            agent_purchase_bonus_rate=0.2,
        ), admin,
    ))
    assert asyncio.run(agent_inventory_purchase_options(agent)).catalog_version == "agent-purchase-v2"


def test_quote_fingerprint_rejects_same_catalog_env_and_channel_flag_changes(monkeypatch):
    agent = request_for({"user_id": 7, "is_admin": False}, method="POST")
    quoted = asyncio.run(agent_inventory_purchase_options(agent))
    card = quoted.options[0]

    monkeypatch.setenv("PRICING_CONFIG", json.dumps({"agent_purchase_bonus_rate": 0.25}))
    with pytest.raises(HTTPException) as env_stale:
        asyncio.run(agent_inventory_purchase(
            AgentPurchaseRequest(
                amount_cents=card.amount_cents,
                option_id=card.option_id,
                expected_catalog_version=quoted.catalog_version,
                expected_quote_fingerprint=card.quote_fingerprint,
                channel="wechat_jsapi",
            ), agent,
        ))
    assert env_stale.value.status_code == 409
    assert env_stale.value.detail["code"] == "QUOTE_FINGERPRINT_CONFLICT"
    assert env_stale.value.detail["current_catalog_version"] == quoted.catalog_version

    monkeypatch.delenv("PRICING_CONFIG")
    quoted = asyncio.run(agent_inventory_purchase_options(agent))
    card = quoted.options[0]
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_TIER_ENABLED'")
    with pytest.raises(HTTPException) as flag_stale:
        asyncio.run(agent_inventory_purchase(
            AgentPurchaseRequest(
                amount_cents=card.amount_cents,
                option_id=card.option_id,
                expected_catalog_version=quoted.catalog_version,
                expected_quote_fingerprint=card.quote_fingerprint,
                channel="wechat_jsapi",
            ), agent,
        ))
    assert flag_stale.value.status_code == 409
    assert flag_stale.value.detail["code"] == "QUOTE_FINGERPRINT_CONFLICT"
    assert db_row("SELECT COUNT(*) AS c FROM recharge_orders")["c"] == 0


def test_quote_fingerprint_rejects_same_catalog_rolling_tier_change():
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_settings SET value='true' WHERE key='CHANNEL_TIER_ENABLED'")
    agent = request_for({"user_id": 7, "is_admin": False}, method="POST")
    quoted = asyncio.run(agent_inventory_purchase_options(agent))
    card = quoted.options[0]

    # 精确 rolling 改变但有效等级/奖励不变时，不制造无意义 409。
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                "VALUES ('rolling-small',7,100,0,0,'paid','agent_inventory_purchase')"
            )
    same_tier = asyncio.run(agent_inventory_purchase_options(agent)).options[0]
    assert same_tier.quote_fingerprint == card.quote_fingerprint

    # 再增加已支付进货，把本单投影从认证档推到优选档；同目录版本必须拒绝旧报价。
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type) "
                "VALUES ('rolling-paid',7,249900,0,0,'paid','agent_inventory_purchase')"
            )
    with pytest.raises(HTTPException) as stale:
        asyncio.run(agent_inventory_purchase(
            AgentPurchaseRequest(
                amount_cents=card.amount_cents,
                option_id=card.option_id,
                expected_catalog_version=quoted.catalog_version,
                expected_quote_fingerprint=card.quote_fingerprint,
                channel="wechat_jsapi",
            ), agent,
        ))
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "QUOTE_FINGERPRINT_CONFLICT"
    assert db_row(
        "SELECT COUNT(*) AS c FROM recharge_orders WHERE payment_status='pending'"
    )["c"] == 0


def test_odd_cent_custom_purchase_reaches_provider_without_float_truncation(monkeypatch):
    import api.agent_workbench_api as workbench
    import services.wechat_pay as wechat_pay

    captured = {}
    monkeypatch.setattr(workbench, "resolve_payment_channel", lambda *_args, **_kwargs: "wechat_native")

    async def fake_native_order(*, out_trade_no, total_yuan, description):
        captured["total_yuan"] = total_yuan
        captured["provider_cents"] = int(total_yuan * 100)
        return {"code_url": "weixin://exact-cents"}

    monkeypatch.setattr(wechat_pay, "create_native_order", fake_native_order)
    agent = request_for({"user_id": 7, "is_admin": False}, method="POST", user_agent="desktop")
    preview = asyncio.run(agent_inventory_purchase_preview(
        AgentPurchasePreviewRequest(amount_cents=201), agent,
    ))
    activate_snapshot_cutover()
    created = asyncio.run(agent_inventory_purchase(
        AgentPurchaseRequest(
            amount_cents=201,
            expected_catalog_version=preview.catalog_version,
            expected_quote_fingerprint=preview.quote_fingerprint,
            channel="wechat_native",
        ), agent,
    ))
    assert str(captured["total_yuan"]) == "2.01"
    assert captured["provider_cents"] == 201
    assert created.amount_cents == 201
    assert created.quote_fingerprint == preview.quote_fingerprint


def test_two_concurrent_admin_saves_only_one_wins():
    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True})
    before = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
    payload = put_payload(before)

    def save_once(index):
        req = request_for({"user_id": 99, "username": f"admin{index}", "is_admin": True}, method="PUT")
        try:
            return asyncio.run(admin_put_inventory_purchase_catalog(payload, req))["catalog_version"]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save_once, (1, 2)))
    assert sorted(results, key=str) == sorted(["agent-purchase-v2", 409], key=str)
    assert db_row("SELECT COUNT(*) AS c FROM audit_logs")["c"] == 1


def test_global_rule_concurrent_occ_allows_one_commit_with_one_audit():
    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True}, method="PUT")
    before = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))

    def save(rate):
        try:
            response = asyncio.run(admin_put_global_pricing_config(
                GlobalPricingConfigRequest(
                    expected_catalog_version=before["catalog_version"],
                    agent_purchase_bonus_rate=rate,
                ), admin,
            ))
            return response["catalog_version"]
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (0.2, 0.3)))
    assert sorted(results, key=str) == sorted(["agent-purchase-v2", 409], key=str)
    audit = db_row(
        "SELECT module,before_snapshot,after_snapshot FROM audit_logs ORDER BY id DESC LIMIT 1"
    )
    assert audit["module"] == "inventory_default_rules"
    assert json.loads(audit["before_snapshot"])["catalog_version"] == "agent-purchase-v1"
    assert json.loads(audit["after_snapshot"])["catalog_version"] == "agent-purchase-v2"
    assert db_row("SELECT COUNT(*) AS c FROM audit_logs")["c"] == 1


def test_price_write_audit_failure_rolls_back_config_version_and_epoch(monkeypatch):
    import services.agent_inventory_pricing as pricing

    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True}, method="PUT")
    before_value = db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"]
    before_epoch = db_row("SELECT value FROM system_settings WHERE key='pricing_config_epoch'")["value"]

    def audit_failure(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(pricing, "insert_pricing_audit", audit_failure)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        asyncio.run(admin_put_global_pricing_config(
            GlobalPricingConfigRequest(
                expected_catalog_version="agent-purchase-v1",
                agent_purchase_bonus_rate=0.2,
            ), admin,
        ))
    assert db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"] == before_value
    assert db_row("SELECT value FROM system_settings WHERE key='pricing_config_epoch'")["value"] == before_epoch
    assert db_row("SELECT COUNT(*) AS c FROM audit_logs")["c"] == 0

    with pytest.raises(RuntimeError, match="audit unavailable"):
        asyncio.run(admin_put_channel_tier_config(
            ChannelTierConfigRequest(
                expected_catalog_version="agent-purchase-v1",
                bonus_validity_months=6,
            ), admin,
        ))
    assert db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"] == before_value
    assert db_row("SELECT value FROM system_settings WHERE key='pricing_config_epoch'")["value"] == before_epoch
    assert db_row("SELECT COUNT(*) AS c FROM audit_logs")["c"] == 0


def test_catalog_and_global_rule_concurrency_preserves_both_changes():
    admin = request_for({"user_id": 99, "username": "admin", "is_admin": True})
    before = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
    catalog_payload = put_payload(before, disable_first=True)

    def save_catalog():
        try:
            return asyncio.run(admin_put_inventory_purchase_catalog(catalog_payload, admin))
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            latest = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
            disabled_id = before["options"][0]["option_id"]
            retry = put_payload(latest)
            retry.options = [item.model_copy(update={"is_enabled": False}) if item.option_id == disabled_id else item for item in retry.options]
            return asyncio.run(admin_put_inventory_purchase_catalog(retry, admin))

    def save_rule():
        try:
            return asyncio.run(admin_put_global_pricing_config(
                GlobalPricingConfigRequest(
                    expected_catalog_version=before["catalog_version"],
                    agent_purchase_bonus_rate=0.2,
                ), admin,
            ))
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            latest = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
            return asyncio.run(admin_put_global_pricing_config(
                GlobalPricingConfigRequest(
                    expected_catalog_version=latest["catalog_version"],
                    agent_purchase_bonus_rate=0.2,
                ), admin,
            ))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save_catalog), pool.submit(save_rule)]
        for future in futures:
            future.result()
    stored = json.loads(db_row("SELECT value FROM system_settings WHERE key='pricing_config'")["value"])
    assert stored["agent_purchase_catalog_version"] == "agent-purchase-v3"
    assert stored["agent_purchase_bonus_rate"] == 0.2
    first = next(row for row in stored["agent_purchase_options"] if row["option_id"] == before["options"][0]["option_id"])
    assert first["is_enabled"] is False


def test_pending_order_uses_old_snapshot_after_catalog_change_and_callback_is_idempotent(monkeypatch):
    agent_request = request_for({"user_id": 7, "is_admin": False}, method="POST")
    options = asyncio.run(__import__("api.agent_workbench_api", fromlist=["agent_inventory_purchase_options"])
                          .agent_inventory_purchase_options(agent_request))
    chosen = options.options[0]
    activate_snapshot_cutover()
    created = asyncio.run(agent_inventory_purchase(
        AgentPurchaseRequest(
            amount_cents=chosen.amount_cents,
            option_id=chosen.option_id,
            expected_catalog_version=options.catalog_version,
            expected_quote_fingerprint=chosen.quote_fingerprint,
            channel="wechat_jsapi",
        ),
        agent_request,
    ))
    old = db_row("SELECT * FROM recharge_orders WHERE id=%s", (created.order_id,))
    assert old["pricing_catalog_version"] == "agent-purchase-v1"
    assert old["pricing_snapshot_jsonb"]["total_points"] == chosen.total_points

    config_row = db_row("SELECT value FROM system_settings WHERE key='pricing_config'")
    changed = json.loads(config_row["value"])
    changed["agent_purchase_catalog_version"] = "agent-purchase-v2"
    changed["wholesale_numer"] = 325
    changed["wholesale_denom"] = 325
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE system_settings SET value=%s WHERE key='pricing_config'", (json.dumps(changed),))

    import db.wallet_db as wallet_db
    import services.agent_inventory as agent_inventory

    monkeypatch.setattr(wallet_db, "_record_channel_revenue_if_applicable", lambda cursor, order: None)

    def record_snapshot_inventory(cursor, agent_user_id, paid_points, bonus_points=0, **_kwargs):
        cursor.execute(
            "INSERT INTO test_inventory(agent_user_id,paid_points,bonus_points,calls) VALUES (%s,%s,%s,1) "
            "ON CONFLICT(agent_user_id) DO UPDATE SET paid_points=test_inventory.paid_points+EXCLUDED.paid_points, "
            "bonus_points=test_inventory.bonus_points+EXCLUDED.bonus_points,calls=test_inventory.calls+1",
            (agent_user_id, paid_points, bonus_points),
        )
        return {"paid_inventory_points": paid_points, "bonus_inventory_points": bonus_points}

    monkeypatch.setattr(agent_inventory, "purchase_inventory_prepay", record_snapshot_inventory)
    wallet_db.complete_recharge(created.order_id, "pay-1")
    wallet_db.complete_recharge(created.order_id, "pay-duplicate")
    credited = db_row("SELECT * FROM test_inventory WHERE agent_user_id=7")
    assert credited["paid_points"] == old["pricing_snapshot_jsonb"]["base_points"]
    assert credited["bonus_points"] == old["pricing_snapshot_jsonb"]["bonus_points"]
    assert credited["calls"] == 1
    paid = db_row("SELECT payment_status,payment_id,pricing_snapshot_jsonb FROM recharge_orders WHERE id=%s", (created.order_id,))
    assert paid["payment_status"] == "paid"
    assert paid["payment_id"] == "pay-1"
    assert paid["pricing_snapshot_jsonb"] == old["pricing_snapshot_jsonb"]


def test_channel_and_founder_snapshot_values_are_used_once(monkeypatch):
    import db.wallet_db as wallet_db
    import services.agent_inventory as agent_inventory
    import services.channel_tier as channel_tier

    snapshot = finalized_snapshot({
        "catalog_version": "agent-purchase-v1", "option_id": "apo_seed1000",
        "amount_cents": 100000, "base_points": 144444,
        "discount_source": "global", "discount_numer": 225, "discount_denom": 325,
        "channel_tier_enabled": True, "tier_at_order": "preferred", "tier_source": "automatic",
        "tier_bonus_rate_bps": 1500, "tier_bonus_points": 21667,
        "founder_eligibility_source": "existing_atomic_gate",
        "founder_min_first_order_yuan_snapshot": "500",
        "founder_bonus_rate_bps_snapshot": 1000,
        "founder_bonus_points_if_eligible": 14444,
        "bonus_validity_months": 12,
        "bonus_rate_bps": 1500, "bonus_points": 21667, "total_points": 166111,
    })
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,pricing_snapshot_jsonb,pricing_catalog_version) "
                "VALUES ('tier-snapshot',7,100000,144444,21667,'pending','agent_inventory_purchase',%s,'agent-purchase-v1')",
                (json.dumps(snapshot),),
            )

    calls = {"purchase": 0, "tier": 0, "founder": 0}
    monkeypatch.setattr(wallet_db, "_record_channel_revenue_if_applicable", lambda *_args: None)

    def purchase(_cursor, *, paid_points, bonus_points, **_kwargs):
        calls["purchase"] += 1
        assert (paid_points, bonus_points) == (144444, 0)
        return {}

    def tier_grant(_cursor, **kwargs):
        calls["tier"] += 1
        assert kwargs["tier"] == "preferred"
        assert kwargs["bonus_points"] == 21667
        assert kwargs["bonus_rate_bps"] == 1500
        return {"granted_points": 21667}

    def first_order(_cursor, _agent, _order, *, amount_yuan, min_first_order_yuan):
        assert str(amount_yuan) == "1000"
        assert str(min_first_order_yuan) == "500"
        return True

    def founder_grant(_cursor, **kwargs):
        calls["founder"] += 1
        assert kwargs["bonus_points"] == 14444
        assert kwargs["bonus_rate_bps"] == 1000
        return {"granted_points": 14444}

    monkeypatch.setattr(agent_inventory, "purchase_inventory_prepay", purchase)
    monkeypatch.setattr(channel_tier, "evaluate_and_apply_tier", lambda *_args, **_kwargs: {"new_tier": "strategic"})
    monkeypatch.setattr(channel_tier, "grant_tier_bonus", tier_grant)
    monkeypatch.setattr(channel_tier, "is_first_order", first_order)
    monkeypatch.setattr(channel_tier, "grab_founder_seat", lambda *_args, **_kwargs: {"is_founder": True})
    monkeypatch.setattr(channel_tier, "grant_founder_first_order_bonus", founder_grant)
    wallet_db.complete_recharge("tier-snapshot", "tier-pay")
    wallet_db.complete_recharge("tier-snapshot", "tier-pay-duplicate")
    assert calls == {"purchase": 1, "tier": 1, "founder": 1}
    assert db_row("SELECT settlement_mode FROM recharge_orders WHERE id='tier-snapshot'")["settlement_mode"] == "agent_inventory_prepay_channel_tier"


def test_real_channel_founder_ledgers_are_atomic_under_concurrent_callbacks(monkeypatch):
    import db.wallet_db as wallet_db

    snapshot = finalized_snapshot({
        "catalog_version": "agent-purchase-v1", "option_id": "apo_seed1000",
        "amount_cents": 100000, "base_points": 144444,
        "discount_source": "global", "discount_numer": 225, "discount_denom": 325,
        "channel_tier_enabled": True, "tier_at_order": "preferred", "tier_source": "automatic",
        "tier_bonus_rate_bps": 1500, "tier_bonus_points": 21667,
        "founder_eligibility_source": "existing_atomic_gate",
        "founder_min_first_order_yuan_snapshot": "500",
        "founder_bonus_rate_bps_snapshot": 1000,
        "founder_bonus_points_if_eligible": 14444,
        "bonus_validity_months": 12,
        "bonus_rate_bps": 1500, "bonus_points": 21667, "total_points": 166111,
    })
    with psycopg2.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO recharge_orders(id,user_id,amount_cents,base_points,bonus_points,payment_status,order_type,pricing_snapshot_jsonb,pricing_catalog_version) "
                "VALUES ('real-tier-snapshot',7,100000,144444,21667,'pending','agent_inventory_purchase',%s,'agent-purchase-v1')",
                (json.dumps(snapshot),),
            )
    # 渠道收益账本不属于本测试；库存、tier、founder 均使用真实服务与真实 PG 表。
    monkeypatch.setattr(wallet_db, "_record_channel_revenue_if_applicable", lambda *_args: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda payment_id: wallet_db.complete_recharge("real-tier-snapshot", payment_id),
            ("real-pay-a", "real-pay-b"),
        ))
    wallet_db.complete_recharge("real-tier-snapshot", "real-pay-third")

    wallet = db_row("SELECT * FROM agent_inventory_wallets WHERE agent_user_id=7")
    assert wallet["paid_inventory_points"] == 144444
    assert wallet["bonus_inventory_points"] == 21667 + 14444
    assert wallet["total_purchased_points"] == 144444 + 21667 + 14444
    assert db_row("SELECT COUNT(*) AS c FROM agent_inventory_transactions WHERE related_order_id='real-tier-snapshot'")["c"] == 3
    grants = db_row(
        "SELECT COUNT(*) AS c,COALESCE(SUM(granted_points),0) AS points FROM bonus_grants WHERE related_order_id='real-tier-snapshot'"
    )
    assert grants == {"c": 2, "points": 21667 + 14444}
    state = db_row("SELECT is_founder,founder_rank,first_order_done FROM agent_channel_tier_state WHERE agent_user_id=7")
    assert state == {"is_founder": True, "founder_rank": 1, "first_order_done": True}
    assert db_row("SELECT used FROM founder_seats WHERE id=1")["used"] == 1
    paid = db_row("SELECT payment_status,payment_id FROM recharge_orders WHERE id='real-tier-snapshot'")
    assert paid["payment_status"] == "paid"
    assert paid["payment_id"] in {"real-pay-a", "real-pay-b"}


def test_old_founder_cap_snapshot_cannot_reduce_newer_operational_cap():
    from services.channel_tier import grab_founder_seat

    with psycopg2.connect(
        os.environ["TEST_DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE founder_seats SET used=10,cap=20 WHERE id=1")
            result = grab_founder_seat(cur, 8, cap_snapshot=10)
            cur.execute("SELECT used,cap FROM founder_seats WHERE id=1")
            seat = dict(cur.fetchone())
    assert result["is_founder"] is False
    assert seat == {"used": 10, "cap": 20}


def test_epoch_read_failure_prevents_order_write(monkeypatch):
    import services.config_epoch as config_epoch
    monkeypatch.setattr(config_epoch, "read_config_epoch_strict", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("epoch down")))
    agent = request_for({"user_id": 7, "is_admin": False}, method="POST")
    with pytest.raises(RuntimeError, match="epoch down"):
        asyncio.run(agent_inventory_purchase(
            AgentPurchaseRequest(amount_cents=201, expected_catalog_version="agent-purchase-v1", channel="wechat_jsapi"),
            agent,
        ))
    assert db_row("SELECT COUNT(*) AS c FROM recharge_orders")["c"] == 0


def test_four_warm_processes_converge_after_catalog_save():
    code = (
        "import sys; from api.agent_workbench_api import _compute_purchase_options; "
        "from config.pricing_config import get_pricing_config; "
        "\nfor line in sys.stdin:\n v,o=_compute_purchase_options(7,include_version=True); "
        "c=get_pricing_config()['agent_purchase_catalog_version']; print(f'{v}|{c}',flush=True)"
    )
    env = dict(os.environ)
    workers = [subprocess.Popen(
        [sys.executable, "-u", "-c", code], env=env, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) for _ in range(4)]
    try:
        for worker in workers:
            worker.stdin.write("read\n"); worker.stdin.flush()
        assert [worker.stdout.readline().strip() for worker in workers] == ["agent-purchase-v1|agent-purchase-v1"] * 4

        admin = request_for({"user_id": 99, "username": "admin", "is_admin": True})
        before = asyncio.run(admin_get_inventory_purchase_catalog(admin, None))
        asyncio.run(admin_put_inventory_purchase_catalog(put_payload(before), admin))
        time.sleep(2.2)  # 超过 config_epoch probe TTL，验证四个已暖进程缓存收敛。
        for worker in workers:
            worker.stdin.write("read\n"); worker.stdin.flush()
        assert [worker.stdout.readline().strip() for worker in workers] == ["agent-purchase-v2|agent-purchase-v2"] * 4
    finally:
        for worker in workers:
            worker.terminate()
            worker.wait(timeout=10)
