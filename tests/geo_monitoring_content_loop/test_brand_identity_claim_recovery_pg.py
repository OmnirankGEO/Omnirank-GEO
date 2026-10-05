from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import psycopg2
import pytest
from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块
from psycopg2.extras import RealDictCursor


PG_URL = os.getenv("BRAND_IDENTITY_PG_TEST_URL", "").strip()
TEST_SCHEMA = "brand_identity_rootfix"


def _connect():
    return psycopg2.connect(
        PG_URL,
        cursor_factory=RealDictCursor,
        options=f"-c search_path={TEST_SCHEMA},public",
    )


@pytest.fixture()
def claim_tables(monkeypatch):
    if not PG_URL:
        pytest.skip("BRAND_IDENTITY_PG_TEST_URL is required")

    from db import connection, monitoring_db

    admin_conn = psycopg2.connect(PG_URL)
    try:
        admin_conn.autocommit = True
        with admin_conn.cursor() as admin_cur:
            admin_cur.execute(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}")
    finally:
        admin_conn.close()

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {TEST_SCHEMA}.brand_aliases")
        cur.execute(f"DROP TABLE IF EXISTS {TEST_SCHEMA}.client_profiles")
        cur.execute(f"DROP TABLE IF EXISTS {TEST_SCHEMA}.brands")
        cur.execute(f"DROP TABLE IF EXISTS {TEST_SCHEMA}.keyword_monitor_subscriptions")
        cur.execute(f"DROP TABLE IF EXISTS {TEST_SCHEMA}.quotes")
        # [R5 ⑤ 批2 2026-08-21] brands 走生产 SSOT 出口（手搓版比生产窄）。只换 brands 这一张表的 DDL 来源，
        #   本文件其余业务表一概不动 —— 批 1 实测证伪过「夹具改跑整个 init_db」：
        #   把 150 张表拖进只要十来张表的夹具，174 passed/0 failed 变 138 passed/34 failed。
        #   手搓版 7 列，生产 32 列；且 brand_display_names 手搓成 JSONB，
        #   生产实查是 text —— 类型不同构的夹具跑出来的绿不算数。
        ensure_brands_schema(cur)
        cur.execute(
            """
            CREATE TABLE client_profiles (
                id BIGSERIAL PRIMARY KEY,
                brand_id BIGINT NOT NULL REFERENCES brands(id),
                brand_display_names JSONB,
                is_deleted INTEGER DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE brand_aliases (
                id BIGSERIAL PRIMARY KEY,
                brand_id BIGINT NOT NULL REFERENCES brands(id),
                canonical_name TEXT,
                alias TEXT,
                source TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE keyword_monitor_subscriptions (
                id BIGINT PRIMARY KEY,
                status TEXT NOT NULL,
                last_charged_at TIMESTAMP,
                last_charge_amount INTEGER,
                total_charged INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE quotes (
                id BIGINT PRIMARY KEY,
                monitoring_last_run_at TIMESTAMP
            )
            """
        )
        previous = datetime(2026, 7, 18, 9, 0, 0)
        cur.execute(
            """
            INSERT INTO keyword_monitor_subscriptions
                (id, status, last_charged_at, total_charged)
            VALUES (596, 'active', %s, 0)
            """,
            (previous,),
        )
        cur.execute(
            "INSERT INTO quotes (id, monitoring_last_run_at) VALUES (596, %s)",
            (previous,),
        )
        cur.executemany(
            """
            INSERT INTO brands (id, name, company_name, industry)
            VALUES (%s, %s, %s, '生物技术')
            """,
            [
                (538, "浙江岱林生物技术股份有限公司", "浙江岱林生物技术股份有限公司"),
                (592, "浙江岱林生物技术股份有限公司", "浙江岱林生物技术股份有限公司"),
                (596, "浙江岱林生物技术股份有限公司", "浙江岱林生物技术股份有限公司"),
            ],
        )
        cur.execute(
            """
            INSERT INTO brand_aliases (brand_id, canonical_name, alias, source)
            VALUES (538, '浙江岱林生物技术股份有限公司', '岱林实验室', 'manual')
            """
        )
        cur.execute("INSERT INTO client_profiles (brand_id) VALUES (596)")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(monitoring_db, "get_connection", _connect)
    monkeypatch.setattr(connection, "get_connection", _connect)
    return monitoring_db, previous


