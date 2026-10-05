import threading
from datetime import datetime, timezone

import psycopg2

from services.notification_events import NotificationEventType, RecipientKind, render_notification


def _facts(**overrides):
    values = {
        "business_no": "ORDER-20260717-1",
        "amount": "100.00 元",
        "points": "10,000",
        "status": "已到账",
        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    values.update(overrides)
    return values


def _enqueue(url: str, barrier: threading.Barrier | None = None) -> None:
    from services.notification_outbox import enqueue_notification_event

    conn = psycopg2.connect(url)
    try:
        if barrier:
            barrier.wait()
        with conn.cursor() as cur:
            enqueue_notification_event(
                cur,
                event_type=NotificationEventType.RECHARGE_CREDITED,
                business_id="ORDER-20260717-1",
                terminal_state="credited",
                recipient_user_id=10,
                recipient_kind=RecipientKind.CUSTOMER,
                facts=_facts(),
            )
        conn.commit()
    finally:
        conn.close()


def test_catalog_rejects_sensitive_or_unknown_fields():
    rendered = render_notification(
        NotificationEventType.RECHARGE_CREDITED,
        RecipientKind.CUSTOMER,
        _facts(),
    )
    assert rendered["level"] == "important"
    assert rendered["route"] == "/customer/wallet"
    try:
        render_notification(
            NotificationEventType.RECHARGE_CREDITED,
            RecipientKind.CUSTOMER,
            _facts(upstream_user_id=99),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unknown relationship field must be rejected")


def test_terminal_rollback_also_rolls_back_outbox(notification_db):
    from services.notification_outbox import enqueue_notification_event

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        enqueue_notification_event(
            cur,
            event_type=NotificationEventType.RECHARGE_CREDITED,
            business_id="ROLLBACK-1",
            terminal_state="credited",
            recipient_user_id=10,
            recipient_kind=RecipientKind.CUSTOMER,
            facts=_facts(business_no="ROLLBACK-1"),
        )
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM notification_outbox")
        assert cur.fetchone()[0] == 0
    conn.close()


def test_twenty_duplicate_events_dispatch_exactly_once(notification_db):
    barrier = threading.Barrier(20)
    workers = [threading.Thread(target=_enqueue, args=(notification_db, barrier)) for _ in range(20)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert not worker.is_alive()

    from services.notification_outbox import dispatch_notification_outbox

    dispatchers = [threading.Thread(target=dispatch_notification_outbox) for _ in range(2)]
    for worker in dispatchers:
        worker.start()
    for worker in dispatchers:
        worker.join(timeout=15)
        assert not worker.is_alive()

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM notification_outbox")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM user_notifications WHERE user_id=10")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT status,attempts FROM notification_outbox")
        assert cur.fetchone() == ("delivered", 0)
    conn.close()


def test_worker_failure_keeps_durable_pending_outbox(notification_db, monkeypatch):
    _enqueue(notification_db)
    import services.notification_outbox as outbox

    monkeypatch.setattr(outbox, "_deliver_claimed", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))
    result = outbox.dispatch_notification_outbox()
    assert result == {"claimed": 1, "delivered": 0, "failed": 1}

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT status,attempts,last_error FROM notification_outbox")
        status, attempts, error = cur.fetchone()
        assert status == "pending"
        assert attempts == 1
        assert "boom" not in error
        cur.execute("SELECT count(*) FROM user_notifications")
        assert cur.fetchone()[0] == 0
    conn.close()


def test_stale_claim_is_recovered(notification_db):
    _enqueue(notification_db)
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE notification_outbox
               SET status='processing', claim_token='dead-worker',
                   claimed_at=clock_timestamp() - interval '10 minutes'"""
        )
    conn.commit()
    conn.close()

    from services.notification_outbox import reclaim_stale_notification_claims

    assert reclaim_stale_notification_claims(300) == 1
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT status,claim_token FROM notification_outbox")
        assert cur.fetchone() == ("pending", None)
    conn.close()
