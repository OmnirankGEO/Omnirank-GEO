"""WORKERS=4 concurrency: 20 concurrent aggregate refreshes converge to exactly
one valid version per cell (unique aggregate_key + deterministic idempotent
upsert), with correct final values."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from services.geo_observation_analytics import aggregates as A

pytestmark = pytest.mark.integration

DAY = date(2026, 7, 10)
CT = datetime(2026, 7, 11, tzinfo=timezone.utc)
URL = os.getenv("TEST_DATABASE_URL", "")


def _worker(_i: int) -> None:
    conn = psycopg2.connect(URL)
    try:
        A.refresh_scope(
            conn, scope_type="public_industry", granularity="day", day=DAY,
            computed_at=CT,
        )
    finally:
        conn.close()


def test_20_concurrent_refreshes_exactly_one_version(seeder, db_conn):
    for _ in range(12):
        seeder.observation(outcome="recommended", processing_state="promoted")
    for _ in range(4):
        seeder.observation(outcome="not_mentioned", processing_state="promoted")
    for _ in range(3):
        seeder.observation(outcome="refused_no_evidence", processing_state="promoted")
    db_conn.commit()

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(_worker, range(20)))

    with db_conn.cursor(cursor_factory=RealDictCursor) as cur:
        # exactly one row per aggregate_key (no duplicate versions)
        cur.execute(
            "SELECT aggregate_key, COUNT(*) AS n FROM geo_observation_aggregates "
            "GROUP BY aggregate_key HAVING COUNT(*) > 1"
        )
        assert cur.fetchall() == []

        # the overall public row is present, exactly once, with correct counts
        cur.execute(
            """SELECT valid_observations, recommended_count, not_mentioned_count,
                      refused_no_evidence_count, presence_rate_bps
               FROM geo_observation_aggregates
               WHERE scope_type='public_industry' AND platform_key IS NULL
                 AND surface_key IS NULL AND source_type IS NULL
                 AND is_branded_prompt IS NULL AND prompt_intent IS NULL
                 AND search_enabled IS NULL"""
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["valid_observations"] == 19  # 12 + 4 + 3
    assert r["recommended_count"] == 12
    assert r["not_mentioned_count"] == 4
    assert r["refused_no_evidence_count"] == 3
    assert r["presence_rate_bps"] == round(12 / 19 * 10000)
