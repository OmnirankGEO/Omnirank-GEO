"""Metrics helpers for Social Studio Agent Loop turns."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
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
                CREATE TABLE IF NOT EXISTS social_agent_metrics (
                    id BIGSERIAL PRIMARY KEY,
                    turn_id TEXT,
                    user_id INTEGER,
                    profile_id TEXT,
                    model TEXT,
                    used_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
                    tool_call_count INTEGER NOT NULL DEFAULT 0,
                    tool_error_count INTEGER NOT NULL DEFAULT 0,
                    cost_points INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    plan_completed BOOLEAN NOT NULL DEFAULT FALSE,
                    confirmation_requested BOOLEAN NOT NULL DEFAULT FALSE,
                    confirmation_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_metrics_created
                ON social_agent_metrics(created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_metrics_profile_created
                ON social_agent_metrics(profile_id, created_at DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_metrics_turn
                ON social_agent_metrics(turn_id)
                """
            )
        _ensured = True
    finally:
        conn.close()


def record_agent_turn_metrics(
    *,
    turn_id: str | None,
    user_id: int | None,
    profile_id: str | None,
    model: str | None,
    used_tools: list[str] | None,
    tool_call_count: int,
    tool_error_count: int,
    cost_points: int,
    latency_ms: int,
    plan_completed: bool = False,
    confirmation_requested: bool = False,
    confirmation_confirmed: bool = False,
) -> dict[str, Any]:
    ensure_tables()
    tools_json = json.dumps([str(tool) for tool in (used_tools or []) if tool], ensure_ascii=False)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO social_agent_metrics (
                    turn_id, user_id, profile_id, model, used_tools, tool_call_count,
                    tool_error_count, cost_points, latency_ms, plan_completed,
                    confirmation_requested, confirmation_confirmed
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, turn_id, user_id, profile_id, model, used_tools,
                          tool_call_count, tool_error_count, cost_points, latency_ms,
                          plan_completed, confirmation_requested, confirmation_confirmed, created_at
                """,
                (
                    turn_id,
                    user_id,
                    profile_id,
                    model,
                    tools_json,
                    max(0, int(tool_call_count or 0)),
                    max(0, int(tool_error_count or 0)),
                    max(0, int(cost_points or 0)),
                    max(0, int(latency_ms or 0)),
                    bool(plan_completed),
                    bool(confirmation_requested),
                    bool(confirmation_confirmed),
                ),
            )
            row = _fetchone_dict(cur)
        conn.commit()
        return row
    finally:
        conn.close()


def get_agent_metrics_by_turn_id(turn_id: str) -> list[dict[str, Any]]:
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, turn_id, user_id, profile_id, model, used_tools,
                       tool_call_count, tool_error_count, cost_points, latency_ms,
                       plan_completed, confirmation_requested, confirmation_confirmed, created_at
                FROM social_agent_metrics
                WHERE turn_id=%s
                ORDER BY created_at ASC, id ASC
                """,
                (turn_id,),
            )
            return _fetchall_dicts(cur)
    finally:
        conn.close()


def get_agent_metrics_summary(*, hours: int = 24, profile_id: str | None = None) -> dict[str, Any]:
    ensure_tables()
    safe_hours = max(1, min(int(hours or 24), 24 * 90))
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if profile_id:
                cur.execute(
                    """
                    SELECT id, turn_id, user_id, profile_id, model, used_tools,
                           tool_call_count, tool_error_count, cost_points, latency_ms,
                           plan_completed, confirmation_requested, confirmation_confirmed, created_at
                    FROM social_agent_metrics
                    WHERE created_at >= NOW() - (%s::int * INTERVAL '1 hour')
                      AND profile_id=%s
                    ORDER BY created_at DESC, id DESC
                    """,
                    (safe_hours, profile_id),
                )
            else:
                cur.execute(
                    """
                    SELECT id, turn_id, user_id, profile_id, model, used_tools,
                           tool_call_count, tool_error_count, cost_points, latency_ms,
                           plan_completed, confirmation_requested, confirmation_confirmed, created_at
                    FROM social_agent_metrics
                    WHERE created_at >= NOW() - (%s::int * INTERVAL '1 hour')
                    ORDER BY created_at DESC, id DESC
                    """,
                    (safe_hours,),
                )
            rows = _fetchall_dicts(cur)
    finally:
        conn.close()
    return summarize_metric_rows(rows, hours=safe_hours, profile_id=profile_id)


def summarize_metric_rows(rows: list[dict[str, Any]], *, hours: int = 24, profile_id: str | None = None) -> dict[str, Any]:
    turn_count = len(rows)
    tool_call_count = sum(int(row.get("tool_call_count") or 0) for row in rows)
    tool_error_count = sum(int(row.get("tool_error_count") or 0) for row in rows)
    cost_total = sum(int(row.get("cost_points") or 0) for row in rows)
    latencies = sorted(int(row.get("latency_ms") or 0) for row in rows)
    plan_completed = sum(1 for row in rows if row.get("plan_completed") is True)
    confirm_requested = sum(1 for row in rows if row.get("confirmation_requested") is True)
    confirm_confirmed = sum(1 for row in rows if row.get("confirmation_confirmed") is True)
    tool_freq: Counter[str] = Counter()
    for row in rows:
        for tool in _coerce_tools(row.get("used_tools")):
            tool_freq[tool] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": int(hours or 24),
        "profile_id": profile_id,
        "turn_count": turn_count,
        "tool_call_count": tool_call_count,
        "tool_call_freq": dict(sorted(tool_freq.items())),
        "tool_failure_rate": _ratio(tool_error_count, tool_call_count),
        "avg_cost_points": (cost_total / turn_count) if turn_count else 0.0,
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "plan_completion_rate": _ratio(plan_completed, turn_count),
        "abcd_confirm_rate": _ratio(confirm_confirmed, confirm_requested),
        "totals": {
            "tool_error_count": tool_error_count,
            "cost_points": cost_total,
            "plan_completed": plan_completed,
            "confirmation_requested": confirm_requested,
            "confirmation_confirmed": confirm_confirmed,
        },
    }


def _coerce_tools(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _ratio(numerator: int, denominator: int) -> float:
    return (float(numerator) / float(denominator)) if denominator else 0.0


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    index = max(0, math.ceil(len(values) * fraction) - 1)
    return values[max(0, min(index, len(values) - 1))]


def _fetchone_dict(cur) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return _normalize_row(dict(row))
    columns = [desc[0] for desc in cur.description]
    return _normalize_row(dict(zip(columns, row)))


def _fetchall_dicts(cur) -> list[dict[str, Any]]:
    rows = cur.fetchall() or []
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [_normalize_row(dict(row)) for row in rows]
    columns = [desc[0] for desc in cur.description]
    return [_normalize_row(dict(zip(columns, row))) for row in rows]


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("used_tools"), str):
        try:
            row["used_tools"] = json.loads(row["used_tools"])
        except Exception:
            row["used_tools"] = []
    return row
