"""Isolated PostgreSQL fixtures for GEO Observation Analytics (AI-3) tests.

Requires a throwaway PostgreSQL whose database name contains ``test``:

    TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55690/geo_observation_test

The schema built here comes only from the executable production migrations.
There is deliberately no second analytics reference DDL that can drift from
the startup/migration SSOT. These tests never touch production.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import pytest

from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块

# make `_support` importable by test modules regardless of package layout
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[2]
# AI-2's current executable migration — tests must exercise the same startup
# truth schema (gold gate included), not the earlier frozen contract projection.
AI2_MIGRATION_SQL = (
    ROOT / "scripts" / "migration_geo_observation_v1_2026_07_17.sql"
).read_text(encoding="utf-8")
COLLECTION_MODE_SQL = (
    ROOT / "scripts" / "migration_geo_observation_collection_mode_2026_07_20.sql"
).read_text(encoding="utf-8")
AGGREGATE_BASIS_SQL = (
    ROOT / "scripts" / "migration_geo_observation_aggregate_basis_2026_07_20.sql"
).read_text(encoding="utf-8")

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

# Minimal app tables AI-3 reads for RBAC (owner_user_id) + private evidence —
# NOT owned by AI-3; mirrors the real columns the reads use.
BRANDS_SCHEMA = """
-- brands: 见 fixture 里的 ensure_brands_schema()（生产 SSOT 出口），不在这里手搓。
CREATE TABLE IF NOT EXISTS monitoring_tasks (
    id INTEGER PRIMARY KEY,
    brand_id INTEGER
);
CREATE TABLE IF NOT EXISTS monitoring_results (
    id INTEGER PRIMARY KEY,
    task_id INTEGER,
    keyword TEXT,
    platform TEXT,
    is_detected SMALLINT DEFAULT 0,
    mention_type TEXT,
    response_snippet TEXT,
    full_response TEXT,
    tested_at TIMESTAMPTZ DEFAULT NOW()
);
"""

# truncated per-test; leave geo_observation_policy (config singleton) + audit +
# _migration_markers intact.
_OBS_TABLES = [
    # Receipts survive output/manifest deletion by design. Reset them first so
    # one test cannot make a later honest-empty scope look like a corrupted
    # previously-published scope.
    "geo_observation_aggregate_bucket_revision",
    "geo_observation_insight_jobs",
    "geo_observation_aggregate_refresh_manifest",
    "geo_observation_aggregates",
    "geo_observation_contributor_buckets",
    "geo_observation_signals",
    "geo_observation_events",
    "monitoring_results",
    "monitoring_tasks",
    "brands",
]


def _assert_safe_test_database() -> None:
    if not TEST_DATABASE_URL:
        raise RuntimeError(
            "TEST_DATABASE_URL is required for GEO observation analytics PostgreSQL "
            "tests (e.g. postgresql://postgres:test@127.0.0.1:55690/geo_observation_test)"
        )
    database_name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if "test" not in database_name:
        raise RuntimeError(f"unsafe TEST_DATABASE_URL database name: {database_name!r}")


@pytest.fixture(scope="session", autouse=True)
def isolated_postgres_schema():
    _assert_safe_test_database()
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
        # AI-2's REAL migration, applied TWICE (proves its 2x idempotency + runs
        # its own reverse-lookup assertion block). AI-3 binds to this schema.
        cur.execute(AI2_MIGRATION_SQL)
        cur.execute(AI2_MIGRATION_SQL)
        # Production prestart runs collection-mode before aggregate-basis.
        # Keep the analytics fixture on that exact manifest order so scheduler
        # wiring cannot look green against a schema missing collection_mode.
        cur.execute(COLLECTION_MODE_SQL)
        cur.execute(COLLECTION_MODE_SQL)
        # Aggregate-basis migration owns analytics support too; run twice.
        cur.execute(AGGREGATE_BASIS_SQL)
        cur.execute(AGGREGATE_BASIS_SQL)
        # [R5 ⑤ 2026-08-20] brands 走生产 SSOT 出口（手搓版 6 列 vs 生产 32 列）
        ensure_brands_schema(cur)
        cur.execute(BRANDS_SCHEMA)
    conn.close()

    from db import connection

    connection.close_pool()
    connection.DATABASE_URL = TEST_DATABASE_URL
    yield
    connection.close_pool()


@pytest.fixture(autouse=True)
def reset_tables(isolated_postgres_schema):
    conn = psycopg2.connect(TEST_DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(_OBS_TABLES) + " RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()
    yield


@pytest.fixture()
def db_conn():
    conn = psycopg2.connect(TEST_DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


class ObsSeeder:
    """Insert promoted (or any-state) observations = event + signal + bucket."""

    def __init__(self, conn):
        self.conn = conn
        self._n = 0

    def brand(self, brand_id: int, owner_user_id: int, name: str = "示例品牌",
              industry: str = "enterprise-service", is_deleted: bool = False) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO brands(id,name,owner_user_id,industry,status,is_deleted) "
                "VALUES (%s,%s,%s,%s,'active',%s) ON CONFLICT (id) DO UPDATE SET "
                "owner_user_id=EXCLUDED.owner_user_id",
                (brand_id, name, owner_user_id, industry, is_deleted),
            )
        self.conn.commit()
        return brand_id

    def observation(
        self,
        *,
        outcome: str,
        processing_state: str = "promoted",
        industry_key: str = "enterprise-service",
        owner_user_id: Optional[int] = None,
        brand_id: Optional[int] = None,
        platform_key: str = "doubao",
        surface_key: str = "doubao_ark_api_search",
        provider_key: str = "volcengine",
        model_key: str = "doubao-pro",
        model_revision: Optional[str] = "r1",
        source_type: str = "recurring_monitoring",
        prompt_family_key: str = "fam-a",
        prompt_intent: str = "category_recommendation",
        is_branded_prompt: bool = False,
        response_status: str = "answered",
        target_position: Optional[int] = None,
        competitor_count: int = 0,
        citation_count: int = 0,
        source_count: int = 0,
        source_domains: Optional[list] = None,
        brand_source_visible: bool = False,   # translated to source_count>=1
        evidence_verifiable: bool = False,    # translated to quality_score_bps high
        search_enabled: Optional[bool] = True,
        search_query_theme_keys: Optional[list] = None,
        base_weight_bps: int = 7000,
        effective_weight_bps: int = 7000,
        quality_score_bps: int = 2000,
        confidence_bps: int = 8000,
        user_bucket: Optional[str] = None,   # default: a DISTINCT vote per obs
        brand_bucket: Optional[str] = None,
        bucket_key_version: int = 1,
        contribution_date: str = "2026-07-10",
        observed_at: Optional[datetime] = None,
        run_index: int = 1,
        withdrawn: bool = False,
    ) -> int:
        self._n += 1
        n = self._n
        if user_bucket is None:
            user_bucket = f"u{n}"
        if brand_bucket is None:
            brand_bucket = f"b{n}"
        if observed_at is None:
            observed_at = datetime(2026, 7, 10, 8, 0, 0, tzinfo=timezone.utc)
        import json as _json

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO geo_observation_events(
                    event_uuid, source_type, source_table, source_record_id, source_subkey,
                    source_event_key, owner_user_id, brand_id, industry_key,
                    platform_key, provider_key, model_key, model_revision, surface_key,
                    search_enabled, session_mode, run_index, processing_state,
                    promotion_legal_basis, observed_at, withdrawn_at)
                VALUES (%s,%s,'src','rec-%s',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'clean',%s,%s,'lb-approved',%s,%s)
                RETURNING id
                """,
                (
                    str(uuid.uuid4()), source_type, n, f"sub-{n}",
                    hashlib.sha256(f"evt-{n}-{outcome}".encode()).hexdigest(),
                    owner_user_id, brand_id, industry_key,
                    platform_key, provider_key, model_key, model_revision, surface_key,
                    search_enabled, run_index, processing_state,
                    observed_at,
                    observed_at if withdrawn else None,
                ),
            )
            event_id = cur.fetchone()[0]
            # translate the two convenience flags onto REAL signal columns:
            #   source_visibility derives from source_count>0; evidence coverage
            #   derives from quality_score_bps (or citation_count>0).
            if brand_source_visible:
                source_count = max(source_count, 1)
            if evidence_verifiable:
                quality_score_bps = max(quality_score_bps, 6000)
            cur.execute(
                """
                INSERT INTO geo_observation_signals(
                    event_id, industry_key, prompt_family_key, prompt_intent, is_branded_prompt,
                    platform_key, provider_key, model_key, model_revision, surface_key,
                    response_status, target_outcome, target_position, sentiment,
                    competitor_count, source_domains, citation_count, source_count,
                    search_query_count, search_query_theme_keys, quality_score_bps, base_weight_bps,
                    effective_weight_bps, confidence_bps, observed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'neutral',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    event_id, industry_key, prompt_family_key, prompt_intent, is_branded_prompt,
                    platform_key, provider_key, model_key, model_revision, surface_key,
                    response_status, outcome, target_position,
                    competitor_count, _json.dumps(source_domains or []), citation_count, source_count,
                    None, _json.dumps(search_query_theme_keys or []), quality_score_bps,
                    base_weight_bps, effective_weight_bps, confidence_bps, observed_at,
                ),
            )
            # AI-2's one-vote门:同 (user,brand,family,platform,day) 只允许一个桶;
            # 其余同键观测是"稳定度样本",有 signal 但无 contributor bucket。忠实复现。
            cur.execute(
                """
                INSERT INTO geo_observation_contributor_buckets(
                    event_id, contributor_user_bucket, contributor_brand_bucket,
                    bucket_key_version, contribution_date, prompt_family_key, platform_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (contributor_user_bucket, contributor_brand_bucket,
                             prompt_family_key, platform_key, contribution_date) DO NOTHING
                """,
                (event_id, user_bucket, brand_bucket, bucket_key_version,
                 contribution_date, prompt_family_key, platform_key),
            )
        self.conn.commit()
        return event_id


@pytest.fixture()
def seeder(db_conn):
    return ObsSeeder(db_conn)