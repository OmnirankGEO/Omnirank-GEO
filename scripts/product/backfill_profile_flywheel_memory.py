"""Backfill flywheel v2 interview state into profile_memory_events.

Dry-run by default. Apply requires both --apply and the admin flag
backfill_profile_flywheel_enabled=true unless --ignore-flag is passed.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from db.connection import get_connection
from db.profile_memory_db import (
    infer_canonical_concept,
    make_event_fingerprint,
    record_profile_memory_event,
)

logger = logging.getLogger("BackfillProfileFlywheelMemory")


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _clean(value: Any, limit: int = 500) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _flag_enabled() -> bool:
    try:
        from db.social_preferences_db import get_admin_setting
        return bool(get_admin_setting("backfill_profile_flywheel_enabled", bool, False))
    except Exception:
        return False


def _iter_sessions(limit: int) -> list[dict]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT session_id, user_id, state_json, updated_at
            FROM interview_sessions
            WHERE state_json LIKE %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            ('%"_v2"%', max(1, int(limit or 1000))),
        )
        return [dict(row) for row in (cur.fetchall() or [])]
    finally:
        conn.close()


def _events_from_session(row: dict) -> list[dict]:
    state = _as_dict(row.get("state_json"))
    profile_id = _clean(state.get("profile_id"), 64)
    if not profile_id:
        return []
    v2 = _as_dict((_as_dict(state.get("collected_data"))).get("_v2"))
    if not v2:
        return []
    session_id = _clean(row.get("session_id"), 160)
    events: list[dict] = []
    for detail in v2.get("accumulated_details") or []:
        if not isinstance(detail, dict):
            continue
        label = _clean(detail.get("label") or "建档细节", 80)
        text = _clean(detail.get("content") or detail.get("text"), 500)
        if not text:
            continue
        concept = infer_canonical_concept(label=label, text=text, dimension=label)
        events.append({
            "profile_id": profile_id,
            "source": "profile_flywheel",
            "event_type": "detail",
            "dimension": label,
            "canonical_concept": concept,
            "title": label,
            "text": text,
            "confidence": 0.7,
            "review_status": "pending",
            "raw_payload": {"evidence": [{"session_id": session_id, "type": "detail", **detail}]},
        })
    for quote in v2.get("accumulated_quotes") or []:
        if not isinstance(quote, dict):
            continue
        text = _clean(quote.get("text"), 300)
        if not text:
            continue
        label = _clean(quote.get("usage_hint") or quote.get("context") or "用户原话", 80)
        concept = infer_canonical_concept(label=label, text=text, dimension="quote")
        events.append({
            "profile_id": profile_id,
            "source": "profile_flywheel",
            "event_type": "quote",
            "dimension": "quote",
            "canonical_concept": concept,
            "title": label,
            "text": text,
            "confidence": 0.7,
            "review_status": "pending",
            "raw_payload": {"evidence": [{"session_id": session_id, "type": "quote", **quote}]},
        })
    summary = _clean(v2.get("understanding_summary"), 800)
    if summary:
        events.append({
            "profile_id": profile_id,
            "source": "profile_flywheel",
            "event_type": "persona_summary",
            "dimension": "summary",
            "canonical_concept": "business_identity",
            "title": "历史建档总结",
            "text": summary,
            "confidence": 0.7,
            "review_status": "pending",
            "raw_payload": {"evidence": [{"session_id": session_id, "type": "persona_summary"}]},
        })
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write events; default is dry-run")
    parser.add_argument("--ignore-flag", action="store_true", help="allow apply without admin flag")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    if args.apply and not args.ignore_flag and not _flag_enabled():
        raise SystemExit("backfill_profile_flywheel_enabled=false; enable it or pass --ignore-flag")

    sessions = _iter_sessions(args.limit)
    total_events = 0
    written = 0
    for row in sessions:
        user_id = row.get("user_id")
        for event in _events_from_session(row):
            total_events += 1
            if not args.apply:
                continue
            fingerprint = make_event_fingerprint(
                event["profile_id"],
                event["source"],
                event["event_type"],
                event["canonical_concept"],
                event["text"],
            )
            event_id = record_profile_memory_event(
                event.pop("profile_id"),
                user_id=user_id,
                event_fingerprint=fingerprint,
                weight_delta=1,
                **event,
            )
            if event_id:
                written += 1
    print(json.dumps({
        "sessions": len(sessions),
        "events_found": total_events,
        "events_written": written,
        "dry_run": not args.apply,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
