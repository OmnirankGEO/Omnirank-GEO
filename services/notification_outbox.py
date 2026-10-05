"""PostgreSQL durable notification outbox and exactly-once dispatcher."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from db.connection import get_db
from services.notification_events import (
    NotificationEventType,
    RecipientKind,
    render_notification,
)

logger = logging.getLogger("GEO-NotificationOutbox")
MAX_ATTEMPTS = 8


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[index] if row else None


def _event_key(event_type: str, business_id: str, terminal_state: str, user_id: int) -> str:
    return f"{event_type}:{business_id}:{terminal_state}:{int(user_id)}"


def enqueue_notification_event(
    cursor,
    *,
    event_type: NotificationEventType | str,
    business_id: str,
    terminal_state: str,
    recipient_user_id: int,
    recipient_kind: RecipientKind | str,
    facts: Mapping[str, Any],
) -> Optional[int]:
    """Use the caller's cursor so business terminal and outbox are atomic."""
    # id < 0 的系统账户不是任何人:users 里没有这一行,投递必撞 user_notifications 的外键,
    #   重试耗尽后还会给全部管理员发「派发失败」。不入队、不告警,返回 None(与「幂等键已存在」同形)。
    if int(recipient_user_id) < 0:
        return None
    rendered = render_notification(event_type, recipient_kind, facts)
    key = _event_key(rendered["event_type"], str(business_id), str(terminal_state), recipient_user_id)
    cursor.execute(
        """
        INSERT INTO notification_outbox
            (event_key,event_type,business_id,terminal_state,recipient_user_id,
             recipient_kind,level,title,content,route,privacy_policy,payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (event_key) DO NOTHING
        RETURNING id
        """,
        (
            key, rendered["event_type"], str(business_id), str(terminal_state), int(recipient_user_id),
            rendered["recipient_kind"], rendered["level"], rendered["title"], rendered["content"],
            rendered["route"], rendered["privacy_policy"],
            json.dumps(rendered["payload"], ensure_ascii=False),
        ),
    )
    row = cursor.fetchone()
    return int(_row_value(row, "id")) if row else None


def admin_user_ids(cursor) -> list[int]:
    cursor.execute(
        """SELECT DISTINCT ur.user_id
           FROM user_roles ur JOIN roles r ON r.id=ur.role_id
           WHERE r.name='admin' ORDER BY ur.user_id"""
    )
    return [int(_row_value(row, "user_id")) for row in cursor.fetchall()]


def enqueue_admin_notification_events(cursor, **kwargs: Any) -> int:
    count = 0
    for user_id in admin_user_ids(cursor):
        inserted = enqueue_notification_event(
            cursor, recipient_user_id=user_id, recipient_kind=RecipientKind.ADMIN, **kwargs
        )
        count += int(inserted is not None)
    return count


def enqueue_brand_owner_notification_event(cursor, *, brand_id: int, **kwargs: Any) -> Optional[int]:
    """Resolve a brand's current owner inside the caller's transaction."""
    cursor.execute("SELECT owner_user_id FROM brands WHERE id=%s", (int(brand_id),))
    row = cursor.fetchone()
    owner_user_id = _row_value(row, "owner_user_id")
    if owner_user_id is None:
        raise ValueError(f"brand {int(brand_id)} has no notification owner")
    return enqueue_notification_event(
        cursor,
        recipient_user_id=int(owner_user_id),
        recipient_kind=RecipientKind.AGENT,
        **kwargs,
    )


