import psycopg2
import pytest


def _count(url: str, event_type: str, recipient: int | None = None) -> int:
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            sql = "SELECT count(*) FROM notification_outbox WHERE event_type=%s"
            params: list[object] = [event_type]
            if recipient is not None:
                sql += " AND recipient_user_id=%s"
                params.append(recipient)
            cur.execute(sql, params)
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def test_monitoring_terminal_and_report_are_durable(notification_db):
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitoring_tasks(brand_id,total_tests,completed_tests,trigger_type) "
            "VALUES (1,2,2,'scheduled') RETURNING id"
        )
        task_id = int(cur.fetchone()[0])
    conn.commit()
    conn.close()

    from db.monitoring_db import save_report, update_task_status

    assert update_task_status(task_id, "completed", completed_tests=2)
    assert update_task_status(task_id, "completed", completed_tests=2)
    assert _count(notification_db, "monitoring.completed", 10) == 1
    assert _count(notification_db, "monitoring.partial_success", 10) == 0

    report_id = save_report(
        brand_id=1,
        report_type="weekly",
        period_start="2026-07-01",
        period_end="2026-07-07",
        summary_data={},
        status="ready",
    )
    assert report_id > 0
    assert _count(notification_db, "report.completed", 10) == 1


def test_marketing_terminal_rolls_back_when_outbox_write_fails(notification_db):
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO marketing_material_jobs(user_id) VALUES (10) RETURNING id")
        job_id = int(cur.fetchone()[0])
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION reject_test_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.business_id = %s THEN RAISE EXCEPTION 'test outbox rejection'; END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER reject_test_outbox_before_insert
              BEFORE INSERT ON notification_outbox
              FOR EACH ROW EXECUTE FUNCTION reject_test_outbox();
            """,
            (str(job_id),),
        )
    conn.commit()
    conn.close()

    from db.marketing_db import update_job

    with pytest.raises(Exception, match="test outbox rejection"):
        update_job(job_id, status="succeeded")

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM marketing_material_jobs WHERE id=%s", (job_id,))
        assert cur.fetchone()[0] == "pending"
        cur.execute("DROP TRIGGER reject_test_outbox_before_insert ON notification_outbox")
        cur.execute("DROP FUNCTION reject_test_outbox()")
    conn.commit()
    conn.close()

    update_job(job_id, status="succeeded")
    update_job(job_id, status="succeeded")
    assert _count(notification_db, "asset.completed", 10) == 1


def test_research_round_and_ai_ops_failures_notify_each_admin_once(notification_db):
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO geo_research_round(round_id) VALUES ('round-notify-1')")
        cur.execute("INSERT INTO ai_ops_tasks(kind) VALUES ('external_publish') RETURNING id")
        task_id = int(cur.fetchone()[0])
    conn.commit()
    conn.close()

    from db.ai_ops_db import update_task_status
    from services.research_monitor.round_state import update_round_complete

    assert update_round_complete("round-notify-1", "failed", {"safe": True})
    assert update_round_complete("round-notify-1", "failed", {"safe": True})
    assert update_task_status(task_id, "failed", summary="safe summary")
    assert update_task_status(task_id, "failed", summary="safe summary")
    assert _count(notification_db, "research.failed") == 2
    assert _count(notification_db, "system.external_channel_failed") == 2


def test_publish_batch_terminal_is_idempotent(notification_db):
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO publish_batches(id,user_id) VALUES ('batch-1',10)")
        cur.execute("INSERT INTO publish_orders(batch_id,status) VALUES ('batch-1','completed')")
    conn.commit()
    conn.close()

    from db.publish_db import recalculate_batch_status

    recalculate_batch_status("batch-1")
    recalculate_batch_status("batch-1")
    assert _count(notification_db, "publication.completed", 10) == 1


def test_password_terminal_and_tenant_read_isolation(notification_db):
    from db.auth_db import update_user_password
    from db.team_db import mark_user_notification_read
    from services.notification_outbox import dispatch_notification_outbox

    assert update_user_password(10, "SafePassword-2026")
    assert _count(notification_db, "account.password_changed", 10) == 1
    assert dispatch_notification_outbox()["delivered"] == 1

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM user_notifications WHERE user_id=10")
        notification_id = int(cur.fetchone()[0])
    conn.close()

    assert mark_user_notification_read(notification_id, 20) is False
    assert mark_user_notification_read(notification_id, 10) is True


def test_publication_refund_notifies_only_after_real_wallet_credit(notification_db):
    from db.meijiehezi_db import refund_for_publish_order

    result = refund_for_publish_order(10, 120, "refund_request:701", "媒体发布未成功")
    assert result["success"] is True
    assert result["refunded"] == 120
    duplicate = refund_for_publish_order(10, 120, "refund_request:701", "重复回调")
    assert duplicate["success"] is True
    assert duplicate["skipped"] is True
    assert _count(notification_db, "publication.refunded", 10) == 1

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=10")
        assert int(cur.fetchone()[0]) == 5120
        cur.execute(
            "SELECT content FROM notification_outbox WHERE event_type='publication.refunded'"
        )
        content = cur.fetchone()[0]
        assert "120" in content
        assert "已退回" in content
    conn.close()


def test_billing_refund_and_missing_outbox_repair_share_exact_charge(notification_db):
    import asyncio
    from middleware.billing import refund_points
    from services.notification_events import (
        NotificationEventType,
        RecipientKind,
        RefundNotificationContext,
    )

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO point_transactions
                   (user_id,type,point_type,amount,balance_after,feature_code,description,order_id)
               VALUES (10,'consume','paid',-300,4700,'managed_campaign_recharge','test','charge-group-1')
               RETURNING id"""
        )
        charge_tx_id = int(cur.fetchone()[0])
        cur.execute("UPDATE user_wallets SET paid_points=4700 WHERE user_id=10")
    conn.commit()
    conn.close()

    notification = RefundNotificationContext(
        event_type=NotificationEventType.MANAGED_CAMPAIGN_REFUNDED,
        business_id=f"managed_refund:{charge_tx_id}",
        terminal_state="refunded",
        recipient_kind=RecipientKind.USER,
        business_no="GEO-TEST701",
        status="已退回",
    )
    first = asyncio.run(refund_points(
        10,
        "managed_campaign_recharge",
        "test failure",
        charge_tx_id=charge_tx_id,
        ledger_type="legacy",
        notification=notification,
    ))
    assert first["success"] is True
    assert first["refunded"] == 300
    assert _count(notification_db, "managed_campaign.refunded", 10) == 1

    # Simulate an old successful refund whose outbox row was lost, then repair it
    # from the exact charge/refund evidence without moving money again.
    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM notification_outbox WHERE event_type='managed_campaign.refunded'")
    conn.commit()
    conn.close()


    repaired = asyncio.run(refund_points(
        10,
        "managed_campaign_recharge",
        "retry",
        charge_tx_id=charge_tx_id,
        ledger_type="legacy",
        notification=notification,
    ))
    assert repaired["success"] is True
    assert repaired["already_refunded"] is True
    assert repaired["confirmed_refunded"] == 300
    assert _count(notification_db, "managed_campaign.refunded", 10) == 1

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=10")
        assert int(cur.fetchone()[0]) == 5000
        cur.execute(
            "SELECT count(*) FROM point_transactions "
            "WHERE user_id=10 AND feature_code='managed_campaign_recharge' AND type='refund'"
        )
        assert int(cur.fetchone()[0]) == 1
    conn.close()


