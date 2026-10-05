"""Audit log for Social Studio agent-loop tool calls."""

from __future__ import annotations

import hashlib
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
    conn = get_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS social_agent_tool_audit (
                    id BIGSERIAL PRIMARY KEY,
                    turn_id TEXT,
                    user_id INTEGER,
                    profile_id TEXT,
                    tool_name TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    result_chars INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(20) NOT NULL,
                    error TEXT,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    cost_points INTEGER NOT NULL DEFAULT 0,
                    charged_points INTEGER NOT NULL DEFAULT 0,
                    refunded_points INTEGER NOT NULL DEFAULT 0,
                    billing_status VARCHAR(30),
                    billing_note TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    CHECK (status IN ('ok', 'error'))
                )
                """
            )
            cur.execute("ALTER TABLE social_agent_tool_audit ADD COLUMN IF NOT EXISTS charged_points INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE social_agent_tool_audit ADD COLUMN IF NOT EXISTS refunded_points INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE social_agent_tool_audit ADD COLUMN IF NOT EXISTS billing_status VARCHAR(30)")
            cur.execute("ALTER TABLE social_agent_tool_audit ADD COLUMN IF NOT EXISTS billing_note TEXT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_tool_audit_turn
                ON social_agent_tool_audit(turn_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_tool_audit_profile
                ON social_agent_tool_audit(profile_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_tool_audit_tool_status
                ON social_agent_tool_audit(tool_name, status, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_tool_audit_billing_status
                ON social_agent_tool_audit(billing_status, created_at DESC)
                """
            )
        _ensured = True
    finally:
        conn.close()


def record_tool_audit(
    *,
    turn_id: str | None,
    user_id: int | None,
    profile_id: str | None,
    tool_name: str,
    args: dict[str, Any] | None,
    result_chars: int,
    status: str,
    latency_ms: int,
    cost_points: int = 0,
    charged_points: int = 0,
    refunded_points: int = 0,
    billing_status: str | None = None,
    billing_note: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if status not in {"ok", "error"}:
        raise ValueError(f"invalid audit status: {status}")
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO social_agent_tool_audit (
                    turn_id, user_id, profile_id, tool_name, args_hash, result_chars,
                    status, error, latency_ms, cost_points, charged_points, refunded_points,
                    billing_status, billing_note
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, turn_id, user_id, profile_id, tool_name, args_hash,
                          result_chars, status, error, latency_ms, cost_points,
                          charged_points, refunded_points, billing_status, billing_note, created_at
                """,
                (
                    turn_id,
                    user_id,
                    profile_id,
                    tool_name,
                    hash_args(args or {}),
                    max(0, int(result_chars or 0)),
                    status,
                    error,
                    max(0, int(latency_ms or 0)),
                    max(0, int(cost_points or 0)),
                    max(0, int(charged_points or 0)),
                    max(0, int(refunded_points or 0)),
                    billing_status,
                    billing_note,
                ),
            )
            row = _fetchone_dict(cur)
        conn.commit()
        return row
    finally:
        conn.close()


def get_audit_by_turn_id(turn_id: str) -> list[dict[str, Any]]:
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, turn_id, user_id, profile_id, tool_name, args_hash,
                       result_chars, status, error, latency_ms, cost_points,
                       charged_points, refunded_points, billing_status, billing_note, created_at
                FROM social_agent_tool_audit
                WHERE turn_id=%s
                ORDER BY created_at ASC, id ASC
                """,
                (turn_id,),
            )
            return _fetchall_dicts(cur)
    finally:
        conn.close()


def get_audit_by_profile_id(profile_id: str, limit: int = 100) -> list[dict[str, Any]]:
    ensure_tables()
    safe_limit = max(1, min(int(limit or 100), 500))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, turn_id, user_id, profile_id, tool_name, args_hash,
                       result_chars, status, error, latency_ms, cost_points,
                       charged_points, refunded_points, billing_status, billing_note, created_at
                FROM social_agent_tool_audit
                WHERE profile_id=%s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (profile_id, safe_limit),
            )
            return _fetchall_dicts(cur)
    finally:
        conn.close()


def hash_args(args: dict[str, Any]) -> str:
    serialized = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


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
