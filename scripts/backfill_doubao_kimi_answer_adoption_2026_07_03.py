"""Backfill answer-adoption marks for historical Doubao / Kimi research rows.

Why this exists
---------------
`services/research_monitor/platforms.py` only called `mark_answer_cited_sources`
inside `query_deepseek` / `query_qwen`, so historical `geo_research_raw` rows for
`doubao` / `kimi` were persisted with `is_answer_cited = FALSE` / `adoption_rank
= NULL` regardless of whether the answer actually cited the source.  That
systematically understated site-wide answer-adoption / explicit-citation rates
(Doubao + Kimi are ~half of all samples).

The forward fix (2026-07-03) now marks Doubao/Kimi live.  This script repairs the
already-stored rows.

Evidence rule (identical spirit to the live `mark_answer_cited_sources`)
------------------------------------------------------------------------
A stored row is upgraded to `is_answer_cited = TRUE` ONLY when there is clear
in-answer evidence: the answer text contains a `[n]` / `【n】` footnote marker
whose number matches the row's `cite_position` (the citation rank).  A plain
`cite_url` (search exposure) is NEVER treated as adoption.  Rows without a
matching marker stay `search_result_only` (is_answer_cited = FALSE).

NOTE: historical rows do NOT persist the raw provider response, so native
annotation URLs (available to the live path) cannot be replayed for backfill.
Historical backfill therefore uses the `[n]`-marker evidence only.  This can
only UNDER-count, never over-count, adoption — which is the safe direction.

Safety
------
- Read-only DRY-RUN by default; pass `--write` to actually UPDATE.
- Only flips rows toward `is_answer_cited = TRUE` where evidence exists; never
  clears an already-adopted row.
- Idempotent: the mark is a deterministic function of `answer_text` +
  `cite_position`, so re-running writes the same values.
- Scope limited to `engine IN ('doubao','kimi')` and a recent time window.

Deploy-CTO runs the `--write` pass after the boss authorizes the numbers.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402
# SSOT for what counts as an in-answer citation marker.
from services.research_monitor.platforms import _answer_citation_indices  # noqa: E402

TARGET_ENGINES = ("doubao", "kimi")


def _load_rows(days: int, limit: int) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        params: list[Any] = [list(TARGET_ENGINES)]
        window = ""
        if days > 0:
            window = "AND created_at >= NOW() - (%s || ' days')::interval"
            params.append(days)
        limit_sql = ""
        if limit > 0:
            limit_sql = "LIMIT %s"
            params.append(limit)
        cur.execute(
            f"""
            SELECT id, engine, batch_id, cite_position, cite_url,
                   answer_text, is_answer_cited, adoption_rank
              FROM geo_research_raw
             WHERE LOWER(engine) = ANY(%s)
               AND COALESCE(answer_text, '') <> ''
               {window}
             ORDER BY id
             {limit_sql}
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _compute_mark(row: dict[str, Any], answer_indices: set[int]) -> tuple[bool, int | None]:
    """Return (is_answer_cited, adoption_rank) from in-answer [n] evidence only."""
    try:
        pos = int(row.get("cite_position") or 0)
    except (TypeError, ValueError):
        pos = 0
    if pos > 0 and pos in answer_indices:
        return True, pos
    return False, None


def backfill(days: int = 20, limit: int = 0, dry_run: bool = True) -> dict[str, Any]:
    rows = _load_rows(days=days, limit=limit)

    # Cache [n] indices per distinct answer_text to avoid re-running the regex
    # for every citation row that shares the same answer.
    answer_cache: dict[str, set[int]] = {}

    scanned = 0
    determinable = 0            # rows we can upgrade to adopted (evidence present)
    undeterminable = 0          # rows staying search_result_only (no marker)
    already_marked = 0          # rows already is_answer_cited=TRUE (left untouched)
    to_write: list[tuple[int, bool, int | None]] = []
    by_engine: dict[str, dict[str, int]] = defaultdict(
        lambda: {"scanned": 0, "determinable": 0, "undeterminable": 0}
    )
    answer_groups: set[tuple[str, str, str]] = set()

    for row in rows:
        scanned += 1
        engine = (row.get("engine") or "").lower()
        by_engine[engine]["scanned"] += 1
        answer_text = row.get("answer_text") or ""
        answer_groups.add((engine, str(row.get("batch_id") or ""), answer_text[:64]))

        if answer_text not in answer_cache:
            answer_cache[answer_text] = _answer_citation_indices(answer_text)
        indices = answer_cache[answer_text]

        new_cited, new_rank = _compute_mark(row, indices)

        if bool(row.get("is_answer_cited")):
            # Already adopted (e.g. via a future path) — never clear it here.
            already_marked += 1
            continue

        if new_cited:
            determinable += 1
            by_engine[engine]["determinable"] += 1
            to_write.append((int(row["id"]), True, new_rank))
        else:
            undeterminable += 1
            by_engine[engine]["undeterminable"] += 1

    written = 0
    if not dry_run and to_write:
        conn = get_connection()
        try:
            cur = conn.cursor()
            for raw_id, cited, rank in to_write:
                cur.execute(
                    """
                    UPDATE geo_research_raw
                       SET is_answer_cited = %s,
                           adoption_rank = %s
                     WHERE id = %s
                       AND is_answer_cited = FALSE
                    """,
                    (cited, rank, raw_id),
                )
                written += cur.rowcount
            conn.commit()
        finally:
            conn.close()

    return {
        "status": "success",
        "dry_run": dry_run,
        "window_days": days,
        "engines": list(TARGET_ENGINES),
        "evidence_rule": "in-answer [n]/【n】 marker matching cite_position; cite_url alone is NOT adoption",
        "scanned_rows": scanned,
        "distinct_answers": len(answer_groups),
        "determinable_adoption_rows": determinable,
        "still_undeterminable_rows": undeterminable,
        "already_marked_rows": already_marked,
        "by_engine": {k: dict(v) for k, v in sorted(by_engine.items())},
        "would_write": len(to_write),
        "written": written,
        "note": "historical rows lack raw provider payload; native-annotation evidence "
                "is unavailable for backfill (marker-only). Under-counts, never over-counts.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=20, help="lookback window in days (0 = all history)")
    parser.add_argument("--limit", type=int, default=0, help="row cap (0 = no limit)")
    parser.add_argument("--write", action="store_true", help="actually UPDATE rows; default is dry-run")
    args = parser.parse_args()
    result = backfill(days=args.days, limit=args.limit, dry_run=not args.write)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
