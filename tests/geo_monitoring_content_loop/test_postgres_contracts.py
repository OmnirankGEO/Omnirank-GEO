from __future__ import annotations

import os
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import pytest


@pytest.fixture(scope="module")
def throwaway_dsn() -> str:
    dsn = os.getenv("QA_THROWAWAY_DATABASE_URL", "")
    if not dsn:
        pytest.skip("QA_THROWAWAY_DATABASE_URL is required for isolated PostgreSQL contracts")
    parsed = urlparse(dsn)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port == 55449
    assert parsed.path == "/omnirank_test"
    return dsn


@pytest.fixture()
def clean_throwaway_db(throwaway_dsn: str):
    conn = psycopg2.connect(throwaway_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS publish_idempotency_keys")
        cur.execute("DROP TABLE IF EXISTS keyword_compliance_log")
        cur.execute("DROP TABLE IF EXISTS topics")
        cur.execute("DROP TABLE IF EXISTS confirmed_keywords")
        cur.execute("DROP TABLE IF EXISTS quotes")
        cur.execute("DROP TABLE IF EXISTS brand_image_assets")
    conn.close()
    yield throwaway_dsn
    conn = psycopg2.connect(throwaway_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS publish_idempotency_keys")
        cur.execute("DROP TABLE IF EXISTS keyword_compliance_log")
        cur.execute("DROP TABLE IF EXISTS topics")
        cur.execute("DROP TABLE IF EXISTS confirmed_keywords")
        cur.execute("DROP TABLE IF EXISTS quotes")
        cur.execute("DROP TABLE IF EXISTS brand_image_assets")
    conn.close()


def test_bulk_image_transaction_risk_skip_and_idempotency(clean_throwaway_db, monkeypatch):
    from db import brand_image_assets_db as image_db

    dsn = clean_throwaway_db
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE brand_image_assets (
                id BIGINT PRIMARY KEY,
                brand_id INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL,
                risk_flags JSONB,
                publish_allowed SMALLINT DEFAULT 0,
                rights_confirmed SMALLINT DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            INSERT INTO brand_image_assets
                (id, brand_id, status, risk_flags, publish_allowed, rights_confirmed)
            VALUES (1, 7, 'active', '[]', 0, 0),
                   (2, 7, 'active', '["qrcode"]', 0, 0),
                   (3, 7, 'archived', '[]', 0, 0)
            """
        )
    conn.close()

    monkeypatch.setattr(image_db, "get_connection", lambda: psycopg2.connect(dsn))
    first = image_db.batch_set_article_usage([1, 2, 3], enabled=True)
    repeat = image_db.batch_set_article_usage([1, 2, 3], enabled=True)
    disabled = image_db.batch_set_article_usage([1], enabled=False)

    assert first["updated"] == 1
    assert first["skipped"] == 2
    assert any("qrcode" in item["reason"] for item in first["items"])
    assert repeat["updated"] == 0
    assert repeat["skipped"] == 3
    assert disabled["updated"] == 1

    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, publish_allowed, rights_confirmed FROM brand_image_assets ORDER BY id"
        )
        assert cur.fetchall() == [(1, 0, 1), (2, 0, 0), (3, 0, 0)]
    conn.close()


def test_supplement_preview_row_lock_hash_and_idempotency_key(
    clean_throwaway_db,
    monkeypatch,
):
    from services.smart_article_supplement import build_supplement_preview_from_db
    import db.wallet_db

    monkeypatch.setattr(
        db.wallet_db,
        "get_feature_pricing",
        lambda _feature: {"feature_code": "article_gen", "cost_points": 390},
    )

    dsn = clean_throwaway_db
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE quotes (
                id BIGINT PRIMARY KEY,
                industry TEXT,
                tier TEXT,
                writing_status TEXT DEFAULT 'titles_generating',
                deleted_at TIMESTAMP
            );
            CREATE TABLE confirmed_keywords (
                id BIGINT PRIMARY KEY,
                keyword TEXT NOT NULL,
                quote_id BIGINT NOT NULL REFERENCES quotes(id),
                required_articles INTEGER NOT NULL
            );
            CREATE TABLE topics (
                id BIGSERIAL PRIMARY KEY,
                keyword_id BIGINT NOT NULL,
                quote_id BIGINT,
                optimized_title TEXT,
                user_choice TEXT,
                style_code TEXT,
                article_style TEXT,
                status TEXT,
                is_optimize BOOLEAN DEFAULT TRUE,
                is_fixed BOOLEAN DEFAULT FALSE,
                user_choice_source TEXT
            );
            CREATE TABLE keyword_compliance_log (
                keyword_id BIGINT,
                keyword_source TEXT,
                quote_id BIGINT,
                check_date DATE,
                effective_rate NUMERIC,
                detection_rate NUMERIC,
                target_rate NUMERIC
            );
            CREATE TABLE publish_idempotency_keys (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                response_json JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
            """
        )
        cur.execute("INSERT INTO quotes VALUES (3, '酒店', 'standard', 'titles_generating', NULL)")
        cur.execute("INSERT INTO confirmed_keywords VALUES (9, '酒店推荐', 3, 4)")
        cur.execute(
            """
            INSERT INTO keyword_compliance_log
                (keyword_id, keyword_source, quote_id, check_date,
                 effective_rate, detection_rate, target_rate)
            VALUES (9, 'confirmed', 3, CURRENT_DATE, 15, 15, 60)
            """
        )

    before = build_supplement_preview_from_db(conn, 9, for_update=True)
    assert before.reason_code == "plan_gap"
    assert before.suggested_articles == 4
    assert before.target_rate == 65
    conn.commit()

    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO topics
                (keyword_id, user_choice, style_code, article_style, status, is_fixed,
                 user_choice_source)
            VALUES (9, 'guide', 'method_guide', 'method_guide', 'titles_ready', FALSE,
                    'batch_distribution')
            """
        )
    after = build_supplement_preview_from_db(conn, 9, for_update=True)
    assert after.suggested_articles == 3
    assert after.plan_hash != before.plan_hash
    conn.commit()

    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO topics
                (keyword_id, user_choice, style_code, article_style, status, is_fixed,
                 user_choice_source)
            VALUES (9, 'comparison', 'comparison_review', 'comparison_review', 'failed', FALSE,
                    'batch_distribution')
            """
        )
    with_failed_slot = build_supplement_preview_from_db(conn, 9, for_update=True)
    assert with_failed_slot.suggested_articles == 2
    assert with_failed_slot.plan_hash != after.plan_hash
    conn.commit()

    with conn, conn.cursor() as cur:
        params = ("writing-supplement:request-1", 8, "writing.optimize-generate.v2")
        cur.execute(
            """
            INSERT INTO publish_idempotency_keys
                (request_id, user_id, endpoint, response_json)
            VALUES (%s, %s, %s, '{}'::jsonb)
            ON CONFLICT (request_id) DO NOTHING
            RETURNING request_id
            """,
            params,
        )
        assert cur.fetchone()["request_id"] == "writing-supplement:request-1"
        cur.execute(
            """
            INSERT INTO publish_idempotency_keys
                (request_id, user_id, endpoint, response_json)
            VALUES (%s, %s, %s, '{}'::jsonb)
            ON CONFLICT (request_id) DO NOTHING
            RETURNING request_id
            """,
            params,
        )
        assert cur.fetchone() is None
    conn.commit()
    conn.close()


def test_quote_title_state_waits_for_every_failed_retry(clean_throwaway_db):
    from services.optimize_title_jobs import _update_quote_writing_status

    dsn = clean_throwaway_db
    conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE quotes (id BIGINT PRIMARY KEY, writing_status TEXT NOT NULL);
            CREATE TABLE topics (
                id BIGINT PRIMARY KEY,
                quote_id BIGINT NOT NULL,
                optimized_title TEXT,
                status TEXT NOT NULL,
                fail_reason TEXT,
                is_optimize BOOLEAN NOT NULL DEFAULT TRUE
            );
            INSERT INTO quotes VALUES (3, 'titles_generating');
            INSERT INTO topics VALUES
                (1, 3, '有效标题', 'pending', NULL, TRUE),
                (2, 3, '标题生成中...', 'failed', '可重试', TRUE),
                (3, 3, '标题生成中...', 'failed', '可重试', TRUE),
                (4, 3, '标题生成中...', 'failed', '可重试', TRUE);
            """
        )
        _update_quote_writing_status(cur, 3, generated_count=1, failed_count=2)
        cur.execute("SELECT writing_status FROM quotes WHERE id=3")
        assert cur.fetchone()["writing_status"] == "pending"

        # Retrying only one failed slot must not hide the other unresolved slot.
        cur.execute("UPDATE topics SET optimized_title='重试成功', status='pending' WHERE id=2")
        _update_quote_writing_status(cur, 3, generated_count=1, failed_count=0)
        cur.execute("SELECT writing_status FROM quotes WHERE id=3")
        assert cur.fetchone()["writing_status"] == "pending"

        cur.execute("UPDATE topics SET optimized_title='全部重试成功', status='pending' WHERE id IN (3, 4)")
        _update_quote_writing_status(cur, 3, generated_count=1, failed_count=0)
        cur.execute("SELECT writing_status FROM quotes WHERE id=3")
        assert cur.fetchone()["writing_status"] == "titles_ready"

        # The endpoint's conditional UPDATE is the atomic double-click gate.
        cur.execute(
            "INSERT INTO topics VALUES (5, 3, '标题生成中...', 'failed', '可重试', TRUE)"
        )
        retry_sql = """
            UPDATE topics SET status='regenerating', optimized_title='标题生成中...', fail_reason=NULL
            WHERE id=%s AND is_optimize=TRUE AND status='failed'
        """
        cur.execute(retry_sql, (5,))
        assert cur.rowcount == 1
        cur.execute(retry_sql, (5,))
        assert cur.rowcount == 0
    conn.close()
