"""Promote JC3 Jina bodies only from hash-bound direct AI observations."""
from __future__ import annotations

from typing import Any, Final

from psycopg2.extras import Json

from .corpus_contract import validate_jc5_promotion


LABELER_VERSION: Final = "jina-jc5-direct-labeler-v1.0"


def promote_jc5_from_direct_signals(*, dry_run: bool = True, limit: int = 500) -> dict[str, Any]:
    """Use exact body-hash/fetch lineage; URL-only matches can never promote."""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH eligible_signals AS (
                SELECT a.id AS article_id,
                       COUNT(DISTINCT s.id) AS signal_count,
                       COUNT(DISTINCT s.provider) AS provider_count,
                       ARRAY_AGG(DISTINCT s.id ORDER BY s.id) AS signal_ids
                  FROM geo_research_articles a
                  JOIN geo_research_source_signals s
                    ON s.article_id=a.id
                  LEFT JOIN geo_research_article_fetches f
                    ON f.id=s.article_fetch_id
                 WHERE a.corpus_grade='JC3'
                   AND a.canonical_body_hash IS NOT NULL
                   AND a.content_cluster_id IS NOT NULL
                   AND s.signal_tier IN ('answer_adopted','cited_source')
                   AND s.label_provenance_type='direct_observation'
                   AND s.lineage_status='complete'
                   AND (
                        s.article_snapshot_hash=a.canonical_body_hash
                        OR (s.article_fetch_id IS NOT NULL AND f.body_hash=a.canonical_body_hash)
                   )
                 GROUP BY a.id
                 ORDER BY COUNT(DISTINCT s.id) DESC, a.id
                 LIMIT %s
            )
            SELECT * FROM eligible_signals
            """,
            (max(1, min(int(limit), 5000)),),
        )
        candidates = [dict(row) for row in cur.fetchall()]
        for candidate in candidates:
            validate_jc5_promotion(
                direct_signal_count=int(candidate.get("signal_count") or 0),
                lineage_complete=True,
            )
        if dry_run:
            conn.rollback()
            return {
                "labeler_version": LABELER_VERSION,
                "dry_run": True,
                "eligible": len(candidates),
                "candidates": candidates,
                "url_only_promotions": 0,
            }

        promoted = 0
        for candidate in candidates:
            cur.execute(
                """
                UPDATE geo_research_articles
                   SET corpus_grade='JC5',
                       label_provenance_version=%s
                 WHERE id=%s AND corpus_grade='JC3'
                RETURNING id
                """,
                (LABELER_VERSION, candidate["article_id"]),
            )
            if not cur.fetchone():
                continue
            cur.execute(
                """
                INSERT INTO geo_research_corpus_label_events (
                    article_id, from_grade, to_grade, labeler_version,
                    direct_signal_count, evidence
                ) VALUES (%s,'JC3','JC5',%s,%s,%s)
                ON CONFLICT (article_id, labeler_version) DO NOTHING
                """,
                (
                    candidate["article_id"], LABELER_VERSION,
                    candidate["signal_count"],
                    Json({
                        "signal_ids": candidate.get("signal_ids") or [],
                        "provider_count": int(candidate.get("provider_count") or 0),
                        "match_rule": "exact_body_hash_or_exact_fetch_body_hash",
                    }),
                ),
            )
            promoted += 1
        conn.commit()
        return {
            "labeler_version": LABELER_VERSION,
            "dry_run": False,
            "eligible": len(candidates),
            "promoted": promoted,
            "url_only_promotions": 0,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
