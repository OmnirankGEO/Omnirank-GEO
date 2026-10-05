from __future__ import annotations

import json

import pytest


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return json.dumps(self._payload)


class _FakeSession:
    payload = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, *args, **kwargs):
        return _FakeResponse(dict(self.payload))


@pytest.mark.asyncio
async def test_xunhupay_payment_creation_rejects_invalid_signed_response(monkeypatch):
    from services import xunhupay

    _FakeSession.payload = {
        "openid": "XHP-ORDER-1",
        "url": "https://attacker.invalid/pay",
        "url_qrcode": "https://attacker.invalid/qr",
        "errcode": 0,
        "errmsg": "success!",
        "hash": "invalid",
    }
    monkeypatch.setattr(xunhupay, "XUNHUPAY_APPID", "app-test")
    monkeypatch.setattr(xunhupay, "XUNHUPAY_APPSECRET", "secret-test")
    monkeypatch.setattr(xunhupay.aiohttp, "ClientSession", _FakeSession)
    monkeypatch.setattr(xunhupay, "verify_xunhupay_callback", lambda payload: False)

    with pytest.raises(RuntimeError, match="支付响应签名无效"):
        await xunhupay.create_xunhupay_order("ORDER-1", 70.00)


@pytest.mark.asyncio
async def test_xunhupay_payment_creation_accepts_valid_signed_response(monkeypatch):
    from services import xunhupay

    _FakeSession.payload = {
        "openid": "XHP-ORDER-2",
        "url": "https://api.xunhupay.com/pay/2",
        "url_qrcode": "https://api.xunhupay.com/qr/2",
        "errcode": 0,
        "errmsg": "success!",
        "hash": "valid",
    }
    monkeypatch.setattr(xunhupay, "XUNHUPAY_APPID", "app-test")
    monkeypatch.setattr(xunhupay, "XUNHUPAY_APPSECRET", "secret-test")
    monkeypatch.setattr(xunhupay.aiohttp, "ClientSession", _FakeSession)
    monkeypatch.setattr(xunhupay, "verify_xunhupay_callback", lambda payload: True)

    result = await xunhupay.create_xunhupay_order("ORDER-2", 70.00)
    assert result["url"].startswith("https://api.xunhupay.com/")


def test_xunhupay_refund_response_od_is_explicitly_retryable():
    from services.refund_cash_execution import _normalize_xunhupay_refund_response

    claim = {
        "provider": "xunhupay",
        "order_id": "ORDER-OD",
        "out_refund_no": "R-ORDER-OD",
        "amount_cents": 7000,
        "total_cents": 7000,
    }
    evidence = _normalize_xunhupay_refund_response(
        claim,
        {
            "errcode": 0,
            "trade_order_id": "ORDER-OD",
            "transaction_id": "PAYMENT-TXN-OD",
            "refund_status": "OD",
        },
    )
    assert evidence["terminal"] == "retryable"
    assert evidence["provider_status"] == "OD"
    assert evidence["payment_transaction_id"] == "PAYMENT-TXN-OD"
    assert evidence["provider_refund_id"] == ""
