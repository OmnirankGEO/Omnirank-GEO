"""Durable optimize-title jobs backed by topic state and scheduler recovery."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from typing import Any, Iterable


logger = logging.getLogger("GEO-OptimizeTitleJobs")
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_QUOTES: set[int] = set()
_ADVISORY_NAMESPACE = 731515


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _mark_failed(topic_ids: Iterable[int], reason: str) -> None:
    ids = [int(topic_id) for topic_id in topic_ids]
    if not ids:
        return
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE topics
            SET status = 'failed', fail_reason = %s
            WHERE id = ANY(%s) AND is_optimize = TRUE AND status = 'regenerating'
            """,
            (reason[:500], ids),
        )
        cur.execute(
            """
            UPDATE quotes SET writing_status='pending'
            WHERE id IN (
                SELECT DISTINCT quote_id FROM topics
                WHERE id = ANY(%s) AND is_optimize = TRUE AND status = 'failed'
            )
              AND writing_status IN ('pending', 'titles_generating', 'titles_ready')
            """,
            (ids,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("优化标题失败态写入异常 topic_count=%s", len(ids))
    finally:
        conn.close()


def _update_quote_writing_status(
    cur: Any,
    quote_id: int,
    *,
    generated_count: int,
    failed_count: int,
) -> None:
    """Derive project readiness from every durable optimize topic in the quote."""
    if generated_count + failed_count <= 0:
        return
    cur.execute(
        """
        UPDATE quotes AS q
        SET writing_status = CASE
            WHEN EXISTS (
                SELECT 1 FROM topics t
                WHERE t.quote_id=q.id AND t.is_optimize=TRUE
                  AND t.status IN ('failed', 'regenerating')
            ) THEN 'pending'
            WHEN EXISTS (
                SELECT 1 FROM topics t
                WHERE t.quote_id=q.id AND t.is_optimize=TRUE
                  AND t.status IN ('pending', 'titles_ready', 'writing', 'completed', 'published')
                  AND COALESCE(NULLIF(BTRIM(t.optimized_title), ''), '标题生成中...') <> '标题生成中...'
            ) THEN 'titles_ready'
            ELSE 'pending'
        END
        WHERE q.id=%s AND q.writing_status IN ('pending', 'titles_generating', 'titles_ready')
        """,
        (int(quote_id),),
    )


async def run_optimize_title_job(quote_id: int, topic_ids: list[int] | None = None) -> dict[str, int]:
    """Generate titles for persisted regenerating topics without changing locked styles."""
    from db.connection import get_connection
    from writing.keyword_topic_generator import KeywordTopicGenerator

    lock_conn = get_connection()
    lock_acquired = False
    candidate_ids: list[int] = []
    try:
        lock_cur = lock_conn.cursor()
        lock_cur.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
            (_ADVISORY_NAMESPACE, int(quote_id)),
        )
        lock_acquired = bool((lock_cur.fetchone() or {}).get("acquired"))
        if not lock_acquired:
            return {"generated": 0, "failed": 0, "skipped": 1}

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT brand_name, industry, brand_id FROM quotes WHERE id = %s",
                (int(quote_id),),
            )
            quote = _row_dict(cur.fetchone())
            if not quote:
                return {"generated": 0, "failed": 0, "skipped": 1}

            params: list[Any] = [int(quote_id)]
            id_clause = ""
            if topic_ids:
                id_clause = " AND id = ANY(%s)"
                params.append([int(topic_id) for topic_id in topic_ids])
            cur.execute(
                f"""
                SELECT id, keyword_id, original_keyword, user_choice, style_code, article_style
                FROM topics
                WHERE quote_id = %s AND is_optimize = TRUE AND status = 'regenerating'
                  AND optimized_title = '标题生成中...'{id_clause}
                ORDER BY keyword_id, id
                """,
                tuple(params),
            )
            topics = [_row_dict(row) for row in (cur.fetchall() or [])]
        finally:
            conn.close()

        if not topics:
            return {"generated": 0, "failed": 0, "skipped": 0}
        candidate_ids = [int(topic["id"]) for topic in topics]

        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for topic in topics:
            grouped[int(topic["keyword_id"])].append(topic)

        keywords = []
        style_plan = []
        topic_by_slot: dict[tuple[int, int], dict[str, Any]] = {}
        for keyword_id, keyword_topics in grouped.items():
            keywords.append({
                "id": keyword_id,
                "keyword": keyword_topics[0]["original_keyword"],
                "required_articles": len(keyword_topics),
            })
            for slot_index, topic in enumerate(keyword_topics):
                style_plan.append({
                    "keyword_id": keyword_id,
                    "slot_index": slot_index,
                    "user_choice": topic["user_choice"],
                })
                topic_by_slot[(keyword_id, slot_index)] = topic

        generator = KeywordTopicGenerator(
            keywords=keywords,
            brand_name=quote.get("brand_name") or "",
            industry=quote.get("industry") or "",
            brand_id=quote.get("brand_id"),
            style_plan=style_plan,
        )
        try:
            generated = await asyncio.wait_for(generator.generate(), timeout=300)
        except asyncio.TimeoutError:
            _mark_failed(candidate_ids, "标题生成超时，可重试")
            return {"generated": 0, "failed": len(candidate_ids), "skipped": 0}
        except Exception as exc:
            logger.warning("优化标题生成失败 quote_id=%s error_type=%s", quote_id, type(exc).__name__)
            _mark_failed(candidate_ids, "标题生成失败，可重试")
            return {"generated": 0, "failed": len(candidate_ids), "skipped": 0}

        generated_by_slot: dict[tuple[int, int], dict[str, Any]] = {}
        seen_by_keyword: dict[int, int] = defaultdict(int)
        for item in generated or []:
            try:
                keyword_id = int(item.get("keyword_id"))
            except (TypeError, ValueError):
                continue
            raw_slot = item.get("slot_index")
            if raw_slot is None:
                slot_index = seen_by_keyword[keyword_id]
            else:
                try:
                    slot_index = int(raw_slot)
                except (TypeError, ValueError):
                    continue
            seen_by_keyword[keyword_id] = max(seen_by_keyword[keyword_id], slot_index + 1)
            generated_by_slot.setdefault((keyword_id, slot_index), item)

        db = get_connection()
        generated_count = 0
        failed_count = 0
        try:
            cur = db.cursor()
            for slot, topic in topic_by_slot.items():
                item = generated_by_slot.get(slot) or {}
                title = str(item.get("optimized_title") or item.get("title") or "").strip()
                if not title:
                    cur.execute(
                        "UPDATE topics SET status='failed', fail_reason='标题结果缺失，可重试' "
                        "WHERE id=%s AND status='regenerating'",
                        (topic["id"],),
                    )
                    failed_count += cur.rowcount
                    continue
                # Deliberately update the title only. user_choice/style_code/article_style
                # were locked when the topic was created and must remain the body SSOT.
                cur.execute(
                    """
                    UPDATE topics
                    SET optimized_title=%s, status='pending', fail_reason=NULL,
                        article_id=NULL, completed_at=NULL, reviewed_at=NULL
                    WHERE id=%s AND status='regenerating'
                    """,
                    (title, topic["id"]),
                )
                generated_count += cur.rowcount
            _update_quote_writing_status(
                cur,
                quote_id,
                generated_count=generated_count,
                failed_count=failed_count,
            )
            db.commit()
        except Exception:
            db.rollback()
            _mark_failed(candidate_ids, "标题保存失败，可重试")
            raise
        finally:
            db.close()
        return {"generated": generated_count, "failed": failed_count, "skipped": 0}
    finally:
        if lock_acquired:
            try:
                lock_cur = lock_conn.cursor()
                lock_cur.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    (_ADVISORY_NAMESPACE, int(quote_id)),
                )
            except Exception:
                logger.warning("优化标题 advisory lock 释放失败 quote_id=%s", quote_id)
        lock_conn.close()