def test_subscription_claim_is_single_winner_and_failed_run_can_retry(claim_tables):
    monitoring_db, previous = claim_tables

    with ThreadPoolExecutor(max_workers=20) as pool:
        claims = list(
            pool.map(
                lambda _index: monitoring_db.claim_subscription_for_today_with_token(596),
                range(20),
            )
        )
    winners = [claim for claim in claims if claim]
    assert len(winners) == 1

    claim = winners[0]
    assert claim["previous_value"] == previous
    assert monitoring_db.release_subscription_claim(
        596,
        claim["claim_token"] - timedelta(seconds=1),
        previous,
    ) is False
    assert monitoring_db.release_subscription_claim(
        596,
        claim["claim_token"],
        previous,
    ) is True
    # A recovery can die after this CAS and before settling its durable row.
    # Replaying the same release remains a success, but a newer claim below is
    # still protected by the exact timestamp fence.
    assert monitoring_db.release_subscription_claim(
        596,
        claim["claim_token"],
        previous,
    ) is True

    retry = monitoring_db.claim_subscription_for_today_with_token(596)
    assert retry is not None
    assert retry["previous_value"] == previous


def test_successful_charge_cannot_be_erased_by_late_claim_release(claim_tables):
    monitoring_db, _previous = claim_tables
    claim = monitoring_db.claim_subscription_for_today_with_token(596)
    assert claim is not None

    assert monitoring_db.record_subscription_charge(596, 130) is True
    assert monitoring_db.release_subscription_claim(
        596,
        claim["claim_token"],
        claim["previous_value"],
    ) is False

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_charge_amount, total_charged FROM keyword_monitor_subscriptions WHERE id=596"
        )
        row = cur.fetchone()
    finally:
        conn.close()
    assert row == {"last_charge_amount": 130, "total_charged": 130}


def test_quote_claim_release_is_cas_safe_and_reclaimable(claim_tables):
    monitoring_db, previous = claim_tables
    claim = monitoring_db.claim_quote_for_monitoring_with_token(596, interval_hours=24)
    assert claim is not None
    assert claim["previous_value"] == previous

    assert monitoring_db.release_quote_monitoring_claim(
        596,
        claim["claim_token"] - timedelta(seconds=1),
        previous,
    ) is False
    assert monitoring_db.release_quote_monitoring_claim(
        596,
        claim["claim_token"],
        previous,
    ) is True
    assert monitoring_db.claim_quote_for_monitoring_with_token(596, interval_hours=24)


@pytest.mark.asyncio
async def test_duplicate_brand_ids_never_borrow_manual_aliases(claim_tables, monkeypatch):
    from db import connection
    from services.brand_identity_resolver import (
        BrandIdentityResolver,
        BrandVerdict,
        VerificationResult,
        load_brand_identity,
    )

    monkeypatch.setattr(connection, "get_connection", _connect)

    identity_538 = load_brand_identity(538)
    identity_596 = load_brand_identity(596)

    assert "岱林实验室" in identity_538.trusted_aliases
    assert "岱林实验室" not in identity_596.trusted_aliases
    assert not identity_596.load_error

    async def reject_untrusted(**_kwargs):
        return VerificationResult(BrandVerdict.NO, "not-trusted-for-brand-596")

    borrowed = await BrandIdentityResolver(
        identity_596,
        verifier=reject_untrusted,
    ).resolve("推荐岱林实验室。")
    legal_short = await BrandIdentityResolver(identity_596).resolve(
        "细胞治疗设备供应商包括岱林生物（DAILIN）。"
    )

    assert borrowed.verdict.value != "YES"
    assert legal_short.verdict.value == "YES"
    assert legal_short.matched_alias == "岱林生物"


def test_confirmed_display_names_persist_only_for_the_selected_brand(claim_tables):
    from services.brand_identity_resolver import (
        load_brand_identity,
        persist_confirmed_display_names,
    )

    saved = persist_confirmed_display_names(
        596,
        ["岱林生物", "DAILIN", "岱林生物"],
    )
    assert saved == ("岱林生物", "DAILIN")

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, brand_display_names FROM brands ORDER BY id")
        brands = cur.fetchall()
        cur.execute(
            "SELECT brand_id, brand_display_names FROM client_profiles WHERE brand_id = 596"
        )
        profile = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS count FROM brand_aliases WHERE brand_id = 596")
        alias_count = cur.fetchone()["count"]
    finally:
        conn.close()

    assert brands == [
        {"id": 538, "brand_display_names": None},
        {"id": 592, "brand_display_names": None},
        {"id": 596, "brand_display_names": ["岱林生物", "DAILIN"]},
    ]
    assert profile == {
        "brand_id": 596,
        "brand_display_names": ["岱林生物", "DAILIN"],
    }
    assert alias_count == 0

    identity = load_brand_identity(596)
    assert "岱林生物" in identity.canonical_names
    assert "DAILIN" in identity.canonical_names
    assert identity.trusted_aliases == ()
    assert "岱林实验室" not in identity.trusted_aliases
