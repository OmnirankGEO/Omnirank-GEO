"""Adversarial PostgreSQL 16 validation for the v1.4 schema contract.

Every scenario uses a unique transaction-local schema and rolls back.  The
script refuses non-local DSNs unless explicitly overridden.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
from uuid import uuid4

import psycopg2


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURE = (ROOT / "scripts/fixtures/geo_article_v14_pg16_base.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "scripts/migration_geo_article_v14_2026_07_19.sql").read_text(encoding="utf-8")
MIGRATION = MIGRATION.replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


def _dsn() -> str:
    value = os.getenv("GEO_ARTICLE_V14_TEST_DSN", "").strip()
    if not value:
        raise RuntimeError("GEO_ARTICLE_V14_TEST_DSN is required")
    if os.getenv("GEO_ARTICLE_V14_ALLOW_NONLOCAL_TEST_DSN", "").lower() not in {"1", "true"}:
        lowered = value.lower()
        if "127.0.0.1" not in lowered and "localhost" not in lowered:
            raise RuntimeError("only a local isolated PostgreSQL is accepted")
    return value


def _base(cur, schema: str) -> None:
    cur.execute(FIXTURE.replace("geo_article_v14_full_20260719", schema))


def _ready(cur) -> list[str]:
    from services.geo_article_v14_schema_contract import schema_blockers

    return schema_blockers(cur)


def _scenario(name: str, callback) -> None:
    schema = f"geo_v14_{name}_{uuid4().hex[:8]}"
    conn = psycopg2.connect(_dsn())
    try:
        cur = conn.cursor()
        _base(cur, schema)
        callback(cur)
        conn.rollback()
        print(f"PASS | {name}")
    finally:
        if not conn.closed:
            conn.close()


def _fresh_2x_and_runtime_link(cur) -> None:
    cur.execute(MIGRATION)
    assert _ready(cur) == []
    cur.execute("INSERT INTO topics DEFAULT VALUES RETURNING id")
    topic_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO articles(topic_id,title,content) VALUES (%s,'safe','safe body') RETURNING id",
        (topic_id,),
    )
    article_id = cur.fetchone()[0]
    cur.execute("UPDATE topics SET article_id=%s WHERE id=%s", (article_id, topic_id))
    cur.execute(
        "SELECT a.id FROM topics t JOIN articles a ON a.id=t.article_id WHERE t.id=%s",
        (topic_id,),
    )
    assert cur.fetchone()[0] == article_id
    cur.execute("SELECT 1 FROM article_generations WHERE id=%s", (article_id,))
    assert cur.fetchone() is None

    # Once the exact canonical FK exists, equal integers in the independent
    # legacy sequence are not ambiguous on a rerun.
    cur.execute("INSERT INTO articles(id,title,content) VALUES (801,'canonical','body')")
    cur.execute("INSERT INTO article_generations(id,content) VALUES (801,'legacy same id')")
    cur.execute("INSERT INTO topics(article_id) VALUES (801) RETURNING id")
    canonical_topic_id = cur.fetchone()[0]
    cur.execute(MIGRATION)
    assert _ready(cur) == []
    cur.execute(
        "SELECT article_id, legacy_article_generation_id FROM topics WHERE id=%s",
        (canonical_topic_id,),
    )
    assert cur.fetchone() == (801, None)


def _partial_table_repair(cur) -> None:
    cur.execute("CREATE TABLE geo_article_review_events (id BIGSERIAL PRIMARY KEY)")
    cur.execute("CREATE TABLE geo_article_gold_labels (id BIGSERIAL PRIMARY KEY)")
    cur.execute(MIGRATION)
    assert _ready(cur) == []


def _wrong_type_blocks(cur) -> None:
    cur.execute("CREATE TABLE geo_article_review_events (id BIGSERIAL PRIMARY KEY, decision INTEGER)")
    try:
        cur.execute(MIGRATION)
    except Exception as exc:
        assert "decision" in str(exc).lower() or "type" in str(exc).lower()
        return
    blockers = _ready(cur)
    assert any(item.startswith("wrong_type:geo_article_review_events.decision") for item in blockers)


def _historical_mixed_values_separate(cur) -> None:
    cur.execute("ALTER TABLE topics ADD COLUMN article_id INTEGER")
    cur.execute("INSERT INTO article_generations(id, content) VALUES (501, 'legacy')")
    cur.execute("INSERT INTO articles(id,title,content) VALUES (601,'current','body')")
    cur.execute("INSERT INTO topics(article_id) VALUES (501), (601)")
    cur.execute(MIGRATION)
    assert _ready(cur) == []
    cur.execute(
        "SELECT article_id, legacy_article_generation_id FROM topics ORDER BY id"
    )
    rows = cur.fetchall()
    assert rows[0] == (None, 501)
    assert rows[1] == (601, None)


def _ambiguous_historical_value_blocks(cur) -> None:
    cur.execute("ALTER TABLE topics ADD COLUMN article_id INTEGER")
    cur.execute("INSERT INTO article_generations(id, content) VALUES (701, 'legacy')")
    cur.execute("INSERT INTO articles(id,title,content) VALUES (701,'current','body')")
    cur.execute("INSERT INTO topics(article_id) VALUES (701)")
    try:
        cur.execute(MIGRATION)
    except Exception as exc:
        assert "NEEDS_PROD" in str(exc) and "both articles and article_generations" in str(exc)
        return
    raise AssertionError("ambiguous historical topics.article_id must fail closed")


def _legacy_fk_same_id_uses_legacy_semantics(cur) -> None:
    cur.execute("ALTER TABLE topics ADD COLUMN article_id INTEGER")
    cur.execute("INSERT INTO article_generations(id, content) VALUES (802, 'legacy')")
    cur.execute("INSERT INTO articles(id,title,content) VALUES (802,'canonical same id','body')")
    cur.execute("INSERT INTO topics(article_id) VALUES (802) RETURNING id")
    topic_id = cur.fetchone()[0]
    cur.execute(
        "ALTER TABLE topics ADD CONSTRAINT topics_article_id_legacy_fk "
        "FOREIGN KEY (article_id) REFERENCES article_generations(id)"
    )
    cur.execute(MIGRATION)
    assert _ready(cur) == []
    cur.execute(
        "SELECT article_id, legacy_article_generation_id FROM topics WHERE id=%s",
        (topic_id,),
    )
    assert cur.fetchone() == (None, 802)


def _expect_bad_article_fk(cur, ddl: str) -> None:
    cur.execute("ALTER TABLE topics ADD COLUMN article_id INTEGER")
    cur.execute(ddl)
    try:
        cur.execute(MIGRATION)
    except Exception as exc:
        assert "GEO_V14_BAD_TOPICS_FK_SEMANTICS" in str(exc)
        return
    raise AssertionError("malformed topics.article_id FK must fail closed")


def _both_article_fks_block(cur) -> None:
    _expect_bad_article_fk(
        cur,
        "ALTER TABLE topics ADD CONSTRAINT topic_article_canonical_fk "
        "FOREIGN KEY (article_id) REFERENCES articles(id); "
        "ALTER TABLE topics ADD CONSTRAINT topic_article_legacy_fk "
        "FOREIGN KEY (article_id) REFERENCES article_generations(id)",
    )


def _composite_article_fk_blocks(cur) -> None:
    cur.execute(
        "CREATE TABLE article_pairs (id INTEGER, quote_id BIGINT, PRIMARY KEY(id, quote_id))"
    )
    _expect_bad_article_fk(
        cur,
        "ALTER TABLE topics ADD CONSTRAINT topic_article_composite_fk "
        "FOREIGN KEY (article_id, quote_id) REFERENCES article_pairs(id, quote_id)",
    )


def _other_article_fk_blocks(cur) -> None:
    _expect_bad_article_fk(
        cur,
        "ALTER TABLE topics ADD CONSTRAINT topic_article_other_fk "
        "FOREIGN KEY (article_id) REFERENCES quotes(id)",
    )


def _wrong_action_article_fk_blocks(cur) -> None:
    _expect_bad_article_fk(
        cur,
        "ALTER TABLE topics ADD CONSTRAINT topic_article_cascade_fk "
        "FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE",
    )


def _unvalidated_article_fk_blocks(cur) -> None:
    _expect_bad_article_fk(
        cur,
        "ALTER TABLE topics ADD CONSTRAINT topic_article_unvalidated_fk "
        "FOREIGN KEY (article_id) REFERENCES articles(id) NOT VALID",
    )


def _deferred_article_fk_blocks(cur) -> None:
    _expect_bad_article_fk(
        cur,
        "ALTER TABLE topics ADD CONSTRAINT topic_article_deferred_fk "
        "FOREIGN KEY (article_id) REFERENCES articles(id) DEFERRABLE INITIALLY DEFERRED",
    )


def _null_default_repaired(cur) -> None:
    cur.execute(MIGRATION)
    cur.execute("ALTER TABLE geo_article_review_events ALTER COLUMN created_at DROP NOT NULL")
    cur.execute("ALTER TABLE geo_article_review_events ALTER COLUMN created_at DROP DEFAULT")
    assert _ready(cur)
    cur.execute(MIGRATION)
    assert _ready(cur) == []


def _weak_check_blocks(cur) -> None:
    cur.execute(MIGRATION)
    cur.execute("ALTER TABLE geo_article_review_events DROP CONSTRAINT ck_geo_article_review_decision")
    cur.execute(
        "ALTER TABLE geo_article_review_events ADD CONSTRAINT ck_geo_article_review_decision CHECK (decision <> '')"
    )
    cur.execute(MIGRATION)
    assert any(item.startswith("weak_constraint:ck_geo_article_review_decision") for item in _ready(cur))


def _wrong_fk_is_repaired(cur) -> None:
    cur.execute(MIGRATION)
    cur.execute("ALTER TABLE topics DROP CONSTRAINT topics_article_id_articles_fk")
    cur.execute(
        "ALTER TABLE topics ADD CONSTRAINT topics_article_id_articles_fk "
        "FOREIGN KEY (article_id) REFERENCES article_generations(id)"
    )
    cur.execute(MIGRATION)
    assert _ready(cur) == []


def _wrong_fk_delete_action_blocks(cur) -> None:
    cur.execute(MIGRATION)
    cur.execute("ALTER TABLE geo_research_article_fetches DROP CONSTRAINT fk_geo_fetch_article")
    cur.execute(
        "ALTER TABLE geo_research_article_fetches ADD CONSTRAINT fk_geo_fetch_article "
        "FOREIGN KEY (article_id) REFERENCES geo_research_articles(id) ON DELETE CASCADE"
    )
    cur.execute(MIGRATION)
    assert any(
        item.startswith("wrong_fk_delete_action:fk_geo_fetch_article")
        for item in _ready(cur)
    )


def _wrong_index_blocks(cur) -> None:
    cur.execute(MIGRATION)
    cur.execute("DROP INDEX idx_geo_article_review_events_article")
    cur.execute(
        "CREATE INDEX idx_geo_article_review_events_article "
        "ON geo_article_review_events(created_at, article_id)"
    )
    cur.execute(MIGRATION)
    assert any(item.startswith("wrong_index:idx_geo_article_review_events_article") for item in _ready(cur))


def _wrong_predicate_blocks(cur) -> None:
    cur.execute(MIGRATION)
    cur.execute("DROP INDEX idx_articles_publication_snapshot")
    cur.execute(
        "CREATE INDEX idx_articles_publication_snapshot ON articles(publication_snapshot_at) "
        "WHERE publication_snapshot_at IS NULL"
    )
    cur.execute(MIGRATION)
    assert any(item.startswith("wrong_index:idx_articles_publication_snapshot") for item in _ready(cur))


def _decoy_constraint_blocks(cur) -> None:
    cur.execute(MIGRATION)
    cur.execute("ALTER TABLE geo_article_review_events DROP CONSTRAINT ck_geo_article_review_decision")
    cur.execute("CREATE TABLE decoy (decision VARCHAR(40))")
    cur.execute(
        "ALTER TABLE decoy ADD CONSTRAINT ck_geo_article_review_decision "
        "CHECK (decision IN ('approved','rejected'))"
    )
    cur.execute(MIGRATION)
    blockers = _ready(cur)
    assert any(
        item.startswith("wrong_constraint:ck_geo_article_review_decision")
        or item.startswith("constraint_count:ck_geo_article_review_decision")
        for item in blockers
    )


def _rollback_then_forward() -> None:
    # First transaction proves a rolled-back attempt leaves no schema.  The
    # second transaction then applies the same migration successfully.
    schema = f"geo_v14_rollback_{uuid4().hex[:8]}"
    conn = psycopg2.connect(_dsn())
    cur = conn.cursor()
    _base(cur, schema)
    cur.execute(MIGRATION)
    conn.rollback()
    cur = conn.cursor()
    _base(cur, schema)
    cur.execute(MIGRATION)
    assert _ready(cur) == []
    conn.rollback()
    conn.close()
    print("PASS | rollback_then_forward")


def main() -> None:
    _scenario("fresh_2x_link", _fresh_2x_and_runtime_link)
    _rollback_then_forward()
    _scenario("partial_repair", _partial_table_repair)
    _scenario("wrong_type", _wrong_type_blocks)
    _scenario("historical_mixed", _historical_mixed_values_separate)
    _scenario("historical_ambiguous", _ambiguous_historical_value_blocks)
    _scenario("legacy_fk_same_id", _legacy_fk_same_id_uses_legacy_semantics)
    _scenario("both_article_fks", _both_article_fks_block)
    _scenario("composite_article_fk", _composite_article_fk_blocks)
    _scenario("other_article_fk", _other_article_fk_blocks)
    _scenario("wrong_action_article_fk", _wrong_action_article_fk_blocks)
    _scenario("unvalidated_article_fk", _unvalidated_article_fk_blocks)
    _scenario("deferred_article_fk", _deferred_article_fk_blocks)
    _scenario("null_default", _null_default_repaired)
    _scenario("weak_check", _weak_check_blocks)
    _scenario("wrong_fk", _wrong_fk_is_repaired)
    _scenario("wrong_fk_delete_action", _wrong_fk_delete_action_blocks)
    _scenario("wrong_index", _wrong_index_blocks)
    _scenario("wrong_predicate", _wrong_predicate_blocks)
    _scenario("decoy_constraint", _decoy_constraint_blocks)
    print("PASS | GEO article v1.4 exact schema adversarial suite")


if __name__ == "__main__":
    main()
