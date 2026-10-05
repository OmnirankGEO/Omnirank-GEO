"""Two-connection PostgreSQL barrier test for human review/edit ordering."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from psycopg2 import sql


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURE = (ROOT / "scripts/fixtures/geo_article_v14_pg16_base.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "scripts/migration_geo_article_v14_2026_07_19.sql").read_text(encoding="utf-8")
MIGRATION = MIGRATION.replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


def _dsn() -> str:
    value = os.getenv("GEO_ARTICLE_V14_TEST_DSN", "").strip()
    if not value:
        raise RuntimeError("GEO_ARTICLE_V14_TEST_DSN is required")
    lowered = value.lower()
    if "127.0.0.1" not in lowered and "localhost" not in lowered:
        raise RuntimeError("concurrency validator accepts only local isolated PostgreSQL")
    return value


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _connect(dsn: str, schema: str):
    return psycopg2.connect(
        dsn,
        cursor_factory=RealDictCursor,
        options=f"-c search_path={schema},public",
    )


def _review(content: str, evidence_hash: str) -> dict:
    return {
        "review_version": "concurrency-fixture-v1",
        "reviewed_content_hash": _hash(content),
        "reviewed_evidence_manifest_hash": evidence_hash,
    }


class _BarrierCursor:
    def __init__(self, cursor, locked: threading.Event, proceed: threading.Event):
        self._cursor = cursor
        self._locked = locked
        self._proceed = proceed

    def execute(self, query, params=None):
        result = self._cursor.execute(query, params)
        if "FOR UPDATE" in str(query).upper():
            self._locked.set()
            if not self._proceed.wait(10):
                raise TimeoutError("approval barrier timed out")
        return result

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _BarrierConnection:
    def __init__(self, conn, locked: threading.Event, proceed: threading.Event):
        self._conn = conn
        self._locked = locked
        self._proceed = proceed

    def cursor(self):
        return _BarrierCursor(self._conn.cursor(), self._locked, self._proceed)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _insert_article(conn, content: str, evidence_hash: str) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO articles (
            title, content, style_code, style_family, article_review_status,
            article_human_review_status, article_review, evidence_manifest_hash,
            publication_profile, current_content_hash
        ) VALUES (
            '并发审核夹具', %s, 'buying_guide', 'implementation_guide',
            'pending_human_review', NULL, %s, %s, 'standard', %s
        ) RETURNING id
        """,
        (content, Json(_review(content, evidence_hash)), evidence_hash, _hash(content)),
    )
    article_id = int(cur.fetchone()["id"])
    conn.commit()
    return article_id


def _edit(conn, article_id: int, content: str, evidence_hash: str) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE articles
           SET content=%s, current_content_hash=%s, evidence_manifest_hash=%s,
               article_review=%s, article_review_status='pending_human_review',
               article_human_review_status=NULL,
               article_human_reviewed_by=NULL,
               article_human_reviewed_at=NULL,
               article_human_review_reason=NULL
         WHERE id=%s
        """,
        (content, _hash(content), evidence_hash, Json(_review(content, evidence_hash)), article_id),
    )
    conn.commit()


def main() -> None:
    dsn = _dsn()
    schema = f"geo_v14_review_race_{uuid4().hex[:8]}"
    setup = psycopg2.connect(dsn)
    setup.autocommit = True
    try:
        cur = setup.cursor()
        cur.execute(FIXTURE.replace("geo_article_v14_full_20260719", schema))
        cur.execute(MIGRATION)

        import db.connection as connection_module
        from services.article_review_gate import evaluate_publication_eligibility, set_human_review

        os.environ["GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED"] = "true"
        original_get_connection = connection_module.get_connection
        try:
            # Approval locks first. The editor attempts the UPDATE, waits, then
            # clears the just-written human state while installing new review.
            old_content, new_content = "旧正文：公开资料、风险与核验步骤。", "新正文：公开资料、限制与复核路径。"
            old_evidence, new_evidence = "a" * 64, "b" * 64
            initial = _connect(dsn, schema)
            article_id = _insert_article(initial, old_content, old_evidence)
            initial.close()

            locked = threading.Event()
            proceed = threading.Event()
            editor_attempted = threading.Event()
            approval_conn = _BarrierConnection(_connect(dsn, schema), locked, proceed)
            connection_module.get_connection = lambda: approval_conn
            errors: list[BaseException] = []

            def approve():
                try:
                    set_human_review(
                        article_id,
                        reviewer_user_id=7,
                        decision="approved",
                        reason="并发签发夹具已核验",
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            def edit():
                conn = _connect(dsn, schema)
                try:
                    assert locked.wait(10)
                    editor_attempted.set()
                    _edit(conn, article_id, new_content, new_evidence)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                finally:
                    conn.close()

            approval_thread = threading.Thread(target=approve)
            editor_thread = threading.Thread(target=edit)
            approval_thread.start()
            editor_thread.start()
            assert locked.wait(10) and editor_attempted.wait(10)
            proceed.set()
            approval_thread.join(10)
            editor_thread.join(10)
            assert not approval_thread.is_alive() and not editor_thread.is_alive()
            assert errors == [], errors

            verify = _connect(dsn, schema)
            vcur = verify.cursor()
            vcur.execute(
                "SELECT content, article_human_review_status FROM articles WHERE id=%s",
                (article_id,),
            )
            row = vcur.fetchone()
            assert row["content"] == new_content
            assert row["article_human_review_status"] is None
            verify.close()

            # Edit commits first. The later approval must bind exactly the new
            # content/evidence hashes, and eligibility verifies that event.
            second = _connect(dsn, schema)
            article_id_2 = _insert_article(second, old_content, old_evidence)
            _edit(second, article_id_2, new_content, new_evidence)
            second.close()
            connection_module.get_connection = lambda: _connect(dsn, schema)
            set_human_review(
                article_id_2,
                reviewer_user_id=8,
                decision="approved",
                reason="编辑后重新核验签发",
            )
            final = _connect(dsn, schema)
            fcur = final.cursor()
            eligibility = evaluate_publication_eligibility(article_id_2, cursor=fcur)
            assert eligibility["eligible"] is True
            assert eligibility["reason"] == "human_approved_snapshot_match"
            fcur.execute(
                """
                SELECT reviewed_content_hash, evidence_manifest_hash
                  FROM geo_article_review_events
                 WHERE article_id=%s AND decision='approved'
                 ORDER BY id DESC LIMIT 1
                """,
                (article_id_2,),
            )
            event = fcur.fetchone()
            assert event["reviewed_content_hash"].strip() == _hash(new_content)
            assert event["evidence_manifest_hash"].strip() == new_evidence
            final.close()
        finally:
            connection_module.get_connection = original_get_connection
        print("PASS | human review/edit two-connection serialization")
    finally:
        if schema.startswith("geo_v14_review_race_"):
            setup.cursor().execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        setup.close()


if __name__ == "__main__":
    main()
