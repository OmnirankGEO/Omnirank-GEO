import psycopg2


def _insert_paid(url: str, order_id: str, mode: str | None, *, order_type: str = "customer_recharge"):
    conn = psycopg2.connect(url)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO recharge_orders
                   (id,user_id,amount_cents,base_points,bonus_points,payment_status,
                    payment_id,paid_at,order_type,settlement_mode)
               VALUES (%s,10,10000,10000,500,'paid','PAY-1',clock_timestamp(),%s,%s)""",
            (order_id, order_type, mode),
        )
    conn.commit()
    conn.close()


def _rows(url: str):
    conn = psycopg2.connect(url)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_type,terminal_state,recipient_user_id,title,content
               FROM notification_outbox ORDER BY recipient_user_id,event_type"""
        )
        result = cur.fetchall()
    conn.close()
    return result


def test_paid_customer_order_repairs_credited_outbox_idempotently(notification_db):
    _insert_paid(notification_db, "RECHARGE-CREDITED-1", "v35_inventory_settlement")
    from db.wallet_db import complete_recharge

    complete_recharge("RECHARGE-CREDITED-1", "IGNORED-RETRY-1")
    complete_recharge("RECHARGE-CREDITED-1", "IGNORED-RETRY-2")
    rows = _rows(notification_db)
    assert len(rows) == 1
    assert rows[0][0:3] == ("recharge.credited", "credited", 10)
    assert "已到账" in rows[0][4]


def test_paid_dispute_order_repairs_review_required_not_credited(notification_db):
    _insert_paid(notification_db, "RECHARGE-REVIEW-1", "dispute_hold")
    from db.wallet_db import complete_recharge

    complete_recharge("RECHARGE-REVIEW-1", "IGNORED-RETRY")
    rows = _rows(notification_db)
    assert len(rows) == 3
    assert {row[0] for row in rows} == {"recharge.review_required"}
    assert {row[1] for row in rows} == {"review_required"}
    assert {row[2] for row in rows} == {10, 90, 91}
    assert all("已到账" not in row[4] for row in rows)


def test_paid_agent_inventory_repairs_inventory_event(notification_db):
    _insert_paid(
        notification_db,
        "AGENT-INVENTORY-1",
        "agent_inventory_prepay",
        order_type="agent_inventory_purchase",
    )
    from db.wallet_db import complete_recharge

    complete_recharge("AGENT-INVENTORY-1", "IGNORED-RETRY")
    rows = _rows(notification_db)
    assert len(rows) == 1
    assert rows[0][0:3] == ("agent_inventory.credited", "credited", 10)


def test_paid_order_with_unknown_settlement_never_claims_success(notification_db):
    _insert_paid(notification_db, "RECHARGE-UNKNOWN-1", None)
    from db.wallet_db import complete_recharge

    complete_recharge("RECHARGE-UNKNOWN-1", "IGNORED-RETRY")
    assert _rows(notification_db) == []
