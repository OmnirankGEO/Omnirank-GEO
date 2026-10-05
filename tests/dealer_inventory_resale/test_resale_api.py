from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from api.dealer_inventory_resale_api import router

from .test_resale_funds import _seed_users, _sell_water_to_consumer, _setup_water_chain, _wallet


def test_dealer_resale_routes_reach_endpoint_ownership_checks():
    from auth.module_mapping import resolve_permission

    assert resolve_permission("/api/dealer-resale/orders/O-1") is None
    assert resolve_permission("/api/dealer-resale") is None
    assert resolve_permission("/api/admin/dealer-resale/readiness") == "users"


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def inject_test_user(request: Request, call_next):
        raw = request.headers.get("x-test-user")
        if raw:
            request.state.user = {
                "user_id": int(raw),
                "is_admin": request.headers.get("x-test-admin") == "1",
            }
        return await call_next(request)

    app.include_router(router)
    return TestClient(app)


def _agent_client() -> TestClient:
    from api.agent_workbench_api import router as agent_router

    app = FastAPI()

    @app.middleware("http")
    async def inject_test_agent(request: Request, call_next):
        request.state.user = {"user_id": 10, "is_admin": False}
        return await call_next(request)

    app.include_router(agent_router)
    return TestClient(app)


def _wallet_admin_client() -> TestClient:
    from api.wallet_api import router as wallet_router

    app = FastAPI()

    @app.middleware("http")
    async def inject_admin(request: Request, call_next):
        request.state.user = {"user_id": 999, "username": "admin-test", "is_admin": True}
        return await call_next(request)

    app.include_router(wallet_router)
    return TestClient(app)


def _queue_consumer_refund(db_conn, *, route: str = "wechat_native") -> str:
    from services import dealer_inventory_resale as resale

    _sell_water_to_consumer(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        "UPDATE recharge_orders SET actual_payment_channel=%s WHERE id='O-SVC-CUSTOMER'",
        (route,),
    )
    case = resale.request_consumer_refund_case(
        cur,
        order_id="O-SVC-CUSTOMER",
        consumer_user_id=40,
        reason="provider execution fixture",
        reason_category="negotiated_other",
        evidence={"fixture": True},
    )
    resale.review_consumer_refund_case(
        cur,
        case_id=str(case["case_id"]),
        actor_user_id=30,
        approve=True,
        note="approved",
    )
    settled = resale.settle_consumer_refund_case(cur, case_id=str(case["case_id"]))
    assert settled["cash_status"] == "queued"
    db_conn.commit()
    return str(case["case_id"])


def test_admin_cash_queue_lists_and_executes_persisted_wechat_amount(db_conn, monkeypatch):
    case_id = _queue_consumer_refund(db_conn)
    calls = {}

    async def fake_refund_order(**kwargs):
        calls.update(kwargs)
        return {"status": "PROCESSING", "refund_id": "WX-REFUND-QUEUED"}

    query_count = 0

    async def fake_query_refund(out_refund_no, *, strict=False):
        nonlocal query_count
        assert strict is True
        query_count += 1
        assert out_refund_no == "RO-SVC-CUSTOMER"
        if query_count == 1:
            return {"status": "NOT_FOUND", "out_refund_no": out_refund_no}
        return {
            "status": "SUCCESS",
            "refund_id": "WX-REFUND-QUEUED",
            "out_trade_no": "O-SVC-CUSTOMER",
            "out_refund_no": out_refund_no,
            "amount": {"refund": 80, "total": 80},
        }

    monkeypatch.setattr("services.wechat_pay.refund_order", fake_refund_order)
    monkeypatch.setattr("services.wechat_pay.query_refund", fake_query_refund)
    client = _client()
    assert client.get(
        "/api/admin/dealer-resale/refund-cash-jobs",
        headers={"x-test-user": "30"},
    ).status_code == 403
    listed = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    )
    assert listed.status_code == 200
    job = listed.json()["items"][0]
    assert job["case_id"] == case_id and job["amount_cents"] == 80

    executed = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={"reason": "approved direct-service refund"},
    )
    assert executed.status_code == 200, executed.text
    assert calls["refund_yuan"] == pytest.approx(0.80)
    assert calls["total_yuan"] == pytest.approx(0.80)
    cur = db_conn.cursor()
    cur.execute("SELECT status,attempt_count FROM service_refund_cash_jobs WHERE case_id=%s", (case_id,))
    assert tuple(cur.fetchone().values()) == ("completed", 1)


def test_wallet_wechat_refund_ignores_request_amount_for_b2b_processing_cost(db_conn, monkeypatch):
    from services import dealer_inventory_resale as resale

    cur = _setup_water_chain(db_conn)
    resale.request_refund(
        cur,
        order_id="O-L2-SVC",
        requested_by_user_id=30,
        processing_cost_cents=3,
        processing_cost_evidence={"invoice": "actual-handling-cost-3c"},
    )
    cur.execute(
        "UPDATE recharge_orders SET actual_payment_channel='wechat_native' "
        "WHERE id='O-L2-SVC'"
    )
    db_conn.commit()
    calls = {}

    async def fake_refund_order(**kwargs):
        calls.update(kwargs)
        return {"status": "PROCESSING", "refund_id": "WX-B2B-67"}

    query_count = 0

    async def fake_query_refund(out_refund_no, *, strict=False):
        nonlocal query_count
        assert strict is True
        query_count += 1
        if query_count == 1:
            return {"status": "NOT_FOUND", "out_refund_no": out_refund_no}
        return {
            "status": "SUCCESS",
            "refund_id": "WX-B2B-67",
            "out_trade_no": "O-L2-SVC",
            "out_refund_no": out_refund_no,
            "amount": {"refund": 67, "total": 70},
        }

    monkeypatch.setattr("services.wechat_pay.refund_order", fake_refund_order)
    monkeypatch.setattr("services.wechat_pay.query_refund", fake_query_refund)
    result = _wallet_admin_client().post(
        "/api/wallet/wechat-refund",
        json={"order_id": "O-L2-SVC", "refund_yuan": 0.01, "reason": "unused lot"},
    )
    assert result.status_code == 200, result.text
    assert calls["refund_yuan"] == pytest.approx(0.67)
    assert calls["total_yuan"] == pytest.approx(0.70)
    cur = db_conn.cursor()
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["state"] == "refunded"


