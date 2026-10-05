"""Revision-preserving full reset for Writing Hall topics."""
from __future__ import annotations

from typing import Any

from psycopg2.extras import Json
from db.xact_lock_guard import require_xact_scope


class ArticleResetConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(code)


def reset_topics_for_full_regeneration(
    cur,
    *,
    quote_id: int,
    topic_ids: list[int],
    actor_user_id: int | None,
    request_id: str,
) -> dict[str, Any]:
    """Retire current title/body while preserving article rows and audit.

    The caller owns the transaction. Replaying the same request id is a no-op.
    """
    ids = sorted({int(value) for value in topic_ids if int(value) > 0})
    # Serialize the request identity before locking individual topics. Without
    # this lock, two concurrent calls could reuse one request id for disjoint
    # topic sets and both extend the supposedly immutable idempotency scope.
    require_xact_scope(cur, where="article_generation_reset.reset_topics_for_full_regeneration")  # §1 硬闸:autocommit 下取事务锁=没锁
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (str(request_id),),
    )
    cur.execute(
        """
        SELECT quote_id,topic_id,previous_published
          FROM article_generation_revision_events
         WHERE request_id=%s
         ORDER BY topic_id
        """,
        (str(request_id),),
    )
    existing_events = [dict(row) for row in cur.fetchall()]
    if existing_events:
        existing_ids = [int(row["topic_id"]) for row in existing_events]
        existing_quotes = {int(row["quote_id"]) for row in existing_events}
        if existing_ids != ids or existing_quotes != {int(quote_id)}:
            raise ArticleResetConflict(
                "ARTICLE_RESET_IDEMPOTENCY_CONFLICT",
                "该操作编号已用于另一组文章，请刷新后重新操作。",
            )
        return {
            "reset_topic_ids": [],
            "replayed_topic_ids": existing_ids,
            "published_topic_ids": [
                int(row["topic_id"])
                for row in existing_events
                if row.get("previous_published")
            ],
            "already_pending_topic_ids": [],
        }

    cur.execute(
        """
        SELECT t.id,t.status,t.optimized_title,t.article_id,t.generation_operation,
               COALESCE(t.generation_revision,0) AS generation_revision,
               a.version AS article_version,a.first_published_at
          FROM topics t
          LEFT JOIN articles a ON a.id=t.article_id
         WHERE t.quote_id=%s AND t.id=ANY(%s)
         ORDER BY t.id
         FOR UPDATE OF t
        """,
        (int(quote_id), ids),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != len(ids):
        raise ArticleResetConflict("ARTICLE_RESET_SCOPE_MISMATCH", "部分文章不存在或不属于当前项目。")
    if any(row.get("status") in {"writing", "regenerating"} for row in rows):
        raise ArticleResetConflict("ARTICLE_RESET_IN_PROGRESS", "写作或标题生成中的文章暂不能重置。")

    reset_ids: list[int] = []
    replayed_ids: list[int] = []
    published_ids: list[int] = []
    already_pending_ids: list[int] = []
    for row in rows:
        if (
            row.get("status") == "pending"
            and row.get("optimized_title") is None
            and row.get("article_id") is None
        ):
            already_pending_ids.append(int(row["id"]))
            continue
        next_revision = int(row.get("generation_revision") or 0) + 1
        snapshot = {
            "previous_title": row.get("optimized_title"),
            "previous_article_id": row.get("article_id"),
            "previous_article_version": row.get("article_version"),
            "previous_status": row.get("status"),
            "previous_published": row.get("first_published_at") is not None,
        }
        cur.execute(
            """
            INSERT INTO article_generation_revision_events (
                request_id,quote_id,topic_id,generation_revision,event_kind,
                previous_article_id,previous_title,previous_published,actor_user_id,snapshot
            ) VALUES (%s,%s,%s,%s,'full_reset',%s,%s,%s,%s,%s)
            ON CONFLICT (request_id,topic_id) DO NOTHING
            RETURNING id
            """,
            (
                request_id,
                int(quote_id),
                int(row["id"]),
                next_revision,
                row.get("article_id"),
                row.get("optimized_title"),
                row.get("first_published_at") is not None,
                actor_user_id,
                Json(snapshot),
            ),
        )
        if not cur.fetchone():
            replayed_ids.append(int(row["id"]))
            continue
        cur.execute(
            """
            UPDATE topics
               SET optimized_title=NULL,article_id=NULL,status='pending',
                   reviewed_at=NULL,completed_at=NULL,writing_started_at=NULL,
                   fail_reason=NULL,generation_revision=%s,
                   generation_operation='full_reset',generation_request_id=NULL,
                   generation_error_code=NULL,generation_error_message=NULL,
                   generation_retryable=NULL,generation_failure_phase=NULL,
                   generation_legal_catalog_version=NULL,
                   generation_refund_status='not_required'
             WHERE id=%s AND quote_id=%s
            """,
            (next_revision, int(row["id"]), int(quote_id)),
        )
        if cur.rowcount != 1:
            raise ArticleResetConflict("ARTICLE_RESET_CONFLICT", "文章状态已变化，请刷新后重试。")
        reset_ids.append(int(row["id"]))
        if row.get("first_published_at") is not None:
            published_ids.append(int(row["id"]))
    return {
        "reset_topic_ids": reset_ids,
        "replayed_topic_ids": replayed_ids,
        "published_topic_ids": published_ids,
        "already_pending_topic_ids": already_pending_ids,
    }
