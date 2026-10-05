"""Append-only Jina fetch provenance helpers (JCC-1/JCC-3)."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4

from psycopg2.extras import Json

from db.connection import get_connection


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def record_fetch_result(
    *,
    source_url: str,
    normalized_url: str,
    url_hash: str,
    result: dict[str, Any],
    round_id: str = "",
    outer_attempt: int = 1,
) -> list[dict[str, Any]]:
    """Persist every actual HTTP attempt; never update an earlier event."""
    attempts = result.get("attempts") or [{
        "attempt_number": 1,
        "status": "success" if result.get("ok") else "failed",
        "http_status": result.get("http_status"),
        "latency_ms": None,
        "error": result.get("error"),
    }]
    fetched_at = result.get("fetched_at") or datetime.now(timezone.utc).isoformat()
    content = str(result.get("content") or "")
    out: list[dict[str, Any]] = []
    conn = get_connection()
    try:
        cur = conn.cursor()
        for actual in attempts:
            event_key = uuid4().hex
            attempt_number = ((max(1, int(outer_attempt)) - 1) * 100) + int(
                actual.get("attempt_number") or 1
            )
            metadata = {
                "round_id": round_id,
                "outer_attempt": outer_attempt,
                "response_metadata": result.get("metadata") or {},
            }
            cur.execute(
                """
                INSERT INTO geo_research_article_fetches (
                    fetch_event_key, source_url, normalized_url, final_url, url_hash,
                    request_profile, request_profile_version, preset, engine,
                    cache_policy, timeout_seconds, token_budget, attempt_number,
                    response_status, http_status, warning, failure_reason,
                    published_time, fetched_at, latency_ms, usage_tokens,
                    parser_version, raw_response_hash, raw_body_hash, body_hash,
                    robots_policy, robots_reason, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s
                ) RETURNING id
                """,
                (
                    event_key, source_url, normalized_url,
                    result.get("final_url") or normalized_url, url_hash,
                    result.get("request_profile") or "style_analysis_v1",
                    result.get("request_profile_version") or "jcc-v1.0",
                    result.get("preset") or "reader",
                    result.get("engine") or "managed_reader",
                    result.get("cache_policy") or "provider_default",
                    60, result.get("token_budget"), attempt_number,
                    actual.get("status") or ("success" if result.get("ok") else "failed"),
                    actual.get("http_status") or result.get("http_status"),
                    result.get("warning"),
                    None if result.get("ok") else str(actual.get("error") or result.get("error") or "unknown")[:80],
                    result.get("published_time"), fetched_at,
                    actual.get("latency_ms"), result.get("usage_tokens"),
                    result.get("parser_version") or "jina-json-markdown-v1",
                    (
                        _sha256(json.dumps(result.get("raw_payload") or {}, ensure_ascii=False, sort_keys=True))
                        if str(actual.get("status") or "").lower() == "success" else None
                    ),
                    (
                        _sha256(content)
                        if content and str(actual.get("status") or "").lower() == "success" else None
                    ),
                    None,
                    result.get("robots_policy") or "prefilter_passed",
                    result.get("robots_reason"), Json(metadata),
                ),
            )
            row = cur.fetchone()
            out.append({"id": row["id"], "fetch_event_key": event_key})
        conn.commit()
        return out
    finally:
        conn.close()


def link_fetch_to_article(
    fetch_id: int,
    *,
    article_id: int,
    raw_object_key: str = "",
    body_object_key: str = "",
) -> None:
    """Complete the new event only; historical events remain immutable."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE geo_research_article_fetches
               SET article_id = %s,
                   raw_object_key = COALESCE(NULLIF(%s, ''), raw_object_key),
                   body_object_key = COALESCE(NULLIF(%s, ''), body_object_key)
             WHERE id = %s AND article_id IS NULL
            """,
            (article_id, raw_object_key, body_object_key, fetch_id),
        )
        conn.commit()
    finally:
        conn.close()


def link_clean_body_to_latest_fetch(
    *, article_id: int, canonical_body_hash: str, body_object_key: str
) -> int | None:
    """Bind the exact cleaned canonical body to the latest successful linked fetch."""
    if not canonical_body_hash:
        raise ValueError("canonical_body_hash_required")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH latest AS (
                SELECT id FROM geo_research_article_fetches
                 WHERE article_id=%s AND response_status='success'
                 ORDER BY fetched_at DESC, id DESC
                 LIMIT 1
            )
            UPDATE geo_research_article_fetches f
               SET body_hash=%s,
                   body_object_key=COALESCE(NULLIF(%s,''), body_object_key)
              FROM latest
             WHERE f.id=latest.id
            RETURNING f.id
            """,
            (article_id, canonical_body_hash, body_object_key),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["id"]) if row else None
    finally:
        conn.close()