def test_xunhupay_partial_refund_fails_closed_before_provider_call(db_conn, monkeypatch):
    from services import dealer_inventory_resale as resale

    cur = _setup_water_chain(db_conn)
    resale.request_refund(
        cur,
        order_id="O-L2-SVC",
        requested_by_user_id=30,
        processing_cost_cents=3,
        processing_cost_evidence={"invoice": "actual-handling-cost-3c"},
    )
    cur.execute(
        "UPDATE recharge_orders SET actual_payment_channel='xunhupay' WHERE id='O-L2-SVC'"
    )
    db_conn.commit()
    called = False

    async def forbidden_provider_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("partial Xunhupay refund must not be initiated")

    monkeypatch.setattr(
        "services.xunhupay.refund_xunhupay_order",
        forbidden_provider_call,
        raising=False,
    )
    result = _wallet_admin_client().post(
        "/api/wallet/wechat-refund",
        json={"order_id": "O-L2-SVC", "refund_yuan": 0.70, "reason": "unused lot"},
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "XUNHUPAY_PARTIAL_REFUND_UNSUPPORTED"
    assert called is False
    cur = db_conn.cursor()
    cur.execute("SELECT status FROM dealer_resale_refunds WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["status"] == "manual_review"


def test_xunhupay_generic_order_cd_is_not_refund_terminal():
    from services.refund_cash_execution import (
        RefundExecutionError,
        _normalize_provider_result,
    )

    claim = {
        "provider": "xunhupay",
        "order_id": "O-XHP-CANCELLED",
        "out_refund_no": "RO-XHP-CANCELLED",
        "amount_cents": 7000,
        "total_cents": 7000,
    }
    with pytest.raises(RefundExecutionError) as exc_info:
        _normalize_provider_result(
            claim,
            {
                "errcode": 0,
                "out_trade_order": "O-XHP-CANCELLED",
                "status": "CD",
                "total_fee": "70.00",
            },
        )
    assert exc_info.value.code == "XUNHUPAY_ORDER_QUERY_NOT_REFUND_PROOF"


def test_xunhupay_generic_order_cd_cannot_reverse_internal_state(db_conn, monkeypatch):
    case_id = _queue_consumer_refund(db_conn, route="xunhupay")
    provider_called = False
    cur = db_conn.cursor()
    cur.execute(
        "SELECT refund_status,refund_completed_at,refunded_amount_cents "
        "FROM recharge_orders WHERE id='O-SVC-CUSTOMER'"
    )
    before_recharge = dict(cur.fetchone())

    async def cancelled_order_query(order_id, *, strict=False):
        assert strict is True
        return {
            "errcode": 0,
            "out_trade_order": order_id,
            "status": "CD",
            "total_fee": "0.80",
        }

    async def forbidden_refund(*args, **kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("generic order cancellation must not initiate or prove a refund")

    monkeypatch.setattr("services.xunhupay.query_xunhupay_order", cancelled_order_query)
    monkeypatch.setattr("services.xunhupay.refund_xunhupay_order", forbidden_refund)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    result = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "XUNHUPAY_ORDER_QUERY_NOT_REFUND_PROOF"
    assert provider_called is False
    cur.execute(
        "SELECT status FROM service_refund_cash_jobs WHERE case_id=%s",
        (case_id,),
    )
    assert cur.fetchone()["status"] == "manual_review"
    cur.execute(
        "SELECT refund_status,refund_completed_at,refunded_amount_cents "
        "FROM recharge_orders WHERE id='O-SVC-CUSTOMER'"
    )
    recharge = dict(cur.fetchone())
    assert recharge["refund_status"] != "completed"
    assert recharge["refund_completed_at"] is None
    assert recharge == before_recharge


def test_admin_cash_queue_executes_full_xunhupay_refund_via_signed_refund_response(
    db_conn, monkeypatch,
):
    case_id = _queue_consumer_refund(db_conn, route="xunhupay")
    calls = []

    async def fake_refund(order_id, reason=""):
        calls.append(("refund", order_id, reason))
        return {
            "errcode": 0,
            "trade_order_id": order_id,
            "transaction_id": "PAY-CUSTOMER",
            "refund_status": "CD",
            "out_refund_no": "XHP-REFUND-80",
            "refund_fee": "0.80",
        }

    async def fake_query(order_id, *, strict=False):
        assert strict is True
        calls.append(("query", order_id))
        return {
            "errcode": 0,
            "out_trade_order": order_id,
            "status": "OD",
            "transaction_id": "XHP-PAID-80",
            "total_fee": "0.80",
        }

    monkeypatch.setattr("services.xunhupay.refund_xunhupay_order", fake_refund)
    monkeypatch.setattr("services.xunhupay.query_xunhupay_order", fake_query)
    client = _client()
    jobs = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"]
    executed = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{jobs[0]['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={"reason": "approved full refund"},
    )
    assert executed.status_code == 200, executed.text
    assert [item[0] for item in calls] == ["query", "refund"]
    cur = db_conn.cursor()
    cur.execute(
        "SELECT status,provider_refund_id,provider_evidence_jsonb "
        "FROM service_refund_cash_jobs WHERE case_id=%s",
        (case_id,),
    )
    job = cur.fetchone()
    assert (job["status"], job["provider_refund_id"]) == ("completed", "XHP-REFUND-80")
    assert (
        job["provider_evidence_jsonb"]["refund_evidence_kind"]
        == "signed_xunhupay_refund_response"
    )


def test_xunhupay_rd_response_stays_processing_and_retry_does_not_reinitiate(
    db_conn, monkeypatch,
):
    _queue_consumer_refund(db_conn, route="xunhupay")
    refund_calls = 0

    async def paid_order_query(order_id, *, strict=False):
        assert strict is True
        return {
            "errcode": 0,
            "out_trade_order": order_id,
            "status": "OD",
            "total_fee": "0.80",
        }

    async def processing_refund(order_id, reason=""):
        nonlocal refund_calls
        refund_calls += 1
        return {
            "errcode": 0,
            "trade_order_id": order_id,
            "transaction_id": "PAY-CUSTOMER",
            "refund_status": "RD",
            "out_refund_no": "XHP-RD-80",
            "refund_fee": "0.80",
        }

    monkeypatch.setattr("services.xunhupay.query_xunhupay_order", paid_order_query)
    monkeypatch.setattr("services.xunhupay.refund_xunhupay_order", processing_refund)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    first = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert first.status_code == 200, first.text
    assert first.json()["refund_execution"]["status"] == "provider_processing"
    second = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert second.status_code == 200, second.text
    assert second.json()["refund_execution"]["status"] == "provider_processing"
    assert refund_calls == 1


def test_xunhupay_od_refund_response_is_retryable_without_internal_reversal(
    db_conn, monkeypatch,
):
    _queue_consumer_refund(db_conn, route="xunhupay")
    refund_calls = 0

    async def paid_order_query(order_id, *, strict=False):
        assert strict is True
        return {
            "errcode": 0,
            "out_trade_order": order_id,
            "status": "OD",
            "total_fee": "0.80",
        }

    async def retryable_refund(order_id, reason=""):
        nonlocal refund_calls
        refund_calls += 1
        return {
            "errcode": 0,
            "trade_order_id": order_id,
            "transaction_id": "PAY-CUSTOMER",
            "refund_status": "OD",
        }

    monkeypatch.setattr("services.xunhupay.query_xunhupay_order", paid_order_query)
    monkeypatch.setattr("services.xunhupay.refund_xunhupay_order", retryable_refund)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]

    first = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert first.status_code == 200, first.text
    assert first.json()["refund_execution"]["status"] == "failed"
    cur = db_conn.cursor()
    cur.execute(
        "SELECT status,provider_refund_id,provider_evidence_jsonb FROM service_refund_cash_jobs "
        "WHERE cash_job_id=%s",
        (job["cash_job_id"],),
    )
    durable = cur.fetchone()
    assert durable["status"] == "failed"
    assert durable["provider_refund_id"] is None
    assert durable["provider_evidence_jsonb"]["provider_status"] == "OD"
    cur.execute(
        "SELECT refund_status,refund_completed_at FROM recharge_orders "
        "WHERE id='O-SVC-CUSTOMER'",
    )
    recharge = cur.fetchone()
    assert recharge["refund_status"] != "completed"
    assert recharge["refund_completed_at"] is None

    second = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert second.status_code == 200, second.text
    assert second.json()["refund_execution"]["status"] == "failed"
    assert refund_calls == 2


def test_xunhupay_callback_keeps_payment_and_refund_references_separate(
    db_conn, monkeypatch,
):
    _queue_consumer_refund(db_conn, route="xunhupay")

    async def paid_order_query(order_id, *, strict=False):
        assert strict is True
        return {
            "errcode": 0,
            "out_trade_order": order_id,
            "status": "OD",
            "total_fee": "0.80",
        }

    async def processing_refund(order_id, reason=""):
        return {
            "errcode": 0,
            "trade_order_id": order_id,
            "transaction_id": "PAY-CUSTOMER",
            "refund_status": "RD",
            "out_refund_no": "XHP-REAL-REFUND-80",
            "refund_fee": "0.80",
        }

    monkeypatch.setattr("services.xunhupay.query_xunhupay_order", paid_order_query)
    monkeypatch.setattr("services.xunhupay.refund_xunhupay_order", processing_refund)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    initiated = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert initiated.status_code == 200, initiated.text

    from api.wallet_api import _flag_channel_refund

    assert _flag_channel_refund(
        "O-SVC-CUSTOMER",
        "CD",
        "0.80",
        channel="xunhupay",
        payment_transaction_id="PAY-CUSTOMER",
    ) is True
    cur = db_conn.cursor()
    cur.execute(
        "SELECT settlement_snapshot_jsonb FROM recharge_orders WHERE id='O-SVC-CUSTOMER'",
    )
    snapshot = cur.fetchone()["settlement_snapshot_jsonb"]
    assert snapshot["provider_refund_id"] == "XHP-REAL-REFUND-80"
    assert snapshot["payment_transaction_id"] == "PAY-CUSTOMER"
    assert snapshot["provider_refund_id"] != snapshot["payment_transaction_id"]
    assert (
        snapshot["provider_refund_reference_kind"]
        == "signed_xunhupay_refund_response"
    )
    assert _flag_channel_refund(
        "O-SVC-CUSTOMER",
        "CD",
        "0.80",
        channel="xunhupay",
        payment_transaction_id="PAY-CUSTOMER",
    ) is True
    assert _flag_channel_refund(
        "O-SVC-CUSTOMER",
        "CD",
        "0.80",
        channel="xunhupay",
        payment_transaction_id="WRONG-PAYMENT-TXN",
    ) is False


def test_xunhupay_cd_without_persisted_refund_number_fails_closed(db_conn):
    _queue_consumer_refund(db_conn, route="xunhupay")
    cur = db_conn.cursor()
    cur.execute(
        "SELECT refund_status,refund_completed_at FROM recharge_orders "
        "WHERE id='O-SVC-CUSTOMER'",
    )
    before = dict(cur.fetchone())

    from api.wallet_api import _flag_channel_refund

    assert _flag_channel_refund(
        "O-SVC-CUSTOMER",
        "CD",
        "0.80",
        channel="xunhupay",
        payment_transaction_id="PAY-CUSTOMER",
    ) is False
    cur.execute(
        "SELECT refund_status,refund_completed_at FROM recharge_orders "
        "WHERE id='O-SVC-CUSTOMER'",
    )
    assert dict(cur.fetchone()) == before


def test_xunhupay_signed_success_internal_failure_replays_only_local_terminal(
    db_conn, monkeypatch,
):
    _queue_consumer_refund(db_conn, route="xunhupay")
    query_calls = 0
    refund_calls = 0

    async def paid_order_query(order_id, *, strict=False):
        nonlocal query_calls
        assert strict is True
        query_calls += 1
        return {
            "errcode": 0,
            "out_trade_order": order_id,
            "status": "OD",
            "total_fee": "0.80",
        }

    async def signed_success(order_id, reason=""):
        nonlocal refund_calls
        refund_calls += 1
        return {
            "errcode": 0,
            "trade_order_id": order_id,
            "transaction_id": "PAY-CUSTOMER",
            "refund_status": "CD",
            "out_refund_no": "XHP-COMPENSATE-80",
            "refund_fee": "0.80",
        }

    from services import refund_cash_execution as execution

    original_persist = execution._persist_provider_result
    persist_calls = 0

    def fail_local_once(claim, evidence):
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 1:
            raise RuntimeError("forced local reversal failure after signed refund success")
        return original_persist(claim, evidence)

    monkeypatch.setattr("services.xunhupay.query_xunhupay_order", paid_order_query)
    monkeypatch.setattr("services.xunhupay.refund_xunhupay_order", signed_success)
    monkeypatch.setattr(execution, "_persist_provider_result", fail_local_once)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]

    first = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert first.status_code == 503
    assert (
        first.json()["detail"]["code"]
        == "REFUND_EXTERNAL_SUCCESS_RECONCILIATION_PENDING"
    )
    cur = db_conn.cursor()
    cur.execute(
        "SELECT status,provider_refund_id,provider_evidence_jsonb "
        "FROM service_refund_cash_jobs WHERE cash_job_id=%s",
        (job["cash_job_id"],),
    )
    durable = cur.fetchone()
    assert durable["status"] == "manual_review"
    assert durable["provider_refund_id"] == "XHP-COMPENSATE-80"
    assert durable["provider_evidence_jsonb"]["terminal"] == "success"

    second = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert second.status_code == 200, second.text
    assert second.json()["refund_execution"]["status"] == "completed"
    assert query_calls == 1
    assert refund_calls == 1
    assert persist_calls == 2
    cur.execute(
        "SELECT refund_status,refunded_amount_cents FROM recharge_orders "
        "WHERE id='O-SVC-CUSTOMER'"
    )
    recharge = cur.fetchone()
    assert recharge["refund_status"] == "completed"
    assert recharge["refunded_amount_cents"] == 80


def test_external_success_internal_failure_creates_durable_manual_review(db_conn, monkeypatch):
    _queue_consumer_refund(db_conn)
    refund_called = False

    async def fake_refund_order(**kwargs):
        nonlocal refund_called
        refund_called = True
        return {"status": "PROCESSING", "refund_id": "WX-EXTERNAL-SUCCESS"}

    async def fake_query_refund(out_refund_no, *, strict=False):
        assert strict is True
        return {
            "status": "SUCCESS",
            "refund_id": "WX-EXTERNAL-SUCCESS",
            "out_trade_no": "O-SVC-CUSTOMER",
            "out_refund_no": out_refund_no,
            "amount": {"refund": 80, "total": 80},
        }

    def fail_internal(*args, **kwargs):
        raise RuntimeError("forced post-provider database failure")

    monkeypatch.setattr("services.wechat_pay.refund_order", fake_refund_order)
    monkeypatch.setattr("services.wechat_pay.query_refund", fake_query_refund)
    monkeypatch.setattr("services.refund_cash_execution._persist_provider_result", fail_internal)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    result = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={"reason": "forced compensation proof"},
    )
    assert result.status_code == 503
    assert result.json()["detail"]["code"] == "REFUND_EXTERNAL_SUCCESS_RECONCILIATION_PENDING"
    assert refund_called is False
    cur = db_conn.cursor()
    cur.execute(
        "SELECT status,provider_refund_id,last_error FROM service_refund_cash_jobs "
        "WHERE cash_job_id=%s",
        (job["cash_job_id"],),
    )
    durable = cur.fetchone()
    assert durable["status"] == "manual_review"
    assert durable["provider_refund_id"] == "WX-EXTERNAL-SUCCESS"
    assert "内部终态待补偿" in durable["last_error"]
    cur.execute("SELECT refund_status FROM recharge_orders WHERE id='O-SVC-CUSTOMER'")
    assert cur.fetchone()["refund_status"] != "completed"


def test_provider_success_with_wrong_amount_is_durable_manual_review(db_conn, monkeypatch):
    _queue_consumer_refund(db_conn)

    async def fake_refund_order(**kwargs):
        return {"status": "PROCESSING", "refund_id": "WX-WRONG-AMOUNT"}

    async def fake_query_refund(out_refund_no, *, strict=False):
        assert strict is True
        return {
            "status": "SUCCESS",
            "refund_id": "WX-WRONG-AMOUNT",
            "out_trade_no": "O-SVC-CUSTOMER",
            "out_refund_no": out_refund_no,
            "amount": {"refund": 79, "total": 80},
        }

    monkeypatch.setattr("services.wechat_pay.refund_order", fake_refund_order)
    monkeypatch.setattr("services.wechat_pay.query_refund", fake_query_refund)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    result = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "REFUND_PROVIDER_AMOUNT_MISMATCH"
    cur = db_conn.cursor()
    cur.execute(
        "SELECT status,provider_refund_id,last_error FROM service_refund_cash_jobs "
        "WHERE cash_job_id=%s",
        (job["cash_job_id"],),
    )
    durable = cur.fetchone()
    assert durable["status"] == "manual_review"
    assert durable["provider_refund_id"] == "WX-WRONG-AMOUNT"
    assert "快照冲突" in durable["last_error"]
    cur.execute("SELECT refund_status FROM recharge_orders WHERE id='O-SVC-CUSTOMER'")
    assert cur.fetchone()["refund_status"] != "completed"


def test_failed_cash_job_retries_same_persisted_amount_and_idempotency_key(db_conn, monkeypatch):
    _queue_consumer_refund(db_conn)
    attempts = []
    provider_accepted = False

    async def flaky_refund_order(**kwargs):
        nonlocal provider_accepted
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise RuntimeError("temporary provider outage")
        provider_accepted = True
        return {"status": "PROCESSING", "refund_id": "WX-RETRY-80"}

    async def successful_query(out_refund_no, *, strict=False):
        assert strict is True
        if not provider_accepted:
            return {"status": "NOT_FOUND", "out_refund_no": out_refund_no}
        return {
            "status": "SUCCESS",
            "refund_id": "WX-RETRY-80",
            "out_trade_no": "O-SVC-CUSTOMER",
            "out_refund_no": out_refund_no,
            "amount": {"refund": 80, "total": 80},
        }

    monkeypatch.setattr("services.wechat_pay.refund_order", flaky_refund_order)
    monkeypatch.setattr("services.wechat_pay.query_refund", successful_query)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    url = f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute"
    first = client.post(url, headers={"x-test-user": "999", "x-test-admin": "1"}, json={})
    assert first.status_code == 503
    failed = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=failed",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    assert failed["attention_reason"]
    second = client.post(url, headers={"x-test-user": "999", "x-test-admin": "1"}, json={})
    assert second.status_code == 200, second.text
    assert [call["refund_yuan"] for call in attempts] == [pytest.approx(0.80), pytest.approx(0.80)]
    assert {call["out_refund_no"] for call in attempts} == {"RO-SVC-CUSTOMER"}
    cur = db_conn.cursor()
    cur.execute("SELECT status,attempt_count FROM service_refund_cash_jobs WHERE cash_job_id=%s", (job["cash_job_id"],))
    final = cur.fetchone()
    assert (final["status"], int(final["attempt_count"])) == ("completed", 2)


def test_provider_accept_then_query_outage_never_reinitiates_cash(db_conn, monkeypatch):
    _queue_consumer_refund(db_conn)
    accepted = False
    refund_calls = 0
    query_calls = 0

    async def fake_refund_order(**kwargs):
        nonlocal accepted, refund_calls
        refund_calls += 1
        accepted = True
        return {"status": "PROCESSING", "refund_id": "WX-ACK-ONLY"}

    async def fake_query_refund(out_refund_no, *, strict=False):
        nonlocal query_calls
        assert strict is True
        query_calls += 1
        if query_calls == 1:
            return {"status": "NOT_FOUND", "out_refund_no": out_refund_no}
        if query_calls == 2:
            raise RuntimeError("query transport failed after provider accepted")
        return {
            "status": "SUCCESS",
            "refund_id": "WX-ACK-ONLY",
            "out_trade_no": "O-SVC-CUSTOMER",
            "out_refund_no": out_refund_no,
            "amount": {"refund": 80, "total": 80},
        }

    monkeypatch.setattr("services.wechat_pay.refund_order", fake_refund_order)
    monkeypatch.setattr("services.wechat_pay.query_refund", fake_query_refund)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    url = f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute"
    first = client.post(url, headers={"x-test-user": "999", "x-test-admin": "1"}, json={})
    assert first.status_code == 503
    cur = db_conn.cursor()
    cur.execute(
        "SELECT status,provider_refund_id FROM service_refund_cash_jobs WHERE cash_job_id=%s",
        (job["cash_job_id"],),
    )
    assert tuple(cur.fetchone().values()) == ("provider_processing", "WX-ACK-ONLY")
    second = client.post(url, headers={"x-test-user": "999", "x-test-admin": "1"}, json={})
    assert second.status_code == 200, second.text
    assert refund_calls == 1


def test_completed_cash_job_with_split_order_terminal_fails_closed(db_conn, monkeypatch):
    case_id = _queue_consumer_refund(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        "UPDATE service_refund_cash_jobs SET status='completed',provider_refund_id='WX-SPLIT' "
        "WHERE case_id=%s",
        (case_id,),
    )
    cur.execute("UPDATE consumer_refund_cases SET cash_status='completed' WHERE case_id=%s", (case_id,))
    cur.execute(
        "UPDATE recharge_orders SET refund_status='pending',refund_completed_at=NULL "
        "WHERE id='O-SVC-CUSTOMER'"
    )
    db_conn.commit()

    async def forbidden_provider_call(*args, **kwargs):
        raise AssertionError("split local terminal must fail before any provider call")

    monkeypatch.setattr("services.wechat_pay.refund_order", forbidden_provider_call)
    monkeypatch.setattr("services.wechat_pay.query_refund", forbidden_provider_call)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=completed",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    result = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "REFUND_JOB_TERMINAL_SPLIT"


def test_signed_xunhupay_refund_response_with_wrong_fee_is_held(db_conn, monkeypatch):
    _queue_consumer_refund(db_conn, route="xunhupay")
    refund_called = False

    async def wrong_fee_refund(order_id, reason=""):
        nonlocal refund_called
        refund_called = True
        return {
            "errcode": 0,
            "trade_order_id": order_id,
            "transaction_id": "PAY-CUSTOMER",
            "refund_status": "CD",
            "out_refund_no": "XHP-WRONG-FEE",
            "refund_fee": "0.79",
        }

    async def paid_order_query(order_id, *, strict=False):
        assert strict is True
        return {
            "errcode": 0,
            "out_trade_order": order_id,
            "status": "OD",
            "transaction_id": "XHP-PAID-80",
            "total_fee": "0.80",
        }

    monkeypatch.setattr("services.xunhupay.refund_xunhupay_order", wrong_fee_refund)
    monkeypatch.setattr("services.xunhupay.query_xunhupay_order", paid_order_query)
    client = _client()
    job = client.get(
        "/api/admin/dealer-resale/refund-cash-jobs?status=queued",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    ).json()["items"][0]
    result = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "REFUND_PROVIDER_AMOUNT_MISMATCH"
    assert refund_called is True
    cur = db_conn.cursor()
    cur.execute("SELECT status FROM service_refund_cash_jobs WHERE cash_job_id=%s", (job["cash_job_id"],))
    assert cur.fetchone()["status"] == "manual_review"
    retry = client.post(
        f"/api/admin/dealer-resale/refund-cash-jobs/{job['cash_job_id']}/execute",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={},
    )
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "REFUND_JOB_MANUAL_REVIEW"
    assert refund_called is True


@pytest.mark.asyncio
async def test_xunhupay_active_query_uses_official_order_field(db_conn, monkeypatch):
    from services import xunhupay

    monkeypatch.setattr(xunhupay, "XUNHUPAY_APPID", "app-test")
    monkeypatch.setattr(xunhupay, "XUNHUPAY_APPSECRET", "secret-test")
    captured = {}
    response = {
        "errcode": 0,
        "trade_order_id": "O-XHP-OFFICIAL-FIELD",
        "status": "OD",
        "total_fee": "0.80",
    }
    response["hash"] = xunhupay._generate_hash(response, "secret-test")

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            import json

            return json.dumps(response)

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, data):
            captured.update(data)
            return FakeResponse()

    monkeypatch.setattr(xunhupay.aiohttp, "ClientSession", FakeSession)
    result = await xunhupay.query_xunhupay_order("O-XHP-OFFICIAL-FIELD", strict=True)
    assert captured["out_trade_order"] == "O-XHP-OFFICIAL-FIELD"
    assert "trade_order_id" not in captured
    assert result and result["status"] == "OD"


