"""Rebuild real answer-adoption metric snapshots from source signals.

This script reads geo_research_source_signals, which already separates answer
adoption, explicit citation, search exposure, and low-weight crawled reference.
It is dry-run by default; pass --full to write the shadow metric table.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.answer_adoption_metrics_db import (  # noqa: E402
    init_answer_adoption_metric_tables,
    upsert_answer_adoption_metrics,
)
from db.connection import get_connection  # noqa: E402
from services.media_entity_flywheel import normalize_industry_key  # noqa: E402
from services.research_monitor.answer_adoption_metrics import build_metric_payload  # noqa: E402


def _limit_clause(limit: int) -> tuple[str, list[Any]]:
    if limit <= 0:
        return "", []
    return "LIMIT %s", [limit]


def load_source_signal_rows(industry: str = "", limit: int = 50000) -> list[dict[str, Any]]:
    """Load deduped source signal rows; no direct geo_research_raw scan here."""
    industry_key = normalize_industry_key(industry) if industry else ""
    params: list[Any] = []
    where = "WHERE COALESCE(source_url, '') <> ''"
    if industry_key:
        where += " AND industry_key = %s"
        params.append(industry_key)
    limit_sql, limit_params = _limit_clause(limit)
    params.extend(limit_params)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            WITH ranked AS (
                SELECT id, source_url, domain, industry_key, engine,
                       COALESCE(prompt_id, '') AS prompt_id,
                       COALESCE(round_id, '') AS round_id,
                       signal_tier, source_position, total_sources_in_answer,
                       balanced_weight, observed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_url, industry_key, engine,
                                        COALESCE(prompt_id, ''),
                                        COALESCE(round_id, ''),
                                        signal_tier
                           ORDER BY observed_at DESC NULLS LAST, id DESC
                       ) AS rn
                  FROM geo_research_source_signals
                  {where}
            )
            SELECT id, source_url, domain, industry_key, engine, prompt_id,
                   round_id, signal_tier, source_position,
                   total_sources_in_answer, balanced_weight, observed_at
              FROM ranked
             WHERE rn = 1
             ORDER BY observed_at DESC NULLS LAST, id DESC
             {limit_sql}
        """, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def rebuild(industry: str = "", limit: int = 50000, dry_run: bool = True) -> dict[str, Any]:
    limit = max(0, min(int(limit or 0), 200000))
    rows = load_source_signal_rows(industry=industry, limit=limit)
    metrics = [build_metric_payload(row) for row in rows if row.get("source_url")]
    tier_counts = Counter(item["signal_tier"] for item in metrics)
    engine_counts = Counter(item["engine"] or "unknown" for item in metrics)

    written = 0
    if not dry_run:
        init_answer_adoption_metric_tables()
        written = upsert_answer_adoption_metrics(metrics)

    return {
        "status": "success",
        "dry_run": dry_run,
        "industry_key": normalize_industry_key(industry) if industry else "",
        "loaded": len(rows),
        "would_write": len(metrics),
        "written": written,
        "cap_hit": bool(limit > 0 and len(rows) >= limit),
        "limit": limit,
        "tier_counts": dict(tier_counts),
        "by_engine": dict(engine_counts),
        "preview": metrics[:20],
        "target_table": "geo_answer_adoption_metrics",
        "source_table": "geo_research_source_signals",
        "shadow_only": True,
        "production_takeover": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--industry", default="")
    parser.add_argument("--limit", type=int, default=50000, help="0 means no SQL LIMIT")
    parser.add_argument("--full", action="store_true", help="write rows; default is dry-run")
    args = parser.parse_args()
    result = rebuild(industry=args.industry, limit=args.limit, dry_run=not args.full)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
