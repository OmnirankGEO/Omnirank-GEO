from decimal import Decimal

import psycopg2


def _events(url: str):
    conn = psycopg2.connect(url)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_type,terminal_state,recipient_user_id,content,route
               FROM notification_outbox ORDER BY id"""
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def test_legacy_withdrawal_full_path_uses_transactional_outbox(notification_db):
    import db.wallet_db as wallet_db
    from db.withdrawal_db import admin_approve, admin_mark_paid, create_withdrawal

    wallet_db._POINT_TX_HAS_SOURCE = None
    result = create_withdrawal(10, Decimal("100.00"), 1, "11111111-1111-1111-1111-111111111111")
    request_id = result["id"]
    # Idempotent retry repairs but never duplicates the submitted events.
    create_withdrawal(10, Decimal("100.00"), 1, "11111111-1111-1111-1111-111111111111")
    admin_approve(request_id, 90)
    admin_mark_paid(request_id, 90, "SECRET-PAYOUT-TRACE")

    events = _events(notification_db)
    assert [(row[0], row[1], row[2]) for row in events] == [
        ("agent_settlement.submitted", "submitted", 10),
        ("agent_settlement.submitted", "submitted", 90),
        ("agent_settlement.submitted", "submitted", 91),
        ("agent_settlement.approved", "approved", 10),
        ("agent_settlement.paid", "paid", 10),
    ]
    assert all(
        row[4] == ("/admin/settlements" if row[2] in (90, 91) else "/agent/settlement")
        for row in events
    )
    assert "SECRET-PAYOUT-TRACE" not in " ".join(row[3] for row in events)


def test_legacy_rejection_reason_is_redacted_and_single(notification_db):
    import db.wallet_db as wallet_db
    from db.withdrawal_db import admin_reject, create_withdrawal

    wallet_db._POINT_TX_HAS_SOURCE = None
    result = create_withdrawal(10, Decimal("100.00"), 1, "22222222-2222-2222-2222-222222222222")
    admin_reject(result["id"], 90, "请检查银行卡号 6222000000000000 和上游倍率")
    admin_reject(result["id"], 90, "重复动作")
    rejected = [row for row in _events(notification_db) if row[1] == "rejected"]
    assert len(rejected) == 1
    assert "6222000000000000" not in rejected[0][3]
    assert "上游" not in rejected[0][3]
    assert "倍率" not in rejected[0][3]
