"""Rebuild trusted source signal shadow rows from GEO research tables.

This script reads existing geo_research_raw / geo_research_articles data and
writes only flywheel shadow tables.  It does not change research raw records,
media inventory, publish recommendations, or writing prompts.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.geo_source_signals_db import init_geo_source_signal_tables, upsert_source_signal  # noqa: E402
from db.connection import get_connection  # noqa: E402
from services.media_entity_flywheel import industry_filter_values, normalize_domain  # noqa: E402
from services.research_monitor.source_signal_classifier import classify_source_signal  # noqa: E402
from services.research_monitor.source_signal_weighting import weighted_signal_value  # noqa: E402


def _sha1(value: str) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def load_raw_rows(industry: str = "", limit: int = 1000) -> list[dict]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list = []
        where = """
        WHERE COALESCE(raw.cite_url, '') != ''
          AND rnd.status = 'completed'
          AND (
                NOT COALESCE(rnd.summary_json ? 'total_articles_seen', FALSE)
                OR COALESCE(
                    CASE
                      WHEN (rnd.summary_json->>'total_articles_seen') ~ '^[0-9]+$'
                      THEN (rnd.summary_json->>'total_articles_seen')::int
                      ELSE 0
                    END,
                    0
                  ) > 0
              )
        """
        if industry:
            values = industry_filter_values(industry)
            placeholders = ", ".join(["%s"] * len(values))
            where += f" AND raw.industry IN ({placeholders})"
            params.extend(values)
        params.append(limit)
        cur.execute(f"""
            SELECT raw.id, raw.industry, raw.query, raw.engine,
                   raw.cited_platform, raw.cite_position,
                   raw.cite_position AS search_rank,
                   raw.cite_url, raw.cite_title, raw.cite_excerpt,
                   raw.answer_text, raw.is_answer_cited, raw.adoption_rank,
                   raw.batch_id, raw.created_at,
                   art.review_status, art.clean_status, art.cleaned_char_count,
                   COUNT(*) OVER (
                       PARTITION BY raw.batch_id, raw.engine, raw.query
                   ) AS total_sources_in_answer
              FROM geo_research_raw raw
              JOIN geo_research_round rnd ON rnd.batch_id = raw.batch_id
              LEFT JOIN LATERAL (
                    SELECT citation.article_id
                      FROM geo_research_article_citations citation
                     WHERE citation.raw_id = raw.id
                     ORDER BY citation.id DESC
                     LIMIT 1
              ) arc ON TRUE
              LEFT JOIN geo_research_articles art ON art.id = arc.article_id
              {where}
             ORDER BY raw.created_at DESC
             LIMIT %s
        """, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def rebuild(industry: str = "", limit: int = 1000, dry_run: bool = False) -> dict:
    if not dry_run:
        init_geo_source_signal_tables()
    rows = load_raw_rows(industry=industry, limit=limit)
    written = 0
    preview = []
    tier_counts: Counter[str] = Counter()
    for row in rows:
        signal = classify_source_signal(row)
        if not signal.source_url:
            continue
        tier_counts[signal.signal_tier] += 1
        payload = {
            "source_url": signal.source_url,
            "url_hash": _sha1(signal.source_url),
            "domain": normalize_domain(signal.source_url),
            "industry_key": signal.industry_key,
            "engine": signal.engine,
            "prompt_id": signal.prompt_id,
            "signal_tier": signal.signal_tier,
            "source_position": signal.source_position,
            "total_sources_in_answer": signal.total_sources_in_answer,
            "balanced_weight": weighted_signal_value(signal),
            "answer_mentioned_brand": signal.answer_mentioned_brand,
            "round_id": row.get("batch_id") or "",
            "metadata": signal.metadata or {},
        }
        if dry_run:
            preview.append(payload)
        else:
            upsert_source_signal(payload)
            written += 1
    return {
        "status": "success",
        "dry_run": dry_run,
        "loaded": len(rows),
        "written": written,
        "tier_counts": dict(tier_counts),
        "preview": preview[:20],
        "shadow_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--industry", default="")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(rebuild(industry=args.industry, limit=args.limit, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
