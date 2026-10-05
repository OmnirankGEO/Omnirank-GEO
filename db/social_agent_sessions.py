"""Durable Social Studio agent-loop chat sessions.

These rows are conversation history, not profile facts. Keep them separate
from profile_memory_events so "what happened in chat" never pollutes the
reviewable customer-memory ledger.
"""

from __future__ import annotations

from typing import Any

_ensured = False


def get_connection():
    from db.connection import get_connection as _get_connection

    return _get_connection()


def ensure_tables() -> None:
    global _ensured
    if _ensured:
        return
    conn = get_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS social_agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    user_id INTEGER,
                    summary TEXT NOT NULL DEFAULT '',
                    topic TEXT NOT NULL DEFAULT '',
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    started_at TIMESTAMPTZ DEFAULT NOW(),
                    last_active_at TIMESTAMPTZ DEFAULT NOW(),
                    CHECK (status IN ('active', 'ended', 'archived'))
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_sessions_profile_status
                ON social_agent_sessions(profile_id, status, last_active_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_sessions_user_active
                ON social_agent_sessions(user_id, last_active_at DESC)
                """
            )
        _ensured = True
    finally:
        conn.close()


def upsert_agent_session(
    *,
    session_id: str,
    user_id: int | None,
    profile_id: str,
    summary: str = "",
    topic: str = "",
    status: str = "active",
) -> dict[str, Any]:
    if not session_id:
        raise ValueError("session_id is required")
    if not profile_id:
        raise ValueError("profile_id is required")
    safe_status = status if status in {"active", "ended", "archived"} else "active"
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO social_agent_sessions (
                    session_id, user_id, profile_id, summary, topic, status
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    user_id = COALESCE(EXCLUDED.user_id, social_agent_sessions.user_id),
                    profile_id = EXCLUDED.profile_id,
                    summary = CASE
                        WHEN EXCLUDED.summary <> '' THEN EXCLUDED.summary
                        ELSE social_agent_sessions.summary
                    END,
                    topic = CASE
                        WHEN EXCLUDED.topic <> '' THEN EXCLUDED.topic
                        ELSE social_agent_sessions.topic
                    END,
                    status = EXCLUDED.status,
                    last_active_at = NOW()
                RETURNING session_id, user_id, profile_id, summary, topic,
                          status, started_at, last_active_at
                """,
                (
                    session_id,
                    user_id,
                    profile_id,
                    str(summary or "")[:1200],
                    str(topic or "")[:300],
                    safe_status,
                ),
            )
            row = _fetchone_dict(cur)
        conn.commit()
        return row
    finally:
        conn.close()


def get_agent_session(session_id: str) -> dict[str, Any] | None:
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, user_id, profile_id, summary, topic, status,
                       started_at, last_active_at
                FROM social_agent_sessions
                WHERE session_id=%s
                """,
                (session_id,),
            )
            return _fetchone_dict(cur)
    finally:
        conn.close()


def search_agent_sessions(*, profile_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search prior agent sessions for a profile.

    This intentionally scopes by profile_id only. The owner guard happens in
    the tool router before a model can call internal_history_search.
    """
    if not profile_id:
        return []
    ensure_tables()
    # H-P0-7 fix · 用 batch helper 一次 SQL 拿所有 session 的 turns(防 N+1)
    from db.social_agent_turns import ensure_tables as ensure_turn_tables
    from db.social_agent_turns import list_turns_for_sessions

    ensure_turn_tables()
    safe_limit = max(1, min(int(limit or 5), 20))
    tokens = _tokens(query)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, user_id, profile_id, summary, topic, status,
                       started_at, last_active_at
                FROM social_agent_sessions
                WHERE profile_id=%s
                  AND status <> 'archived'
                ORDER BY last_active_at DESC
                LIMIT 120
                """,
                (profile_id,),
            )
            rows = _fetchall_dicts(cur)
    finally:
        conn.close()

    # H-P0-7 fix · batch query 一次拿所有 session 的 top 12 turns(原 120 queries → 1 query)
    session_ids = [str(row.get("session_id") or "") for row in rows if row.get("session_id")]
    turns_by_session = list_turns_for_sessions(session_ids, per_session_limit=12)

    ranked: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        turns = turns_by_session.get(str(row.get("session_id") or ""), [])
        haystack = " ".join(
            [
                str(row.get("summary") or ""),
                str(row.get("topic") or ""),
                " ".join(str(turn.get("content") or "") for turn in turns),
            ]
        ).lower()
        score = _score(tokens, haystack)
        if tokens and score <= 0:
            continue
        matched_turns = [
            {
                "role": turn.get("role"),
                "content": str(turn.get("content") or "")[:500],
                "created_at": turn.get("created_at"),
            }
            for turn in turns
            if not tokens or _score(tokens, str(turn.get("content") or "").lower()) > 0
        ][:5]
        item = dict(row)
        item["matched_turns"] = matched_turns
        item["score"] = score
        ranked.append((score, item))

    ranked.sort(key=lambda pair: (pair[0], str(pair[1].get("last_active_at") or "")), reverse=True)
    return [item for _, item in ranked[:safe_limit]]


def _tokens(query: str) -> list[str]:
    raw = str(query or "").replace("，", " ").replace("。", " ").replace(",", " ")
    return [token.strip().lower() for token in raw.split() if len(token.strip()) >= 2][:12]


def _score(tokens: list[str], text: str) -> int:
    if not tokens:
        return 1
    return sum(1 for token in tokens if token in text)


def _fetchone_dict(cur) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def _fetchall_dicts(cur) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [dict(row) for row in rows]
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in rows]