@pytest.mark.asyncio
async def test_twenty_concurrent_cash_executors_initiate_provider_once(db_conn, monkeypatch):
    _queue_consumer_refund(db_conn)
    accepted = False
    refund_calls = 0

    async def fake_query_refund(out_refund_no, *, strict=False):
        assert strict is True
        await asyncio.sleep(0.01)
        if not accepted:
            return {"status": "NOT_FOUND", "out_refund_no": out_refund_no}
        return {
            "status": "SUCCESS",
            "refund_id": "WX-CONCURRENT-ONE",
            "out_trade_no": "O-SVC-CUSTOMER",
            "out_refund_no": out_refund_no,
            "amount": {"refund": 80, "total": 80},
        }

    async def fake_refund_order(**kwargs):
        nonlocal accepted, refund_calls
        refund_calls += 1
        accepted = True
        return {"status": "PROCESSING", "refund_id": "WX-CONCURRENT-ONE"}

    monkeypatch.setattr("services.wechat_pay.query_refund", fake_query_refund)
    monkeypatch.setattr("services.wechat_pay.refund_order", fake_refund_order)
    from services.refund_cash_execution import execute_persisted_refund

    results = await asyncio.gather(
        *(execute_persisted_refund(order_id="O-SVC-CUSTOMER") for _ in range(20))
    )
    assert refund_calls == 1
    assert all(item["status"] in {"completed", "provider_processing"} for item in results)
    cur = db_conn.cursor()
    cur.execute("SELECT status,attempt_count FROM service_refund_cash_jobs WHERE source_order_id='O-SVC-CUSTOMER'")
    row = cur.fetchone()
    assert row["status"] == "completed" and int(row["attempt_count"]) == 1