def brand_owner_notification_event_exists(
    cursor, *, brand_id: int, event_type: NotificationEventType | str,
    business_id: str, terminal_state: str,
) -> bool:
    """这条事件是不是**已经**在 outbox 里了(只读,不写、不推)。

    [#178 · 2026-09-12] 读面(GET 选词页 / 重放分支)要回答「报价方收到通知了吗」。
    为什么放在本模块而不是调用方自己拼:``event_key`` 的格式(``_event_key``)是本模块
    私有合同,调用方复制一份就会漂移 —— 而漂移的表现是**恒 False**(查不到 ⇒ 前端永远
    显示"去通知"),不报错、没人发现。

    🔴 只按完整 ``event_key`` 查:该列是 UNIQUE(有索引)。按
    ``(event_type, business_id, terminal_state)`` 三列查会在一张只增不减的表上走顺序扫,
    而调用方是**公开的、未登录的**客户选词页 GET。
    """
    cursor.execute("SELECT owner_user_id FROM brands WHERE id=%s", (int(brand_id),))
    row = cursor.fetchone()
    owner_user_id = _row_value(row, "owner_user_id")
    if owner_user_id is None:
        return False
    event_value = (
        event_type.value if isinstance(event_type, NotificationEventType) else str(event_type)
    )
    key = _event_key(event_value, str(business_id), str(terminal_state), int(owner_user_id))
    cursor.execute("SELECT 1 FROM notification_outbox WHERE event_key=%s LIMIT 1", (key,))
    return cursor.fetchone() is not None


def brand_owner_notification_event_exists_durable(*, brand_id: int, **kwargs: Any) -> bool:
    """Durable bridge for read paths that hold no caller transaction."""
    with get_db() as conn:
        return brand_owner_notification_event_exists(
            conn.cursor(), brand_id=int(brand_id), **kwargs
        )


def enqueue_brand_owner_notification_event_durable(*, brand_id: int, **kwargs: Any) -> Optional[int]:
    """Compatibility bridge for legacy post-commit business hooks.

    New terminal writers must use ``enqueue_brand_owner_notification_event`` with
    their own cursor. This bridge still guarantees durable/idempotent outbox state
    and exists only where the legacy business write already commits separately.
    """
    with get_db() as conn:
        return enqueue_brand_owner_notification_event(
            conn.cursor(), brand_id=int(brand_id), **kwargs
        )


def enqueue_user_notification_event_durable(
    *, recipient_user_id: int, recipient_kind: RecipientKind | str, **kwargs: Any
) -> Optional[int]:
    """Durable bridge for non-transactional scheduler/account completion hooks."""
    with get_db() as conn:
        return enqueue_notification_event(
            conn.cursor(),
            recipient_user_id=int(recipient_user_id),
            recipient_kind=recipient_kind,
            **kwargs,
        )


def enqueue_admin_notification_events_durable(**kwargs: Any) -> int:
    """Durable bridge for scheduler/cron alerts that have no caller transaction."""
    with get_db() as conn:
        return enqueue_admin_notification_events(conn.cursor(), **kwargs)


