"""报价定价免责协议闸 单测(2026-06-08 · Option B 实名=X)。

require_pricing_authority(request):
  admin → 豁免 · 服务商(agent_level>=1)→ 放行 · 普通用户已签免责协议 → 放行 · 未签 → 403。
is_pricing_disclaimer_signed(user_id):
  签了 → True · 未签 → False · 异常/None → False(失败安全)。

纯 mock · 不连真 DB。
"""
import pytest
from fastapi import HTTPException

import auth.agreement_gate as gate
import services.agent_agreement as aa


class _FakeCursor:
    def __init__(self, level):
        self._level = level
    def execute(self, *a, **k):
        return None
    def fetchone(self):
        return {"agent_level": self._level}


class _FakeConn:
    def __init__(self, level):
        self._cur = _FakeCursor(level)
    def cursor(self):
        return self._cur
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeState:
    def __init__(self, user):
        self.user = user


class _FakeReq:
    def __init__(self, user):
        self.state = _FakeState(user)


def _patch_gate(monkeypatch, *, level, signed):
    monkeypatch.setattr(gate, "get_db", lambda: _FakeConn(level))
    monkeypatch.setattr(gate, "is_agent_signed", lambda cur, uid, ver: signed)


def test_admin_exempt(monkeypatch):
    _patch_gate(monkeypatch, level=0, signed=False)
    r = gate.require_pricing_authority(_FakeReq({"user_id": 1, "is_admin": True}))
    assert r["allowed"] is True
    assert r.get("exempt_reason") == "admin"


def test_agent_allowed_without_disclaimer(monkeypatch):
    # 服务商(level>=1)走 v2.3·不需另签本免责协议
    _patch_gate(monkeypatch, level=1, signed=False)
    r = gate.require_pricing_authority(_FakeReq({"user_id": 2, "is_admin": False}))
    assert r["allowed"] is True
    assert r.get("exempt_reason") == "agent"


def test_l0_signed_allowed(monkeypatch):
    _patch_gate(monkeypatch, level=0, signed=True)
    r = gate.require_pricing_authority(_FakeReq({"user_id": 3, "is_admin": False}))
    assert r["allowed"] is True


def test_l0_unsigned_403(monkeypatch):
    _patch_gate(monkeypatch, level=0, signed=False)
    with pytest.raises(HTTPException) as ei:
        gate.require_pricing_authority(_FakeReq({"user_id": 4, "is_admin": False}))
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "PRICING_DISCLAIMER_NOT_SIGNED"


def test_no_user_401(monkeypatch):
    _patch_gate(monkeypatch, level=0, signed=False)
    with pytest.raises(HTTPException) as ei:
        gate.require_pricing_authority(_FakeReq(None))
    assert ei.value.status_code == 401


def test_is_pricing_disclaimer_signed_true(monkeypatch):
    monkeypatch.setattr("db.connection.get_db", lambda: _FakeConn(0))
    monkeypatch.setattr(aa, "is_agent_signed", lambda cur, uid, ver: True)
    assert aa.is_pricing_disclaimer_signed(7) is True


def test_is_pricing_disclaimer_signed_false_when_unsigned(monkeypatch):
    monkeypatch.setattr("db.connection.get_db", lambda: _FakeConn(0))
    monkeypatch.setattr(aa, "is_agent_signed", lambda cur, uid, ver: False)
    assert aa.is_pricing_disclaimer_signed(7) is False


def test_is_pricing_disclaimer_signed_none_user_is_false():
    assert aa.is_pricing_disclaimer_signed(None) is False


def test_is_pricing_disclaimer_signed_exception_is_false(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("db.connection.get_db", _boom)
    assert aa.is_pricing_disclaimer_signed(9) is False
