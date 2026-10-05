"""Regression tests for the independent review findings on retail SKU decoupling."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest
from fastapi import HTTPException, Request
from psycopg2.extras import RealDictCursor

from api.admin_factory_api import (
    AdminSyncAgentPricesRequest,
    admin_pricing_sync_agent_prices,
)
from api.agent_workbench_api import agent_pricing_sku_create
from schemas.v35_w2_dto import AgentSKUCreateRequest
from services import (
    account_codes,
    agent_pricing,
    pricing_catalog,
    pricing_publication,
    pricing_readiness,
)
from services.agent_retail_sku_schema import schema_status
from services.pricing_publication import lock_retail_management_sources
from api.wallet_api import (
    _assert_legacy_retail_snapshot_payable,
    _lock_legacy_template_sku,
)
from helpers import set_flags


def _request(user_id: int, *, is_admin: bool = False) -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.user = {"user_id": user_id, "is_admin": is_admin}
    return request


def _migration(name: str) -> str:
    return (Path(__file__).resolve().parents[2] / "scripts" / name).read_text(
        encoding="utf-8"
    )


def test_startup_default_tuple_cursor_accepts_valid_retail_schema():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        status = schema_status(conn.cursor())
    finally:
        conn.close()
    assert status == {"ready": True, "blockers": []}


def test_forward_and_compatibility_rollback_keep_old_writer_working():
    migration = _migration("migration_agent_retail_sku_decoupling_2026_07_17.sql")
    rollback = _migration("rollback_agent_retail_sku_decoupling_2026_07_17.sql")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    try:
        cur = conn.cursor()
        old_insert = """
            INSERT INTO agent_sku_overrides
                (agent_user_id, sku_template_id, custom_name, retail_cents, is_active)
            VALUES (100, 1, NULL, 180000, TRUE)
            RETURNING retail_sku_id, points_granted, custom_name
        """
        cur.execute(old_insert)
        forward_row = cur.fetchone()
        assert forward_row[0].startswith("RSKU-")
        assert int(forward_row[1]) == 195000
        assert forward_row[2] == "基础算力包"

        cur.execute(rollback)
        cur.execute(old_insert)
        rollback_row = cur.fetchone()
        assert rollback_row[0].startswith("RSKU-")
        assert int(rollback_row[1]) == 195000
        cur.execute(migration)
    finally:
        conn.close()


def test_rerun_does_not_materialize_future_provider_or_template():
    migration = _migration("migration_agent_retail_sku_decoupling_2026_07_17.sql")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO users(id,phone,is_active) VALUES (777,'review-777',1)")
        cur.execute("INSERT INTO user_wallets(user_id,agent_level) VALUES (777,1)")
        cur.execute(
            """INSERT INTO sku_templates
                   (id,template_code,sku_type,default_name,points_granted,
                    wholesale_cents,suggested_retail_cents,is_active)
               VALUES (777,'REVIEW777','credit_pack','未来模板',777,500,900,TRUE)"""
        )
        cur.execute(migration)
        cur.execute(
            """SELECT COUNT(*) FROM agent_sku_overrides
                WHERE agent_user_id=777 OR source_template_id=777"""
        )
        assert int(cur.fetchone()[0]) == 0
    finally:
        conn.close()


def test_rollback_forward_does_not_repeat_initial_fallback_freeze():
    migration = _migration("migration_agent_retail_sku_decoupling_2026_07_17.sql")
    rollback = _migration("rollback_agent_retail_sku_decoupling_2026_07_17.sql")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM _migrations WHERE name=%s",
            ("agent_retail_sku_initial_fallback_freeze_2026_07_17",),
        )
        assert cur.fetchone() == (1,)
        cur.execute(rollback)
        cur.execute("INSERT INTO users(id,phone,is_active) VALUES (778,'review-778',1)")
        cur.execute("INSERT INTO user_wallets(user_id,agent_level) VALUES (778,1)")
        cur.execute(
            """INSERT INTO sku_templates
                   (id,template_code,sku_type,default_name,points_granted,
                    wholesale_cents,suggested_retail_cents,is_active)
               VALUES (778,'REVIEW778','credit_pack','回滚后模板',778,500,900,TRUE)"""
        )
        cur.execute(migration)
        cur.execute(
            """SELECT COUNT(*) FROM agent_sku_overrides
                WHERE agent_user_id=778 OR source_template_id=778"""
        )
        assert int(cur.fetchone()[0]) == 0
    finally:
        conn.close()


def test_schema_readiness_rejects_noop_legacy_compatibility_function(raw_conn):
    cur = raw_conn.cursor()
    cur.execute(
        """CREATE OR REPLACE FUNCTION agent_retail_sku_legacy_fill()
           RETURNS TRIGGER AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql"""
    )
    tampered = schema_status(cur)
    assert tampered["ready"] is False
    assert any("函数 agent_retail_sku_legacy_fill" in item for item in tampered["blockers"])

    raw_conn.rollback()
    restored = schema_status(raw_conn.cursor())
    assert restored == {"ready": True, "blockers": []}


def test_source_template_provenance_cannot_take_over_legacy_identity(raw_conn):
    cur = raw_conn.cursor()
    cur.execute(
        """INSERT INTO agent_sku_overrides
               (agent_user_id,sku_template_id,source_template_id,retail_sku_id,
                points_granted,custom_name,retail_cents,is_active,sort_order,
                version,deleted_at)
           VALUES
               (100,1,1,'RSKU-OLD-LEGACY-0001',195000,'已下架旧包',180000,FALSE,0,1,NOW()),
               (100,NULL,1,'RSKU-NEW-SOURCE-0001',12000,'全新独立包',10000,TRUE,0,1,NULL)"""
    )
    assert agent_pricing.get_sku_for_customer(cur, 100, 1) is None
    with pytest.raises(HTTPException) as stopped:
        _lock_legacy_template_sku(cur, agent_user_id=100, sku_template_id=1)
    assert stopped.value.status_code == 400


def test_quote_off_legacy_path_rejects_unsettleable_custom_terms():
    with pytest.raises(HTTPException) as rejected:
        _assert_legacy_retail_snapshot_payable(
            agent_user_id=100,
            points_granted=3_000_000_000,
            retail_cents=1,
        )
    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "LEGACY_SKU_QUOTE_REQUIRED"

    cost = _assert_legacy_retail_snapshot_payable(
        agent_user_id=100,
        points_granted=12000,
        retail_cents=10000,
    )
    assert 0 < cost <= 10000


def test_global_retail_writer_fence_serializes_cross_provider_dml():
    def write(agent_user_id: int) -> str:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        conn.cursor_factory = RealDictCursor
        try:
            cur = conn.cursor()
            cur.execute("SET lock_timeout='8s'")
            lock_retail_management_sources(cur)
            cur.execute(
                """INSERT INTO agent_sku_overrides
                       (agent_user_id,retail_sku_id,points_granted,custom_name,
                        retail_cents,is_active,sort_order,version)
                   VALUES (%s,%s,12000,'并发锁序',10000,TRUE,0,1)""",
                (agent_user_id, f"RSKU-LOCK-{agent_user_id:08d}"),
            )
            conn.commit()
            return "committed"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (100, 200)))
    assert results == ["committed", "committed"]


def test_two_providers_can_mutate_and_publish_without_deadlock():
    account_codes.prepare_account_codes(service_user_ids=[100, 200], channel_user_ids=[])
    set_flags(dual=True, quote_required=False, channel=False)

    def create(agent_user_id: int) -> dict:
        request = AgentSKUCreateRequest(
            client_request_id=f"concurrent-provider-{agent_user_id}",
            display_name=f"服务商 {agent_user_id} 安全包",
            points_granted=12000,
            retail_cents=10000,
        )
        return asyncio.run(agent_pricing_sku_create(request, _request(agent_user_id)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(create, (100, 200)))
    assert all(row["success"] for row in rows)
    assert all(row["published_catalog_version"] for row in rows)


def test_platform_suggested_price_sync_is_disabled_with_dual_on():
    set_flags(dual=True, quote_required=False, channel=False)
    with pytest.raises(HTTPException) as stopped:
        asyncio.run(
            admin_pricing_sync_agent_prices(
                AdminSyncAgentPricesRequest(dry_run=False, only_loss=True),
                _request(900, is_admin=True),
            )
        )
    assert stopped.value.status_code == 410
    assert stopped.value.detail["code"] == "PLATFORM_SUGGESTED_PRICE_SYNC_DISABLED"
    with psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS count FROM agent_sku_overrides")
        assert int(cur.fetchone()["count"]) == 0
        cur.execute("SELECT COUNT(*) AS count FROM pricing_catalog_versions")
        assert int(cur.fetchone()["count"]) == 0


def test_platform_suggested_price_sync_is_disabled_with_dual_off():
    set_flags(dual=False, quote_required=False, channel=False)
    with pytest.raises(HTTPException) as stopped:
        asyncio.run(
            admin_pricing_sync_agent_prices(
                AdminSyncAgentPricesRequest(dry_run=False, only_loss=True),
                _request(900, is_admin=True),
            )
        )
    assert stopped.value.status_code == 410
    with psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS count FROM agent_sku_overrides")
        assert int(cur.fetchone()["count"]) == 0
        cur.execute("SELECT COUNT(*) AS count FROM pricing_catalog_versions")
        assert int(cur.fetchone()["count"]) == 0


def test_publication_drift_ready_false_blocks_readiness(monkeypatch):
    class CatalogCursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return {
                "id": 1,
                "version_code": "legacy-v1",
                "calc_meta_jsonb": {"source_fingerprint": "old"},
                "entry_count": 1,
            }

    monkeypatch.setattr(
        pricing_publication,
        "publication_status",
        lambda cur=None: {
            "ready": False,
            "needs_publication_count": 2,
            "blockers": [],
            "scopes": [{"action": "publish"}],
        },
    )
    result = pricing_readiness._check_catalogs(
        CatalogCursor(), {"service_accounts": []}
    )
    assert result["ready"] is False
    assert any("重新发布" in problem for problem in result["problems"])
