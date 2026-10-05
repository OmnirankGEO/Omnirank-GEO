"""Consolidate similar profile memory events without deleting audit history."""

from __future__ import annotations

import argparse
import re
from typing import Any


def _norm(text: Any) -> str:
    value = str(text or "").lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[，。！？；：、“”‘’\"'`~|,.!?;:()（）\[\]{}]+", "", value)
    value = value.replace("到", "-")
    return value[:500]


def _tokens(text: Any) -> set[str]:
    value = _norm(text)
    if not value:
        return set()
    grams = {value[i : i + 2] for i in range(max(1, len(value) - 1)) if value[i : i + 2].strip()}
    grams.update({ch for ch in value if ch.strip()})
    return grams


def similarity(a: Any, b: Any) -> float:
    left = _tokens(a)
    right = _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def find_memory_consolidation_candidates(rows: list[dict], *, threshold: float = 0.85) -> list[dict]:
    """Return merge plans grouped by canonical_concept.

    The first matching row is kept; later similar rows are soft-archived into it.
    """
    plans: list[dict] = []
    by_concept: dict[str, list[dict]] = {}
    for row in rows:
        concept = str(row.get("canonical_concept") or "business_identity")
        by_concept.setdefault(concept, []).append(row)

    for concept, concept_rows in by_concept.items():
        used: set[int] = set()
        for row in concept_rows:
            row_id = int(row.get("id") or 0)
            if not row_id or row_id in used:
                continue
            merge_ids: list[int] = []
            for other in concept_rows:
                other_id = int(other.get("id") or 0)
                if not other_id or other_id == row_id or other_id in used:
                    continue
                score = similarity(row.get("text") or row.get("title"), other.get("text") or other.get("title"))
                if score >= threshold:
                    merge_ids.append(other_id)
                    used.add(other_id)
            if merge_ids:
                used.add(row_id)
                plans.append({"concept": concept, "keep_id": row_id, "merge_ids": merge_ids})
    return plans


def consolidate_profile_memory(profile_id: str, *, dry_run: bool = True, threshold: float = 0.85, limit: int = 200) -> dict:
    from db.profile_memory_db import list_profile_memory_events, _get_conn

    rows = list_profile_memory_events(
        profile_id,
        limit=limit,
        active_only=True,
        prompt_safe=True,
        review_statuses=("approved", "auto"),
    )
    plans = find_memory_consolidation_candidates(rows, threshold=threshold)
    if dry_run or not plans:
        return {"profile_id": profile_id, "dry_run": dry_run, "merged": 0, "plans": plans}

    conn = _get_conn()
    try:
        cur = conn.cursor()
        merged_count = 0
        for plan in plans:
            keep_id = int(plan["keep_id"])
            merge_ids = [int(item) for item in plan["merge_ids"]]
            if not merge_ids:
                continue
            cur.execute(
                """
                UPDATE profile_memory_events
                SET importance_score = LEAST(10, COALESCE(importance_score, 5) + 1),
                    access_count = COALESCE(access_count, 0) + %s,
                    notes = COALESCE(notes || E'\n', '') || %s
                WHERE id = %s
                """,
                (len(merge_ids), f"consolidated duplicates: {merge_ids}", keep_id),
            )
            cur.execute(
                """
                UPDATE profile_memory_events
                SET is_active = FALSE,
                    review_status = 'dismissed',
                    reviewed_at = NOW(),
                    notes = COALESCE(notes || E'\n', '') || %s
                WHERE id = ANY(%s)
                """,
                (f"merged into memory event {keep_id}", merge_ids),
            )
            merged_count += len(merge_ids)
        conn.commit()
        return {"profile_id": profile_id, "dry_run": False, "merged": merged_count, "plans": plans}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--threshold", type=float, default=0.85)
    args = parser.parse_args()
    result = consolidate_profile_memory(args.profile_id, dry_run=not args.apply, threshold=args.threshold)
    print(result)


if __name__ == "__main__":
    main()
