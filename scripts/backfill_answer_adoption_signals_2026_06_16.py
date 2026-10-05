"""Backfill legacy GEO raw rows with answer-adoption markers.

This script only updates geo_research_raw.is_answer_cited and
geo_research_raw.adoption_rank.  It is dry-run by default; pass --full to write.
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
from services.media_entity_flywheel import industry_filter_values  # noqa: E402
from services.research_monitor.answer_adoption import infer_answer_adoption  # noqa: E402


def _load_rows(industry: str, limit: int) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = []
        limit_sql = ""
        where = """
        WHERE raw.researcher = 'legacy_csv_import'
          AND COALESCE(raw.answer_text, '') <> ''
          AND COALESCE(raw.cite_position, 0) > 0
        """
        if industry:
            values = industry_filter_values(industry)
            placeholders = ", ".join(["%s"] * len(values))
            where += f" AND raw.industry IN ({placeholders})"
            params.extend(values)
        if limit > 0:
            limit_sql = "LIMIT %s"
            params.append(limit)
        cur.execute(f"""
            SELECT raw.id, raw.industry, raw.engine, raw.query,
                   raw.answer_text, raw.cite_position, raw.is_answer_cited,
                   raw.adoption_rank, raw.cite_url
              FROM geo_research_raw raw
              {where}
             ORDER BY raw.id
             {limit_sql}
        """, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def backfill(industry: str = "", limit: int = 50000, full: bool = False) -> dict[str, Any]:
    requested_limit = 50000 if limit is None else int(limit)
    limit = max(0, min(requested_limit, 200000))
    rows = _load_rows(industry=industry, limit=limit)
    by_reason: Counter[str] = Counter()
    by_engine: Counter[str] = Counter()
    by_industry: Counter[str] = Counter()
    sample_updates: list[dict[str, Any]] = []
    updates: list[tuple[bool, int | None, int]] = []

    for row in rows:
        inferred = infer_answer_adoption(row)
        is_answer_cited = bool(inferred["is_answer_cited"])
        adoption_rank = inferred["adoption_rank"]
        by_reason[str(inferred["reason"])] += 1
        by_engine[str(row.get("engine") or "unknown")] += 1
        by_industry[str(row.get("industry") or "unknown")] += 1

        current_flag = bool(row.get("is_answer_cited"))
        current_rank = row.get("adoption_rank")
        if current_flag != is_answer_cited or current_rank != adoption_rank:
            updates.append((is_answer_cited, adoption_rank, int(row["id"])))
            if len(sample_updates) < 20:
                sample_updates.append({
                    "id": row.get("id"),
                    "industry": row.get("industry"),
                    "engine": row.get("engine"),
                    "cite_position": row.get("cite_position"),
                    "is_answer_cited": is_answer_cited,
                    "adoption_rank": adoption_rank,
                    "reason": inferred["reason"],
                    "cite_url": row.get("cite_url") or "",
                })

    written = 0
    if full and updates:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.executemany("""
                UPDATE geo_research_raw
                   SET is_answer_cited = %s,
                       adoption_rank = %s
                 WHERE id = %s
            """, updates)
            written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(updates)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return {
        "status": "success",
        "dry_run": not full,
        "limit": limit,
        "cap_hit": bool(limit > 0 and len(rows) >= limit),
        "loaded": len(rows),
        "would_update": len(updates),
        "written": written,
        "by_reason": dict(by_reason),
        "by_engine": dict(by_engine),
        "by_industry": dict(by_industry),
        "sample_updates": sample_updates,
        "target_table": "geo_research_raw",
        "updated_fields": ["is_answer_cited", "adoption_rank"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--industry", default="")
    parser.add_argument("--limit", type=int, default=50000, help="0 means no SQL LIMIT")
    parser.add_argument("--full", action="store_true", help="write updates; omitted means dry-run")
    args = parser.parse_args()
    print(json.dumps(backfill(industry=args.industry, limit=args.limit, full=args.full), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