def test_billing_legacy_refund_without_charge_id_uses_only_fresh_refund_result(notification_db):
    """旧发布残余路径无 charge id 时不得 int(None)，也不得猜历史退款。"""
    import asyncio
    from middleware.billing import refund_points
    from services.notification_events import publication_refund_context

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO point_transactions
                   (user_id,type,point_type,amount,balance_after,feature_code,description)
               VALUES (10,'consume','paid',-80,4920,'media_proxy_publish','legacy residual')"""
        )
        cur.execute("UPDATE user_wallets SET paid_points=4920 WHERE user_id=10")
    conn.commit()
    conn.close()

    result = asyncio.run(refund_points(
        10,
        "media_proxy_publish",
        "legacy publication failure",
        ledger_type="legacy",
        notification=publication_refund_context("legacy-publication-701"),
    ))
    assert result["success"] is True
    assert result["refunded"] == 80
    assert _count(notification_db, "publication.refunded", 10) == 1

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=10")
        assert int(cur.fetchone()[0]) == 5000
        cur.execute(
            "SELECT content FROM notification_outbox WHERE event_type='publication.refunded'"
        )
        assert "80" in cur.fetchone()[0]
    conn.close()


def test_billing_legacy_refund_without_charge_or_consume_does_not_invent_terminal(notification_db):
    """无精确 charge 且无真实扣费时只返回未找到，不得崩溃或宣称已退款。"""
    import asyncio
    from middleware.billing import refund_points
    from services.notification_events import publication_refund_context

    result = asyncio.run(refund_points(
        10,
        "media_proxy_publish",
        "no matching consume",
        ledger_type="legacy",
        notification=publication_refund_context("legacy-publication-no-charge"),
    ))
    assert result == {"success": False, "reason": "未找到扣费记录"}
    assert _count(notification_db, "publication.refunded", 10) == 0
