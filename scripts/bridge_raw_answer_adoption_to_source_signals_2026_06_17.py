"""Bridge raw answer-adoption evidence into source-signal shadow rows.

R5 metrics read geo_research_source_signals.  Some historical/prod batches have
is_answer_cited/adoption_rank in geo_research_raw but no corresponding source
signal rows.  This script fills that bridge only; it is dry-run by default and
writes only geo_research_source_signals when --full is passed.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402
from db.geo_source_signals_db import init_geo_source_signal_tables, upsert_source_signal  # noqa: E402
from services.media_entity_flywheel import industry_filter_values, normalize_industry_key  # noqa: E402
from services.research_monitor.raw_adoption_bridge import (  # noqa: E402
    build_raw_adoption_source_signal_payload,
)


MAX_LIMIT = 200000
DEFAULT_LIMIT = 50000


def _limit_clause(limit: int) -> tuple[str, list[Any]]:
    if limit <= 0:
        return "", []
    return "LIMIT %s", [limit]


def _normalize_limit(limit: int) -> int:
    if limit <= 0:
        return 0
    return min(int(limit), MAX_LIMIT)


def load_raw_adoption_rows(
    industry: str = "",
    batch_id: str = "",
    researcher: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Load raw rows with explicit answer-adoption evidence.

    The window count is calculated before filtering to adopted rows, so source
    fairness uses the full answer source-list size rather than only adopted
    sources.
    """
    params: list[Any] = []
    where = "WHERE COALESCE(raw.cite_url, '') <> ''"
    if industry:
        values = industry_filter_values(industry)
        placeholders = ", ".join(["%s"] * len(values))
        where += f" AND raw.industry IN ({placeholders})"
        params.extend(values)
    if batch_id:
        where += " AND raw.batch_id = %s"
        params.append(batch_id)
    if researcher is not None:
        where += " AND COALESCE(raw.researcher, '') = %s"
        params.append(researcher)

    limit = _normalize_limit(limit)
    limit_sql, limit_params = _limit_clause(limit)
    params.extend(limit_params)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            WITH scoped_raw AS (
                SELECT raw.id, raw.industry, raw.query, raw.engine,
                       raw.cited_platform, raw.cite_position,
                       raw.cite_position AS search_rank,
                       raw.cite_url, raw.cite_title, raw.cite_excerpt,
                       raw.answer_text, raw.is_answer_cited, raw.adoption_rank,
                       raw.batch_id, raw.researcher, raw.created_at,
                       COUNT(*) OVER (
                           PARTITION BY raw.batch_id, raw.engine, raw.query
                       ) AS total_sources_in_answer
                  FROM geo_research_raw raw
                  {where}
            )
            SELECT id, industry, query, engine, cited_platform, cite_position,
                   search_rank, cite_url, cite_title, cite_excerpt, answer_text,
                   is_answer_cited, adoption_rank, batch_id, researcher,
                   created_at, total_sources_in_answer
              FROM scoped_raw
             WHERE COALESCE(is_answer_cited, FALSE) = TRUE
                OR adoption_rank IS NOT NULL
             ORDER BY created_at DESC NULLS LAST, id DESC
             {limit_sql}
        """, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def bridge(
    industry: str = "",
    batch_id: str = "",
    researcher: str | None = None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = True,
) -> dict[str, Any]:
    limit = _normalize_limit(limit)
    rows = load_raw_adoption_rows(
        industry=industry,
        batch_id=batch_id,
        researcher=researcher,
        limit=limit,
    )
    payloads = [build_raw_adoption_source_signal_payload(row) for row in rows]
    tier_counts = Counter(item["signal_tier"] for item in payloads)
    engine_counts = Counter(item["engine"] or "unknown" for item in payloads)
    batch_counts = Counter(item["round_id"] or "unknown" for item in payloads)

    written = 0
    if not dry_run:
        init_geo_source_signal_tables()
        for payload in payloads:
            upsert_source_signal(payload)
            written += 1

    return {
        "status": "success",
        "dry_run": dry_run,
        "industry_key": normalize_industry_key(industry) if industry else "",
        "batch_id": batch_id,
        "researcher": researcher,
        "loaded": len(rows),
        "would_write": len(payloads),
        "written": written,
        "cap_hit": bool(limit > 0 and len(rows) >= limit),
        "limit": limit,
        "tier_counts": dict(tier_counts),
        "by_engine": dict(engine_counts),
        "by_batch": dict(batch_counts),
        "preview": payloads[:20],
        "source_table": "geo_research_raw",
        "target_table": "geo_research_source_signals",
        "shadow_only": True,
        "production_takeover": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--industry", default="")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--researcher", default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="0 means no SQL LIMIT")
    parser.add_argument("--full", action="store_true", help="write rows; default is dry-run")
    args = parser.parse_args()

    result = bridge(
        industry=args.industry,
        batch_id=args.batch_id,
        researcher=args.researcher,
        limit=args.limit,
        dry_run=not args.full,
    )
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
