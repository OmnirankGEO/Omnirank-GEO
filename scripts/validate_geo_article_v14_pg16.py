"""PostgreSQL 16 integration fixture for strict GEO article outcome lineage.

Set ``GEO_ARTICLE_V14_TEST_DSN`` to an isolated database.  Every synthetic row
is written inside one uncommitted transaction; ``load_strict_outcomes`` closes
the connection and PostgreSQL rolls the fixture back.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

import psycopg2
from psycopg2.extras import Json, RealDictCursor


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCHEMA = "geo_article_v14_full_20260719"


def _required_dsn() -> str:
    dsn = os.getenv("GEO_ARTICLE_V14_TEST_DSN", "").strip()
    if not dsn:
        raise RuntimeError("GEO_ARTICLE_V14_TEST_DSN is required; production DSNs are forbidden")
    if os.getenv("GEO_ARTICLE_V14_ALLOW_NONLOCAL_TEST_DSN", "").lower() not in {"1", "true"}:
        lowered = dsn.lower()
        if not any(host in lowered for host in ("localhost", "127.0.0.1", "host=127.0.0.1", "host=localhost")):
            raise RuntimeError("integration fixture accepts local isolated PostgreSQL only")
    return dsn


def _insert_fixture(cur) -> None:
    cur.execute("INSERT INTO quotes (brand_id, industry) VALUES (8, '制造业') RETURNING id")
    quote_id = int(cur.fetchone()["id"])
    cur.execute("INSERT INTO topics (quote_id) VALUES (%s) RETURNING id", (quote_id,))
    topic_id = int(cur.fetchone()["id"])

    article_ids: list[int] = []
    for suffix in ("mhz", "extension", "draft-only", "manual", "legacy-publish"):
        cur.execute(
            """
            INSERT INTO articles (
                topic_id, quote_id, title, content, style_code, style_family,
                article_review_status, publication_profile, created_at
            ) VALUES (%s,%s,%s,%s,'buying_guide','implementation_guide',
                      'approved','standard',CURRENT_TIMESTAMP - INTERVAL '5 days')
            RETURNING id
            """,
            (topic_id, quote_id, f"fixture-{suffix}", f"fixture body {suffix}"),
        )
        article_ids.append(int(cur.fetchone()["id"]))

    mhz_article, extension_article, draft_article, manual_article, legacy_article = article_ids
    cur.execute(
        "INSERT INTO mhz_publish_orders (article_id, article_title) VALUES (%s,'mhz') RETURNING id",
        (mhz_article,),
    )
    order_id = int(cur.fetchone()["id"])
    cur.execute(
        """
        INSERT INTO mhz_publish_order_items (
            order_id,status,publish_url,published_at,submitted_title_snapshot,
            submitted_content_snapshot,submitted_content_snapshot_hash,
            submitted_content_snapshot_at,submitted_content_snapshot_source
        ) VALUES
          (%s,'published','https://Example.com/CasePath?a=1&utm_source=x#fragment',
           CURRENT_TIMESTAMP - INTERVAL '2 days','mhz','fixture body mhz',%s,
           CURRENT_TIMESTAMP - INTERVAL '2 days','fixture_channel'),
          (%s,'rejected','https://example.com/rejected',
           CURRENT_TIMESTAMP - INTERVAL '2 days','rejected','rejected body',%s,
           CURRENT_TIMESTAMP - INTERVAL '2 days','fixture_channel')
        """,
        (order_id, "a" * 64, order_id, "b" * 64),
    )
    cur.execute(
        """
        INSERT INTO publish_records (
            article_id,status,draft_url,created_at,submitted_title_snapshot,
            submitted_content_snapshot,submitted_content_snapshot_hash,
            submitted_content_snapshot_at,submitted_content_snapshot_source,
            public_url,public_url_reported_explicitly,public_url_report_source
        ) VALUES
          (%s,'success','https://draft.invalid/2',CURRENT_TIMESTAMP - INTERVAL '2 days',
           'extension','fixture body extension',%s,CURRENT_TIMESTAMP - INTERVAL '2 days',
           'fixture_channel','https://publisher.example/Article?sku=42&utm_medium=x',TRUE,
           'extension_explicit_public_url'),
          (%s,'success','https://draft.invalid/3',CURRENT_TIMESTAMP - INTERVAL '2 days',
           'draft','fixture body draft',%s,CURRENT_TIMESTAMP - INTERVAL '2 days',
           'fixture_channel',NULL,FALSE,NULL)
        """,
        (extension_article, "c" * 64, draft_article, "d" * 64),
    )
    cur.execute(
        """
        INSERT INTO media_publications (
            quote_id,article_id,platform_url,publish_date,created_at
        ) VALUES (%s,%s,'https://manual.example/current-proxy',CURRENT_DATE - 3,
                  CURRENT_TIMESTAMP - INTERVAL '2 days')
        """,
        (quote_id, manual_article),
    )
    cur.execute(
        "INSERT INTO publish_orders (article_id, article_title, article_content) "
        "VALUES (%s,'legacy','fixture body legacy') RETURNING id",
        (legacy_article,),
    )
    legacy_order_id = int(cur.fetchone()["id"])
    cur.execute(
        """
        INSERT INTO publish_order_items (order_id,status,publish_url,published_at)
        VALUES (%s,'published','https://legacy.example/PublishedPath?sku=7&utm_source=x',
                CURRENT_TIMESTAMP - INTERVAL '2 days')
        RETURNING id
        """,
        (legacy_order_id,),
    )
    legacy_item_id = int(cur.fetchone()["id"])
    legacy_content_hash = "e" * 64
    cur.execute(
        """
        UPDATE articles
           SET publication_snapshot=%s,
               publication_snapshot_hash=%s,
               publication_snapshot_at=CURRENT_TIMESTAMP - INTERVAL '2 days',
               publication_snapshot_source='publish_order_items',
               publication_snapshot_source_id=%s
         WHERE id=%s
        """,
        (
            Json({"snapshot_version": "fixture", "content_hash": legacy_content_hash}),
            "f" * 64,
            legacy_item_id,
            legacy_article,
        ),
    )

    cur.execute("INSERT INTO monitoring_tasks (brand_id) VALUES (8) RETURNING id")
    brand_task = int(cur.fetchone()["id"])
    cur.execute("INSERT INTO monitoring_tasks (brand_id) VALUES (9) RETURNING id")
    other_task = int(cur.fetchone()["id"])
    citations = Json([
        {"url": "https://example.com/CasePath?a=1"},
        {"link": "https://publisher.example/Article?utm_source=x&sku=42#x"},
        {"url": "https://legacy.example/PublishedPath?utm_medium=x&sku=7"},
    ])
    rows = (
        (brand_task, "1 day", citations),
        (other_task, "1 day", citations),
        (brand_task, "3 days", citations),
        (brand_task, "-1 day", citations),
    )
    for task_id, age, row_citations in rows:
        cur.execute(
            """
            INSERT INTO monitoring_results (
                task_id,tested_at,search_citations,sent_question_snapshot,
                keyword_source,keyword_type,question_family,question_family_version,
                provider,model,model_revision,surface,target_outcome,lineage_status,sent_at
            ) VALUES (
                %s,CURRENT_TIMESTAMP - (%s::interval),%s,'怎么选？',
                'confirmed','purchased','recommendation_selection','question-family-v1.0',
                'dashscope','qwen-fixture','fixture','ai_search','candidate_only','complete',
                CURRENT_TIMESTAMP - (%s::interval)
            )
            """,
            (task_id, age, row_citations, age),
        )


def main() -> None:
    dsn = _required_dsn()
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    cur.execute(f"SET search_path TO {SCHEMA}, public")
    from services.geo_article_v14_schema_contract import assert_schema_ready

    assert_schema_ready(cur)
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema=%s AND table_name='publish_records'
           AND column_name IN ('public_url','public_url_reported_explicitly','submitted_content_snapshot_hash')
        """,
        (SCHEMA,),
    )
    columns = {row["column_name"]: row for row in cur.fetchall()}
    assert set(columns) == {"public_url", "public_url_reported_explicitly", "submitted_content_snapshot_hash"}
    assert columns["public_url_reported_explicitly"]["data_type"] == "boolean"
    assert columns["public_url_reported_explicitly"]["is_nullable"] == "NO"
    _insert_fixture(cur)

    import db.connection as connection_module
    from services.strict_article_outcomes import load_strict_outcomes

    original = connection_module.get_connection
    connection_module.get_connection = lambda: conn
    try:
        result = load_strict_outcomes(since_days=180)
    finally:
        connection_module.get_connection = original
        if not conn.closed:
            conn.rollback()
            conn.close()

    assert result["event_count"] == 3, result
    assert len({event["article_id"] for event in result["events"]}) == 3
    assert {event["publication_source"] for event in result["events"]} == {
        "mhz_publish_order_items", "publish_records", "publish_order_items",
    }
    legacy_events = [
        event for event in result["events"]
        if event["publication_source"] == "publish_order_items"
    ]
    assert legacy_events[0]["publication_snapshot_hash"] == "e" * 64
    assert result["quality_counts"]["publication_state_time_conflict"] == 1
    assert result["quality_counts"]["publication_time_unknown"] == 1
    assert result["quality_counts"]["prepublication_citation"] == 3
    assert result["quality_counts"]["future_monitoring"] == 1
    print("PASS | GEO article v1.4 PostgreSQL 16 rollback-only integration")


if __name__ == "__main__":
    main()
