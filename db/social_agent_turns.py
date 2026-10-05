"""Turn-level history for Social Studio agent-loop sessions."""

from __future__ import annotations

import json
from typing import Any

_ensured = False


def get_connection():
    from db.connection import get_connection as _get_connection

    return _get_connection()


def ensure_tables() -> None:
    global _ensured
    if _ensured:
        return
    from db.social_agent_sessions import ensure_tables as ensure_session_tables

    ensure_session_tables()
    conn = get_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS social_agent_turns (
                    id BIGSERIAL PRIMARY KEY,
                    turn_id TEXT,
                    session_id TEXT NOT NULL REFERENCES social_agent_sessions(session_id) ON DELETE CASCADE,
                    user_id INTEGER,
                    profile_id TEXT,
                    role VARCHAR(20) NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tool_calls_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    cost_points INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    CHECK (role IN ('user', 'assistant', 'tool', 'system'))
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_turns_session_created
                ON social_agent_turns(session_id, created_at ASC, id ASC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_turns_profile_created
                ON social_agent_turns(profile_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_turns_turn
                ON social_agent_turns(turn_id)
                """
            )
        _ensured = True
    finally:
        conn.close()


def record_agent_turn(
    *,
    session_id: str,
    turn_id: str | None,
    user_id: int | None,
    profile_id: str | None,
    role: str,
    content: str,
    tool_calls_json: list[dict[str, Any]] | None = None,
    cost_points: int = 0,
) -> dict[str, Any]:
    if not session_id:
        raise ValueError("session_id is required")
    if role not in {"user", "assistant", "tool", "system"}:
        raise ValueError(f"invalid role: {role}")
    ensure_tables()
    tools_json = json.dumps(tool_calls_json or [], ensure_ascii=False, default=str)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO social_agent_turns (
                    session_id, turn_id, user_id, profile_id, role, content,
                    tool_calls_json, cost_points
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                RETURNING id, session_id, turn_id, user_id, profile_id, role,
                          content, tool_calls_json, cost_points, created_at
                """,
                (
                    session_id,
                    turn_id,
                    user_id,
                    profile_id,
                    role,
                    str(content or "")[:8000],
                    tools_json,
                    max(0, int(cost_points or 0)),
                ),
            )
            row = _fetchone_dict(cur)
        conn.commit()
        return row
    finally:
        conn.close()


def record_agent_turn_pair(
    *,
    session_id: str,
    turn_id: str | None,
    user_id: int | None,
    profile_id: str | None,
    user_content: str,
    assistant_content: str,
    tool_results: list[dict[str, Any]] | None = None,
    cost_points: int = 0,
) -> list[dict[str, Any]]:
    rows = [
        record_agent_turn(
            session_id=session_id,
            turn_id=turn_id,
            user_id=user_id,
            profile_id=profile_id,
            role="user",
            content=user_content,
        ),
        record_agent_turn(
            session_id=session_id,
            turn_id=turn_id,
            user_id=user_id,
            profile_id=profile_id,
            role="assistant",
            content=assistant_content,
            tool_calls_json=tool_results or [],
            cost_points=cost_points,
        ),
    ]
    return rows


def list_turns_by_session(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    ensure_tables()
    safe_limit = max(1, min(int(limit or 50), 200))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, session_id, turn_id, user_id, profile_id, role,
                       content, tool_calls_json, cost_points, created_at
                FROM social_agent_turns
                WHERE session_id=%s
                ORDER BY created_at ASC, id ASC
                LIMIT %s
                """,
                (session_id, safe_limit),
            )
            return _fetchall_dicts(cur)
    finally:
        conn.close()


def list_turns_for_sessions(
    session_ids: list[str],
    *,
    per_session_limit: int = 12,
) -> dict[str, list[dict[str, Any]]]:
    """H-P0-7 fix · batch 拿多 session 的 top N turns(防 N+1).

    修前:search_agent_sessions for row in rows · 每个 row 单独调 list_turns_by_session
    = 120 session × 1 query = 120 queries · multi-second 单次搜索 prod 必慢。
    现在用 PostgreSQL window function ROW_NUMBER() 一次 SQL 拿所有 sessions 的 top N。
    """
    if not session_ids:
        return {}
    ensure_tables()
    # 去重 + 类型保护
    clean_ids = [str(sid) for sid in session_ids if sid]
    if not clean_ids:
        return {}
    safe_per = max(1, min(int(per_session_limit or 12), 50))
    placeholders = ",".join(["%s"] * len(clean_ids))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH ranked AS (
                    SELECT id, session_id, turn_id, user_id, profile_id, role,
                           content, tool_calls_json, cost_points, created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY session_id
                               ORDER BY created_at ASC, id ASC
                           ) AS rn
                    FROM social_agent_turns
                    WHERE session_id IN ({placeholders})
                )
                SELECT id, session_id, turn_id, user_id, profile_id, role,
                       content, tool_calls_json, cost_points, created_at
                FROM ranked WHERE rn <= %s
                ORDER BY session_id, rn
                """,
                (*clean_ids, safe_per),
            )
            rows = _fetchall_dicts(cur)
    finally:
        conn.close()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sid = str(row.get("session_id") or "")
        if not sid:
            continue
        grouped.setdefault(sid, []).append(row)
    return grouped


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