def test_order_detail_export_and_chain_enforce_one_hop_idor(db_conn):
    _setup_water_chain(db_conn)
    client = _client()

    ancestor = client.get(
        "/api/dealer-resale/orders/O-L2-SVC", headers={"x-test-user": "10"}
    )
    assert ancestor.status_code == 403
    assert client.get(
        "/api/dealer-resale/orders/O-L2-SVC/export", headers={"x-test-user": "10"}
    ).status_code == 403

    seller = client.get(
        "/api/dealer-resale/orders/O-L2-SVC", headers={"x-test-user": "20"}
    )
    assert seller.status_code == 200
    seller_data = seller.json()
    assert seller_data["direction"] == "sale"
    assert "margin_cents" not in seller_data
    assert "seller_cost_basis_cents" not in seller_data
    assert "downstream_markup_bps" not in seller_data
    assert "buyer_user_id" not in seller_data and "seller_user_id" not in seller_data

    buyer = client.get(
        "/api/dealer-resale/orders/O-L2-SVC", headers={"x-test-user": "30"}
    )
    assert buyer.status_code == 200
    assert "margin_cents" not in buyer.json()
    assert buyer.json()["refund_handling"] == "PLATFORM_MANAGED"
    assert "counterparty_account_code" not in buyer.json()
    assert "refund_responsible_party" not in buyer.json()

    admin = client.get(
        "/api/admin/dealer-resale/orders/O-L2-SVC/chain",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    )
    assert admin.status_code == 200
    assert {row["order_id"] for row in admin.json()["orders"]} == {
        "O-PLATFORM-L1", "O-L1-L2", "O-L2-SVC",
    }