def claim_notification_outbox(limit: int = 100) -> tuple[str, list[Dict[str, Any]]]:
    token = uuid.uuid4().hex
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            WITH picked AS (
                SELECT id FROM notification_outbox
                WHERE status='pending' AND available_at <= clock_timestamp()
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s
            )
            UPDATE notification_outbox o
            SET status='processing', claim_token=%s, claimed_at=clock_timestamp(), updated_at=clock_timestamp()
            FROM picked WHERE o.id=picked.id
            RETURNING o.*
            """,
            (max(1, min(int(limit), 500)), token),
        )
        rows = [dict(row) if isinstance(row, dict) else {"id": row[0]} for row in cur.fetchall()]
    return token, rows


def _deliver_claimed(outbox_id: int, claim_token: str) -> bool:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT * FROM notification_outbox
               WHERE id=%s AND status='processing' AND claim_token=%s FOR UPDATE""",
            (int(outbox_id), claim_token),
        )
        row = cur.fetchone()
        if not row:
            return False
        item = dict(row) if isinstance(row, dict) else {}
        metadata = json.dumps({"event_type": item["event_type"]}, ensure_ascii=False)
        cur.execute(
            """
            INSERT INTO user_notifications
                (user_id,type,title,content,link,level,metadata,event_key)
            VALUES (%s,'system',%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (user_id,event_key) WHERE event_key IS NOT NULL DO NOTHING
            RETURNING id
            """,
            (
                int(item["recipient_user_id"]), item["title"], item["content"], item["route"],
                item["level"], metadata, item["event_key"],
            ),
        )
        notification = cur.fetchone()
        if notification:
            notification_id = int(_row_value(notification, "id"))
        else:
            cur.execute(
                "SELECT id FROM user_notifications WHERE user_id=%s AND event_key=%s",
                (int(item["recipient_user_id"]), item["event_key"]),
            )
            notification_id = int(_row_value(cur.fetchone(), "id"))
        cur.execute(
            """UPDATE notification_outbox
               SET status='delivered', delivered_notification_id=%s,
                   delivered_at=clock_timestamp(), claim_token=NULL, claimed_at=NULL,
                   last_error=NULL, updated_at=clock_timestamp()
               WHERE id=%s AND status='processing' AND claim_token=%s""",
            (notification_id, int(outbox_id), claim_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("notification outbox claim was lost")
    return True


def _mark_delivery_failure(outbox_id: int, claim_token: str, exc: Exception) -> None:
    safe_error = f"{type(exc).__name__}: notification delivery failed"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE notification_outbox
            SET attempts=attempts+1,
                status=CASE WHEN attempts+1 >= %s THEN 'failed' ELSE 'pending' END,
                available_at=clock_timestamp() +
                    make_interval(secs => LEAST(3600, 5 * (2 ^ LEAST(attempts, 9))))::interval,
                claim_token=NULL, claimed_at=NULL, last_error=%s, updated_at=clock_timestamp()
            WHERE id=%s AND status='processing' AND claim_token=%s
            RETURNING event_type,business_id,attempts,status
            """,
            (MAX_ATTEMPTS, safe_error, int(outbox_id), claim_token),
        )
        failed = cur.fetchone()
        if not failed:
            return
        item = dict(failed) if isinstance(failed, dict) else {}
        if item.get("status") == "failed" and item.get("event_type") != NotificationEventType.NOTIFICATION_DISPATCH_FAILED.value:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            enqueue_admin_notification_events(
                cur,
                event_type=NotificationEventType.NOTIFICATION_DISPATCH_FAILED,
                business_id=str(item.get("business_id") or outbox_id),
                terminal_state="failed",
                facts={
                    "business_no": str(item.get("business_id") or outbox_id),
                    "status": "需要人工处理",
                    "occurred_at": now,
                    "summary": "通知已保留在待处理队列，请检查派发服务。",
                },
            )


def reclaim_stale_notification_claims(stale_seconds: int = 300) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE notification_outbox
               SET status='pending', claim_token=NULL, claimed_at=NULL,
                   available_at=clock_timestamp(), updated_at=clock_timestamp(),
                   last_error='stale claim recovered'
               WHERE status='processing'
                 AND claimed_at < clock_timestamp() - make_interval(secs => %s)""",
            (max(30, int(stale_seconds)),),
        )
        return int(cur.rowcount)


def dispatch_notification_outbox(limit: int = 100) -> Dict[str, int]:
    token, rows = claim_notification_outbox(limit)
    stats = {"claimed": len(rows), "delivered": 0, "failed": 0}
    for row in rows:
        try:
            stats["delivered"] += int(_deliver_claimed(int(row["id"]), token))
        except Exception as exc:
            logger.exception("notification outbox delivery failed id=%s", row.get("id"))
            _mark_delivery_failure(int(row["id"]), token, exc)
            stats["failed"] += 1
    return stats


def notification_outbox_cron() -> Dict[str, int]:
    from services.cron_gate import cron_should_fire

    if not cron_should_fire():
        return {"claimed": 0, "delivered": 0, "failed": 0, "reclaimed": 0}
    reclaimed = reclaim_stale_notification_claims()
    result = dispatch_notification_outbox()
    result["reclaimed"] = reclaimed
    return result
