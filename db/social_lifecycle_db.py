"""
社媒内容生命周期账本

把内容规划、文案、发布、数据复盘和互动沉淀串成同一条可追踪链路。
这层不替代已有 social_project_db / plan_db，而是作为跨模块事件与效果回写层。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import shortuuid

logger = logging.getLogger("GEO-SocialLifecycle")


def get_connection():
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


_tables_ensured = False


def _json_dumps(value: Any) -> str:
    if value is None:
        value = {}
    return json.dumps(value, ensure_ascii=False, default=str)


def _event_id() -> str:
    return shortuuid.uuid()[:16]


def ensure_tables() -> None:
    """确保社媒生命周期账本表存在。"""
    global _tables_ensured
    if _tables_ensured:
        return

    conn = get_connection()
    try:
        conn.autocommit = True
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_lifecycle_events (
                id SERIAL PRIMARY KEY,
                event_id VARCHAR(24) UNIQUE NOT NULL,
                profile_id TEXT,
                brand_id INTEGER,
                team_id INTEGER,
                actor_user_id TEXT,
                plan_id VARCHAR(22),
                plan_task_id VARCHAR(22),
                topic_id INTEGER,
                script_id INTEGER,
                publication_id INTEGER,
                event_type TEXT NOT NULL,
                event_source TEXT DEFAULT 'api',
                payload JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_publications (
                id SERIAL PRIMARY KEY,
                profile_id TEXT,
                brand_id INTEGER,
                team_id INTEGER,
                plan_id VARCHAR(22),
                plan_task_id VARCHAR(22),
                script_id INTEGER,
                platform TEXT,
                publish_url TEXT,
                platform_item_id TEXT,
                status TEXT DEFAULT 'published',
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_publication_metrics (
                id SERIAL PRIMARY KEY,
                publication_id INTEGER REFERENCES social_publications(id),
                script_id INTEGER,
                platform TEXT,
                views INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                collects INTEGER DEFAULT 0,
                follows INTEGER DEFAULT 0,
                dms INTEGER DEFAULT 0,
                leads INTEGER DEFAULT 0,
                completion_rate NUMERIC,
                five_sec_drop_rate NUMERIC,
                engagement_rate NUMERIC,
                raw_data JSONB DEFAULT '{}'::jsonb,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_interactions (
                id SERIAL PRIMARY KEY,
                publication_id INTEGER REFERENCES social_publications(id),
                script_id INTEGER,
                platform TEXT,
                interaction_type TEXT,
                user_handle TEXT,
                user_profile_url TEXT,
                content TEXT,
                sentiment TEXT,
                is_lead BOOLEAN DEFAULT FALSE,
                lead_status TEXT,
                raw_data JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        for sql in [
            "CREATE INDEX IF NOT EXISTS idx_social_lifecycle_profile_time ON social_lifecycle_events(profile_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_social_lifecycle_task ON social_lifecycle_events(plan_task_id) WHERE plan_task_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_social_lifecycle_script ON social_lifecycle_events(script_id) WHERE script_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_social_publications_script ON social_publications(script_id) WHERE script_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_social_publications_task ON social_publications(plan_task_id) WHERE plan_task_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_social_metrics_publication_time ON social_publication_metrics(publication_id, recorded_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_social_interactions_publication ON social_interactions(publication_id) WHERE publication_id IS NOT NULL",
        ]:
            cursor.execute(sql)

        cursor.close()
        logger.info("[SocialLifecycle] 生命周期表已就绪")
    except Exception as exc:
        logger.warning(f"[SocialLifecycle] 建表失败: {exc}")
    finally:
        conn.close()
        _tables_ensured = True


def log_event(
    event_type: str,
    *,
    profile_id: str = None,
    brand_id: int = None,
    team_id: int = None,
    actor_user_id: str = None,
    plan_id: str = None,
    plan_task_id: str = None,
    topic_id: int = None,
    script_id: int = None,
    publication_id: int = None,
    event_source: str = "api",
    payload: Dict[str, Any] = None,
) -> Optional[str]:
    """写入一条生命周期事件，失败不影响主业务。"""
    ensure_tables()
    eid = _event_id()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO social_lifecycle_events
                (event_id, profile_id, brand_id, team_id, actor_user_id, plan_id,
                 plan_task_id, topic_id, script_id, publication_id, event_type,
                 event_source, payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                eid, profile_id, brand_id, team_id, actor_user_id, plan_id,
                plan_task_id, topic_id, script_id, publication_id, event_type,
                event_source, _json_dumps(payload),
            ),
        )
        conn.commit()
        cursor.close()
        return eid
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[SocialLifecycle] log_event 失败: {exc}")
        return None
    finally:
        conn.close()


def record_publication(
    *,
    script_id: int = None,
    publish_url: str = None,
    platform: str = None,
    profile_id: str = None,
    brand_id: int = None,
    team_id: int = None,
    plan_id: str = None,
    plan_task_id: str = None,
    platform_item_id: str = None,
    status: str = "published",
    published_at: Any = None,
) -> Optional[Dict[str, Any]]:
    """记录或更新一次发布证据。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        existing = None
        if script_id and publish_url:
            cursor.execute(
                """
                SELECT * FROM social_publications
                WHERE script_id = %s AND publish_url = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (script_id, publish_url),
            )
            existing = cursor.fetchone()
        elif script_id:
            cursor.execute(
                """
                SELECT * FROM social_publications
                WHERE script_id = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (script_id,),
            )
            existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """
                UPDATE social_publications
                SET publish_url = COALESCE(%s, publish_url),
                    platform = COALESCE(%s, platform),
                    profile_id = COALESCE(%s, profile_id),
                    brand_id = COALESCE(%s, brand_id),
                    team_id = COALESCE(%s, team_id),
                    plan_id = COALESCE(%s, plan_id),
                    plan_task_id = COALESCE(%s, plan_task_id),
                    platform_item_id = COALESCE(%s, platform_item_id),
                    status = COALESCE(%s, status),
                    published_at = COALESCE(%s, published_at),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                RETURNING *
                """,
                (
                    publish_url, platform, profile_id, brand_id, team_id, plan_id,
                    plan_task_id, platform_item_id, status, published_at, existing["id"],
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO social_publications
                    (profile_id, brand_id, team_id, plan_id, plan_task_id, script_id,
                     platform, publish_url, platform_item_id, status, published_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, CURRENT_TIMESTAMP))
                RETURNING *
                """,
                (
                    profile_id, brand_id, team_id, plan_id, plan_task_id, script_id,
                    platform, publish_url, platform_item_id, status, published_at,
                ),
            )
        row = dict(cursor.fetchone())
        conn.commit()
        cursor.close()
        return row
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[SocialLifecycle] record_publication 失败: {exc}")
        return None
    finally:
        conn.close()


def get_publication_by_script_id(script_id: int) -> Optional[Dict[str, Any]]:
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM social_publications
            WHERE script_id = %s
            ORDER BY updated_at DESC, created_at DESC LIMIT 1
            """,
            (script_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        return dict(row) if row else None
    finally:
        conn.close()


def record_metrics(
    *,
    publication_id: int = None,
    script_id: int = None,
    platform: str = None,
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    collects: int = 0,
    follows: int = 0,
    dms: int = 0,
    leads: int = 0,
    completion_rate: float = None,
    five_sec_drop_rate: float = None,
    raw_data: Dict[str, Any] = None,
) -> Optional[Dict[str, Any]]:
    """记录一次发布效果快照。"""
    ensure_tables()
    if not publication_id and script_id:
        publication = get_publication_by_script_id(script_id)
        publication_id = publication.get("id") if publication else None
        platform = platform or (publication.get("platform") if publication else None)

    engagement_rate = None
    try:
        if views:
            engagement_rate = round((likes + comments + shares) / views * 100, 4)
    except Exception:
        engagement_rate = None

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO social_publication_metrics
                (publication_id, script_id, platform, views, likes, comments, shares,
                 collects, follows, dms, leads, completion_rate, five_sec_drop_rate,
                 engagement_rate, raw_data)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (
                publication_id, script_id, platform, int(views or 0), int(likes or 0),
                int(comments or 0), int(shares or 0), int(collects or 0),
                int(follows or 0), int(dms or 0), int(leads or 0),
                completion_rate, five_sec_drop_rate, engagement_rate,
                _json_dumps(raw_data),
            ),
        )
        row = dict(cursor.fetchone())
        conn.commit()
        cursor.close()
        return row
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[SocialLifecycle] record_metrics 失败: {exc}")
        return None
    finally:
        conn.close()


def record_interaction(
    *,
    publication_id: int = None,
    script_id: int = None,
    platform: str = None,
    interaction_type: str = None,
    user_handle: str = None,
    user_profile_url: str = None,
    content: str = None,
    sentiment: str = None,
    is_lead: bool = False,
    lead_status: str = None,
    raw_data: Dict[str, Any] = None,
) -> Optional[Dict[str, Any]]:
    """记录评论、私信、线索等互动。"""
    ensure_tables()
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO social_interactions
                (publication_id, script_id, platform, interaction_type, user_handle,
                 user_profile_url, content, sentiment, is_lead, lead_status, raw_data)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (
                publication_id, script_id, platform, interaction_type, user_handle,
                user_profile_url, content, sentiment, bool(is_lead), lead_status,
                _json_dumps(raw_data),
            ),
        )
        row = dict(cursor.fetchone())
        conn.commit()
        cursor.close()
        return row
    except Exception as exc:
        conn.rollback()
        logger.warning(f"[SocialLifecycle] record_interaction 失败: {exc}")
        return None
    finally:
        conn.close()


__all__ = [
    "ensure_tables",
    "log_event",
    "record_publication",
    "record_metrics",
    "record_interaction",
    "get_publication_by_script_id",
]
