import asyncio
from types import SimpleNamespace

import psycopg2


def _request(user_id: int, *, admin: bool = False):
    return SimpleNamespace(
        state=SimpleNamespace(user={"user_id": user_id, "id": user_id, "is_admin": admin})
    )


def _outbox(url: str):
    conn = psycopg2.connect(url)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT event_type,terminal_state,recipient_user_id,content,route
               FROM notification_outbox ORDER BY id"""
        )
        result = cur.fetchall()
    conn.close()
    return result


def _insert_request(url: str, status: str = "pending") -> int:
    conn = psycopg2.connect(url)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_settlement_requests
                   (agent_user_id,request_amount_cents,status,gateway_fee_cents,
                    settlement_fee_cents,tax_cents,net_amount_cents)
               VALUES (10,10000,%s,100,100,300,9500) RETURNING id""",
            (status,),
        )
        request_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return request_id


def test_submit_notifies_agent_and_each_admin_in_same_transaction(notification_db, monkeypatch):
    import api.agent_workbench_api as api

    monkeypatch.setattr(api, "_require_agent", lambda _request: {"user_id": 10})

    def fake_create(cur, **_kwargs):
        cur.execute(
            """INSERT INTO agent_settlement_requests
                   (agent_user_id,request_amount_cents,status,gateway_fee_cents,
                    settlement_fee_cents,tax_cents,net_amount_cents)
               VALUES (10,10000,'pending',100,100,300,9500) RETURNING id"""
        )
        request_id = cur.fetchone()["id"]
        return {
            "request_id": request_id,
            "request_amount_cents": 10000,
            "net_cents": 9500,
            "total_fee_cents": 500,
            "platform_fee_cents": 200,
            "tax_cents": 300,
            "locked_items": [{"ledger_id": 1}],
        }

    monkeypatch.setattr(api, "create_settlement_request", fake_create)
    req = api.AgentSettlementRequestCreate(
        amount_cents=10000,
        bank_name="测试银行",
        bank_account="6222000000000000",
        account_holder="测试用户",
        invoice_required=False,
    )
    response = asyncio.run(api.agent_settlement_request_create(req, _request(10)))
    assert response.status == "pending"
    rows = _outbox(notification_db)
    assert len(rows) == 3
    assert {row[2] for row in rows} == {10, 90, 91}
    assert all(row[0:2] == ("agent_settlement.submitted", "submitted") for row in rows)
    assert all("6222" not in row[3] for row in rows)


def test_approve_reject_and_paid_emit_only_the_committed_state(notification_db, monkeypatch):
    import api.admin_factory_api as api
    from schemas.v35_w2_dto import AdminSettlementPatchRequest

    monkeypatch.setattr(api, "_require_admin", lambda _request: {"user_id": 90, "is_admin": True})

    approved_id = _insert_request(notification_db)
    asyncio.run(
        api.admin_settlements_patch(
            approved_id,
            AdminSettlementPatchRequest(action="approve"),
            _request(90, admin=True),
        )
    )

    rejected_id = _insert_request(notification_db)
    asyncio.run(
        api.admin_settlements_patch(
            rejected_id,
            AdminSettlementPatchRequest(action="reject", reject_reason="资料需要重新核对 6222000000000000"),
            _request(90, admin=True),
        )
    )

    paid_id = _insert_request(notification_db, "approved")
    asyncio.run(
        api.admin_settlements_patch(
            paid_id,
            AdminSettlementPatchRequest(action="mark_paid", wire_transfer_no="INTERNAL-SECRET-1"),
            _request(90, admin=True),
        )
    )

    rows = _outbox(notification_db)
    assert [(row[0], row[1]) for row in rows] == [
        ("agent_settlement.approved", "approved"),
        ("agent_settlement.rejected", "rejected"),
        ("agent_settlement.paid", "paid"),
    ]
    combined = " ".join(row[3] for row in rows)
    assert "6222000000000000" not in combined
    assert "INTERNAL-SECRET-1" not in combined
    assert all(row[4] == "/agent/settlement" for row in rows)


def test_state_rollback_removes_its_notification(notification_db, monkeypatch):
    import api.admin_factory_api as api
    import services.notification_outbox as outbox
    from schemas.v35_w2_dto import AdminSettlementPatchRequest

    monkeypatch.setattr(api, "_require_admin", lambda _request: {"user_id": 90, "is_admin": True})
    request_id = _insert_request(notification_db)
    original = outbox.enqueue_notification_event

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("forced rollback")

    monkeypatch.setattr(outbox, "enqueue_notification_event", fail_after_insert)
    try:
        asyncio.run(
            api.admin_settlements_patch(
                request_id,
                AdminSettlementPatchRequest(action="approve"),
                _request(90, admin=True),
            )
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced notification failure must abort the state transaction")

    conn = psycopg2.connect(notification_db)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agent_settlement_requests WHERE id=%s", (request_id,))
        assert cur.fetchone()[0] == "pending"
        cur.execute("SELECT count(*) FROM notification_outbox")
        assert cur.fetchone()[0] == 0
    conn.close()