def test_b2b_refund_endpoint_requires_explicit_whole_lot_ack(db_conn):
    _setup_water_chain(db_conn)
    client = _client()
    denied = client.post(
        "/api/dealer-resale/orders/O-L2-SVC/refund",
        headers={"x-test-user": "30"},
        json={"reason": "unused", "acknowledge_whole_lot_and_72h": False},
    )
    assert denied.status_code == 422

    accepted = client.post(
        "/api/dealer-resale/orders/O-L2-SVC/refund",
        headers={"x-test-user": "30"},
        json={"reason": "unused", "acknowledge_whole_lot_and_72h": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["refund"]["status"] == "requested"
    assert "responsible_seller_user_id" not in accepted.json()["refund"]
    assert "requested_by_user_id" not in accepted.json()["refund"]
    assert "responsible_party" not in accepted.json()["refund"]
    assert accepted.json()["refund"]["refund_handling"] == "PLATFORM_MANAGED"
    assert accepted.json()["cash_execution_required"] is True


def test_admin_external_b2b_material_cannot_create_cash_terminal(db_conn):
    _setup_water_chain(db_conn)
    client = _client()
    requested = client.post(
        "/api/dealer-resale/orders/O-L2-SVC/refund",
        headers={"x-test-user": "30"},
        json={"reason": "unused", "acknowledge_whole_lot_and_72h": True},
    )
    assert requested.status_code == 200
    payload = {
        "amount_cents": 70,
        "provider": "xunhupay_manual",
        "external_refund_id": "XHP-R-O-L2-SVC",
        "external_completed_at": "2026-07-15T12:00:00+00:00",
        "evidence": {"operator_ticket": "throwaway-ticket-001"},
    }
    denied = client.post(
        "/api/admin/dealer-resale/orders/O-L2-SVC/external-refund-complete",
        headers={"x-test-user": "30"}, json=payload,
    )
    assert denied.status_code == 403
    first = client.post(
        "/api/admin/dealer-resale/orders/O-L2-SVC/external-refund-complete",
        headers={"x-test-user": "999", "x-test-admin": "1"}, json=payload,
    )
    second = client.post(
        "/api/admin/dealer-resale/orders/O-L2-SVC/external-refund-complete",
        headers={"x-test-user": "999", "x-test-admin": "1"}, json=payload,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["refund"]["status"] == "pending_verification"
    assert first.json()["refund"]["idempotent"] is False
    assert second.json()["refund"]["idempotent"] is True
    assert first.json()["cash_terminal_recorded"] is False
    assert first.json()["automatic_inventory_or_profit_movement"] is False
    assert first.json()["ancestor_orders_touched"] == 0
    conflicting = client.post(
        "/api/admin/dealer-resale/orders/O-L2-SVC/external-refund-complete",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={**payload, "external_refund_id": "DIFFERENT-REFUND"},
    )
    assert conflicting.status_code == 422
    assert conflicting.json()["detail"]["code"] == "REFUND_IDEMPOTENCY_CONFLICT"
    cur = db_conn.cursor()
    assert _wallet(cur, 20)["paid_inventory_points"] == 0
    assert _wallet(cur, 30)["paid_inventory_points"] == 0
    assert _wallet(cur, 30)["frozen_inventory_points"] == 1
    assert _wallet(cur, 10)["paid_inventory_points"] == 0
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L1-L2'")
    assert cur.fetchone()["state"] == "paid"
    cur.execute("SELECT state FROM dealer_resale_orders WHERE order_id='O-L2-SVC'")
    assert cur.fetchone()["state"] == "refund_pending"
    cur.execute(
        "SELECT refund_status,refund_completed_at,refunded_amount_cents "
        "FROM recharge_orders WHERE id='O-L2-SVC'"
    )
    recharge = cur.fetchone()
    assert recharge["refund_status"] != "completed"
    assert recharge["refund_completed_at"] is None
    assert int(recharge["refunded_amount_cents"] or 0) == 0
    cur.execute("SELECT status FROM dealer_resale_hop_profit_ledger WHERE root_order_id='O-L2-SVC'")
    assert cur.fetchone()["status"] == "pending"


def test_consumer_refund_request_review_and_detail_are_direct_party_only(db_conn):
    _sell_water_to_consumer(db_conn)
    client = _client()
    denied = client.post(
        "/api/dealer-resale/consumer/orders/O-SVC-CUSTOMER/refunds",
        headers={"x-test-user": "20"}, json={"reason": "not my order"},
    )
    assert denied.status_code == 403
    created = client.post(
        "/api/dealer-resale/consumer/orders/O-SVC-CUSTOMER/refunds",
        headers={"x-test-user": "40"}, json={"reason": "unused"},
    )
    assert created.status_code == 200
    assert "responsible_party" not in created.json()
    assert created.json()["refund_handling"] == "PLATFORM_MANAGED"
    assert created.json()["b2b_72h_rule_applied"] is False
    case_id = created.json()["refund"]["case_id"]
    assert "consumer_user_id" not in created.json()["refund"]
    assert "responsible_service_user_id" not in created.json()["refund"]

    assert client.get(
        f"/api/dealer-resale/consumer/refunds/{case_id}", headers={"x-test-user": "10"},
    ).status_code == 403
    consumer_view = client.get(
        f"/api/dealer-resale/consumer/refunds/{case_id}", headers={"x-test-user": "40"},
    )
    assert consumer_view.status_code == 200
    assert consumer_view.json()["refund_handling"] == "PLATFORM_MANAGED"
    assert "responsible_service_account_code" not in consumer_view.json()
    assert "responsible_service_user_id" not in consumer_view.json()

    wrong_seller = client.post(
        f"/api/dealer-resale/consumer/refunds/{case_id}/review",
        headers={"x-test-user": "20"}, json={"approve": True, "note": "wrong hop"},
    )
    assert wrong_seller.status_code == 403
    reviewed = client.post(
        f"/api/dealer-resale/consumer/refunds/{case_id}/review",
        headers={"x-test-user": "30"}, json={"approve": True, "note": "confirmed"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["refund"]["status"] == "platform_execution"
    assert reviewed.json()["internal_ledger_settled"] is True
    assert reviewed.json()["cash_completed"] is False
    assert "consumer_user_id" not in reviewed.json()["refund"]
    assert "responsible_service_user_id" not in reviewed.json()["refund"]


def test_manufacturer_origin_issue_is_admin_only_audited_and_idempotent(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 999)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    db_conn.commit()
    client = _client()
    payload = {
        "points": 5,
        "acquisition_cost_cents": 250,
        "pricing_version": "factory-2026-07-15",
        "idempotency_key": "invoice-factory-001",
        "evidence": {"invoice": "factory-001", "approved_reason": "throwaway test"},
    }
    assert client.post(
        "/api/admin/dealer-resale/manufacturer-lots/issue",
        headers={"x-test-user": "1"}, json=payload,
    ).status_code == 403
    first = client.post(
        "/api/admin/dealer-resale/manufacturer-lots/issue",
        headers={"x-test-user": "999", "x-test-admin": "1"}, json=payload,
    )
    second = client.post(
        "/api/admin/dealer-resale/manufacturer-lots/issue",
        headers={"x-test-user": "999", "x-test-admin": "1"}, json=payload,
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["issue_id"] == second.json()["issue_id"]
    cur = db_conn.cursor()
    assert _wallet(cur, 1)["paid_inventory_points"] == 5
    cur.execute("SELECT COUNT(*) AS c FROM dealer_manufacturer_lot_issuances")
    assert int(cur.fetchone()["c"]) == 1


def test_admin_promotion_governance_and_campaign_origin_lot_are_operational(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 30, 999)
    cur.execute("UPDATE dealer_resale_global_settings SET platform_seller_user_id=1")
    db_conn.commit()
    client = _client()
    now = datetime.now(timezone.utc)
    payload = {
        "funding_scope": "PLATFORM",
        "seller_user_id": None,
        "product_code": "WATER",
        "root_catalog_version": "proc-test-v1",
        "promotion_discount_bps": 9000,
        "eligibility": {"buyer_user_ids": [30], "order_kinds": ["B2B"], "min_points": 1},
        "status": "draft",
        "campaign_version": "p90-v1",
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "expected_row_version": 0,
    }
    url = "/api/admin/dealer-resale/promotions/P90-ADMIN"
    assert client.put(url, headers={"x-test-user": "30"}, json=payload).status_code == 403
    first = client.put(
        url, headers={"x-test-user": "999", "x-test-admin": "1"}, json=payload,
    )
    retry = client.put(
        url, headers={"x-test-user": "999", "x-test-admin": "1"}, json=payload,
    )
    assert first.status_code == retry.status_code == 200
    assert retry.json()["idempotent"] is True
    issue = {
        "points": 5,
        "acquisition_cost_cents": 250,
        "pricing_version": "proc-test-v1",
        "idempotency_key": "factory-p90-lot-001",
        "promotion_campaign_id": "P90-ADMIN",
        "evidence": {"invoice": "factory-p90-001", "approved_reason": "promotion test"},
    }
    issued = client.post(
        "/api/admin/dealer-resale/manufacturer-lots/issue",
        headers={"x-test-user": "999", "x-test-admin": "1"}, json=issue,
    )
    issued_retry = client.post(
        "/api/admin/dealer-resale/manufacturer-lots/issue",
        headers={"x-test-user": "999", "x-test-admin": "1"}, json=issue,
    )
    assert issued.status_code == issued_retry.status_code == 200
    assert issued.json()["promotion_campaign_id"] == "P90-ADMIN"
    immutable_mutation = dict(payload)
    immutable_mutation["promotion_discount_bps"] = 8000
    immutable_mutation["expected_row_version"] = first.json()["row_version"]
    assert client.put(
        url, headers={"x-test-user": "999", "x-test-admin": "1"},
        json=immutable_mutation,
    ).status_code == 422
    activation = dict(payload)
    activation["status"] = "active"
    activation["expected_row_version"] = first.json()["row_version"]
    activated = client.put(
        url, headers={"x-test-user": "999", "x-test-admin": "1"}, json=activation,
    )
    assert activated.status_code == 200
    invalid = dict(payload)
    invalid["eligibility"] = {"region": "CN"}
    assert client.put(
        "/api/admin/dealer-resale/promotions/INVALID-RULE",
        headers={"x-test-user": "999", "x-test-admin": "1"}, json=invalid,
    ).status_code == 422
    listed = client.get(
        "/api/admin/dealer-resale/promotions",
        headers={"x-test-user": "999", "x-test-admin": "1"},
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["campaign_id"] == "P90-ADMIN"

    assert client.post(
        f"{url}/end", headers={"x-test-user": "999", "x-test-admin": "1"},
        json={"expected_row_version": 999},
    ).status_code == 409
    ended = client.post(
        f"{url}/end", headers={"x-test-user": "999", "x-test-admin": "1"},
        json={"expected_row_version": activated.json()["row_version"]},
    )
    repeated_end = client.post(
        f"{url}/end", headers={"x-test-user": "999", "x-test-admin": "1"},
        json={"expected_row_version": activated.json()["row_version"]},
    )
    assert ended.status_code == repeated_end.status_code == 200
    assert ended.json()["status"] == "ended"
    assert repeated_end.json()["idempotent"] is True
    issue["idempotency_key"] = "factory-p90-lot-after-end"
    assert client.post(
        "/api/admin/dealer-resale/manufacturer-lots/issue",
        headers={"x-test-user": "999", "x-test-admin": "1"}, json=issue,
    ).status_code == 422


def test_resale_flag_requires_dual_catalog_and_quote_before_new_agent_order(db_conn):
    from services.agent_agreement import CURRENT_CONTENT_HASH, CURRENT_VERSION

    cur = db_conn.cursor()
    _seed_users(cur, 10)
    cur.execute(
        """INSERT INTO agent_factory_agreements
             (agent_user_id,version,status,content_hash,signed_at)
           VALUES (10,%s,'signed',%s,NOW())""",
        (CURRENT_VERSION, "wrong-body-hash"),
    )
    cur.execute(
        """INSERT INTO system_settings(key,value,value_type)
           VALUES ('PRICING_DUAL_SSOT_ENABLED','false','boolean'),
                  ('PRICING_QUOTE_REQUIRED','false','boolean'),
                  ('CHANNEL_PRICING_ENABLED','true','boolean')
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"""
    )
    db_conn.commit()
    client = _agent_client()
    stale_signature = client.post(
        "/api/agent/inventory/purchase-preview",
        json={"amount_cents": 50},
    )
    assert stale_signature.status_code == 403
    cur = db_conn.cursor()
    cur.execute(
        """UPDATE agent_factory_agreements SET content_hash=%s
           WHERE agent_user_id=10 AND version=%s""",
        (CURRENT_CONTENT_HASH, CURRENT_VERSION),
    )
    db_conn.commit()
    prerequisite = client.post(
        "/api/agent/inventory/purchase-preview",
        json={"amount_cents": 50},
    )
    assert prerequisite.status_code == 503
    assert prerequisite.json()["detail"] == {
        "code": "PROCUREMENT_PRICING_PREREQUISITE_MISSING",
        "message": "当前进货价格配置尚未就绪，请稍后重试",
    }

    cur = db_conn.cursor()
    cur.execute(
        "UPDATE system_settings SET value='true' WHERE key='PRICING_DUAL_SSOT_ENABLED'"
    )
    db_conn.commit()
    quote_required = client.post("/api/agent/inventory/purchase", json={})
    assert quote_required.status_code == 422
    assert quote_required.json()["detail"] == {
        "code": "PROCUREMENT_QUOTE_REQUIRED",
        "message": "当前进货必须先获取有效报价",
    }


def test_consumer_refund_after_ten_days_requires_durable_service_review_not_b2b_window(db_conn):
    from services import dealer_inventory_resale as resale

    _sell_water_to_consumer(db_conn, paid_at_sql="NOW()-INTERVAL '10 days'")
    client = _wallet_admin_client()
    body = {
        "order_id": "O-SVC-CUSTOMER",
        "reason": "consumer evidence approved",
        "reason_category": "negotiated_other",
        "evidence": {"chat": "customer request"},
    }
    requested = client.post("/api/wallet/refund", json=body)
    assert requested.status_code == 200
    assert requested.json()["refund"]["decision_kind"] == "negotiated"
    assert requested.json()["cash_completed"] is False
    assert _wallet(db_conn.cursor(), 30)["paid_inventory_points"] == 0

    cur = db_conn.cursor()
    cur.execute("SELECT * FROM consumer_refund_cases WHERE source_order_id='O-SVC-CUSTOMER'")
    case = cur.fetchone()
    resale.review_consumer_refund_case(
        cur, case_id=case["case_id"], actor_user_id=30, approve=True, note="approved",
    )
    settled = resale.settle_consumer_refund_case(cur, case_id=case["case_id"])
    assert settled["cash_status"] == "queued"
    db_conn.commit()

    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 1
    cur.execute("SELECT negative_settlement_cents FROM service_refund_liability_ledger")
    assert int(cur.fetchone()["negative_settlement_cents"]) == 0
    completed = _client().post(
        f"/api/admin/dealer-resale/consumer/refunds/{case['case_id']}/cash-complete",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={
            "amount_cents": 80,
            "provider": "wechat",
            "external_refund_id": "WX-REFUND-1",
            "evidence": {"signed_callback": True},
        },
    )
    assert completed.status_code == 422
    assert completed.json()["detail"]["code"] == "CONSUMER_REFUND_PROVIDER_PROOF_MISSING"
    cur = db_conn.cursor()
    cur.execute(
        """UPDATE recharge_orders
           SET refund_status='completed',refund_completed_at=NOW(),refunded_amount_cents=80,
               settlement_snapshot_jsonb=COALESCE(settlement_snapshot_jsonb,'{}'::jsonb)
                 || %s::jsonb
           WHERE id='O-SVC-CUSTOMER'""",
        ('{"refund_provider":"wechat","provider_refund_id":"WX-REFUND-1",'
         '"refund_evidence_kind":"signed_wechat_callback"}',),
    )
    db_conn.commit()
    completed = _client().post(
        f"/api/admin/dealer-resale/consumer/refunds/{case['case_id']}/cash-complete",
        headers={"x-test-user": "999", "x-test-admin": "1"},
        json={
            "amount_cents": 80,
            "provider": "wechat",
            "external_refund_id": "WX-REFUND-1",
            "evidence": {"reconciliation": "matches persisted callback"},
        },
    )
    assert completed.status_code == 200, completed.text
    cur = db_conn.cursor()
    assert _wallet(cur, 30)["paid_inventory_points"] == 1
    cur.execute("SELECT status FROM consumer_refund_cases WHERE source_order_id='O-SVC-CUSTOMER'")
    assert cur.fetchone()["status"] == "completed"


def test_admin_resale_policy_requires_cas_and_writes_atomic_audit(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 10, 999)
    db_conn.commit()
    client = _client()
    endpoint = "/api/admin/dealer-resale/policies/10"
    headers = {
        "x-test-user": "999", "x-test-admin": "1", "x-request-id": "policy-audit-1",
    }
    body = {
        "downstream_markup_bps": 12000,
        "authorized_min_markup_bps": 10000,
        "authorized_max_markup_bps": 15000,
        "policy_version": "policy-v1",
        "expected_row_version": 0,
        "reason": "批准首版经销商价格范围",
    }

    missing_cas = client.put(
        endpoint, headers=headers,
        json={key: value for key, value in body.items() if key != "expected_row_version"},
    )
    assert missing_cas.status_code == 422

    saved = client.put(endpoint, headers=headers, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["row_version"] == 1
    cur = db_conn.cursor()
    cur.execute(
        "SELECT summary,before_snapshot,after_snapshot FROM audit_logs "
        "WHERE module='dealer_resale_pricing' AND entity_type='dealer_resale_policy'"
    )
    audit = cur.fetchone()
    assert "policy-audit-1" in audit["summary"]
    assert '"policy": null' in audit["before_snapshot"]
    assert '"row_version": 1' in audit["after_snapshot"]

    stale = client.put(
        endpoint,
        headers={**headers, "x-request-id": "policy-audit-stale"},
        json={**body, "downstream_markup_bps": 13000, "expected_row_version": 0},
    )
    assert stale.status_code == 409
    cur.execute(
        "SELECT downstream_markup_bps,row_version FROM dealer_resale_policies "
        "WHERE seller_user_id=10"
    )
    assert tuple(cur.fetchone().values()) == (12000, 1)
    cur.execute("SELECT COUNT(*) AS c FROM audit_logs WHERE summary LIKE '%policy-audit-stale%'")
    assert int(cur.fetchone()["c"]) == 0


def test_admin_resale_settings_cas_and_audit_share_one_transaction(db_conn):
    cur = db_conn.cursor()
    _seed_users(cur, 1, 999)
    db_conn.commit()
    client = _client()
    body = {
        "platform_seller_user_id": 1,
        "default_downstream_markup_bps": 11000,
        "settings_version": "settings-v2",
        "expected_row_version": 1,
        "reason": "切换厂家默认售价范围",
    }
    saved = client.put(
        "/api/admin/dealer-resale/settings",
        headers={"x-test-user": "999", "x-test-admin": "1", "x-request-id": "settings-audit-1"},
        json=body,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["row_version"] == 2
    cur = db_conn.cursor()
    cur.execute(
        "SELECT summary,before_snapshot,after_snapshot FROM audit_logs "
        "WHERE module='dealer_resale_pricing' AND entity_type='dealer_resale_settings'"
    )
    audit = cur.fetchone()
    assert "settings-audit-1" in audit["summary"]
    assert '"row_version": 1' in audit["before_snapshot"]
    assert '"row_version": 2' in audit["after_snapshot"]
