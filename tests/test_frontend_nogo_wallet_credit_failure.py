"""Failure semantics for the V3.5 managed customer credit read path."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


class _Cursor:
    def __init__(self):
        self._last_query = ""

    def execute(self, query, _params=None):
        self._last_query = " ".join(str(query).split())
        if "FROM customer_agent_credit_wallets" in self._last_query:
            raise RuntimeError("injected managed-credit read failure")

    def fetchone(self):
        if "user_social_subscriptions" in self._last_query:
            return None
        if "commission_clawback_pending" in self._last_query:
            return {"pending": 0}
        return None


class _Connection:
    def cursor(self):
        return _Cursor()

    def close(self):
        return None


def test_managed_credit_query_failure_is_explicit_not_zero(monkeypatch):
    import db.wallet_db as wallet_db

    monkeypatch.setattr(wallet_db, "get_or_create_wallet", lambda _user_id: {
        "paid_points": 900_000,
        "commission_points": 0,
        "bonus_points": 0,
        "frozen_points": 0,
        "total_recharged": 900_000,
    })
    monkeypatch.setattr(wallet_db, "get_connection", _Connection)

    result = wallet_db.get_wallet_balance(42)

    assert result["customer_credit_status"] == "unavailable"
    assert result["customer_credit"] is None


def test_wallet_http_endpoint_rejects_unavailable_managed_credit(monkeypatch):
    import api.wallet_api as wallet_api
    import config.v3_3_1_flags as flags

    monkeypatch.setattr(flags, "is_v3_3_1_enabled", lambda: False)
    monkeypatch.setattr(wallet_api, "get_wallet_balance", lambda _user_id: {
        "paid_points": 900_000,
        "customer_credit_status": "unavailable",
        "customer_credit": None,
    })

    class _State:
        user = {"user_id": 42}

    class _Request:
        state = _State()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(wallet_api.get_balance(_Request()))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "CUSTOMER_CREDIT_UNAVAILABLE"