def _thread_target(quote_id: int, topic_ids: list[int] | None) -> None:
    try:
        asyncio.run(run_optimize_title_job(quote_id, topic_ids))
    except Exception:
        logger.exception("优化标题线程异常 quote_id=%s", quote_id)
        if topic_ids:
            _mark_failed(topic_ids, "标题任务异常，可重试")
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT_QUOTES.discard(int(quote_id))


def dispatch_optimize_title_job(quote_id: int, topic_ids: list[int] | None = None) -> bool:
    """Best-effort immediate dispatch; persisted rows remain recoverable if it fails."""
    with _INFLIGHT_LOCK:
        if int(quote_id) in _INFLIGHT_QUOTES:
            return False
        _INFLIGHT_QUOTES.add(int(quote_id))
    try:
        thread = threading.Thread(
            target=_thread_target,
            args=(int(quote_id), list(topic_ids) if topic_ids else None),
            daemon=True,
            name=f"optimize-title-{quote_id}",
        )
        thread.start()
        return True
    except Exception:
        with _INFLIGHT_LOCK:
            _INFLIGHT_QUOTES.discard(int(quote_id))
        logger.exception("优化标题线程启动失败 quote_id=%s", quote_id)
        return False


def recover_optimize_title_jobs() -> dict[str, int]:
    """Scheduler compensation: redispatch every durable regenerating quote."""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT quote_id, ARRAY_AGG(id ORDER BY id) AS topic_ids
            FROM topics
            WHERE is_optimize=TRUE AND status='regenerating'
              AND optimized_title='标题生成中...'
            GROUP BY quote_id
            ORDER BY quote_id
            LIMIT 50
            """
        )
        rows = [_row_dict(row) for row in (cur.fetchall() or [])]
    finally:
        conn.close()
    dispatched = sum(
        1
        for row in rows
        if dispatch_optimize_title_job(int(row["quote_id"]), list(row.get("topic_ids") or []))
    )
    return {"queued_quotes": len(rows), "dispatched_quotes": dispatched}
