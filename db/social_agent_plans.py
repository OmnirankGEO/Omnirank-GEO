"""Persistent plan state for the Social Studio agent loop."""

from __future__ import annotations

import json
import uuid
from typing import Any

_ensured = False

# P0-9 fix · SSOT v1.2 §2.4b 明文要求加 'superseded' enum
# (新 plan 启动时旧 active plan archive 成 superseded · 不删 · 历史可追)
PLAN_STATUSES = ("active", "stale", "completed", "cancelled", "superseded")


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
            # P0-9 fix · CHECK 加 'superseded'(SSOT v1.2 §2.4b)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS social_agent_plans (
                    plan_id VARCHAR(36) PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    current_step INTEGER DEFAULT 0,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    user_intent_summary TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    CHECK (status IN ('active', 'stale', 'completed', 'cancelled', 'superseded'))
                )
                """
            )
            # P0-9 fix · 现有表升级老 CHECK 约束 · _safe_drop_check 模式
            # (CLAUDE.md SQL 4 维度核验 · IF EXISTS 安全)
            cur.execute(
                """
                DO $$
                BEGIN
                    BEGIN
                        ALTER TABLE social_agent_plans DROP CONSTRAINT IF EXISTS social_agent_plans_status_check;
                    EXCEPTION WHEN OTHERS THEN NULL;
                    END;
                    BEGIN
                        ALTER TABLE social_agent_plans ADD CONSTRAINT social_agent_plans_status_check
                            CHECK (status IN ('active', 'stale', 'completed', 'cancelled', 'superseded'));
                    EXCEPTION WHEN duplicate_object THEN NULL;
                    END;
                END $$;
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_social_agent_plans_one_active
                ON social_agent_plans(user_id, profile_id)
                WHERE status='active'
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_social_agent_plans_profile_status
                ON social_agent_plans(profile_id, status, updated_at DESC)
                """
            )
        _ensured = True
    finally:
        conn.close()


def upsert_active_plan(
    *,
    user_id: int,
    profile_id: str,
    state_json: dict[str, Any],
    user_intent_summary: str = "",
) -> dict[str, Any]:
    """P0-10 fix · 改 archive-old → INSERT-new(防历史 plan summary 永久丢失).

    修前 ON CONFLICT DO UPDATE 直接覆盖旧 active plan · 用户历史 user_intent_summary
    全丢 · 看不到 30 天前用户讲的"先调研 3 国"等任何记录。
    现在改:
      1. 旧 active plan UPDATE status='superseded'(保留 · 不删 · 历史可追)
      2. 新 plan INSERT 成 active
    一次事务 atomic · 唯一活跃 partial index 仍生效。
    """
    ensure_tables()
    current_step = _current_step(state_json)
    payload = _json_dump(state_json)
    new_plan_id = str(uuid.uuid4())
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Step 1 · 旧 active plan archive 成 superseded(保留历史 user_intent_summary)
            cur.execute(
                """
                UPDATE social_agent_plans
                SET status='superseded', updated_at=NOW()
                WHERE user_id=%s AND profile_id=%s AND status='active'
                """,
                (user_id, profile_id),
            )
            # Step 2 · 新 active plan INSERT(唯一 partial index 保证只 1 个 active)
            cur.execute(
                """
                INSERT INTO social_agent_plans (
                    plan_id, profile_id, user_id, state_json, current_step, status, user_intent_summary
                )
                VALUES (%s, %s, %s, %s::jsonb, %s, 'active', %s)
                RETURNING plan_id, profile_id, user_id, state_json, current_step, status,
                          user_intent_summary, created_at, updated_at
                """,
                (new_plan_id, profile_id, user_id, payload, current_step, user_intent_summary),
            )
            row = _fetchone_dict(cur)
        conn.commit()
        return row
    finally:
        conn.close()


def get_active_plan(*, user_id: int, profile_id: str) -> dict[str, Any] | None:
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan_id, profile_id, user_id, state_json, current_step, status,
                       user_intent_summary, created_at, updated_at
                FROM social_agent_plans
                WHERE user_id=%s AND profile_id=%s AND status='active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user_id, profile_id),
            )
            return _fetchone_dict(cur)
    finally:
        conn.close()


def get_plan_by_id(plan_id: str) -> dict[str, Any] | None:
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan_id, profile_id, user_id, state_json, current_step, status,
                       user_intent_summary, created_at, updated_at
                FROM social_agent_plans
                WHERE plan_id=%s
                """,
                (plan_id,),
            )
            return _fetchone_dict(cur)
    finally:
        conn.close()


def update_plan_status(plan_id: str, status: str) -> dict[str, Any] | None:
    if status not in PLAN_STATUSES:
        raise ValueError(f"invalid plan status: {status}")
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE social_agent_plans
                SET status=%s, updated_at=NOW()
                WHERE plan_id=%s
                RETURNING plan_id, profile_id, user_id, state_json, current_step, status,
                          user_intent_summary, created_at, updated_at
                """,
                (status, plan_id),
            )
            row = _fetchone_dict(cur)
        conn.commit()
        return row
    finally:
        conn.close()


def mark_stale_plans(*, stale_after_hours: int = 24) -> int:
    ensure_tables()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE social_agent_plans
                SET status='stale', updated_at=NOW()
                WHERE status='active'
                  AND updated_at < NOW() - (%s::int * INTERVAL '1 hour')
                RETURNING plan_id
                """,
                (stale_after_hours,),
            )
            rows = cur.fetchall() or []
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _current_step(state_json: dict[str, Any]) -> int:
    raw = state_json.get("current_step", state_json.get("current_step_index", 0))
    try:
        return max(0, int(raw))
    except Exception:
        return 0


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _fetchone_dict(cur) -> dict[str, Any] | None:
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    columns = [desc[0] for desc in cur.description]
    data = dict(zip(columns, row))
    if isinstance(data.get("state_json"), str):
        try:
            data["state_json"] = json.loads(data["state_json"])
        except Exception:
            data["state_json"] = {}
    return data
